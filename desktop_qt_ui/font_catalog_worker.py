"""Font catalog subprocess. JSON lines on stdout; diagnostics only on stderr."""

import json
import os
import sys
import time
from contextlib import redirect_stdout
from dataclasses import asdict


def main():
    # Source/portable launches may not put the UI and repository on sys.path.
    ui_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(ui_dir))
    sys.path.insert(0, ui_dir)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    output = sys.stdout
    request = json.load(sys.stdin)

    def emit(message):
        output.write(json.dumps(message, ensure_ascii=False) + "\n")
        output.flush()

    # Preserve the Windows c10-before-Qt DLL ordering. This worker never
    # creates/shows widgets, starts services, loads configs or changes files.
    with redirect_stdout(sys.stderr):
        from desktop_qt_ui.torch_bootstrap import prepare_torch_dlls

        prepare_torch_dlls()
        from PyQt6.QtGui import QFontDatabase, QGuiApplication
        from desktop_qt_ui.utils import font_list

        app = QGuiApplication.instance() or QGuiApplication([])
        project_families = set()
        for path in request["font_paths"]:
            project_families.update(font_list.register_font_file(path))
            font_list._remember_original_font_names(path)
        names = QFontDatabase.families() if request["include_system"] else sorted(project_families)
        families = [
            name for name in names
            if name and not font_list.qt_family_is_ambiguous(name) and QFontDatabase.isScalable(name)
        ]
        last_progress = 0.0

        def progress(done, total):
            nonlocal last_progress
            now = time.monotonic()
            if done == total or now - last_progress >= 0.1:
                emit({"progress": [done, total]})
                last_progress = now

        entries = font_list._build_font_style_entries(request["locale"], families, progress=progress)
        # Face hashes stay in this process; selection/search aliases suffice in UI.
        payload = [
            {key: value for key, value in asdict(entry).items() if key != "candidate_key"}
            for entry in entries
        ]
        emit({"entries": payload})
        del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
