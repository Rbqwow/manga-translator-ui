import _bootstrap  # noqa: F401, I001

import asyncio
import json
import io
import os
import sys
import tempfile
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageCms
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from app_logic import MainAppLogic
from core.config_models import CliSettings
from manga_translator.config import CliConfig
from manga_translator.image_formats import LOSSLESS_SWITCHABLE_PIL_FORMATS
from manga_translator.manga_translator import MangaTranslator
from manga_translator.save import save_result
from manga_translator.utils import Context
from manga_translator.utils.generic import save_pil_image
from services.export_service import ExportService
from ui.main_page import dynamic_settings
from ui.main_page.view import MainView


def test_lossless_switchable_formats_constant():
    assert "WEBP" in LOSSLESS_SWITCHABLE_PIL_FORMATS
    assert "AVIF" in LOSSLESS_SWITCHABLE_PIL_FORMATS
    assert "HEIF" in LOSSLESS_SWITCHABLE_PIL_FORMATS
    assert "PNG" not in LOSSLESS_SWITCHABLE_PIL_FORMATS
    assert "JPEG" not in LOSSLESS_SWITCHABLE_PIL_FORMATS


def test_save_pil_image_webp_lossless():
    np.random.seed(42)
    arr = np.random.randint(0, 256, (32, 32, 3), dtype=np.uint8)
    img = Image.fromarray(arr, mode="RGB")

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Lossless save
        lossless_path = os.path.join(tmpdir, "test_lossless.webp")
        save_pil_image(img, lossless_path, lossless=True, quality=100)
        with open(lossless_path, "rb") as f:
            header = f.read(16)
        assert b"VP8L" in header, "Lossless WebP header must contain VP8L chunk"

        # Check bit-exact round-trip
        with Image.open(lossless_path) as reloaded:
            reloaded_arr = np.array(reloaded.convert("RGB"))
        assert np.array_equal(arr, reloaded_arr), "Lossless WebP must preserve exact pixel values"

        # 2. Lossy save (default or lossless=False)
        lossy_path = os.path.join(tmpdir, "test_lossy.webp")
        save_pil_image(img, lossy_path, lossless=False, quality=100)
        with open(lossy_path, "rb") as f:
            lossy_header = f.read(16)
        assert b"VP8 " in lossy_header, "Lossy WebP header must contain VP8 chunk"


def test_save_result_respects_context_lossless():
    np.random.seed(123)
    arr = np.random.randint(0, 256, (20, 20, 3), dtype=np.uint8)
    img = Image.fromarray(arr, mode="RGB")

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_lossless = os.path.join(tmpdir, "out_lossless.webp")
        ctx_lossless = Context()
        ctx_lossless.save_quality = 100
        ctx_lossless.save_lossless = True
        save_result(img, dest_lossless, ctx_lossless)

        with open(dest_lossless, "rb") as f:
            header = f.read(16)
        assert b"VP8L" in header

        dest_lossy = os.path.join(tmpdir, "out_lossy.webp")
        ctx_lossy = Context()
        ctx_lossy.save_quality = 100
        ctx_lossy.save_lossless = False
        save_result(img, dest_lossy, ctx_lossy)

        with open(dest_lossy, "rb") as f:
            header_lossy = f.read(16)
        assert b"VP8 " in header_lossy


def test_config_models_save_lossless_and_effort_default():
    # The release template is the product default; keep the models on it so a
    # new install and the CLI do not encode every page differently.
    cli_cfg = CliConfig()
    assert cli_cfg.save_lossless is True
    assert cli_cfg.compression_effort == 100

    cli_settings = CliSettings()
    assert cli_settings.save_lossless is True
    assert cli_settings.compression_effort == 100


