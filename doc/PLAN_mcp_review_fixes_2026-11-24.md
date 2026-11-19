# MCP 复审修复计划（基于 `4b2e98bc feat: MCP`）

- **基准提交**：`4b2e98bc8823e96a7016f414a98a9dba7ad9eb61` (`feat: MCP`)
- **制定日期**：2026-11-24
- **目标**：系统性修复复审发现的 P1（并发竞态 / 生命周期安全 / 语义冲突）、P2（功能边界 / 启动性能 / 配置写入）与 P3（体验与卫生项），并补齐对应回归测试。

---

## 总体实施顺序与依赖关系

建议按 5 个阶段串行推进，每阶段完成后即可独立跑通测试：

1. **Phase 1（P1 核心状态机与并发安全）**：修复 `McpJobManager` / `McpTranslationJob` 的上下文顺序、术语增量、空闲卸载语义、`ctx` 生命周期、租约保护与超时取消。
2. **Phase 2（P1 传输与渲染隔离）**：修复 `McpHttpServer.stop()` 强制退出与非阻塞 UI 启停，以及共享 `MangaTranslator` 调试上下文并发竞争。
3. **Phase 3（P2 管线边界与配置写入）**：修复 `retranslate_json` 字号缩放一致性、全页失败终态、初始化期取消、投机修复失败重领、压缩包 `save_to_source_dir` 产物保护、客户端配置写入边界。
4. **Phase 4（P2 启动懒加载优化）**：消除 `desktop_qt_ui.services` 启动时急切导入 `cv2` / `uvicorn` / `mcp` / `starlette`。
5. **Phase 5（P3 体验打磨与全量回归）**：精简无关 API 冲突误报、完善 UI 打开实际输出目录、过期租约展示同步、多语言词条补齐与测试清理。

---

## Phase 1：任务管理器与并发安全（P1-1 ~ P1-4）

### 1.1 修复 `models_idle_ttl = 0` 语义与 UI 提示相反
- **涉及文件**：
  - `manga_translator/mcp/job_manager.py`（`McpJobManager._schedule_idle_cleanup_locked`）
  - `test/test_mcp_pipeline.py`（`test_delayed_finish_cancel_idle_dedup_and_all_skipped_ui_progress`）
- **设计决策**：
  - 严格遵守 `zh_CN.json` / `en_US.json` 的用户契约：`models_idle_ttl == 0` 表示 **MCP 任务完成后不自动卸载模型**（手动点击“卸载模型”或调用 `unload_models()` 工具仍可用）。
  - 在 `_schedule_idle_cleanup_locked()` 中移除 `elif bool(app_cfg.get("unload_models_after_translation", True)):` 回退分支：仅当 `ttl > 0` 时启动 `threading.Timer(ttl, self._idle_timer_fired)`。
  - *(备选：若产品上坚持 `0` 表示跟随 `app.unload_models_after_translation`，则改为保留逻辑并同步修改 `zh_CN.json`、`en_US.json` 中 `MCP Idle TTL Hint` 的描述，避免误导用户。)*
- **测试更新与新增**：
  - 更新 `test_delayed_finish_cancel_idle_dedup_and_all_skipped_ui_progress`：验证 `models_idle_ttl=0` 且 `unload_models_after_translation=True` 时 **不触发**自动卸载；再以极短正 TTL（或 monkeypatch `threading.Timer` 触发）验证 `cancel_job` + 延迟 `_handle_job_finished` 去重后最多卸载一次。

### 1.2 修复 `prev_context` 选入未来页的问题
- **涉及文件**：
  - `manga_translator/mcp/job_manager.py`（`McpTranslationJob._build_prev_context_locked`）
- **具体修改**：
  - 计算 `min_batch_order = min(batch_orders)`（`batch_orders` 为空时直接返回 `[]`）。
  - 过滤条件改为 `order < min_batch_order and self._translation_history.get(order)`，确保任何跳过页或并发乱序提交的后续页都不会进入 `prev_context`。
  - 保留 `"page_index": order` 字段名以兼容既有协议与测试，同时补充 `"source_order": order`（或在 `skill_template.md` 注明 `prev_context[].page_index` 为整书阅读顺序编号而非待翻队列下标）。
