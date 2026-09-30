import _bootstrap  # noqa: F401, I001

import asyncio
import importlib
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from manga_translator.args import create_parser
from manga_translator.config import CliConfig, Config, Translator, TranslatorConfig
from manga_translator.manga_translator import MangaTranslator
from manga_translator.translators.common import CommonTranslator, merge_glossary_to_file
from manga_translator.translators.prompt_loader import load_prompt_file
from manga_translator.utils import Context
from manga_translator.utils.batch_skip import normalize_path
from manga_translator.utils.concurrent_pipeline import ConcurrentPipeline


class FakeTranslator:
    """Exercise real worker threads without models, images or API credentials."""

    def __init__(self, request, *, ignore_errors=False):
        self.request = request
        self.ignore_errors = ignore_errors
        self.context_size = 3
        self.all_page_translations = []
        self._resume_context_pages = []
        self._resume_context_order = {}
        self._cancel_check_callback = None
        self.progress = []
        self.started = []
        self.histories = {}
        self.thread_ids = set()
        self.loop_ids = set()
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def set_cancel_check_callback(self, callback):
        self._cancel_check_callback = callback

    def _check_cancelled(self):
        if self._cancel_check_callback and self._cancel_check_callback():
            raise asyncio.CancelledError()

    async def _report_progress(self, state):
        self.progress.append(state)

    async def _batch_translate_contexts(self, batch, _batch_size, *, context_history):
        names = tuple(ctx.image_name for ctx, _config in batch)
        with self.lock:
            self.started.append(names)
            self.histories[names[0]] = context_history
            self.thread_ids.add(threading.get_ident())
            self.loop_ids.add(id(asyncio.get_running_loop()))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            await self.request(self, names)
            for ctx, _config in batch:
                for region in ctx.text_regions:
                    region.translation = f'translated {ctx.image_name}'
            return batch
        finally:
            with self.lock:
                self.active -= 1

    _build_page_context_entries = MangaTranslator._build_page_context_entries
    _get_context_region_count = staticmethod(MangaTranslator._get_context_region_count)

    def _mark_context_failure(self, ctx, exc, *, stage):
        ctx.translation_error = str(exc)
        ctx.success = False
        return ctx


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def build_pipeline(translator, *, count=14, batch_size=3, workers=3, detection_delay=0):
    pipeline = ConcurrentPipeline(translator, batch_size=batch_size, max_workers=workers)
    names = [f'{index:02}.png' for index in range(count)]

    def detect(paths, configs):
        try:
            for name, config in zip(paths, configs):
                pipeline._check_cancelled_or_raise('Test detection')
                if detection_delay:
                    time.sleep(detection_delay)
                ctx = Context(image_name=name, text_regions=[SimpleNamespace(text=name, translation='')])
                with pipeline._lock:
                    pipeline.base_contexts[name] = ctx
                    pipeline.inpaint_done[name] = True
                pipeline._enqueue_translation_task(name, config)
        finally:
            pipeline.detection_ocr_done = True

    def render():
        while not pipeline.stop_workers and pipeline.stats['rendering'] < len(names):
            try:
                ctx, _config = pipeline.render_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            # Verify history is saved before downstream cleanup releases regions.
            ctx.translated_text = [region.translation for region in ctx.text_regions]
            ctx.text_regions = None
            with pipeline._results_lock:
                pipeline._results.append(ctx)
            pipeline.stats['rendering'] += 1

    pipeline._detection_ocr_thread = detect
    pipeline._inpaint_thread = lambda: None
    pipeline._render_thread = render
    return pipeline, names


def run_pipeline(pipeline, names):
    return asyncio.run(pipeline.process_batch(names, [None] * len(names)))


