import _bootstrap  # noqa: F401, I001

import asyncio
import io
import tempfile
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from manga_translator.config import Config
from manga_translator.server import request_extraction
from manga_translator.server.core import logging_manager, middleware, task_manager
from manga_translator.server.routes import history, translation


@pytest.fixture
def export_request(monkeypatch, tmp_path):
    image = Image.new("RGB", (16, 16), (12, 34, 56))
    ctx = SimpleNamespace(
        result=image,
        text_regions=[SimpleNamespace(text="original", translation="translated")],
    )
    config = Config(cli={"format": "png", "compression_effort": 0})
    saved = []
    temporary_files = []
    temporary_dirs = []

    async def verify_auth(*_args, **_kwargs):
        return "test-user", "127.0.0.1"

    async def user_env(*_args, **_kwargs):
        return {}

    async def translated(*_args, **_kwargs):
        return [ctx]

    def save_history(**kwargs):
        saved.append((
            threading.get_ident(), kwargs["metadata"],
            [Path(filename).read_bytes() for filename in kwargs["files"]],
        ))

    def noop(*_args, **_kwargs):
        pass

    create_file = tempfile.TemporaryFile
    create_dir = tempfile.mkdtemp

    def temporary_file(*args, **kwargs):
        kwargs.setdefault("dir", tmp_path)
        handle = create_file(*args, **kwargs)
        temporary_files.append(handle)
        return handle

    def temporary_dir(*args, **kwargs):
        kwargs.setdefault("dir", tmp_path)
        path = create_dir(*args, **kwargs)
        temporary_dirs.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "TemporaryFile", temporary_file)
    monkeypatch.setattr(tempfile, "mkdtemp", temporary_dir)
    monkeypatch.setattr(translation, "verify_translation_auth", verify_auth)
    monkeypatch.setattr(translation, "apply_user_env_vars", user_env)
    monkeypatch.setattr(translation, "get_batch_ctx", translated)
    for name in ("add_log", "track_task_start", "track_task_end", "log_translation_task_created"):
        monkeypatch.setattr(translation, name, noop)
    monkeypatch.setattr(logging_manager, "add_log", noop)
    monkeypatch.setattr(middleware, "get_services", lambda: (
        None, None, SimpleNamespace(check_offline_translation_permission=lambda _username: True)
    ))
    monkeypatch.setattr(task_manager, "register_active_task", noop)
    monkeypatch.setattr(task_manager, "unregister_active_task", noop)
    monkeypatch.setattr(history, "get_history_service", lambda: SimpleNamespace(save_translation_result=save_history))

    async def invoke(endpoint):
        if endpoint == "history":
            return await request_extraction.save_translation_to_history(
                ctx, "test-user", "test-task", "normal", "page.png", config
            )
        data = request_extraction.BatchTranslateRequest(
            images=[b"stub"], filenames=["page.png"], config=config
        )
        response = await translation.batch_images(SimpleNamespace(), data)
        return b"".join([chunk async for chunk in response.body_iterator])

    yield SimpleNamespace(
        invoke=invoke, image=image, saved=saved,
        temporary_files=temporary_files, temporary_dirs=temporary_dirs,
    )
    image.close()


@pytest.mark.parametrize("endpoint", ["batch", "batch_history", "history"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "cancel_error"])
def test_encoding_keeps_loop_live_and_resources_until_worker_finishes(
    export_request, monkeypatch, endpoint, outcome
):
    release = threading.Event()
    snapshots = []
    main_thread = threading.get_ident()
    original_save = translation.save_pil_image
    encoder_failed = outcome in {"error", "cancel_error"}
    cancelled = outcome in {"cancel", "cancel_error"}

    async def run():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop_errors = []
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        def encode(image, output, **kwargs):
            snapshots.append(image)
            loop.call_soon_threadsafe(entered.set)
            assert threading.get_ident() != main_thread
            assert release.wait(5), "The event loop did not release the encoder"
            # Exercise both image reads and output writes after cancellation was
            # requested: neither resource may have been closed or deleted yet.
            assert image.tobytes() == export_request.image.tobytes()
            original_save(image, output, **kwargs)
            if encoder_failed:
                raise RuntimeError("controlled encoder failure")

        # Also cancel a batch after its ZIP is finished, while its history
        # encoder still owns a snapshot inside the route's outer cleanup scope.
        if endpoint != "batch_history":
            monkeypatch.setattr(translation, "save_pil_image", encode)
        monkeypatch.setattr(request_extraction, "save_pil_image", encode)
        task = asyncio.create_task(export_request.invoke(endpoint))
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            # Reaching this coroutine while the worker is gated proves the
            # actual route leaves the event loop responsive, without a codec
            # benchmark or timing threshold for successful encoding.
            assert not task.done()
            if cancelled:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "Cancellation must drain the encoder"

            assert snapshots[0].tobytes() == export_request.image.tobytes()
            assert all(
                handle.closed == (endpoint == "batch_history")
                for handle in export_request.temporary_files
            )
            assert all(path.exists() for path in export_request.temporary_dirs)
            release.set()

            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not export_request.saved
            elif encoder_failed:
                if endpoint == "batch":
                    with pytest.raises(RuntimeError, match="controlled encoder failure"):
                        await task
                else:
                    # History recording has always logged failures without
                    # turning an otherwise completed translation into an error.
                    result = await task
                    if endpoint == "history":
                        assert result is None
                    else:
                        with zipfile.ZipFile(io.BytesIO(result)) as archive:
                            assert archive.namelist() == ["page.png"]
                assert not export_request.saved
            else:
                result = await task
                if endpoint != "history":
                    with zipfile.ZipFile(io.BytesIO(result)) as archive:
                        assert archive.namelist() == ["page.png"]
                        assert archive.read("page.png") == export_request.saved[0][2][0]
                thread, metadata, _files = export_request.saved[0]
                assert thread == main_thread
                assert metadata["workflow"] == "normal"
                assert metadata["text_regions"] == [{"original": "original", "translated": "translated"}]

            assert not loop_errors
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert all(handle.closed for handle in export_request.temporary_files)
    assert all(not path.exists() for path in export_request.temporary_dirs)
    for snapshot in snapshots:
        with pytest.raises(ValueError, match="closed image"):
            snapshot.tobytes()
    assert export_request.image.tobytes() == bytes([12, 34, 56]) * 16 * 16