- **新增测试**：
  - 在 `test/test_mcp_pipeline.py` 增加用例：后半段存在跳过页（如 p3、p4 已完成，p1、p2 待翻）时，p1 的 `prev_context` 必须为空，不能包含 p3/p4；并发乱序下 Batch 2 先提交后 Batch 1 重领时，Batch 1 的 `prev_context` 不得包含 Batch 2。

### 1.3 修复多并发 Worker 场景 `glossary_delta` 漏发
- **涉及文件**：
  - `manga_translator/mcp/job_manager.py`（`McpTranslationJob.__init__`、`_collect_glossary_delta_locked`、`submit_translation_batch`）
  - `manga_translator/translators/common.py`（可选：确认是否需要返回实际合并列表/数量，或由调用方过滤）
- **具体修改**：
  - 将 `_glossary_events` 元素结构改为 `(version: int, author_worker_key: Optional[str], terms: List[Dict[str, Any]])`。
  - 提交新词成功后，**不再**直接把 `self._worker_glossary_seen[worker_id] = self._glossary_version` 跳到最新全局版本；改为在 `_collect_glossary_delta_locked(worker_key)` 中按 `ver > seen_ver and author != worker_key` 收集增量，并更新 `self._worker_glossary_seen[worker_key] = self._glossary_version`。
  - （可选增强）在 `_glossary_events` 仅记录规范化且实际触发合并的新词条目，避免把无效/重复词条广播给其他 Worker。
- **新增测试**：
  - 双 Worker 交错提交场景：W1、W2 在 `v=0` 同时领批次 -> W2 提交 `TermB`（`v=1`）-> W1 提交 `TermA`（`v=2`）-> W1 下一次 `next_translation_batch` 必须收到 `TermB` 且不重复收到 `TermA`。

### 1.4 消除 `page.ctx` 跨线程释放竞态、租约预览期失守与 `cancel()` 超时放锁
- **涉及文件**：
  - `manga_translator/mcp/job_manager.py`（`McpPageState`、`McpBatchState`、`_render_batch_previews`、`next_translation_batch`、`get_page_preview`、`_reclaim_expired_leases_locked`、`_finalize_job_locked`、`cancel`、`_run_gpu_worker`、`_run_render_worker`、`McpJobManager.has_active_job`、`McpJobManager.cancel_job`）
- **具体修改**：
  1. **`ctx` 读引用计数保护**：
     - 在 `McpPageState` 增加 `preview_readers: int = 0`（受 `self._cond` 保护）。
     - `_render_batch_previews`、`next_translation_batch` 回退渲染、`get_page_preview` 在锁内校验 `not self._finalized and not page.committed and page.ctx is not None` 后执行 `page.preview_readers += 1`，在 `finally` 块中锁内执行 `page.preview_readers -= 1` 并 `notify_all()`。
     - `RenderWorker` 在调用 `render_and_save_page` 前、以及 `_finalize_job_locked()` 清理 `p.ctx` 前，确认 `p.preview_readers == 0`（或等待短暂超时并在 `preview_readers` 归零时由最后一个释放者完成 `_cleanup_context_memory`），彻底避免正在读 `ctx.input` 时被 `close()`。
  2. **预览生成期租约保护与刷新**：
     - 在 `McpBatchState` 增加 `preparing: bool = False`。
     - `next_translation_batch` 选中 `READY` 批次置 `LEASED` 时同步置 `preparing = True`；`_reclaim_expired_leases_locked()` 跳过 `batch.preparing or batch.submitting` 的批次。
     - `next_translation_batch` 在完成预览准备重新加锁后、构造并返回 `BatchPayload` 前，校验 `batch.status == "LEASED" and batch.worker_id == worker_key` 且未被取消，然后刷新 `batch.leased_at = time.monotonic()`，并在 `finally` 中置 `batch.preparing = False`。
  3. **`cancel()` 超时后保持 GPU 互斥直到工作线程真正退出**：
     - 在 `McpTranslationJob` 增加 `is_worker_alive() -> bool`（检查 `_gpu_thread` / `_render_thread` 是否存活）与 `_deferred_finalize: bool`。
     - `cancel(join_timeout=5.0)` 若 `join` 超时且 `_gpu_thread` 或 `_render_thread` 仍存活：
       - 不立即清理正在被线程占用的 `page.ctx` 与 `archive_to_temp_map`，标记 `_deferred_finalize = True`，由 `_run_gpu_worker` / `_run_render_worker` 退出前的 `finally` 块在两线程均停机后完成 `_finalize_job_locked()`。
       - `McpJobManager.has_active_job()` 与 `unload_models()` 额外检查是否有处于 `cancelling` 且工作线程仍存活的任务，防止新任务或模型卸载与仍在推理中的旧 GPU 线程撞车。
