import _bootstrap  # noqa: F401, I001

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from PIL import Image

from manga_translator.config import CliConfig, Config, Translator, TranslatorConfig
from manga_translator.manga_translator import MangaTranslator
from manga_translator.utils import Context
from manga_translator.utils.failure_policy import FailureSkipLimitExceeded, FailureSkipPolicy
from manga_translator.translators.common import CommonTranslator
from manga_translator.utils.retry import (RetryExhaustedError, is_retryable_api_error, run_with_retry)
from test_translation_concurrency import build_pipeline


def engine(monkeypatch, **params):
    translator = MangaTranslator(params={
        'use_gpu': False, 'filter_text_enabled': False, 'context_size': 0,
        'skip_failed_after_retries': True, 'max_failure_skips': 0, **params,
    })
    monkeypatch.setattr(translator, '_cleanup_gpu_memory', lambda **_kwargs: None)
    monkeypatch.setattr(translator, '_cleanup_batch_memory', lambda **_kwargs: None)
    monkeypatch.setattr(translator, '_cleanup_context_memory', lambda *_args, **_kwargs: None)

    async def prepare(_config, ctx):
        return ctx

    async def post(ctx, _config):
        return ctx.text_regions

    monkeypatch.setattr(translator, '_load_and_prepare_prompts', prepare)
    monkeypatch.setattr(translator, '_apply_post_translation_processing', post)
    return translator


def config(kind=Translator.openai):
    return Config(translator=TranslatorConfig(
        translator=kind, enable_post_translation_check=False,
    ))


def context(name):
    return Context(image_name=name, text_regions=[SimpleNamespace(text=name, translation='')])


@pytest.fixture(params=['timeout', 'rate_limit', 'server', 'connection'])
def sakura_request_error(request):
    import httpx
    import openai

    api_request = httpx.Request('POST', 'http://sakura.test/v1/chat/completions')
    errors = {
        'timeout': asyncio.TimeoutError('request timed out'),
        'rate_limit': openai.RateLimitError(
            'rate limited', response=httpx.Response(429, request=api_request), body=None,
        ),
        'server': openai.InternalServerError(
            'server unavailable', response=httpx.Response(500, request=api_request), body=None,
        ),
        'connection': openai.APIConnectionError(request=api_request),
    }
    return errors[request.param]


@pytest.fixture
def sakura_without_network(monkeypatch, tmp_path):
    from manga_translator.translators.sakura import SakuraTranslator

    monkeypatch.setattr(SakuraTranslator, '_setup_client', lambda self: None)
    monkeypatch.setattr(SakuraTranslator, 'get_dict_path', lambda self: str(tmp_path / 'dictionary.txt'))

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    return SakuraTranslator


def test_sakura_exhausted_retries_preserve_cause_and_can_skip(
    monkeypatch, sakura_without_network, sakura_request_error,
):
    sakura = sakura_without_network()
    calls = []

    async def request(prompt):
        calls.append(prompt)
        raise sakura_request_error

    monkeypatch.setattr(sakura, '_request_translation', request)
    with pytest.raises(RetryExhaustedError) as error:
        asyncio.run(sakura._handle_translation_request('source'))

    assert calls == ['source'] * 3
    assert error.value.__cause__ is sakura_request_error
    assert FailureSkipPolicy(True, 0).can_skip(error.value)


