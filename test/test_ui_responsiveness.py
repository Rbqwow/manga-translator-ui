import _bootstrap  # noqa: F401

import threading
import time
from types import SimpleNamespace

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QStyleOptionViewItem

from ui.main_page.dynamic_settings import _process_next_setting_chunk
from ui.secondary_pages.replacements_editor import ReplacementsEditorPanel
from ui.secondary_pages.rich_text_rules_editor import RichTextRulesEditorPanel


@pytest.fixture(scope="session")
def responsive_app():
    return QApplication.instance() or QApplication([])


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate()


@pytest.fixture
def rules_panel(responsive_app, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("common:\n- pattern: original\n  replace: replacement\n", encoding="utf-8")

    class Panel(ReplacementsEditorPanel):
        def _get_file_path(self):
            return str(path)

    panel = Panel()
    wait_until(lambda: not panel._loading)
    yield panel, path
    panel._timer.stop()
    sip.delete(panel)


@pytest.fixture
def rich_rules_panel(responsive_app, tmp_path):
    path = tmp_path / "rich-rules.yaml"
    path.write_text("common:\n- pattern: original\n  style: {bold: true}\n", encoding="utf-8")

    class Panel(RichTextRulesEditorPanel):
        def _get_file_path(self):
            return str(path)

    panel = Panel()
    wait_until(lambda: not panel._loading)
    yield panel, path
    panel._timer.stop()
    sip.delete(panel)


def test_invalid_rich_rule_preserves_file_and_previous_table_and_can_be_repaired(rich_rules_panel):
    panel, path = rich_rules_panel
    previous_table = panel.tables["common"]
    previous_signature = panel._last_loaded_signature
    invalid = "common:\n- pattern: first\n  style: invalid\n- pattern: second\n  style: {italic: true}\n"
    path.write_text(invalid, encoding="utf-8")
    original_bytes = path.read_bytes()
    panel.refresh()
    wait_until(lambda: not panel._loading)

    assert "style must be a mapping" in panel.status.text()
    assert panel.tables["common"] is previous_table
    assert previous_table.item(0, panel.COL_PATTERN).text() == "original"
    assert panel._last_loaded_signature == previous_signature
    assert not previous_table.isEnabled()
    assert not panel.add_button.isEnabled()
    previous_table.item(0, panel.COL_PATTERN).setText("attempted edit")
    assert not panel._save_current_content()
    assert not panel._timer.isActive()
    assert path.read_bytes() == original_bytes

    panel._enter_raw_mode()
    assert panel.raw_editor.toPlainText() == invalid
    assert not panel.raw_editor.isReadOnly()
    assert not panel._save_current_content()
    assert path.read_bytes() == original_bytes
    repaired = invalid.replace("style: invalid", "style: {bold: true}")
    panel.raw_editor.setPlainText(repaired)
    panel._timer.stop()
    panel._enter_table_mode()
    wait_until(lambda: not panel._loading)
    assert not panel._raw_mode
    assert not panel._load_error
    assert panel.tables["common"].rowCount() == 2
    assert panel.tables["common"].cellWidget(0, panel.COL_STYLE).property("richStyle") == {"bold": True}
    assert path.read_text(encoding="utf-8") == repaired


def test_row_construction_failure_discards_staged_tables_and_retry_is_atomic(rules_panel, monkeypatch):
    panel, path = rules_panel
    previous_table = panel.tables["common"]
    original_populate = panel._populate_row

    def fail_second_row(table, row, rule):
        if row == 1:
            raise RuntimeError("widget creation failed")
        original_populate(table, row, rule)

    changed = "common:\n- pattern: first\n  replace: one\n- pattern: second\n  replace: two\n"
    path.write_text(changed, encoding="utf-8")
    original_bytes = path.read_bytes()
    monkeypatch.setattr(panel, "_populate_row", fail_second_row)
    panel.refresh()
    wait_until(lambda: not panel._loading)
    assert "widget creation failed" in panel.status.text()
    assert panel.tables["common"] is previous_table
    assert previous_table.rowCount() == 1
    assert previous_table.item(0, panel.COL_PATTERN).text() == "original"
    assert panel._pending_stack is None
    assert panel._pending_rows is None
    assert not panel._populate_timer.isActive()
    assert not panel._save_current_content()
    assert path.read_bytes() == original_bytes

    monkeypatch.setattr(panel, "_populate_row", original_populate)
    panel.refresh()
    wait_until(lambda: not panel._loading)
    assert not panel._load_error
    assert panel.tables["common"] is not previous_table
    assert panel.tables["common"].rowCount() == 2
    assert panel.group_stack.count() == 3
    assert panel.group_stack.currentWidget() is panel._current_table()
    assert panel._tables is panel.tables
    assert panel._group_stack is panel.group_stack
    assert path.read_bytes() == original_bytes


def test_invalid_raw_rule_cannot_autosave_or_replace_valid_table(rich_rules_panel, monkeypatch):
    panel, path = rich_rules_panel
    monkeypatch.setattr("ui.secondary_pages.base_rule_editor.themed_warning", lambda *args: None)
    original_bytes = path.read_bytes()
    previous_table = panel.tables["common"]
    panel._enter_raw_mode()
    panel.raw_editor.setPlainText("common:\n- pattern: edited\n  style: invalid\n")
    panel._timer.stop()
    assert not panel._save_current_content()
    panel._enter_table_mode()
    wait_until(lambda: not panel._loading)
    assert panel._raw_mode
    assert panel.tables["common"] is previous_table
    assert previous_table.item(0, panel.COL_PATTERN).text() == "original"
    assert path.read_bytes() == original_bytes


def test_initial_invalid_rich_rule_does_not_allow_saving_an_empty_table(rich_rules_panel):
    previous, path = rich_rules_panel
    path.write_text("common:\n- pattern: first\n  style: invalid\n", encoding="utf-8")
    original = path.read_bytes()
    panel = type(previous)()
    try:
        wait_until(lambda: not panel._loading)
        assert "style must be a mapping" in panel.status.text()
        assert panel.tables["common"].rowCount() == 0
        assert not panel._save_current_content()
        assert path.read_bytes() == original
        panel._enter_raw_mode()
        assert panel.raw_editor.toPlainText() == path.read_text(encoding="utf-8")
    finally:
        panel._timer.stop()
        sip.delete(panel)


def test_rules_refresh_preserves_rows_when_file_is_unchanged(rules_panel, monkeypatch):
    panel, path = rules_panel
    item = panel.tables["common"].item(0, panel.COL_PATTERN)
    panel.tables["common"].selectRow(0)
    panel.refresh()
    wait_until(lambda: not panel._loading)
    assert panel.tables["common"].item(0, panel.COL_PATTERN) is item
    assert panel._get_selected_rows() == [0]

    path.write_text("common:\n- pattern: changed\n  replace: new\n", encoding="utf-8")
    panel.refresh()
    wait_until(lambda: not panel._loading)
    assert panel.tables["common"].item(0, panel.COL_PATTERN).text() == "changed"
    assert not panel._modified


def test_rules_read_runs_in_background_and_recovers_from_invalid_yaml(rules_panel, monkeypatch):
    panel, path = rules_panel
    started, release = threading.Event(), threading.Event()
    threads = []
    parser = type(panel)._safe_load_yaml

    def slow_parse(text):
        threads.append(threading.get_ident())
        started.set()
        release.wait(2)
        return parser(text)

    monkeypatch.setattr(type(panel), "_safe_load_yaml", staticmethod(slow_parse))
    path.write_text("common: [\n", encoding="utf-8")
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    try:
        panel.refresh()
        assert not panel._toolbar.isEnabled()
        wait_until(started.is_set)
        QTest.qWait(70)
        assert len(ticks) >= 3
        assert threads[0] != threading.get_ident()
        release.set()
        wait_until(lambda: not panel._loading)
        assert "Load error" in panel.status.text()
        assert panel._toolbar.isEnabled()
        assert panel.tables["common"].item(0, panel.COL_PATTERN).text() == "original"

        path.write_text("common: []\n", encoding="utf-8")
        panel.refresh()
        wait_until(lambda: not panel._loading)
        assert panel.tables["common"].rowCount() == 0
    finally:
        release.set()
        timer.stop()


def test_large_rules_table_is_populated_between_event_loop_ticks(rules_panel):
    panel, path = rules_panel
    content = "common:\n" + "".join(f"- pattern: rule_{i}\n  replace: value\n" for i in range(2000))
    path.write_text(content, encoding="utf-8")
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(panel._pending_rows is not None))
    timer.start(1)
    try:
        panel.refresh()
        wait_until(lambda: not panel._loading)
        assert sum(ticks) >= 2
        assert panel.tables["common"].rowCount() == 2000
        assert panel.tables["common"].item(1999, panel.COL_PATTERN).text() == "rule_1999"
        assert path.read_text(encoding="utf-8") == content
        assert not panel._modified
    finally:
        timer.stop()


