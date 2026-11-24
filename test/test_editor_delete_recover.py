import _bootstrap  # noqa: F401

"""Regression tests for deleting a text region together with its mask."""

import numpy as np


def test_region_mask_removal_clears_only_the_selected_polygon():
    from editor.mask_region import remove_region_from_mask

    mask = np.full((20, 24), 255, dtype=np.uint8)
    region = {"lines": [[[2, 3], [7, 3], [7, 8], [2, 8]]]}

    result = remove_region_from_mask(mask, region)

    assert result is not None
    assert np.all(result[3:9, 2:8] == 0)
    assert result[2, 2] == 255
    assert result[3, 8] == 255


def test_delete_region_command_restores_region_and_masks_on_undo():
    from editor.commands import DeleteRegionCommand
    from editor.document_state import DocumentSnapshot
    from editor.editor_model import EditorModel

    old_raw = np.full((20, 24), 255, dtype=np.uint8)
    old_refined = old_raw.copy()
    new_raw = old_raw.copy()
    new_refined = old_refined.copy()
    new_raw[3:9, 2:8] = 0
    new_refined[3:9, 2:8] = 0

    model = EditorModel()
    model.apply_document_snapshot(
        DocumentSnapshot(
            source_path="page.png",
            image=np.zeros((20, 24, 3), dtype=np.uint8),
            regions=[{"lines": [[[2, 3], [7, 3], [7, 8], [2, 8]]]}],
            raw_mask=old_raw,
        )
    )

    command = DeleteRegionCommand(
        model,
        0,
        model.get_region_by_index(0),
        old_raw_mask=old_raw,
        new_raw_mask=new_raw,
        old_refined_mask=old_refined,
        new_refined_mask=new_refined,
    )
    command.redo()
    assert model.get_regions() == []
    assert np.array_equal(model.get_raw_mask(), new_raw)
    assert np.array_equal(model.get_refined_mask(), new_refined)

    command.undo()
    assert len(model.get_regions()) == 1
    assert np.array_equal(model.get_raw_mask(), old_raw)
    assert np.array_equal(model.get_refined_mask(), old_refined)
    assert model.get_selection() == [0]
