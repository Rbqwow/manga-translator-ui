import _bootstrap  # noqa: F401

import asyncio
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest


def test_cold_ui_snapshots_and_cleanup_do_not_import_torch():
    subprocess.run([sys.executable, "-c", """
import sys
from types import SimpleNamespace
from unittest.mock import patch
import desktop_qt_ui.main
from manga_translator.config import Config
from services.ocr_service import OcrService
from editor.controller_inpaint_service import EditorControllerInpaintService
from editor.core.resource_manager import ResourceManager

config = Config()
config.cli.use_gpu = True
config_service = SimpleNamespace(get_config=lambda: config)
ocr = OcrService(config_service)
ocr.get_config()
ocr._get_current_config()
with patch('editor.controller_inpaint_service.get_config_service', return_value=config_service):
    snapshot = EditorControllerInpaintService(None)._snapshot_inpaint_config()
assert snapshot.device == 'cuda'
resources = ResourceManager()
resources.clear_image_cache()
resources.unload_image()
resources.cleanup_all()
assert 'torch' not in sys.modules
"""], cwd=_bootstrap.ROOT, capture_output=True, text=True, check=True, timeout=45)


def test_background_torch_import_after_qt_keeps_event_loop_running():
    result = subprocess.run([sys.executable, "-c", """
import sys, time, threading
import desktop_qt_ui.main as entry
from PyQt6.QtCore import QTimer
from qfluentwidgets import CaptionLabel
from services.async_service import get_async_service, shutdown_async_service

assert 'torch' not in sys.modules
app = entry.QApplication([])
label = CaptionLabel('Backend initialization')
label.adjustSize()
ticks = []
timer = QTimer()
timer.timeout.connect(lambda: ticks.append(time.monotonic()))
timer.start(10)
main_thread = threading.get_ident()

async def load():
    assert threading.get_ident() != main_thread
    import torch
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    assert (torch.ones(2, device=device) * 3).sum().item() == 6
    return device

future = get_async_service().submit_task(load())
poll = QTimer()
poll.timeout.connect(lambda: app.quit() if future.done() else None)
poll.start(20)
QTimer.singleShot(45000, app.quit)
app.exec()
assert future.done(), 'Backend initialization timed out'
print(future.result())
assert len(ticks) >= 10, f'GUI did not process events during initialization: {len(ticks)}'
shutdown_async_service()
"""], cwd=_bootstrap.ROOT, capture_output=True, text=True, check=True, timeout=60)
    assert result.stdout.strip() in {"cpu", "cuda"}


@pytest.mark.parametrize("key,name", [
    ("32px", "Model32pxOCR"),
    ("48px", "Model48pxOCR"),
    ("48px_ctc", "Model48pxCTCOCR"),
    ("paddleocr", "ModelPaddleOCR"),
    ("paddleocr_korean", "ModelPaddleOCRKorean"),
    ("paddleocr_latin", "ModelPaddleOCRLatin"),
    ("paddleocr_thai", "ModelPaddleOCRThai"),
])
def test_lazy_ocr_selection_preserves_classes_and_cache(key, name, monkeypatch, tmp_path):
    import manga_translator.ocr as ocr
    from manga_translator.config import Ocr
    from manga_translator.utils.inference import ModelWrapper

    monkeypatch.setattr(ModelWrapper, "_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(ocr, "ocr_cache", {})
    model = ocr.get_ocr(Ocr(key))
    assert type(model) is getattr(ocr, name)
    assert ocr.get_ocr(Ocr(key)) is model


def test_lazy_colorizer_selection_preserves_class_and_cache(monkeypatch, tmp_path):
    import manga_translator.colorization as colorization
    from manga_translator.config import Colorizer
    from manga_translator.utils.inference import ModelWrapper

    monkeypatch.setattr(ModelWrapper, "_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(colorization, "colorizer_cache", {})
    model = colorization.get_colorizer(Colorizer.mc2)
    assert type(model) is colorization.MangaColorizationV2
    assert colorization.get_colorizer(Colorizer.mc2) is model


@pytest.mark.parametrize("use_gpu,available,expected", [
    (False, True, "cpu"), (True, False, "cpu"), (True, True, "cuda"),
])
def test_ocr_device_detection_is_deferred_and_honors_explicit_model(
    use_gpu, available, expected, monkeypatch,
):
    from manga_translator.config import Config, Ocr
    from services import ocr_service

    config = Config()
    config.cli.use_gpu = use_gpu
    probe = Mock(return_value=available)
    monkeypatch.setattr(ocr_service.OcrService, "_check_gpu_available", probe)
    service = ocr_service.OcrService(SimpleNamespace(get_config=lambda: config))
    service.get_config()
    service._get_current_config()
    probe.assert_not_called()
    prepared = []

    async def prepare(key, device):
        prepared.append((key, device))

    monkeypatch.setattr(ocr_service, "prepare_ocr", prepare)
    asyncio.run(service.prepare_model(Ocr.ocr48px))
    assert prepared == [(Ocr.ocr48px, expected)]
    assert service.device == expected
    assert probe.call_count == int(use_gpu)


@pytest.mark.parametrize("requested,available,expected", [
    ("cpu", True, "cpu"), ("cuda", False, "cpu"), ("cuda", True, "cuda"),
])
def test_inpaint_dispatch_resolves_device_preference_in_job(
    requested, available, expected, monkeypatch,
):
    import torch
    from editor.controller_inpaint_service import EditorControllerInpaintService
    from editor.inpaint_state import InpaintConfigSnapshot

    image = np.zeros((4, 5, 3), dtype=np.uint8)
    mask = np.zeros((4, 5), dtype=np.uint8)
    request = SimpleNamespace(config=InpaintConfigSnapshot(
        inpainter="lama_large", inpainting_precision="fp32",
        force_use_torch_inpainting=False, inpainting_size=1024, device=requested,
    ))
    calls = []

    async def dispatch(**kwargs):
        calls.append(kwargs["device"])
        return kwargs["image"]

    monkeypatch.setitem(sys.modules, "manga_translator.inpainting", SimpleNamespace(dispatch=dispatch))
    probe = Mock(return_value=available)
    monkeypatch.setattr(torch.cuda, "is_available", probe)
    result = asyncio.run(EditorControllerInpaintService._dispatch_inpaint(request, image, mask))
    assert calls == [expected]
    assert probe.call_count == int(requested == "cuda")
    assert np.array_equal(result, image)
