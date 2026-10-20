import _bootstrap  # noqa: F401

import subprocess
import sys
from unittest.mock import patch

import pytest


ROOT = _bootstrap.ROOT


def test_main_import_is_quiet_and_defers_inference_dependencies():
    result = subprocess.run(
        [sys.executable, "-c", """
import desktop_qt_ui.main
import sys
deferred = ('torch', 'ultralytics', 'torchvision', 'google.genai', 'openai', 'networkx', 'pythainlp')
loaded = [name for name in deferred if name in sys.modules]
assert not loaded, f'Startup imported unused inference dependencies: {loaded}'
"""],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "QFluentWidgets Pro" not in result.stdout


@pytest.fixture(scope="session")
def startup_app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_ui_uses_native_font_family(startup_app):
    from qfluentwidgets import fontFamilies, setFontFamilies
    from ui.theme import configure_ui_font

    previous = fontFamilies()
    try:
        configure_ui_font(startup_app)
        assert fontFamilies() == [startup_app.font().family()]
    finally:
        setFontFamilies(previous, save=False)


def test_repeated_theme_application_skips_restyle_and_switches_still_apply(startup_app):
    from PyQt6.QtGui import QColor
    from qfluentwidgets import qconfig, setTheme, setThemeColor
    from ui import theme

    previous_mode = qconfig.get(qconfig.themeMode)
    previous_color = QColor(qconfig.get(qconfig.themeColor))
    previous_theme = theme.get_current_theme()
    try:
        theme.apply_application_theme("dark", startup_app)
        with (
            patch("qfluentwidgets.setTheme", wraps=setTheme) as change_mode,
            patch("qfluentwidgets.setThemeColor", wraps=setThemeColor) as change_color,
        ):
            theme.apply_application_theme("dark", startup_app)
            change_mode.assert_not_called()
            change_color.assert_not_called()

            theme.apply_application_theme("rose", startup_app)
            change_mode.assert_not_called()
            change_color.assert_called_once()
            assert theme.get_current_theme() == "rose"
            assert qconfig.get(qconfig.themeColor) == QColor(theme.resolve_theme_color("rose"))

            theme.apply_application_theme("light", startup_app)
            change_mode.assert_called_once()
            assert qconfig.get(qconfig.themeMode) == theme.resolve_qfluent_theme("light")
            assert qconfig.get(qconfig.themeColor) == QColor(theme.resolve_theme_color("light"))
    finally:
        setTheme(previous_mode)
        setThemeColor(previous_color)
        theme.set_current_theme(previous_theme)
