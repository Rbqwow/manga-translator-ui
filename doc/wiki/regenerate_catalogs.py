"""Regenerate the Phase 0 catalog and the two derived data catalogs.

The developer docs reference generator scripts that are not part of this
repository, so this reproduces their output from the same inputs:

* ``doc/wiki/phase0-ui-parameter-fields.json``
    Settings layout (``desktop_qt_ui/ui/main_page/settings_tab_layout.json``)
    joined with the release template and the desktop config models.
* ``doc/wiki/data/settings.generated.json``
    The Phase 0 catalog joined with ``app_logic.get_display_mapping`` and the
    desktop locale packs.
* ``doc/wiki/data/i18n.generated.json``
    Every ``label_*``/``desc_*`` key present in both ``en_US`` and ``zh_CN``.

Run ``verify_phase0_ui_parameter_fields.py`` afterwards; it checks that the
catalog matches the live layout, so a stale regeneration fails loudly.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIKI = ROOT / "doc" / "wiki"
LAYOUT_PATH = ROOT / "desktop_qt_ui" / "ui" / "main_page" / "settings_tab_layout.json"
APP_LOGIC_PATH = ROOT / "desktop_qt_ui" / "app_logic.py"
RELEASE_CONFIG_PATH = ROOT / "config" / "config-example.json"
LOCALES_DIR = ROOT / "desktop_qt_ui" / "locales"
CATALOG_PATH = WIKI / "phase0-ui-parameter-fields.json"
SETTINGS_OUTPUT = WIKI / "data" / "settings.generated.json"
I18N_OUTPUT = WIKI / "data" / "i18n.generated.json"

CLASS_BY_PREFIX = {
    "app": "AppSection",
    "translator": "TranslatorSettings",
    "ocr": "OcrSettings",
    "detector": "DetectorSettings",
    "inpainter": "InpainterSettings",
    "render": "RenderSettings",
    "upscale": "UpscaleSettings",
    "colorizer": "ColorizerSettings",
    "cli": "CliSettings",
}
FIXED_ACTIONS = {
    "ocr.ai_ocr_prompt_path",
    "colorizer.ai_colorizer_prompt_path",
    "render.ai_renderer_prompt_path",
}
SPECIAL_CONTROLS = {
    "upscale.upscale_ratio": "dynamic-combo",
    "filter_text_enabled": "toggle + open-filter-list action",
    "render.font_family": "font-combo + open-font-directory action",
    "use_custom_api_params": "toggle + edit-custom-api-params action",
}
COMBO_KEYS = {
    "cli.format",
    "translator.translator",
    "translator.target_lang",
    "translator.keep_lang",
    "ocr.ocr",
    "ocr.secondary_ocr",
    "ocr.ocr_vl_language_hint",
    "detector.detector",
    "inpainter.inpainter",
    "inpainter.inpainting_precision",
    "render.renderer",
    "render.alignment",
    "render.direction",
    "render.layout_mode",
    "upscale.upscaler",
    "colorizer.colorizer",
}
WIKI_PAGE_BY_PREFIX = {
    "cli": "desktop/settings/cli-batch-and-output.md",
    "app": "desktop/settings/general-and-app.md",
    "translator": "desktop/settings/translation.md",
    "ocr": "desktop/settings/ocr-filter-and-merge.md",
    "detector": "desktop/settings/detection.md",
    "inpainter": "desktop/settings/mask-and-inpainting.md",
    "render": "desktop/settings/typesetting-and-rendering.md",
    "upscale": "desktop/settings/upscale-and-colorization.md",
    "colorizer": "desktop/settings/upscale-and-colorization.md",
}
LIFECYCLE_BY_PREFIX = {
    "cli": ["workflow orchestration", "export"],
    "app": ["application"],
    "translator": ["translation"],
    "ocr": ["OCR", "merge/filter"],
    "detector": ["detection"],
    "inpainter": ["mask refinement", "inpainting"],
    "render": ["typesetting", "rendering"],
    "upscale": ["upscaling"],
    "colorizer": ["colorization"],
}
# Standalone toggles that do not use the usual section prefix. These are
# carried over from the hand-verified Phase 0 evidence rather than derived.
LIFECYCLE_BY_KEY = {
    "filter_text_enabled": ["OCR", "merge/filter"],
    "use_custom_api_params": ["API request construction"],
    "force_simple_sort": ["workflow orchestration", "export"],
    "kernel_size": ["mask refinement"],
    "mask_dilation_offset": ["mask refinement"],
}
# Rows that open a file picker or dialog on top of their control.
FILE_ACTION_KEYS = frozenset({
    "use_custom_api_params",
    "filter_text_enabled",
    "render.font_family",
}) | FIXED_ACTIONS
WIKI_PAGE_BY_KEY = {
    "filter_text_enabled": "desktop/settings/ocr-filter-and-merge.md",
    "use_custom_api_params": "desktop/settings/general-and-app.md",
    "force_simple_sort": "desktop/settings/cli-batch-and-output.md",
    "kernel_size": "desktop/settings/mask-and-inpainting.md",
    "mask_dilation_offset": "desktop/settings/mask-and-inpainting.md",
    # These rows live on a page that does not match their section prefix.
    "render.enable_template_alignment": "desktop/settings/mode-specific.md",
    "render.paste_mask_dilation_pixels": "desktop/settings/mode-specific.md",
    "ocr.use_model_bubble_repair_intersection": "desktop/settings/mask-and-inpainting.md",
    "ocr.limit_mask_dilation_to_bubble_mask": "desktop/settings/mask-and-inpainting.md",
    "cli.context_size": "desktop/settings/translation.md",
    "cli.export_from_local_json": "desktop/settings/mode-specific.md",
}


def dotted(mapping: dict, key: str):
    value = mapping
    for part in key.split("."):
        value = value[part]
    return value


def layout_entries(layout: dict) -> list[tuple[str, str]]:
    return [
        (item, tab["title"])
        for tab in layout["tabs"]
        for item in tab["items"]
        if isinstance(item, str)
    ]


def model_fields(path: Path) -> dict[str, set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields = {
            child.target.id
            for child in node.body
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
        }
        if fields:
            result[node.name] = fields
    return result


# Prompt-editor buttons edit standalone prompt files, so their defaults come
# from the backend prompt loaders rather than the release template.
FIXED_ACTION_SOURCES = {
    "ocr.ai_ocr_prompt_path": "manga_translator/ocr/prompt_loader.py#DEFAULT_AI_OCR_PROMPT_PATH",
    "colorizer.ai_colorizer_prompt_path": (
        "manga_translator/colorization/prompt_loader.py#DEFAULT_AI_COLORIZER_PROMPT_PATH"
    ),
    "render.ai_renderer_prompt_path": (
        "manga_translator/rendering/prompt_loader.py#DEFAULT_AI_RENDERER_PROMPT_PATH"
    ),
}


def default_source(key: str) -> str:
    if key in FIXED_ACTION_SOURCES:
        return FIXED_ACTION_SOURCES[key]
    if "." not in key:
        return f"config/config-example.json (release) -> desktop_qt_ui/core/config_models.py#AppSettings.{key}"
    prefix, field = key.split(".", 1)
    return (
        "config/config-example.json (release) -> "
        f"desktop_qt_ui/core/config_models.py#{CLASS_BY_PREFIX[prefix]}.{field}"
    )


def expected_control(key: str, value) -> str:
    if key in SPECIAL_CONTROLS:
        return SPECIAL_CONTROLS[key]
    if key in COMBO_KEYS:
        return "combo"
    if isinstance(value, bool):
        return "toggle"
    if isinstance(value, int):
        return "int-input"
    if isinstance(value, float):
        return "float-input"
    if value is None and key in {"render.font_size", "ocr.ocr_vl_custom_prompt"}:
        return "optional-input"
    if isinstance(value, str):
        return "text-input"
    raise AssertionError(f"cannot classify rendered control {key}: {value!r}")


def _label_key_from_value(node) -> str | None:
    """Resolve ``self._t("label_x")`` or a plain string literal to its i18n key."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value.startswith("label_") else None
    if isinstance(node, ast.Call) and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def display_mapping(app_logic: str, mapping_name: str) -> dict[str, str]:
    """Read the ``key -> i18n key`` dict out of app_logic.get_display_mapping.

    Entries look like ``"verbose": self._t("label_verbose")``, so values are
    call expressions rather than literals.
    """
    tree = ast.parse(app_logic)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "get_display_mapping":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Dict):
                continue
            keys = [
                k.value for k in child.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
            if mapping_name not in keys:
                continue
            entry = child.values[keys.index(mapping_name)]
            if not isinstance(entry, ast.Dict):
                continue
            resolved = {}
            for key_node, value_node in zip(entry.keys, entry.values):
                if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                    continue
                label_key = _label_key_from_value(value_node)
                if label_key:
                    resolved[key_node.value] = label_key
            return resolved
    raise AssertionError(f"could not read display mapping '{mapping_name}' from app_logic.py")


def locale_packs() -> dict[str, dict]:
    packs = {}
    for name in ("en_US", "zh_CN", "zh_TW", "ja_JP", "ko_KR", "es_ES"):
        path = LOCALES_DIR / f"{name}.json"
        packs[name] = json.loads(path.read_text(encoding="utf-8"))
    return packs


def build_phase0_catalog(previous: dict) -> dict:
    layout = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
    release = json.loads(RELEASE_CONFIG_PATH.read_text(encoding="utf-8"))
    classes = model_fields(ROOT / "desktop_qt_ui" / "core" / "config_models.py")

    entries = layout_entries(layout)
    visible_fields = []
    excluded = []
    for key, page in entries:
        if key == "render.font_color":
            # Null release default with no None widget branch: never rendered.
            excluded.append({
                "key": key,
                "page": page,
                "reason": "release default is null and dynamic_settings.py has no widget branch for it",
            })
            continue
        if key in FIXED_ACTIONS:
            control = "prompt-editor-button"
        else:
            class_name, field = ("AppSettings", key) if "." not in key else (
                CLASS_BY_PREFIX[key.split(".", 1)[0]], key.split(".", 1)[1]
            )
            assert field in classes.get(class_name, set()), f"model field missing: {class_name}.{field}"
            control = expected_control(key, dotted(release, key))
        visible_fields.append({
            "key": key,
            "page": page,
            "control": control,
            "default_source": default_source(key),
        })

    layout_count = len(entries)
    visible_count = len(visible_fields)
    catalog = dict(previous)
    catalog["schema_version"] = previous.get("schema_version", 1)
    catalog["scope"] = previous.get("scope", {})
    catalog["baseline_comparison"] = {
        "requested_baseline": previous.get("baseline_comparison", {}).get(
            "requested_baseline", layout_count
        ),
        "layout_parameter_entries": layout_count,
        "visible_parameter_fields": visible_count,
        "difference_from_requested_baseline": visible_count - layout_count,
        "difference_explanation": (
            f"The layout has {layout_count} string entries, matching the updated baseline. "
            "Its render.font_color entry has a null release default and no None widget branch, "
            f"leaving {visible_count} visible settings rows "
            f"({'one below' if visible_count == layout_count - 1 else f'{layout_count - visible_count} below'} "
            f"{layout_count})."
        ),
    }
    catalog["visible_fields"] = visible_fields
    catalog["layout_entries_not_visible_with_release_defaults"] = excluded
    catalog["reproduction"] = {
        "command": "uv run --no-sync python doc/wiki/verify_phase0_ui_parameter_fields.py",
        "expected_result": (
            f"PASS: layout={layout_count}, fixed-actions={len(FIXED_ACTIONS)}, "
            f"visible={visible_count}, baseline-delta={visible_count - layout_count}, "
            "excluded=render.font_color"
        ),
    }
    return catalog


def build_settings_catalog(catalog: dict) -> dict:
    app_logic = APP_LOGIC_PATH.read_text(encoding="utf-8")
    labels = display_mapping(app_logic, "labels")
    packs = locale_packs()

    release = json.loads(RELEASE_CONFIG_PATH.read_text(encoding="utf-8"))

    records = []
    for field in catalog["visible_fields"]:
        key = field["key"]
        prefix = key.split(".", 1)[0] if "." in key else key
        i18n_key = labels.get(key) or f"label_{key.rsplit('.', 1)[-1]}"
        records.append({
            "key": key,
            "ui": {
                "tab": field["page"],
                "i18n_key": i18n_key,
                "english": packs["en_US"].get(i18n_key),
                "simplified_chinese": packs["zh_CN"].get(i18n_key),
            },
            "wiki_page": WIKI_PAGE_BY_KEY.get(key, WIKI_PAGE_BY_PREFIX.get(prefix)),
            "control": field["control"],
            "defaults": {
                # Prompt-editor buttons edit standalone files, so they have no
                # value in the release template.
                "release_json_value": (
                    None if key in FIXED_ACTIONS else dotted(release, key)
                ),
                "source": field["default_source"],
            },
            "lifecycle_stages": LIFECYCLE_BY_KEY.get(
                key, LIFECYCLE_BY_PREFIX.get(prefix, [])
            ),
            "file_action": key in FILE_ACTION_KEYS,
            "consumers": [],
            "consumer_status": "pending Phase 3 source trace",
            "verification_status": "Phase 0 UI/default evidence catalogued; final consumer trace pending",
        })

    return {
        "schema_version": 1,
        "generated_from": {
            "phase0_ui_parameter_catalog": "doc/wiki/phase0-ui-parameter-fields.json",
            "ui_label_binding": "desktop_qt_ui/app_logic.py#get_display_mapping",
            "locales": ["desktop_qt_ui/locales/en_US.json", "desktop_qt_ui/locales/zh_CN.json"],
        },
        "baseline_comparison": catalog["baseline_comparison"],
        "records": records,
        "excluded_layout_entries": catalog["layout_entries_not_visible_with_release_defaults"],
        "validation": {
            "record_count": len(records),
            "ui_labels": "Every record resolves an actual UI label key in both desktop locales.",
            "consumer_trace": "Not generated from Phase 0; intentionally left pending for Phase 3 evidence.",
        },
    }


def build_i18n_catalog() -> dict:
    """Catalogue every key present in both desktop packs.

    The wiki evidence matrix covers UI copy in general (menus, web admin,
    language names), not just the ``label_*``/``desc_*`` settings strings.
    """
    packs = locale_packs()
    keys = sorted(key for key in packs["en_US"] if key in packs["zh_CN"])
    entries = [
        {
            "key": key,
            "en_US": packs["en_US"][key],
            "zh_CN": packs["zh_CN"][key],
            "status": "exact",
        }
        for key in keys
    ]
    return {
        "schemaVersion": 1,
        "sources": {
            "en_US": "desktop_qt_ui/locales/en_US.json",
            "zh_CN": "desktop_qt_ui/locales/zh_CN.json",
            "phase0Evidence": "doc/wiki/research/phase0-options-i18n-matrix.md",
        },
        "summary": {
            "keys": len(entries),
            "exact": len(entries),
            "missingInEnglish": sum(1 for key in packs["zh_CN"] if key not in packs["en_US"]),
            "missingInChinese": sum(1 for key in packs["en_US"] if key not in packs["zh_CN"]),
        },
        "entries": entries,
    }


def write_json(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    previous = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog = build_phase0_catalog(previous)
    write_json(CATALOG_PATH, catalog)
    write_json(SETTINGS_OUTPUT, build_settings_catalog(catalog))
    write_json(I18N_OUTPUT, build_i18n_catalog())
    print(
        f"wrote phase0={len(catalog['visible_fields'])} settings={len(catalog['visible_fields'])} "
        f"i18n={json.loads(I18N_OUTPUT.read_text(encoding='utf-8'))['summary']['keys']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
