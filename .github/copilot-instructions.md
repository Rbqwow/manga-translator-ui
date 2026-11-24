# Repository Instructions for Coding & Review Agents

本仓库统一规范见根目录 [AGENTS.md](../AGENTS.md)，审查或改动前请按需读取：
- 核心引擎/服务端/MCP：[manga_translator/AGENTS.md](../manga_translator/AGENTS.md)
- Qt 桌面端/编辑器：[desktop_qt_ui/AGENTS.md](../desktop_qt_ui/AGENTS.md)
- 测试反模式与验证矩阵：[doc/harness/testing.md](../doc/harness/testing.md)
- 专项变更检查清单：[doc/harness/checklists.md](../doc/harness/checklists.md)

## 关键门禁摘要
1. Python 严格 `>=3.12,<3.13`；执行 `uv run` 时使用 `--no-sync` 避免破坏本机 GPU 依赖组；不可手改 `uv.lock`。
2. 完整测试命令（Windows / CI 对齐）：
   `$env:QT_QPA_PLATFORM="offscreen"; $env:PYTHONUTF8="1"; uv run --with pytest==9.1.1 --no-sync python -m pytest test -q`
3. 文本文件统一 UTF-8 + `LF`（`.editorconfig`）；**禁止**修改 `*.bat` 的编码或换行符（`.gitattributes` 标记为二进制以保护 GBK）。
4. 禁止提交"为了测试而测试 / 先射箭后画靶"测试（源码/提示词文本扫描、测试 Mock 本身、伪造不可达私有状态、无意义烟雾断言、本机私有文件 `@skipUnless`）。
5. 新增/修改设置项必须走完模型、默认模板、布局、6 套 locale、Web 权限与 Wiki 目录同步链路，不可只改单端。
