import _bootstrap  # noqa: F401, I001

import asyncio
import logging

import numpy as np

from manga_translator.config import OcrConfig
from manga_translator.ocr.model_paddleocr_vl import ModelPaddleOCRVL
from manga_translator.utils import Quadrilateral


def test_paddleocr_vl_recognition_keeps_native_vertical_crop():
    image = np.full((195, 137, 3), 255, dtype=np.uint8)
    image[20:90, 35:100] = 0
    q = Quadrilateral(
        pts=np.array([[0, 0], [136, 0], [136, 194], [0, 194]], dtype=np.int32),
        text="",
        prob=1.0,
    )
    seen = {}
    ocr = ModelPaddleOCRVL.__new__(ModelPaddleOCRVL)
    ocr.logger = logging.getLogger("test.paddleocr_vl")
    ocr.use_gpu = False

    def recognize(region, _prompt):
        seen["recognition"] = region.copy()
        return "お・・・"

    def estimate_colors(source_image, _q, direction):
        seen["color_source"] = source_image
        seen["color_direction"] = direction

    ocr._recognize_single = recognize
    ocr._estimate_colors_48px = estimate_colors
    ocr._cleanup_ocr_memory = lambda *_args: None

    result = asyncio.run(ocr._infer(
        image,
        [q],
        OcrConfig(ignore_bubble=0, ocr_vl_language_hint="Japanese"),
    ))

    assert np.array_equal(seen["recognition"], image)
    assert seen["color_source"] is image
    assert seen["color_direction"] == "v"
    assert result[0].text == "お・・・"