- **新增测试**：
  - `get_page_preview` 阻塞期间并发触发 `cancel_job` 或 `submit_translation_batch`，验证不会抛出 `Operation on closed image` / `AttributeError`，且最终 `ctx` 被正确释放。
  - HQ 预览渲染耗时超过 `lease_timeout_seconds` 时，另一 Worker 不会抢走正在准备中的批次，原 Worker 拿到的 `leased_at` 为返回时刻。
  - 模拟慢速 GPU 阶段超过 `cancel(join_timeout=0.05)`，验证取消期间 `has_active_job()` 仍阻止新任务与 `unload_models()`，直到 GPU 线程退出后才完成清理并解除互斥。

---

## Phase 2：HTTP 服务生命周期与共享 `MangaTranslator` 隔离（P1-5 ~ P1-6）

### 2.1 修复 `McpHttpServer.stop()` 遗留孤儿线程、UI 同步卡顿与停服孤立任务
- **涉及文件**：
  - `manga_translator/mcp/server_app.py`（`McpHttpServer.start`、`McpHttpServer.stop`）
  - `desktop_qt_ui/services/mcp_service.py`（`McpService.stop_server`、新增异步启停或回调机制）
  - `desktop_qt_ui/ui/main_page/pages/mcp_page.py`（`_on_toggle_server_clicked`、按钮禁用态）
- **具体修改**：
  1. 在 `uvicorn.Config` 中传入 `timeout_graceful_shutdown=2.0`。
  2. 在 `McpHttpServer.stop(timeout=5.0)` 中：
     - 先置 `server.should_exit = True`，等待最多 `min(timeout, 2.5)` 秒；
     - 若 `thread.is_alive()` 仍为真，置 `server.force_exit = True` 并继续等待剩余时间；
     - **仅当 `not thread.is_alive()` 时**才调用 `_cleanup_stopped_refs_locked()`；若仍存活则记录错误并返回/抛异常，不伪装成已停止。
  3. 停服与活跃任务联动：
     - `McpService.stop_server(cancel_active_job: bool = True)`（或在 UI 弹窗确认后先取消活跃 MCP 任务）先中断等待中的 `McpTranslationJob`（从而立刻唤醒 `next_translation_batch` / `submit_translation_batch` 长轮询），再停止 HTTP 服务，避免长轮询卡死停服或留下永久阻塞 GUI 的僵尸任务。
  4. `McpPagePanel._on_toggle_server_clicked` 改为异步线程调用（启停期间禁用切换按钮，通过既有 `server_state_changed` 信号恢复按钮并更新状态），不再在 Qt 主线程阻塞最长 5 秒。
- **新增测试**：
  - 在 `test/test_mcp_server.py` 增加测试：客户端正在长轮询 `next_translation_batch` 时调用 `server.stop()`（配合取消任务），验证在限定时间内干净退出、无残留 `mcp-http-server` 线程，且立即可以在同端口重新 `start()`。

### 2.2 隔离 `GpuPriorityWorker` 与 `RenderWorker` 对共享 `MangaTranslator` 的状态修改
- **涉及文件**：
  - `manga_translator/manga_translator.py`（`_set_image_context`、`_get_image_subfolder`、`_save_current_image_context`、`_restore_image_context`、`_run_text_rendering`）
  - `manga_translator/mcp/batch_adapter.py`（`preprocess_page`、`render_and_save_page`）
