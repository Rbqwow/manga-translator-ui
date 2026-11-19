# 通用 Harness MCP 改造计划（Plan）

- **基准提交**：`cffacd34afb4219fa1562c319cce9d51b54f8946`（`feat: Antigravity MCP`）
- **改造目标**：去除原提交中对 Google Antigravity 的硬编码耦合，使本地 Streamable HTTP MCP 服务、配套 Skill、一键配置写入器与 UI 文案适用于所有主流 MCP Coding Harness（Claude Code、Codex CLI、Cursor、OpenCode、Antigravity、VS Code、Windsurf 以及通过 `mcp-remote` 桥接的 stdio-only 客户端）。
- **核心原则**：保持底层协议、GPU/渲染双队列流水线与 0-API 硬隔离机制不变，仅改造表层耦合项与客户端接入层。

---

## 一、提交检查确认结果

### 1.1 已完全通用、无需改动的核心层

以下模块基于标准 MCP Streamable HTTP 协议实现，无 Antigravity 专属逻辑：

- `manga_translator/mcp/server_app.py`：FastMCP Streamable HTTP（`/mcp`，`stateless_http=True`），返回标准 `TextContent` 与 base64 `ImageContent`。
- `manga_translator/mcp/job_manager.py`（核心调度逻辑）：双队列（`GpuPriorityWorker` + `RenderWorker`）、租约超时回收、乱序安全上下文、`auto` / `mcp_image` / `file_path` 三档图片投递（临时图位于工作区输出目录下的 `manga_translator_work/mcp_preview/{job_id}`）。
- `manga_translator/mcp/batch_adapter.py`（核心流水线逻辑）：0-API 隔离、本地检测/OCR/合并/修图/渲染、术语表并发锁、译后质检与 `pending_redo`。
- `desktop_qt_ui/core/config_models.py` & `config/config-example.json`：`McpSettings` 字段已完全中立。
- 其余集成文件：`app_logic.py`、`services/__init__.py`、`log_service.py`、`view.py`、`main_window.py`、`manga_translator.py`、`batch_skip.py`、`path_manager.py`、`pyproject.toml`、`test/test_mcp_server.py`。
- 非提交内文件：`presets/Antigravity.json` 为用户本地预设，不在提交范围内，改造时保持不动。

### 1.2 已确认的 Antigravity 专属耦合点清单

| 类别 | 文件与位置 | 当前耦合内容 |
| --- | --- | --- |
| Skill 模板文件名 | `manga_translator/mcp/antigravity_skill_template.md` | 模板文件以 `antigravity_` 命名 |
| Skill 宿主工具名 | `manga_translator/mcp/antigravity_skill_template.md:54` | 写死调用 `read_file` 查看 `file_path` 回退图片 |
| Skill 子代理工具名 | `manga_translator/mcp/antigravity_skill_template.md:82` | 写死调用 `invoke_subagent`，缺少单会话宿主回退说明 |
| 运行时提示词 | `manga_translator/mcp/batch_adapter.py:587, 595` | `hints` 写死 `"Use read_file on preview_path..."` |
| 运行时提示词 | `manga_translator/mcp/job_manager.py:1237` | `PagePreviewResponse.note` 写死 `"call read_file to inspect"` |
| 客户端配置写入 | `desktop_qt_ui/services/mcp_service.py:257-306` | `install_antigravity_mcp_config()` 写死 `~/.gemini/config/mcp_config.json` 与 `mcpServers.url` 结构 |
| Skill 安装路径 | `desktop_qt_ui/services/mcp_service.py:308-322` | `install_antigravity_skill()` 写死 `~/.gemini/config/skills/manga-translate/SKILL.md` |
| UI 集成控件 | `desktop_qt_ui/ui/main_page/pages/mcp_page.py:112, 185, 190, 713, 722, 961` | 快捷按钮硬编码绑定 Antigravity 配置与 Skill 安装方法 |
| 中英文文案 | `desktop_qt_ui/locales/zh_CN.json:1541-1586`、`en_US.json:1542-1587` | 10 条文案直接写死 "Antigravity" / "Google Antigravity" |
| 注释与测试 | `manga_translator/mcp/__init__.py:1`、`protocol_models.py:134, 141`、`test/test_mcp_pipeline.py:842-862` | 模块 docstring 与单测沿用 Antigravity 命名与路径 |

---

## 二、改造设计方案

### 2.1 泛化 Skill 模板与运行时提示词

1. **重命名模板文件**：`manga_translator/mcp/antigravity_skill_template.md` -> `manga_translator/mcp/skill_template.md`。
   - 保留标准 Agent Skills YAML frontmatter（`name: manga-translate` + `description`），兼容 Claude Code、Codex、Cursor、OpenCode、Antigravity 等跨宿主规范。
