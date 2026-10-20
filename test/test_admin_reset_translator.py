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
