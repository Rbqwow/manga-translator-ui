"""GUI and MCP translation must never start on the GPU at the same time.

The GUI used to check McpJobManager.has_active_job() and mark itself busy only
later, while create_translation_job() checked is_gui_task_active() from an MCP
worker thread; a job created in between saw neither, and both pipelines ran.
"""

import _bootstrap  # noqa: F401, I001

from types import SimpleNamespace

import pytest

import services
from app_logic import MainAppLogic
from manga_translator.mcp.job_manager import McpJobManager
from test_mcp_pipeline import _base_config, _make_block, _make_png, _make_stub_factory


def test_gui_reservation_blocks_mcp_jobs_until_released(tmp_path):
    out_dir = tmp_path / "out"
    page = _make_png(tmp_path / "p1.png")
    factory, _ = _make_stub_factory({"p1.png": [_make_block("互斥")]})
    cfg = _base_config(str(out_dir))
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg, translator_factory=factory)
    try:
        assert mgr.try_reserve_gpu_for_gui() is True
        with pytest.raises(RuntimeError, match="GUI"):
            mgr.create_translation_job(inputs=[page], output_dir=str(out_dir))
        with pytest.raises(RuntimeError, match="GUI"):
            mgr.unload_models()
        assert mgr.get_workspace_info().busy is True

        mgr.release_gpu_for_gui()
        job = mgr.create_translation_job(inputs=[page], output_dir=str(out_dir))
        assert mgr.try_reserve_gpu_for_gui() is False, "an MCP job already owns the GPU"
        mgr.cancel_job(job.job_id)
    finally:
        mgr.shutdown()


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def _logic(events):
    return SimpleNamespace(
        _start_backend_task_reserved=lambda: events.append("start"),
        _ui_log=lambda *_args, **_kwargs: None,
        warning_dialog_requested=_Signal(),
        _t=lambda key, **_kwargs: key,
    )


class _Mcp:
    def __init__(self, events, *, grant=True):
        self.events = events
        self.grant = grant

    def try_reserve_gpu_for_gui(self):
        self.events.append("reserve")
        return self.grant

    def release_gpu_for_gui(self):
        self.events.append("release")


def test_gui_start_holds_the_reservation_until_its_task_is_started(monkeypatch):
    events = []
    monkeypatch.setattr(services, "get_mcp_service", lambda: _Mcp(events))

    MainAppLogic.start_backend_task(_logic(events))

    assert events == ["reserve", "start", "release"]


def test_gui_start_is_refused_while_an_mcp_job_owns_the_gpu(monkeypatch):
    events = []
    monkeypatch.setattr(services, "get_mcp_service", lambda: _Mcp(events, grant=False))
    logic = _logic(events)

    MainAppLogic.start_backend_task(logic)

    assert events == ["reserve"], "neither started nor released a reservation it never got"
    assert logic.warning_dialog_requested.emitted == [("MCP Active Job Blocks GUI Task",)]


def test_gui_start_releases_the_reservation_when_starting_fails(monkeypatch):
    events = []
    monkeypatch.setattr(services, "get_mcp_service", lambda: _Mcp(events))
    logic = _logic(events)

    def _fail():
        raise RuntimeError("scan could not start")

    logic._start_backend_task_reserved = _fail
    with pytest.raises(RuntimeError):
        MainAppLogic.start_backend_task(logic)

    assert events == ["reserve", "release"]


def test_gui_reservation_does_not_load_the_mcp_stack_and_carries_over(tmp_path):
    from services.mcp_service import McpService

    svc = McpService(root_dir=str(tmp_path))
    try:
        assert svc.try_reserve_gpu_for_gui() is True
        assert svc._job_manager is None, "starting a GUI task must not create the MCP job manager"

        # An MCP manager created while the GUI task starts inherits the reservation.
        assert svc.job_manager._gui_busy_locked() is True
        svc.release_gpu_for_gui()
        assert svc.job_manager._gui_reservations == 0
    finally:
        svc.shutdown()


def test_gui_reservation_keeps_the_idle_unload_timer_armed(tmp_path):
    cfg = _base_config(str(tmp_path / "out"), models_idle_ttl=300)
    mgr = McpJobManager(root_dir=str(_bootstrap.ROOT), get_config=lambda: cfg)
    try:
        with mgr._lock:
            mgr._schedule_idle_cleanup_locked()
        timer = mgr._idle_timer
        assert timer is not None

        assert mgr.try_reserve_gpu_for_gui() is True
        mgr.release_gpu_for_gui()

        assert mgr._idle_timer is timer, "a GUI task must not drop the pending idle unload"
    finally:
        mgr.shutdown()
