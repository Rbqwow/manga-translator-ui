import _bootstrap  # noqa: F401, I001

import asyncio
import importlib
import threading

import pytest

from manga_translator.utils.failure_policy import FailureSkipLimitExceeded
from manga_translator.utils.retry import RetryExhaustedError
from test_failure_skip_policy import config, engine
from test_translation_concurrency import build_pipeline, wait_until


@pytest.mark.parametrize('limit', [0, 2])
def test_task_policy_reaches_every_pipeline_worker(monkeypatch, limit):
    translator = engine(
        monkeypatch, batch_concurrent=True, translation_concurrency=3,
        batch_size=1, max_failure_skips=limit,
    )
    default_policy = translator.failure_skip_policy
    pipeline, names = build_pipeline(translator, count=8, batch_size=1, workers=3)
    worker_policies = []
    request_policies = []
    request_threads = set()
    observations_lock = threading.Lock()
    workers_started = threading.Barrier(6)

    def observe_worker(worker, stage):
        def run(*args):
            with observations_lock:
                worker_policies.append((stage, translator.failure_skip_policy))
            # All six workers enter before any failure can stop the pipeline.
            workers_started.wait(timeout=5)
            return worker(*args)

        return run

    for method, stage in [
        ('_detection_ocr_thread', 'detection'),
        ('_translation_thread', 'translation'),
        ('_inpaint_thread', 'inpainting'),
        ('_render_thread', 'rendering'),
    ]:
        monkeypatch.setattr(pipeline, method, observe_worker(getattr(pipeline, method), stage))

    async def fail(*_args, **_kwargs):
        with observations_lock:
            request_threads.add(threading.get_ident())
            request_policies.append(translator.failure_skip_policy)
        # Exercise policy lookup inside each worker's asyncio task as well.
        await wait_until(lambda: len(request_threads) == 3)
        raise RetryExhaustedError('API retries exhausted')

    monkeypatch.setattr(translator, '_batch_translate_texts', fail)
    pipeline_module = importlib.import_module('manga_translator.utils.concurrent_pipeline')
    monkeypatch.setattr(pipeline_module, 'ConcurrentPipeline', lambda *_args, **_kwargs: pipeline)

    async def run():
        # Use the public entry point so it binds a fresh task policy rather
        # than exercising only the constructor's ContextVar default.
        if limit:
            with pytest.raises(FailureSkipLimitExceeded):
                await translator.translate_batch([(name, config()) for name in names])
        else:
            results = await translator.translate_batch([(name, config()) for name in names])
            assert len(results) == len(names)
            assert all(ctx.skipped for ctx in results)
        return translator.failure_skip_policy

    task_policy = asyncio.run(run())
    assert task_policy is not default_policy
    assert sorted(stage for stage, _policy in worker_policies) == [
        'detection', 'inpainting', 'rendering', 'translation', 'translation', 'translation',
    ]
    assert all(policy is task_policy for _stage, policy in worker_policies)
    assert len(request_threads) == 3
    assert all(policy is task_policy for policy in request_policies)
    assert len(task_policy.skipped_items) == (limit or len(names))
    assert default_policy.skipped_items == []
