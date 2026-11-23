"""The admin "release memory" action must actually unload model caches.

`reset_global_translator()` used to call a non-existent `unload_models()` behind
a `hasattr` guard, so releasing memory only dropped the translator reference
while the module-level caches kept the weights resident. It also had no caller
at all, which made the fix unreachable from the server.
"""

import _bootstrap  # noqa: F401

import asyncio
from types import SimpleNamespace

import pytest

from manga_translator.server.core import task_manager
from manga_translator.server.routes import admin as admin_routes


@pytest.fixture
def unloaded(monkeypatch):
    calls = []

    def fake_cleanup(unload_models=False):
        calls.append(unload_models)
        return 3

    monkeypatch.setattr(task_manager.model_cleanup, "cleanup_model_caches_sync", fake_cleanup)
    monkeypatch.setattr(task_manager, "_global_translator", object(), raising=False)
    monkeypatch.setattr(task_manager, "_translator_params_hash", "hash", raising=False)
    return calls


def test_reset_unloads_caches_and_drops_the_shared_translator(unloaded):
    result = task_manager.reset_global_translator()

    assert unloaded == [True], "caches must be unloaded, not just the reference dropped"
    assert result["unloaded"] == 3
    assert result["success"] is True
    assert task_manager._global_translator is None
    assert task_manager._translator_params_hash is None


def test_reset_still_unloads_when_the_translator_was_already_dropped(unloaded):
    """A settings change resets the instance but leaves the caches populated."""
    task_manager._global_translator = None

    result = task_manager.reset_global_translator()

    assert unloaded == [True]
    assert result["unloaded"] == 3


def test_reset_reports_nothing_to_do_when_there_is_nothing_to_release(monkeypatch):
    monkeypatch.setattr(
        task_manager.model_cleanup, "cleanup_model_caches_sync",
        lambda unload_models=False: 0,
    )
    monkeypatch.setattr(task_manager, "_global_translator", None, raising=False)

    result = task_manager.reset_global_translator()

    assert result["success"] is True
    assert result["unloaded"] == 0


def test_admin_exposes_a_post_endpoint_that_reaches_the_reset():
    """Without a route the reset was dead code no administrator could trigger."""
    # Assert on the router itself: this FastAPI version includes routers lazily
    # (`_IncludedRouter`), so a freshly created app exposes no leaf routes yet.
    routes = {
        route.path: route.methods
        for route in admin_routes.router.routes
        if route.path.endswith("/translator/reset")
    }

    assert routes == {"/admin/translator/reset": {"POST"}}


def test_endpoint_returns_the_reset_result(monkeypatch):
    calls = []

    def fake_reset():
        calls.append(True)
        return {"success": True, "unloaded": 2, "message": "已卸载 2 个模型缓存"}

    monkeypatch.setattr(task_manager, "reset_global_translator", fake_reset)

    result = asyncio.run(
        admin_routes.reset_translator_endpoint(
            session=SimpleNamespace(username="admin"),
        )
    )

    assert calls == [True]
    assert result["unloaded"] == 2


def test_reset_is_refused_while_a_translation_task_runs(unloaded):
    """Tasks load models from the same caches; unloading under them fails them."""
    translator = task_manager._global_translator

    with task_manager._model_gate.use():
        result = task_manager.reset_global_translator()

    assert result["success"] is False
    assert result["busy"] is True
    assert unloaded == [], "caches must not be unloaded while a task runs"
    assert task_manager._global_translator is translator


def test_translator_thread_holds_the_model_gate_for_the_whole_task():
    seen = []

    def task():
        seen.append(task_manager._model_gate.users)
        return "done"

    assert asyncio.run(task_manager.run_in_translator_thread(task)) == "done"
    assert seen == [1]
    assert task_manager._model_gate.users == 0


def test_new_translation_waits_for_an_unload_in_progress(monkeypatch):
    import threading

    unload_started = threading.Event()
    finish_unload = threading.Event()
    order = []

    def slow_cleanup(unload_models=False):
        unload_started.set()
        finish_unload.wait(timeout=5.0)
        order.append("unload")
        return 1

    monkeypatch.setattr(task_manager.model_cleanup, "cleanup_model_caches_sync", slow_cleanup)
    resetter = threading.Thread(target=task_manager.reset_global_translator, daemon=True)
    resetter.start()
    assert unload_started.wait(timeout=5.0)

    def task():
        order.append("task")

    runner = threading.Thread(
        target=lambda: asyncio.run(task_manager.run_in_translator_thread(task)), daemon=True
    )
    runner.start()
    runner.join(timeout=0.3)
    assert order == [], "a task must not load models while they are being unloaded"

    finish_unload.set()
    resetter.join(timeout=5.0)
    runner.join(timeout=5.0)
    assert order == ["unload", "task"]


def test_model_gate_refuses_exclusive_access_while_in_use():
    gate = task_manager._ModelUsageGate()
    with gate.use():
        with gate.try_exclusive() as acquired:
            assert acquired is False
    with gate.try_exclusive() as acquired:
        assert acquired is True
    assert gate.users == 0


def test_endpoint_reports_a_refused_reset_as_conflict(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        task_manager,
        "reset_global_translator",
        lambda: {"success": False, "busy": True, "unloaded": 0, "message": "有 1 个翻译任务正在运行"},
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(admin_routes.reset_translator_endpoint(session=SimpleNamespace(username="admin")))

    assert excinfo.value.status_code == 409
