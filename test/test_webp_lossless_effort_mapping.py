import _bootstrap  # noqa: F401, I001

import io

import numpy as np
import pytest
from PIL import Image

from manga_translator.utils.generic import lossless_webp_method, save_pil_image


def _capture_webp_options(monkeypatch, effort):
    options = []
    original_save = Image.Image.save

    def capture_save(image, fp, format=None, **kwargs):
        options.append(kwargs)
        return original_save(image, fp, format=format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", capture_save)
    with Image.new("RGB", (16, 16), (120, 80, 40)) as image, io.BytesIO() as output:
        save_pil_image(image, output, format="WEBP", lossless=True, compression_effort=effort)
    assert len(options) == 1
    return options[0]


@pytest.mark.parametrize("effort", [0, 1, 50, 70, 90, 92, 99])
def test_lossless_webp_keeps_linear_method_mapping_below_maximum(effort):
    assert lossless_webp_method(effort) == round(effort * 6 / 100)


@pytest.mark.parametrize("effort", [100, None, "", "invalid", 150])
def test_maximum_lossless_webp_effort_never_requests_libwebp_exhaustive_search(monkeypatch, effort):
    # method=6 with quality=100 makes libwebp re-encode the image once per
    # transform/entropy combination: several times slower, same output size.
    options = _capture_webp_options(monkeypatch, effort)
    assert options["lossless"] is True
    assert options["quality"] == 100
    assert options["method"] == 5
    assert options["exact"] is True


@pytest.mark.parametrize("effort", [0, 50, 90, 99, 100])
def test_lossless_webp_options_never_combine_method_6_with_quality_100(monkeypatch, effort):
    options = _capture_webp_options(monkeypatch, effort)
    assert options["quality"] == effort
    assert not (options["method"] == 6 and options["quality"] == 100)


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_maximum_effort_lossless_webp_is_pixel_exact(mode):
    channels = 4 if mode == "RGBA" else 3
    pixels = np.random.default_rng(2024).integers(0, 256, (37, 41, channels), dtype=np.uint8)
    if mode == "RGBA":
        pixels[:9, :, 3] = 0
    with Image.fromarray(pixels) as image, io.BytesIO() as output:
        save_pil_image(image, output, format="WEBP", lossless=True, compression_effort=100)
        output.seek(0)
        with Image.open(output) as decoded:
            assert np.array_equal(np.asarray(decoded.convert(mode)), pixels)
