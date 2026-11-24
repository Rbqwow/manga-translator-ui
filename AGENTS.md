# AGENTS.md — Manga Translator UI 仓库协作总纲

本文件是本仓库所有编码 Agent 的**单一事实源（Single Source of Truth）**。按目录深度与任务领域渐进展开：

- 核心引擎、CLI、Web 服务端与本地 MCP：`manga_translator/AGENTS.md`
- Qt6 桌面端、编辑器与本地服务：`desktop_qt_ui/AGENTS.md`
- 安装器、打包与版本发布：`packaging/AGENTS.md`
- 测试规范与反水测红线：`doc/harness/testing.md`
- 变更自检清单（设置/模型/MCP/打包）：`doc/harness/checklists.md`
- 富文本数据结构与渲染契约：`manga_translator/rendering/RICH_TEXT_RENDERING.md`

---

## 1. 项目定位与环境事实源

- **项目构成**：PyQt6 + QFluentWidgets 桌面客户端（`desktop_qt_ui/`）+ 核心漫画翻译引擎/CLI/FastAPI Web/Streamable HTTP MCP 服务（`manga_translator/`）+ 启动器/更新/打包脚本（`packaging/`）。
- **Python 版本**：严格要求 `>=3.12,<3.13`（见 `pyproject.toml`）。
- **版本真相源**：`packaging/VERSION`（当前为 `v3.0.4`），由 `desktop_qt_ui/utils/app_version.py` 与 `packaging/check_version.py` 读取；`pyproject.toml` 中的 `version` 字段为历史遗留，不作为版本源。
- **互斥后端依赖组**（`pyproject.toml` `[tool.uv] conflicts`）：`cpu`、`cuda13.0`（默认）、`cuda12.6`、`rocm7.2.1`、`metal` 五组互斥；**禁止手改 `uv.lock`**。
- **文档纠偏提示**：`doc/DEVELOPMENT.md` 中提及的 `desktop_qt_ui/ui/styles.py`、`desktop_qt_ui/ui/theme_tokens.py`、`doc/wiki/scripts/build-settings-catalog.py`、`build-i18n-catalog.mjs` 已过时/不存在；主题实现位于 `desktop_qt_ui/ui/theme.py` 与 `desktop_qt_ui/theme_registry.py`，Wiki 目录生成与校验位于 `doc/wiki/regenerate_catalogs.py` 与 `doc/wiki/verify_phase0_ui_parameter_fields.py`。

---

## 2. 常用验证与运行命令

在执行 `uv run` 时**始终加 `--no-sync`**，避免将当前机器已安装的特定 GPU 依赖组覆盖回默认的 `cuda13.0`。

### 运行测试（Windows PowerShell / CI 对齐）
```powershell
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTHONUTF8 = "1"
# 若当前 .venv 已同步 test 组（如 CI 或默认 uv sync）：
uv run --no-sync pytest test -q
# 若本机仅通过 start.bat 同步了运行环境（未装 test 组）：
uv run --with pytest==9.1.1 --no-sync python -m pytest test -q
```
单文件/聚焦测试：`uv run --with pytest==9.1.1 --no-sync python -m pytest test/<test_file>.py -q`（按模块分组矩阵见 `doc/harness/testing.md` 第 3 节）。

### 启动入口（Windows PowerShell）
- **桌面 GUI**：`$env:PYTHONUTF8 = "1"; uv run --no-sync python -m desktop_qt_ui.main`
- **CLI 本地翻译**：`uv run --no-sync python -m manga_translator local -i <input> [-o <output>]`
- **Web 服务端**：`uv run --no-sync python -m manga_translator web --host 127.0.0.1 --port 8000 [--use-gpu]`

### 静态检查与目录校验
- **可选 Ruff 检查**（未纳入 `pyproject.toml` 或 CI 门禁）：`ruff check desktop_qt_ui manga_translator --config desktop_qt_ui/ruff.toml`
- **Wiki 配置/多语言目录生成与校验**：
  ```powershell
  uv run --no-sync python doc/wiki/regenerate_catalogs.py
  uv run --no-sync python doc/wiki/verify_phase0_ui_parameter_fields.py
  ```
- **Admin 前端 JS 语法检查**：`node --check manga_translator/server/static/js/admin/components/permission-editor.js`

---

## 3. 仓库结构速查

