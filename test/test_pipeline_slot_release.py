import _bootstrap  # noqa: F401

import asyncio
import threading

import pytest

from manga_translator.utils import Context
from manga_translator.utils.concurrent_pipeline import ConcurrentPipeline
from test_translation_concurrency import FakeTranslator, build_pipeline


def test_missing_base_context_releases_its_page_slot(monkeypatch):
    """Every other rendering exit released the page slot; this one did not.

    The run-time leak only surfaced after max_inflight_pages such pages, when
    detection blocked forever in _acquire_page_slot.
    """
    pipeline, names = build_pipeline(FakeTranslator(lambda *_: None), count=4)

    calls = []
    original_finish = pipeline._finish_context

    def tracking_finish(ctx):
        calls.append('finished')
        original_finish(ctx)

    monkeypatch.setattr(pipeline, '_finish_context', tracking_finish)
    assert pipeline._page_slots._value == pipeline.max_inflight_pages

    ctx = Context(image_name='ghost.png', text_regions=[])
    pipeline._acquire_page_slot('ghost.png')
    assert pipeline._page_slots._value == pipeline.max_inflight_pages - 1

    # Mirror the rendering thread: the context is absent from base_contexts,
    # so the branch must release the slot on its way out.
    pipeline.base_contexts.pop('ghost.png', None)
    with pipeline._lock:
        verified = pipeline.base_contexts.get(ctx.image_name)
    if not verified:
        pipeline._finish_context(ctx)

    assert pipeline._page_slots._value == pipeline.max_inflight_pages
    assert 'ghost.png' not in pipeline._inflight_pages
    assert calls == ['finished']


def test_stop_releases_every_page_slot():
    """A cancelled run must not leave slots held for the next task."""
    pipeline, names = build_pipeline(FakeTranslator(lambda *_: None), count=6)
    assert pipeline.max_inflight_pages == 3 * (3 + 1)

    held = []
    for name in names[:4]:
        pipeline._acquire_page_slot(name)
        held.append(name)
    assert pipeline._page_slots._value == pipeline.max_inflight_pages - 4

    for name in held:
        pipeline._release_page_slot(name)
    assert pipeline._page_slots._value == pipeline.max_inflight_pages
    assert pipeline._inflight_pages == set()


def test_discard_pending_contexts_releases_slots():
    """Cleanup is the last line of defence for slots held by pending pages."""
    pipeline, names = build_pipeline(FakeTranslator(lambda *_: None), count=3)
    for name in names:
        pipeline._acquire_page_slot(name)
        pipeline.base_contexts[name] = Context(image_name=name, text_regions=[])
    assert pipeline._page_slots._value == pipeline.max_inflight_pages - 3

    pipeline._discard_pending_contexts()
    assert pipeline._page_slots._value == pipeline.max_inflight_pages
    assert pipeline._inflight_pages == set()
