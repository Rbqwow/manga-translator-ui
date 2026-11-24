import _bootstrap  # noqa: F401, I001

import io

import pytest
from PIL import Image

from manga_translator.utils.generic import save_pil_image


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


@pytest.mark.parametrize("effort", [100, None, "", "invalid", 150])
def test_maximum_lossless_webp_effort_never_requests_libwebp_exhaustive_search(monkeypatch, effort):
    # method=6 with quality=100 makes libwebp re-encode the image once per
    # transform/entropy combination: several times slower, same output size.
    options = _capture_webp_options(monkeypatch, effort)
    assert options["lossless"] is True
    assert options["quality"] == 100
    assert options["method"] == 5
    assert options["exact"] is True
