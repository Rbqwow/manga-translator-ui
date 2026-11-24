"""Pure pipeline adapter functions bridging MangaTranslator and MCP jobs."""

from __future__ import annotations

import copy
from io import BytesIO
import logging
import os
import re
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from PIL import Image

from .protocol_models import (
    ApiConflictItem,
    PageBatchItem,
    RegionItem,
    TranslationSubmissionItem,
    ValidationErrorItem,
)

logger = logging.getLogger("manga_translator.mcp")

# Serializes glossary read-merge-write across parallel MCP batch submissions.
_GLOSSARY_MERGE_LOCK = threading.Lock()
# Serializes translator overlay handoff across GpuPriorityWorker and RenderWorker.
_OVERLAY_STATE_LOCK = threading.Lock()

_API_CONFLICT_SPECS = (
    ("ocr", "ocr", "OCR"),
    ("ocr", "secondary_ocr", "Secondary OCR"),
    ("render", "renderer", "Renderer"),
    ("colorizer", "colorizer", "Colorizer"),
)

_PREVIEW_MIME_BY_FORMAT = {
    "jpeg": ("JPEG", "image/jpeg", ".jpg"),
    "webp": ("WEBP", "image/webp", ".webp"),
    "png": ("PNG", "image/png", ".png"),
}

_LOW_CONFIDENCE_THRESHOLD = 0.6


