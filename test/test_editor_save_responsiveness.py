import _bootstrap  # noqa: F401, I001

import logging
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication, QWidget

from editor.controller_document_service import EditorControllerDocumentService
from editor.controller_export_service import EditorControllerExportService, ExportJob, ExportOutcome, SaveJob
from editor.document_state import ExportBase
from editor.inpaint_state import InpaintArtifact, InpaintKey
from services.history_service import EditorStateManager


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture
def editor(app, tmp_path):
    image = np.full((16, 16, 3), 37, dtype=np.uint8)
    mask = np.full((16, 16), 255, dtype=np.uint8)
    regions = [{"translation": "snapshot"}]
    overlays = [{"offset": [1, 2]}]
    config_dict = {"cli": {"save_lossless": True, "compression_effort": 70}}
    config = SimpleNamespace(
        model_dump=lambda: config_dict,
        app=SimpleNamespace(editor_auto_save_on_switch=True, editor_auto_export_on_switch=False),
    )
    window = QWidget()
    window.show()
    history = EditorStateManager()
    history.mark_dirty()
    errors = []
    model = SimpleNamespace(
        get_source_image_path=lambda: str(tmp_path / "page.avif"),
        get_document_identity=lambda: (1, "page.avif"),
        get_image=lambda: image,
        get_regions=lambda: regions,
        get_refined_mask=lambda: mask,
        get_display_layers=lambda: SimpleNamespace(inpaint_display_image=image),
        get_paint_overlay_image=lambda: None,
        get_stamp_overlay_image=lambda: None,
        get_paste_overlays=lambda: overlays,
    )
    controller = SimpleNamespace(
        model=model,
        view=window,
        history_service=history,
        logger=logging.getLogger(__name__),
        config_service=SimpleNamespace(get_config=lambda: config),
        commit_pending_edits=lambda: None,
        get_toast_manager=lambda: SimpleNamespace(
            show_success=lambda *_: None, show_error=lambda message, *_: errors.append(message)
        ),
    )
    service = EditorControllerExportService(controller)
    controller.export_service = service
    controller.save_editor_state = service.save_editor_state
    documents = EditorControllerDocumentService(controller)
    loaded = []
    documents.do_load_image = loaded.append
    yield SimpleNamespace(
        controller=controller, service=service, documents=documents, loaded=loaded,
        image=image, mask=mask, regions=regions, overlays=overlays,
        config=config_dict, errors=errors, window=window,
    )
    service.shutdown()
    documents.shutdown()
    window.close()
    window.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("fail", [False, True])
def test_slow_save_keeps_qt_live_and_switch_waits_for_persistence(editor, monkeypatch, fail):
    from ui.secondary_pages import themed_progress_dialog

    entered = threading.Event()
    release = threading.Event()
    written = threading.Event()
    worker_threads = []
    dialogs = []
    create_dialog = themed_progress_dialog.create_progress_dialog

    def create(*args, **kwargs):
        dialog = create_dialog(*args, **kwargs)
        dialogs.append(dialog)
        return dialog

    monkeypatch.setattr(themed_progress_dialog, "create_progress_dialog", create)

    def save_project(*args, **kwargs):
        worker_threads.append(threading.get_ident())

    def save_image(*args):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3), "Qt did not service the release timer"
        if fail:
            raise RuntimeError("encoder failed")
        written.set()

    monkeypatch.setattr(editor.service._export_service, "save_editor_project", save_project)
    monkeypatch.setattr(editor.service._export_service, "save_inpainted_image", save_image)
    observations = []
    attempts = []
    timer = QTimer()
    timer.setInterval(10)

    def heartbeat():
        if entered.is_set() and not release.is_set():
            observations.append((
                editor.controller.history_service.is_clean(),
                list(editor.loaded), editor.service.unfinished_count(),
            ))
            if not attempts:
                attempts.append(editor.service.save_editor_state())
                attempts.append(editor.window.close())
                editor.documents.load_image_and_regions("reentrant.png")

    timer.timeout.connect(heartbeat)
    timer.start()
    QTimer.singleShot(350, release.set)
    try:
        editor.documents.load_image_and_regions("next.png")
    finally:
        release.set()
        timer.stop()

    assert len(observations) >= 3
    assert all(clean is False and loaded == [] and pending == 1 for clean, loaded, pending in observations)
    assert attempts == [False, False], "Repeated save and close must be rejected during the wait"
    assert editor.window.isVisible()
    assert worker_threads and all(thread != threading.get_ident() for thread in worker_threads)
    assert len(dialogs) == 1
    assert editor.service.unfinished_count() == 0
    assert not editor.service.saving_in_progress()
    assert editor.controller.history_service.is_clean() is not fail
    assert editor.loaded == ([] if fail else ["next.png"])
    assert written.is_set() is not fail
    assert bool(editor.errors) is fail


