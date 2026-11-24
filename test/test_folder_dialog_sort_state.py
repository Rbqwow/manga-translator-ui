import os

import _bootstrap  # noqa: F401
from PyQt6.QtGui import QFileSystemModel
from ui.secondary_pages.folder_dialog import CaseInsensitiveSortProxyModel


def test_folder_dialog_modified_time_sort_uses_timestamp(tmp_path):
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    older.mkdir()
    newer.mkdir()
    older_timestamp = 1_754_107_200  # 2025-08-02 00:00:00 UTC
    newer_timestamp = 1_755_057_600  # 2025-08-13 00:00:00 UTC

    os.utime(older, (older_timestamp, older_timestamp))
    os.utime(newer, (newer_timestamp, newer_timestamp))

    source_model = QFileSystemModel()
    source_model.setRootPath(str(tmp_path))
    proxy_model = CaseInsensitiveSortProxyModel()
    proxy_model.setSourceModel(source_model)

    older_index = source_model.index(str(older), 3)
    newer_index = source_model.index(str(newer), 3)

    assert older_index.isValid()
    assert newer_index.isValid()
    assert proxy_model.lessThan(older_index, newer_index)
    assert not proxy_model.lessThan(newer_index, older_index)