def _enum_str(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _as_dict(config_data: Any) -> Dict[str, Any]:
    if hasattr(config_data, "model_dump"):
        return config_data.model_dump()
    if isinstance(config_data, dict):
        return copy.deepcopy(config_data)
    return {}


def find_api_backend_conflicts(config_data: Any) -> List[ApiConflictItem]:
    """Return any OCR/renderer/colorizer settings that rely on external APIs."""
    data = _as_dict(config_data)
    conflicts: List[ApiConflictItem] = []
    for section, setting, label in _API_CONFLICT_SPECS:
        section_data = data.get(section)
        if not isinstance(section_data, dict):
            continue
        if section == "ocr" and setting == "secondary_ocr" and not bool(section_data.get("use_hybrid_ocr", False)):
            continue
        value = _enum_str(section_data.get(setting))
        lowered = value.lower()
        if lowered.startswith("openai_") or lowered.startswith("gemini_"):
            conflicts.append(
                ApiConflictItem(
                    section=section,
                    setting=setting,
                    value=value,
                    message=(
                        f"{label} ({section}.{setting}) is set to external API backend '{value}'. "
                        "Switch it to a local model in Settings before starting an MCP job."
                    ),
                )
            )
    return conflicts


def build_mcp_engine_config(
    config_dict: Any,
    root_dir: str,
    *,
    target_lang: Optional[str] = None,
    overwrite: Optional[bool] = None,
    batch_size: Optional[int] = None,
    context_size: Optional[int] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """Build a 0-API engine Config snapshot and MangaTranslator init params."""
    from manga_translator.config import (
        CliConfig,
        ColorizerConfig,
        Config,
        DetectorConfig,
        InpainterConfig,
        OcrConfig,
        RenderConfig,
        TranslatorConfig,
        UpscaleConfig,
    )

    raw = _as_dict(config_dict)
    cli_data = dict(raw.get("cli") or {})
    if overwrite is not None:
        cli_data["overwrite"] = bool(overwrite)
    if batch_size is not None:
        cli_data["batch_size"] = max(1, int(batch_size))
    if context_size is not None:
        cli_data["context_size"] = max(0, int(context_size))

    # MCP owns orchestration; disable workflow override flags cleanly.
    for workflow_flag in (
        "load_text",
        "translate_json_only",
        "template",
        "generate_and_export",
        "colorize_only",
        "upscale_only",
        "inpaint_only",
        "replace_translation",
    ):
        cli_data[workflow_flag] = False

    render_data = dict(raw.get("render") or {})
    if "direction" in render_data:
        dir_val = render_data["direction"]
        if dir_val == "h":
            render_data["direction"] = "horizontal"
        elif dir_val == "v":
            render_data["direction"] = "vertical"

    translator_data = dict(raw.get("translator") or {})
    if target_lang:
        translator_data["target_lang"] = str(target_lang).strip()

    # Hard 0-API isolation: never instantiate OpenAI/Gemini/Sakura translators or read API keys.
    translator_data["translator"] = "none"
    translator_data["translator_chain"] = None
    translator_data["selective_translation"] = None
    translator_data["user_api_key"] = None
    translator_data["user_api_base"] = None
    translator_data["user_api_model"] = None

    hq_prompt_path = translator_data.get("high_quality_prompt_path")
    if hq_prompt_path and not os.path.isabs(hq_prompt_path):
        candidate = resolve_custom_prompt_file_path(hq_prompt_path, root_dir)
        if candidate and os.path.exists(candidate):
            translator_data["high_quality_prompt_path"] = candidate

    upscale_data = dict(raw.get("upscale") or {})
    if "upscale_ratio" in upscale_data:
        ratio_val = upscale_data["upscale_ratio"]
        if ratio_val in ("不使用", None, ""):
            upscale_data["upscale_ratio"] = None
        elif isinstance(ratio_val, str) and ratio_val in ("x2", "x4", "DAT2 x4"):
            upscale_data["upscale_ratio"] = ratio_val
        else:
            try:
                upscale_data["upscale_ratio"] = int(ratio_val)
            except (TypeError, ValueError):
                upscale_data["upscale_ratio"] = None

    explicit_keys = {"render", "upscale", "translator", "detector", "colorizer", "inpainter", "ocr", "cli"}
    remaining_config = {
        key: value
        for key, value in raw.items()
        if key in Config.model_fields and key not in explicit_keys
    }
    filtered_cli_data = {
        key: value
        for key, value in cli_data.items()
        if key in CliConfig.model_fields
    }

    config = Config(
        render=RenderConfig(**render_data),
        upscale=UpscaleConfig(**upscale_data),
        translator=TranslatorConfig(**translator_data),
        detector=DetectorConfig(**dict(raw.get("detector") or {})),
        colorizer=ColorizerConfig(**dict(raw.get("colorizer") or {})),
        inpainter=InpainterConfig(**dict(raw.get("inpainter") or {})),
        ocr=OcrConfig(**dict(raw.get("ocr") or {})),
        cli=CliConfig(**filtered_cli_data),
        **remaining_config,
    )
    config._allow_server_api_keys = False

    translator_params = dict(cli_data)
    translator_params.update(
        {
            "filter_text_enabled": bool(raw.get("filter_text_enabled", False)),
            "kernel_size": int(raw.get("kernel_size", 3) or 3),
            "force_simple_sort": bool(raw.get("force_simple_sort", False)),
            "mask_dilation_offset": int(raw.get("mask_dilation_offset", 50) or 50),
            "font_family": render_data.get("font_family"),
            # Skip eager prepare_translation() in _translate_until_translation;
            # local CV models still lazy-load on first use, and McpJobManager owns idle TTL.
            "models_loaded": True,
            "start_model_cleanup_job": False,
        }
    )
    return config, translator_params


def create_mcp_translator(translator_params: Dict[str, Any]):
    """Lazily instantiate MangaTranslator configured for MCP 0-API execution."""
    from manga_translator.manga_translator import MangaTranslator

    translator = MangaTranslator(params=translator_params)
    translator._mcp_context_enabled = True
    translator._models_loaded = True
    translator.start_model_cleanup_job = False
    return translator


def _get_prompt_br_helper():
    from manga_translator.translators.common import CommonTranslator

    class _McpPromptAndBrHelper(CommonTranslator):
        async def _translate(self, from_lang: str, to_lang: str, queries: List[str]) -> List[str]:
            raise RuntimeError("MCP mode never calls external translator APIs")

    return _McpPromptAndBrHelper()


def resolve_custom_prompt_file_path(prompt_path: Optional[str], root_dir: str) -> Optional[str]:
    """Resolve the active custom prompt YAML/JSON path matching prompt_loader fallback rules."""
    if not prompt_path:
        return None
    resolved = prompt_path if os.path.isabs(prompt_path) else os.path.join(root_dir, prompt_path)
    if os.path.exists(resolved):
        return os.path.abspath(resolved)
    base, ext = os.path.splitext(resolved)
    for alt_ext in (".yaml", ".yml", ".json"):
        if alt_ext != ext:
            alt_path = base + alt_ext
            if os.path.exists(alt_path):
                return os.path.abspath(alt_path)
    return os.path.abspath(resolved)


async def load_job_guidelines_and_glossary(
    translator: Any,
    config: Any,
    root_dir: str,
) -> Tuple[str, Dict[str, Any], Optional[str], bool]:
    """Load task-level system guidelines, glossary snapshot, resolved glossary path, and effective extract flag."""
    from manga_translator.utils import Context

    temp_ctx = Context()
    temp_ctx.config = config
    temp_ctx = await translator._load_and_prepare_prompts(config, temp_ctx)

    custom_prompt_json = getattr(temp_ctx, "custom_prompt_json", None)
    helper = _get_prompt_br_helper()
    effective_extract_glossary, use_builtin_system_prompt = helper._resolve_prompt_flags(
        config,
        custom_prompt_json,
    )
    guidelines = helper._build_system_prompt(
        source_lang="",
        target_lang=config.translator.target_lang,
        custom_prompt_json=custom_prompt_json,
        line_break_prompt_json=getattr(temp_ctx, "line_break_prompt_json", None),
        extract_glossary=effective_extract_glossary,
        use_builtin_system_prompt=use_builtin_system_prompt,
    )
    mcp_note = (
        "\n\n[MCP Submission Note]\n"
        "When running under MCP, submit all translated items via the "
        "`submit_translation_batch(job_id, batch_id, translations, new_terms=...)` tool arguments "
        "(and pass extracted glossary entries in `new_terms` when enabled) rather than outputting raw JSON code blocks."
    )
    guidelines = f"{guidelines.rstrip()}{mcp_note}" if guidelines else mcp_note.strip()

    custom_json = custom_prompt_json or {}
    raw_glossary = custom_json.get("glossary") if isinstance(custom_json, dict) else {}
    glossary = copy.deepcopy(raw_glossary) if isinstance(raw_glossary, dict) else {}

    prompt_path = getattr(config.translator, "high_quality_prompt_path", None)
    resolved_prompt_path = resolve_custom_prompt_file_path(prompt_path, root_dir) if custom_prompt_json else None
    return guidelines, glossary, resolved_prompt_path, effective_extract_glossary


async def preprocess_page(
    translator: Any,
    image_path: str,
    config: Any,
    *,
    retranslate_json: bool = False,
):
    """Run local preprocessing (or load existing JSON in retranslate_json mode) for one page."""
    import cv2
    import numpy as np
    from manga_translator.utils import Context, load_image, open_pil_image
    from manga_translator.utils.generic import get_image_md5
    from manga_translator.utils.path_manager import find_work_image_path

    if not retranslate_json:
        with open(image_path, "rb") as handle:
            image = open_pil_image(handle, eager=True)
        image.name = image_path
        translator._set_image_context(config, image)
        image_md5 = get_image_md5(image)
        translator._save_current_image_context(image_md5)
        ctx = await translator._translate_until_translation(image, config)
        ctx.image_name = image_path
        ctx.original_size = image.size
        ctx.config = config
        ctx._initial_region_ids = {id(r) for r in (ctx.text_regions or [])}
        return ctx

    with _OVERLAY_STATE_LOCK:
        loaded_regions, loaded_mask, mask_is_refined, _skip_scale, _skip_rep, parse_failures = (
            translator._load_text_and_regions_from_file(image_path, config)
        )
        loaded_render_overlays = getattr(translator, "_loaded_render_overlays", None)
        pending_paste_overlays = getattr(translator, "_pending_paste_overlays", None)
        if hasattr(translator, "_loaded_render_overlays"):
            translator._loaded_render_overlays = None
        if hasattr(translator, "_pending_paste_overlays"):
            translator._pending_paste_overlays = None
    if loaded_regions is None:
        raise FileNotFoundError(f"Existing translation JSON not found for: {image_path}")

    with open(image_path, "rb") as handle:
        orig_image = open_pil_image(handle, eager=True)
    orig_image.name = image_path

    work_image_path = find_work_image_path(image_path)
    if work_image_path and os.path.exists(work_image_path):
        with open(work_image_path, "rb") as handle:
            upscaled_image = open_pil_image(handle, eager=True)
        upscaled_image.name = image_path
    else:
        upscaled_image = orig_image

    ctx = Context()
    ctx.input = orig_image
    ctx.img_colorized = orig_image
    ctx.upscaled = upscaled_image
    ctx.image_name = image_path
    ctx.original_size = orig_image.size
    ctx.config = config
    ctx.verbose = getattr(translator, "verbose", False)
    ctx.save_quality = getattr(translator, "save_quality", 100)
    ctx.save_lossless = getattr(translator, "save_lossless", True)
    ctx.compression_effort = getattr(translator, "compression_effort", 100)
    ctx.load_text_parse_failures = parse_failures
    ctx._mcp_loaded_render_overlays = loaded_render_overlays
    ctx._mcp_pending_paste_overlays = pending_paste_overlays

    translator._set_image_context(config, orig_image)
    image_md5 = get_image_md5(orig_image)
    translator._save_current_image_context(image_md5)

    ctx.img_rgb, ctx.img_alpha = load_image(ctx.upscaled)
    ctx.bubble_mask = None

    translator._prepare_loaded_regions(loaded_regions, use_text_as_translation=False)
    for region in loaded_regions:
        region.translation = ""
        if hasattr(region, "translation_raw"):
            region.translation_raw = ""
        if hasattr(region, "translation_rich"):
            region.translation_rich = None

    ctx.text_regions = loaded_regions
    # Re-translation produces new text with different lengths; re-run smart font scaling.
    ctx.skip_font_scaling = False
    translator._apply_pre_dictionary_to_regions(ctx)

    target_h, target_w = ctx.img_rgb.shape[:2]
    if loaded_mask is not None:
        mask_arr = np.asarray(loaded_mask)
        if mask_arr.ndim == 3:
            mask_arr = mask_arr[:, :, 0]
        elif mask_arr.ndim != 2:
            squeezed = np.squeeze(mask_arr)
            if squeezed.ndim == 2:
                mask_arr = squeezed
            else:
                logger.warning(
                    f"[retranslate_json] Invalid loaded mask shape {mask_arr.shape}; falling back to zero mask"
                )
                mask_arr = np.zeros((target_h, target_w), dtype=np.uint8)
        if mask_arr.shape[:2] != (target_h, target_w):
            mask_arr = cv2.resize(mask_arr, (target_w, target_h), interpolation=cv2.INTER_NEAREST)
        if mask_arr.dtype != np.uint8:
            mask_arr = mask_arr.astype(np.uint8, copy=False)
        if mask_is_refined:
            ctx.mask = mask_arr
            ctx.mask_raw = mask_arr.copy()
        else:
            ctx.mask_raw = mask_arr
    else:
        mask = np.zeros((target_h, target_w), dtype=np.uint8)
        polygons = []
        for r in ctx.text_regions:
            lines_val = getattr(r, "lines", None)
            if lines_val is None:
                continue
            for p in lines_val:
                arr_p = np.asarray(p)
                if arr_p.size >= 6:
                    polygons.append(np.asarray(arr_p, dtype=np.int32).reshape((-1, 1, 2)))
        if polygons:
            cv2.fillPoly(mask, polygons, 255)
        ctx.mask_raw = mask

    needs_bubble_cache = (
        bool(getattr(config.ocr, "limit_mask_dilation_to_bubble_mask", False))
        or bool(getattr(config.ocr, "use_model_bubble_repair_intersection", False))
        or getattr(config.render, "layout_mode", None) == "balloon_fill"
        or bool(getattr(config.render, "center_text_in_bubble", False))
        or bool(getattr(config.inpainter, "solid_fill_pure_bubbles", False))
    )
    if needs_bubble_cache and hasattr(translator, "_prime_bubble_detection_cache"):
        translator._prime_bubble_detection_cache(config, ctx)

    ctx._initial_region_ids = {id(r) for r in loaded_regions}
    return ctx


def build_page_region_items(
    ctx: Any,
    start_order: int,
    *,
    low_conf_threshold: float = _LOW_CONFIDENCE_THRESHOLD,
) -> Tuple[List[RegionItem], List[int], List[int]]:
    """Build RegionItem payloads, continuous text_order IDs, and low-confidence IDs."""
    regions = list(getattr(ctx, "text_regions", None) or [])
    items: List[RegionItem] = []
    text_order: List[int] = []
    low_conf_ids: List[int] = []

    for offset, region in enumerate(regions):
        region_id = start_order + offset
        text_order.append(region_id)

        raw_lines = getattr(region, "lines", None)
        try:
            region_count = max(int(len(raw_lines)), 1) if raw_lines is not None else 1
        except TypeError:
            region_count = 1

        raw_prob = getattr(region, "prob", 1.0)
        try:
            prob_val = round(float(raw_prob), 4)
        except (TypeError, ValueError):
            prob_val = 1.0

        raw_dir = getattr(region, "direction", None)
        if not raw_dir:
            raw_dir = getattr(region, "_direction", "auto")
        direction_str = _enum_str(raw_dir) or "auto"

        text_val = str(getattr(region, "text", "") or "")
        items.append(
            RegionItem(
                id=region_id,
                text=text_val,
                original_region_count=region_count,
                direction=direction_str,
                prob=prob_val,
            )
        )
        if prob_val < low_conf_threshold:
            low_conf_ids.append(region_id)

    return items, text_order, low_conf_ids


def encode_pil_preview(
    image: Image.Image,
    *,
    max_size: int = 1024,
    fmt: str = "jpeg",
    quality: int = 82,
) -> Tuple[bytes, str, int, int]:
    """Downscale and encode a PIL image for efficient MCP multimodal transport."""
    from PIL import Image
    from manga_translator.utils.image_modes import normalize_rgb_image

    rgb = normalize_rgb_image(image)
    w, h = rgb.size
    if max_size > 0 and max(w, h) > max_size:
        scale = max_size / float(max(w, h))
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        rgb = rgb.resize((new_w, new_h), Image.LANCZOS)

    fmt_key = str(fmt or "jpeg").strip().lower()
    pil_fmt, mime_type, _ext = _PREVIEW_MIME_BY_FORMAT.get(fmt_key, _PREVIEW_MIME_BY_FORMAT["jpeg"])

    buf = BytesIO()
    if pil_fmt == "PNG":
        rgb.save(buf, format="PNG", optimize=True)
    elif pil_fmt == "WEBP":
        rgb.save(buf, format="WEBP", quality=max(1, min(100, int(quality))))
    else:
        rgb.save(buf, format="JPEG", quality=max(1, min(100, int(quality))), optimize=True)

    return buf.getvalue(), mime_type, rgb.width, rgb.height


def _base_preview_image_and_upscaled_size(ctx: Any) -> Tuple[Image.Image, Optional[Tuple[int, int]]]:
    from PIL import Image

    base_img = getattr(ctx, "input", None)
    img_rgb = getattr(ctx, "img_rgb", None)
    upscaled_size = tuple(img_rgb.shape[:2]) if img_rgb is not None else None
    if isinstance(base_img, Image.Image):
        return base_img.copy(), upscaled_size
    if img_rgb is not None:
        return Image.fromarray(img_rgb), None
    raise RuntimeError("No image buffer available for preview rendering")


def render_numbered_page_preview(
    ctx: Any,
    text_order: Sequence[int],
    *,
    max_size: int = 1024,
    fmt: str = "jpeg",
    quality: int = 82,
) -> Tuple[bytes, str, int, int]:
    """Render full-page preview with numbered bounding boxes drawn on CPU."""
    from manga_translator.translators.common import draw_text_boxes_on_image

    base_img, upscaled_size = _base_preview_image_and_upscaled_size(ctx)
    regions = list(getattr(ctx, "text_regions", None) or [])
    if regions and text_order:
        drawn = draw_text_boxes_on_image(base_img, regions, list(text_order), upscaled_size)
    else:
        drawn = base_img
    return encode_pil_preview(drawn, max_size=max_size, fmt=fmt, quality=quality)


def render_cropped_region_preview(
    ctx: Any,
    all_text_order: Sequence[int],
    region_ids: Sequence[int],
    *,
    max_size: int = 1536,
    fmt: str = "jpeg",
    quality: int = 88,
    pad_ratio: float = 0.25,
    min_pad_px: int = 64,
) -> Tuple[bytes, str, int, int]:
    """Render a cropped preview focused around the requested numbered bubble IDs."""
    import numpy as np
    from manga_translator.translators.common import draw_text_boxes_on_image

    base_img, upscaled_size = _base_preview_image_and_upscaled_size(ctx)
    regions = list(getattr(ctx, "text_regions", None) or [])
    if not regions or not all_text_order:
        return encode_pil_preview(base_img, max_size=max_size, fmt=fmt, quality=quality)

    requested_set = {int(rid) for rid in region_ids}
    selected_indices = [idx for idx, rid in enumerate(all_text_order) if rid in requested_set and idx < len(regions)]
    if not selected_indices:
        raise ValueError(f"Requested region_ids {sorted(requested_set)} not found on page")

    drawn = draw_text_boxes_on_image(base_img, regions, list(all_text_order), upscaled_size)

    w, h = drawn.size
    scale_x, scale_y = 1.0, 1.0
    if upscaled_size is not None and upscaled_size[0] > 0 and upscaled_size[1] > 0:
        scale_x = w / float(upscaled_size[1])
        scale_y = h / float(upscaled_size[0])

    xs1, ys1, xs2, ys2 = [], [], [], []
    for idx in selected_indices:
        region = regions[idx]
        if hasattr(region, "xyxy"):
            x1, y1, x2, y2 = region.xyxy
        elif hasattr(region, "min_rect"):
            pts = np.asarray(region.min_rect, dtype=np.float64).reshape(-1, 2)
            x1, y1 = float(pts[:, 0].min()), float(pts[:, 1].min())
            x2, y2 = float(pts[:, 0].max()), float(pts[:, 1].max())
        else:
            continue
        xs1.append(float(x1) * scale_x)
        ys1.append(float(y1) * scale_y)
        xs2.append(float(x2) * scale_x)
        ys2.append(float(y2) * scale_y)

    if not xs1:
        return encode_pil_preview(drawn, max_size=max_size, fmt=fmt, quality=quality)

    ux1, uy1, ux2, uy2 = min(xs1), min(ys1), max(xs2), max(ys2)
    box_w = max(1.0, ux2 - ux1)
    box_h = max(1.0, uy2 - uy1)
    pad_x = max(float(min_pad_px), box_w * pad_ratio)
    pad_y = max(float(min_pad_px), box_h * pad_ratio)

    crop_x1 = max(0, int(round(ux1 - pad_x)))
    crop_y1 = max(0, int(round(uy1 - pad_y)))
    crop_x2 = min(w, int(round(ux2 + pad_x)))
    crop_y2 = min(h, int(round(uy2 + pad_y)))
    if crop_x2 <= crop_x1 or crop_y2 <= crop_y1:
        cropped = drawn
    else:
        cropped = drawn.crop((crop_x1, crop_y1, crop_x2, crop_y2))

    return encode_pil_preview(cropped, max_size=max_size, fmt=fmt, quality=quality)


def write_preview_bytes_to_disk(
    raw_bytes: bytes,
    preview_dir: str,
    filename_stem: str,
    fmt: str,
) -> str:
    """Persist encoded preview bytes into preview_dir and return the absolute path."""
    os.makedirs(preview_dir, exist_ok=True)
    fmt_key = str(fmt or "jpeg").strip().lower()
    _pil_fmt, _mime, ext = _PREVIEW_MIME_BY_FORMAT.get(fmt_key, _PREVIEW_MIME_BY_FORMAT["jpeg"])
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename_stem).strip("_") or "preview"
    target = os.path.abspath(os.path.join(preview_dir, f"{safe_stem}{ext}"))
    with open(target, "wb") as handle:
        handle.write(raw_bytes)
    return target


def resolve_batch_image_delivery(
    encoded_pages: Dict[int, Tuple[bytes, str, int, int, str]],
    *,
    delivery: str,
    budget_mb: float,
    preview_dir: str,
    batch_id: str,
    fmt: str,
) -> Tuple[Dict[int, Dict[str, Any]], List[str]]:
    """Decide inline MCP ImageContent vs disk file_path per batch honoring budget."""
    mode = str(delivery or "auto").strip().lower()
    total_bytes = sum(len(item[0]) for item in encoded_pages.values())
    budget_bytes = max(1, int(float(budget_mb) * 1024 * 1024))

    use_file_path = False
    fallback_reason: Optional[str] = None
    if mode == "file_path":
        use_file_path = True
    elif mode == "auto" and total_bytes > budget_bytes:
        use_file_path = True
        fallback_reason = (
            f"Batch preview size ({total_bytes / (1024 * 1024):.2f} MB) exceeded "
            f"inline_image_budget_mb ({budget_mb:.2f} MB); wrote preview files to disk. "
            "Inspect preview_path using your local file/image viewing tool."
        )

    resolved: Dict[int, Dict[str, Any]] = {}
    hints: List[str] = []
    if fallback_reason:
        hints.append(fallback_reason)
    elif use_file_path and encoded_pages:
        hints.append(
            "Preview delivery is set to file_path; inspect preview_path using your local file/image viewing tool."
        )

    for page_index, (raw_bytes, mime_type, width, height, filename) in encoded_pages.items():
        if use_file_path:
            stem = f"{batch_id}_p{page_index}_{os.path.splitext(filename)[0]}"
            file_path = write_preview_bytes_to_disk(raw_bytes, preview_dir, stem, fmt)
            resolved[page_index] = {
                "mode": "file_path",
                "bytes": None,
                "file_path": file_path,
                "mime_type": mime_type,
                "width": width,
                "height": height,
            }
        else:
            resolved[page_index] = {
                "mode": "mcp_image",
                "bytes": raw_bytes,
                "file_path": None,
                "mime_type": mime_type,
                "width": width,
                "height": height,
            }

    return resolved, hints


async def validate_batch_translations(
    translator: Any,
    config: Any,
    pages_in_batch: Sequence[Any],
    submissions: Sequence[TranslationSubmissionItem | Dict[str, Any]],
    *,
    retry_count: int = 0,
) -> Tuple[bool, List[ValidationErrorItem], Dict[int, str]]:
    """Validate completeness, [BR] markers, repetition hallucinations, and target language ratio."""
    errors: List[ValidationErrorItem] = []

    expected_items: List[Tuple[int, Any, Any]] = []
    expected_ids: List[int] = []
    ignored_failed_ids: set[int] = set()
    for page in pages_in_batch:
        if getattr(page, "failed", False):
            ignored_failed_ids.update(getattr(page, "text_order", ()) or ())
            continue
        ctx = page.ctx
        regions = list(getattr(ctx, "text_regions", None) or [])
        for rid, region in zip(page.text_order, regions):
            expected_items.append((rid, region, page))
            expected_ids.append(rid)

    expected_set = set(expected_ids)
    submitted_map: Dict[int, str] = {}
    duplicate_ids: List[int] = []
    unknown_ids: List[int] = []

    for raw_item in submissions:
        if isinstance(raw_item, TranslationSubmissionItem):
            rid = int(raw_item.id)
            text_val = str(raw_item.translation)
        elif isinstance(raw_item, dict):
            if "id" not in raw_item or "translation" not in raw_item:
                errors.append(
                    ValidationErrorItem(
                        id=raw_item.get("id"),
                        reason="invalid_item_format",
                        detail="Each translation item must contain 'id' and 'translation'.",
                    )
                )
                continue
            rid = int(raw_item["id"])
            text_val = str(raw_item["translation"])
        else:
            errors.append(
                ValidationErrorItem(
                    id=None,
                    reason="invalid_item_format",
                    detail="Unsupported translation item type.",
                )
            )
            continue

        if rid in ignored_failed_ids:
            continue
        if rid not in expected_set:
            unknown_ids.append(rid)
            continue
        if rid in submitted_map:
            duplicate_ids.append(rid)
            continue
        submitted_map[rid] = text_val

    for rid in duplicate_ids:
        errors.append(
            ValidationErrorItem(
                id=rid,
                reason="duplicate_id",
                detail=f"Duplicate translation submitted for id={rid}.",
            )
        )
    for rid in unknown_ids:
        errors.append(
            ValidationErrorItem(
                id=rid,
                reason="unknown_id",
                detail=f"id={rid} does not belong to this batch.",
            )
        )

    missing_ids = [rid for rid in expected_ids if rid not in submitted_map]
    for rid in missing_ids:
        region = next(r for r_id, r, _p in expected_items if r_id == rid)
        raw_lines = getattr(region, "lines", None)
        region_count = max(int(len(raw_lines)), 1) if raw_lines is not None else 1
        errors.append(
            ValidationErrorItem(
                id=rid,
                reason="missing_id",
                original_region_count=region_count,
                detail=f"Missing translation for id={rid}.",
            )
        )

    if errors:
        return False, errors, {}

    if not expected_ids:
        return True, [], {}

    ordered_translations = [submitted_map[rid] for rid in expected_ids]
    ordered_queries = [str(getattr(region, "text", "") or "") for _rid, region, _p in expected_items]
    ordered_regions = [region for _rid, region, _p in expected_items]

    # Run _validate_br_markers on a synthetic context holding all regions of the batch
    # (this cleans extra [BR] on single-region items even when check_br_and_retry is False).
    helper = _get_prompt_br_helper()
    synthetic_ctx = SimpleNamespace(config=config, text_regions=ordered_regions)
    br_ok = helper._validate_br_markers(
        ordered_translations,
        queries=ordered_queries,
        ctx=synthetic_ctx,
    )
    cleaned_map = {rid: trans for rid, trans in zip(expected_ids, ordered_translations)}

    max_br_retries = max(1, int(getattr(config.translator, "post_check_max_retry_attempts", 3) or 3))
    if not br_ok:
        if retry_count < max_br_retries:
            for rid, region, _page in expected_items:
                raw_lines = getattr(region, "lines", None)
                region_count = max(int(len(raw_lines)), 1) if raw_lines is not None else 1
                trans_val = cleaned_map[rid]
                if region_count >= 2 and not re.search(r"(\[BR\]|【BR】|<br\s*/?>)", trans_val, flags=re.IGNORECASE):
                    errors.append(
                        ValidationErrorItem(
                            id=rid,
                            reason="missing_br_marker",
                            original_region_count=region_count,
                            detail=(
                                f"id={rid} has original_region_count={region_count} (>=2) and "
                                "requires explicit [BR] line break marker(s)."
                            ),
                        )
                    )
            if not errors:
                errors.append(
                    ValidationErrorItem(
                        id=None,
                        reason="missing_br_marker",
                        detail="Batch failed [BR] marker validation; ensure multi-line bubbles include [BR].",
                    )
                )
            return False, errors, cleaned_map
        logger.warning(
            f"[MCP] [BR] marker check failed after {retry_count} retry attempt(s) "
            f"(limit={max_br_retries}); accepting batch to prevent an infinite retry loop."
        )

    # Optional post-translation quality checks (gated by retry budget so a batch can't loop forever)
    enable_post_check = bool(getattr(config.translator, "enable_post_translation_check", False))
    max_retries = max(1, int(getattr(config.translator, "post_check_max_retry_attempts", 3) or 1))
    if enable_post_check and retry_count < max_retries:
        rep_threshold = int(getattr(config.translator, "post_check_repetition_threshold", 20) or 20)
        for rid, trans_val in cleaned_map.items():
            plain = re.sub(r"(\[BR\]|【BR】|<br\s*/?>)", " ", trans_val, flags=re.IGNORECASE)
            if await translator._check_repetition_hallucination(plain, rep_threshold, silent=False):
                errors.append(
                    ValidationErrorItem(
                        id=rid,
                        reason="repetition_hallucination",
                        detail=f"Detected repetition hallucination in translation for id={rid}.",
                    )
                )

        if len(ordered_regions) > 10:
            proxy_regions = [SimpleNamespace(translation=cleaned_map[rid]) for rid in expected_ids]
            lang_ok = await translator._check_target_language_ratio(
                proxy_regions,
                config.translator.target_lang,
                min_ratio=float(getattr(config.translator, "post_check_target_lang_threshold", 0.5) or 0.5),
            )
            if not lang_ok:
                errors.append(
                    ValidationErrorItem(
                        id=None,
                        reason="target_language_ratio_low",
                        detail=(
                            f"Batch translations failed target language ratio check for "
                            f"target_lang={config.translator.target_lang}."
                        ),
                    )
                )

    if errors:
        return False, errors, cleaned_map

    return True, [], cleaned_map


def _snapshot_page_regions(ctx: Any) -> Tuple[Optional[List[Any]], List[Tuple[Any, Dict[str, Any]]]]:
    regions = getattr(ctx, "text_regions", None)
    region_list = list(regions) if regions is not None else None
    states = [(region, dict(vars(region))) for region in (region_list or []) if hasattr(region, "__dict__")]
    return region_list, states


def _restore_page_regions(ctx: Any, snapshot: Tuple[Optional[List[Any]], List[Tuple[Any, Dict[str, Any]]]]) -> None:
    region_list, states = snapshot
    for region, state in states:
        region.__dict__.clear()
        region.__dict__.update(state)
    ctx.text_regions = region_list


async def apply_batch_post_processing(
    translator: Any,
    config: Any,
    pages_in_batch: Sequence[Any],
    id_to_translation: Dict[int, str],
    *,
    new_terms: Optional[List[Dict[str, Any]]] = None,
    prompt_file_path: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[List[int], List[Dict[str, Any]]]:
    """Apply validated translations, run post-processing/filtering, and merge glossary."""
    from manga_translator.translators.common import merge_glossary_to_file_with_delta

    # Disable translator's internal API retry inside _apply_post_translation_processing
    # so it NEVER calls external dispatch(); MCP handles validation retries via retry_needed.
    post_config = config.model_copy(deep=True)
    post_config.translator.enable_post_translation_check = False

    redo_pages: List[int] = []
    # The batch stays leased when this raises, and a resubmission pairs
    # page.text_order with ctx.text_regions by position. Leaving translated or
    # filtered regions behind would shift every later ID onto the wrong bubble,
    # so a failure restores each page touched so far.
    snapshots: List[Tuple[Any, Any]] = []
    try:
        for page in pages_in_batch:
            if cancel_event is not None and cancel_event.is_set():
                break
            if getattr(page, "failed", False):
                continue
            ctx = page.ctx
            regions = list(getattr(ctx, "text_regions", None) or [])
            if not regions:
                continue
            snapshots.append((ctx, _snapshot_page_regions(ctx)))

            initial_ids = getattr(ctx, "_initial_region_ids", None)
            if initial_ids is None:
                initial_ids = {id(r) for r in regions}
                ctx._initial_region_ids = initial_ids

            for rid, region in zip(page.text_order, regions):
                trans = id_to_translation.get(rid, "")
                if getattr(config.render, "uppercase", False):
                    trans = trans.upper()
                elif getattr(config.render, "lowercase", False):
                    trans = trans.lower()
                region.translation = trans
                region.target_lang = config.translator.target_lang
                region._alignment = config.render.alignment
                region._direction = config.render.direction

            ctx.text_regions = await translator._apply_post_translation_processing(ctx, post_config)
            ctx.text_regions = translator._filter_translated_regions(ctx, config, force_filter=True)

            final_ids = {id(r) for r in (ctx.text_regions or [])}
            if initial_ids - final_ids:
                redo_pages.append(page.page_index)

        merged_terms: List[Dict[str, Any]] = []
        if (
            (cancel_event is None or not cancel_event.is_set())
            and new_terms
            and bool(getattr(config.translator, "extract_glossary", False))
            and prompt_file_path
        ):
            with _GLOSSARY_MERGE_LOCK:
                if cancel_event is None or not cancel_event.is_set():
                    glossary_modified, merged_terms = merge_glossary_to_file_with_delta(
                        prompt_file_path, new_terms
                    )
                    if not glossary_modified:
                        merged_terms = []
    except BaseException:
        for ctx, snapshot in reversed(snapshots):
            _restore_page_regions(ctx, snapshot)
        raise

    return redo_pages, merged_terms


async def run_page_inpaint(
    translator: Any,
    config: Any,
    ctx: Any,
    *,
    is_redo: bool = False,
) -> None:
    """Run mask refinement and local inpainting for one page."""
    import numpy as np
    from manga_translator.utils.generic import get_image_md5

    if hasattr(ctx, "input") and ctx.input is not None:
        image_md5 = get_image_md5(ctx.input)
        if not translator._restore_image_context(image_md5):
            translator._set_image_context(config, ctx.input)

    if is_redo:
        # Clear previous mask so mask refinement regenerates strictly for remaining regions.
        ctx.mask = None

    if not getattr(ctx, "text_regions", None):
        ctx.img_inpainted = ctx.img_rgb
        return

    if getattr(ctx, "mask", None) is None:
        ctx.mask = await translator._run_mask_refinement(config, ctx)

    if ctx.mask is None or not np.any(ctx.mask):
        ctx.img_inpainted = ctx.img_rgb
        return

    ctx.img_inpainted = await translator._run_inpainting(config, ctx)


async def render_and_save_page(
    translator: Any,
    config: Any,
    ctx: Any,
    save_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Render translated text on the dedicated RenderWorker thread and save outputs."""
    import numpy as np
    from manga_translator.utils.generic import dump_image, get_image_md5

    image_name = getattr(ctx, "image_name", "") or ""
    if getattr(ctx, "translation_error", None):
        translator._cleanup_context_memory(ctx, keep_result=False)
        return {
            "image_name": image_name,
            "output_path": None,
            "success": False,
            "skipped": False,
            "skip_reason": None,
            "error": str(ctx.translation_error),
        }

    if getattr(ctx, "img_rgb", None) is None:
        err = RuntimeError("Missing preprocessed RGB image buffer for rendering")
        translator._mark_context_failure(ctx, err, stage="rendering")
        translator._cleanup_context_memory(ctx, keep_result=False)
        return {
            "image_name": image_name,
            "output_path": None,
            "success": False,
            "skipped": False,
            "skip_reason": None,
            "error": str(ctx.translation_error),
        }

    if hasattr(ctx, "input") and ctx.input is not None:
        image_md5 = get_image_md5(ctx.input)
        if not translator._restore_image_context(image_md5):
            translator._set_image_context(config, ctx.input)

    inpainted_snapshot = None
    if (
        (getattr(translator, "save_text", False) or getattr(translator, "text_output_file", None))
        and getattr(ctx, "img_inpainted", None) is not None
        and getattr(ctx, "mask", None) is not None
        and np.any(ctx.mask)
    ):
        inpainted_snapshot = np.copy(ctx.img_inpainted)

    loaded_overlays = getattr(ctx, "_mcp_loaded_render_overlays", None)
    pending_paste = getattr(ctx, "_mcp_pending_paste_overlays", None)
    has_overlays = bool(loaded_overlays or pending_paste)
    if has_overlays and hasattr(translator, "_compose_render_overlays_on_inpainted"):
        with _OVERLAY_STATE_LOCK:
            translator._loaded_render_overlays = loaded_overlays
            translator._pending_paste_overlays = pending_paste
            try:
                translator._compose_render_overlays_on_inpainted(ctx)
            finally:
                translator._loaded_render_overlays = None
                translator._pending_paste_overlays = None
    ctx._mcp_loaded_render_overlays = None
    ctx._mcp_pending_paste_overlays = None

    if not getattr(ctx, "text_regions", None):
        if has_overlays:
            if getattr(ctx, "img_inpainted", None) is not None:
                ctx.result = dump_image(ctx.input, ctx.img_inpainted, getattr(ctx, "img_alpha", None))
            elif getattr(ctx, "upscaled", None) is not None and hasattr(ctx.upscaled, "copy"):
                ctx.result = ctx.upscaled.copy()
            else:
                ctx.result = dump_image(ctx.input, ctx.img_rgb, getattr(ctx, "img_alpha", None))
        else:
            ctx.result = dump_image(ctx.input, ctx.img_rgb, getattr(ctx, "img_alpha", None))
    else:
        render_config = config.model_copy(deep=False) if hasattr(config, "model_copy") else config
        ctx.img_rendered = await translator._run_text_rendering(
            render_config,
            ctx,
            skip_font_scaling=bool(getattr(ctx, "skip_font_scaling", False)),
        )
        ctx.result = dump_image(
            ctx.input,
            ctx.img_rendered,
            getattr(ctx, "img_alpha", None),
            mask=getattr(ctx, "mask", None),
            render_alpha=getattr(ctx, "img_render_alpha", None),
        )

    ctx = await translator._revert_upscale(config, ctx)

    if inpainted_snapshot is not None and image_name:
        try:
            saved_inpainted = translator._save_inpainted_image(image_name, inpainted_snapshot)
            if saved_inpainted is None:
                raise OSError(f"Failed to write inpainted sidecar image: {image_name}")
        finally:
            inpainted_snapshot = None

    if save_info and getattr(ctx, "result", None) is not None:
        translator._save_and_cleanup_context(ctx, save_info, config, "MCP")
    elif getattr(ctx, "result", None) is not None:
        ctx.success = True

    if (
        translator._should_write_text_sidecar(ctx)
        and (getattr(translator, "save_text", False) or getattr(translator, "text_output_file", None))
        and getattr(ctx, "text_regions", None) is not None
        and image_name
    ):
        parse_failures = int(getattr(ctx, "load_text_parse_failures", 0) or 0)
        if parse_failures > 0:
            logger.error(
                f"{parse_failures} region(s) failed to parse for "
                f"{os.path.basename(image_name)}; skipped JSON write-back to protect the project file"
            )
        else:
            translator._save_text_to_file(image_name, ctx, config)

    result_payload = {
        "image_name": image_name,
        "output_path": getattr(ctx, "output_path", None),
        "success": bool(getattr(ctx, "success", False)),
        "skipped": bool(getattr(ctx, "skipped", False)),
        "skip_reason": getattr(ctx, "skip_reason", None),
        "error": getattr(ctx, "translation_error", None) or getattr(ctx, "error", None),
    }
    translator._cleanup_context_memory(ctx, keep_result=False)
    return result_payload
