import _bootstrap  # noqa: F401, I001

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from manga_translator.config import CliConfig, Config, Translator, TranslatorConfig
from manga_translator.manga_translator import MangaTranslator
from manga_translator.utils import Context
from manga_translator.utils.failure_policy import FailureSkipLimitExceeded
from manga_translator.utils.retry import RetryExhaustedError


async def wait_for_event(event):
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize('execution', ['async_tasks', 'server_threads'])
@pytest.mark.parametrize('scenario', ['other_request_hits_limit', 'other_request_disables_skips'])
def test_shared_translator_keeps_each_requests_failure_policy(monkeypatch, execution, scenario):
    translator = MangaTranslator(params={
        'use_gpu': False, 'filter_text_enabled': False, 'context_size': 0, 'batch_size': 1,
    })
    monkeypatch.setattr(translator, '_cleanup_gpu_memory', lambda **_kwargs: None)
    monkeypatch.setattr(translator, '_set_image_context', lambda *_args: None)
    monkeypatch.setattr(translator, '_save_current_image_context', lambda *_args: None)
    monkeypatch.setattr(translator, '_restore_image_context', lambda *_args: True)
    a_waiting, b_finished = threading.Event(), threading.Event()
    hits_limit = scenario == 'other_request_hits_limit'

    async def preprocess(image, _config):
        if image.name == 'a-first.png':
            raise RetryExhaustedError('first failure in A')
        if image.name == 'a-last.png':
            a_waiting.set()
            await wait_for_event(b_finished)
            translator._check_cancelled()
            if not hits_limit:
                raise RetryExhaustedError('second failure in A')
        elif hits_limit:
            raise RetryExhaustedError('B exhausted its API retries')
        return Context(input=image, image_name=image.name, text_regions=[])

    async def finish(ctx, _config):
        ctx.success = True
        return ctx

    monkeypatch.setattr(translator, '_translate_until_translation', preprocess)
    monkeypatch.setattr(translator, '_complete_translation_pipeline', finish)

    def config(enabled, limit):
        return Config(
            cli=CliConfig(skip_failed_after_retries=enabled, max_failure_skips=limit),
            translator=TranslatorConfig(translator=Translator.openai),
        )

    async def invoke(names, cfg):
        images = [Image.new('RGB', (2, 2)) for _ in names]
        for image, name in zip(images, names):
            image.name = name
        try:
            try:
                result = await translator.translate_batch([(image, cfg) for image in images])
            except Exception as error:
                result = error
            # Read the summary in the request's context, as the UI does.
            return result, translator.failure_skip_policy.skipped_items
        finally:
            for image in images:
                image.close()

    a_names = ['a-last.png'] if hits_limit else ['a-first.png', 'a-last.png']
    a_config = config(not hits_limit, 0)
    b_config = config(hits_limit, 1)

    async def run_tasks():
        a_task = asyncio.create_task(invoke(a_names, a_config))
        try:
            await wait_for_event(a_waiting)
            b_result = await invoke(['b.png'], b_config)
        finally:
            b_finished.set()
        return await a_task, b_result

    if execution == 'async_tasks':
        a_result, b_result = asyncio.run(run_tasks())
    else:
        with ThreadPoolExecutor(max_workers=2) as executor:
            a_task = executor.submit(asyncio.run, invoke(a_names, a_config))
            try:
                assert a_waiting.wait(5)
                b_task = executor.submit(asyncio.run, invoke(['b.png'], b_config))
                b_result = b_task.result(timeout=10)
            finally:
                b_finished.set()
            a_result = a_task.result(timeout=10)

    a_contexts, a_skips = a_result
    b_contexts, b_skips = b_result
    assert isinstance(a_contexts, list), a_contexts
    if hits_limit:
        assert a_contexts[0].success
        assert a_skips == []
        assert isinstance(b_contexts, FailureSkipLimitExceeded)
        assert [item['original_path'] for item in b_skips] == ['b.png']
    else:
        assert all(ctx.skipped for ctx in a_contexts)
        assert [item['original_path'] for item in a_skips] == a_names
        assert b_contexts[0].success
        assert b_skips == []
