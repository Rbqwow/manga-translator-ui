"""Prepare Windows DLL ordering without importing the inference framework."""

import ctypes
import importlib
import importlib.util
import os
import sys


# Both the DLL and its search directory must survive until process exit.
_dll_handles = []
_prepared = False


def prepare_torch_dlls():
    """Run before Qt; fall back to the original torch-first order if needed.

    Loading c10 before Qt prevents WinError 1114 when a background task later
    imports torch. See https://github.com/pytorch/pytorch/issues/166628.
    """
    global _prepared
    if _prepared or sys.platform != "win32" or "torch" in sys.modules:
        return

    try:
        candidates = []
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            candidates.extend([
                os.path.join(sys._MEIPASS, "torch", "lib", "c10.dll"),
                os.path.join(sys._MEIPASS, "c10.dll"),
            ])
        spec = importlib.util.find_spec("torch")
        if spec is not None and spec.origin:
            candidates.append(os.path.join(os.path.dirname(spec.origin), "lib", "c10.dll"))
        if not candidates:
            return
        dll_path = next((path for path in candidates if os.path.isfile(path)), None)
        if dll_path is None:
            raise FileNotFoundError("Cannot locate torch c10.dll")
        _dll_handles.append(os.add_dll_directory(os.path.dirname(dll_path)))
        _dll_handles.append(ctypes.CDLL(dll_path))
    except (OSError, ImportError, AttributeError, ValueError):
        # Unusual/older installations keep the proven full pre-Qt import path.
        try:
            importlib.import_module("torch")
        except ImportError:
            pass
    _prepared = True
