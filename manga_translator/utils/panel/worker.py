"""Standalone worker: deliberately avoid importing the UI or model packages."""
import json
import sys
from pathlib import Path


def main(args):
    # Also works with the source files bundled under PyInstaller's _MEIPASS.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lib.page import Page

    input_path, output_path, numbering = args
    page = Page(input_path, numbering=numbering)
    panels = [list(map(int, panel.to_xywh())) for panel in page.panels]
    Path(output_path).write_text(json.dumps(panels), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
