# 测试规范与反水测约束（Testing Harness）

## 1. 运行方式与目录规则

- **文件位置约束**：`.gitignore` 配置了 `/test/**` 与 `!/test/*.py`。所有纳入版本库的 pytest 测试**必须扁平放在 `test/test_*.py`**，禁止放在 `test/` 子目录或 `tests/` 中。
- **`pyproject.toml` pytest 配置**：`testpaths = ["test"]`、`pythonpath = [".", "desktop_qt_ui"]`。
- **标准命令**：Windows PowerShell 下须先设置 `$env:QT_QPA_PLATFORM = "offscreen"` 与 `$env:PYTHONUTF8 = "1"`，再执行 `uv run --with pytest==9.1.1 --no-sync python -m pytest test -q`（完整说明见根目录 `AGENTS.md` 第 2 节）。
- **公共引导 `test/_bootstrap.py`**：凡是导入仓内代码或 PyQt6 的测试，第一句导入必须写 `import _bootstrap  # noqa: F401`（若文件有模块 docstring 或 `from __future__ import annotations`，置于其后）。它统一完成 `sys.path` 注入、设置 `QT_QPA_PLATFORM=offscreen`，并在 Windows 上确保 `torch` 早于 `PyQt6` 加载。
- **独立调试/基线工具（非 pytest 收集）**：`test/render_golden.py`、`test/chinese_linebreak_debug_ui.py`（临时性能基准脚本不得留在 `test/` 中）。

---

## 2. 测试质量红线（禁止“为了测试而测试 / 先射箭后画靶”）

编写与审查测试时，以下 8 类测试一概禁止合入：

1. **禁止源码 / 提示词文本扫描**：不要用 `read_text()`、`ast.parse()` 或正则去检查生产代码源码写法或静态 Prompt 措辞（如断言某句英文提示词存在或某旧标题已被删除）。唯一例外是 CI 测试隔离基础设施 `test/test_test_collection_isolation.py`（扫描测试文件自身的模块级 `sys.modules` 污染）、`test_default_config_coverage.py`（校验 `config-example.json` 等发行配置资产本身的一致性）与 `test_settings_ui_startup.py`（校验 6 语言 Qt UI 展示文案（除 11 条历史控制台日志键）与内置/默认 Prompt 不含触发 Windows Qt 全量字体回退的 emoji/变体选择符，见 `doc/harness/checklists.md` 第 E 节）。
2. **禁止“测试 Mock 本身”**：不要把被测方法依赖的真实协作者全部替换成 `SimpleNamespace` / 手写假类，然后断言假对象自己的条件分支或内部调用顺序（例如测试 `fake_model.install()` 自己写的 `if artifact.key != self.key`）。应构造真实的 `EditorModel` / `EditorSession` / `ConfigService` / `McpJobManager` / `MangaTranslator`（仅对重量级模型权重推理或外部网络 I/O 打桩）。
3. **禁止测试常量、字面量字典或框架内置行为**：不要断言 `LOSSLESS_SWITCHABLE_PIL_FORMATS` 含 `"WEBP"`、不要复制生产文件的静态字典再跑一遍 round-trip、不要测 Pydantic/dataclass 的字段默认值赋值或 `QIntValidator(low, high)` 自身构造器。
4. **禁止伪造生产中不可达的私有状态**：不要直接把内部字段强行拨成生产中不可能出现的组合（如 `_saving_table_switch=True` 且 `_loading=False`）去迎合刚写好的那行 `if` 分支。
5. **禁止每个功能复制一份通用配置/Locale 检查**：通用校验已由以下三者分工兜底，新增配置/文案不要再单开“验证默认值和 6 语言非空”的测试文件：
   - `test/test_default_config_coverage.py`：默认配置与非敏感字段覆盖率；
   - `test/test_error_i18n.py`：全量 6 语言键集合完整性与 `{placeholder}` 一致性（含核心错误键与 `label_*`/`desc_*` 非空）；
   - `test/test_settings_ui_startup.py`：全量 6 语言 Qt 渲染文案（除 `_LEGACY_CONSOLE_LOG_EMOJI_KEYS` 11 条历史控制台日志键外）、编辑器右键菜单及内置/默认 Prompt 无 emoji/变体选择符。
6. **禁止永久跳过的私有资产测试**：不要提交依赖本机未入库字体/图片/模型路径的 `@unittest.skipUnless(os.path.exists(...))`。若需测字体或图片处理，应使用仓库内 `fonts/` 资产或现场在 `tmp_path` 生成最小样本（如用 `fontTools` / `PIL`）。
7. **禁止无意义的烟雾断言**：当被测入口外层已有 `try/except` 兜底降级时，仅仅断言 `result is not None` 或 `font_size >= 1` 无法抓出内部崩溃；必须断言可观测的差异化输出（如像素颜色、布局尺寸、回退等价性或错误报告）。
8. **必须维护测试间全局状态隔离**：
   - 禁止在模块顶层修改 `sys.modules`（`test_test_collection_isolation.py` 强制门禁），统一在测试函数/fixture 内用 `monkeypatch.setitem(sys.modules, ...)`。
   - 若测试触发导入 `manga_translator.server.main`（其 `logging_manager` 会设置 `logging.getLogger("manga_translator").propagate = False`）或调用 `configure_queue_logging()`，须在 teardown/测试中使用 `monkeypatch` 或 `shutdown_queue_logging()` 还原，禁止污染后续测试。

