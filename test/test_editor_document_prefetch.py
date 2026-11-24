import _bootstrap  # noqa: F401, I001

import concurrent.futures
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from editor.document_load_worker import DocumentLoadWorker
from editor.document_state import DocumentSnapshot
from editor.editor_logic import EditorLogic
from editor.core.resource_manager import ResourceManager


def test_neighbor_range_prefers_forward_then_backward_with_radius_two(tmp_path):
    paths = [str(tmp_path / f"page-{index}.png") for index in range(6)]
    logic = EditorLogic.__new__(EditorLogic)
    logic.file_model = SimpleNamespace(
        files=[SimpleNamespace(path=path) for path in paths]
    )

    assert logic._adjacent_image_paths(paths[2]) == [
        paths[3],
        paths[1],
        paths[4],
        paths[0],
    ]


def test_document_worker_prefetches_complete_snapshot_without_changing_current(tmp_path):
    image_path = tmp_path / "page.png"
    Image.new("RGB", (32, 24), (90, 40, 20)).save(image_path)
    manager = ResourceManager()
    logger = logging.getLogger(__name__)
    service = SimpleNamespace(
        controller=SimpleNamespace(),
        logger=logger,
        resource_manager=manager,
        resolve_editor_image_paths=lambda _path: (str(image_path), str(image_path)),
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        result = DocumentLoadWorker(
            service,
            str(image_path),
            executor,
            prefetch=True,
        ).load()

    assert isinstance(result, DocumentSnapshot)
    assert result.source_path == str(image_path)
    assert result.display_image_path == str(image_path)
    assert result.image.size == (32, 24)
    assert result.source_qimage is not None
    assert manager.get_current_image() is None


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        test_neighbor_range_prefers_forward_then_backward_with_radius_two(tmp_path)
        test_document_worker_prefetches_complete_snapshot_without_changing_current(
            tmp_path
        )
    print("2 document prefetch regressions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
