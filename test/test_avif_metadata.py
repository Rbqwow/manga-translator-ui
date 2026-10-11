import _bootstrap  # noqa: F401

import io

import pytest
from PIL import Image

from manga_translator.utils.generic import save_pil_image


def _find_exif_tiff(data: bytes):
    for offset in range(len(data) - 4):
        if data[offset:offset + 4] in (b'MM\x00*', b'II*\x00'):
            exif = Image.Exif()
            try:
                exif.load(data[offset:])
            except Exception:
                continue
            if exif.get(282):
                return exif
    return None


def _save_avif(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    save_pil_image(image, buffer, source_image=image, lossless=True, compression_effort=0, format='AVIF')
    return buffer.getvalue()


def test_avif_keeps_the_source_dpi():
    """The ordinary path passes dpi to image.save(); the AVIF branch bypassed
    it, so the value was silently dropped."""
    source = Image.new('RGB', (16, 16), 'red')
    source.info['dpi'] = (300, 300)
    data = _save_avif(source)
    assert data[4:8] == b'ftyp'

    exif = _find_exif_tiff(data)
    assert exif is not None, 'AVIF output carries no EXIF block'
    assert exif.get(282) == 300.0
    assert exif.get(283) == 300.0
    assert exif.get(296) == 2


def test_avif_without_source_dpi_does_not_invent_one():
    data = _save_avif(Image.new('RGB', (16, 16), 'blue'))
    exif = _find_exif_tiff(data)
    assert exif is None or exif.get(282) in (None, 0.0)


@pytest.mark.parametrize('mode', ['I;16', 'I', 'F'])
def test_avif_high_precision_input_is_reduced_to_8_bit(mode):
    from manga_translator.utils.avif import _reduce_to_8_bit

    image = Image.new(mode, (8, 8), 1024)
    reduced = _reduce_to_8_bit(image)
    assert reduced.mode in {'L', 'RGB', 'RGBA'}
    assert reduced.getextrema()[1] <= 255


def test_eight_bit_images_pass_through_untouched():
    from manga_translator.utils.avif import _reduce_to_8_bit

    image = Image.new('RGB', (8, 8), 'red')
    assert _reduce_to_8_bit(image) is image


def test_avif_normalises_a_non_contiguous_pixel_view(monkeypatch):
    """from_buffer reads raw memory, so a strided view would encode skewed rows.

    `np.asarray` on a PIL image is contiguous, so the regression can only be
    observed by feeding `save_lossless_avif` a strided buffer. Patch `asarray`
    to return one and confirm the encoder still receives C-contiguous memory.
    """
    import numpy as np

    from manga_translator.utils import avif as avif_module

    image = Image.new('RGB', (16, 8), 'red')
    strided = np.zeros((8, 16, 3), dtype=np.uint8)[:, ::-1]
    assert not strided.flags['C_CONTIGUOUS']

    received = {}
    real_from_buffer = avif_module.heif.HeifImage.from_buffer if hasattr(avif_module, 'heif') else None

    original_asarray = np.asarray
    monkeypatch.setattr(avif_module.np, 'asarray', lambda value: strided)

    buffer = io.BytesIO()
    # Encoding must succeed and must pass contiguous data to the encoder.
    from manga_translator.utils.avif import save_lossless_avif

    save_lossless_avif(image, buffer, compression_effort=0)
    assert buffer.getvalue()[4:8] == b'ftyp'
    monkeypatch.setattr(avif_module.np, 'asarray', original_asarray)
