import _bootstrap  # noqa: F401

import unittest
import threading
import time
from unittest.mock import patch

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit

from utils import font_list


class TestFontComboStartup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        font_list._clear_font_catalog_caches()
        self.combos = []
        self.files = patch.object(font_list, "list_font_files", return_value=[])
        self.files.start()
        self.addCleanup(self.files.stop)

    def tearDown(self):
        for combo in self.combos:
            combo._closeComboMenu()
            sip.delete(combo)
        font_list._clear_font_catalog_caches()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def combo(self, **kwargs):
        combo = font_list.FontComboBox(**kwargs)
        self.combos.append(combo)
        return combo

    def test_initialization_and_config_refresh_do_not_build_catalog(self):
        with (
            patch.object(font_list, "_list_font_style_entries_cached", side_effect=AssertionError("eager catalog")),
            patch.object(font_list, "_resolve_catalog_value", side_effect=AssertionError("eager resolution")),
        ):
            locale = ["en_US"]
            combo = self.combo(locale_getter=lambda: locale[0])
            changes = []
            combo.currentFontChanged.connect(changes.append)
            saved = "Unavailable Saved Font::Bold Italic"
            combo.setCurrentFamily(saved)
            self.assertEqual(combo.currentFamily(), saved)
            self.assertEqual(len(changes), 1)
            changes.clear()

            locale[0] = "zh_CN"
            combo.refresh_ui_texts()
            combo.set_include_system_fonts(not combo._include_system_fonts)
            combo.refresh(force=True)

            self.assertEqual(combo.currentFamily(), saved)
            self.assertEqual(changes, [])

    def test_popup_loads_catalog_and_selecting_a_font_emits_change(self):
        saved = "Saved Family::Bold"
        other = "Other Family::Regular"
        entries = tuple(
            font_list._FontCatalogEntry(value, value, (value,), (value,), ())
            for value in (saved, other)
        )
        with (
            patch.object(font_list, "_load_font_catalog", return_value=entries) as catalog,
        ):
            combo = self.combo()
            combo.setCurrentFamily(saved)
            changes = []
            combo.currentFontChanged.connect(changes.append)
            catalog.assert_not_called()

            with patch.object(font_list._FontComboBoxMenu, "show_for_anchor"):
                combo._showComboMenu()
            self.wait_until(lambda: combo._catalog_loaded)
            self.assertTrue(catalog.called)
            self.assertEqual(combo.count(), 2)
            self.assertEqual(combo.currentFamily(), saved)
            self.assertEqual(changes, [])

            menu = combo.dropMenu
            hovered = []
            menu.fontHovered.connect(hovered.append)
            source = menu.view._font_model.index(combo.findData(other), 0)
            proxy = menu.view._filter_model.mapFromSource(source)
            menu._on_index_entered(proxy)
            self.assertEqual(hovered[-1], other)
            menu._on_index_clicked(proxy)
            self.assertEqual(combo.currentFamily(), other)
            self.assertEqual(len(changes), 1)

    def test_missing_saved_font_survives_full_catalog_loading(self):
        saved = "Uninstalled Family::Heavy"
        with (
            patch.object(font_list, "_load_font_catalog", return_value=()),
        ):
            combo = self.combo()
            combo.setCurrentFamily(saved)
            combo.refresh(load_catalog=True)
            self.wait_until(lambda: combo._catalog_loaded)
            self.assertEqual(combo.currentFamily(), saved)

    def test_slow_catalog_keeps_events_running_and_closed_menu_stays_closed(self):
        started = threading.Event()
        release = threading.Event()
        worker_threads = []
        def slow_catalog(*args):
            worker_threads.append(threading.get_ident())
            started.set()
            release.wait(2)
            return ()
        with (
            patch.object(font_list, "_load_font_catalog", side_effect=slow_catalog),
        ):
            combo = self.combo()
            changes = []
            combo.currentFontChanged.connect(changes.append)
            ticks = []
            timer = QTimer()
            timer.timeout.connect(lambda: ticks.append(1))
            timer.start(10)
            try:
                with patch.object(font_list._FontComboBoxMenu, "show_for_anchor"):
                    combo._showComboMenu()
                menu = combo.dropMenu
                self.assertFalse(menu.search_edit.isEnabled())
                self.assertTrue(menu._container.autoFillBackground())
                self.assertNotEqual(menu.search_edit.placeholderText(), menu.status_label.text())
                self.wait_until(started.is_set)
                QTest.qWait(80)
                self.assertGreaterEqual(len(ticks), 3)
                self.assertNotEqual(worker_threads[0], threading.get_ident())
                combo._closeComboMenu()
                release.set()
                self.wait_until(lambda: combo._catalog_loaded)
                self.assertIsNone(combo.dropMenu)
                self.assertEqual(changes, [])
            finally:
                release.set()
                timer.stop()

    def test_delayed_catalog_focuses_search_and_accepts_keyboard_input(self):
        started, release = threading.Event(), threading.Event()
        entries = tuple(
            font_list._FontCatalogEntry(value, value, (value,), (value,), ())
            for value in ("Saved Font", "Other Font")
        )

        def slow_catalog(*args):
            started.set()
            release.wait(3)
            return entries

        with patch.object(font_list, "_load_font_catalog", side_effect=slow_catalog):
            combo = self.combo()
            combo.setCurrentFamily("Saved Font")
            combo.show()
            self.app.processEvents()
            try:
                combo._showComboMenu()
                menu = combo.dropMenu
                self.wait_until(started.is_set)
                QTest.qWait(50)  # Let the initial focus request run while disabled.
                self.assertFalse(menu.search_edit.isEnabled())
                self.assertFalse(menu.search_edit.hasFocus())
                release.set()
                self.wait_until(lambda: combo._catalog_loaded)
                self.assertIs(self.app.focusWidget(), menu.search_edit)
                QTest.keyClicks(self.app.focusWidget(), "Other")
                self.assertEqual(menu.search_edit.text(), "Other")
                self.assertEqual(menu.view.model().rowCount(), 1)
                self.assertEqual(menu.view.model().index(0, 0).data(), "Other Font")
                self.assertEqual(combo.currentFamily(), "Saved Font")
            finally:
                release.set()

    def test_delayed_catalog_does_not_take_focus_after_popup_closed(self):
        started, release = threading.Event(), threading.Event()

        def slow_catalog(*args):
            started.set()
            release.wait(3)
            return ()

        with patch.object(font_list, "_load_font_catalog", side_effect=slow_catalog):
            combo = self.combo()
            combo.show()
            self.app.processEvents()
            other = QLineEdit()
            self.addCleanup(sip.delete, other)
            try:
                combo._showComboMenu()
                self.wait_until(started.is_set)
                combo._closeComboMenu()
                other.show()
                other.activateWindow()
                other.setFocus()
                self.wait_until(lambda: self.app.focusWidget() is other)
                release.set()
                self.wait_until(lambda: combo._catalog_loaded)
                self.assertIsNone(combo.dropMenu)
                self.assertIs(self.app.focusWidget(), other)
                QTest.keyClicks(self.app.focusWidget(), "keep typing")
                self.assertEqual(other.text(), "keep typing")
            finally:
                release.set()

    def test_loading_popup_never_covers_the_current_font(self):
        screen = self.app.primaryScreen().availableGeometry()
        for y, upward in ((screen.top() + 60, False), (screen.bottom() - 60, True)):
            combo = self.combo()
            combo.resize(260, 32)
            combo.move(screen.left() + 60, y)
            combo.show()
            self.app.processEvents()
            menu = combo._createComboMenu()
            menu.bind_to_anchor(combo)
            menu.set_loading("Loading fonts… 20/100")
            anchor = QRect(combo.mapToGlobal(QPoint()), combo.size())
            status = QRect(menu.status_label.mapToGlobal(QPoint()), menu.status_label.size())
            search = QRect(menu.search_edit.mapToGlobal(QPoint()), menu.search_edit.size())
            self.assertEqual(menu._opens_upward, upward)
            self.assertFalse(menu.frameGeometry().intersects(anchor))
            self.assertFalse(status.intersects(anchor))
            self.assertFalse(status.intersects(search))
            menu.set_entries([("Font", "Font", "font")], 0)
            self.assertFalse(menu.frameGeometry().intersects(anchor))
            self.assertTrue(menu._status_panel.isHidden())
            sip.delete(menu)

    def test_native_deactivation_before_press_consumes_only_the_closing_click(self):
        with (
            patch.object(font_list, "_load_font_catalog", return_value=()),
            patch.object(font_list._FontComboBoxMenu, "show_for_anchor"),
            patch.object(font_list, "_left_mouse_button_down", return_value=True) as held,
        ):
            combo = self.combo()
            combo.resize(260, 32)
            combo._showComboMenu()
            menu = combo.dropMenu
            menu._was_activated = True
            with patch.object(font_list.QCursor, "pos", return_value=combo.mapToGlobal(combo.rect().center())):
                self.app.sendEvent(menu, QEvent(QEvent.Type.WindowDeactivate))
            self.assertIsNone(combo.dropMenu)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(sip.isdeleted(menu))
            QTest.mousePress(combo, Qt.MouseButton.LeftButton)
            QTest.qWait(60)  # A held click must remain suppressed across timer ticks.
            held.return_value = False
            QTest.mouseRelease(combo, Qt.MouseButton.LeftButton)
            self.assertIsNone(combo.dropMenu)
            self.assertFalse(combo._popup_close_click_timer.isActive())
            QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
            self.assertIsNotNone(combo.dropMenu)

    def test_escape_does_not_consume_the_next_click_even_if_cursor_is_over_combo(self):
        with (
            patch.object(font_list, "_load_font_catalog", return_value=()),
            patch.object(font_list._FontComboBoxMenu, "show_for_anchor"),
            patch.object(font_list, "_left_mouse_button_down", return_value=False),
        ):
            combo = self.combo()
            combo._showComboMenu()
            with patch.object(font_list.QCursor, "pos", return_value=combo.mapToGlobal(combo.rect().center())):
                QTest.keyClick(combo.dropMenu, Qt.Key.Key_Escape)
            self.assertIsNone(combo.dropMenu)
            QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
            self.assertIsNotNone(combo.dropMenu)

    def test_abandoned_close_click_expires_when_mouse_released_elsewhere(self):
        with (
            patch.object(font_list, "_load_font_catalog", return_value=()),
            patch.object(font_list._FontComboBoxMenu, "show_for_anchor"),
            patch.object(font_list, "_left_mouse_button_down", return_value=True) as held,
        ):
            combo = self.combo()
            combo._showComboMenu()
            with patch.object(font_list.QCursor, "pos", return_value=combo.mapToGlobal(combo.rect().center())):
                combo.dropMenu.close()
            self.assertTrue(combo._close_popup_on_release)
            # The release is delivered to another window, not the combo box.
            held.return_value = False
            self.wait_until(lambda: not combo._popup_close_click_timer.isActive())
            QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
            self.assertIsNotNone(combo.dropMenu)

    def test_loading_and_loaded_list_recompute_direction_and_stay_on_screen(self):
        screen = self.app.primaryScreen().availableGeometry()
        for distance in (160, 180):
            combo = self.combo()
            combo.resize(260, 32)
            combo.move(screen.left() + 60, screen.bottom() - distance)
            combo.show()
            self.app.processEvents()
            menu = combo._createComboMenu()
            menu.bind_to_anchor(combo)
            self.assertFalse(menu._opens_upward)
            menu.set_loading("Loading fonts…")
            self.assertTrue(screen.contains(menu.frameGeometry()))
            if distance == 160:
                self.assertTrue(menu._opens_upward)
            menu.set_entries([(f"Font {i}", f"Font {i}", str(i)) for i in range(100)], 0)
            self.assertTrue(menu._opens_upward)
            self.assertGreater(menu.height(), 300)
            self.assertTrue(screen.contains(menu.frameGeometry()))
            anchor = QRect(combo.mapToGlobal(QPoint()), combo.size())
            self.assertFalse(menu.frameGeometry().intersects(anchor))
            sip.delete(menu)

    def test_failed_load_can_retry_and_empty_selection_stays_empty(self):
        with (
            patch.object(font_list, "_load_font_catalog", side_effect=[RuntimeError("unreadable font"), ()]),
        ):
            combo = self.combo(locale_getter=lambda: "en_US")
            combo.setCurrentFamily("")
            with patch.object(font_list._FontComboBoxMenu, "show_for_anchor"):
                combo._showComboMenu()
            self.wait_until(lambda: "failed" in combo.dropMenu.status_label.text().lower())
            combo._closeComboMenu()
            combo.refresh(load_catalog=True)
            self.wait_until(lambda: combo._catalog_loaded)
            self.assertEqual(combo.currentFamily(), "")

    def test_catalog_is_shared_and_old_locale_results_are_ignored(self):
        first_started, release = threading.Event(), threading.Event()
        calls = []
        def build(locale, *args):
            calls.append(locale)
            if len(calls) == 1:
                first_started.set()
                release.wait(2)
            value = f"{locale} Family::Regular"
            return (font_list._FontCatalogEntry(value, value, (value,), (value,), ()),)
        with (
            patch.object(font_list, "_load_font_catalog", side_effect=build),
        ):
            locale = ["en_US"]
            first = self.combo(locale_getter=lambda: locale[0])
            second = self.combo(locale_getter=lambda: "en_US")
            try:
                first.refresh(load_catalog=True)
                second.refresh(load_catalog=True)
                self.wait_until(first_started.is_set)
                locale[0] = "zh_CN"
                first.refresh_ui_texts()
                first.refresh(load_catalog=True)
                release.set()
                self.wait_until(lambda: first._catalog_loaded and second._catalog_loaded)
                self.assertEqual(calls, ["en_US", "zh_CN"])
                self.assertGreaterEqual(first.findData("zh_CN Family::Regular"), 0)
                self.assertEqual(first.findData("en_US Family::Regular"), -1)
                second.refresh(load_catalog=True)
                self.wait_until(lambda: second._catalog_loaded)
                self.assertEqual(len(calls), 2)
            finally:
                release.set()


if __name__ == "__main__":
    unittest.main()
