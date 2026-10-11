"""Pixel-exact AVIF encoding for the application's 8-bit output images."""

import os
from typing import BinaryIO

import numpy as np
from PIL import Image

from ..image_formats import normalize_compression_effort
from .image_modes import pil_image_has_alpha

_HIGH_DEPTH_MODES = {"I", "F", "I;16", "I;16L", "I;16B", "I;16N"}


def _exif_with_dpi(exif, dpi) -> bytes | None:
    """Carry the source DPI in EXIF, the only metadata AVIF exposes for it.

    AVIF has no native resolution field, and Pillow cannot open AVIF, so a
    reader gets the DPI from the same EXIF block it already uses for the image.
    """
    if not dpi and not exif:
        return None

    try:
        resolved = Image.Exif()
        if isinstance(exif, Image.Exif):
            resolved = exif
        elif exif:
            resolved = Image.Exif()
            resolved.load(exif.tobytes() if isinstance(exif, Image.Exif) else exif)

        if dpi and len(dpi) >= 2 and dpi[0] and dpi[1]:
            # 282/283 = X/YResolution, 296 = ResolutionUnit (2 = inch).
            resolved[282] = float(dpi[0])
            resolved[283] = float(dpi[1])
            resolved.setdefault(296, 2)
        return resolved.tobytes() if resolved else None
    except Exception:
        # Unreadable EXIF must not fail an otherwise valid save.
        return exif.tobytes() if isinstance(exif, Image.Exif) else exif


def _reduce_to_8_bit(image: Image.Image) -> Image.Image:
    """Convert 16-bit/float images to an 8-bit mode for AVIF encoding.

    ``Image.convert("L")`` is not usable here: Pillow clamps ``I;16`` values
    above 255 instead of scaling them, so a full-range 16-bit page collapses to
    black-and-white. Scale explicitly instead, keeping the source's own range:

    * ``I;16*`` — the container is 16-bit, so divide by 256 (65535 -> 255).
    * ``I`` / ``F`` — the container holds arbitrary numbers, so divide by the
      observed peak when it exceeds the 8-bit range.

    Anything already 8-bit is returned unchanged so existing callers keep their
    exact pixels.
    """
    if image.mode not in _HIGH_DEPTH_MODES:
        return image

    import numpy as np

    pixels = np.nan_to_num(np.asarray(image, dtype=np.float64))

    peak = float(pixels.max()) if pixels.size else 0.0

    if image.mode == "F":
        # Float images already use 0-255, but tolerate -1/255 sentinels.
        pixels = np.clip(pixels, 0, 255)
    elif image.mode.startswith("I;16") and peak > 255:
        # The container is 16-bit, so the samples are 16-bit codes.
        pixels = pixels / 256.0
    elif peak > 255:
        # Plain "I" (and 16-bit files holding 8-bit data) carry arbitrary
        # values; scale by the observed peak so nothing is clipped to white.
        pixels = pixels * (255.0 / min(peak, 65535.0)) if peak <= 65535 else pixels * (255.0 / peak)

    pixels = np.clip(pixels, 0, 255)
    reduced = Image.fromarray(pixels.astype(np.uint8))
    if pil_image_has_alpha(image) or "transparency" in getattr(image, "info", {}):
        return reduced.convert("RGBA")
    return reduced


def save_lossless_avif(
    image: Image.Image,
    output_path: str | os.PathLike[str] | BinaryIO,
    *,
    compression_effort: int = 100,
    icc_profile: bytes | None = None,
    exif: bytes | Image.Exif | None = None,
    xmp: bytes | str | None = None,
    dpi: tuple[float, float] | None = None,
    max_threads: int | None = None,
) -> None:
    # A 16-bit TIFF is a supported input (PIL_FORMAT_BY_EXTENSION), so failing
    # the whole batch on it is worse than the silent 8-bit reduction that
    # JPEG/BMP/HEIF/WebP already perform. AVIF output stays 8-bit, and the
    # lossless promise applies to the written file, not the input depth.
    image = _reduce_to_8_bit(image)

    # Keep the native encoder optional at import time for existing installations.
    try:
        import pylibheif as heif
    except ImportError as exc:
        raise RuntimeError(
            "Lossless AVIF saving requires pylibheif; update the project dependencies."
        ) from exc

    descriptors = heif.get_encoder_descriptors(heif.HeifCompressionFormat.AV1, "aom")
    if not descriptors:
        raise RuntimeError("Lossless AVIF saving requires the pylibheif AOM encoder.")

    encoder = heif.HeifEncoder(descriptors[0])
    encoder.set_lossless(True)
    encoder.set_boolean_parameter("lossless-alpha", True)
    encoder.set_string_parameter("chroma", "444")
    effort = normalize_compression_effort(compression_effort)
    encoder.set_integer_parameter("speed", 9 - round(effort * 9 / 100))
    threads = min(os.cpu_count() or 1, 4) if max_threads is None else max_threads
    encoder.set_integer_parameter("threads", max(1, min(64, int(threads))))

    converted = None
    context = heif.HeifContext()
    try:
        if image.mode not in {"RGB", "RGBA"} or "transparency" in image.info:
            mode = "RGBA" if pil_image_has_alpha(image) or "transparency" in image.info else "RGB"
            converted = image.convert(mode)
            image = converted

        # from_buffer reads raw memory assuming tightly packed rows. A strided
        # view (crop, transpose, or a read-only buffer) would make the encoder
        # walk the wrong bytes and produce skewed rows, so normalise the layout.
        pixels = np.ascontiguousarray(np.asarray(image))
        raw_image = heif.HeifImage.from_buffer(
            pixels,
            image.width,
            image.height,
            heif.HeifColorspace.RGB,
            heif.HeifChroma.InterleavedRGBA if image.mode == "RGBA" else heif.HeifChroma.InterleavedRGB,
            bit_depth=8,
        )
        # Identity stores RGB as GBR planes without RGB/YUV rounding. Full range
        # and 4:4:4 are necessary even when AV1 quantization is disabled.
        raw_image.set_nclx_color_profile(heif.HeifColorProfileNclx(
            heif.HeifColorPrimaries.Unspecified,
            heif.HeifTransferCharacteristics.Unspecified,
            heif.HeifMatrixCoefficients.RGB_GBR,
            True,
        ))
        if icc_profile:
            raw_image.set_raw_color_profile("prof", icc_profile)

        options = heif.HeifEncodingOptions()
        options.save_alpha_channel = True
        options.save_two_colr_boxes_when_ICC_and_nclx_available = True
        handle = encoder.encode_image(context, raw_image, options=options)
        exif_payload = _exif_with_dpi(exif, dpi)
        if exif_payload:
            context.add_exif_metadata(handle, exif_payload)
        if xmp:
            context.add_xmp_metadata(handle, xmp.encode("utf-8") if isinstance(xmp, str) else xmp)

        encoded = context.write_to_memoryview()
        if hasattr(output_path, "write"):
            # The server encodes ZIP entries to BytesIO; the caller owns the stream.
            output_path.write(encoded)
        else:
            # Python handles Unicode paths and the caller's atomic .tmp destination.
            with open(output_path, "wb") as output:
                output.write(encoded)
    finally:
        context.close()
        if converted is not None:
            converted.close()
