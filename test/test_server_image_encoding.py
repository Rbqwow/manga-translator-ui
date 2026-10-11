import _bootstrap  # noqa: F401, I001

import io
import json
from base64 import b64encode
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageCms


IMAGE_ENDPOINTS = [
    "/image", "/with-form/image", "/upscale", "/colorize", "/inpaint",
    "/import/json", "/import/txt", "/complete",
    "/image/stream", "/with-form/image/stream", "/with-form/image/stream/web",
    "/upscale/stream", "/colorize/stream", "/inpaint/stream",
    "/import/json/stream", "/import/txt/stream",
]


@pytest.fixture
def image_api(monkeypatch, tmp_path):
    from manga_translator.server import request_extraction
    from manga_translator.server.core import config_manager, logging_manager, middleware, task_manager
    from manga_translator.server.routes import translation

    monkeypatch.chdir(tmp_path)
    rows = np.random.default_rng(1234).integers(0, 256, (8, 33, 4), dtype=np.uint8)
    pixels = np.repeat(rows, 4, axis=0)
    pixels[:8, :, 3] = 0
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    state = SimpleNamespace(missing_result=False, placeholder=False)
    resources = []

    with Image.fromarray(pixels) as image, io.BytesIO() as output:
        image.save(output, format="PNG")
        input_image = output.getvalue()

    def translated_context():
        image = None
        if not state.missing_result:
            image = Image.fromarray(pixels[:1, :1] if state.placeholder else pixels)
            image.info["icc_profile"] = profile
            resources.append(image)
        # Deliberately omit ctx.config: output settings belong to the request.
        return SimpleNamespace(result=image, input=None, text_regions=[], use_placeholder=state.placeholder)

    async def get_ctx(_req, _config, image, _workflow):
        if isinstance(image, Image.Image):
            image.close()
        return translated_context()

    async def translate_in_thread(*_args, **_kwargs):
        return translated_context()

    async def verify_auth(*_args, **_kwargs):
        return "test-user", "127.0.0.1"

    async def user_env(*_args, **_kwargs):
        return {}

    def noop(*_args, **_kwargs):
        pass

    async def async_noop(*_args, **_kwargs):
        pass

    monkeypatch.setattr(translation, "get_ctx", get_ctx)
    monkeypatch.setattr(translation, "verify_translation_auth", verify_auth)
    monkeypatch.setattr(translation, "apply_user_env_vars", user_env)
    for name in ("track_task_start", "track_task_end", "log_translation_task_created"):
        monkeypatch.setattr(translation, name, noop)
    monkeypatch.setattr(task_manager, "run_in_translator_thread", translate_in_thread)
    for name in (
        "get_semaphore", "init_semaphore", "register_active_task", "unregister_active_task",
        "update_task_status", "cleanup_context",
    ):
        monkeypatch.setattr(task_manager, name, noop)
    monkeypatch.setattr(task_manager, "is_task_cancelled", lambda _task_id: False)
    for name in ("check_concurrent_limit", "increment_task_count", "decrement_task_count"):
        monkeypatch.setattr(middleware, name, noop)
    for name in ("add_log", "set_task_id", "set_session_id"):
        monkeypatch.setattr(logging_manager, name, noop)
    monkeypatch.setattr(config_manager, "reload_admin_settings_if_changed", noop)
    monkeypatch.setattr(request_extraction, "_create_task_log_handler", noop)
    monkeypatch.setattr(request_extraction, "save_translation_to_history", async_noop)

    app = FastAPI()
    app.include_router(translation.router)

    with TestClient(app) as client:
        def request(endpoint, effort):
            config = {"cli": {"compression_effort": effort, "format": "webp"}}
            if endpoint in ("/image", "/image/stream"):
                return client.post("/translate" + endpoint, json={
                    "image": "data:image/png;base64," + b64encode(input_image).decode("ascii"),
                    "config": config,
                })
            files = {"image": ("page.png", input_image, "image/png")}
            if endpoint.startswith("/import/"):
                files["json_file"] = ("page.json", json.dumps({
                    "page.png": {"regions": [{"text": "hello", "translation": ""}]},
                }).encode(), "application/json")
            if endpoint.startswith("/import/txt"):
                files["txt_file"] = ("page.txt", b'{"hello": "world"}', "text/plain")
                files["template"] = ("template.txt", b"<original>: <translated>", "text/plain")
            return client.post("/translate" + endpoint, data={"config": json.dumps(config)}, files=files)

        yield SimpleNamespace(request=request, pixels=pixels, profile=profile, state=state)

    for image in resources:
        image.close()


def stream_messages(content):
    messages = []
    while content:
        assert len(content) >= 5
        size = int.from_bytes(content[1:5], "big")
        assert len(content) >= size + 5
        messages.append((content[0], content[5:5 + size]))
        content = content[5 + size:]
    return messages


def response_png(response, endpoint):
    assert response.status_code == 200, response.text
    if "/stream" in endpoint:
        assert response.headers["content-type"] == "application/octet-stream"
        messages = stream_messages(response.content)
        assert not [payload for status, payload in messages if status == 2]
        image, = [payload for status, payload in messages if status == 0]
        return image
    if endpoint == "/complete":
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {response.headers['content-type']}\r\n\r\n".encode() + response.content
        )
        image, = [part for part in message.iter_parts() if part.get_content_type() == "image/png"]
        return image.get_payload(decode=True)
    assert response.headers["content-type"] == "image/png"
    return response.content


@pytest.mark.parametrize("endpoint", IMAGE_ENDPOINTS)
def test_single_image_responses_honor_compression_effort(image_api, endpoint):
    fast = response_png(image_api.request(endpoint, 0), endpoint)
    compressed = response_png(image_api.request(endpoint, 100), endpoint)

    assert len(fast) > len(compressed)
    for encoded in (fast, compressed):
        with Image.open(io.BytesIO(encoded)) as decoded:
            assert decoded.format == "PNG"
            assert np.array_equal(np.asarray(decoded), image_api.pixels)
            assert decoded.info["icc_profile"] == image_api.profile


def test_web_placeholder_honors_compression_effort(image_api):
    image_api.state.placeholder = True
    endpoint = "/with-form/image/stream/web"
    fast = response_png(image_api.request(endpoint, 0), endpoint)
    compressed = response_png(image_api.request(endpoint, 100), endpoint)
    assert len(fast) > len(compressed)
    for encoded in (fast, compressed):
        with Image.open(io.BytesIO(encoded)) as decoded:
            assert decoded.size == (1, 1)
            assert np.array_equal(np.asarray(decoded), image_api.pixels[:1, :1])


@pytest.mark.parametrize("endpoint", [endpoint for endpoint in IMAGE_ENDPOINTS if endpoint != "/complete"])
def test_missing_single_image_preserves_error_response(image_api, endpoint):
    image_api.state.missing_result = True
    response = image_api.request(endpoint, 0)
    if "/stream" in endpoint:
        assert response.status_code == 200
        messages = stream_messages(response.content)
        assert not [payload for status, payload in messages if status == 0]
        error, = [json.loads(payload) for status, payload in messages if status == 2]
        assert error["stage"] == "no_result"
    else:
        assert response.status_code == 500
        assert "failed" in response.json()["detail"].lower()
