#!/bin/bash
# Source-tree launcher for macOS; pinned to the Metal dependency group.
# Linux users should use Unix-Start.sh, which selects the GPU group itself.
set -euo pipefail

printf '\033]0;manga-translator-ui\007'
cd "$(dirname "$0")"

uv sync --no-default-groups --group metal
uv run --no-sync python -m desktop_qt_ui.main
