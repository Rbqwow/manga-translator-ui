import _bootstrap  # noqa: F401

import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtWidgets import QApplication

from app_logic import MainAppLogic
from core.config_models import AppSettings
from services import config_service as config_module
from services.config_service import ConfigService


@pytest.fixture(scope="session")
def config_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def service(config_app, tmp_path, monkeypatch):
    template = tmp_path / "config-example.json"
    user = tmp_path / "config.json"
    config = AppSettings()
    config.render.font_family = "Saved Font::Medium"
    config.render.center_text_in_bubble = True
    config.ocr.use_hybrid_ocr = True
    original = json.dumps(config.model_dump(), ensure_ascii=False, indent=4) + "\n"
    template.write_text(original, encoding="utf-8")
    user.write_text(original, encoding="utf-8")
    monkeypatch.setenv(config_module.APP_DOTENV_PATH_ENV, str(tmp_path / ".env"))
    monkeypatch.setattr(ConfigService, "get_default_config_path", lambda self: str(template))
    monkeypatch.setattr(ConfigService, "get_user_config_path", lambda self: str(user))
    for name in (
        "ensure_custom_api_params_file", "ensure_ai_ocr_prompt_file",
        "ensure_ai_renderer_prompt_file", "ensure_ai_colorizer_prompt_file",
    ):
        monkeypatch.setattr(config_module, name, lambda *args, **kwargs: None)
    instance = ConfigService(str(tmp_path))
    yield instance
    instance.shutdown()


def _logic(service):
    return SimpleNamespace(
        config_service=service,
        logger=logging.getLogger(__name__),
        translation_service=Mock(),
        render_setting_changed=Mock(),
    )


@pytest.mark.parametrize("frozen", [False, True])
def test_autosave_only_updates_user_file_in_source_and_packaged_runs(service, monkeypatch, frozen):
    from pathlib import Path

    template = Path(service.default_config_path)
    original = template.read_bytes()
    mtime = template.stat().st_mtime_ns
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    MainAppLogic.update_single_config(_logic(service), "render.center_text_in_bubble", False)
    assert service.flush_pending_writes()
    saved = json.loads(Path(service.user_config_path).read_text(encoding="utf-8"))
    assert saved["render"]["center_text_in_bubble"] is False
    assert saved["render"]["font_family"] == "Saved Font::Medium"
    assert saved["ocr"]["use_hybrid_ocr"] is True
    assert template.read_bytes() == original
    assert template.stat().st_mtime_ns == mtime


def test_unchanged_value_does_not_emit_or_schedule_a_save(service):
    changes = []
    service.config_changed.connect(changes.append)
    logic = _logic(service)
    MainAppLogic.update_single_config(logic, "render.center_text_in_bubble", True)
    assert changes == []
    assert service._pending_config_writes == {}
    assert not service._write_timer.isActive()
    logic.render_setting_changed.emit.assert_not_called()


def test_toggle_round_trip_and_repeated_save_preserve_bytes_and_mtime(service, monkeypatch):
    from pathlib import Path

    user = Path(service.user_config_path)
    original = user.read_bytes()
    mtime = user.stat().st_mtime_ns
    write = Mock(wraps=ConfigService._atomic_write_text)
    monkeypatch.setattr(ConfigService, "_atomic_write_text", write)
    logic = _logic(service)
    MainAppLogic.update_single_config(logic, "ocr.use_hybrid_ocr", False)
    MainAppLogic.update_single_config(logic, "ocr.use_hybrid_ocr", True)
    assert service.flush_pending_writes()
    assert service.save_config_file()
    assert service.flush_pending_writes()
    write.assert_not_called()
    assert user.read_bytes() == original
    assert user.stat().st_mtime_ns == mtime


def test_explicit_export_keeps_selected_values_and_next_autosave_targets_user(service, tmp_path):
    from pathlib import Path

    export = tmp_path / "export.json"
    service.current_config.detector.min_box_area_ratio = 0.012
    payload = service.get_config().model_dump()
    assert service.save_config_file(str(export))
    assert json.loads(export.read_text(encoding="utf-8")) == payload
    original_export = export.read_bytes()
    service.current_config.app.theme = "dark"
    assert service.save_config_file()
    assert service.flush_pending_writes()
    assert export.read_bytes() == original_export
    assert json.loads(Path(service.user_config_path).read_text(encoding="utf-8"))["app"]["theme"] == "dark"


def test_reading_template_does_not_make_it_an_autosave_target(service):
    from pathlib import Path

    template = Path(service.default_config_path)
    original = template.read_bytes()
    assert service.load_default_config()
    assert service.config_path == service.default_config_path
    service.current_config.app.theme = "dark"
    assert service.save_config_file()
    assert service.flush_pending_writes()
    assert template.read_bytes() == original
    assert service.config_path == service.user_config_path


def test_changed_payload_still_reports_write_failures(service, monkeypatch):
    def fail_write(*args):
        raise PermissionError("read-only destination")

    monkeypatch.setattr(ConfigService, "_atomic_write_text", fail_write)
    service.current_config.app.theme = "dark"
    assert service.save_config_file()
    assert not service.flush_pending_writes()
