import _bootstrap  # noqa: I001

import asyncio
import subprocess
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from manga_translator.utils import panel


def test_panel_worker_detects_without_loading_model_stack():
    image = np.full((96, 96, 3), 255, dtype=np.uint8)
    panels = panel.get_panels_from_array(image, timeout=15)
    assert panels
    assert all(len(box) == 4 and all(isinstance(v, int) for v in box) for box in panels)


@pytest.mark.parametrize('cancel', [False, True])
def test_stuck_worker_is_reaped_and_temporary_images_removed(monkeypatch, cancel):
    processes = []
    temp_paths = []
    real_popen = subprocess.Popen

    def command(input_path, output_path, rtl):
        temp_paths.extend((input_path, output_path))
        return [sys.executable, '-c', 'import time; time.sleep(60)']

    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def check_cancel():
        if cancel and processes:
            raise asyncio.CancelledError()

    monkeypatch.setattr(panel, '_worker_command', command)
    monkeypatch.setattr(panel.subprocess, 'Popen', start)
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        panel.get_panels_from_array(np.zeros((16, 16, 3), dtype=np.uint8),
                                    timeout=0.2, cancel_check=check_cancel)
    assert time.monotonic() - started < 5
    assert len(processes) == 1 and processes[0].poll() is not None
    assert all(not path.parent.exists() for path in temp_paths)


def test_packaged_worker_command_uses_early_entrypoint(monkeypatch):
    monkeypatch.setattr(panel.sys, 'frozen', True, raising=False)
    command = panel._worker_command(Path('input.png'), Path('output.json'), False)
    assert command == [sys.executable, '--panel-detection-worker', 'input.png', 'output.json', 'ltr']


def test_packaged_entrypoint_runs_before_ui_imports(tmp_path):
    from PIL import Image

    worker_dir = Path(panel.__file__).parent
    shutil.copytree(worker_dir, tmp_path / 'panel_worker', ignore=shutil.ignore_patterns('__pycache__'))
    source = tmp_path / 'input.png'
    output = tmp_path / 'output.json'
    Image.new('RGB', (64, 64), 'white').save(source)
    script = '''import runpy, sys
sys._MEIPASS = sys.argv[1]
entry = sys.argv[2]
sys.argv = [entry, '--panel-detection-worker', *sys.argv[3:]]
try:
    runpy.run_path(entry, run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0, exc.code
assert 'torch' not in sys.modules
assert 'PyQt6' not in sys.modules
'''
    subprocess.run(
        [sys.executable, '-c', script, str(tmp_path),
         str(_bootstrap.ROOT / 'desktop_qt_ui' / 'main.py'), str(source), str(output), 'rtl'],
        check=True, capture_output=True, timeout=15,
    )
    assert output.is_file()