def test_manga_translator_parse_save_lossless_and_effort():
    translator = MangaTranslator()
    translator.parse_init_params({"save_lossless": True, "save_quality": 90, "compression_effort": 85})
    assert translator.save_lossless is True
    assert translator.save_quality == 90
    assert translator.compression_effort == 85

    translator2 = MangaTranslator()
    translator2.parse_init_params({})
    assert translator2.save_lossless is False
    assert translator2.save_quality == 100
    assert translator2.compression_effort == 100


def test_save_pil_image_compression_effort():
    img = Image.new("RGB", (20, 20), color=(120, 80, 40))
    with tempfile.TemporaryDirectory() as tmpdir:
        webp_path = os.path.join(tmpdir, "out_effort.webp")
        save_pil_image(img, webp_path, lossless=True, compression_effort=80)
        with open(webp_path, "rb") as f:
            assert b"VP8L" in f.read(16)

        png_path = os.path.join(tmpdir, "out_effort.png")
        save_pil_image(img, png_path, compression_effort=90)
        assert os.path.exists(png_path)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


class _SettingsController:
    get_display_mapping = MainAppLogic.get_display_mapping
    get_options_for_key = MainAppLogic.get_options_for_key

    def __init__(self, locale):
        catalog = json.loads(
            (_bootstrap.ROOT / "desktop_qt_ui" / "locales" / f"{locale}.json").read_text(encoding="utf-8")
        )
        self._t = lambda key: catalog.get(key, key)
        self.translation_service = SimpleNamespace(
            get_target_languages=lambda: {}, get_keep_languages=lambda: {}
        )


@pytest.fixture
def settings_view(qt_app):
    resources = []

    def make_view(fmt="不指定", lossless=False, locale="en_US"):
        controller = _SettingsController(locale)
        config = {"cli": {
            "format": fmt,
            "save_lossless": lossless,
            "save_quality": 90,
            "compression_effort": 70,
        }}
        view = MainView.__new__(MainView)
        QObject.__init__(view)
        view.controller = controller
        view._t = controller._t
        view.config_service = SimpleNamespace(get_config=lambda: config)
        view._settings_value_bindings = {}
        view._settings_row_widgets = {}
        panel = QWidget()
        layout = QVBoxLayout(panel)
        view._create_param_widgets(config["cli"], layout, "cli")
        view._update_image_format_settings_visibility()

        def save_setting(key, value):
            config["cli"][key.split(".")[-1]] = value

        view.setting_changed.connect(save_setting)
        resources.append((view, panel))
        return view, panel, config

    yield make_view
    for view, panel in resources:
        panel.deleteLater()
        view.deleteLater()
    qt_app.processEvents()


@pytest.mark.parametrize("lossless", [False, True])
@pytest.mark.parametrize("fmt", ["jpg", "jpeg", "jfif", "webp", "png", "bmp", "tiff", "tif", "heic", "heif", "avif", "不指定"])
def test_dynamic_settings_visibility_matrix(settings_view, fmt, lossless):
    view, _panel, _config = settings_view(fmt, lossless)
    switchable = (True, not lossless, lossless)
    expected = {
        "jpg": (False, True, False), "jpeg": (False, True, False),
        "jfif": (False, True, False), "avif": switchable,
        "webp": switchable, "heic": switchable, "heif": switchable,
        "png": (False, False, True), "bmp": (False, False, False),
        "tiff": (False, False, False), "tif": (False, False, False),
        "不指定": (True, True, True),
    }[fmt]
    assert not view._settings_row_widgets["cli.format"].isHidden()
    actual = tuple(
        not view._settings_row_widgets[f"cli.{key}"].isHidden()
        for key in ("save_lossless", "save_quality", "compression_effort")
    )
    assert actual == expected


