import _bootstrap  # noqa: F401, I001

import concurrent.futures
from types import SimpleNamespace
from typing import ClassVar

import numpy as np
import pytest

from editor.controller_export_service import EditorControllerExportService
from editor.document_state import DocumentSnapshot
from editor.editor_model import EditorModel
from editor.inpaint_state import InpaintArtifact, InpaintKey


class _History:
    def __init__(self):
        self.clean = False

    def mark_clean(self):
        self.clean = True


class _Controller:
    def __init__(self, source_path, *, mask=None, artifact_image_value=None):
        self.base_image = np.full((8, 8, 3), 23, dtype=np.uint8)
        self.model = EditorModel()
        self.model.apply_document_snapshot(
            DocumentSnapshot(
                source_path=str(source_path),
                image=self.base_image,
                regions=[{"translation": "saved text"}],
                raw_mask=mask,
            )
        )
        self.artifact = None
        if artifact_image_value is not None:
            self.artifact = InpaintArtifact(
                key=self.model.get_inpaint_key(),
                mask=mask,
                image=np.full((8, 8, 3), artifact_image_value, dtype=np.uint8),
            )
            assert self.model.install_inpaint_artifact(self.artifact)
        self.history_service = _History()
        self.logger = SimpleNamespace(
            error=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        )
        self.config_service = SimpleNamespace(
            get_config=lambda: SimpleNamespace(
                model_dump=lambda: {"app": {}, "cli": {}}
            )
        )
        self.commits = 0

    def commit_pending_edits(self):
        self.commits += 1

    def get_toast_manager(self):
        return None


class _PersistenceService:
    calls: ClassVar[list] = []
    saved_images: ClassVar[list[np.ndarray]] = []
    sidecar_path = None

    def save_editor_project(self, source_path, regions, mask, config, **kwargs):
        self.calls.append((regions, None, source_path, mask, config, kwargs))

    def save_inpainted_image(self, _source_path, image, _config):
        self.saved_images.append(np.array(image, copy=True))

    def delete_inpainted_image(self, _source_path):
        if self.sidecar_path is None:
            return False
        self.sidecar_path.unlink(missing_ok=True)
        return True

    def build_output_path(self, _config, source_path):
        return str(source_path) + ".export.png"


class _RenderParameterService:
    def export_parameters_for_backend(self, _index, _region):
        return {}


def _completed_future():
    future = concurrent.futures.Future()
    future.set_result(None)
    return future


def test_inpaint_artifact_owns_immutable_arrays():
    source_mask = np.full((3, 4), 29, dtype=np.uint8)
    source_image = np.full((3, 4, 3), 47, dtype=np.uint8)
    artifact = InpaintArtifact(
        key=InpaintKey(1, 3),
        mask=source_mask,
        image=source_image,
    )

    source_mask[:] = 0
    source_image[:] = 0

    assert np.all(artifact.mask == 29)
    assert np.all(artifact.image == 47)
    assert not artifact.mask.flags.writeable
    assert not artifact.image.flags.writeable
    with pytest.raises(ValueError):
        artifact.mask[:] = 5
    with pytest.raises(ValueError):
        artifact.image[:] = 6


def test_save_editor_state_with_empty_mask_deletes_sidecar_without_exporting(
    monkeypatch, tmp_path
):
    from services import export_service

    _PersistenceService.calls.clear()
    _PersistenceService.saved_images.clear()
    monkeypatch.setattr(export_service, "ExportService", _PersistenceService)
    source_path = tmp_path / "page.png"
    source_path.touch()
    stale_sidecar = tmp_path / "stale_inpainted.png"
    stale_sidecar.write_bytes(b"stale")
    _PersistenceService.sidecar_path = stale_sidecar
    empty_mask = np.zeros((8, 8), dtype=np.uint8)
    controller = _Controller(source_path, mask=empty_mask)
    ready_calls = 0
    orig_ready = controller.model.get_ready_inpaint_artifact

    def _spy_ready():
        nonlocal ready_calls
        ready_calls += 1
        return orig_ready()

    monkeypatch.setattr(controller.model, "get_ready_inpaint_artifact", _spy_ready)
    service = EditorControllerExportService(controller)
    submitted = []
    monkeypatch.setattr(service, "_submit_job", lambda job: submitted.append(job))

    assert service.save_editor_state() is True
    assert controller.commits == 1
    assert controller.history_service.clean is True
    assert ready_calls == 0
    assert submitted == []
    assert len(_PersistenceService.calls) == 1
    regions, _json_path, saved_source_path, mask, _config, _kwargs = (
        _PersistenceService.calls[0]
    )
    assert regions == [{"translation": "saved text"}]
    assert saved_source_path == str(source_path)
    assert np.array_equal(mask, empty_mask)
    assert not stale_sidecar.exists()

    service.shutdown()


def test_export_uses_mask_and_image_from_one_ready_artifact(monkeypatch, tmp_path):
    from editor import controller_export_service as export_module

    monkeypatch.setattr(
        export_module,
        "get_render_parameter_service",
        lambda: _RenderParameterService(),
    )
    source_path = tmp_path / "page.png"
    source_path.touch()
    model_mask = np.full((8, 8), 255, dtype=np.uint8)
    controller = _Controller(source_path, mask=model_mask, artifact_image_value=149)
    artifact = controller.artifact
    ready_calls = 0
    orig_ready = controller.model.get_ready_inpaint_artifact

    def _spy_ready():
        nonlocal ready_calls
        ready_calls += 1
        return orig_ready()

    monkeypatch.setattr(controller.model, "get_ready_inpaint_artifact", _spy_ready)
    service = EditorControllerExportService(controller)
    submitted = []
    monkeypatch.setattr(
        service,
        "_submit_job",
        lambda job: submitted.append(job) or _completed_future(),
    )

    assert service.export_image() is not None
    assert ready_calls == 0
    assert len(submitted) == 1
    controller.base_image[:] = 99
    assert np.all(np.asarray(submitted[0].export_base.source_image) == 23)
    assert np.array_equal(submitted[0].export_base.mask, artifact.mask)
    assert np.array_equal(submitted[0].export_base.mask, model_mask)
    assert np.array_equal(submitted[0].export_base.render_image, artifact.image)
    assert submitted[0].export_base.render_image.flags.writeable
    assert not np.shares_memory(
        submitted[0].export_base.render_image,
        artifact.image,
    )
    submitted[0].release_resources()
    service.shutdown()