@pytest.mark.parametrize('limit', [0, 2])
def test_sakura_batch_continues_after_skip_and_honors_task_limit(
    monkeypatch, sakura_without_network, sakura_request_error, limit,
):
    translator = engine(monkeypatch, max_failure_skips=limit)
    cfg = config(Translator.sakura)
    cfg.translator.target_lang = 'CHS'
    names = ['bad.png', 'good.png', 'bad-again.png', 'later.png']
    pages = [context(name) for name in names]
    calls = []

    async def request(_self, prompt):
        name = prompt[0].strip('「」')
        calls.append(name)
        if name.startswith('bad'):
            raise sakura_request_error
        return '翻译成功'

    # Keep the real batch dispatcher, Sakura translator and request retry loop.
    monkeypatch.setattr(sakura_without_network, '_request_translation', request)
    task = translator._batch_translate_contexts([(page, cfg) for page in pages], 1)
    if limit:
        with pytest.raises(FailureSkipLimitExceeded):
            asyncio.run(task)
        assert calls == ['bad.png'] * 3 + ['good.png'] + ['bad-again.png'] * 3
        assert not pages[-1].skipped
    else:
        results = asyncio.run(task)
        assert [ctx for ctx, _cfg in results] == pages
        assert calls == ['bad.png'] * 3 + ['good.png'] + ['bad-again.png'] * 3 + ['later.png']
        assert pages[-1].text_regions[0].translation == '翻译成功'
    assert pages[1].text_regions[0].translation == '翻译成功'
    assert pages[0].skip_reason == pages[2].skip_reason == 'retry_exhausted'
    assert len(translator.failure_skip_policy.skipped_items) == 2


@pytest.mark.parametrize('attempts', [0, 2])
def test_existing_retries_finish_before_skip_and_later_page_runs(monkeypatch, attempts):
    translator = engine(monkeypatch)
    cfg = config()
    cfg.cli.attempts = attempts
    calls = []

    async def no_sleep(_delay):
        pass

    async def translate(texts, _config, *_args, **_kwargs):
        async def request():
            calls.append(texts[0])
            if texts[0] == 'bad.png':
                raise TimeoutError('request timed out')
            return ['translated']
        return await run_with_retry(
            operation=request, runtime_config=cfg, provider_name='test',
            operation_name='translation', logger=logging.getLogger(__name__),
        )

    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    monkeypatch.setattr(translator, '_batch_translate_texts', translate)
    results = asyncio.run(translator._batch_translate_contexts(
        [(context('bad.png'), cfg), (context('good.png'), cfg)], 1,
    ))
    assert calls == ['bad.png'] * (attempts + 1) + ['good.png']
    assert results[0][0].skip_reason == 'retry_exhausted'
    assert not results[0][0].success
    assert results[1][0].text_regions[0].translation == 'translated'
    assert len(translator.failure_skip_policy.skipped_items) == 1


@pytest.mark.parametrize('ignore_errors', [False, True])
def test_limit_stops_task_even_when_ignoring_errors(monkeypatch, ignore_errors):
    translator = engine(monkeypatch, max_failure_skips=2, ignore_errors=ignore_errors)
    calls = []

    async def fail(texts, *_args, **_kwargs):
        calls.extend(texts)
        raise RetryExhaustedError('exhausted')

    monkeypatch.setattr(translator, '_batch_translate_texts', fail)
    with pytest.raises(FailureSkipLimitExceeded):
        asyncio.run(translator._batch_translate_contexts(
            [(context(str(i)), config()) for i in range(4)], 1,
        ))
    assert calls == ['0', '1']
    assert len(translator.failure_skip_policy.skipped_items) == 2