@pytest.mark.parametrize("locale", ["zh_CN", "zh_TW", "en_US", "ja_JP", "ko_KR", "es_ES"])
def test_format_change_keeps_canonical_value_and_updates_visibility(settings_view, locale):
    view, _panel, config = settings_view(locale=locale)
    combo = view._settings_value_bindings["cli.format"][0]
    toggle = view._settings_value_bindings["cli.save_lossless"][0]
    assert combo.currentText() == view._t("format_not_specified")
    assert view._get_live_setting_value("cli.format") == "不指定"

    combo.setCurrentText("webp")
    toggle.setChecked(True)
    assert config["cli"]["format"] == "webp"
    assert config["cli"]["save_lossless"] is True
    assert view._settings_row_widgets["cli.save_quality"].isHidden()
    assert not view._settings_row_widgets["cli.compression_effort"].isHidden()

    combo.setCurrentText("avif")
    assert not view._settings_row_widgets["cli.save_lossless"].isHidden()
    assert view._settings_row_widgets["cli.save_quality"].isHidden()
    assert not view._settings_row_widgets["cli.compression_effort"].isHidden()
    toggle.setChecked(False)
    assert not view._settings_row_widgets["cli.save_quality"].isHidden()
    assert view._settings_row_widgets["cli.compression_effort"].isHidden()

    combo.setCurrentText(view._t("format_not_specified"))
    assert config["cli"]["format"] == "不指定"
    assert all(not row.isHidden() for row in view._settings_row_widgets.values())


@pytest.mark.parametrize("fmt", ["", "none", None, "不指定"])
def test_loaded_format_alias_resets_existing_combo(settings_view, fmt):
    view, _panel, config = settings_view("webp", True)
    config["cli"]["format"] = fmt
    assert dynamic_settings._sync_setting_widget_values(view, config)
    assert view._get_live_setting_value("cli.format") == "不指定"
    assert all(not row.isHidden() for row in view._settings_row_widgets.values())


def test_settings_initialization_completes_with_bound_visibility_methods(settings_view, monkeypatch):
    view, panel, config = settings_view("png")
    view._settings_tabs_use_reclassify = True
    view.settings_tab_layout = [{"id": "general", "items": [f"cli.{key}" for key in config["cli"]]}]
    view.tab_frames = {"general": panel}
    for attr in ("env_preset_layout", "env_group_container_layout", "ocr_container_layout", "color_container_layout", "render_container_layout"):
        setattr(view, attr, QVBoxLayout())
    view.controller.config_service = SimpleNamespace(get_current_preset=lambda: "")
    for attr in ("_refresh_preset_list", "_refresh_api_feature_selectors", "_refresh_prompt_manager", "_on_add_preset_clicked", "_on_delete_preset_clicked", "_on_preset_changed"):
        setattr(view, attr, lambda *_args: None)
    monkeypatch.setattr(dynamic_settings, "_refresh_env_api_groups", lambda *_args, **_kwargs: None)

    view.set_parameters(config)
    assert view._settings_ui_ready is True
    assert not view._settings_row_widgets["cli.compression_effort"].isHidden()


@pytest.mark.parametrize("text, expected", [("", 100), (" ", 100), ("invalid", 100), ("0", 0), ("-1", 0), ("101", 100)])
def test_compression_input_recovers_default_and_preserves_zero(settings_view, text, expected):
    view, _panel, config = settings_view("png")
    edit = view._settings_value_bindings["cli.compression_effort"][0]
    edit.setText(text)
    edit.editingFinished.emit()
    assert config["cli"]["compression_effort"] == expected
    assert dynamic_settings._sync_setting_widget_values(view, config)
    assert edit.text() == str(expected)
    CliSettings.model_validate(config["cli"])


@pytest.mark.parametrize("effort", [None, "", "invalid", 0])
@pytest.mark.parametrize("extension", ["png", "jpg", "webp", "avif"])
def test_editor_export_handles_empty_compression_effort(tmp_path, effort, extension):
    config = {"cli": {"compression_effort": effort, "save_lossless": True}}
    source = tmp_path / f"source.{extension}"
    output = tmp_path / f"output.{extension}"
    service = ExportService()
    with Image.new("RGB", (32, 32), (120, 80, 40)) as image:
        image.save(source)
        service._save_rendered_image(image, str(output), config)
        inpainted = service.save_inpainted_image(str(source), image, config)
        for path in (output, inpainted):
            with Image.open(path) as decoded:
                decoded.load()
                assert decoded.size == image.size
    assert not output.with_suffix(output.suffix + ".tmp").exists()


