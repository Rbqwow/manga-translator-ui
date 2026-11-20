# MCP 二次复审修复计划（Plan）

- **基准提交**：
  - `cffacd34afb4219fa1562c319cce9d51b54f8946`（`feat: Antigravity MCP`）
  - `3081dfcf29c1ec2eed7d578aa42089a8d3886c90`（`refactor: not only Antigravity`）
  - `91351eaf87236fe9147401386bf433e5f5286e0d`（`fix: review`）
- **改造目标**：修复二次复审中发现的 HQ 预览渲染失败静默丢弃、页面 shutdown 信号断连遗漏、预览池回写竞态等低优先级问题，并补齐对应回归测试与此前遗留的跨客户端真机验收项。
- **核心原则**：不破坏现有 MCP 工具契约与 0-API 硬隔离设计；所有协议改动仅做**可选字段增量**，向后兼容已接入的多 Harness 客户端。

---

## 一、二次复审问题清单

| 编号 | 优先级 | 类别 | 涉及文件与位置 | 问题描述 |
| --- | --- | --- | --- | --- |
| R2-1 | 低 | 协议可观测性 | `manga_translator/mcp/job_manager.py:1106-1125`、`1154-1157`；`manga_translator/mcp/protocol_models.py:86-98` | HQ 模式下若某页 `render_numbered_page_preview` 抛错（`preview_error` 置位），该页从 `encoded_pages` 缺席，最终 `PageBatchItem.image_delivery_mode` 静默降级为 `"none"`，客户端 LLM 收不到图片也收不到失败原因，无法区分「预览生成失败」与「正常无图」。 |
| R2-2 | 低 | 信号对称性 | `desktop_qt_ui/ui/main_page/pages/mcp_page.py:1112-1125` | `McpPagePanel.shutdown()` 断开了其余 4 个 `McpService` 信号，但漏掉 `models_unload_finished`。因 `closeEvent` 先调本方法再停服、且 Qt 会在对象销毁时自动断连，实际无风险，仅为对称性疏漏。 |
| R2-3 | 低 | 竞态防御 | `manga_translator/mcp/job_manager.py:525-569`、`618-637`、`1106-1125`、`1414-1416`、`1585-1593` | `_finalize_job_locked` 以 `pool.shutdown(wait=False, cancel_futures=True)` 关闭预览池并清空 `p.ctx`/`p.preview_cache` 后，正在执行的 `_render_batch_previews` 仍可能在 finalize 后回写 `preview_cache`/`preview_error`，或对已 `committed` 页面重新写回缓存；此外 `next_translation_batch` 在 `preview_ready.wait()` 被 cancel/finalize 唤醒后未检查终态，会继续走同步兜底渲染并触碰已清空的 `page.ctx`。 |
| R2-4 | 信息 | 设计取舍 | `manga_translator/mcp/job_manager.py:1021-1034` | `max_parallel_batches >= 2` 时 `next_translation_batch` 不排除同一 `worker_id` 已持有的 `LEASED` 批次，单 worker 轮询可同时持有 2 个租约；若其先翻译第二批，第一批可能超时被回收造成返工。Skill 模板已约定各子代理使用不同 `worker_id`，故属可接受设计取舍，记录在案供真机联调观察。 |

### 已评估、无需修改的备忘项

- `get_gui_state_snapshot` 在缓存为 `None` 时的非 GUI 线程回源刷新：`MainAppLogic.__init__` 已初始化缓存，实际不可达；列表/属性读取受 GIL 保护且由 `_source_snapshot_lock` 串行化，无实际风险。
- `unload_models_async` 若在 `_shutdown_done` 后完成则不补发信号、按钮不复位：仅发生在应用退出过程中的极端窗口，主窗口即将销毁，无修复价值。
- `next_translation_batch` 等待期占用 anyio 工作线程（≤25s）：并发受 `max_parallel_batches ≤ 4` 约束，线程池上限 40，无耗尽风险。

---

## 二、分项修复方案

### 2.1 HQ 预览渲染失败随批次下发（R2-1）

