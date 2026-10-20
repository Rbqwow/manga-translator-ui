"""测试公共前置：sys.path、offscreen、torch/PyQt6 加载顺序。

任何要用到 Qt 或本仓代码的测试脚本，**第一句** import 就写：

    import _bootstrap  # noqa: F401

它把三件每个测试都得自己记一遍的事收在一处：

1. ``sys.path`` —— 仓库根 + ``desktop_qt_ui``（不然 ``No module named 'editor'``）；
2. ``QT_QPA_PLATFORM=offscreen`` —— 必须早于任何 PyQt6 导入；
3. Windows 需要在 PyQt6 之前准备 ``c10.dll``。直接先导入 PyQt6、再导入
   torch 可能破坏 DLL 初始化顺序，得到
   ``OSError: [WinError 1114] 动态链接库(DLL)初始化例程失败``。
   正式入口仅预加载 c10，测试公共环境仍完整预载 torch。
   启动与后台导入顺序由独立子进程测试覆盖。参考：
   https://github.com/pytorch/pytorch/issues/166628。

没装 torch 的环境照常跑 —— 预载失败就跳过，不影响纯 Qt 测试。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "desktop_qt_ui"):
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 必须在任何 PyQt6 导入之前；理由见模块 docstring。
try:
    import torch  # noqa: F401
except ImportError:
    pass

__all__ = ["ROOT"]
