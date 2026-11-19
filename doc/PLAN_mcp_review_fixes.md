# MCP 代码复审修复计划（Plan）

- **基准提交**：
  - `cffacd34afb4219fa1562c319cce9d51b54f8946`（`feat: Antigravity MCP`）
  - `3081dfcf29c1ec2eed7d578aa42089a8d3886c90`（`refactor: not only Antigravity`）
- **改造目标**：修复上述两次提交复审中发现的线程/内存泄漏、Qt 跨线程回调失效、GPU 互斥竞态、`retranslate_json` 误跳过及收尾/关窗边界问题，并补齐回归测试与真机联调验收项。
- **核心原则**：不破坏现有 0-API 硬隔离、单 GPU 优先队列与多 Harness 通用接入设计，保持对外 MCP 工具契约向后兼容。

---

## 一、复审问题清单

| 编号 | 优先级 | 类别 | 涉及文件与位置 | 问题描述 |
| --- | --- | --- | --- | --- |
| P0-1 | 高 | Qt 线程安全 | `desktop_qt_ui/ui/main_page/pages/mcp_page.py:884-908`、`desktop_qt_ui/services/mcp_service.py:424-436` | `unload_models_async` 在普通 Python 线程中执行回调，而回调使用无 receiver 的 `QTimer.singleShot(0, lambda)`，导致回调永不触发、“立即卸载显存”按钮永久置灰 |
| P0-2 | 高 | 资源泄漏 | `manga_translator/mcp/job_manager.py:591, 664-685, 1480, 1511` | 任务正常完成时从未关闭 `_preview_pool` 线程池（仅 `cancel()` 关闭），且 `McpJobManager._jobs` 无限累积历史任务对象（持有 `translator`/`plan`/`pages` 引用） |
| P0-3 | 高 | 并发竞态 | `manga_translator/mcp/job_manager.py:1573-1715` | `create_translation_job` 检查互斥后即释放 `_lock`，在目录扫描/压缩包解压期间未占位，并发创建任务或同时点击 GUI 翻译可绕过 GPU 互斥 |
| P0-4 | 高 | 功能逻辑 | `manga_translator/mcp/job_manager.py:1686-1687`、`manga_translator/mcp/batch_adapter.py:113` | `retranslate_json=True` 且默认 `overwrite=False` 时，`plan_batch_inputs` 会将已有输出图全部跳过，导致重翻模式直接得到 `pending_pages=0` |
| P1-1 | 中 | 状态机边界 | `manga_translator/mcp/job_manager.py:1043-1056` | 收尾等待 `_finished_event.wait(timeout=120.0)` 超时后仍强行返回 `done=True`，重修图或无损编码超 120s 时误判完成 |
| P1-2 | 中 | 关窗死锁 | `desktop_qt_ui/app_logic.py:2139-2150`、`desktop_qt_ui/ui/main_window.py:981-982` | `closeEvent` 同步 `join` HTTP 线程时，若在途 MCP 请求调用 `BlockingQueuedConnection` 读取 GUI 快照，主线程与 HTTP 线程互等直至 5s 超时 |
| P1-3 | 中 | 失败清理 | `manga_translator/mcp/job_manager.py:854-869, 1372-1388` | P2 预修图在 `LEASED` 阶段失败时未释放 `page.ctx`，且若整批均在预修阶段失败，批次状态滞留 `COMMITTED` 而非 `FAILED` |
| P1-4 | 中 | 协议格式 | `manga_translator/mcp/server_app.py:293, 360`、`desktop_qt_ui/services/mcp_service.py:374, 391, 456` | `McpSettings` 允许 `host="::1"`，但 URL 拼接未给 IPv6 地址加方括号，会生成非法地址 `http://::1:8765/mcp` |
| P2-1 | 低 | 配置合并 | `desktop_qt_ui/services/mcp_service.py:160-209` | Codex `config.toml` 若使用内联表 `mcp_servers = { manga-translator = ... }` 写法，合并报错提示不够直观 |
| P2-2 | 低 | 停服清理 | `manga_translator/mcp/server_app.py:318-350` | 停服时 `sse_starlette` 的 `_shutdown_watcher` 偶发 `Task was destroyed but it is pending` 告警 |

---

## 二、分项修复方案

### 2.1 高优先级修复（P0）

1. **修复显存卸载异步回调跨线程投递（P0-1）**
   - 方案 A（推荐）：在 `McpService` 新增信号 `models_unload_finished = pyqtSignal(bool, str)`，在 `unload_models_async` 线程结束时 `emit(True, "")` 或 `emit(False, str(exc))`，`McpPagePanel` 通过 `Qt.ConnectionType.QueuedConnection` 接收后恢复按钮状态并弹 Toast。
   - 方案 B（最小改动）：保留现有回调，但将 `mcp_page.py` 中的 `QTimer.singleShot(0, lambda: ...)` 改为 `QTimer.singleShot(0, self, lambda: ...)`。