def test_zero_effort_is_used_by_editor_png_encoder(tmp_path, monkeypatch):
    options = []
    original_save = Image.Image.save

    def capture_save(image, fp, format=None, **kwargs):
        options.append(kwargs)
        return original_save(image, fp, format=format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", capture_save)
    with Image.new("RGB", (32, 32)) as image:
        ExportService()._save_rendered_image(
            image, str(tmp_path / "output.png"), {"cli": {"compression_effort": 0}}
        )
    assert options[0]["compress_level"] == 0


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
@pytest.mark.parametrize("quality", [0, 90, 100])
def test_heif_lossless_preserves_pixels_at_different_efforts(tmp_path, mode, quality):
    pillow_heif = pytest.importorskip("pillow_heif")
    if not pillow_heif.libheif_info().get("HEIF"):
        pytest.skip("HEIF encoder unavailable")
    channels = 4 if mode == "RGBA" else 3
    pixels = np.random.default_rng(1234).integers(0, 256, (32, 32, channels), dtype=np.uint8)
    encoded = []
    with Image.fromarray(pixels) as image:
        for effort in (0, 70, 100):
            path = tmp_path / f"output-{effort}.heic"
            save_pil_image(image, str(path), lossless=True, quality=quality, compression_effort=effort)
            with Image.open(path) as decoded:
                assert np.array_equal(pixels, np.asarray(decoded.convert(mode)))
            encoded.append(path.read_bytes())
    assert encoded[0] != encoded[-1], "HEIF effort must affect the actual encoder output"


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
@pytest.mark.parametrize("quality", [0, 90, 100])
def test_avif_lossless_preserves_pixels_at_different_efforts(tmp_path, mode, quality):
    channels = 4 if mode == "RGBA" else 3
    pixels = np.random.default_rng(1234).integers(0, 256, (31, 33, channels), dtype=np.uint8)
    if mode == "RGBA":
        pixels[:8, :, 3] = 0
    encoded = []
    with Image.fromarray(pixels) as image:
        for effort in (0, 70, 100):
            path = tmp_path / f"output-{effort}.avif"
            save_pil_image(image, str(path), lossless=True, quality=quality, compression_effort=effort)
            with Image.open(path) as decoded:
                assert decoded.format == "AVIF"
                assert np.array_equal(pixels, np.asarray(decoded.convert(mode)))
            encoded.append(path.read_bytes())
    assert encoded[0] != encoded[-1], "AVIF effort must affect the actual encoder output"


def test_avif_lossy_retains_pillow_quality_control(tmp_path):
    Image.init()
    if "AVIF" not in Image.SAVE:
        pytest.skip("AVIF encoder unavailable")
    pixels = np.random.default_rng(1234).integers(0, 256, (32, 32, 3), dtype=np.uint8)
    with Image.fromarray(pixels) as image:
        lossy = tmp_path / "lossy.avif"
        lossless = tmp_path / "lossless.avif"
        save_pil_image(image, str(lossy), quality=35, lossless=False)
        save_pil_image(image, str(lossless), quality=35, lossless=True)
        reference = tmp_path / "pillow.avif"
        image.save(reference, format="AVIF", quality=35)
    assert lossy.read_bytes() == reference.read_bytes()
    with Image.open(lossy) as decoded:
        assert not np.array_equal(pixels, np.asarray(decoded.convert("RGB")))
    with Image.open(lossless) as decoded:
        assert np.array_equal(pixels, np.asarray(decoded.convert("RGB")))


@pytest.mark.parametrize("mode", ["L", "LA", "P"])
def test_avif_lossless_preserves_grayscale_and_palette_pixels(tmp_path, mode):
    pixels = np.random.default_rng(1234).integers(0, 256, (17, 19), dtype=np.uint8)
    with Image.fromarray(pixels) as grayscale:
        image = grayscale.convert(mode)
    try:
        if mode == "P":
            image.putpalette(np.random.default_rng(42).integers(0, 256, 768, dtype=np.uint8).tobytes())
            image.info["transparency"] = 0
        elif mode == "LA":
            image.putalpha(Image.fromarray(pixels))
        target_mode = "RGB" if mode == "L" else "RGBA"
        expected = np.asarray(image.convert(target_mode))
        path = tmp_path / f"{mode}.avif"
        save_pil_image(image, str(path), lossless=True)
        with Image.open(path) as decoded:
            assert np.array_equal(expected, np.asarray(decoded.convert(target_mode)))
    finally:
        image.close()


@pytest.mark.parametrize("metadata_source", ["source", "image", "explicit"])
def test_avif_lossless_preserves_icc_with_unicode_temporary_path(tmp_path, metadata_source):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    path = tmp_path / "无损图像.avif.tmp"
    with Image.new("RGBA", (17, 19), (12, 34, 56, 0)) as image, Image.new("RGB", (17, 19)) as source:
        kwargs = {}
        if metadata_source == "source":
            source.info["icc_profile"] = profile
            kwargs["source_image"] = source
        elif metadata_source == "image":
            image.info["icc_profile"] = profile
        else:
            source.info["icc_profile"] = b"overridden profile"
            kwargs.update(source_image=source, icc_profile=profile)
        save_pil_image(image, str(path), format="AVIF", lossless=True, **kwargs)
        with Image.open(path) as decoded:
            assert decoded.info["icc_profile"] == profile
            assert np.array_equal(np.asarray(image), np.asarray(decoded.convert("RGBA")))


def test_avif_lossless_keeps_explicit_exif_and_xmp(tmp_path):
    exif = Image.Exif()
    exif[315] = "AVIF test"
    xmp = '<x:xmpmeta xmlns:x="adobe:ns:meta/"></x:xmpmeta>'
    path = tmp_path / "metadata.avif"
    with Image.new("RGB", (17, 19)) as image:
        save_pil_image(image, str(path), lossless=True, exif=exif, xmp=xmp)
    with Image.open(path) as decoded:
        assert decoded.getexif()[315] == "AVIF test"
        assert decoded.info["xmp"] == xmp.encode("utf-8")
    assert exif[315] == "AVIF test"


def test_avif_lossless_context_and_editor_exports_preserve_pixels(tmp_path):
    pixels = np.random.default_rng(1234).integers(0, 256, (31, 33, 3), dtype=np.uint8)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source_path = tmp_path / "原图.avif"
    context_path = tmp_path / "翻译结果.avif"
    editor_path = tmp_path / "编辑结果.avif"
    config = {"cli": {"save_lossless": True, "save_quality": 0, "compression_effort": 0}}
    context = Context()
    context.save_lossless = True
    context.save_quality = 0
    context.compression_effort = 0
    with Image.fromarray(pixels) as image:
        image.info["icc_profile"] = profile
        save_pil_image(image, str(source_path), lossless=True, compression_effort=0)
        save_result(image, str(context_path), context)
        service = ExportService()
        service._save_rendered_image(image, str(editor_path), config, source_image=image)
        inpainted_path = service.save_inpainted_image(str(source_path), image, config)
    for path in (context_path, editor_path, inpainted_path):
        with Image.open(path) as decoded:
            assert decoded.info["icc_profile"] == profile
            assert np.array_equal(pixels, np.asarray(decoded.convert("RGB")))
    assert not list(tmp_path.glob("*.tmp"))


def test_avif_lossless_missing_encoder_does_not_overwrite_existing_output(tmp_path, monkeypatch):
    path = tmp_path / "existing.avif"
    path.write_bytes(b"existing output")
    monkeypatch.setitem(sys.modules, "pylibheif", None)
    with Image.new("RGB", (17, 19)) as image:
        with pytest.raises(RuntimeError, match="requires pylibheif"):
            save_pil_image(image, str(path), lossless=True)
    assert path.read_bytes() == b"existing output"


@pytest.mark.parametrize("mode", ["I;16", "I", "F"])
def test_avif_lossless_reduces_high_precision_input_instead_of_failing(tmp_path, mode):
    """16-bit TIFFs are valid inputs, so a batch must not die on one.

    JPEG/BMP go through normalize_rgb_image and HEIF/WebP use .convert; AVIF
    alone raised, which aborted an entire --format avif --lossless run.
    """
    path = tmp_path / "high-precision.avif"
    with Image.new(mode, (17, 19), 1024) as image:
        save_pil_image(image, str(path), lossless=True)
    assert path.exists() and path.stat().st_size > 0
    header = path.read_bytes()[:12]
    assert header[4:8] == b"ftyp"


@pytest.mark.parametrize("effort", [0, 70, 100])
def test_webp_lossless_preserves_rgb_under_transparent_pixels(tmp_path, effort):
    pixels = np.random.default_rng(1234).integers(0, 256, (32, 32, 4), dtype=np.uint8)
    pixels[:8, :, 3] = 0
    path = tmp_path / "transparent.webp"
    with Image.fromarray(pixels) as image:
        save_pil_image(image, str(path), lossless=True, compression_effort=effort)
    with Image.open(path) as decoded:
        assert np.array_equal(pixels, np.asarray(decoded.convert("RGBA")))


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_avif_lossless_writes_to_caller_owned_stream(mode):
    channels = 4 if mode == "RGBA" else 3
    pixels = np.random.default_rng(1234).integers(0, 256, (31, 33, channels), dtype=np.uint8)
    if mode == "RGBA":
        pixels[:8, :, 3] = 0
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    with Image.fromarray(pixels) as image, io.BytesIO() as output:
        image.info["icc_profile"] = profile
        save_pil_image(image, output, format="AVIF", lossless=True, compression_effort=0)
        assert not output.closed
        output.seek(0)
        with Image.open(output) as decoded:
            assert decoded.info["icc_profile"] == profile
            assert np.array_equal(pixels, np.asarray(decoded.convert(mode)))


# --- P3-10: streaming transforms run off the event loop -----------------

def test_streaming_transform_runs_in_a_worker_thread(monkeypatch):
    import threading

    import manga_translator.server.request_extraction as extraction

    main_thread = threading.get_ident()
    seen = {}

    def transform(ctx):
        seen['thread'] = threading.get_ident()
        return b'payload'

    async def run():
        return await extraction.run_image_encoding(transform, None)

    result = asyncio.run(run())
    assert result == b'payload'
    assert seen['thread'] != main_thread, 'transform still ran on the event loop'


# --- P3-9: PNG compression follows effort, not the lossless flag ---------

def test_png_compression_level_follows_effort_and_ignores_lossless():
    """PNG is always lossless, so `lossless` must not change the encoding."""
    from PIL import Image
    from manga_translator.utils.generic import save_pil_image

    image = Image.new('RGB', (64, 64), 'red')
    sizes = {}
    for effort in (0, 50, 100):
        buffer = io.BytesIO()
        save_pil_image(image, buffer, format='PNG', compression_effort=effort)
        sizes[effort] = len(buffer.getvalue())
    # effort 0 means compress_level 0 (stored), 100 means level 9.
    assert sizes[100] <= sizes[0]

    on, off = io.BytesIO(), io.BytesIO()
    save_pil_image(image, on, format='PNG', compression_effort=100, lossless=True)
    save_pil_image(image, off, format='PNG', compression_effort=100, lossless=False)
    assert on.getvalue() == off.getvalue()