1. **协议增量**：在 `PageBatchItem` 中新增可选字段 `preview_error: Optional[str] = None`（`protocol_models.py`）。新增可选字段不破坏既有客户端解析。
2. **状态回填与锁同步**：在 `McpTranslationJob.next_translation_batch()`（`job_manager.py:1112-1125`）中将 `page.preview_cache` / `page.preview_error` 的读取与兜底回写纳入 `with self._cond:`（仅在锁外执行 `render_numbered_page_preview` 本身），避免 `preview_ready.wait(timeout=30.0)` 超时时与预览池线程产生读写竞态；在组装 `PageBatchItem` 处（`1154-1157` 的 `else` 分支），当 `d_info is None` 时填入 `preview_error=page.preview_error`（无错误且无图的情形维持 `None`，语义不变）。
3. **hints 提示**：若本批存在 `preview_error` 的页面，向 `hints` 追加一条汇总提示（如 `Preview rendering failed for pages [...]; translate these pages from OCR text and prev_context.`），使纯文本回退行为对客户端显式可见。
4. **按需看图路径同步**：`get_page_preview` 失败以异常抛出、客户端可感知，无需改动；仅覆盖批次附图这一静默路径。

### 2.2 补齐 `McpPagePanel.shutdown()` 信号断连（R2-2）

在 `mcp_page.py` 的 `shutdown()` 断连元组列表中加入：
```python
(self.mcp_service.models_unload_finished, self._on_models_unload_finished),
```

### 2.3 预览池与兜底路径的终态竞态防御（R2-3）

1. **`_render_batch_previews()`（`job_manager.py:618-637`）**：渲染前先在 `self._cond` 下检查终态/提交状态并快照 `ctx`、`text_order`、`filename`；成功与失败回写统一收拢到 `finally` 的一次 `with self._cond:` 内，同时阻止对已 finalize、已取消或已 `committed` 页面的回写：

```python
def _render_batch_previews(self, batch: McpBatchState) -> None:
    for p_idx in batch.page_indices:
        page = self.pages[p_idx]
        with self._cond:
            if self._finalized or self.cancel_event.is_set() or page.committed or page.ctx is None:
                page.preview_ready.set()
                continue
            ctx = page.ctx
            text_order = list(page.text_order)
            filename = page.filename

        encoded = None
        err_msg: Optional[str] = None
        try:
            encoded = render_numbered_page_preview(
                ctx,
                text_order,
                max_size=self.preview_max_size,
                fmt=self.preview_format,
                quality=self.preview_quality,
            )
        except Exception as exc:
            err_msg = str(exc)
            logger.warning(f"[MCP] Preview render failed for {filename}: {exc}")
        finally:
            # Finalize/commit may have already cleared preview buffers; do not resurrect them.
            with self._cond:
                if not self._finalized and not self.cancel_event.is_set() and not page.committed:
                    if encoded is not None and page.preview_cache is None and page.preview_error is None:
                        page.preview_cache = encoded
                    elif err_msg is not None and page.preview_error is None:
                        page.preview_error = err_msg
            page.preview_ready.set()
```

2. **`next_translation_batch()` 同步兜底路径（`job_manager.py:1108-1125`）**：在 `page.preview_ready.wait(timeout=30.0)` 返回后，先在 `with self._cond:` 内检查 `self.cancel_event.is_set() or self._finalized`（若已终止则直接返回 `done=True` 的完成摘要，不再调用同步兜底渲染或 `build_page_region_items(page.ctx, ...)`）；若需要同步兜底渲染，同样先快照 `ctx`/`text_order` 再出锁渲染，并在 `with self._cond:` 终态检查后回写 `preview_cache` / `preview_error`。
3. **顺带清理 `cancel()` 冗余逻辑（`job_manager.py:1585-1592`）**：移除 `cancel()` 中调用 `_finalize_job_locked()` 前重复清理 `p.ctx` 的循环（该循环未置 `p.ctx = None`，且 `_finalize_job_locked()` 已统一完成清理与置空）。

### 2.4 同 worker 双租约（R2-4，可选项、默认不改）

- **默认**：保持现状，在 `skill_template.md` 的 Parallel Workers 小节补一句说明（同 worker 重复轮询可能额外占用租约，请各子代理使用独立 `worker_id` 且不重复轮询）。
- **可选严格化（如真机联调发现租约浪费再启用）**：在 `next_translation_batch` 领批前检查 `worker_key` 是否已持有 `LEASED` 批次，若持有则返回 `waiting=True, waiting_reason="worker_already_leases_batch"`，并在 `hints` 中明确带上该 worker 当前持有的 `batch_id`（提示先提交或调用 `release_batch`），防止客户端按通用 `waiting=True` 规则空转等待至租约过期；经确认不会引入单 worker 串行死锁（`submit_translation_batch(..., return_next=True)` 提交后原批次已非 `LEASED`，可安全领下一批）。

---

## 三、改动文件清单

