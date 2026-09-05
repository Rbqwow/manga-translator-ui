@echo off
title manga-translator-ui
uv sync
uv run --no-sync python -m desktop_qt_ui.main
pause
