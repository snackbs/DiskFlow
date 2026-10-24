"""权限 / 平台辅助：管理员权限检测（不涉及 smartctl）。
"""

from __future__ import annotations

import ctypes
import platform


# ---------------------------------------------------------------------------
# 权限 / 平台辅助
# ---------------------------------------------------------------------------


def is_admin() -> bool:
    """是否以管理员身份运行。非 Windows 返回 False。"""
    if platform.system() != "Windows":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False