- **具体修改**：
  - 将 `MangaTranslator._current_image_context` 改为通过 `threading.local()` 存取（对外保留 `_current_image_context` 属性 getter/setter 以完全向后兼容所有既有单线程/异步调用方），并对 `_saved_image_contexts` 读写加轻量 `threading.Lock`。
  - `render_and_save_page` 中若 `verbose=True`，使用 `config.model_copy(deep=False)`（或独立的 `_chinese_linebreak_debug_records`）避免并发写共享 `config` 属性。
- **新增测试**：
  - 多页 `verbose=True` + `batch_concurrent=True` 流水线并发预处理与渲染测试，验证每页的调试输出目录互相独立不串写。

---

## Phase 3：管线边界与配置写入修复（P2-7 ~ P2-12）

### 3.1 统一 `retranslate_json` 的 `skip_font_scaling` 语义
- **涉及文件**：
  - `manga_translator/mcp/batch_adapter.py`（`preprocess_page`、`render_and_save_page`）
- **具体修改**：
  - 与 `MangaTranslator` 的 `translate_json_only`（`manga_translator.py#L920-L921`）保持一致：重译产生的新译文长度与旧译文不同，应重新执行智能字号缩放；因此 `retranslate_json=True` 时显式设置 `ctx.skip_font_scaling = False`，并在 `render_and_save_page` 调用 `translator._run_text_rendering(config, ctx, skip_font_scaling=bool(getattr(ctx, "skip_font_scaling", False)))`，使实际渲染行为与回写到 JSON 的 `skip_font_scaling` 字段完全一致。
- **新增测试**：
  - 在 `test/test_mcp_pipeline.py` 验证 `retranslate_json=True` 对原有 `skip_font_scaling: true` 的 JSON 重译后，渲染传参 `skip_font_scaling` 与写回 JSON 的值一致。

### 3.2 修复全页失败时的任务终态为 `"failed"`
- **涉及文件**：
  - `manga_translator/mcp/job_manager.py`（`McpTranslationJob._finalize_job_locked`）
  - `test/test_mcp_pipeline.py`（`test_p2_inpaint_failure_during_lease_releases_ctx_and_converges_batch`）
- **具体修改**：
  - `_finalize_job_locked` 终态判定改为：
    - 取消 -> `"cancelled"`
    - 存在 `self.error` 或存在失败页：若 `not self.output_paths and (self.error or (self.pages and all(p.failed for p in self.pages)))` -> `"failed"`；否则 -> `"completed_with_errors"`
    - 其余 -> `"completed"`
- **测试更新与新增**：
  - 更新 `test_p2_inpaint_failure_during_lease_releases_ctx_and_converges_batch` 第 1319 行断言为 `assert internal_job.status == "failed"`。
  - 新增 2 页中仅 1 页失败的用例，断言 `status == "completed_with_errors"`。

### 3.3 修复初始化期（`_creating_job=True`）取消任务误命中旧任务
- **涉及文件**：
  - `manga_translator/mcp/job_manager.py`（`McpJobManager.__init__`、`create_translation_job`、`cancel_job`）
  - `desktop_qt_ui/services/mcp_service.py`（`cancel_active_job`）
- **具体修改**：
  - 在 `McpJobManager` 增加 `self._create_cancel_event = threading.Event()`；进入 `create_translation_job` 时清除该事件，在文件展开/压缩包解压后、加载 prompt 后、创建 `McpTranslationJob` 前检查该事件，若已置位则中止创建并走 `except` 清理临时压缩包目录。
  - `cancel_job(job_id=None)` 在 `job_id is None and self._creating_job and self._active_job_id is None` 时置位 `_create_cancel_event`，返回 `{"status": "cancelling", ...}` 而非回退到 `_latest_job_id` 或抛 `KeyError`。

