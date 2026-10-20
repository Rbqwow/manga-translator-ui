import _bootstrap  # noqa: F401

import asyncio
import os
import queue
import sys
from contextvars import ContextVar
from copy import deepcopy
from types import SimpleNamespace

import pytest

import manga_translator
from manga_translator.mode import subprocess_manager
from manga_translator.utils.failure_policy import FailureSkipLimitExceeded, FailureSkipPolicy
from manga_translator.utils.retry import RetryExhaustedError


@pytest.fixture
def subprocess_task(monkeypatch):
    """Exercise the real worker/parent protocol without models or processes."""
    monkeypatch.setattr(sys, 'path', list(sys.path))
    state = SimpleNamespace(calls=[], limits=[], processes=[], queues=[], results=[])
    exhausted = set()
    existing = set()
    broken = set()

    class Translator:
        def __init__(self, params):
            state.limits.append(params['max_failure_skips'])
            self._policy_options = (
                params['skip_failed_after_retries'], params['max_failure_skips'],
            )
            self._failure_skip_policy = ContextVar(
                'failure_skip_policy', default=FailureSkipPolicy(),
            )
            self._last_batch_partial_results = []

        @property
        def failure_skip_policy(self):
            return self._failure_skip_policy.get()

        async def translate_batch(self, items, save_info):
            # Match the real translator's per-task policy. Reading after
            # asyncio.run exits would return the empty default policy.
            self._failure_skip_policy.set(FailureSkipPolicy(*self._policy_options))
            results = []
            # The real engine publishes finished pages here, so a limit abort
            # still reports what was already written.
            self._last_batch_partial_results = results
            for path, _config in items:
                state.calls.append(path)
                ctx = SimpleNamespace(
                    image_name=path, success=True, skipped=path in existing,
                    translation_error=None, skip_message='Already exists' if path in existing else None,
                )
                if path in broken:
                    raise ValueError('bad input')
                if path in exhausted:
                    error = RetryExhaustedError('API unavailable')
                    ctx.translation_error = str(error)
                    if not self.failure_skip_policy.record(ctx, error):
                        raise error
                results.append(ctx)
            return results

    class ResultQueue(queue.Queue):
        closed = False
        drained = False

        def __init__(self):
            super().__init__()
            state.queues.append(self)

        def put(self, result):
            state.results.append(deepcopy(result))
            super().put(result)

        def close(self):
            self.closed = True

        def join_thread(self):
            self.drained = True

    class Process:
        joined = False

        def __init__(self, *, target, args):
            self.target, self.args = target, args
            state.processes.append(self)

        def start(self):
            # A real Process runs its own asyncio loop; emulate that isolation
            # while retaining the actual worker and its serialized result.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(self.target, *self.args).result()

        def join(self, timeout):
            self.joined = True

        def is_alive(self):
            return False

    monkeypatch.setattr(manga_translator, 'MangaTranslator', Translator)
    monkeypatch.setattr('manga_translator.utils.init_logging', lambda: None)
    monkeypatch.setattr('manga_translator.utils.set_log_level', lambda _level: None)
    monkeypatch.setattr(subprocess_manager.multiprocessing, 'Process', Process)
    monkeypatch.setattr(subprocess_manager.multiprocessing, 'Queue', ResultQueue)
    monkeypatch.setattr(subprocess_manager, 'get_memory_usage_mb', lambda: 0)
    monkeypatch.setattr(subprocess_manager, 'get_total_memory_mb', lambda: 1000)

    def run(paths, *, limit=3, enabled=True, batch_size=2, failures=(), skips=(), errors=()):
        exhausted.update(failures)
        existing.update(skips)
        broken.update(errors)
        config = {'cli': {'skip_failed_after_retries': enabled, 'max_failure_skips': limit}}
        original_config = deepcopy(config)
        try:
            return asyncio.run(subprocess_manager.translate_with_subprocess(
                paths, '.', config, False, False, batch_per_restart=batch_size,
            ))
        finally:
            assert config == original_config
            assert all(process.joined for process in state.processes)
            assert all(q.closed and q.drained for q in state.queues)

    return run, state


def test_failure_budget_is_shared_across_worker_restarts(subprocess_task):
    run, state = subprocess_task
    with pytest.raises(FailureSkipLimitExceeded, match=r'\(3/3\)'):
        run(list('abcdefgh'), failures='ace')
    assert state.calls == list('abcde')
    assert state.limits == [3, 2, 1]
    assert state.results[-1]['status'] == 'failure_skip_limit'
    assert [item['original_path'] for item in state.results[-1]['failure_skips']] == ['e']


def test_child_reaching_limit_stops_parent_before_next_batch(subprocess_task):
    run, state = subprocess_task
    with pytest.raises(FailureSkipLimitExceeded, match=r'\(2/2\)'):
        run(list('abcdef'), limit=2, failures='abcdef')
    assert state.calls == ['a', 'b']
    assert len(state.processes) == 1
    # The aborted batch still wrote its pages ("a" was skipped and recorded
    # before "b" tripped the limit), so its outcome must be reported instead
    # of vanishing from completed/skipped/failed.
    assert state.results[-1]['skipped'] == ['a']
    assert state.results[-1]['completed'] == []
    assert state.results[-1]['failed'] == []


def test_existing_output_skips_do_not_consume_failure_budget(subprocess_task):
    run, state = subprocess_task
    assert run(list('abcdefg'), limit=2, skips='abcd', failures='e') == (2, 5, 0)
    assert state.limits == [2, 2, 2, 1]
    assert state.results[0]['failure_skips'] == []


def test_zero_budget_stays_unlimited_across_restarts(subprocess_task):
    run, state = subprocess_task
    assert run(list('abcdef'), limit=0, failures='abcdef') == (0, 6, 0)
    assert state.limits == [0, 0, 0]


def test_disabled_policy_preserves_subprocess_error_handling(subprocess_task):
    run, state = subprocess_task
    assert run(list('abcd'), limit=1, enabled=False, batch_size=1, failures='abcd') == (0, 0, 4)
    assert state.calls == list('abcd')
    assert all(result['status'] == 'error' for result in state.results)


def test_other_failures_do_not_consume_retry_skip_budget(subprocess_task):
    run, state = subprocess_task
    assert run(list('abcd'), limit=2, batch_size=1, failures='b', errors='a') == (2, 1, 1)
    assert state.limits == [2, 2, 1, 1]


@pytest.mark.parametrize('alias', [
    './page.png',
    pytest.param('PAGE.PNG', marks=pytest.mark.skipif(os.name != 'nt', reason='Windows path identity')),
])
def test_duplicate_path_aliases_do_not_spend_budget_in_separate_workers(subprocess_task, alias):
    run, state = subprocess_task
    assert run(
        ['page.png', alias, 'other.png'], limit=2, batch_size=1,
        failures=['page.png', alias],
    ) == (1, 1, 0)
    assert state.calls == ['page.png', 'other.png']
    assert state.limits == [2, 1]