def test_fast_save_does_not_flash_progress_dialog(editor, monkeypatch):
    from ui.secondary_pages import themed_progress_dialog

    dialogs = []
    monkeypatch.setattr(themed_progress_dialog, "create_progress_dialog", lambda *args: dialogs.append(args))
    monkeypatch.setattr(editor.service._export_service, "save_editor_project", lambda *args, **kwargs: None)
    monkeypatch.setattr(editor.service._export_service, "save_inpainted_image", lambda *args: None)

    assert editor.service.save_editor_state()
    assert dialogs == []


def test_background_edits_cannot_be_marked_saved_or_discarded_on_switch(editor, monkeypatch):
    from PyQt6.QtGui import QUndoCommand

    release = threading.Event()
    snapshots = []

    def save_project(path, regions, mask, config, **kwargs):
        assert release.wait(3)
        snapshots.append((regions, mask.copy(), config, kwargs))

    def save_image(path, image, config):
        snapshots.append(np.array(image, copy=True))

    monkeypatch.setattr(editor.service._export_service, "save_editor_project", save_project)
    monkeypatch.setattr(editor.service._export_service, "save_inpainted_image", save_image)

    def queued_edit():
        editor.regions[0]["translation"] = "new edit"
        editor.mask[:] = 0
        editor.image[:] = 91
        editor.overlays[0]["offset"][0] = 9
        editor.config["cli"]["compression_effort"] = 0
        editor.controller.history_service.execute(QUndoCommand("background OCR result"))
        release.set()

    QTimer.singleShot(30, queued_edit)
    try:
        editor.documents.load_image_and_regions("next.png")
    finally:
        release.set()

    assert editor.loaded == []
    assert not editor.controller.history_service.is_clean()
    assert editor.errors
    regions, mask, config, kwargs = snapshots[0]
    assert regions == [{"translation": "snapshot"}]
    assert np.all(mask == 255)
    assert config["cli"]["compression_effort"] == 70
    assert kwargs["paste_overlays"] == [{"offset": [1, 2]}]
    assert np.all(snapshots[1] == 37)


