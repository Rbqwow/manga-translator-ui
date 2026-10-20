@echo off
setlocal

REM Avoid conda/python encoding issues (same reason as Win-Start.bat).
set "PYTHONUTF8=1"

title manga-translator-ui
cd /d "%~dp0"
if errorlevel 1 goto finish

REM Pinned to the CUDA 13 dependency group. CUDA 12.6, AMD and CPU machines
REM must use Win-Start.bat (or the portable package) instead.
uv sync --no-default-groups --group cuda13.0
if errorlevel 1 goto finish
uv run --no-sync python -m desktop_qt_ui.main
:finish
set "launch_exit_code=%errorlevel%"
pause
exit /b %launch_exit_code%
