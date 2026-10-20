import _bootstrap  # noqa: F401

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_qt_ui import torch_bootstrap


@pytest.fixture
def bootstrap(monkeypatch, tmp_path):
    lib = tmp_path / "torch" / "lib"
    lib.mkdir(parents=True)
    (lib / "c10.dll").touch()
    monkeypatch.setattr(torch_bootstrap, "sys", SimpleNamespace(platform="win32", modules={}))
    monkeypatch.setattr(torch_bootstrap, "_prepared", False)
    monkeypatch.setattr(torch_bootstrap, "_dll_handles", [])
    monkeypatch.setattr(torch_bootstrap.importlib.util, "find_spec", Mock(
        return_value=SimpleNamespace(origin=str(lib.parent / "__init__.py"))
    ))
    monkeypatch.setattr(torch_bootstrap.os, "add_dll_directory", Mock(), raising=False)
    monkeypatch.setattr(torch_bootstrap.ctypes, "CDLL", Mock())
    monkeypatch.setattr(torch_bootstrap.importlib, "import_module", Mock())
    return torch_bootstrap


def test_preload_retains_handles_without_importing_torch(bootstrap):
    bootstrap.prepare_torch_dlls()
    bootstrap.prepare_torch_dlls()

    bootstrap.ctypes.CDLL.assert_called_once()
    bootstrap.os.add_dll_directory.assert_called_once()
    bootstrap.importlib.import_module.assert_not_called()
    assert bootstrap._dll_handles == [
        bootstrap.os.add_dll_directory.return_value,
        bootstrap.ctypes.CDLL.return_value,
    ]


@pytest.mark.parametrize("failure", ["missing_dll", "load_error", "directory_error"])
def test_preload_failure_falls_back_to_full_import(bootstrap, failure, monkeypatch):
    if failure == "missing_dll":
        monkeypatch.setattr(bootstrap.os.path, "isfile", lambda path: False)
    elif failure == "load_error":
        bootstrap.ctypes.CDLL.side_effect = OSError("DLL initialization failed")
    else:
        bootstrap.os.add_dll_directory.side_effect = OSError("DLL directory unavailable")

    bootstrap.prepare_torch_dlls()
    bootstrap.importlib.import_module.assert_called_once_with("torch")


def test_frozen_preload_finds_bundled_dll(bootstrap, tmp_path):
    bundled = tmp_path / "bundle"
    bundled.mkdir()
    (bundled / "c10.dll").touch()
    bootstrap.sys.frozen = True
    bootstrap.sys._MEIPASS = str(bundled)
    bootstrap.importlib.util.find_spec.return_value = None

    bootstrap.prepare_torch_dlls()
    bootstrap.ctypes.CDLL.assert_called_once_with(str(bundled / "c10.dll"))
    bootstrap.importlib.import_module.assert_not_called()


@pytest.mark.parametrize("platform,loaded", [("linux", False), ("darwin", False), ("win32", True)])
def test_non_windows_or_loaded_torch_needs_no_preload(bootstrap, platform, loaded):
    bootstrap.sys.platform = platform
    if loaded:
        bootstrap.sys.modules["torch"] = object()
    bootstrap.prepare_torch_dlls()
    bootstrap.ctypes.CDLL.assert_not_called()
    bootstrap.importlib.import_module.assert_not_called()