---

## 3. 按改动范围的最小验证矩阵（唯一权威矩阵）

| 改动区域 | 必跑聚焦测试与校验 |
| --- | --- |
| `desktop_qt_ui/core/config_models.py`, `manga_translator/config.py`, `config/config-example.json`, `settings_tab_layout.json` | `pytest test/test_default_config_coverage.py test/test_config_autosave.py test/test_numeric_setting_validators.py -q` + `doc/wiki/verify_phase0_ui_parameter_fields.py` |
| `desktop_qt_ui/locales/*.json` | `pytest test/test_error_i18n.py test/test_settings_ui_startup.py test/test_app_logic_task_completion.py -q` |
| `dict/` 内置/示例 Prompt、`manga_translator/*/prompt_loader.py`、提示词管理页 | `pytest test/test_prompt_priority_and_builtin.py test/test_settings_ui_startup.py -q` |
| `desktop_qt_ui/main.py`, `torch_bootstrap.py`, `services/` 启动与懒加载 | `pytest test/test_lazy_inference_startup.py test/test_torch_bootstrap.py test/test_main_startup_output.py -q` |
| 字体目录与桌面弹窗控件 | `pytest test/test_font_catalog_worker.py test/test_font_combo_startup.py test/test_font_family_sanitization.py test/test_toast_notification_lifecycle.py test/test_combo_popup_lifetime.py test/test_preset_service.py test/test_dotenv_utils.py -q` |
| `desktop_qt_ui/editor/`, `ui/editor/`, 批量编辑、富文本规则编辑器 | `pytest test/test_editor_document_state.py test/test_editor_inpaint_cache_reset.py test/test_editor_save_responsiveness.py test/test_editor_export_executor.py test/test_editor_auto_save_export_switch.py test/test_editor_document_prefetch.py test/test_editor_image_lifetime.py test/test_editor_region_regressions.py test/test_paste_overlay_state.py test/test_paste_overlay_command.py test/test_paste_overlay_document.py test/test_batch_edit_engine.py test/test_ui_responsiveness.py -q` |
| `manga_translator/rendering/`, 字体, 换行 | `pytest test/test_rich_text_rendering.py test/test_rich_text_editing.py test/test_rich_text_rules.py test/test_rich_text_rules_editor_live.py test/test_rich_text_sync.py test/test_rich_text_strikethrough.py test/test_textblock_rich_safety.py test/test_text_renderer_backend.py test/test_chinese_linebreak_spaces.py test/test_layout_mode_common_flow.py test/test_export_rotated_rich_text_alignment.py -q` |
| 翻译器 / API 轮询 / 代理 / 术语表 | `pytest test/test_api_slot_reorder.py test/test_glossary_conditional_entries.py test/test_system_proxy.py test/test_curl_cffi_windows_path_encoding.py test/test_sakura_repeat_detection.py test/test_gemini_hq_image_preparation.py test/test_post_check_failure_policy.py -q` |
| 图像格式与无损导出 | `pytest test/test_lossless_export_config.py test/test_lossless_image_saving.py test/test_avif_metadata.py test/test_webp_lossless_effort_mapping.py -q` |
| 检测 / OCR / 修复 / 超分 / 分格 | `pytest test/test_yolo_obb_sfx_filter.py test/test_yolo_obb_rearrange_edge_merge.py test/test_mangalens_long_image_memory.py test/test_ocr_device_switch.py test/test_paddleocr_vl_native_crop.py test/test_per_block_inpainting.py test/test_flux_inpainter.py test/test_mask_refinement_split.py test/test_onnx_runtime.py test/test_upscaler_temp_cleanup.py test/test_panel_worker_lifetime.py test/test_panel_line_shapes.py -q` |
| `manga_translator/utils/concurrent_pipeline.py`, `failure_policy.py`, `batch_skip.py`, `model_cleanup.py` | `pytest test/test_translation_concurrency.py test/test_pipeline_memory_bound.py test/test_batch_memory_cleanup.py test/test_batch_skip_planning.py test/test_failure_skip_policy.py test/test_failure_skip_request_isolation.py test/test_failure_skip_pipeline_context.py test/test_skip_no_text.py test/test_subprocess_failure_policy.py -q` |
| `manga_translator/mcp/`, `desktop_qt_ui/services/mcp_service.py` | `pytest test/test_mcp_server.py test/test_mcp_pipeline.py test/test_mcp_client_config_install.py test/test_gui_mcp_gpu_exclusion.py -q` |
| `manga_translator/server/` | `pytest test/test_server_image_encoding.py test/test_server_export_responsiveness.py test/test_server_batch_zip_partial_failure.py test/test_server_skip_visibility.py test/test_admin_reset_translator.py test/test_code_scanning_security.py -q` |
| `packaging/`, `pyproject.toml`, `Win-*.bat` / `Unix-*.sh` | `pytest test/test_dependency_groups.py test/test_pytorch_detection.py test/test_pytorch_runtime_repair.py test/test_launch_maintenance_restart.py test/test_update_service.py test/test_update_fetch_failure.py -q` |
| 任一测试文件改动或新增 | 必过 `pytest test/test_test_collection_isolation.py -q`，合入前跑全量 `pytest test -q` |