def test_disabled_switch_preserves_failure_and_cancellation_is_not_skipped(monkeypatch):
    translator = engine(monkeypatch, skip_failed_after_retries=False)

    async def fail(*_args, **_kwargs):
        raise RetryExhaustedError('exhausted')

    monkeypatch.setattr(translator, '_batch_translate_texts', fail)
    with pytest.raises(RetryExhaustedError):
        asyncio.run(translator._batch_translate_contexts([(context('bad'), config())], 1))
    translator.failure_skip_policy = FailureSkipPolicy(True, 0)

    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(translator, '_batch_translate_texts', cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(translator._batch_translate_contexts([(context('bad'), config())], 1))
    assert not translator.failure_skip_policy.skipped_items


def test_failed_batch_counts_only_affected_pages(monkeypatch):
    translator = engine(monkeypatch)
    cfg = config()
    empty = Context(image_name='empty', text_regions=[])
    no_source_text = Context(image_name='no-source', text_regions=[SimpleNamespace(text=None)])
    previous_error = Context(image_name='broken', text_regions=[], translation_error='bad image')

    async def fail(*_args, **_kwargs):
        raise RetryExhaustedError('exhausted')

    monkeypatch.setattr(translator, '_batch_translate_texts', fail)
    results = asyncio.run(translator._batch_translate_contexts(
        [(ctx, cfg) for ctx in [context('a'), empty, context('b'), previous_error, no_source_text]], 5,
    ))
    assert len(results) == 5
    assert len(translator.failure_skip_policy.skipped_items) == 2
    assert not empty.skipped
    assert not no_source_text.skipped
    assert previous_error.translation_error == 'bad image'


def test_skip_accounting_is_atomic_and_deduplicates_pages():
    policy = FailureSkipPolicy(True, 3)

    def fail(index):
        ctx = Context(image_name=f'{index % 4}.png', translation_error='timeout')
        try:
            policy.record(ctx, RetryExhaustedError('timeout'))
        except FailureSkipLimitExceeded:
            pass

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(fail, range(32)))
    assert len(policy.skipped_items) == 3
    with pytest.raises(FailureSkipLimitExceeded):
        policy.check_limit()


def test_unnamed_pages_deduplicate_by_source_and_keep_identity_after_cleanup():
    policy = FailureSkipPolicy(True, 0)
    error = RetryExhaustedError('exhausted')
    images = [Image.new('RGB', (2, 2)) for _ in range(2)]
    try:
        first = Context(input=images[0], translation_error='first failure')
        same_page = Context(input=images[0], translation_error='another stage failed')
        other_page = Context(input=images[1], translation_error='independent failure')
        for ctx in (first, same_page, other_page):
            assert policy.record(ctx, error)
        first.input = None  # Match normal context cleanup; identity must survive.
        assert policy.record(first, error)
        assert len(policy.skipped_items) == 2
        assert first.skip_display_name == same_page.skip_display_name == 'Unnamed image 1'
        assert other_page.skip_display_name == 'Unnamed image 2'
        assert first.image_name is None
        assert [item['original_path'] for item in policy.skipped_items] == [
            'Unnamed image 1', 'Unnamed image 2',
        ]
    finally:
        for image in images:
            image.close()


def test_unnamed_contexts_without_images_remain_distinct():
    policy = FailureSkipPolicy(True, 0)
    pages = [Context(translation_error='exhausted') for _ in range(2)]
    for page in [*pages, *pages]:
        assert policy.record(page, RetryExhaustedError('exhausted'))
    assert len(policy.skipped_items) == 2


@pytest.mark.parametrize('kind', [Translator.openai, Translator.openai_hq])
@pytest.mark.parametrize('failure_stage', ['preprocessing', 'translation'])
def test_unnamed_pil_batch_stops_at_failure_skip_limit(monkeypatch, kind, failure_stage):
    translator = engine(monkeypatch, max_failure_skips=2, batch_size=1)
    cfg = config(kind)
    images = [Image.new('RGB', (2, 2)) for _ in range(4)]
    calls = []

    async def preprocess(image, _cfg):
        calls.append(image)
        if failure_stage == 'preprocessing':
            raise RetryExhaustedError('exhausted')
        return Context(input=image, text_regions=[SimpleNamespace(text='source')])

    async def fail(*_args, **_kwargs):
        raise RetryExhaustedError('exhausted')

    monkeypatch.setattr(translator, '_set_image_context', lambda *_args: None)
    monkeypatch.setattr(translator, '_save_current_image_context', lambda *_args: None)
    monkeypatch.setattr(translator, '_translate_until_translation', preprocess)
    monkeypatch.setattr(translator, '_batch_translate_texts', fail)
    translator._detector_cleanup_task = object()

    async def run():
        with pytest.raises(FailureSkipLimitExceeded, match=r'\(2/2\)'):
            await translator.translate_batch([(image, cfg) for image in images])
        return translator.failure_skip_policy.skipped_items

    try:
        skipped = asyncio.run(run())
        assert len(calls) == 2
        assert all(actual is expected for actual, expected in zip(calls, images))
        assert [item['original_path'] for item in skipped] == [
            'Unnamed image 1', 'Unnamed image 2',
        ]
    finally:
        for image in images:
            image.close()


