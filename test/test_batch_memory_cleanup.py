import _bootstrap  # noqa: F401, I001

import asyncio
import gc
import importlib
import sys
import weakref
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import torch
from PIL import Image

from desktop_qt_ui.utils import memory_cleanup
from manga_translator.config import Config, Translator, TranslatorConfig
from manga_translator.manga_translator import MangaTranslator
from manga_translator.utils import Context
from manga_translator.utils.gpu_memory import release_gpu_memory
from manga_translator.utils.inference import ModelWrapper
from manga_translator.utils.retry import RetryExhaustedError


def engine(monkeypatch):
    translator = MangaTranslator(params={
        'use_gpu': False, 'filter_text_enabled': False,
        'skip_failed_after_retries': True, 'max_failure_skips': 0,
    })
    monkeypatch.setattr(translator, '_cleanup_gpu_memory', lambda **kwargs: None)
    return translator


@pytest.mark.parametrize('mode', ['preprocessed', 'translated', 'aliased', 'distinct'])
def test_batch_cleanup_releases_arrays_from_every_context_list(monkeypatch, mode):
    translator = engine(monkeypatch)
    ctx = Context(input=Image.new('RGB', (8, 8)), img_rgb=np.ones((32, 32, 3)),
                  raw_mask_mask=np.ones((32, 32)), text_regions=['retained metadata'])
    refs = [weakref.ref(ctx.img_rgb), weakref.ref(ctx.raw_mask_mask)]
    results = [ctx]  # Returning metadata must not retain intermediate images.
    first = [(ctx, None)]
    second = first if mode == 'aliased' else list(first)
    translator._cleanup_batch_memory(
        preprocessed_contexts=first if mode != 'translated' else None,
        translated_contexts=second if mode != 'preprocessed' else None,
    )
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert results[0].text_regions == ['retained metadata']
    assert ctx.input is None


def test_cleanup_preserves_result_that_aliases_input(monkeypatch):
    translator = engine(monkeypatch)
    image = Image.new('RGB', (8, 8), 'red')
    ctx = Context(input=image, result=image, upscaled=image)
    translator._cleanup_batch_memory(
        current_batch_images=[(image, None)], preprocessed_contexts=[(ctx, None)],
    )
    assert ctx.result.getpixel((0, 0)) == (255, 0, 0)
    assert ctx.input is ctx.upscaled is None
    image.close()


@pytest.mark.parametrize('cancel', [False, True])
def test_hq_failure_and_cancellation_release_preprocessed_images(monkeypatch, tmp_path, cancel):
    translator = engine(monkeypatch)
    cfg = Config(translator=TranslatorConfig(translator=Translator.openai_hq))
    refs = []
    for method in ('_set_image_context', '_save_current_image_context'):
        monkeypatch.setattr(translator, method, lambda *args: None)

    async def preprocess(image, config):
        ctx = Context(input=image, image_name=image.name,
                      text_regions=[SimpleNamespace(text='source')],
                      img_rgb=np.ones((128, 128, 3), dtype=np.uint8))
        refs.append(weakref.ref(ctx.img_rgb))
        return ctx

    async def prompts(config, ctx):
        return ctx

    async def fail(*args, **kwargs):
        if cancel:
            raise asyncio.CancelledError()
        raise RetryExhaustedError('simulated timeout')

    monkeypatch.setattr(translator, '_translate_until_translation', preprocess)
    monkeypatch.setattr(translator, '_load_and_prepare_prompts', prompts)
    monkeypatch.setattr(translator, '_batch_translate_texts', fail)
    inputs = []
    for index in range(3):
        path = tmp_path / f'{index}.png'
        Image.new('RGB', (128, 128)).save(path)
        inputs.append((str(path), cfg))
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(translator.translate_batch(inputs))
    else:
        results = asyncio.run(translator.translate_batch(inputs))
        assert len(results) == 3
        assert all(ctx.skipped and ctx.img_rgb is None for ctx in results)
    gc.collect()
    assert refs and all(ref() is None for ref in refs)


@pytest.fixture
def isolated_caches(monkeypatch):
    for module_name, cache_name in memory_cleanup._MODEL_CACHES:
        module = importlib.import_module(f'manga_translator.{module_name}')
        monkeypatch.setattr(module, cache_name, {})
    module = importlib.import_module('manga_translator.utils.mangalens_detector')
    monkeypatch.setattr(module, '_default_detector', None)
    return sys.modules['manga_translator.ocr'].ocr_cache


