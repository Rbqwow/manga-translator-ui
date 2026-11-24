# packaging/AGENTS.md — 安装器、打包与版本发布指南

适用于对 `packaging/`、根目录启动脚本（`start.bat`、`Win-*.bat`、`Unix-*.sh`、`start.command`）、`pyproject.toml` 依赖组与 GitHub Actions 发布流水线的改动。全局规则见根目录 `AGENTS.md`。

## 模块划分
- **版本唯一源**：`packaging/VERSION`（当前为 `v3.0.4`），由 `desktop_qt_ui/utils/app_version.py` 与 `packaging/check_version.py` 读取；`pyproject.toml` 中的旧 `version` 字段不作为版本源。
- **统一安装与更新器**：`packaging/launch.py`（环境检测、GPU 变体选择、依赖安装/升级、VC++ 修复、维护自重启）、`packaging/git_update.py`（与 `desktop_qt_ui/core/git_update_helpers.py` 协同）。
- **本地 PyInstaller 打包**：`packaging/build_packages.py`、`packaging/manga-translator-cpu.spec`、`packaging/manga-translator-gpu.spec`、`packaging/create-manga-pdfs.spec`、`packaging/manga-chapter-splitter.spec`、`packaging/pyi_rth_onnxruntime.py`。
- **容器构建**：`packaging/Dockerfile`、`packaging/docker-entrypoint.sh`。

## 子系统核心不变量
1. **NVIDIA GPU 变体选择规则**（`packaging/launch.py`、`test/test_dependency_groups.py`）：
   - GeForce 10 系强制使用 `cuda12.6`；CUDA 13 要求 Turing+ 算力；RTX 50 系强制拒绝 `cuda12.6`。
   - 升级依赖时清理过期的 `torchaudio`，但必须保留 ROCm 包。
2. **Windows ROCm 7.2.1 安装路径**（`pyproject.toml`、`.github/workflows/build-and-release.yml`、`packaging/launch.py`）：
   - `pyproject.toml` 的 `rocm7.2.1` 依赖组仅声明 Linux `x86_64` 的 `torch` / `torchvision` / `triton-rocm`；Windows 端由 `launch.py` 与 CI 按 Radeon ROCm 7.2.1 SDK → PyTorch 顺序通过 pip 安装。
3. **VC++ 运行时修复与维护自重启**（`packaging/launch.py`、`desktop_qt_ui/core/git_update_helpers.py`）：
   - PyTorch 加载失败时先执行 VC++ 修复与复检，再决定是否重装 PyTorch。
   - `restart_maintenance` 必须传递绝对脚本路径，且 Windows 下使用 `Popen` argv 序列。
   - git 更新辅助函数强制设置 `GIT_TERMINAL_PROMPT=0`、`GCM_INTERACTIVE=Never`、`CREATE_NO_WINDOW`，并仅允许 `SUPPORTED_BRANCHES = ("main", "beta")`。
4. **启动脚本与两条打包链路分工**：
   - 根目录启动脚本：`start.bat`（仅 `cuda13.0` 的 `uv sync`）、`Win-Start.bat` / `Win-Install-or-Update.bat`（便携 Python → `.venv` → Conda）、`Unix-Start.sh` / `Unix-Install-or-Update.sh` / `start.command`。`*.bat` 在 `.gitattributes` 中标记为 `-text -diff` 以保留 GBK 编码，编辑时严禁转为 UTF-8 或改动换行符。
   - **CI 便携包归档**（`.github/workflows/build-and-release.yml`）：基于便携 Python 压缩包按 `$excluded` 复制仓库文件，**不走 PyInstaller**。
   - **本地 PyInstaller 打包**（`packaging/build_packages.py` + `*.spec` + `pyi_rth_onnxruntime.py`）：新增打包态子进程入口（如 `--font-catalog-worker`、`--panel-detection-worker`）或数据目录时，须同步检查 `desktop_qt_ui/main.py` 与 `*.spec`。
5. **待发布 Changelog 惯例**：`packaging/VERSION` 仅记录当前已发布版本（`v3.0.4`），下一版本可提前存在标记 `发布状态：待发布` 的 `doc/CHANGELOG_v<NEXT>.md`（如 `doc/CHANGELOG_v3.1.0.md`）；非正式发版不要误改 `packaging/VERSION`。

## 聚焦验证入口
- 依赖组、PyTorch 检测/修复、自重启与更新检查测试清单见 `doc/harness/testing.md` 第 3 节（`packaging/` 行）。