@pytest.mark.parametrize('kind', [Translator.openai, Translator.openai_hq])
def test_whole_task_continues_after_skip_and_resets_counter(monkeypatch, kind):
    translator = engine(monkeypatch, batch_size=1)
    cfg = config(kind)
    calls = []
    images = [Image.new('RGB', (2, 2)) for _ in range(2)]
    for index, image in enumerate(images):
        image.name = f'{index}.png'

    async def preprocess(image, _cfg):
        return Context(input=image, image_name=image.name, text_regions=[SimpleNamespace(text=image.name)])

    async def translate(texts, *_args, **_kwargs):
        calls.extend(texts)
        if texts[0] == '0.png':
            raise RetryExhaustedError('timeout')
        return ['translated']

    async def finish(ctx, _cfg):
        ctx.success = True
        return ctx

    monkeypatch.setattr(translator, '_set_image_context', lambda *_args: None)
    monkeypatch.setattr(translator, '_restore_image_context', lambda *_args: True)
    monkeypatch.setattr(translator, '_save_current_image_context', lambda *_args: None)
    monkeypatch.setattr(translator, '_translate_until_translation', preprocess)
    monkeypatch.setattr(translator, '_batch_translate_texts', translate)
    monkeypatch.setattr(translator, '_complete_translation_pipeline', finish)
    translator._detector_cleanup_task = object()
    progress = []

    async def report(state, _finished):
        if state.startswith('batch:'):
            progress.append(state)

    translator.add_progress_hook(report)

    async def run_tasks():
        for _ in range(2):
            results = await translator.translate_batch([(image, cfg) for image in images])
            assert results[0].skipped
            assert results[1].success
            assert len(translator.failure_skip_policy.skipped_items) == 1

    try:
        asyncio.run(run_tasks())
    finally:
        for image in images:
            image.close()
    assert calls == ['0.png', '1.png', '0.png', '1.png']
    assert 'batch:2:2:2:0:1' in progress


@pytest.mark.parametrize('limit', [0, 2])
def test_concurrent_batches_share_failure_skip_budget(monkeypatch, limit):
    translator = engine(monkeypatch, max_failure_skips=limit, ignore_errors=True)

    async def fail(*_args, **_kwargs):
        await asyncio.sleep(0.01)
        raise RetryExhaustedError('timeout')

    monkeypatch.setattr(translator, '_batch_translate_texts', fail)
    pipeline, names = build_pipeline(translator, count=8, batch_size=1, workers=3)
    if limit:
        with pytest.raises(FailureSkipLimitExceeded):
            asyncio.run(pipeline.process_batch(names, [config()] * len(names)))
        assert len(translator.failure_skip_policy.skipped_items) == limit
    else:
        results = asyncio.run(pipeline.process_batch(names, [config()] * len(names)))
        assert len(results) == 8
        assert all(ctx.skipped for ctx in results)
        assert pipeline._get_failed_count() == 0


@pytest.mark.parametrize('permanent', [
    RuntimeError('Invalid API key provided'),
    RuntimeError('model_not_found: gpt-does-not-exist'),
    RuntimeError('The model `gpt-4o` does not exist or you do not have access to it'),
    RuntimeError('Your account has been deactivated'),
    RuntimeError('insufficient_quota: billing hard limit reached'),
    type('AuthError', (Exception,), {})('invalid api key') ,
])
def test_configuration_errors_are_not_skippable_after_exhaustion(permanent):
    """A retry budget cannot fix credentials, model names, or a disabled account.

    The provider loops re-wrap every failure in RetryExhaustedError, so the
    skip branch used to absorb configuration mistakes. With max_failure_skips=0
    that silently skipped every page and still reported success.
    """
    from manga_translator.utils.failure_policy import is_retry_exhausted
    from manga_translator.utils.retry import is_permanent_api_error

    wrapped = RetryExhaustedError('failed after 5 attempts')
    wrapped.__cause__ = permanent
    assert is_permanent_api_error(wrapped)
    assert not is_retry_exhausted(wrapped)
    assert not FailureSkipPolicy(True, 0).can_skip(wrapped)


