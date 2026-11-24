from __future__ import annotations

import _bootstrap  # noqa: F401

from types import SimpleNamespace

import numpy as np


def test_async_region_update_uses_region_id_on_main_thread() -> None:
    from desktop_qt_ui.editor.document_state import DocumentSnapshot
    from desktop_qt_ui.editor.editor_controller import EditorController, _AsyncRegionUpdateRequest
    from desktop_qt_ui.editor.editor_model import EditorModel

    model = EditorModel()
    model.apply_document_snapshot(
        DocumentSnapshot(
            source_path="page.png",
            image=np.zeros((8, 8, 3), dtype=np.uint8),
            regions=[{"text": "old"}],
        )
    )
    target_id = model.get_region_id(0)
    assert target_id is not None

    # 在前面插入新区域，原区域顺移到索引 1，但 region_id 保持不变
    model.insert_region(0, {"text": "new"})
    assert model.find_region_index(target_id) == 1

    changes = []
    model.regions_changed.connect(changes.append)

    finish_calls = []
    executed_commands = []

    def execute_command(command):
        executed_commands.append(command)
        command.redo()

    controller = SimpleNamespace(
        model=model,
        logger=SimpleNamespace(error=lambda *args, **kwargs: None),
        execute_command=execute_command,
        _finish_async_region_update=lambda *args, **kwargs: finish_calls.append((args, kwargs)),
    )

    request = _AsyncRegionUpdateRequest("text", [(target_id, "ocr result")], "ocr")
    EditorController.on_regions_update_finished(controller, request)

    regions = model.get_regions()
    assert regions[0]["text"] == "new"
    assert regions[1]["text"] == "ocr result"
    assert len(changes) == 1
    assert "text" in changes[0].fields
    assert changes[0].source == "async"

    # 写回必须走撤销栈里的命令，且 undo 能还原
    assert len(executed_commands) == 1
    executed_commands[0].undo()
    regions_after_undo = model.get_regions()
    assert regions_after_undo[0]["text"] == "new"
    assert regions_after_undo[1]["text"] == "old"


def test_legacy_white_frame_is_normalized_to_render_box() -> None:
    from desktop_qt_ui.editor.region_geometry_state import (
        RegionGeometryState,
        normalize_region_geometry_data,
    )
    from desktop_qt_ui.editor.region_render_snapshot import RegionRenderSnapshot

    legacy = {
        "center": [50, 50],
        "angle": 0,
        "has_custom_white_frame": False,
        "white_frame_rect_local": [10, 20, 30, 40],
    }

    normalized = normalize_region_geometry_data(legacy)
    state = RegionGeometryState.from_region_data(legacy)
    snapshot = RegionRenderSnapshot.from_sources(0, legacy)

    assert normalized.get("render_box_rect_local") == [10, 20, 30, 40]
    assert "white_frame_rect_local" not in normalized
    assert list(state.render_box_local) == [10, 20, 30, 40]
    assert tuple(state.white_frame_local) == snapshot.white_frame_local


def main() -> int:
    test_async_region_update_uses_region_id_on_main_thread()
    test_legacy_white_frame_is_normalized_to_render_box()
    print("EDITOR_REGION_REGRESSIONS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