@pytest.mark.parametrize('unload', [False, True])
def test_model_cleanup_awaits_public_hook_and_honors_keep_setting(isolated_caches, unload):
    calls = []

    class Model:
        async def unload(self):
            await asyncio.sleep(0)
            calls.append('unloaded')

    model = Model()
    isolated_caches['test'] = model
    count = asyncio.run(memory_cleanup.cleanup_all_model_caches(unload))
    assert count == int(unload)
    assert calls == (['unloaded'] if unload else [])
    assert ('test' in isolated_caches) is not unload


def test_model_cleanup_passes_device_to_offline_translator_hook(isolated_caches):
    """OfflineTranslator overrides unload() with unload(device).

    A no-argument call made every cached translator raise TypeError, and the
    cleanup deliberately retained owners whose unload failed, so the largest
    models were never released. Mock models with a parameterless unload hid it.
    """
    from manga_translator.config import Translator
    from manga_translator.translators.common import OfflineTranslator

    calls = []

    class TranslatorLike(OfflineTranslator):
        def __init__(self):
            self._loaded = True

        async def _load(self, from_lang, to_lang, device):
            pass

        async def _infer(self, from_lang, to_lang, queries):
            return queries

        async def _unload(self):
            calls.append('unloaded')

    model = TranslatorLike()
    isolated_caches[Translator.none] = model
    assert asyncio.run(memory_cleanup.cleanup_all_model_caches(True)) == 1
    assert calls == ['unloaded']
    assert Translator.none not in isolated_caches


@pytest.mark.parametrize('hook', ['unload', '_unload'])
def test_unload_hook_args_tolerate_every_signature_shape(isolated_caches, hook):
    """Device is passed only to hooks that require it, never to optional ones."""
    calls = []

    class Model:
        async def unload(self, device='cpu'):
            calls.append(('optional', device))

        async def _unload(self):
            calls.append(('plain', None))

    if hook == 'unload':
        class Model:
            async def unload(self, device):
                calls.append(('required', device))
    else:
        class Model:
            async def _unload(self):
                calls.append(('plain', None))

    model = Model()
    isolated_caches['test'] = model
    assert asyncio.run(memory_cleanup.cleanup_all_model_caches(True)) == 1
    expected = ('required', 'cuda') if hook == 'unload' else ('plain', None)
    assert calls == [expected]


def test_failed_unload_can_be_retried(isolated_caches):
    class Model:
        async def unload(self):
            raise RuntimeError('busy')

    model = Model()
    isolated_caches['test'] = model
    assert asyncio.run(memory_cleanup.cleanup_all_model_caches(True)) == 0
    assert isolated_caches['test'] is model


@pytest.mark.parametrize('module_name,class_name', [
    ('default', 'DefaultDetector'), ('dbnet_convnext', 'DBConvNextDetector'),
    ('craft', 'CRAFTDetector'),
])
def test_detector_unload_releases_global_model(monkeypatch, module_name, class_name):
    module = importlib.import_module(f'manga_translator.detection.{module_name}')
    detector = getattr(module, class_name).__new__(getattr(module, class_name))
    detector.model = np.ones((64, 64))
    ref = weakref.ref(detector.model)
    monkeypatch.setattr(module, 'MODEL', detector.model)
    if module_name == 'craft':
        detector.model_refiner = np.ones((64, 64))
        refiner_ref = weakref.ref(detector.model_refiner)
    asyncio.run(detector._unload())
    assert module.MODEL is None
    assert ref() is None
    if module_name == 'craft':
        assert refiner_ref() is None


@pytest.mark.parametrize('device,aggressive,expected', [
    ('mps', True, ['sync', 'empty']), ('mps', False, []), ('cpu', True, []),
])
def test_mps_cleanup_runs_only_at_requested_boundaries(monkeypatch, device, aggressive, expected):
    calls = []
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    monkeypatch.setattr(torch.backends.mps, 'is_available', lambda: True)
    monkeypatch.setattr(torch.mps, 'synchronize', lambda: calls.append('sync'))
    monkeypatch.setattr(torch.mps, 'empty_cache', lambda: calls.append('empty'))
    release_gpu_memory(device, aggressive=aggressive)
    assert calls == expected