2. **去耦合文件查看工具名**：
   - `skill_template.md` 第 54 行：将 `call read_file on preview_path / file_path` 改为泛化表述，例如 `open or inspect preview_path / file_path using your host's local file/image viewing tool`。
   - `manga_translator/mcp/batch_adapter.py:587, 595`：将 `Use read_file on preview_path` 改为 `Inspect preview_path using your local file/image viewing tool`。
   - `manga_translator/mcp/job_manager.py:1237`：将 `call read_file to inspect` 改为 `inspect file_path using your local file/image viewing tool`。
3. **去耦合子代理编排指令**：
   - `skill_template.md` 第 82 行：移除 `invoke_subagent`，改为说明「若当前宿主支持派生子代理（subagent），按 3–4 批一组串行接力或按 `max_parallel_batches` 并发拉起不同 `worker_id` 的子代理；若宿主仅支持单会话，则在主会话中连续处理，或在新会话中传入已有 `job_id` 续接」。

### 2.2 表驱动多 Harness 接入器（`desktop_qt_ui/services/mcp_service.py`）

将 `mcp_service.py` 中的 Antigravity 专属方法重构为声明式客户端配置注册表（`MCP_CLIENT_PROFILES`），统一支持「生成配置片段」「合并写入配置文件」「安装 Skill」三个能力：

| Client Key | 显示名称 | 默认配置路径 | 格式与配置结构 | 默认 Skill 路径 |
| --- | --- | --- | --- | --- |
| `generic` | 通用 Streamable HTTP (仅复制) | 无（仅复制片段/URL） | JSON: `{"mcpServers": {"manga-translator": {"url": "<url>"}}}` | `~/.agents/skills/manga-translate/SKILL.md` |
| `claude_code` | Claude Code | `~/.claude.json` | JSON: `mcpServers.manga-translator = {"type": "http", "url": "<url>"}` | `~/.claude/skills/manga-translate/SKILL.md` |
| `codex` | Codex CLI | `~/.codex/config.toml` | TOML: `[mcp_servers.manga-translator]` + `url = "<url>"` | `~/.agents/skills/manga-translate/SKILL.md` |
| `cursor` | Cursor | `~/.cursor/mcp.json` | JSON: `mcpServers.manga-translator = {"url": "<url>"}` | `~/.cursor/skills/manga-translate/SKILL.md` |
| `opencode` | OpenCode | `~/.config/opencode/opencode.json` | JSON: `mcp.manga-translator = {"type": "remote", "url": "<url>"}` | `~/.config/opencode/skills/manga-translate/SKILL.md` |
| `antigravity` | Google Antigravity | `~/.gemini/config/mcp_config.json` | JSON: `mcpServers.manga-translator = {"url": "<url>"}` | `~/.gemini/config/skills/manga-translate/SKILL.md` |
| `windsurf` | Windsurf | `~/.codeium/windsurf/mcp_config.json` | JSON: `mcpServers.manga-translator = {"serverUrl": "<url>"}` | `~/.codeium/windsurf/skills/manga-translate/SKILL.md` |
| `vscode` | VS Code / Copilot | 需指定路径（如 `.vscode/mcp.json`） | JSON: `{"servers": {"manga-translator": {"type": "http", "url": "<url>"}}}` | `~/.agents/skills/manga-translate/SKILL.md` |
| `stdio_bridge` | Stdio 桥接 (`mcp-remote`) | 无（仅复制片段） | JSON: `{"mcpServers": {"manga-translator": {"command": "npx", "args": ["-y", "mcp-remote", "<url>"]}}}` | `~/.agents/skills/manga-translate/SKILL.md` |

实现细节：

1. **统一服务接口**：
   - `list_mcp_client_profiles() -> List[McpClientProfile]`
   - `get_mcp_client_config_snippet(client: str = "generic", port: Optional[int] = None) -> str`
   - `install_mcp_config(client: str, config_path: Optional[str | Path] = None, *, port: Optional[int] = None) -> str`
   - `install_skill(target: str = "agents_standard", skill_path: Optional[str | Path] = None) -> str`（支持按 `client` 写入对应目录、写入通用 `~/.agents/skills/manga-translate/SKILL.md`，或写入用户指定的自定义路径）
2. **安全合并策略**：
   - **JSON / JSONC**：沿用现有 `_atomic_write_text` 原子写入，只更新 `manga-translator` 对应键，完整保留已有其他 server 与顶层字段；读取 OpenCode / VS Code JSON 文件时，如遇注释可用仓库已有依赖 `json5` 兜底解析。
   - **TOML（Codex CLI）**：使用 Python 3.12 内置 `tomllib` 校验语法合法性，通过定向区块替换/追加更新 `[mcp_servers.manga-translator]`（或 `[mcp_servers."manga-translator"]`），不引入新第三方依赖且保留文件原有注释与其他配置项。
