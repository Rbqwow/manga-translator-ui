import _bootstrap  # noqa: F401

import os
import unittest
from unittest.mock import patch

from PyQt6.QtGui import QPainterPath

from manga_translator.rendering.text_render import _fonts, _glyphs


class FontRuntimeSimulationTest(unittest.TestCase):
    def test_offscreen_font_directory_bootstrap_sets_default_once(self):
        with (
            patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}, clear=True),
            patch.object(_fonts, "BASE_PATH", "C:/project"),
            patch.object(_fonts.os.path, "isdir", return_value=True),
        ):
            _fonts._bootstrap_qt_fontdir_for_offscreen()
            self.assertEqual(
                os.environ["QT_QPA_FONTDIR"],
                os.path.join("C:/project", "fonts"),
            )

    def test_font_directory_bootstrap_preserves_existing_or_non_offscreen_environment(self):
        with patch.dict(
            os.environ,
            {"QT_QPA_PLATFORM": "offscreen", "QT_QPA_FONTDIR": "existing"},
            clear=True,
        ):
            _fonts._bootstrap_qt_fontdir_for_offscreen()
            self.assertEqual(os.environ["QT_QPA_FONTDIR"], "existing")

        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "minimal"}, clear=True):
            _fonts._bootstrap_qt_fontdir_for_offscreen()
            self.assertNotIn("QT_QPA_FONTDIR", os.environ)


class GlyphSimulationTest(unittest.TestCase):
    def test_rasterize_path_handles_zero_height_path(self):
        path = QPainterPath()
        path.moveTo(3, 3)
        path.lineTo(4, 3)
        alpha, left, top = _glyphs._rasterize_path(path)
        self.assertEqual(alpha.shape, (0, 0))
        self.assertEqual((left, top), (3, 3))

    def test_rasterize_path_handles_empty_and_zero_area_paths(self):
        empty = QPainterPath()
        alpha, left, top = _glyphs._rasterize_path(empty)
        self.assertEqual(alpha.shape, (0, 0))
        self.assertEqual((left, top), (0, 0))

        point = QPainterPath()
        point.moveTo(3, 3)
        alpha, left, top = _glyphs._rasterize_path(point)
        self.assertEqual(alpha.shape, (0, 0))
        self.assertEqual((left, top), (0, 0))


def main() -> int:
    result = unittest.main(exit=False)
    return 0 if result.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
