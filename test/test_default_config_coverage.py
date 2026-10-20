import _bootstrap  # noqa: F401

import json
from pathlib import Path

from core.config_models import AppSettings
from manga_translator.config import Config
from ui.main_page.dynamic_settings import _FIXED_PROMPT_KEYS


ROOT = Path(_bootstrap.ROOT)
SENSITIVE_CONFIG_FIELDS = {"translator.user_api_key"}


def _config_paths(value, prefix=""):
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        yield path
        if isinstance(item, dict):
            yield from _config_paths(item, path)


def _example():
    return json.loads((ROOT / "config" / "config-example.json").read_text(encoding="utf-8"))


def test_default_config_covers_ui_and_non_sensitive_engine_settings():
    example = _example()
    expected = set(_config_paths(AppSettings().model_dump(mode="json")))
    expected.update(_config_paths(Config().model_dump(mode="json")))
    actual = set(_config_paths(example))
    assert not (expected - SENSITIVE_CONFIG_FIELDS - actual)
    assert not (actual & SENSITIVE_CONFIG_FIELDS)
    assert AppSettings.model_validate(example).model_dump(mode="json") == example



def test_every_settings_page_parameter_has_a_default():
    layout = json.loads((ROOT / "desktop_qt_ui" / "ui" / "main_page" / "settings_tab_layout.json").read_text(encoding="utf-8"))
    settings = {
        item for tab in layout["tabs"] for item in tab["items"]
        if isinstance(item, str)
    }
    # These three entries are buttons editing standalone prompt files, not
    # configuration parameters. Their paths are fixed by the prompt loaders.
    assert not (settings - _FIXED_PROMPT_KEYS - set(_config_paths(_example())))


def test_non_sensitive_settings_survive_desktop_and_engine_round_trips():
    example = _example()
    example["force_simple_sort"] = True
    example["app"]["editor_center_scale_enabled"] = True
    example["render"]["force_strict_layout"] = True
    example["cli"].update(save_lossless=True, compression_effort=0, save_quality=63)
    example["translator"].update(
        skip_lang="JPN,ENG",
        translator_chain="openai:JPN;gemini:ENG",
        selective_translation="openai:JPN;gemini:ENG",
        user_api_base="http://localhost:8000/v1",
        user_api_model="local-model",
        enable_post_translation_check=True,
        post_check_max_retry_attempts=6,
        post_check_repetition_threshold=30,
        post_check_target_lang_threshold=0.75,
    )
    saved = AppSettings.model_validate(example).model_dump(mode="json")
    assert saved == example
    restored = Config.model_validate(saved).model_dump(mode="json")
    assert restored["force_simple_sort"] is True
    assert restored["render"]["force_strict_layout"] is True
    for key, value in example["translator"].items():
        assert restored["translator"][key] == value
    for key in ("save_lossless", "compression_effort", "save_quality"):
        assert restored["cli"][key] == example["cli"][key]


def test_api_credentials_are_not_added_to_desktop_settings():
    data = _example()
    data["translator"]["user_api_key"] = "test-secret"
    saved = AppSettings.model_validate(data).model_dump(mode="json")
    assert "user_api_key" not in saved["translator"]
    assert "test-secret" not in json.dumps(saved)