2. **修复 `_preview_pool` 线程泄漏与 `_jobs` 历史对象堆积（P0-2）**
   - 在 `McpTranslationJob._finalize_job_locked()` 中，记录并置空 `pool = self._preview_pool; self._preview_pool = None`，在锁外（或 finish 钩子线程中）调用 `pool.shutdown(wait=False, cancel_futures=True)`。
   - 任务正常收尾时清理所有 `page.preview_cache = None` 与 `page.ctx = None`。
   - 在 `McpJobManager` 中维护 `_completed_job_summaries: OrderedDict[str, Dict[str, Any]]`（上限如 20 条）；任务完成后在 `_handle_job_finished` 中缓存终态摘要并从 `_jobs` 释放重量级 `McpTranslationJob` 实例（或仅保留最近 1 个终态对象以兼容紧随其后的 `get_job_status` 查询）。

3. **消除 `create_translation_job` GPU 互斥 TOCTOU 竞态（P0-3）**
   - 在 `McpJobManager.__init__` 增加 `self._creating_job: bool = False`。
   - 在 `has_active_job()` 中若 `self._creating_job` 为 `True` 同样返回 `True`（使 GUI `start_backend_task` 和并发 MCP 建任务都会被立即拦截）。
   - 在 `create_translation_job()` 开头持 `self._lock` 校验并置 `self._creating_job = True`，用 `try ... finally` 包裹整个建任务流程，在 `finally` 中持锁复位 `self._creating_job = False`。

4. **修复 `retranslate_json=True` 被 `overwrite=False` 误跳过（P0-4）**
   - 在 `McpJobManager.create_translation_job()` 中：若 `retranslate_json=True` 且调用方未显式传入 `overwrite`（即 `overwrite is None`），将传给 `build_mcp_engine_config` 的 `overwrite` 设为 `True`，确保已有成品图不会阻断基于现有 JSON 的重翻流程；若调用方显式传入 `overwrite=False`，仍尊重显式参数。

### 2.2 中优先级修复（P1）

5. **收尾渲染等待超时返回 `waiting=True` 而非误报 `done=True`（P1-1）**
   - 在 `McpTranslationJob.next_translation_batch()` 中，将 `self._finished_event.wait(timeout=120.0)` 改为检查返回值：
     - 若返回 `False`（尚未落盘收尾完成），返回 `BatchPayload(done=False, waiting=True, waiting_reason="finalizing_render", job_id=self.job_id, progress=progress)`，客户端可继续轮询直至真正完成。

6. **移除关窗期 `BlockingQueuedConnection` 死锁风险（P1-2）**
   - 在 `MainAppLogic` 中维护受 `_source_snapshot_lock` 保护的不可变缓存快照 `_cached_gui_snapshot`，在初始化及 `source_files` / `excluded_subfolders` / `excluded_files` / 输出路径变更时于主线程同步刷新该缓存。
   - `get_gui_state_snapshot()` 直接加锁返回缓存拷贝，不再调用 `QMetaObject.invokeMethod(..., BlockingQueuedConnection)`，彻底消除 `MainWindow.closeEvent` 期间的 5 秒互等卡死。

7. **补全 P2 预修图失败页的上下文释放与整批失败状态更新（P1-3）**
   - 在 `McpTranslationJob.submit_translation_batch()` 标记 `page.committed = True` 时，若 `page.failed` 已为 `True` 且 `page.ctx is not None`，立即调用 `self.translator._cleanup_context_memory(page.ctx, keep_result=False)` 并置 `page.ctx = None`。
   - 紧接 `batch.status = "COMMITTED"` 后检查：若 `all(self.pages[i].rendered or self.pages[i].failed for i in batch.page_indices)`，直接将 `batch.status` 置为 `"FAILED"`（或 `"DONE"`）、调用 `_delete_batch_preview_files(batch)` 并检查 `_check_all_done_locked()`。

8. **统一 IPv6 回环地址 URL 格式化（P1-4）**
   - 新增公共辅助函数 `format_mcp_endpoint_url(host: str, port: int) -> str`：当 `":" in host and not host.startswith("[")` 时返回 `f"http://[{host}]:{port}/mcp"`，否则返回 `f"http://{host}:{port}/mcp"`。
   - 在 `server_app.py`（`endpoint_url` / `start`）与 `mcp_service.py`（`endpoint_url` / `start_server` / `_resolved_endpoint_url`）统一复用。

### 2.3 低优先级优化（P2）

9. **改善 Codex TOML 内联表报错提示（P2-1）**
   - 在 `_merge_codex_toml_config` 捕获合并后 `tomllib.TOMLDecodeError` 时，若原文本中存在 `mcp_servers` 内联表定义，给出明确提示建议改写为标准 `[mcp_servers.manga-translator]` 区块。

