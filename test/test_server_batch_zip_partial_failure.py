"""One unencodable page must not turn a whole batch download into a 500.

The ZIP builder used to let the first encoder error escape, so a single
unsupported page (a lossless AVIF save without pylibheif, for example) lost the
other pages with it — while the CLI, the desktop app and the concurrent
pipeline all record the page and keep going.
"""

import _bootstrap  # noqa: F401

import io
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from manga_translator.server.routes import translation as routes


def _config():
    return SimpleNamespace(
        cli=SimpleNamespace(
            format="png", save_quality=90, save_lossless=False, compression_effort=100,
        )
    )


def _images(*names):
    return [
        {"index": index, "image": Image.new("RGB", (8, 8), "white")}
        for index, _name in enumerate(names)
    ]


@pytest.fixture
def flaky_encoder(monkeypatch):
    """Encode everything except the images registered as failing."""
    failing = set()

    def fake_save(image, target, **_kwargs):
        if id(image) in failing:
            raise RuntimeError("controlled encoder failure")
        image.save(target, format="PNG")
        return True

    monkeypatch.setattr(routes, "save_pil_image", fake_save)
    return failing


def test_one_bad_page_is_skipped_but_the_rest_still_download(flaky_encoder):
    images = _images("a.jpg", "b.jpg", "c.jpg")
    flaky_encoder.add(id(images[1]["image"]))

    data, skipped = routes._build_batch_image_zip(images, ["a.jpg", "b.jpg", "c.jpg"], _config())

    assert skipped == ["b.png"]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["a.png", "c.png"]


def test_all_pages_failing_still_raises_instead_of_returning_an_empty_zip(flaky_encoder):
    images = _images("a.jpg", "b.jpg")
    flaky_encoder.update(id(item["image"]) for item in images)

    with pytest.raises(RuntimeError, match="No page could be encoded"):
        routes._build_batch_image_zip(images, ["a.jpg", "b.jpg"], _config())


def test_nothing_to_encode_produces_an_empty_archive(flaky_encoder):
    data, skipped = routes._build_batch_image_zip([], [], _config())

    assert skipped == []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == []
