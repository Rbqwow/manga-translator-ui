import _bootstrap  # noqa: F401, I001

import asyncio
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageCms

from manga_translator.args import create_parser
from manga_translator.config import Config
from manga_translator.mode import local, subprocess_manager


@pytest.mark.parametrize(
    "saved_lossless, extra_args, expected_lossless, expected_format",
    [
        (False, ["--lossless", "--format", "avif"], True, "avif"),
        (True, [], True, "webp"),
        (False, [], False, "webp"),
    ],
)
def test_local_subprocess_honors_output_overrides(
    tmp_path, monkeypatch, saved_lossless, extra_args, expected_lossless, expected_format
):
    from desktop_qt_ui.services import config_service

    captured = {}
    config = Config(cli={"save_lossless": saved_lossless, "format": "webp", "compression_effort": 0})
    monkeypatch.setattr(config_service, "ConfigService", lambda *_args: SimpleNamespace(get_config=lambda: config))

    async def translate_with_subprocess(**kwargs):
        captured.update(kwargs["config_dict"]["cli"])
        return 1, 0, 0

    monkeypatch.setattr(subprocess_manager, "translate_with_subprocess", translate_with_subprocess)
    source_path = tmp_path / "page.png"
    with Image.new("RGB", (8, 8)) as image:
        image.save(source_path)
    args = create_parser().parse_args([
        "local", "-i", str(source_path), "-o", str(tmp_path / "output"), "--subprocess", *extra_args
    ])
    asyncio.run(local.run_local_mode(args))

    assert captured["save_lossless"] is expected_lossless
    assert captured["format"] == expected_format
    assert captured["compression_effort"] == 0
    assert config.cli.save_lossless is saved_lossless


@pytest.fixture
def server_export(monkeypatch):
    from manga_translator.server.core import logging_manager, middleware, task_manager
    from manga_translator.server.routes import history, translation
    from manga_translator.server.request_extraction import BatchTranslateRequest

    rows = np.random.default_rng(1234).integers(0, 256, (8, 33, 4), dtype=np.uint8)
    pixels = np.repeat(rows, 4, axis=0)
    pixels[:8, :, 3] = 0
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    resources = []
    history_files = []

    async def verify_auth(*_args, **_kwargs):
        return "test-user", "127.0.0.1"

    async def user_env(*_args, **_kwargs):
        return {}

    async def translate_batch(*_args, **_kwargs):
        image = Image.fromarray(pixels)
        image.info["icc_profile"] = profile
        resources.append(image)
        return [SimpleNamespace(result=image, text_regions=[])]

    def save_history(**kwargs):
        for filename in kwargs["files"]:
            path = Path(filename)
            history_files.append((path, path.read_bytes()))

    def noop(*_args, **_kwargs):
        pass

    monkeypatch.setattr(translation, "verify_translation_auth", verify_auth)
    monkeypatch.setattr(translation, "apply_user_env_vars", user_env)
    monkeypatch.setattr(translation, "get_batch_ctx", translate_batch)
    for name in ("add_log", "track_task_start", "track_task_end", "log_translation_task_created"):
        monkeypatch.setattr(translation, name, noop)
    monkeypatch.setattr(logging_manager, "add_log", noop)
    monkeypatch.setattr(middleware, "get_services", lambda: (
        None, None, SimpleNamespace(check_offline_translation_permission=lambda _username: True)
    ))
    monkeypatch.setattr(task_manager, "register_active_task", noop)
    monkeypatch.setattr(task_manager, "unregister_active_task", noop)
    monkeypatch.setattr(history, "get_history_service", lambda: SimpleNamespace(save_translation_result=save_history))

    def export(fmt, lossless, effort):
        config = Config(cli={
            "format": fmt, "save_lossless": lossless, "compression_effort": effort, "save_quality": 35,
        })
        history_files.clear()
        request = BatchTranslateRequest(images=[b"stub"], filenames=["page.webp"], config=config)

        async def collect_zip():
            response = await translation.batch_images(SimpleNamespace(), request)
            return b"".join([chunk async for chunk in response.body_iterator])

        zip_data = asyncio.run(collect_zip())
        with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
            name, = archive.namelist()
            downloaded = archive.read(name)
        history_path, recorded = history_files[0]
        assert history_path.name == name
        assert not history_path.parent.exists(), "History must clean up its temporary directory"
        assert resources[-1].tobytes() == pixels.tobytes(), "Saving must keep the caller's image open"
        return downloaded, recorded, pixels, profile

    yield export
    for image in resources:
        image.close()


@pytest.mark.parametrize(
    "fmt, lossless, effort",
    [
        ("webp", True, 0), ("webp", True, 100),
        ("avif", True, 0), ("avif", True, 100),
        ("heic", True, 0), ("png", False, 0), ("png", False, 100),
        ("webp", False, 70), ("avif", False, 70),
        ("不指定", True, 0),
    ],
)
def test_server_download_and_history_honor_lossless_config(server_export, fmt, lossless, effort):
    if fmt == "heic":
        pillow_heif = pytest.importorskip("pillow_heif")
        if not pillow_heif.libheif_info().get("HEIF"):
            pytest.skip("HEIF encoder unavailable")
    downloaded, recorded, pixels, profile = server_export(fmt, lossless, effort)
    for encoded in (downloaded, recorded):
        with Image.open(io.BytesIO(encoded)) as decoded:
            assert decoded.info["icc_profile"] == profile
            matches = np.array_equal(pixels, np.asarray(decoded.convert("RGBA")))
            assert matches == (lossless or fmt == "png")
        if fmt in {"webp", "不指定"}:
            assert (b"VP8L" in encoded) is lossless


@pytest.mark.parametrize("fmt", ["png", "webp", "avif"])
def test_server_compression_effort_changes_download_and_history_encoding(server_export, fmt):
    fast = server_export(fmt, True, 0)[:2]
    compressed = server_export(fmt, True, 100)[:2]
    for fast_image, compressed_image in zip(fast, compressed):
        assert fast_image != compressed_image
        if fmt == "png":
            assert len(fast_image) > len(compressed_image)