| 文件路径 | 涉及修复项 |
| --- | --- |
| `manga_translator/mcp/protocol_models.py` | R2-1（`PageBatchItem.preview_error` 可选字段） |
| `manga_translator/mcp/job_manager.py` | R2-1（回填 `preview_error` 并追加 hints）、R2-3（预览池回写终态防御） |
| `desktop_qt_ui/ui/main_page/pages/mcp_page.py` | R2-2（断连列表补 `models_unload_finished`） |
| `manga_translator/mcp/skill_template.md` | R2-4（默认方案：补充 worker_id 使用说明） |
| `test/test_mcp_pipeline.py` | R2-1、R2-3 回归测试 |

---

## 四、分阶段实施步骤

1. **Phase 1 — 协议与调度层修复（R2-1、R2-3）**
   - `protocol_models.py` 新增 `PageBatchItem.preview_error` 字段。
   - `job_manager.py` 在 `next_translation_batch` 补齐 `preview_cache`/`preview_error` 锁同步、终态早退（`cancel`/`finalize` 唤醒时直接返回 `done=True`）、回填 `preview_error` 并追加 hints；在 `_render_batch_previews` 增加渲染前终态/`committed` 检查、上下文快照与 `finally` 统一加锁回写；顺带移除 `cancel()` 中重复清理 `p.ctx` 的冗余循环。
2. **Phase 2 — UI 与模板小修（R2-2、R2-4 默认方案）**
   - `mcp_page.py` 补齐信号断连。
   - `skill_template.md` 补充 Parallel Workers 小节的 `worker_id` 使用与轮询说明。
3. **Phase 3 — 自动化回归测试**
   - 在 `test/test_mcp_pipeline.py` 新增：
     - HQ 模式下注入 `render_numbered_page_preview` 单页失败（monkeypatch 抛错），断言失败页 `PageBatchItem.preview_error` 非空、`image_delivery_mode == "none"`、正常页 `preview_error is None`、`hints` 含预览失败提示，且任务其余页面照常完成；
     - R2-3 防御：用事件同步模拟 finalize 先于预览线程写回，同时覆盖**正常返回**与**抛错返回**两种路径，断言 finalize 后 `preview_cache` 与 `preview_error` 均保持为 `None` 且 `preview_ready.is_set()` 为 `True`；补充测试 `next_translation_batch` 在 `preview_ready.wait()` 期间被 cancel/finalize 唤醒时直接返回 `done=True` 且不触发兜底渲染。
   - 运行 `pytest test/test_mcp_pipeline.py test/test_mcp_server.py` 全部通过。
4. **Phase 4 — 跨客户端真机联调（前几份 Plan 遗留验收项，一并收口）**
   - 抽测至少 2 个不同 MCP 客户端（Claude Code / Codex CLI / Cursor / OpenCode / Antigravity），验证 Streamable HTTP 握手、`tools/list`、`tools/call` 全链路。
   - 验证 Electron 类桌面宿主发送的 `Origin` 头是否触发 `TransportSecuritySettings` 的 `403`；如触发，仅在 `server_app.py` 中对实测到的 `Origin` 做白名单适配（并可顺带补齐 `https://[::1]:*` 对称项），不关闭 `enable_dns_rebinding_protection` 或放宽 `allowed_hosts` 的 loopback 限制。
   - 端到端实测 `hq`、`text_on_demand`、`text_only`、`retranslate_json`、断点续传、UI「立即卸载显存」及运行中关窗保护；全程 0 外部 LLM API 调用。
   - 联调期间观察 R2-4 场景（`max_parallel_batches >= 2`）是否出现租约超时返工，据实决定是否启用可选严格化。

---

## 五、验收标准

- HQ 模式下预览渲染失败页在批次负载中携带 `preview_error` 与 `hints` 提示，客户端可明确区分「预览失败」与「正常无图」；协议改动为可选字段增量，旧客户端不受影响。
- `McpPagePanel.shutdown()` 与 `McpService` 的全部 5 个信号一一断连。
- finalize、cancel 或页面 commit 之后无任何预览池或兜底线程回写 `page.preview_cache` / `page.preview_error`，且 `next_translation_batch` 被 cancel/finalize 唤醒时安全返回 `done=True`、不触碰已清空的 `page.ctx`。
- `pytest test/test_mcp_pipeline.py test/test_mcp_server.py` 全部通过（含新增回归用例），无新增告警。
- 完成至少 2 个真实 MCP 客户端的端到端漫画翻译链路验证，并记录 `Origin` 头兼容性结论。
