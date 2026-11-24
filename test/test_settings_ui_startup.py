"""Settings-page UI responsiveness regressions for the startup path."""

import _bootstrap  # noqa: F401

import json
from pathlib import Path
from types import SimpleNamespace

# --- P3-13: font catalog progress must not run backwards ----------------

def test_font_catalog_progress_is_monotonic(monkeypatch):
    """A per-pass denominator made the percentage jump backwards."""
    from desktop_qt_ui.utils import font_list

    calls = []
    # 4 families collapse into 3 merged candidates (len(candidates) < len(families))
    families = ['A', 'B', 'C', 'D']
    monkeypatch.setattr(font_list, '_font_styles', lambda family: ['Regular'])
    monkeypatch.setattr(font_list, 'localized_font_family',
                        lambda family, locale, style: (family, ()))
    monkeypatch.setattr(font_list, 'font_value', lambda family, style: f'{family}|{style}')
    monkeypatch.setattr(font_list, '_font_name_records', lambda family, style: [])
    monkeypatch.setattr(
        font_list,
        '_font_candidate_key',
        lambda family, style: ('shared', style) if family in {'A', 'B'} else (family, style),
    )
    monkeypatch.setattr(font_list, '_canonical_font_rank', lambda entry: (0,))
    monkeypatch.setattr(font_list, '_resolved_font_identity', lambda family, style: ())

    font_list._build_font_style_entries(
        'en_US', families, progress=lambda done, total: calls.append((done, total)),
    )
    assert len(calls) == len(families) + 3
    totals = {total for _done, total in calls}
    assert len(totals) == 1, f'denominator changed mid-run: {sorted(totals)}'
    done_values = [done for done, _total in calls]
    assert done_values == sorted(done_values), f'progress went backwards: {done_values}'
    assert max(done_values) <= totals.pop()


def _has_emoji_or_vs(text: str) -> list[str]:
    return [
        f'{ch}(U+{ord(ch):04X})'
        for ch in str(text)
        if (
            0x23E9 <= ord(ch) <= 0x23FF
            or 0x2600 <= ord(ch) <= 0x27BF
            or 0x2B00 <= ord(ch) <= 0x2BFF
            or 0xFE0E <= ord(ch) <= 0xFE0F
            or ord(ch) == 0x200D
            or ord(ch) >= 0x1F000
        )
    ]


# 历史遗留日志文案（当前桌面 UI 未直接渲染）；新增或修改任意 UI 文案不得加入此集合
_LEGACY_CONSOLE_LOG_EMOJI_KEYS = frozenset({
    '⏭️ Skipped {count} existing files.',
    '📊 Batch processing mode: {total} images in {batches} batches',
    '🔧 Translation workflow: {mode}',
    '📁 Output directory: {dir}',
    '🚀 Starting translation (loading images in batches to save memory)...',
    '\n📦 Processing batch {current}/{total} (images {start}-{end})...',
    '\n⚠️ Batch translation completed: {success}/{total} succeeded, {failed}/{total} failed',
    '✅ Batch translation completed: {success}/{total} succeeded',
    '💾 Files saved to: {dir}',
    '📥 Importing translations from TXT files to JSON...',
    '⚠️ Warning: Cannot find template file, skipping auto-import',
})


def test_locale_descriptions_contain_no_emoji_fallback_triggers():
    locale_dir = _bootstrap.ROOT / 'desktop_qt_ui' / 'locales'
    locales = ('zh_CN', 'zh_TW', 'en_US', 'ja_JP', 'ko_KR', 'es_ES')

    assert not any(
        key.startswith(('label_', 'desc_'))
        for key in _LEGACY_CONSOLE_LOG_EMOJI_KEYS
    )

    for locale in locales:
        catalog = json.loads((locale_dir / f'{locale}.json').read_text(encoding='utf-8'))
        for key in ('desc_render_check_br_and_retry', 'desc_upscale_upscaler'):
            assert catalog.get(key, '').strip(), (locale, key)
        for key, value in catalog.items():
            if key in _LEGACY_CONSOLE_LOG_EMOJI_KEYS:
                continue
            bad = _has_emoji_or_vs(value)
            assert not bad, f'{locale}:{key} contains emoji chars {bad}'


