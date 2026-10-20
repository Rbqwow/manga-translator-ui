"""Settings-page UI responsiveness regressions for the startup path."""

import _bootstrap  # noqa: F401

import asyncio
from types import SimpleNamespace

import pytest

# --- P3-13: font catalog progress must not run backwards ----------------

def test_font_catalog_progress_is_monotonic(monkeypatch):
    """A per-pass denominator made the percentage jump backwards."""
    from desktop_qt_ui.utils import font_list

    calls = []
    families = ['A', 'B', 'C']            # -> 3 candidates after enumeration
    monkeypatch.setattr(font_list, '_font_styles', lambda family: ['Regular'])
    monkeypatch.setattr(font_list, 'localized_font_family',
                        lambda family, locale, style: (family, ()))
    monkeypatch.setattr(font_list, 'font_value', lambda family, style: f'{family}|{style}')
    monkeypatch.setattr(font_list, '_font_name_records', lambda family, style: [])
    monkeypatch.setattr(font_list, '_font_candidate_key', lambda family, style: (family, style))
    monkeypatch.setattr(font_list, '_canonical_font_rank', lambda entry: (0,))
    monkeypatch.setattr(font_list, '_resolved_font_identity', lambda family, style: ())

    font_list._build_font_style_entries(
        'en_US', families, progress=lambda done, total: calls.append((done, total)),
    )
    assert calls, 'no progress was reported'
    totals = {total for _done, total in calls}
    assert len(totals) == 1, f'denominator changed mid-run: {sorted(totals)}'
    done_values = [done for done, _total in calls]
    assert done_values == sorted(done_values), f'progress went backwards: {done_values}'
    assert max(done_values) <= totals.pop()


# --- P3-11: the settings spinner stops even when the build raises --------

def test_settings_loading_bar_stops_when_the_build_raises(monkeypatch):
    from ui.main_page import dynamic_settings

    class Bar:
        stopped = False

        def show(self):
            pass

        def start(self):
            pass

        def stop(self):
            self.stopped = True

        def hide(self):
            pass

    view = SimpleNamespace(
        _settings_loading_bar=Bar(),
        tab_frames={},
        _settings_build_seq=0,
    )
    monkeypatch.setattr(dynamic_settings, '_settings_structure_signature', lambda *a, **k: 'sig')
    monkeypatch.setattr(dynamic_settings, '_refresh_env_api_groups', lambda *a, **k: None)
    monkeypatch.setattr(
        dynamic_settings, '_drop_cached_settings_widget_refs',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('layout exploded')),
    )

    with pytest.raises(RuntimeError, match='layout exploded'):
        dynamic_settings.set_parameters(view, {'cli': {'format': 'webp'}})
    assert view._settings_loading_bar.stopped, 'spinner kept running after a failed build'