3. **路径覆盖**：所有写入接口均接受可选 `config_path` / `skill_path` 参数，供 UI 自定义路径选择和单元测试注入 `tmp_path`。

### 2.3 MCP 页面 UI 与多语言文案改造

1. **控制卡片改造（`desktop_qt_ui/ui/main_page/pages/mcp_page.py`）**：
   - 在端点地址行增加快捷「复制地址」能力，或保留当前可选中 URL。
   - 快捷接入区域增加 **目标客户端下拉框**（`NoWheelComboBox`，列出上述 Harness Profile）。
   - 保留并泛化三个按钮：
     - `复制配置片段`：根据当前选中的客户端复制对应格式（JSON / TOML / `mcp-remote`）。
     - `写入客户端配置`：当选中支持自动写入的客户端时合并写入默认路径；选中 `vscode` 时弹出文件选择框让用户选定工作区 `.vscode/mcp.json`；选中纯复制项（`generic` / `stdio_bridge`）时禁用或提示仅支持复制。
     - `安装 Skill`：按所选客户端写入对应 `SKILL.md`（若选中 `generic` / `codex` / `vscode` / `stdio_bridge` 则默认写入通用 `~/.agents/skills/manga-translate/SKILL.md`）。
2. **多语言文案泛化（`desktop_qt_ui/locales/zh_CN.json`、`en_US.json`）**：
   - 将现有 10 条含 "Antigravity" 的文案改为通用的「MCP 客户端 / AI 宿主」（"MCP client / AI harness"）。
   - 将 `MCP Write Antigravity Config` 替换为 `MCP Write Client Config`，新增目标客户端标签、下拉项及复制/写入提示文案。
3. **注释与 docstring 清理**：
   - 更新 `manga_translator/mcp/__init__.py:1` 与 `manga_translator/mcp/protocol_models.py:134, 141` 中的 Antigravity 字样。
   - `doc/PLAN_antigravity_mcp.md` 作为历史设计文档保持原样，不强制删除。

---

## 三、分阶段实施步骤

1. **Phase 1 — 协议提示词与 Skill 模板去耦合**
   - 将 `manga_translator/mcp/antigravity_skill_template.md` 重命名为 `manga_translator/mcp/skill_template.md`，移除 `read_file` / `invoke_subagent` 硬编码并补充单会话回退说明。
   - 更新 `manga_translator/mcp/batch_adapter.py:587, 595`、`manga_translator/mcp/job_manager.py:1237` 的 `hints` / `note`。
   - 清理 `manga_translator/mcp/__init__.py`、`manga_translator/mcp/protocol_models.py` docstring。
2. **Phase 2 — 多 Harness 配置与 Skill 安装器**
   - 在 `desktop_qt_ui/services/mcp_service.py` 实现 `MCP_CLIENT_PROFILES` 表驱动注册表、JSON 安全合并、Codex TOML 区块安全合并与通用 Skill 安装逻辑。
3. **Phase 3 — UI 接入与多语言更新**
   - 更新 `desktop_qt_ui/ui/main_page/pages/mcp_page.py` 控制卡片（增加目标客户端选择器，联动复制/写入/Skill 安装按钮行为与提示文案）。
   - 同步更新 `desktop_qt_ui/locales/zh_CN.json` 与 `desktop_qt_ui/locales/en_US.json`。
4. **Phase 4 — 自动化测试与跨客户端验证**
   - 更新 `test/test_mcp_pipeline.py` 并补充覆盖各客户端配置片段生成、JSON 保留合并、Codex TOML 保留注释合并、Skill 模板读取以及去耦合 `hints`/`note` 的单元测试。
   - 运行 `pytest test/test_mcp_pipeline.py test/test_mcp_server.py` 确认全部通过。
   - 实机抽测至少 2 个不同 Harness（如 Claude Code / Codex / OpenCode / Antigravity），重点确认：
     - Streamable HTTP 握手与 `tools/list` 正常；
     - 若 Electron 类桌面客户端发送非 `http://` 的 `Origin` 头触发 `403`，记录并在 `server_app.py` 的 `TransportSecuritySettings` 中按需放宽或做针对性适配；
     - `hq` 内联图、`file_path` 回退图与 `text_on_demand` 按需看图全链路跑通。

---

## 四、验收标准

- 仓库代码与 UI 文案中除客户端下拉项 `"Google Antigravity"` 与历史文档 `doc/PLAN_antigravity_mcp.md` 外，不再出现硬编码的 Antigravity 专属路径、宿主工具名（`read_file`、`invoke_subagent`）或限定文案。
- 所有支持的 Harness 均可一键复制正确格式的配置片段，支持写入的客户端均可无损合并配置文件并原子落盘。
- `pytest test/test_mcp_pipeline.py test/test_mcp_server.py` 全部通过，现有 MCP 服务端与 GUI 翻译流水线零回归。