### 3.4 修复投机 P2 inpaint 失败页在批次重领后的处理
- **涉及文件**：
  - `manga_translator/mcp/job_manager.py`（`next_translation_batch`、`submit_translation_batch`、`_reclaim_expired_leases_locked`、`release_batch`）
  - `manga_translator/mcp/batch_adapter.py`（`validate_batch_translations`）
- **具体修改**：
  - `next_translation_batch` 构造 `pages` 时过滤 `page.failed` 页（并在 `hints` 中附带跳过提示），且若批次中所有页均已 `failed`，直接将该批次标记为 `"FAILED"` 并清理 `ctx`，继续挑选下一个 `READY` 批次。
  - `validate_batch_translations` 同步跳过 `getattr(page, "failed", False)` 的页，避免要求客户端为已失败页面提交译文。
- **新增测试**：
  - 2 页批次中第 1 页在投机 P2 inpaint 阶段失败 -> 调用 `release_batch` -> 新 Worker 重领该批次 -> 只收到未失败的第 2 页，提交后批次正常收敛。

### 3.5 修复 `save_to_source_dir=True` + 无 `output_dir` + 压缩包输入的产物被误删
- **涉及文件**：
  - `manga_translator/mcp/job_manager.py`（`create_translation_job`、`_cleanup_temp_artifacts`）
- **具体修改**：
  - 当 `save_to_source_dir=True` 且展开结果包含压缩包（`archive_to_temp_map` 非空）且 `resolved_output_dir` 为空时：
    - 若目标压缩包解压到了临时目录，禁止直接把结果写入临时目录后销毁；改为要么在 `create_translation_job` 阶段将压缩包解压目标基目录设为对应压缩包所在目录（`<archive_parent>`），要么显式报错提示提供 `output_dir`（推荐：**解压仍走临时目录，但计算压缩包输出目录时以原压缩包所在目录为基准，或直接拒绝 `save_to_source_dir=True` 无 `output_dir` 的压缩包任务并给出清晰错误提示**——后者改动最小、与 GUI 路径规则零分歧）。
- **新增测试**：
  - `save_to_source_dir=True`、`output_dir=""` 传入 `.cbz`/`.zip` 压缩包，验证不会出现“任务显示 completed 但输出文件已被删除”的静默数据丢失。

### 3.6 加固客户端配置文件合并（Codex TOML 与 UTF-8 BOM）
- **涉及文件**：
  - `desktop_qt_ui/services/mcp_service.py`（`_merge_codex_toml_config`、`install_mcp_config`）
- **具体修改**：
  - `install_mcp_config` 读取既有 TOML/JSON 文件时统一改用 `encoding="utf-8-sig"`，兼容 Windows 带 BOM 文件。
  - `_merge_codex_toml_config` 更新既有 `[mcp_servers.manga-translator]` 节时，移除同节内冲突的 stdio 键（`command = ...`、`args = [...]`，支持多行数组），保证合并后的配置符合 Codex CLI 的 Streamable HTTP 规范。
- **新增测试**：
  - 在 `test/test_mcp_pipeline.py` 增加带 UTF-8 BOM 的 JSON/TOML 测试，以及从旧 stdio `[mcp_servers.manga-translator]`（含 `command`/`args`）升级为 HTTP `url` 的合并测试。

---

## Phase 4：桌面端冷启动懒加载优化（P2-13）

- **涉及文件**：
  - `manga_translator/mcp/batch_adapter.py`
  - `manga_translator/mcp/__init__.py`
  - `manga_translator/mcp/server_app.py`
  - `desktop_qt_ui/services/mcp_service.py`
- **具体修改**：
  1. `batch_adapter.py`：将顶层 `import cv2`、`import numpy as np`、`from PIL import Image` 移入真正使用的函数内部（类型注解使用 `from __future__ import annotations` + `TYPE_CHECKING`）。
  2. `manga_translator/mcp/__init__.py`：通过 PEP 562 `__getattr__` 延迟导出 `McpHttpServer`、`create_fastmcp_server`、`McpJobManager` 等重型符号；保留纯轻量函数 `find_api_backend_conflicts` / `format_mcp_endpoint_url` 直接导入。
  3. `desktop_qt_ui/services/mcp_service.py`：`McpService.__init__` 不再顶层导入 `mcp`/`uvicorn`/`starlette`，改为在首次 `start_server()` 时延迟创建 `McpHttpServer` 并调用 `configure_mcp_transport_logging()`；`job_manager` 也在首次需要（或延迟属性访问）时实例化，确保 `auto_start=False` 时桌面端启动不加载 `cv2` / `mcp` / `uvicorn` / `starlette`。
