import _bootstrap  # noqa: F401, I001

import asyncio
import logging
from types import SimpleNamespace

import pytest

from manga_translator import translators
from manga_translator.config import CliConfig, Config, TranslatorConfig
from manga_translator.manga_translator import MangaTranslator
from manga_translator.utils import Context
from manga_translator.utils.failure_policy import FailureSkipLimitExceeded
from manga_translator.utils.retry import run_with_retry


@pytest.fixture(params=['batch_language', 'region_repetition'])
def post_check_task(monkeypatch, request):
    """Keep both real post-check paths and request retry accounting in the test."""
    kind = request.param

    def build(*, enabled=True, limit=0, ignore_errors=False, error_type=TimeoutError):
        translator = MangaTranslator(params={
            'use_gpu': False,
            'filter_text_enabled': False,
            'context_size': 10,
            'skip_failed_after_retries': enabled,
            'max_failure_skips': limit,
            'ignore_errors': ignore_errors,
        })
        monkeypatch.setattr(translator, '_cleanup_gpu_memory', lambda **_kwargs: None)
        cfg = Config(
            cli=CliConfig(attempts=1),
            translator=TranslatorConfig(
                enable_post_translation_check=True,
                post_check_max_retry_attempts=3,
            ),
        )
        names = ['bad.png', 'good.png', 'bad-again.png', 'later.png']
        region_count = 11 if kind == 'batch_language' else 1
        pages = [Context(
            image_name=name,
            text_regions=[
                SimpleNamespace(text=f'{name}:{index}', translation='')
                for index in range(region_count)
            ],
        ) for name in names]
        initial_calls, retry_calls, retry_configs = [], [], []

        async def no_sleep(_seconds):
            pass

        async def prepare(_config, ctx):
            return ctx

        async def repetition(text, *_args, **_kwargs):
            return kind == 'region_repetition' and text == 'invalid translation'

        async def language(regions, *_args, **_kwargs):
            return all(region.translation != 'invalid translation' for region in regions)

        async def retry_request(name, runtime_config):
            retry_configs.append(runtime_config)
            # A translator-only config would silently select unlimited retries.
            assert runtime_config is cfg

            async def operation():
                retry_calls.append(name)
                raise error_type('quality-check request failed')

            return await run_with_retry(
                operation=operation,
                runtime_config=runtime_config,
                provider_name='test',
                operation_name='post-check translation',
                logger=logging.getLogger(__name__),
            )

        async def translate(texts, runtime_config, *_args, **_kwargs):
            names = list(dict.fromkeys(text.split(':', 1)[0] for text in texts))
            if any(name in initial_calls for name in names):
                return await retry_request(','.join(names), runtime_config)
            initial_calls.extend(names)
            return [
                'invalid translation' if text.startswith('bad') else 'valid translation'
                for text in texts
            ]

        async def dispatch(_chain, texts, runtime_config, *_args, **_kwargs):
            return await retry_request(texts[0].split(':', 1)[0], runtime_config)

        monkeypatch.setattr(asyncio, 'sleep', no_sleep)
        monkeypatch.setattr(translator, '_load_and_prepare_prompts', prepare)
        monkeypatch.setattr(translator, '_check_repetition_hallucination', repetition)
        monkeypatch.setattr(translator, '_check_target_language_ratio', language)
        monkeypatch.setattr(translator, '_batch_translate_texts', translate)
        monkeypatch.setattr(translators, 'dispatch', dispatch)
        return SimpleNamespace(
            translator=translator, cfg=cfg, pages=pages, kind=kind,
            initial_calls=initial_calls, retry_calls=retry_calls,
            retry_configs=retry_configs,
        )

    return build


@pytest.mark.parametrize('enabled', [False, True])
def test_exhausted_post_check_requests_obey_skip_switch_and_continue(post_check_task, enabled):
    task = post_check_task(enabled=enabled)
    results = asyncio.run(task.translator._batch_translate_contexts(
        [(ctx, task.cfg) for ctx in task.pages], 1,
    ))

    assert [ctx for ctx, _cfg in results] == task.pages
    assert task.initial_calls == [ctx.image_name for ctx in task.pages]
    assert task.retry_calls == ['bad.png'] * 2 + ['bad-again.png'] * 2
    assert all(config is task.cfg for config in task.retry_configs)
    assert len(task.translator.failure_skip_policy.skipped_items) == (2 if enabled else 0)
    for index in (0, 2):
        ctx = task.pages[index]
        if enabled:
            assert ctx.skipped and ctx.skip_reason == 'retry_exhausted'
            assert not ctx.success and not ctx.text_regions
        else:
            assert not ctx.skipped and not ctx.translation_error
            assert ctx.text_regions[0].translation == 'invalid translation'
    for index in (1, 3):
        assert task.pages[index].text_regions[0].translation == 'valid translation'
    history = task.translator.all_page_translations
    assert len(history) == (2 if enabled else 4)
    if enabled:
        assert all(entry['translation'] == 'valid translation' for page in history for entry in page)


def test_multi_page_batch_skips_only_pages_in_failed_post_check_request(post_check_task):
    task = post_check_task()
    pages = task.pages[:2]
    results = asyncio.run(task.translator._batch_translate_contexts(
        [(ctx, task.cfg) for ctx in pages], 2,
    ))

    assert [ctx for ctx, _cfg in results] == pages
    assert pages[0].skipped
    batch_request = task.kind == 'batch_language'
    assert bool(pages[1].skipped) is batch_request
    assert len(task.translator.failure_skip_policy.skipped_items) == (2 if batch_request else 1)
    assert len(task.translator.all_page_translations) == (0 if batch_request else 1)
    if not batch_request:
        assert pages[1].text_regions[0].translation == 'valid translation'


@pytest.mark.parametrize('limit', [1, 2])
@pytest.mark.parametrize('ignore_errors', [False, True])
def test_post_check_skips_stop_at_task_limit(post_check_task, limit, ignore_errors):
    task = post_check_task(limit=limit, ignore_errors=ignore_errors)
    with pytest.raises(FailureSkipLimitExceeded):
        asyncio.run(task.translator._batch_translate_contexts(
            [(ctx, task.cfg) for ctx in task.pages], 1,
        ))

    expected_names = ['bad.png'] if limit == 1 else ['bad.png', 'good.png', 'bad-again.png']
    assert task.initial_calls == expected_names
    assert len(task.retry_calls) == limit * 2
    assert len(task.translator.failure_skip_policy.skipped_items) == limit
    assert len(task.translator.all_page_translations) == limit - 1
    assert not task.pages[-1].skipped


def test_local_post_check_errors_keep_existing_fallback(post_check_task):
    task = post_check_task(error_type=ValueError)
    results = asyncio.run(task.translator._batch_translate_contexts(
        [(ctx, task.cfg) for ctx in task.pages], 1,
    ))

    assert len(results) == len(task.pages)
    assert task.retry_calls == ['bad.png', 'bad-again.png']
    assert not task.translator.failure_skip_policy.skipped_items
    assert all(not ctx.translation_error and not ctx.skipped for ctx in task.pages)
    assert task.pages[0].text_regions[0].translation == 'invalid translation'


def test_post_check_cancellation_is_never_skipped(post_check_task):
    task = post_check_task(error_type=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(task.translator._batch_translate_contexts(
            [(ctx, task.cfg) for ctx in task.pages], 1,
        ))

    assert task.initial_calls == ['bad.png']
    assert task.retry_calls == ['bad.png']
    assert not task.translator.failure_skip_policy.skipped_items
    assert not task.translator.all_page_translations