def test_editor_context_menu_uses_icons_without_emojis(monkeypatch):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QApplication, QWidget
    from ui.editor import graphics_view_input as gvi

    app = QApplication.instance() or QApplication([])
    assert app is not None

    captured_menus = []

    def fake_exec(menu, _pos):
        actions = [a for a in menu.menuActions() if not a.isSeparator()]
        captured_menus.append(actions)
        return None

    monkeypatch.setattr(gvi.RoundMenu, 'exec', fake_exec)

    class _DummyGraphicsView(gvi.GraphicsViewInputMixin, QWidget):
        def __init__(self):
            super().__init__()
            self._active_tool = None
            self.selection = []
            self.model = SimpleNamespace(get_selection=lambda: list(self.selection))
            self.editor_view = SimpleNamespace(
                _t=lambda key, **kwargs: f"{key} ({kwargs['count']})" if kwargs else key
            )

        def _cancel_active_interaction(self, commit=False):
            return None

    view = _DummyGraphicsView()
    event = SimpleNamespace(globalPos=lambda: QPoint(10, 10), accept=lambda: None)

    for selection, expected_count in (([0], 5), ([0, 1], 4), ([], 3)):
        view.selection = selection
        view.contextMenuEvent(event)
        actions = captured_menus[-1]
        assert len(actions) == expected_count, (selection, [a.text() for a in actions])
        for action in actions:
            assert not action.icon().isNull(), f'missing icon for {action.text()}'
            assert action.text().strip()
            assert not _has_emoji_or_vs(action.text()), action.text()


def test_builtin_prompts_contain_no_emoji_fallback_triggers():
    from manga_translator.colorization.prompt_loader import DEFAULT_AI_COLORIZER_PROMPT
    from manga_translator.ocr.prompt_loader import DEFAULT_AI_OCR_PROMPT
    from manga_translator.rendering.prompt_loader import DEFAULT_AI_RENDERER_PROMPT
    from manga_translator.translators.common import CommonTranslator
    from manga_translator.translators.prompt_loader import (
        BUILTIN_TRANSLATION_PROMPTS,
        load_builtin_prompt_text,
        resolve_builtin_prompt_path,
    )

    dict_dir = str(_bootstrap.ROOT / 'dict')
    for spec in BUILTIN_TRANSLATION_PROMPTS:
        stem = spec['stem']
        field_key = spec['field_key']
        resolved_path = Path(resolve_builtin_prompt_path(dict_dir, stem))
        assert resolved_path.is_file(), f'missing built-in prompt: {resolved_path}'

        raw_text = resolved_path.read_text(encoding='utf-8')
        assert raw_text.strip(), f'empty built-in prompt file: {resolved_path}'
        bad_raw = _has_emoji_or_vs(raw_text)
        assert not bad_raw, f'{resolved_path.name} raw text contains emoji chars {bad_raw}'

        prompt_text = load_builtin_prompt_text(str(resolved_path), field_key)
        assert prompt_text.strip(), f'empty field {field_key} in {resolved_path}'
        bad_prompt = _has_emoji_or_vs(prompt_text)
        assert not bad_prompt, f'{resolved_path.name}:{field_key} contains emoji chars {bad_prompt}'

    example_path = _bootstrap.ROOT / 'dict' / 'prompt_example.yaml'
    example_text = example_path.read_text(encoding='utf-8')
    assert example_text.strip()
    bad_example = _has_emoji_or_vs(example_text)
    assert not bad_example, f'prompt_example.yaml contains emoji chars {bad_example}'

    default_prompts = (
        ('CommonTranslator._HQ_FALLBACK_PROMPT', CommonTranslator._HQ_FALLBACK_PROMPT),
        ('DEFAULT_AI_OCR_PROMPT', DEFAULT_AI_OCR_PROMPT),
        ('DEFAULT_AI_COLORIZER_PROMPT', DEFAULT_AI_COLORIZER_PROMPT),
        ('DEFAULT_AI_RENDERER_PROMPT', DEFAULT_AI_RENDERER_PROMPT),
    )
    for name, prompt_text in default_prompts:
        assert prompt_text.strip(), name
        bad_default = _has_emoji_or_vs(prompt_text)
        assert not bad_default, f'{name} contains emoji chars {bad_default}'