def test_rule_status_uses_icons_without_changing_saved_boolean(rules_panel):
    panel, _ = rules_panel
    table = panel.tables["common"]
    option = QStyleOptionViewItem()
    table.itemDelegate().initStyleOption(option, table.model().index(0, panel.COL_ENABLED))
    assert option.text == ""
    assert not option.icon.isNull()
    assert panel._extract_row_data(table, 0).get("enabled", True)
    table.selectRow(0)
    panel._on_toggle_enabled()
    panel._timer.stop()
    assert panel._extract_row_data(table, 0)["enabled"] is False


def test_raw_to_table_parses_and_saves_in_background_and_populates_in_chunks(rules_panel, monkeypatch):
    panel, path = rules_panel
    original = path.read_bytes()
    previous_table = panel.tables["common"]
    content = "common:\n" + "".join(f"- pattern: rule_{i}\n  replace: value\n" for i in range(2000))
    panel._enter_raw_mode()
    panel.raw_editor.setPlainText(content)
    started, release = threading.Event(), threading.Event()
    parser = type(panel)._safe_load_yaml
    parse_threads, save_threads, row_threads = [], [], []

    def parse(raw):
        parse_threads.append(threading.get_ident())
        started.set()
        release.wait(2)
        return parser(raw)

    monkeypatch.setattr(type(panel), "_safe_load_yaml", staticmethod(parse))
    monkeypatch.setattr(type(panel), "_invalidate_cache_for_path", staticmethod(
        lambda path: save_threads.append(threading.get_ident())
    ))
    populate = panel._populate_row

    def populate_row(*args):
        row_threads.append(threading.get_ident())
        populate(*args)

    monkeypatch.setattr(panel, "_populate_row", populate_row)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(panel._pending_rows is not None))
    timer.start(1)
    try:
        panel._enter_table_mode()
        assert panel._loading and panel._raw_mode
        assert panel.raw_editor.isReadOnly()
        assert not panel.mode_segment.isEnabled()
        assert not panel._timer.isActive()
        assert panel.tables["common"] is previous_table
        wait_until(started.is_set)
        QTest.qWait(70)
        assert len(ticks) >= 3
        assert path.read_bytes() == original
        release.set()
        wait_until(lambda: not panel._loading)
        assert len(parse_threads) == 1  # Saving must not reparse on the GUI thread.
        assert parse_threads[0] != threading.get_ident()
        assert save_threads and save_threads[0] != threading.get_ident()
        assert set(row_threads) == {threading.get_ident()}
        assert sum(ticks) >= 2
        assert not panel._raw_mode and not panel._modified
        assert panel.tables["common"].rowCount() == 2000
        assert panel.tables["common"].item(1999, panel.COL_PATTERN).text() == "rule_1999"
        assert path.read_text(encoding="utf-8") == content
    finally:
        release.set()
        timer.stop()


