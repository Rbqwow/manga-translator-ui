# 专项变更自检清单（Change Checklists）

针对跨多文件协同、极易漏改的 5 类高风险改动，修改后按以下清单逐项核对。

---

## A. 新增或修改配置项（Settings）

1. **桌面配置模型**：`desktop_qt_ui/core/config_models.py` 添加字段、默认值、校验器及迁移兼容（**禁止**把 API Key 等敏感凭据加入 `AppSettings`）。
2. **核心引擎配置**：若参数进入翻译/CLI/服务端/渲染流水线，必须同步 `manga_translator/config.py` 对应 `*Config` 模型与枚举，并检查 `manga_translator/args.py` 是否需要 CLI 参数。
3. **默认发布模板**：同步 `config/config-example.json`（需与 `AppSettings` 及 `Config` 非敏感字段精确对齐，且不含密钥）。
4. **设置页布局与动态控件**：
   - 将 `section.key` 加入 `desktop_qt_ui/ui/main_page/settings_tab_layout.json`。
   - 若为下拉选项或需联动/特殊控件/隐藏规则，同步 `desktop_qt_ui/app_logic.py`（`get_options_for_key`、`get_display_mapping`、`update_single_config`）与 `desktop_qt_ui/ui/main_page/dynamic_settings.py`（数值框同步 `_INT_INPUT_BOUNDS`）。
   - 若涉及编辑器局部参数覆盖或导入导出排除，同步 `desktop_qt_ui/services/render_parameter_service.py` 与 `app_logic.py`。
5. **6 语言同步**：`desktop_qt_ui/locales/{zh_CN,zh_TW,en_US,ja_JP,ko_KR,es_ES}.json` 全部补齐 `label_*`、`desc_*` 及枚举选项文案，保证键集合与 `{placeholder}` 一致且不含 emoji/变体选择符（详见第 E 节）。
6. **Web 端配置与权限**：检查 `manga_translator/server/routes/config.py`——桌面端/CLI 专属设置项必须加入 `SERVER_HIDDEN_CONFIG_KEYS`；如需在后台权限编辑器控制可见/禁用，同步 `manga_translator/server/static/js/admin/components/permission-editor.js`（确保 `data-fullkey="section.key"` 一致）并运行 `node --check`。
7. **Wiki 目录同步（如影响 Wiki/设置页）**：同步 `doc/wiki/{zh,en}/`；若新增下拉项、特殊控件、固定动作或跨页设置，须同步更新 `doc/wiki/regenerate_catalogs.py` 与 `doc/wiki/verify_phase0_ui_parameter_fields.py` 中的 `COMBO_KEYS`、`SPECIAL_CONTROLS`、`FIXED_ACTIONS`、`WIKI_PAGE_BY_KEY`、`LIFECYCLE_BY_KEY`，并执行真实脚本更新及校验：
   ```powershell
   uv run --no-sync python doc/wiki/regenerate_catalogs.py
   uv run --no-sync python doc/wiki/verify_phase0_ui_parameter_fields.py
   ```
8. **验证**：按 `doc/harness/testing.md` 第 3 节运行配置与设置页聚焦测试及 `verify_phase0_ui_parameter_fields.py`。

---

## B. 新增或修改翻译器 / OCR / 检测 / 修复 / 渲染 / 超分后端

1. **后端枚举与工厂注册**：更新 `manga_translator/config.py` 对应枚举与阶段包 `__init__.py` 工厂映射。注意包初始化策略差异：
   - **懒加载 + 模块级缓存**：`manga_translator/ocr/__init__.py`（`OCRS` + `ocr_cache`）、`manga_translator/colorization/__init__.py`（`COLORIZERS` + `colorizer_cache`）。
   - **顶层急切导入**：`detection/`（`detector_cache`）、`inpainting/`（`inpainter_cache`）、`upscaling/`（`upscaler_cache`）、`translators/`（`translator_cache`，且 `translator_cache` 刻意不纳入 `model_cleanup.py:MODEL_CACHES`）。
   - **无模块级模型缓存**：`rendering/__init__.py`。
2. **显存清理闭环**：确保新模型实现 `unload` / `_unload` 钩子并登记在 `manga_translator/utils/model_cleanup.py` 可触及的缓存中；若存在缓存外单例，必须纳入 `model_cleanup.py`。
3. **API 注册与代理传输**：若涉及在线 API 通道或新环境变量，同步 `config/config/translators.json`、`desktop_qt_ui/ui/main_page/env_management.py`、`desktop_qt_ui/services/config_service.py`（`PRESET_SPECIAL_ENV_VARS`、`API_ROTATION_ENV_GROUPS`、`RUNTIME_API_REQUIREMENTS`）及 `manga_translator/server/routes/config.py`（`WEB_API_ENV_KEYS`）；HTTP 客户端遵守系统代理与 `curl_cffi_transport.py` / API Key `latin-1` 校验。
4. **0-API MCP 冲突判定**：若新后端会请求外部云端 API，检查 `manga_translator/mcp/batch_adapter.py:find_api_backend_conflicts()` 是否需要拦截。
5. **字体/模型缺省回退**：渲染/断句后端缺失本地模型（如 HanLP）或缺失字体时必须稳健回退，不可崩溃流水线。

