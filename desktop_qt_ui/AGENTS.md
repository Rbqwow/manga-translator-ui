# desktop_qt_ui/AGENTS.md — Qt 桌面端与编辑器子系统指南

适用于对 `desktop_qt_ui/` 下启动流程、服务层、设置页、编辑器与桌面 UI 的改动。全局规则见根目录 `AGENTS.md`。

## 模块划分
- **启动与环境**：`main.py`（最顶部优先分发 `--font-catalog-worker` 与打包态 `--panel-detection-worker`，之后才初始化环境/Qt）、`torch_bootstrap.py`、`font_catalog_worker.py`、`app_logic.py`。
- **配置与服务层**：`core/config_models.py`（`AppSettings`）、`services/__init__.py`（`ServiceManager` DI 容器）、`services/config_service.py`（防抖原子写配置与 `.env`）、`services/mcp_service.py`、`services/i18n_service.py`、`services/batch_edit_engine.py`、`services/render_parameter_service.py`、`services/export_service.py`。
- **编辑器逻辑 (`editor/`)**：`editor_controller.py`、`editor_model.py`、`editor_logic.py`、`session.py`、`document_state.py`、`inpaint_state.py`、`paste_overlay_state.py`、`commands.py`、`controller_{document,export,inpaint}_service.py`、`core/resource_manager.py`、`rich_text_*.py`。
- **UI 视图 (`ui/`)**：`main_window.py`、`theme.py`（与 `desktop_qt_ui/theme_registry.py` 协同；不存在 `ui/styles.py` 或 `ui/theme_tokens.py`）、`main_page/`（`dynamic_settings.py`, `settings_tab_layout.json`, `env_management.py`, `pages/`）、`editor/`、`secondary_pages/`、`widgets/`。
- **多语言与工具**：`locales/{zh_CN,zh_TW,en_US,ja_JP,ko_KR,es_ES}.json`、`utils/`（`app_version.py`, `font_list.py`, `memory_cleanup.py`, `archive_extractor.py`）。

## 子系统核心不变量
1. **冷启动惰性导入与 Torch/Qt 顺序**：导入 `desktop_qt_ui.main` 与构造 `OcrService` / `McpService` / `EditorControllerInpaintService` / `ResourceManager` 时禁止触发 `torch`、`mcp`、`uvicorn`、`starlette` 导入；`services/mcp_service.py` 作为孤立 MCP 模块还额外禁止顶层导入 `cv2`、`numpy`、`PIL`。Windows 下须在任何 PyQt6 导入之前执行 `torch_bootstrap.py:prepare_torch_dlls()`。
2. **主线程非阻塞与 Qt 对象生命周期**：
   - `MainAppLogic` 任务序列化跑在单 worker `ThreadPoolExecutor`，禁止在主线程阻塞 `join` 至退出清理阶段；`base_rule_editor.py` 中规则文件加载与源码切表格保存（`_save_table_switch`）走 `_RULE_READ_EXECUTOR` 后台线程（表格按事件循环分片填充），而防抖自动保存（`_on_auto_save_timeout` → `_save_current_content` → `_write_content` → `_write_rule_snapshot`）仍在 GUI 主线程同步执行原子临时文件替换；`dynamic_settings.py` 通过 `_settings_build_seq` 序列号分片构建设置项并丢弃过期构建。
   - `qfluentwidgets` 弹窗动画/Toast 关闭时必须防备 C++ 对象已销毁（`sip.isdeleted`）；`main.py` 刻意设置 `faulthandler.enable(..., all_threads=False)` 防止 Windows 原生文件对话框 `0x8001010e` 竞态崩溃，严禁改为 `True`。
3. **配置安全与自动保存隔离**：`AppSettings` 禁止暴露 `translator.user_api_key`；`ConfigService` 自动保存仅写用户 `config/config.json`，跳过无变化写入（保持 mtime），加载模板或导出配置不得劫持后续自动保存目标。
4. **GUI ↔ MCP GPU 互斥**：`MainAppLogic.start_backend_task` 通过 `McpService.try_reserve_gpu_for_gui()` / `release_gpu_for_gui()` 在 `try/finally` 中占位，且 MCP 未启动时只记录预留标志，不可提前初始化 `McpJobManager`。
5. **编辑器状态与生命周期**：文档切换需检查资源/渲染结果代际身份（避免过期异步回调覆盖新页面）；导出/保存必须从快照或冻结的 `ExportJob` 拷贝读取像素，避免与前台绘制共享可变缓冲。
6. **6 语言与无 Emoji 文本**：因 `ui/theme.py:configure_ui_font()` 仅启用单 UI 字体家族，所有 Qt 渲染文案与内置/示例 Prompt 均不得包含 emoji 或变体选择符以防 Windows 全系统字体回退卡顿，装饰图标改用 `FluentIcon`；新增设置文案需同步全部 6 份语言 JSON（完整清单与校验入口见 `doc/harness/checklists.md` 第 E 节）。
7. **批量编辑引擎**（`services/batch_edit_engine.py`）：保持纯非 Qt 模块；执行全读-局部改-全写以保留 `mask_raw`、`original_width` / `original_height` 及覆层字段；条件匹配与文本替换基于富文本纯文本（`\n`）而非 `[BR]`；经 `rich_text_sync` 拼接并在无样式时剥离冗余 `translation_rich`；严格执行动作顺序与备份/恢复语义。
8. **贴图覆层 `paste_overlays`**（`editor/paste_overlay_state.py`、`editor/controller_export_service.py`）：与 `regions` 同级以 base64 RGBA PNG 存储；`imdecode` 前校验 PNG IHDR 防解压缩炸弹；仿射变换前先预乘 alpha 防黑边；合成放在导出工作线程而非 GUI 线程；撤销/重做深拷贝并保持 ID；与区域选区互斥。
9. **自动保存 vs 自动导出、预取与图片寿命**（`editor/controller_document_service.py`、`core/resource_manager.py`）：切换图片时严格区分 `editor_auto_save_on_switch` 与 `editor_auto_export_on_switch`；已加载图片禁止长期占用 OS 文件句柄；LRU 淘汰与文档切换不得提前 `.close()` 外部仍在使用的图片；半径 2 向前优先预取且不得修改当前文档状态。
10. **渲染参数与方向归一**（`services/render_parameter_service.py`、`manga_translator/utils/textblock.py`、`editor/controller_export_service.py`）：排版方向统一为 `auto` / `h` / `v`（`hr` / `vr` 仅作历史兼容输入）；测量与绘制统一采用 `effective_stroke_width`；`ExportJob.__post_init__` 强制 `disable_auto_wrap=True`、`upscale_ratio=None`、`colorizer="none"`。
11. **字体目录身份**（`utils/font_list.py`）：配置保存为 `family|style`；按实际字体内容去重排版家族名与传统家族名；加载进度分母保持单调；`fonts/` 文件增删或原路径替换时刷新缓存。

## 聚焦验证入口
- 启动与懒加载、配置与多语言、编辑器核心/导出、字体目录与桌面弹窗控件的聚焦测试命令统一维护在 `doc/harness/testing.md` 第 3 节。