10. **清理停服时的异步 Pending Task 告警（P2-2）**
    - 在 `McpHttpServer.start()` 内部 `_serve()` 的 `finally` 块中，关闭循环前取消并等待所有残余 `asyncio.all_tasks(loop)`（带短超时），消除 `sse_starlette` 的 `Task was destroyed but it is pending` 警告。

---

## 三、改动文件清单

| 文件路径 | 涉及修复项 |
| --- | --- |
| `desktop_qt_ui/services/mcp_service.py` | P0-1（信号化显存卸载结果）、P1-4（IPv6 URL 格式化）、P2-1（Codex TOML 提示优化） |
| `desktop_qt_ui/ui/main_page/pages/mcp_page.py` | P0-1（改用信号或带 receiver 的 `QTimer.singleShot`） |
| `desktop_qt_ui/app_logic.py` | P1-2（GUI 状态不可变缓存快照，移除 `BlockingQueuedConnection`） |
| `manga_translator/mcp/job_manager.py` | P0-2（关闭 `_preview_pool` 与 `_jobs` 裁剪）、P0-3（`_creating_job` 互斥占位）、P0-4（`retranslate_json` 默认覆盖）、P1-1（`finalizing_render` 等待态）、P1-3（预修失败页清理与整批状态更新） |
| `manga_translator/mcp/server_app.py` | P1-4（IPv6 URL 格式化）、P2-2（停服前取消残余 pending tasks） |
| `test/test_mcp_pipeline.py` | 新增 P0-2、P0-3、P0-4、P1-1、P1-3、P1-4 回归单测 |
| `test/test_mcp_server.py` | 验证停服无 pending task 告警及 IPv6/回环端点格式 |

---

## 四、分阶段实施步骤

1. **Phase 1 — 高优先级稳定性修复（P0-1 ~ P0-4）**
   - 修复 `mcp_page.py` / `mcp_service.py` 卸载显存回调线程问题。
   - 修复 `job_manager.py` 的 `_preview_pool` 释放、`_jobs` 裁剪、`_creating_job` 并发互斥及 `retranslate_json` 覆盖策略。
2. **Phase 2 — 中低优先级边界加固（P1-1 ~ P1-4, P2-1 ~ P2-2）**
   - 修复 `job_manager.py` 收尾等待超时状态与 P2 预修失败页清理。
   - 将 `app_logic.py` 的 GUI 快照改为无阻塞缓存读取。
   - 统一 `server_app.py` 与 `mcp_service.py` 的 IPv6 URL 格式化、TOML 错误提示及停服异步任务清理。
3. **Phase 3 — 自动化回归测试**
   - 在 `test/test_mcp_pipeline.py` 补充以下测试用例：
     - 任务正常完成后 `_preview_pool` 已关闭且无残留 `mcp-preview-*` 线程；
     - 并发调用 `create_translation_job`（模拟扫描耗时）只允许 1 个成功、其余被互斥拒绝；
     - 输出图已存在且 `overwrite=False` 时，`create_translation_job(retranslate_json=True)` 仍能正常进入待翻队列；
     - 收尾等待超时时 `next_translation_batch` 返回 `done=False, waiting=True, waiting_reason="finalizing_render"`；
     - P2 预修图在 `LEASED` 阶段抛错后，提交批次时能释放 `ctx` 且批次状态正确收敛；
     - `host="::1"` 生成 `http://[::1]:<port>/mcp`。
   - 运行 `pytest test/test_mcp_pipeline.py test/test_mcp_server.py` 全部通过且无 pending task 告警。
4. **Phase 4 — 跨客户端真机联调（补齐前两份 Plan 待验收项）**
   - 抽测至少 2 个不同 MCP 客户端（如 Claude Code / Codex CLI / Cursor / OpenCode / Antigravity）。
   - 验证 Electron 类桌面宿主发送的 `Origin` 头是否触发 `TransportSecuritySettings` 的 `403`，如触发则按需放宽或做针对性白名单适配。
   - 端到端实测 `hq`、`text_on_demand`、`text_only`、`retranslate_json`、断点续传、UI“立即卸载显存”及运行中关窗保护。

---

## 五、验收标准

- `pytest test/test_mcp_pipeline.py test/test_mcp_server.py` 全部通过（含新增回归用例），输出中无 `Task was destroyed but it is pending` 告警。
- 连续执行多轮 MCP 任务后，`mcp-preview-*` 线程数不随任务数增长，历史任务不滞留大图像素缓冲。
- GUI 点击“立即卸载显存”后按钮正常恢复并弹出完成/错误 Toast；有在途 MCP 请求时关闭主窗口不卡死。
- `retranslate_json=True` 在默认配置下可直接重翻已有输出目录的漫画页。
- 至少 2 个真实 MCP 客户端完成端到端漫画翻译链路验证，全过程 0 外部 LLM API 调用。