def test_raw_to_table_construction_error_preserves_source_file_and_old_table(rules_panel, monkeypatch):
    panel, path = rules_panel
    original = path.read_bytes()
    previous_table = panel.tables["common"]
    panel._enter_raw_mode()
    content = "common:\n- pattern: edited\n  replace: new\n"
    panel.raw_editor.setPlainText(content)

    def fail(*args):
        raise RuntimeError("row construction failed")

    monkeypatch.setattr(panel, "_populate_row", fail)
    panel._enter_table_mode()
    wait_until(lambda: not panel._loading)
    assert panel._raw_mode and panel._modified
    assert not panel.raw_editor.isReadOnly()
    assert "row construction failed" in panel.status.text()
    assert panel.raw_editor.toPlainText() == content
    assert panel.tables["common"] is previous_table
    assert path.read_bytes() == original


def test_raw_to_table_write_failure_can_retry_without_losing_edits(rules_panel, monkeypatch):
    panel, path = rules_panel
    original = path.read_bytes()
    previous_table = panel.tables["common"]
    panel._enter_raw_mode()
    content = "common:\n- pattern: edited\n  replace: new\n"
    panel.raw_editor.setPlainText(content)

    def fail_replace(*args):
        raise PermissionError("file is read-only")

    with monkeypatch.context() as patcher:
        patcher.setattr("ui.secondary_pages.base_rule_editor.os.replace", fail_replace)
        panel._enter_table_mode()
        wait_until(lambda: not panel._loading)
        assert panel._raw_mode and panel._modified
        assert "file is read-only" in panel.status.text()
        assert panel.tables["common"] is previous_table
        assert path.read_bytes() == original
        assert list(path.parent.glob(".rules-*.tmp")) == []
    panel._enter_table_mode()
    wait_until(lambda: not panel._loading)
    assert not panel._raw_mode and not panel._modified
    assert path.read_text(encoding="utf-8") == content