| 路径 | 职责与核心入口 |
| --- | --- |
| `manga_translator/` | 核心引擎、CLI (`__main__.py`, `args.py`)、配置模式 (`config.py`)、流水线 (`manga_translator.py`)、各阶段实现 (`detection/`, `ocr/`, `textline_merge/`, `translators/`, `mask_refinement/`, `inpainting/`, `rendering/`, `upscaling/`, `colorization/`, `utils/`)、`mode/` (`local`, `ws`, `share`)、`server/`、`mcp/` |
| `desktop_qt_ui/` | 桌面端入口 (`main.py`, `torch_bootstrap.py`)、主控逻辑 (`app_logic.py`)、配置模型 (`core/config_models.py`)、DI 服务容器 (`services/`)、编辑器状态与命令 (`editor/`)、界面 (`ui/main_window.py`, `ui/theme.py`, `ui/main_page/`, `ui/editor/`, `ui/secondary_pages/`, `ui/widgets/`)、6 套翻译 (`locales/`) |
| `config/` | 发布默认模板 `config/config-example.json`（与模型同步）与 API 注册表 `config/config/translators.json`；用户本地配置（`config.json` 等）已被 `.gitignore` 忽略 |
| `dict/` | 内置/示例 Prompt（`system_prompt_hq*.yaml`, `system_prompt_line_break.yaml`, `glossary_extraction_prompt.yaml`）与术语词典；`.gitignore` 默认忽略 `dict/*`（仅放行 `!dict/system_prompt_hq_format.yaml`）与 `fonts/`，新增入库资产须同步更新 `.gitignore` 或显式 `git add -f`；`ai_*_prompt.yaml` 为运行时生成文件 |
| `packaging/` | 版本文件 `VERSION`、统一安装/更新器 `launch.py`、PyInstaller 打包脚本 (`build_packages.py`, `*.spec`)、Docker 构建 (`Dockerfile`, `docker-entrypoint.sh`) |
| `doc/` | 开发与使用文档、`doc/harness/` Agent 规范、VitePress Wiki (`doc/wiki/`) |
| `test/` | 扁平存放 `test/test_*.py`（受 `.gitignore` `/test/**` + `!/test/*.py` 约束），公共前置 `test/_bootstrap.py` |

---

## 4. 十条不可破坏的架构不变量

1. **冷启动惰性导入与 Torch/Qt 顺序**：严格区分两条边界——桌面冷启动（`desktop_qt_ui.main` 与核心服务）禁止急切导入 `torch`/`mcp`/`uvicorn`/`starlette`；孤立 MCP 模块还禁止顶层导入 `cv2`/`numpy`/`PIL`。Windows 下 Torch/DLL 初始化必须早于 PyQt6（避免 `WinError 1114`），具体边界分别见 `desktop_qt_ui/AGENTS.md` 与 `manga_translator/AGENTS.md`。
2. **Qt 主线程响应性、生命周期与无 Emoji 文本**：耗时文件/YAML 读取、字体枚举、大表渲染与模型推理不得阻塞 GUI 主线程，销毁前需检查 `sip.isdeleted` 且 `faulthandler` 保持 `all_threads=False`（详见 `desktop_qt_ui/AGENTS.md`）；所有 Qt 渲染文案及内置/示例 Prompt 严禁包含 emoji 或变体选择符，装饰图标统一使用 `FluentIcon`（完整清单见 `doc/harness/checklists.md` 第 E 节）。
3. **配置同步与凭据隔离**：`config/config-example.json`、`AppSettings`、`Config` 与 `settings_tab_layout.json` 必须保持非敏感字段一致；严禁将 API Key 等密钥写入配置或模板；自动保存仅写用户 `config/config.json`（完整多文件同步点见 `doc/harness/checklists.md` 第 A 节）。
4. **GUI ↔ MCP GPU 互斥与回环安全**：GUI 任务与 MCP 作业通过 `McpService.try_reserve_gpu_for_gui()` / `release_gpu_for_gui()` 协调共享模型缓存且冷态维持惰性占位；MCP 仅绑 loopback 并启用 DNS rebinding 防护，0-API 模式禁止外部 API 后端（详见 `doc/harness/checklists.md` 第 C 节）。
5. **模型显存卸载与并发门控**：所有模型缓存与缓存外单例释放统一经 `manga_translator/utils/model_cleanup.py`，服务端重置经并发排他门控，流水线保持有界页面内存上限（详见 `manga_translator/AGENTS.md`）。
6. **富文本 `richtext.v1` 安全契约**：只接受严格 `richtext.v1`（见 `manga_translator/rendering/RICH_TEXT_RENDERING.md`）；损坏的 `translation_rich` 只能记录告警并丢弃样式，绝不抛异常或丢失文本框，对 `TextBlock.translation` 赋相同字符串不得清空 `translation_rich`。
7. **Windows 编码、路径与子进程约定**：除 `*.bat` 保持 GBK 二进制属性外全仓统一 UTF-8 + `LF`；运行时开启 `PYTHONUTF8=1`，libcurl ANSI 边界、Unicode 图像路径、无控制台标准流兼容及独立子进程规范见 `manga_translator/AGENTS.md` 与 `packaging/AGENTS.md`。
8. **安全边界**：服务端拒绝远程图片 URL 防 SSRF（HTTP 422），文件下载强制路径边界校验防穿越，API 状态指纹不得包含明文密钥（详见 `manga_translator/AGENTS.md`）。
9. **六语言同步**：所有桌面 UI 文案与错误提示必须同步更新 `desktop_qt_ui/locales/{zh_CN,zh_TW,en_US,ja_JP,ko_KR,es_ES}.json`，键集合与 `{placeholder}` 占位符在 6 份语言文件中保持一致。
10. **测试真实性与隔离红线**：测试平铺在 `test/test_*.py`、首行 `import _bootstrap  # noqa: F401`（若导入仓内代码或 Qt）、严禁模块顶层修改 `sys.modules`、必须还原全局 logger 状态，且严禁编写水测；反水测红线与最小验证矩阵见 `doc/harness/testing.md`。
