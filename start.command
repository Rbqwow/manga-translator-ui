#!/bin/bash
printf '\033]0;manga-translator-ui\007'
cd "$(dirname "$0")"
uv sync --no-default-groups --group metal
uv run --no-sync python -m desktop_qt_ui.main
# read -p "Press any key to exit..."
