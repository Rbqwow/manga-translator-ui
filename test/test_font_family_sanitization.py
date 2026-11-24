import _bootstrap  # noqa: F401

import unittest

from manga_translator.rendering.text_render import (
    qt_family_is_ambiguous,
    strip_qt_foundry_brackets,
)


class TestQtFamilyAmbiguity(unittest.TestCase):
    def test_leading_bracket_is_ambiguous(self):
        # Qt 的 parseFontName 会把 "[X]Y" 拆成空家族名 + 厂商 X
        self.assertTrue(qt_family_is_ambiguous('[工具箱]书卷楷-简繁'))
        self.assertTrue(qt_family_is_ambiguous('[toolbox]FangYuan-GBK W7'))
        self.assertTrue(qt_family_is_ambiguous('  [toolbox]QiangDiao-W'))

    def test_normal_names_are_not_ambiguous(self):
        self.assertFalse(qt_family_is_ambiguous('工具箱书卷楷-简繁'))
        self.assertFalse(qt_family_is_ambiguous('Microsoft YaHei UI'))
        # 尾部厂商写法家族名非空，Qt 能解析出 "Helvetica"
        self.assertFalse(qt_family_is_ambiguous('Helvetica [Cronyx]'))
        self.assertFalse(qt_family_is_ambiguous(''))
        self.assertFalse(qt_family_is_ambiguous('[未闭合'))
        self.assertFalse(qt_family_is_ambiguous('闭合]在前'))

    def test_strip_brackets(self):
        self.assertEqual(strip_qt_foundry_brackets('[工具箱]书卷楷-简繁'), '工具箱书卷楷-简繁')
        self.assertEqual(strip_qt_foundry_brackets(' [toolbox]X '), 'toolboxX')
        self.assertEqual(strip_qt_foundry_brackets(''), '')


if __name__ == '__main__':
    unittest.main()
