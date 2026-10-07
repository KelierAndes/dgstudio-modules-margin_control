from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CORE_NAMES = ("DG-LAB-X-VRChat-OSC", "DG-LAB-X-VRChat-OSC-development",
              "DGStudio", "DGStudio-Core")


def _core_root() -> str:
    env = os.environ.get("DGSTUDIO_CORE")
    if env and os.path.isfile(os.path.join(env, "plugins.py")):
        return os.path.abspath(env)
    parent = os.path.dirname(HERE)
    for name in CORE_NAMES:
        cand = os.path.join(parent, name)
        if os.path.isfile(os.path.join(cand, "plugins.py")):
            return cand
    return ""


_CORE = _core_root()
if not _CORE:
    raise RuntimeError(
        "未找到 DGStudio 核心仓库（需含 plugins.py 与 dglab/）。"
        "请将其克隆到本仓库同级目录，或设置环境变量 DGSTUDIO_CORE "
        "指向核心仓库根目录后重试。")
if _CORE not in sys.path:
    sys.path.append(_CORE)
