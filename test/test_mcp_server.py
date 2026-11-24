import _bootstrap  # noqa: F401

import asyncio
import gc
import json
import logging
import os
import threading

import numpy as np
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

from manga_translator.manga_translator import MangaTranslator
from manga_translator.mcp.job_manager import McpJobManager
from manga_translator.mcp.server_app import McpHttpServer
from manga_translator.utils.textblock import TextBlock


def _make_png(path, size=(240, 320), color=(240, 240, 240)) -> str:
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")
    return str(path)


def _make_block(text: str, y: int = 30) -> TextBlock:
    return TextBlock(
        lines=[[(20, y), (100, y), (100, y + 26), (20, y + 26)]],
        texts=[text],
        prob=0.92,
        font_size=18,
    )


def _stub_factory(translator_params):
    translator = MangaTranslator(params=translator_params)
    translator._mcp_context_enabled = True
    translator._models_loaded = True
    translator.start_model_cleanup_job = False

    async def _detect(config, ctx):
        img_name = (
            getattr(ctx, "image_name", None)
            or getattr(getattr(ctx, "input", None), "name", "")
            or ""
        )
        fname = os.path.basename(img_name)
        text = "こんにちは" if fname == "01.png" else "さようなら"
        blocks = [_make_block(text)]
        h, w = ctx.img_rgb.shape[:2]
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[30:56, 20:100] = 255
        return blocks, mask, mask.copy()

    async def _ocr(config, ctx):
        return list(ctx.textlines or [])

    async def _merge(config, ctx):
        return list(ctx.textlines or [])

    async def _mask(config, ctx):
        h, w = ctx.img_rgb.shape[:2]
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[30:56, 20:100] = 255
        return mask

    async def _inpaint(config, ctx):
        return ctx.img_rgb.copy()

    async def _render(config, ctx, **_kwargs):
        return ctx.img_inpainted.copy()

    translator._run_detection = _detect
    translator._run_ocr = _ocr
    translator._run_textline_merge = _merge
    translator._run_mask_refinement = _mask
    translator._run_inpainting = _inpaint
    translator._run_text_rendering = _render
    return translator