def test_raw_to_table_discards_results_if_source_changes_during_parse(rules_panel, monkeypatch):
    panel, path = rules_panel
    original = path.read_bytes()
    previous_table = panel.tables["common"]
    panel._enter_raw_mode()
    panel.raw_editor.setPlainText("common: []\n")
    started, release = threading.Event(), threading.Event()
    parser = type(panel)._safe_load_yaml

    def parse(text):
        started.set()
        release.wait(2)
        return parser(text)

    monkeypatch.setattr(type(panel), "_safe_load_yaml", staticmethod(parse))
    try:
        panel._enter_table_mode()
        wait_until(started.is_set)
        # A programmatic edit must also invalidate the pending snapshot.
        changed = "common:\n- pattern: newer\n  replace: value\n"
        panel.raw_editor.setPlainText(changed)
        release.set()
        wait_until(lambda: not panel._loading)
        panel._timer.stop()
        assert panel._raw_mode and panel._modified
        assert panel.raw_editor.toPlainText() == changed
        assert panel.tables["common"] is previous_table
        assert path.read_bytes() == original
    finally:
        release.set()


def test_unchanged_raw_to_table_does_not_rewrite_file(rules_panel, monkeypatch):
    panel, path = rules_panel
    original = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    panel._enter_raw_mode()

    def unexpected_write(*args):
        raise AssertionError("Unchanged source should not be saved")

    monkeypatch.setattr(type(panel), "_write_rule_snapshot", unexpected_write)
    panel._enter_table_mode()
    wait_until(lambda: not panel._loading)
    assert not panel._raw_mode and not panel._load_error
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == mtime


def test_refresh_never_cancels_a_pending_switch_save(rules_panel):
    """A read used to share one future with the source->table save, so a refresh
    arriving while the save was queued cancelled the write and left the panel
    flagged as still saving (which then blocked later forced reloads)."""
    panel, _path = rules_panel

    class StubFuture:
        cancel_calls = 0

        def cancel(self):
            StubFuture.cancel_calls += 1
            return True

    save_future = StubFuture()
    panel._save_future = save_future
    panel._saving_table_switch = True

    panel._load_data()

    assert StubFuture.cancel_calls == 0, "a read must not cancel the pending save"
    assert panel._save_future is save_future
    assert panel._saving_table_switch is False, "the superseded switch flag must clear"
    wait_until(lambda: not panel._loading)


def test_save_jobs_are_tracked_apart_from_loads(rules_panel):
    panel, _path = rules_panel
    load_future = panel._load_future

    panel._submit_rule_job(
        lambda: ((None, None, None), ""),
        "_switch_save_ready",
        panel._load_generation - 1,  # stale generation: the reply is ignored
        saving=True,
    )

    assert panel._save_future is not None
    assert panel._load_future is load_future


def test_unsaved_edits_are_not_overwritten_by_background_refresh(rules_panel, monkeypatch):
    panel, path = rules_panel
    started, release = threading.Event(), threading.Event()
    parser = type(panel)._safe_load_yaml
    def slow_parse(text):
        started.set()
        release.wait(2)
        return parser(text)
    monkeypatch.setattr(type(panel), "_safe_load_yaml", staticmethod(slow_parse))
    path.write_text("common:\n- pattern: external\n  replace: new\n", encoding="utf-8")
    try:
        panel.refresh()
        wait_until(started.is_set)
        panel.tables["common"].item(0, panel.COL_PATTERN).setText("local edit")
        panel._timer.stop()
        release.set()
        wait_until(lambda: not panel._loading)
        assert panel.tables["common"].item(0, panel.COL_PATTERN).text() == "local edit"
        assert panel._modified
        assert "external" in path.read_text(encoding="utf-8")
    finally:
        release.set()


def test_settings_rebuild_yields_and_discards_obsolete_build(responsive_app):
    visited = []
    finished = []

    def rows():
        for number in range(20):
            time.sleep(0.003)  # Model a slow individual QWidget construction.
            visited.append(number)
            yield 1

    view = SimpleNamespace(_settings_build_seq=1, _settings_row_iterator=rows())
    view._process_next_setting_chunk = lambda seq: _process_next_setting_chunk(view, seq)
    view._finalize_settings_ui = lambda seq: finished.append(seq)
    view._process_next_setting_chunk(1)
    assert 0 < len(visited) < 20
    view._settings_build_seq = 2
    count = len(visited)
    QTest.qWait(40)
    assert len(visited) == count
    assert finished == []
    view._process_next_setting_chunk(2)
    wait_until(lambda: finished == [2])
    assert visited == list(range(20))