@pytest.mark.parametrize('retryable', [
    TimeoutError('request timed out'),
    RuntimeError('HTTP 503 Service Unavailable'),
    RuntimeError('connection reset by peer'),
    RuntimeError('429 Too Many Requests: rate limit'),
])
def test_transient_errors_still_skip_after_exhaustion(retryable):
    from manga_translator.utils.failure_policy import is_retry_exhausted
    from manga_translator.utils.retry import is_permanent_api_error

    wrapped = RetryExhaustedError('failed after 5 attempts')
    wrapped.__cause__ = retryable
    assert not is_permanent_api_error(wrapped)
    assert is_retry_exhausted(wrapped)
    assert FailureSkipPolicy(True, 0).can_skip(wrapped)


def test_rotated_api_exhaustion_stays_skippable_when_transient():
    """APIRotationExhaustedError subclasses RetryExhaustedError for a reason."""
    from manga_translator.api_key_rotation import APIRotationExhaustedError
    from manga_translator.utils.failure_policy import is_retry_exhausted

    error = APIRotationExhaustedError('all candidates failed')
    error.__cause__ = TimeoutError('timed out')
    assert is_retry_exhausted(error)
    assert FailureSkipPolicy(True, 0).can_skip(error)


def test_rotated_api_exhaustion_with_bad_key_is_not_skippable():
    from manga_translator.api_key_rotation import APIRotationExhaustedError
    from manga_translator.utils.failure_policy import is_retry_exhausted

    error = APIRotationExhaustedError('all candidates failed')
    error.__cause__ = RuntimeError('401 Unauthorized: Invalid API key')
    assert not is_retry_exhausted(error)
    assert not FailureSkipPolicy(True, 0).can_skip(error)


def test_new_config_defaults_and_validation():
    # Release-template defaults: skip after retries is on, with a budget of 5.
    from desktop_qt_ui.core.config_models import CliSettings
    for model in (CliConfig, CliSettings):
        assert model().skip_failed_after_retries is True
        assert model().max_failure_skips == 5
        with pytest.raises(ValueError):
            model(max_failure_skips=-1)


def test_api_failover_finishes_each_candidates_retries_before_skipping(monkeypatch):
    from manga_translator import api_key_rotation as rotation

    candidates = (SimpleNamespace(label='first'), SimpleNamespace(label='second'))
    calls = []
    monkeypatch.setattr(rotation, 'iter_api_candidates', lambda *_args: candidates)
    monkeypatch.setattr(rotation, 'record_api_failure', lambda *_args: None)

    async def no_sleep(_delay):
        pass

    async def request(endpoint):
        calls.append(endpoint.label)
        raise TimeoutError('timed out')

    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    with pytest.raises(RetryExhaustedError) as error:
        asyncio.run(rotation.run_with_api_candidates(
            endpoints=candidates, strategy='failover', operation=request,
            provider_name='test', operation_name='request', retry_attempts=2,
            logger=logging.getLogger(__name__),
        ))
    assert calls == ['first'] * 3 + ['second'] * 3
    assert FailureSkipPolicy(True, 0).can_skip(error.value)


def test_unlimited_retries_can_succeed_without_skipping(monkeypatch):
    calls = 0

    async def request():
        nonlocal calls
        calls += 1
        if calls <= 6:
            raise TimeoutError('timed out')
        return 'ok'

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr(asyncio, 'sleep', no_sleep)
    result = asyncio.run(run_with_retry(
        operation=request, runtime_config={'cli': {'attempts': -1}},
        provider_name='test', operation_name='request', logger=logging.getLogger(__name__),
    ))
    assert result == 'ok'
    assert calls == 7