def test_three_batches_overlap_and_refill_before_slowest_request_finishes():
    next_batch_started = threading.Event()

    async def request(translator, names):
        if names[0] in {'00.png', '03.png', '06.png'}:
            await wait_until(lambda: translator.peak == 3)
        if names[0] == '00.png':
            await wait_until(next_batch_started.is_set)
        if names[0] == '09.png':
            next_batch_started.set()
        await asyncio.sleep(0.02)

    translator = FakeTranslator(request)
    pipeline, names = build_pipeline(translator, detection_delay=0.005)
    results = run_pipeline(pipeline, names)

    assert translator.peak == 3
    assert len(translator.thread_ids) == len(translator.loop_ids) == 3
    assert sorted(translator.started) == [tuple(names[i:i + 3]) for i in range(0, len(names), 3)]
    assert [ctx.image_name for ctx in results] == names
    assert [ctx.translated_text for ctx in results] == [[f'translated {name}'] for name in names]
    assert pipeline.stats['translation'] == pipeline.stats['rendering'] == len(names)
    assert pipeline.translation_thread_done
    assert pipeline.translation_queue.maxsize == 9
    assert translator.progress[-1] == 'batch:1:14:14:0:0'
    history_names = [page[0]['text'] for page in translator.all_page_translations]
    assert history_names == names[-pipeline._history_limit:]
    for first_page, history in translator.histories.items():
        assert all(page[0]['text'] < first_page for page in history)


def test_concurrency_one_preserves_sequential_history_and_final_partial_batch():
    async def request(_translator, _names):
        await asyncio.sleep(0)

    translator = FakeTranslator(request)
    pipeline, names = build_pipeline(translator, count=7, workers=1)
    run_pipeline(pipeline, names)
    assert translator.peak == 1
    assert translator.started == [tuple(names[:3]), tuple(names[3:6]), tuple(names[6:])]
    assert translator.histories[names[0]] == []
    assert [page[0]['text'] for page in translator.histories[names[3]]] == names[:3]
    assert [page[0]['text'] for page in translator.histories[names[6]]] == names[:6]


def test_cancellation_aborts_all_inflight_batches_and_restores_callback():
    cancelled = threading.Event()

    async def request(translator, _names):
        await wait_until(lambda: translator.peak == 3)
        cancelled.set()
        await asyncio.Event().wait()

    translator = FakeTranslator(request)
    original_callback = cancelled.is_set
    translator.set_cancel_check_callback(original_callback)
    pipeline, names = build_pipeline(translator)
    with pytest.raises(asyncio.CancelledError):
        run_pipeline(pipeline, names)
    assert translator.active == 0
    assert len(translator.started) == 3
    assert pipeline.translation_thread_done
    assert translator._cancel_check_callback is original_callback
    assert pipeline._worker_tasks == {}


def test_fatal_batch_error_stops_other_requests_and_preserves_original_error():
    async def request(translator, names):
        await wait_until(lambda: translator.peak == 3)
        if names[0] == '03.png':
            raise RuntimeError('API request failed')
        await asyncio.Event().wait()

    translator = FakeTranslator(request)
    pipeline, names = build_pipeline(translator)
    with pytest.raises(RuntimeError, match='API request failed'):
        run_pipeline(pipeline, names)
    assert translator.active == 0
    assert pipeline.has_critical_error
    assert pipeline.translation_thread_done


def test_ignored_batch_failure_does_not_stop_later_batches():
    async def request(_translator, names):
        if names[0] == '03.png':
            raise RuntimeError('temporary failure')
        await asyncio.sleep(0.01)

    translator = FakeTranslator(request, ignore_errors=True)
    pipeline, names = build_pipeline(translator)
    results = run_pipeline(pipeline, names)
    assert len(results) == len(names)
    assert pipeline.stats['translation'] == len(names)
    assert pipeline.failed_images == set(names[3:6])
    assert not pipeline.has_critical_error
    assert translator.progress[-1] == 'batch:1:14:14:3:0'


def test_resume_context_boundaries_remain_ordered_in_parallel():
    async def request(translator, names):
        if names[0] == '00.png':
            await wait_until(lambda: len(translator.started) == 2)

    translator = FakeTranslator(request)
    # Source page 1 was skipped, leaving pages 0, 2, 3 to translate.
    translator._resume_context_order = {
        normalize_path('00.png'): 0,
        normalize_path('skipped.png'): 1,
        normalize_path('01.png'): 2,
        normalize_path('02.png'): 3,
    }
    translator._resume_context_pages = [(1, 'skipped.png', [{'text': 'saved', 'translation': 'Saved page'}])]
    pipeline, names = build_pipeline(translator, count=3, workers=2)
    run_pipeline(pipeline, names)
    assert sorted(translator.started) == [(names[0],), tuple(names[1:])]
    assert translator.histories[names[0]] == []
    assert translator.histories[names[1]] == [translator._resume_context_pages[0][2]]


