import _bootstrap  # noqa: F401, I001

import numpy as np

from manga_translator import rendering
from manga_translator.config import Config
from manga_translator.utils import TextBlock


def test_balloon_fill_mask_layout_preserves_translator_line_breaks():
    image = np.zeros((400, 400, 3), dtype=np.uint8)
    bubble_mask = np.zeros((400, 400), dtype=np.uint8)
    bubble_mask[90:271, 90:331] = 1

    translation = "第一行文字[BR]第二行更长的文字[BR]第三行"
    region = TextBlock(
        lines=[
            [[100, 100], [320, 100], [320, 180], [100, 180]],
            [[100, 180], [320, 180], [320, 260], [100, 260]],
        ],
        texts=["原文", "原文"],
        font_size=48,
        translation=translation,
        direction="h",
        target_lang="CHS",
    )
    config = Config()
    config.render.layout_mode = "balloon_fill"
    config.render.balloon_fill_mask_layout = True

    points = rendering.resize_regions_to_font_size(
        image,
        [region],
        config,
        original_img=image.copy(),
        skip_text_replacements=True,
        bubble_mask=bubble_mask,
    )[0]

    assert region.translation == translation
    assert rendering._polygon_fully_inside_mask(np.asarray(points[0]), bubble_mask)


def test_balloon_fill_mask_layout_uses_mask_range_for_text_without_breaks():
    image = np.zeros((400, 400, 3), dtype=np.uint8)
    bubble_mask = np.zeros((400, 400), dtype=np.uint8)
    bubble_mask[90:271, 90:331] = 1

    source_text = "这是一段没有显式断行并且需要根据气泡范围重新安排断点的测试文字"

    def run_layout(safety_enabled: bool) -> str:
        region = TextBlock(
            lines=[
                [[190, 100], [210, 100], [210, 180], [190, 180]],
                [[190, 180], [210, 180], [210, 260], [190, 260]],
            ],
            texts=["原文", "原文"],
            font_size=24,
            translation=source_text,
            direction="h",
            target_lang="CHS",
        )
        config = Config()
        config.render.layout_mode = "balloon_fill"
        config.render.balloon_fill_mask_layout = safety_enabled
        rendering.resize_regions_to_font_size(
            image,
            [region],
            config,
            original_img=image.copy(),
            skip_text_replacements=True,
            bubble_mask=bubble_mask,
        )
        return region.translation

    legacy_text = run_layout(False)
    mask_layout_text = run_layout(True)

    assert mask_layout_text.replace("[BR]", "") == source_text
    assert mask_layout_text.count("[BR]") < legacy_text.count("[BR]")