def test_wrapped_api_failure_can_skip_but_local_errors_do_not(monkeypatch):
    from manga_translator.manga_translator import FileTranslationFailure

    translator = engine(monkeypatch)
    failure = FileTranslationFailure('ocr', RetryExhaustedError('API unavailable'))
    ctx = translator._mark_context_failure(context('api.png'), failure, stage='preprocessing')
    assert ctx.skip_reason == 'retry_exhausted'
    assert 'OCR' in ctx.skip_message
    local_error = ValueError('invalid image')
    assert not translator._can_continue_after_error(local_error)
    ctx = translator._mark_context_failure(context('local.png'), local_error, stage='preprocessing')
    assert not ctx.skipped
    assert len(translator.failure_skip_policy.skipped_items) == 1


@pytest.mark.parametrize('mode', ['load_text', 'replace_translation'])
@pytest.mark.parametrize('limit', [0, 1])
def test_import_and_replace_modes_obey_failure_skip_policy(monkeypatch, tmp_path, mode, limit):
    translator = engine(monkeypatch, **{mode: True}, max_failure_skips=limit, batch_size=1)
    cfg = config()
    images = [Image.new('RGB', (2, 2)) for _ in range(2)]
    for index, image in enumerate(images):
        image.name = str(tmp_path / f'{index}.png')
    seen = []
    progress = []

    def fail(image):
        seen.append(image.name)
        raise RetryExhaustedError('API retries exhausted')

    async def preprocess(image, _config):
        fail(image)

    async def report(state, _finished):
        if state.startswith('batch:'):
            progress.append(state)

    monkeypatch.setattr(translator, '_preprocess_load_text_mode', lambda *_args: None)
    if mode == 'load_text':
        monkeypatch.setattr(translator, '_set_image_context', lambda _cfg, image: fail(image))
    else:
        from manga_translator.utils import replace_translation
        monkeypatch.setattr(replace_translation, 'find_translated_image', lambda *_args: 'paired.png')
        monkeypatch.setattr(translator, '_set_image_context', lambda *_args: None)
        monkeypatch.setattr(translator, '_translate_until_translation', preprocess)
    monkeypatch.setattr(translator, '_save_and_cleanup_context', lambda *_args: pytest.fail('skipped images must not be saved'))
    translator._detector_cleanup_task = object()
    translator.add_progress_hook(report)
    task = translator.translate_batch(
        [(image, cfg) for image in images],
        save_info={'output_folder': str(tmp_path / 'output'), 'overwrite': True, 'input_folders': set()},
    )
    skipped_items = []

    async def run_task():
        nonlocal skipped_items
        try:
            return await task
        finally:
            skipped_items = translator.failure_skip_policy.skipped_items

    try:
        if limit:
            with pytest.raises(FailureSkipLimitExceeded):
                asyncio.run(run_task())
            assert len(seen) == 1
            assert len(skipped_items) == 1
        else:
            results = asyncio.run(run_task())
            assert len(seen) == 2
            assert all(ctx.skip_reason == 'retry_exhausted' for ctx in results)
            assert 'batch:2:2:2:0:2' in progress
            assert len(skipped_items) == 2
    finally:
        for image in images:
            image.close()


def test_ocr_unavailable_candidates_remain_skippable_after_previous_failure(monkeypatch):
    from manga_translator.ocr import model_api_ocr

    ocr = SimpleNamespace(
        PROVIDER_NAME='test',
        _build_ocr_prompt=lambda _config: 'Read text',
        _generate_text_direction=lambda _lines: [],
        _read_runtime_config=lambda _config: SimpleNamespace(api_key='test', candidates=(), strategy='failover'),
    )
    monkeypatch.setattr(model_api_ocr, 'iter_api_candidates', lambda *_args: [])
    with pytest.raises(RetryExhaustedError) as error:
        asyncio.run(model_api_ocr.BaseAPIOCR._infer(ocr, None, [], config().ocr))
    assert FailureSkipPolicy(True, 0).can_skip(error.value)