def test_desktop_cleanup_collects_before_releasing_gpu_memory(monkeypatch, isolated_caches):
    calls = []
    monkeypatch.setattr(memory_cleanup.gc, 'collect', lambda: calls.append('gc'))
    monkeypatch.setattr(memory_cleanup, 'cleanup_gpu_memory', lambda: calls.append('gpu'))
    monkeypatch.setattr(memory_cleanup, 'cleanup_physical_memory', Mock(return_value=False))
    asyncio.run(memory_cleanup.full_memory_cleanup())
    assert calls == ['gc', 'gpu']


def test_retained_model_reloads_when_switching_device(monkeypatch):
    calls = []

    class Model(ModelWrapper):
        def is_downloaded(self):
            return True

        async def _load(self, device):
            calls.append(('load', device))

        async def _unload(self):
            calls.append(('unload', None))

        async def _infer(self):
            pass

    model = Model.__new__(Model)
    model._loaded = False
    monkeypatch.setattr('manga_translator.utils.gpu_memory.release_gpu_memory', lambda: None)

    async def use():
        await model.load('cpu')
        await model.load('cpu')
        await model.load('mps')

    asyncio.run(use())
    assert calls == [('load', 'cpu'), ('unload', None), ('load', 'mps')]


@pytest.mark.parametrize('fail_reload', [False, True])
def test_retained_lama_reloads_when_force_torch_changes(
    monkeypatch, tmp_path, isolated_caches, fail_reload,
):
    from manga_translator import inpainting
    from manga_translator.config import Inpainter
    from manga_translator.inpainting.inpainting_lama_mpe import LamaLargeInpainter

    calls = []
    reload_failed = False

    async def load(self, device, force_torch=False):
        nonlocal reload_failed
        calls.append((device, force_torch))
        if force_torch and fail_reload and not reload_failed:
            reload_failed = True
            raise RuntimeError('temporary model load failure')
        self.backend = 'torch' if force_torch else 'onnx'
        if force_torch:
            self.model = object()
        else:
            self.session = object()

    monkeypatch.setattr(LamaLargeInpainter, '_MODEL_DIR', str(tmp_path))
    monkeypatch.setattr(LamaLargeInpainter, '_check_downloaded', lambda self: True)
    monkeypatch.setattr(LamaLargeInpainter, '_load', load)
    monkeypatch.setattr('manga_translator.utils.gpu_memory.release_gpu_memory', lambda: None)

    async def use():
        await inpainting.prepare(Inpainter.lama_large, 'cpu', force_torch=False)
        model = inpainting.get_inpainter(Inpainter.lama_large)
        first_session = model.session
        await memory_cleanup.cleanup_all_model_caches(unload_models=False)
        await inpainting.prepare(Inpainter.lama_large, 'cpu', force_torch=False)
        assert model.session is first_session
        assert calls == [('cpu', False)]

        if fail_reload:
            with pytest.raises(RuntimeError, match='temporary model load failure'):
                await inpainting.prepare(Inpainter.lama_large, 'cpu', force_torch=True)
            assert not model.is_loaded()
            assert not hasattr(model, 'session')
        await inpainting.prepare(Inpainter.lama_large, 'cpu', force_torch=True)
        assert inpainting.get_inpainter(Inpainter.lama_large) is model
        assert model.backend == 'torch'
        assert model.is_loaded()
        assert not hasattr(model, 'session')

        torch_model = model.model
        await memory_cleanup.cleanup_all_model_caches(unload_models=False)
        await inpainting.prepare(Inpainter.lama_large, 'cpu', force_torch=True)
        assert model.model is torch_model
        await inpainting.prepare(Inpainter.lama_large, 'cpu', force_torch=False)
        assert model.backend == 'onnx'
        assert not hasattr(model, 'model')

    asyncio.run(use())
    assert calls == [('cpu', False)] + [('cpu', True)] * (1 + fail_reload) + [('cpu', False)]


