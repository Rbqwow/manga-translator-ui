import _bootstrap  # noqa: F401

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image

from manga_translator.config import Config
from manga_translator.manga_translator import MangaTranslator
from manga_translator.mcp.batch_adapter import build_mcp_engine_config, load_job_guidelines_and_glossary
from manga_translator.mcp.job_manager import McpJobManager
from manga_translator.translators.common import CommonTranslator
from manga_translator.translators.gemini import GeminiTranslator
from manga_translator.translators.gemini_hq import GeminiHighQualityTranslator
from manga_translator.translators.openai import OpenAITranslator
from manga_translator.translators.openai_hq import OpenAIHighQualityTranslator
from manga_translator.translators.prompt_loader import (
    BUILTIN_TRANSLATION_PROMPTS,
    ensure_builtin_prompt_file,
    get_builtin_prompt_spec_by_path,
    load_builtin_prompt_text,
    load_custom_prompt,
    resolve_builtin_prompt_path,
    save_builtin_prompt_text,
)
from ui.secondary_pages.prompt_preview import PromptPreviewPanel


class _PromptTestTranslator(CommonTranslator):
    async def _translate(self, from_lang: str, to_lang: str, queries):
        return queries


def _make_png(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 160), (240, 240, 240)).save(path, format="PNG")
    return str(path)


def _base_mcp_config(output_dir: str, **translator_overrides) -> dict:
    translator_cfg = {
        "translator": "openai_hq",
        "target_lang": "CHS",
        "extract_glossary": False,
        "use_builtin_system_prompt": True,
        "high_quality_prompt_path": None,
        "enable_post_translation_check": False,
        "no_text_lang_skip": False,
    }
    translator_cfg.update(translator_overrides)
    return {
        "app": {
            "last_output_path": output_dir,
            "current_preset": "默认",
            "unload_models_after_translation": False,
        },
        "translator": translator_cfg,
        "ocr": {"ocr": "48px", "secondary_ocr": "mocr", "use_hybrid_ocr": False},
        "detector": {"detector": "default", "detection_size": 1024},
        "inpainter": {"inpainter": "lama_large", "inpainting_size": 1024},
        "render": {
            "renderer": "default",
            "disable_auto_wrap": False,
            "check_br_and_retry": False,
        },
        "colorizer": {"colorizer": "none"},
        "upscale": {"upscale_ratio": None},
        "cli": {
            "use_gpu": False,
            "batch_size": 3,
            "context_size": 3,
            "format": "png",
            "overwrite": True,
            "skip_no_text": True,
            "save_text": True,
        },
        "mcp": {
            "host": "127.0.0.1",
            "port": 8765,
            "auto_start": False,
            "translation_mode": "text",
            "allow_on_demand_image": False,
            "models_idle_ttl": 0,
            "use_gui_files_by_default": False,
        },
    }


def _stub_mcp_factory(translator_params):
    translator = MangaTranslator(params=translator_params)
    translator._mcp_context_enabled = True
    translator._models_loaded = True
    translator.start_model_cleanup_job = False
    return translator


def test_custom_prompt_has_highest_priority_and_precedes_builtin_and_linebreak():
    helper = _PromptTestTranslator()
    prompt = helper._build_system_prompt(
        source_lang="JPN",
        target_lang="CHS",
        custom_prompt_json={
            "system_prompt": "Always keep Japanese honorifics for {{{target_lang}}} translation.",
            "glossary": {"Person": []},
        },
        line_break_prompt_json={"line_break_prompt": "Insert [BR] markers carefully."},
        retry_attempt=1,
        retry_reason="count mismatch",
        extract_glossary=False,
        use_builtin_system_prompt=True,
    )

    retry_pos = prompt.index("Reason: count mismatch")
    custom_header_pos = prompt.index("USER CUSTOM TRANSLATION PROMPT (HIGHEST PRIORITY)")
    custom_body_pos = prompt.index("Always keep Japanese honorifics for Chinese (Simplified) translation.")
    builtin_header_pos = prompt.index("BUILT-IN DEFAULT TRANSLATION PROMPT (LOWER PRIORITY FALLBACK)")
    builtin_body_pos = prompt.index("You are a professional manga translator")
    linebreak_pos = prompt.index("Insert [BR] markers carefully.")
    format_pos = prompt.index("CRITICAL OUTPUT REQUIREMENTS")

    assert retry_pos < custom_header_pos < custom_body_pos < builtin_header_pos < builtin_body_pos < linebreak_pos < format_pos