def test_streamable_http_server_e2e(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    p1 = _make_png(tmp_path / "01.png")
    p2 = _make_png(tmp_path / "02.png")

    cfg = {
        "app": {"last_output_path": str(out_dir), "current_preset": "默认", "unload_models_after_translation": False},
        "translator": {"translator": "openai_hq", "target_lang": "CHS", "extract_glossary": False},
        "ocr": {"ocr": "48px", "secondary_ocr": "mocr", "use_hybrid_ocr": False, "prob": 0.1},
        "detector": {"detector": "default"},
        "inpainter": {"inpainter": "lama_large"},
        "render": {"renderer": "default", "disable_auto_wrap": False},
        "colorizer": {"colorizer": "none"},
        "upscale": {"upscale_ratio": None},
        "cli": {"use_gpu": False, "batch_size": 3, "context_size": 3, "batch_concurrent": False, "format": "png", "overwrite": True, "save_text": True},
        "mcp": {
            "host": "127.0.0.1",
            "port": 0,
            "translation_mode": "hq",
            "allow_on_demand_image": True,
            "image_delivery": "auto",
            "inline_image_budget_mb": 2.0,
            "preview_max_size": 512,
            "preview_format": "jpeg",
            "preview_quality": 80,
            "max_parallel_batches": 1,
            "lease_timeout_seconds": 30,
            "models_idle_ttl": 0,
            "use_gui_files_by_default": False,
        },
    }

    mgr = McpJobManager(
        root_dir=str(tmp_path),
        get_config=lambda: cfg,
        translator_factory=_stub_factory,
    )
    server = McpHttpServer(mgr)
    url = server.start(host="127.0.0.1", port=0)
    assert server.is_running
    assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")

    async def _drive_client():
        async with streamable_http_client(url) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                tools_res = await session.list_tools()
                tool_names = {t.name for t in tools_res.tools}
                expected_tools = {
                    "get_workspace_info",
                    "create_translation_job",
                    "next_translation_batch",
                    "get_page_preview",
                    "submit_translation_batch",
                    "release_batch",
                    "get_job_status",
                    "cancel_job",
                    "unload_models",
                }
                assert tool_names == expected_tools

                ws_res = await session.call_tool("get_workspace_info", {})
                ws_data = json.loads(ws_res.content[0].text)
                assert ws_data["effective_mode"] == "hq"
                assert ws_data["api_conflicts"] == []

                job_res = await session.call_tool(
                    "create_translation_job",
                    {"inputs": [p1, p2], "output_dir": str(out_dir)},
                )
                job_data = json.loads(job_res.content[0].text)
                job_id = job_data["job_id"]
                assert job_data["total_pages"] == 2

                batch_res = await session.call_tool("next_translation_batch", {"job_id": job_id})
                assert len(batch_res.content) == 3
                assert isinstance(batch_res.content[0], types.TextContent)
                assert isinstance(batch_res.content[1], types.ImageContent)
                assert isinstance(batch_res.content[2], types.ImageContent)
                assert batch_res.content[1].mimeType == "image/jpeg"
                assert len(batch_res.content[1].data) > 50

                batch_data = json.loads(batch_res.content[0].text)
                batch_id = batch_data["batch_id"]
                assert len(batch_data["pages"]) == 2

                preview_res = await session.call_tool(
                    "get_page_preview",
                    {"job_id": job_id, "page_index": 0, "region_ids": [1]},
                )
                assert len(preview_res.content) == 2
                assert isinstance(preview_res.content[0], types.TextContent)
                assert isinstance(preview_res.content[1], types.ImageContent)

                sub_res = await session.call_tool(
                    "submit_translation_batch",
                    {
                        "job_id": job_id,
                        "batch_id": batch_id,
                        "translations": [
                            {"id": 1, "translation": "你好"},
                            {"id": 2, "translation": "再见"},
                        ],
                        "return_next": True,
                    },
                )
                sub_data = json.loads(sub_res.content[0].text)
                assert sub_data["status"] == "accepted"
                assert sub_data["done"] is True
                assert sub_data["summary"]["rendered_pages"] == 2

                status_res = await session.call_tool("get_job_status", {"job_id": job_id})
                status_data = json.loads(status_res.content[0].text)
                assert status_data["status"] == "completed"
                assert status_data["on_demand_image_requests"] == 1

    try:
        asyncio.run(_drive_client())
        assert (out_dir / "01.png").exists()
        assert (out_dir / "02.png").exists()
    finally:
        server.stop()
        mgr.shutdown()

    assert not server.is_running
    lingering = [t for t in threading.enumerate() if t.is_alive() and t.name == "mcp-http-server"]
    assert lingering == []


def test_format_mcp_endpoint_url_handles_ipv4_and_ipv6():
    from manga_translator.mcp.server_app import format_mcp_endpoint_url

    assert format_mcp_endpoint_url("::1", 8765) == "http://[::1]:8765/mcp"
    assert format_mcp_endpoint_url("[::1]", 8765) == "http://[::1]:8765/mcp"
    assert format_mcp_endpoint_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/mcp"


def test_mcp_http_server_clean_shutdown_leaves_no_pending_tasks(tmp_path, caplog):
    mgr = McpJobManager(root_dir=str(tmp_path), get_config=lambda: {}, translator_factory=_stub_factory)
    server = McpHttpServer(mgr)
    with caplog.at_level("WARNING", logger="asyncio"):
        url = server.start(host="127.0.0.1", port=0)
        assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
        server.stop()
        mgr.shutdown()
        gc.collect()
    assert not any("Task was destroyed but it is pending" in rec.getMessage() for rec in caplog.records)


def test_mcp_server_suppresses_transport_log_noise(tmp_path, monkeypatch):
    captured = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            captured.append((record.levelno, record.levelname, record.name, record.getMessage()))

    root_logger = logging.getLogger()
    prev_handlers = list(root_logger.handlers)
    prev_level = root_logger.level
    prev_filters = list(root_logger.filters)

    capture_handler = _CaptureHandler()
    capture_handler.setLevel(logging.DEBUG)
    root_logger.handlers = [capture_handler]
    root_logger.setLevel(logging.DEBUG)
    monkeypatch.setattr(logging.getLogger("manga_translator"), "propagate", True)

    cfg = {
        "app": {"last_output_path": str(tmp_path), "current_preset": "默认", "unload_models_after_translation": False},
        "translator": {"translator": "openai_hq", "target_lang": "CHS"},
        "ocr": {"ocr": "48px"},
        "detector": {"detector": "default"},
        "inpainter": {"inpainter": "lama_large"},
        "render": {"renderer": "default"},
        "colorizer": {"colorizer": "none"},
        "upscale": {"upscale_ratio": None},
        "cli": {"use_gpu": False},
        "mcp": {"host": "127.0.0.1", "port": 0},
    }

    mgr = McpJobManager(root_dir=str(tmp_path), get_config=lambda: cfg, translator_factory=_stub_factory)
    server = McpHttpServer(mgr)

    async def _probe_client(url: str) -> None:
        async with streamable_http_client(url) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                await session.list_tools()
                await session.call_tool("get_workspace_info", {})

    try:
        url = server.start(host="127.0.0.1", port=0)
        asyncio.run(_probe_client(url))
        server.stop()
        # A second stop() should be a silent no-op.
        server.stop()

        logging.getLogger("mcp.server.transport_security").warning("synthetic security warning")
        logging.getLogger("uvicorn.error").warning("synthetic uvicorn warning")
    finally:
        mgr.shutdown()
        root_logger.handlers = prev_handlers
        root_logger.setLevel(prev_level)
        root_logger.filters = prev_filters

    noisy_prefixes = ("mcp", "sse_starlette", "uvicorn", "starlette")
    noisy_low_priority = [
        (lvl_name, name, msg)
        for lvl_no, lvl_name, name, msg in captured
        if lvl_no < logging.WARNING
        and (name in noisy_prefixes or any(name.startswith(p + ".") for p in noisy_prefixes))
    ]
    assert noisy_low_priority == []

    root_stream_noise = [
        msg
        for lvl_no, _lvl_name, name, msg in captured
        if lvl_no < logging.WARNING and name == "root" and "Read stream closed by client" in msg
    ]
    assert root_stream_noise == []

    own_info_logs = [
        msg
        for lvl_no, _lvl_name, name, msg in captured
        if lvl_no == logging.INFO and name == "manga_translator.mcp"
    ]
    assert sum("Streamable HTTP server listening on" in m for m in own_info_logs) == 1
    assert sum("Streamable HTTP server stopped" in m for m in own_info_logs) == 1

    warning_logs = [
        (name, msg)
        for lvl_no, _lvl_name, name, msg in captured
        if lvl_no >= logging.WARNING
    ]
    assert ("mcp.server.transport_security", "synthetic security warning") in warning_logs
    assert ("uvicorn.error", "synthetic uvicorn warning") in warning_logs


def test_server_stop_with_active_long_poll_exits_cleanly_and_rebinds_same_port(tmp_path):
    """P1-5: stop() under an active long-polling request cancels cleanly and allows immediate same-port restart."""
    import time

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    p1 = _make_png(tmp_path / "01.png")

    detect_entered = threading.Event()
    allow_detect = threading.Event()

    def _slow_detect_factory(params):
        tr = _stub_factory(params)
        orig_detect = tr._run_detection

        async def _slow_detect(config, ctx):
            detect_entered.set()
            allow_detect.wait(timeout=5.0)
            return await orig_detect(config, ctx)

        tr._run_detection = _slow_detect
        return tr

    cfg = {
        "app": {"last_output_path": str(out_dir), "current_preset": "默认", "unload_models_after_translation": False},
        "translator": {"translator": "openai_hq", "target_lang": "CHS"},
        "ocr": {"ocr": "48px"},
        "detector": {"detector": "default"},
        "inpainter": {"inpainter": "lama_large"},
        "render": {"renderer": "default"},
        "colorizer": {"colorizer": "none"},
        "upscale": {"upscale_ratio": None},
        "cli": {"use_gpu": False},
        "mcp": {"host": "127.0.0.1", "port": 0},
    }

    mgr = McpJobManager(root_dir=str(tmp_path), get_config=lambda: cfg, translator_factory=_slow_detect_factory)
    server = McpHttpServer(mgr)
    poll_holder = {}
    poll_started = threading.Event()

    def _client_long_poll(url: str) -> None:
        async def _run():
            async with streamable_http_client(url) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    job_res = await session.call_tool(
                        "create_translation_job",
                        {"inputs": [p1], "output_dir": str(out_dir)},
                    )
                    job_id = json.loads(job_res.content[0].text)["job_id"]
                    assert detect_entered.wait(timeout=5.0)
                    poll_started.set()
                    res = await session.call_tool("next_translation_batch", {"job_id": job_id})
                    poll_holder["result"] = json.loads(res.content[0].text)

        try:
            asyncio.run(_run())
        except Exception as exc:
            poll_holder["exc"] = exc

    try:
        url = server.start(host="127.0.0.1", port=0)
        bound_port = server.port

        t_client = threading.Thread(target=_client_long_poll, args=(url,), daemon=True)
        t_client.start()
        assert poll_started.wait(timeout=5.0)
        time.sleep(0.15)  # ensure next_translation_batch is actively parked inside long poll

        t0 = time.monotonic()
        mgr.cancel_job(join_timeout=1.0)
        server.stop(timeout=5.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 4.5
        assert not server.is_running

        t_client.join(timeout=5.0)
        assert not t_client.is_alive()
        lingering = [t for t in threading.enumerate() if t.is_alive() and t.name == "mcp-http-server"]
        assert lingering == []

        # Immediate restart on the exact same port must succeed cleanly.
        allow_detect.set()
        restarted_url = server.start(host="127.0.0.1", port=bound_port)
        assert restarted_url == url
        assert server.is_running
    finally:
        allow_detect.set()
        if server.is_running:
            server.stop()
        mgr.shutdown()

