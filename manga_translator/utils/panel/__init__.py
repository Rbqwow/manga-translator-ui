"""Run panel detection in a disposable process with a hard time limit."""
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _worker_command(input_path, output_path, rtl):
    args = [str(input_path), str(output_path), 'rtl' if rtl else 'ltr']
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--panel-detection-worker', *args]
    return [sys.executable, str(Path(__file__).with_name('worker.py')), *args]


def get_panels_from_array(img_rgb, rtl=True, logger=None, *, timeout=60.0, cancel_check=None):
    from ..generic import imwrite_unicode
    logger = logger or logging.getLogger(__name__)

    # The parent owns temporary files so timeout/termination cleans them too.
    with tempfile.TemporaryDirectory(prefix='manga-panels-') as work_dir:
        input_path = Path(work_dir) / 'input.png'
        output_path = Path(work_dir) / 'panels.json'
        if cancel_check:
            cancel_check()
        if not imwrite_unicode(str(input_path), img_rgb, logger):
            raise OSError('Could not write panel detection input')
        command = _worker_command(input_path, output_path, rtl)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=creationflags,
        ) as process:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if cancel_check:
                        cancel_check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'Panel detection timeout ({timeout:g}s)')
                    try:
                        _, error = process.communicate(timeout=min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    detail = error.decode('utf-8', errors='replace')[-2000:]
                    raise RuntimeError(f'Panel worker failed: {detail}')
                return json.loads(output_path.read_text(encoding='utf-8'))
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