@pytest.mark.parametrize('switch_source', ['config', 'environment'])
def test_retained_paddle_reloads_when_onnx_provider_policy_changes(
    monkeypatch, tmp_path, isolated_caches, switch_source,
):
    from manga_translator import ocr
    from manga_translator.config import Ocr
    from manga_translator.ocr.model_paddleocr import ModelPaddleOCR
    from manga_translator.utils import onnx_runtime

    sessions = []

    def create_session(path, *, sess_options, providers):
        names = [item[0] if isinstance(item, tuple) else item for item in providers]
        session = SimpleNamespace(get_providers=lambda: names)
        sessions.append(session)
        return session

    ort = SimpleNamespace(
        get_available_providers=lambda: ['CUDAExecutionProvider', 'CPUExecutionProvider'],
        InferenceSession=create_session,
    )

    async def load(self, device):
        self.session, _ = onnx_runtime.create_inference_session(
            ort, 'unused.onnx', device=device, sess_options=None,
        )

    monkeypatch.setattr(ModelPaddleOCR, '_MODEL_DIR', str(tmp_path))
    monkeypatch.setattr(ModelPaddleOCR, '_check_downloaded', lambda self: True)
    monkeypatch.setattr(ModelPaddleOCR, '_load', load)
    monkeypatch.setattr(onnx_runtime, '_DISABLE_ONNX_GPU', False)
    monkeypatch.delenv('MT_DISABLE_ONNX_GPU', raising=False)
    monkeypatch.setattr('manga_translator.utils.gpu_memory.release_gpu_memory', lambda: None)

    async def use():
        model = ocr.get_ocr(Ocr.paddleocr)
        for disabled in (False, True, False):
            if switch_source == 'config':
                onnx_runtime.set_onnx_gpu_disabled(disabled)
            else:
                monkeypatch.setenv('MT_DISABLE_ONNX_GPU', str(disabled))
            await ocr.prepare(Ocr.paddleocr, 'cuda')
            assert ocr.get_ocr(Ocr.paddleocr) is model
            assert ('CUDAExecutionProvider' in model.session.get_providers()) is not disabled
            session = model.session
            await memory_cleanup.cleanup_all_model_caches(unload_models=False)
            await ocr.prepare(Ocr.paddleocr, 'cuda')
            assert model.session is session

    asyncio.run(use())
    assert len(sessions) == 3


def test_retained_model_snapshots_mutable_load_options(monkeypatch):
    calls = []

    class Model(ModelWrapper):
        def is_downloaded(self):
            return True

        async def _load(self, device, options):
            calls.append(options['backend'])

        async def _unload(self):
            pass

        async def _infer(self):
            pass

    model = Model.__new__(Model)
    model._loaded = False
    monkeypatch.setattr('manga_translator.utils.gpu_memory.release_gpu_memory', lambda: None)

    async def use():
        options = {'backend': 'onnx'}
        await model.load('cpu', options=options)
        options['backend'] = 'torch'
        await model.load('cpu', options=options)
        await model.load('cpu', options={'backend': 'torch'})

    asyncio.run(use())
    assert calls == ['onnx', 'torch']


# --- P3-16: the cleanup log callback is actually used --------------------

def test_memory_cleanup_reports_through_the_callback(monkeypatch):
    from desktop_qt_ui.utils import memory_cleanup

    messages = []
    monkeypatch.setattr(memory_cleanup, 'cleanup_gpu_memory', lambda: True)
    monkeypatch.setattr(memory_cleanup, 'cleanup_physical_memory', lambda: False)
    monkeypatch.setattr(memory_cleanup, 'cleanup_all_model_caches', lambda unload_models=False: _caches())

    result = asyncio.run(memory_cleanup.full_memory_cleanup(log_callback=messages.append))
    assert len(messages) == 1
    assert 'Memory cleanup finished' in messages[0]
    assert result['gpu_cleared'] is True


async def _caches():
    return 2


def test_memory_cleanup_survives_a_failing_callback(monkeypatch):
    from desktop_qt_ui.utils import memory_cleanup

    monkeypatch.setattr(memory_cleanup, 'cleanup_gpu_memory', lambda: False)
    monkeypatch.setattr(memory_cleanup, 'cleanup_physical_memory', lambda: False)
    monkeypatch.setattr(memory_cleanup, 'cleanup_all_model_caches', lambda unload_models=False: _caches())

    def boom(_message):
        raise RuntimeError('ui gone')

    result = asyncio.run(memory_cleanup.full_memory_cleanup(log_callback=boom))
    assert result['caches_cleared'] == 2