def test_inpainting_waits_for_last_translation_worker_and_handles_late_redo():
    async def request(_translator, names):
        if names[0] == '02.png':
            await wait_until(lambda: pipeline._active_translation_workers == 1 and '02.png' in pipeline.inpaint_done)
            pipeline.base_contexts['02.png'].text_regions.pop()

    translator = FakeTranslator(request)
    pipeline, names = build_pipeline(translator, count=3, batch_size=1)
    inpaint_calls = []

    async def mask(_config, _ctx):
        return True

    async def inpaint(_config, ctx):
        inpaint_calls.append(ctx.image_name)
        return 'inpainted'

    def detect(paths, configs):
        try:
            for name, config in zip(paths, configs):
                regions = [SimpleNamespace(text=name, translation='') for _ in range(2)]
                ctx = Context(image_name=name, text_regions=regions, mask=None, _initial_region_ids={id(r) for r in regions})
                with pipeline._lock:
                    pipeline.base_contexts[name] = ctx
                pipeline._enqueue_translation_task(name, config)
                pipeline.inpaint_queue.put((name, config, False))
        finally:
            pipeline.detection_ocr_done = True

    translator._run_mask_refinement = mask
    translator._run_inpainting = inpaint
    pipeline._detection_ocr_thread = detect
    pipeline._inpaint_thread = lambda: ConcurrentPipeline._inpaint_thread(pipeline)
    results = run_pipeline(pipeline, names)
    assert len(results) == 3
    assert inpaint_calls.count('02.png') == 2
    assert pipeline.translation_thread_done
    assert not pipeline.pending_redo


@pytest.mark.parametrize('value', [0, -1, 33])
def test_invalid_translation_concurrency_is_rejected(value):
    with pytest.raises(ValueError):
        CliConfig(translation_concurrency=value)
    with pytest.raises(ValueError):
        ConcurrentPipeline(None, max_workers=value)


def test_cli_exposes_independent_batch_size_and_concurrency():
    parser = create_parser()
    args = parser.parse_args(['local', '-i', 'pages', '--concurrent', '--batch-size', '2', '--translation-concurrency', '4'])
    assert args.concurrent
    assert args.batch_size == 2
    assert args.translation_concurrency == 4
    assert parser.parse_args(['local', '-i', 'pages']).translation_concurrency is None


def test_engine_uses_batch_snapshot_without_mutating_shared_history(monkeypatch):
    translator = MangaTranslator.__new__(MangaTranslator)
    shared_history = [[{'text': 'global', 'translation': 'Global history'}]]
    translator.all_page_translations = shared_history
    translator.ignore_errors = False
    translator.context_size = 3
    config = Config(translator=TranslatorConfig(translator=Translator.openai, enable_post_translation_check=False))
    snapshot = [[{'text': 'previous', 'translation': 'Previous translation'}]]
    ctx = Context(image_name='page.png', from_lang='auto', text_regions=[SimpleNamespace(text='source')])

    async def prepare(_config, merged):
        return merged

    async def post_process(context, _config):
        return context.text_regions

    async def translate(texts, _config, _context, *_args, **kwargs):
        assert kwargs['context_history'] is snapshot
        assert texts == ['source']
        history = translator._build_prev_context(context_history=kwargs['context_history'])
        assert 'Previous translation' in history
        assert 'Global history' not in history
        return ['result']

    async def report(_state):
        pass

    monkeypatch.setattr(translator, '_check_cancelled', lambda: None)
    monkeypatch.setattr(translator, '_cleanup_gpu_memory', lambda **_kwargs: None)
    monkeypatch.setattr(translator, '_report_progress', report)
    monkeypatch.setattr(translator, '_load_and_prepare_prompts', prepare)
    monkeypatch.setattr(translator, '_apply_post_translation_processing', post_process)
    monkeypatch.setattr(translator, '_batch_translate_texts', translate)
    results = asyncio.run(translator._batch_translate_contexts([(ctx, config)], 1, context_history=snapshot))
    assert results[0][0].text_regions[0].translation == 'result'
    assert translator.all_page_translations is shared_history
    assert shared_history == [[{'text': 'global', 'translation': 'Global history'}]]
    assert snapshot == [[{'text': 'previous', 'translation': 'Previous translation'}]]