- **新增测试**：
  - 子进程冷启动断言：导入 `desktop_qt_ui.services` 并构造 `McpService`（不调用 `start_server`）后，验证 `{"torch", "cv2", "mcp", "uvicorn", "starlette"} & set(sys.modules) == set()`。

---

## Phase 5：P3 体验打磨、多语言补齐与测试清理

1. **避免误拦未启用的副 OCR**：
   - `manga_translator/mcp/batch_adapter.py`（`find_api_backend_conflicts`）：检查 `ocr.secondary_ocr` 时先判断 `bool(ocr_section.get("use_hybrid_ocr", False))`，关闭混合 OCR 时不报冲突。
   - 同步更新 `test/test_mcp_pipeline.py::test_zero_api_enforcement_rejects_external_backends`（`secondary_ocr` 用例置 `use_hybrid_ocr=True`）并补充 `use_hybrid_ocr=False` 不拦截的反向用例。
2. **`get_job_status()` 及时回收过期租约**：
   - `manga_translator/mcp/job_manager.py`（`McpTranslationJob.get_status_dict`）：锁内先调用 `self._reclaim_expired_leases_locked()`，使状态面板与工具返回的 `leases` 列表保持实时准确。
3. **“打开输出目录”优先打开最近一次 MCP 任务目录**：
   - `desktop_qt_ui/ui/main_page/pages/mcp_page.py`：记录最近一次任务状态的 `output_dir`；点击 `open_output_button` 时若存在有效目录则直接打开，否则回退 `self.controller.open_output_folder()`。
4. **`guidelines` 提示词去噪（可选轻量优化）**：
   - `manga_translator/mcp/batch_adapter.py`（`load_job_guidelines_and_glossary`）：在末尾追加简短 MCP 说明，提示通过 `submit_translation_batch(translations, new_terms)` 参数提交，无需额外输出原始 JSON 代码块。
5. **补齐其余 4 个语言包词条**：
   - 补齐 `desktop_qt_ui/locales/{zh_TW,ja_JP,ko_KR,es_ES}.json` 中新增的 70 条 MCP 词条。
6. **清理废弃测试配置键**：
   - 移除 `test/test_mcp_pipeline.py` 与 `test/test_mcp_server.py` 中 `mcp` 字典里已不存在于 `McpSettings` 的 `"enabled": True` 键。
7. **确认保留项（不做回退）**：
   - `desktop_qt_ui/services/log_service.py` 中 `RecentLogHandler` 默认过滤 `INFO+` 已有专门单测 `test_recent_log_handler_filters_debug_truncates_and_tracks_sequence` 锁定意图，维持现状即可（仅可在 `LogService.get_recent_logs` docstring 中注明内存环形缓冲区只收录 `INFO+`，完整 `DEBUG` 请查看日志文件）。

---

## 验收与回归命令

每阶段完成及全部修复收尾后，执行以下命令验证：

```powershell
# 1. MCP 全量单测与端到端 HTTP 测试
uv run --no-sync pytest test/test_mcp_server.py test/test_mcp_pipeline.py -q

# 2. 相关核心模块与启动懒加载回归测试
uv run --no-sync pytest test/test_default_config_coverage.py test/test_batch_skip_planning.py test/test_br_single_region_cleanup.py test/test_skip_no_text.py test/test_app_logic_file_sources.py test/test_app_logic_task_completion.py test/test_code_scanning_security.py test/test_lazy_inference_startup.py -q

# 3. Wiki 参数字段一致性校验
uv run --no-sync python doc/wiki/verify_phase0_ui_parameter_fields.py
```
