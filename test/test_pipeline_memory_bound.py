import _bootstrap  # noqa: F401, I001

import asyncio
import gc
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from manga_translator.config import Config
from manga_translator.manga_translator import MangaTranslator
from manga_translator.utils.concurrent_pipeline import ConcurrentPipeline


@pytest.mark.parametrize('stage,with_text,cancel,fail', [
    ('render', False, False, False),
    ('render', True, False, False),
    ('inpaint', True, False, False),
    ('render', True, True, False),
    ('render', False, False, True),
])
def test_pipeline_bounds_decoded_pages_and_drains_on_finish(
    monkeypatch, tmp_path, stage, with_text, cancel, fail,
):
    translator = MangaTranslator(params={'use_gpu': False, 'filter_text_enabled': False})
    translator.ignore_errors = True
    translator._current_save_info = {'test': True}
    translator._cleanup_gpu_memory = lambda **kwargs: None
    translator._save_editor_base_if_needed = lambda *args: None
    translator._build_page_context_entries = lambda ctx: []
    cfg = Config()
    cfg.upscale.upscale_ratio = None
    pipeline = ConcurrentPipeline(translator, batch_size=2, max_workers=3)
    gate, full, cancelled = threading.Event(), threading.Event(), threading.Event()
    translator.set_cancel_check_callback(cancelled.is_set)
    loaded = []
    refs = []

    async def detect(config, ctx):
        loaded.append(ctx.image_name)
        refs.append(weakref.ref(ctx.img_rgb))
        if len(loaded) == pipeline.max_inflight_pages:
            full.set()
        if fail:
            raise RuntimeError('simulated OCR failure')
        regions = [SimpleNamespace(text='source', translation='translated')] if with_text else []
        return regions, None, None

    async def ocr(config, ctx):
        return ctx.textlines

    async def translate(batch, size, **kwargs):
        return batch

    async def mask(config, ctx):
        return np.zeros(ctx.img_rgb.shape[:2], dtype=np.uint8)

    async def inpaint_or_render(config, ctx):
        return ctx.img_rgb.copy()

    def save(ctx, *args):
        ctx.result.close()
        ctx.result = None
        ctx.success = True
        return True

    translator._run_detection = detect
    translator._run_ocr = ocr
    translator._run_textline_merge = ocr
    translator._batch_translate_contexts = translate
    translator._run_mask_refinement = mask
    translator._run_inpainting = inpaint_or_render
    translator._run_text_rendering = inpaint_or_render
    translator._save_and_cleanup_context = save

    original_worker = getattr(pipeline, f'_{stage}_thread')

    def stalled_worker():
        while not gate.wait(0.05):
            pipeline._check_cancelled_or_raise('Test slow stage')
        return original_worker()

    monkeypatch.setattr(pipeline, f'_{stage}_thread', stalled_worker)
    paths = []
    for index in range(17):
        path = tmp_path / f'{index:02}.png'
        Image.new('RGB', (64, 64), 'white').save(path)
        paths.append(str(path))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(pipeline.process_batch(paths, [cfg] * len(paths))))
        try:
            assert full.wait(5), 'Producer did not fill the available page slots'
            time.sleep(0.15)
            assert len(loaded) == pipeline.max_inflight_pages
            assert len(pipeline.base_contexts) == pipeline.max_inflight_pages
            if cancel:
                cancelled.set()
                with pytest.raises(asyncio.CancelledError):
                    future.result(timeout=8)
            else:
                gate.set()
                results = future.result(timeout=8)
                assert len(results) == len(paths)
                assert [ctx.image_name for ctx in results] == paths
        finally:
            cancelled.set()
            gate.set()

    gc.collect()
    assert all(ref() is None for ref in refs)
    assert not pipeline.base_contexts
    assert not pipeline._inflight_pages
    assert not pipeline.translation_done
    assert not pipeline.inpaint_done
    assert pipeline.render_queue.empty() and pipeline.inpaint_queue.empty()
    assert not pipeline._worker_tasks
