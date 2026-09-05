@echo off
title manga-translator-ui
uv sync --no-default-groups --group cuda13.0
uv run --no-sync python -m desktop_qt_ui.main
pause