@pytest.mark.parametrize('module_name, class_name, kind', [
    ('openai', 'OpenAITranslator', Translator.openai),
    ('openai_hq', 'OpenAIHighQualityTranslator', Translator.openai_hq),
    ('gemini', 'GeminiTranslator', Translator.gemini),
    ('gemini_hq', 'GeminiHighQualityTranslator', Translator.gemini_hq),
])
def test_api_batches_own_and_close_clients(monkeypatch, module_name, class_name, kind):
    instances = []

    class FakeAPI:
        def __init__(self):
            self.closed = False
            instances.append(self)

        def parse_args(self, _config):
            pass

        def set_prev_context(self, context):
            self.context = context

        async def _translate(self, _source, _target, texts, _ctx):
            assert 'Previous translation' in self.context
            return [f'translated {text}' for text in texts]

        async def _close_current_client(self):
            self.closed = True

    module = importlib.import_module(f'manga_translator.translators.{module_name}')
    monkeypatch.setattr(module, class_name, FakeAPI)
    translator = MangaTranslator.__new__(MangaTranslator)
    translator.context_size = 3
    translator._cancel_check_callback = None
    translator.all_page_translations = []
    config = Config(translator=TranslatorConfig(translator=kind))
    history = [[{'text': 'previous', 'translation': 'Previous translation'}]]

    async def run():
        return await asyncio.gather(*[
            translator._batch_translate_texts([name], config, Context(from_lang='auto'), context_history=history)
            for name in ('one', 'two')
        ])

    assert asyncio.run(run()) == [['translated one'], ['translated two']]
    assert len(instances) == 2
    assert all(instance.closed for instance in instances)


def test_rate_limit_spaces_request_starts_across_threads():
    class LimitedTranslator:
        _REQUEST_RATE_LOCK = threading.Lock()
        _GLOBAL_LAST_REQUEST_TS = {}
        _MAX_REQUESTS_PER_MINUTE = 1200
        _last_request_ts_key = 'test-model'
        _wait_for_shared_rate_limit = CommonTranslator._wait_for_shared_rate_limit

        def _check_cancelled(self):
            pass

        async def _sleep_with_cancel_polling(self, delay):
            await asyncio.sleep(delay)

    barrier = threading.Barrier(3)

    def start_request():
        instance = LimitedTranslator()
        barrier.wait(timeout=5)

        async def run():
            await instance._wait_for_shared_rate_limit()
            with instance._REQUEST_RATE_LOCK:
                return instance._GLOBAL_LAST_REQUEST_TS[instance._last_request_ts_key]

        return asyncio.run(run())

    with ThreadPoolExecutor(max_workers=3) as executor:
        starts = sorted(executor.map(lambda _index: start_request(), range(3)))
    assert all(later - earlier >= 0.049 for earlier, later in zip(starts, starts[1:]))


def test_concurrent_glossary_updates_preserve_every_term(tmp_path, monkeypatch):
    from manga_translator.translators import prompt_loader

    prompt_path = tmp_path / 'prompt.json'
    prompt_path.write_text(json.dumps({'system': 'Keep this prompt'}), encoding='utf-8')
    original_load = prompt_loader.load_prompt_file

    def slow_load(path):
        data = original_load(path)
        # Expose the read/modify/write race if the merge lock is removed.
        time.sleep(0.01)
        return data

    monkeypatch.setattr(prompt_loader, 'load_prompt_file', slow_load)
    barrier = threading.Barrier(4)

    def merge(index):
        barrier.wait(timeout=5)
        return merge_glossary_to_file(str(prompt_path), [{
            'original': f'name-{index}',
            'category': 'Person',
            'aliases': [{'original': f'name-{index}', 'translations': [{'text': f'translation-{index}'}]}],
        }])

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(executor.map(merge, range(4)))
    data = load_prompt_file(str(prompt_path))
    assert data['system'] == 'Keep this prompt'
    assert {entry['original'] for entry in data['glossary']['Person']} == {f'name-{i}' for i in range(4)}
