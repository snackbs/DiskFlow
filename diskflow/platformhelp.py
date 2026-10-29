"""权限 / 平台辅助：管理员权限检测 + 打开 Windows 管理控制台（不涉及 smartctl）。
"""

from __future__ import annotations

import ctypes
import os
import platform
from pathlib import Path


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


# ---------------------------------------------------------------------------
# 打开 Windows 管理控制台（.msc）
#
# 这两个入口只是「叫系统自己开一个自带工具」，所以我们既不解析它的输出、也不读它的
# 窗口，失败就老老实实把系统给的错误原样报上去（多半是没提权）。
# 放在 platformhelp 而不是 gui：GUI 层不许出现启动外部程序的代码（见
# test_gui_layer_does_not_call_smartctl），这里集中一处，将来加别的系统工具也只改这。
# ---------------------------------------------------------------------------

# 磁盘管理：分区、卷、盘符都在这里；设备管理器：能看到每块盘的驱动与错误状态。
MSC_DISK_MANAGEMENT = "diskmgmt.msc"
MSC_DEVICE_MANAGER = "devmgmt.msc"
MSC_FILENAMES = (MSC_DISK_MANAGEMENT, MSC_DEVICE_MANAGER)


def system32_msc_path(filename: str) -> Path:
    """System32 下某个 .msc 的完整路径（SystemRoot 取不到时退回 C:\\Windows）。"""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return Path(root) / "System32" / filename


def open_management_console(filename: str) -> str:
    """用系统默认方式打开一个 .msc，返回实际传给系统的路径。

    优先传 System32 下的绝对路径：直接写 "diskmgmt.msc" 依赖 Shell 的搜索路径，
    在 PATH 被改过的机器上可能找不到；文件不存在时再退回裸文件名（让 Shell 自己找）。
    """
    if platform.system() != "Windows":
        raise OSError(f"{filename} 只能在 Windows 上打开")
    target = system32_msc_path(filename)
    launch = str(target) if target.is_file() else filename
    os.startfile(launch)  # type: ignore[attr-defined]
    return launch