def test_use_builtin_system_prompt_false_omits_base_prompt_with_real_custom_prompt():
    helper = _PromptTestTranslator()
    test_yaml = _bootstrap.ROOT / "dict" / "test.yaml"
    custom_data = load_custom_prompt(str(test_yaml))
    assert custom_data is not None

    prompt = helper._build_system_prompt(
        source_lang="JPN",
        target_lang="CHS",
        custom_prompt_json=custom_data,
        line_break_prompt_json={"line_break_prompt": "LINE BREAK RULE"},
        extract_glossary=True,
        use_builtin_system_prompt=False,
    )

    assert "USER CUSTOM TRANSLATION PROMPT (PRIMARY INSTRUCTIONS)" in prompt
    assert "资深汉化译者" in prompt
    assert "You are a professional manga translator" not in prompt
    assert "BUILT-IN DEFAULT TRANSLATION PROMPT" not in prompt
    assert "LINE BREAK RULE" in prompt
    assert "ADDITIONAL INSTRUCTION (GLOSSARY EXTRACTION)" in prompt
    assert "CRITICAL OUTPUT REQUIREMENTS" in prompt


@pytest.mark.parametrize(
    "custom_json",
    [
        None,
        load_custom_prompt(str(_bootstrap.ROOT / "dict" / "prompt_example.yaml")),
        {"system_prompt": "   ", "glossary": {"Person": [{"original": "太郎", "translation": "太郎"}]}},
    ],
)
def test_use_builtin_system_prompt_false_falls_back_when_no_custom_instructions(custom_json, caplog):
    helper = _PromptTestTranslator()
    with caplog.at_level(logging.WARNING):
        prompt = helper._build_system_prompt(
            source_lang="JPN",
            target_lang="CHS",
            custom_prompt_json=custom_json,
            extract_glossary=False,
            use_builtin_system_prompt=False,
        )

    assert "You are a professional manga translator" in prompt
    assert "falling back to built-in base prompt" in caplog.text


def test_all_api_translators_resolve_prompt_flags_consistently():
    cfg = Config()
    cfg.translator.extract_glossary = True
    cfg.translator.use_builtin_system_prompt = False

    custom_json = {"system_prompt": "Custom style rule only."}
    for cls in (
        OpenAITranslator,
        OpenAIHighQualityTranslator,
        GeminiTranslator,
        GeminiHighQualityTranslator,
    ):
        assert cls._resolve_prompt_flags(cfg, None) == (False, False)
        assert cls._resolve_prompt_flags(cfg, custom_json) == (True, False)

    helper = _PromptTestTranslator()
    oai = OpenAITranslator()
    oai_hq = OpenAIHighQualityTranslator()
    gem = GeminiTranslator()
    gem_hq = GeminiHighQualityTranslator()

    expected = helper._build_system_prompt(
        "JPN",
        "CHS",
        custom_prompt_json=custom_json,
        extract_glossary=True,
        use_builtin_system_prompt=False,
    )
    assert oai._get_system_prompt("JPN", "CHS", custom_prompt_json=custom_json, extract_glossary=True, use_builtin_system_prompt=False) == expected
    assert oai_hq._get_system_prompt("JPN", "CHS", custom_prompt_json=custom_json, extract_glossary=True, use_builtin_system_prompt=False) == expected
    assert gem._get_system_instruction("JPN", "CHS", custom_prompt_json=custom_json, extract_glossary=True, use_builtin_system_prompt=False) == expected
    assert gem_hq._get_system_instruction("JPN", "CHS", custom_prompt_json=custom_json, extract_glossary=True, use_builtin_system_prompt=False) == expected