def test_shutdown_drains_owned_save_snapshots_and_rejects_new_saves(editor, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    writes = []

    def save_project(*args, **kwargs):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(editor.service._export_service, "save_editor_project", save_project)
    monkeypatch.setattr(editor.service._export_service, "save_inpainted_image", lambda path, image, config: writes.append(image.tobytes()))
    source = Image.new("RGB", (8, 8), (1, 2, 3))
    job = SaveJob("page.avif", [], None, {}, source)
    source.close()
    future = editor.service._submit_save_job(job)
    assert entered.wait(2)
    shutdown = threading.Thread(target=editor.service.shutdown)
    shutdown.start()
    try:
        deadline = time.monotonic() + 2
        while editor.service._accepting and time.monotonic() < deadline:
            time.sleep(0.001)
        assert shutdown.is_alive()
        assert editor.service.save_editor_state() is False
        assert not future.done()
        release.set()
        shutdown.join(2)
        assert not shutdown.is_alive()
        assert future.result(timeout=2) is None
        assert writes == [bytes([1, 2, 3]) * 64]
        assert editor.service.unfinished_count() == 0
        with pytest.raises(ValueError, match="closed image"):
            job.inpainted_image.tobytes()
    finally:
        release.set()
        shutdown.join(3)


def test_auto_save_does_not_overwrite_inpainted_image_from_auto_export(editor, monkeypatch, tmp_path):
    # Both operations write the same sidecar. An export may produce a newer
    # backend-inpaint result than the editor snapshot captured for saving.
    sidecar = tmp_path / "inpainted.png"
    config = editor.controller.config_service.get_config()
    config.app.editor_auto_export_on_switch = True
    monkeypatch.setattr(editor.service._export_service, "save_editor_project", lambda *args, **kwargs: None)

    def save_image(path, image, config):
        Image.fromarray(image).save(sidecar)

    def export_image(automatic):
        assert automatic

        def persist_export():
            with Image.new("RGB", (16, 16), (99, 99, 99)) as rendered:
                rendered.save(sidecar)

        return editor.service._executor.submit(persist_export)

    monkeypatch.setattr(editor.service._export_service, "save_inpainted_image", save_image)
    editor.controller.export_image = export_image
    editor.documents.load_image_and_regions("next.png")
    editor.service.shutdown()

    assert editor.loaded == ["next.png"]
    with Image.open(sidecar) as image:
        assert np.all(np.asarray(image) == 99)


@pytest.mark.parametrize("finish_before_save", [False, True])
def test_save_snapshots_preceding_export_artifact_after_queued_delivery(editor, monkeypatch, tmp_path, finish_before_save):
    sidecar = tmp_path / "inpainted.png"
    entered = threading.Event()
    release = threading.Event()
    display = {"image": editor.image}
    key = InpaintKey(1, 0)
    artifact = InpaintArtifact(key, editor.mask, np.full_like(editor.image, 99))

    class Completion(QObject):
        finished = pyqtSignal(object)

        @pyqtSlot(object)
        def install(self, outcome):
            display["image"] = outcome.generated_artifact.image

    completion = Completion()
    completion.finished.connect(completion.install, type=Qt.ConnectionType.QueuedConnection)
    editor.controller._export_job_finished_signal = completion.finished
    editor.controller._export_queue_status_signal = SimpleNamespace(emit=lambda *_: None)
    editor.controller.model.get_display_layers = lambda: SimpleNamespace(inpaint_display_image=display["image"])
    source_path = editor.controller.model.get_source_image_path()
    job = ExportJob(
        False, source_path, "export.avif",
        ExportBase("backend_inpaint", editor.image, editor.image, editor.mask, key),
        [], {},
    )

    def export(export_job):
        entered.set()
        assert release.wait(3)
        Image.fromarray(artifact.image).save(sidecar)
        return ExportOutcome(False, source_path, "export.avif", True, generated_artifact=artifact)

    def save_image(path, image, config):
        Image.fromarray(image).save(sidecar)

    monkeypatch.setattr(editor.service, "execute_export_job", export)
    monkeypatch.setattr(editor.service._export_service, "save_editor_project", lambda *args, **kwargs: None)
    monkeypatch.setattr(editor.service._export_service, "save_inpainted_image", save_image)
    exported = editor.service._submit_job(job)
    assert entered.wait(2)
    if finish_before_save:
        release.set()
        exported.result(timeout=2)
        # Finish worker callbacks without processing the queued Qt artifact.
        editor.service._executor.submit(lambda: None).result(timeout=2)
        assert editor.service.unfinished_count() == 0
        assert np.all(display["image"] == 37)
    else:
        QTimer.singleShot(30, release.set)

    try:
        assert editor.service.save_editor_state()
    finally:
        release.set()
    assert editor.controller.history_service.is_clean()
    assert np.all(display["image"] == 99)
    with Image.open(sidecar) as image:
        assert np.array_equal(np.asarray(image), display["image"])


def test_save_wait_allows_internal_progress_toast_to_close(editor, monkeypatch):
    from PyQt6 import sip
    from ui.widgets.toast_notification import ToastManager

    toast = ToastManager(editor.window).show_info("background work", duration=0)
    bar = toast._bar
    assert bar.isVisible()
    release = threading.Event()

    def save_image(*args):
        assert release.wait(3)

    monkeypatch.setattr(editor.service._export_service, "save_editor_project", lambda *args, **kwargs: None)
    monkeypatch.setattr(editor.service._export_service, "save_inpainted_image", save_image)
    QTimer.singleShot(20, toast.close)
    QTimer.singleShot(50, release.set)
    try:
        assert editor.service.save_editor_state()
    finally:
        release.set()
    assert toast._bar is None
    assert sip.isdeleted(bar) or not bar.isVisible()