@pytest.mark.parametrize('kind', [Translator.openai, Translator.openai_hq])
def test_task_batch_does_not_charge_pages_without_request_text(monkeypatch, kind):
    translator = engine(monkeypatch, batch_size=2)
    cfg = config(kind)
    images = [Image.new('RGB', (2, 2)) for _ in range(2)]
    for index, image in enumerate(images):
        image.name = f'{index}.png'

    async def preprocess(image, _config):
        text = None if image.name == '0.png' else 'source'
        return Context(input=image, image_name=image.name, text_regions=[SimpleNamespace(text=text)])

    async def translate(texts, *_args, **_kwargs):
        assert texts == ['source']
        raise RetryExhaustedError('exhausted')

    async def finish(ctx, _config):
        ctx.success = True
        return ctx

    monkeypatch.setattr(translator, '_set_image_context', lambda *_args: None)
    monkeypatch.setattr(translator, '_restore_image_context', lambda *_args: True)
    monkeypatch.setattr(translator, '_save_current_image_context', lambda *_args: None)
    monkeypatch.setattr(translator, '_translate_until_translation', preprocess)
    monkeypatch.setattr(translator, '_batch_translate_texts', translate)
    monkeypatch.setattr(translator, '_complete_translation_pipeline', finish)
    translator._detector_cleanup_task = object()

    async def run_task():
        results = await translator.translate_batch([(image, cfg) for image in images])
        return results, translator.failure_skip_policy.skipped_items

    try:
        results, skipped_items = asyncio.run(run_task())
    finally:
        for image in images:
            image.close()
    assert results[0].success
    assert not results[0].skipped
    assert results[1].skipped
    assert len(skipped_items) == 1


# --- P3-4: the ContextVar default must not be a shared mutable policy -------

def test_failure_policy_default_is_per_context_not_shared(monkeypatch):
    """A shared default instance leaked skip counts between requests."""
    translator = engine(monkeypatch, skip_failed_after_retries=True, max_failure_skips=0)

    async def read_in_fresh_context():
        # A new task starts with an empty context: it must get its own policy.
        return translator.failure_skip_policy

    first = asyncio.run(read_in_fresh_context())
    second = asyncio.run(read_in_fresh_context())
    assert first is not second
    assert first.max_skips == second.max_skips == 0
    assert first.skipped_items == [] and second.skipped_items == []


def test_failure_policy_is_stable_within_one_context(monkeypatch):
    translator = engine(monkeypatch)

    async def read_twice():
        return translator.failure_skip_policy, translator.failure_skip_policy

    first, second = asyncio.run(read_twice())
    assert first is second, 'the policy must survive the task for its error summary'


def test_skips_do_not_leak_between_tasks(monkeypatch):
    translator = engine(monkeypatch, skip_failed_after_retries=True, max_failure_skips=0)

    async def record_one(name):
        policy = translator.failure_skip_policy
        policy.record(Context(image_name=name, translation_error='timeout'),
                      RetryExhaustedError('timeout'))
        return len(policy.skipped_items)

    assert asyncio.run(record_one('a.png')) == 1
    # A second task must start clean rather than inherit a.png.
    assert asyncio.run(record_one('b.png')) == 1


def test_finished_task_summary_is_readable_but_not_shared(monkeypatch):
    """Summaries are read after the task ends; a live task must not borrow them."""
    translator = engine(monkeypatch, skip_failed_after_retries=True, max_failure_skips=0)

    async def record(name):
        policy = translator.failure_skip_policy
        policy.record(Context(image_name=name, translation_error='timeout'),
                      RetryExhaustedError('timeout'))
        return len(policy.skipped_items)

    async def concurrent_read():
        # A task started after the first one finished must still start clean.
        return len(translator.failure_skip_policy.skipped_items)

    asyncio.run(record('a.png'))
    # No event loop any more: this is how the desktop/CLI read the summary
    # after asyncio.run() returns.
    assert len(translator.failure_skip_policy.skipped_items) == 1, (
        'the finished task summary must remain readable outside the loop'
    )
    # But a new task must not inherit that tally.
    assert asyncio.run(concurrent_read()) == 0


