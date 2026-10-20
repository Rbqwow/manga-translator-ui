import _bootstrap  # noqa: F401

import asyncio
from types import SimpleNamespace

import pytest

from manga_translator.config import Ocr


class Recorder:
    def __init__(self, gpu_available):
        self.gpu_available = gpu_available
        self.prepared = []
        self.unloaded = []
        self.use_gpu = True


@pytest.fixture
def ocr_service(monkeypatch):
    from desktop_qt_ui.services import ocr_service as module

    recorder = Recorder(gpu_available=False)

    async def prepare_ocr(ocr, device):
        recorder.prepared.append((ocr, device))

    async def unload_ocr(ocr):
        recorder.unloaded.append(ocr)

    monkeypatch.setattr(module, 'prepare_ocr', prepare_ocr)
    monkeypatch.setattr(module, 'unload_ocr', unload_ocr)
    monkeypatch.setattr(module, 'OCR_AVAILABLE', True)

    config = SimpleNamespace(ocr=Ocr.ocr48px, cli=SimpleNamespace(use_gpu=True))
    service = module.OcrService(SimpleNamespace(get_config=lambda: config))
    monkeypatch.setattr(service, '_check_gpu_available', lambda: recorder.gpu_available)
    return service, recorder


def test_gpu_toggle_reloads_the_cached_model_on_the_new_device(ocr_service):
    """The prepared-model guard ignored the device, so the cached model stayed
    on whichever device it was first loaded with while requests used
    `self.device`."""
    service, recorder = ocr_service
    asyncio.run(service.prepare_model(Ocr.ocr48px))
    assert recorder.prepared == [(Ocr.ocr48px, 'cpu')]
    assert service.current_prepared_device == 'cpu'

    # Same model, same config; only the GPU availability changed.
    recorder.gpu_available = True
    asyncio.run(service.prepare_model(Ocr.ocr48px))
    assert recorder.unloaded == [Ocr.ocr48px]
    assert recorder.prepared == [(Ocr.ocr48px, 'cpu'), (Ocr.ocr48px, 'cuda')]
    assert service.current_prepared_device == 'cuda'

    recorder.gpu_available = False
    asyncio.run(service.prepare_model(Ocr.ocr48px))
    assert recorder.prepared[-1] == (Ocr.ocr48px, 'cpu')
    assert service.current_prepared_device == 'cpu'


def test_same_device_reuses_the_prepared_model(ocr_service):
    service, recorder = ocr_service
    asyncio.run(service.prepare_model(Ocr.ocr48px))
    asyncio.run(service.prepare_model(Ocr.ocr48px))
    assert len(recorder.prepared) == 1
    assert recorder.unloaded == []


def test_model_switch_still_unloads_the_previous_model(ocr_service):
    service, recorder = ocr_service
    asyncio.run(service.prepare_model(Ocr.ocr48px))
    asyncio.run(service.prepare_model(Ocr.mocr))
    assert recorder.unloaded == [Ocr.ocr48px]
    assert recorder.prepared[-1] == (Ocr.mocr, 'cpu')
    assert service.current_prepared_device == 'cpu'


def test_unload_current_model_clears_the_device_tracking(ocr_service):
    service, recorder = ocr_service
    asyncio.run(service.prepare_model(Ocr.ocr48px))
    asyncio.run(service.unload_current_model())
    assert service.current_prepared_device is None
    assert service.current_prepared_ocr is None
    assert service.model_prepared is False