@pytest.mark.parametrize("prompt_setting", [None, "dict/nonexistent_prompt_xyz.yaml"])
def test_mcp_extract_glossary_disabled_without_valid_custom_prompt_matches_api(tmp_path, prompt_setting):
    img = _make_png(tmp_path / "in" / "001.png")
    out_dir = str(tmp_path / "out")
    cfg = _base_mcp_config(
        out_dir,
        extract_glossary=True,
        use_builtin_system_prompt=False,
        high_quality_prompt_path=prompt_setting,
    )
    mgr = McpJobManager(
        root_dir=str(_bootstrap.ROOT),
        get_config=lambda: cfg,
        translator_factory=_stub_mcp_factory,
    )
    try:
        ws = mgr.get_workspace_info()
        assert ws.extract_glossary is True
        assert ws.use_builtin_system_prompt is False

        resp = mgr.create_translation_job(inputs=[img], output_dir=out_dir)
        assert resp.extract_glossary is False
        assert resp.use_builtin_system_prompt is False
        assert "ADDITIONAL INSTRUCTION (GLOSSARY EXTRACTION)" not in resp.guidelines
        assert '"new_terms"' not in resp.guidelines
        # No custom instructions loaded -> falls back to built-in base prompt identically to API.
        assert "You are a professional manga translator" in resp.guidelines

        internal_job = mgr._jobs[resp.job_id]
        assert internal_job.prompt_file_path is None
        assert internal_job.config.translator.extract_glossary is False
    finally:
        mgr.shutdown()


