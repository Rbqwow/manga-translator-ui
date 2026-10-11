"""Pixel-exact AVIF encoding for the application's 8-bit output images."""

import os
from typing import BinaryIO

import numpy as np
from PIL import Image

from ..image_formats import normalize_compression_effort
from .image_modes import pil_image_has_alpha


def save_lossless_avif(
    image: Image.Image,
    output_path: str | os.PathLike[str] | BinaryIO,
    *,
    compression_effort: int = 70,
    icc_profile: bytes | None = None,
    exif: bytes | Image.Exif | None = None,
    xmp: bytes | str | None = None,
    max_threads: int | None = None,
) -> None:
    if image.mode in {"I", "F", "I;16", "I;16L", "I;16B", "I;16N"}:
        raise ValueError("Lossless AVIF saving supports 8-bit images; high-precision pixels cannot be reduced losslessly.")

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

        pixels = np.asarray(image)
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
        if exif:
            context.add_exif_metadata(handle, exif.tobytes() if isinstance(exif, Image.Exif) else exif)
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
