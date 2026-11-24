import _bootstrap  # noqa: F401

import io

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