def test_mcp_guidelines_match_api_prompt_builder_with_custom_prompt_and_disabled_builtin(tmp_path):
    prompt_file = tmp_path / "custom.yaml"
    prompt_file.write_text(
        yaml.safe_dump(
            {
                "system_prompt": "Translate punchy dialogue for {{{target_lang}}}.",
                "glossary": {"Person": []},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    img = _make_png(tmp_path / "in" / "001.png")
    out_dir = str(tmp_path / "out")
    cfg = _base_mcp_config(
        out_dir,
        extract_glossary=True,
        use_builtin_system_prompt=False,
        high_quality_prompt_path=str(prompt_file),
    )

    mgr = McpJobManager(
        root_dir=str(_bootstrap.ROOT),
        get_config=lambda: cfg,
        translator_factory=_stub_mcp_factory,
    )
    try:
        resp = mgr.create_translation_job(inputs=[img], output_dir=out_dir)
        assert resp.extract_glossary is True
        assert resp.use_builtin_system_prompt is False
        assert "You are a professional manga translator" not in resp.guidelines

        helper = _PromptTestTranslator()
        engine_cfg, _ = build_mcp_engine_config(cfg, str(_bootstrap.ROOT))
        expected_api_prompt = helper._build_system_prompt(
            "",
            engine_cfg.translator.target_lang,
            custom_prompt_json=load_custom_prompt(str(prompt_file)),
            extract_glossary=True,
            use_builtin_system_prompt=False,
        )
        assert resp.guidelines.startswith(expected_api_prompt)
        assert "[MCP Submission Note]" in resp.guidelines
    finally:
        mgr.shutdown()


def test_builtin_prompt_loader_roundtrip_preserves_placeholders_and_indentation(tmp_path):
    assert len(BUILTIN_TRANSLATION_PROMPTS) == 4
    yaml_path = tmp_path / "system_prompt_hq_format.yaml"
    sample_text = (
        "**TARGET: {{{target_lang}}}**\n\n"
        "  1. Indented line\n"
        "  {{{optional_new_terms_rule}}}\n"
        "End."
    )
    ensure_builtin_prompt_file(str(yaml_path), "system_prompt_hq_format", sample_text)
    assert load_builtin_prompt_text(str(yaml_path), "system_prompt_hq_format") == sample_text

    updated = sample_text + "\n\nUpdated line"
    save_builtin_prompt_text(str(yaml_path), "system_prompt_hq_format", updated)
    assert load_builtin_prompt_text(str(yaml_path), "system_prompt_hq_format") == updated

    trailing_nl = sample_text + "\n"
    save_builtin_prompt_text(str(yaml_path), "system_prompt_hq_format", trailing_nl)
    assert load_builtin_prompt_text(str(yaml_path), "system_prompt_hq_format") == trailing_nl

    json_path = tmp_path / "system_prompt_line_break.json"
    save_builtin_prompt_text(str(json_path), "line_break_prompt", "Use [BR]")
    assert load_builtin_prompt_text(str(json_path), "line_break_prompt") == "Use [BR]"

    spec = get_builtin_prompt_spec_by_path(str(yaml_path))
    assert spec is not None
    assert spec["stem"] == "system_prompt_hq_format"
    assert spec["config_key"] is None
    assert get_builtin_prompt_spec_by_path(str(tmp_path / "custom.yaml")) is None

    resolved = resolve_builtin_prompt_path(str(tmp_path), "system_prompt_hq_format")
    assert resolved == str(yaml_path)


def test_prompt_preview_distinguishes_builtin_from_user_template_structured():
    for key in ("system_prompt_hq_format", "line_break_prompt", "glossary_extraction_prompt"):
        payload = {key: "content"}
        assert PromptPreviewPanel._is_builtin_prompt_structured(payload) is True
        assert PromptPreviewPanel._is_structured(payload) is False

    assert PromptPreviewPanel._is_builtin_prompt_structured({"system_prompt": "custom"}) is False
    assert PromptPreviewPanel._is_structured({"system_prompt": "custom"}) is True


def test_all_locale_files_parse_and_contain_new_builtin_prompt_keys():
    required_keys = {
        "label_use_builtin_system_prompt",
        "Built-in Prompts",
        "Builtin Prompts Priority Hint",
        "Builtin Base Translation Prompt",
        "AI Line Break Prompt",
        "Glossary Extraction Prompt",
        "Output Format Prompt",
        "Required",
        "Preview",
        "desc_builtin_system_prompt_hq",
        "desc_builtin_system_prompt_line_break",
        "desc_builtin_glossary_extraction_prompt",
        "desc_builtin_system_prompt_hq_format",
        "Builtin Format Prompt Required Hint",
    }
    locale_dir = _bootstrap.ROOT / "desktop_qt_ui" / "locales"
    for locale_file in sorted(locale_dir.glob("*.json")):
        data = json.loads(locale_file.read_text(encoding="utf-8"))
        missing = required_keys - set(data.keys())
        assert not missing, f"{locale_file.name} missing keys: {sorted(missing)}"
        for key in required_keys:
            assert isinstance(data[key], str) and data[key].strip(), f"{locale_file.name}[{key}] is empty"


def test_load_job_guidelines_and_glossary_returns_four_tuple(tmp_path):
    import asyncio

    cfg = _base_mcp_config(str(tmp_path / "out"), use_builtin_system_prompt=True)
    engine_cfg, params = build_mcp_engine_config(cfg, str(_bootstrap.ROOT))
    translator = _stub_mcp_factory(params)
    guidelines, glossary, resolved_path, effective_extract = asyncio.run(
        load_job_guidelines_and_glossary(translator, engine_cfg, str(_bootstrap.ROOT))
    )
    assert "You are a professional manga translator" in guidelines
    assert glossary == {}
    assert resolved_path is None
    assert effective_extract is False


def test_builtin_prompt_ui_widgets_preview_edit_and_toggle_sync(tmp_path):
    import os
    import subprocess
    import sys

    script = """
import os
import sys
from pathlib import Path
from types import SimpleNamespace

root = Path.cwd()
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "desktop_qt_ui"))

from PyQt6.QtWidgets import QApplication
from core.config_models import AppSettings
from manga_translator.translators.prompt_loader import (
    ensure_builtin_prompt_file,
    load_builtin_prompt_text,
    save_builtin_prompt_text,
)
from ui.main_page import layout as layout_parts
from ui.main_page.pages.prompt_page import create_prompt_page
from ui.secondary_pages.simple_prompt_editor_dialog import SimplePromptEditorDialog

app = QApplication.instance() or QApplication([])
assert app is not None

cfg = AppSettings()
cfg.translator.use_builtin_system_prompt = False
cfg.render.disable_auto_wrap = True
cfg.translator.extract_glossary = False
emitted_settings = []

class _FakeSignal:
    def emit(self, key, value):
        emitted_settings.append((key, value))

class _DummyHost:
    def __init__(self):
        self.config_service = SimpleNamespace(get_config=lambda: cfg)
        self.controller = SimpleNamespace(
            get_hq_prompt_options=lambda: ["prompt_example.yaml", "test.yaml"],
            open_dict_directory=lambda: None,
        )
        self.setting_changed = _FakeSignal()

    def _t(self, text, **kwargs):
        return text.format(**kwargs) if kwargs else text

    def _dialog_parent(self):
        return None

    def _create_new_prompt(self):
        pass

    def _copy_selected_prompt(self):
        pass

    def _rename_selected_prompt(self):
        pass

    def _delete_selected_prompt(self):
        pass

    def _refresh_prompt_manager(self):
        layout_parts.refresh_prompt_manager(self)

    def _apply_selected_prompt(self):
        layout_parts.apply_selected_prompt(self)

    def _on_prompt_selection_changed(self, current, previous):
        layout_parts.on_prompt_selection_changed(self, current, previous)

    def _open_prompt_editor(self, file_path):
        layout_parts.open_prompt_editor(self, file_path)

    def _preview_builtin_prompt(self, stem):
        return layout_parts.preview_builtin_prompt(self, stem)

    def _edit_builtin_prompt(self, stem):
        layout_parts.edit_builtin_prompt(self, stem)

    def _on_builtin_prompt_toggle_changed(self, config_key, checked):
        layout_parts.on_builtin_prompt_toggle_changed(self, config_key, checked)

host = _DummyHost()
page = create_prompt_page(host)
assert page is not None
assert set(host.builtin_prompt_rows.keys()) == {
    "system_prompt_hq",
    "system_prompt_line_break",
    "glossary_extraction_prompt",
    "system_prompt_hq_format",
}
assert set(host.builtin_prompt_toggles.keys()) == {
    "translator.use_builtin_system_prompt",
    "render.disable_auto_wrap",
    "translator.extract_glossary",
}

layout_parts.refresh_prompt_manager(host)
assert host.builtin_prompt_toggles["translator.use_builtin_system_prompt"].isChecked() is False
assert host.builtin_prompt_toggles["render.disable_auto_wrap"].isChecked() is True
assert host.builtin_prompt_toggles["translator.extract_glossary"].isChecked() is False
assert emitted_settings == []

for stem in host.builtin_prompt_rows:
    resolved_path = host._preview_builtin_prompt(stem)
    assert os.path.isfile(resolved_path)
    assert host.prompt_preview_panel._current_path == resolved_path
    assert host.prompt_preview_panel._edit_btn.isEnabled() is True

host.builtin_prompt_toggles["translator.use_builtin_system_prompt"].setChecked(True)
assert ("translator.use_builtin_system_prompt", True) in emitted_settings

temp_builtin = Path(sys.argv[1]) / "system_prompt_hq.yaml"
dlg = SimplePromptEditorDialog(
    str(temp_builtin),
    title_text="Built-in Base",
    description_text="Desc",
    section_text="System Prompt",
    hint_text="dict/system_prompt_hq.yaml",
    default_prompt_text="Keep {{{target_lang}}} intact.",
    ensure_prompt_func=lambda p: ensure_builtin_prompt_file(p, "system_prompt", "Keep {{{target_lang}}} intact."),
    load_prompt_func=lambda p: load_builtin_prompt_text(p, "system_prompt"),
    save_prompt_func=lambda p, txt: save_builtin_prompt_text(p, "system_prompt", txt),
    t_func=lambda k, **kw: k,
)
assert dlg.editor.toPlainText() == "Keep {{{target_lang}}} intact."
dlg.editor.setPlainText("Edited {{{target_lang}}} rule.")
dlg._save()
assert dlg.get_was_modified() is True
assert load_builtin_prompt_text(str(temp_builtin), "system_prompt") == "Edited {{{target_lang}}} rule."
page.deleteLater()
dlg.deleteLater()
"""

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    res = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=_bootstrap.ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"