---

## C. 修改 MCP 服务与前后端互斥

1. **冷启动隔离**：改动 `manga_translator/mcp/{__init__,batch_adapter,job_manager}.py` 或 `desktop_qt_ui/services/mcp_service.py` 后，按 `manga_translator/AGENTS.md` / `desktop_qt_ui/AGENTS.md` 验证顶层轻量化导入边界（禁 `torch` / `mcp` / `uvicorn` / `starlette` / `cv2` / `numpy` / `PIL`）。
2. **GPU 互斥**：保证桌面任务通过 `McpService.try_reserve_gpu_for_gui()` / `release_gpu_for_gui()` 与 MCP 作业互斥，且未启动 MCP 时维持惰性占位。
3. **网络安全与协议契约**：保持 `McpSettings` 仅回环地址校验与 `server_app.py` DNS rebinding 防护；保持批处理租约超时回收、取消唤醒与后处理失败回滚语义。
4. **多客户端配置安装**：改动 `install_mcp_config` / `install_skill` 需保持 JSONC 注释备份与 Codex TOML 增量更新/错误拒绝逻辑。

---

## D. 依赖、打包脚本与版本发布

1. **版本唯一源**：发版只更新 `packaging/VERSION`（与 `doc/CHANGELOG_v<VERSION>.md`），不要依赖 `pyproject.toml` 中的旧 `version` 字段；下一版本未正式发布前仅可存在标记 `发布状态：待发布` 的预备 Changelog，勿提前改动 `packaging/VERSION`。
2. **互斥依赖组与锁文件**：改动 `pyproject.toml` 时需保持 `cpu` / `cuda13.0` / `cuda12.6` / `rocm7.2.1` / `metal` 互斥，注意 `cuda12.6` 下 `onnxruntime-gpu` 停留在 `1.26.0`（1.27+ 需 CUDA 13）；通过 `uv lock` 更新锁文件，禁止手改 `uv.lock`。
3. **Windows 批处理与两条打包链路**：
   - **Windows 批处理编码**：`*.bat` 在 `.gitattributes` 中标记为 `-text -diff` 以保留 GBK 编码，编辑时严禁转成 UTF-8 或改动换行符。
   - **CI 便携包归档**（`.github/workflows/build-and-release.yml`）：基于便携 Python 压缩包按 `$excluded` 复制仓库文件（不走 PyInstaller）；新增运行时资源目录时检查打包复制与排除规则。
   - **本地 PyInstaller 打包**（`packaging/build_packages.py` + `packaging/manga-translator-{cpu,gpu}.spec` + `packaging/pyi_rth_onnxruntime.py`）：新增打包态子进程入口（如 `--font-catalog-worker`、`--panel-detection-worker`）或数据目录时同步检查 `desktop_qt_ui/main.py` 与 `*.spec`。
4. **验证**：详细不变量见 `packaging/AGENTS.md`，聚焦测试命令见 `doc/harness/testing.md` 第 3 节（`packaging/` 行）。

---

## E. 新增或修改内置 / 示例 Prompt 或桌面展示文案

1. **禁止 Emoji 与变体选择符（单一权威清单）**：以下文本资产与接口均不得包含 emoji 或变体选择符（`U+FE0E`/`U+FE0F` 等），装饰图标统一使用 `FluentIcon`：
   - `dict/` 内置/示例 Prompt：`system_prompt_hq.yaml`、`system_prompt_hq_format.yaml`、`system_prompt_line_break.yaml`、`glossary_extraction_prompt.yaml`、`prompt_example.yaml`；
   - 代码内置默认 Prompt：`CommonTranslator._HQ_FALLBACK_PROMPT`、`DEFAULT_AI_OCR_PROMPT`、`DEFAULT_AI_COLORIZER_PROMPT`、`DEFAULT_AI_RENDERER_PROMPT`；
   - 全部 Qt 渲染的 UI 文案、菜单、状态栏与 `desktop_qt_ui/locales/*.json`。
2. **占位符与键名对齐**：保留必需占位符（如 `{{{target_lang}}}`、`{{{optional_new_terms_*}}}`）及与 `manga_translator/translators/prompt_loader.py:BUILTIN_TRANSLATION_PROMPTS` 对齐的 YAML 顶层键名（`system_prompt`、`line_break_prompt`、`glossary_extraction_prompt`、`system_prompt_hq_format`）。
3. **`.gitignore` 资产陷阱**：`.gitignore` 默认忽略 `dict/*`（仅放行 `!dict/system_prompt_hq_format.yaml`）与 `fonts/`；新增需纳入版本库的内置 Prompt 或字体时必须更新 `.gitignore` 或显式 `git add -f`，且严禁在测试中引用未入库的本机 `dict/*.yaml`。
4. **验证**：按 `doc/harness/testing.md` 第 3 节运行多语言与 Prompt 聚焦测试。