# --- P3-5: max_skips counts allowed skips --------------------------------

def test_max_skips_of_one_aborts_on_the_first_skip():
    """The limit is checked after recording, so 1 allows exactly one skip."""
    policy = FailureSkipPolicy(True, 1)
    with pytest.raises(FailureSkipLimitExceeded, match=r'\(1/1\)'):
        policy.record(Context(image_name='a.png', translation_error='timeout'),
                      RetryExhaustedError('timeout'))
    assert len(policy.skipped_items) == 1


def test_max_skips_zero_means_unlimited():
    policy = FailureSkipPolicy(True, 0)
    for index in range(20):
        policy.record(Context(image_name=f'{index}.png', translation_error='timeout'),
                      RetryExhaustedError('timeout'))
    assert len(policy.skipped_items) == 20


# --- P3-2: a limit error is honoured even without a latched limit ---------

def test_limit_error_is_reraised_even_when_policy_has_no_latched_limit(monkeypatch):
    translator = engine(monkeypatch)
    error = FailureSkipLimitExceeded('limit reached elsewhere')
    with pytest.raises(FailureSkipLimitExceeded):
        translator._can_continue_after_error(error)
    with pytest.raises(FailureSkipLimitExceeded):
        translator._mark_context_failure(Context(image_name='a.png'), error, stage='translation')


# --- P3-3: the skip limit is not a cancellation --------------------------

def test_check_cancelled_raises_limit_error_not_cancelled_error(monkeypatch):
    """`CancelledError` made "too many skips" look like a user stop."""
    translator = engine(monkeypatch)
    policy = FailureSkipPolicy(True, 1)
    with pytest.raises(FailureSkipLimitExceeded):
        policy.record(Context(image_name='a.png', translation_error='timeout'),
                      RetryExhaustedError('timeout'))
    translator.failure_skip_policy = policy
    # The latch is set, so the cancellation check must surface the real cause.
    with pytest.raises(FailureSkipLimitExceeded):
        translator._check_cancelled()


def test_check_cancelled_still_raises_cancelled_error_for_user_stop(monkeypatch):
    translator = engine(monkeypatch)
    translator.set_cancel_check_callback(lambda: True)
    with pytest.raises(asyncio.CancelledError):
        translator._check_cancelled()


# --- P3-6: pre-check exhaustion uses RetryExhaustedError ----------------

def test_global_attempt_precheck_raises_retry_exhausted(monkeypatch):
    """A plain Exception fell through the skip policy as a local error."""
    class Translator(CommonTranslator):
        async def _translate(self, from_lang, to_lang, queries, ctx=None):
            return queries

    instance = Translator()
    instance.logger = SimpleNamespace(error=lambda *a, **k: None)
    instance._max_total_attempts = 1
    instance._global_attempt_count = 1
    instance._reset_global_attempt_count = lambda: None

    async def call():
        return await instance._translate_with_split(lambda *a, **k: None, ['text'])

    with pytest.raises(RetryExhaustedError, match='max total attempts'):
        asyncio.run(call())
    # And the policy now recognises it as skippable rather than a hard error.
    assert FailureSkipPolicy(True, 0).can_skip(
        RetryExhaustedError('Translation failed: reached max total attempts (1)')
    )


# --- P3-7: loose retry markers no longer swallow config errors ----------

@pytest.mark.parametrize('message', [
    'connection is not configured',
    'unsupported connection mode',
    'the connect stage is disabled',
])
def test_configuration_phrases_are_not_retryable(message):
    assert not is_retryable_api_error(RuntimeError(message))


@pytest.mark.parametrize('message', [
    'connection reset by peer',
    'connection refused',
    'failed to connect to host',
    'network is unreachable',
    'read timed out',
    'HTTP 503 Service Unavailable',
])
def test_real_transport_failures_remain_retryable(message):
    assert is_retryable_api_error(RuntimeError(message))

