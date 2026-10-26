"""数据读取层第二步：SmartProvider 注入点。

SmartctlProvider 是默认实现（真机调用 smartctl），FixturesProvider 是测试实现
（读 fixtures/ 下的 JSON 样本）。**所有 smartctl 调用只出现在本模块**，
GUI 层只拿到 DiskSnapshot 列表，便于在无真实硬盘的环境下测试。
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .config import APP_ICON_DIRNAME, APP_ICON_FILENAME, PROJECT_DIR, SMARTCTL_EXE
from .i18n import tr
from .models import DiskSnapshot, ProviderResult
from .parse import parse_smartctl_payload


# ---------------------------------------------------------------------------
# 数据读取层：provider 接口 + 真实实现 + fixture 实现
# ---------------------------------------------------------------------------


def locate_smartctl(extra_candidates: Iterable[str | None] = ()) -> str | None:
    """查找 smartctl.exe。

    顺序：sys._MEIPASS（打包资源目录）-> exe 所在目录 -> PATH -> 官方默认安装路径。
    """
    names = (SMARTCTL_EXE, "smartctl")
    candidates: list[str] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.extend(str(Path(meipass) / n) for n in names)

    try:
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(str(exe_dir / n) for n in names)
    except (OSError, ValueError):
        pass

    for part in os.environ.get("PATH", "").split(os.pathsep):
        part = part.strip().strip('"')
        if not part:
            continue
        candidates.extend(str(Path(part) / n) for n in names)

    candidates.append(r"C:\Program Files\smartmontools\bin\smartctl.exe")
    candidates.append(r"C:\Program Files (x86)\smartmontools\bin\smartctl.exe")

    for extra in extra_candidates:
        if extra:
            candidates.append(str(extra))

    for candidate in candidates:
        try:
            if candidate and os.path.isfile(candidate):
                return candidate
        except OSError:
            continue
    return None


def _default_run_command(args: Sequence[str], timeout: float = 25.0) -> tuple[int, str, str]:
    """执行命令并返回 (returncode, stdout, stderr)。Windows 下隐藏控制台窗口。"""
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    proc = subprocess.run(list(args), **kwargs)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, out, err


def locate_app_icon(extra_candidates: Iterable[str | None] = ()) -> str | None:
    """查找应用图标 diskflow.ico。

    顺序与 smartctl 保持一致：sys._MEIPASS（打包资源目录）-> exe 所在目录
    -> 源码目录 -> 源码目录下的 diskflow-icons/。
    找不到返回 None：界面照常启动，只是窗口图标退回系统默认，不报错。
    """
    candidates: list[str] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(str(Path(meipass) / APP_ICON_FILENAME))

    try:
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(str(exe_dir / APP_ICON_FILENAME))
    except (OSError, ValueError):
        pass

    try:
        source_dir = PROJECT_DIR
        candidates.append(str(source_dir / APP_ICON_FILENAME))
        candidates.append(str(source_dir / APP_ICON_DIRNAME / APP_ICON_FILENAME))
    except (OSError, ValueError):
        pass

    for extra in extra_candidates:
        if extra:
            candidates.append(str(extra))

    for candidate in candidates:
        try:
            if candidate and os.path.isfile(candidate):
                return candidate
        except OSError:
            continue
    return None

class SmartctlProvider:
    """默认实现：真实调用 smartctl。所有 smartctl 调用只出现在这一层。"""

    def __init__(self, smartctl_path: str | None = None,
                 run_command: Callable[[Sequence[str]], tuple[int, str, str]] | None = None):
        self._smartctl_path = smartctl_path
        self._run = run_command or _default_run_command

    def resolve_path(self) -> str | None:
        if self._smartctl_path:
            return self._smartctl_path
        self._smartctl_path = locate_smartctl()
        return self._smartctl_path

    def scan_devices(self) -> list[str]:
        path = self.resolve_path()
        if not path:
            return []
        try:
            code, out, _err = self._run([path, "--scan-open", "--json"])
        except (OSError, subprocess.SubprocessError):
            return []
        try:
            payload = json.loads(out or "{}")
        except json.JSONDecodeError:
            return []
        devices = []
        for entry in payload.get("devices") or []:
            if isinstance(entry, dict) and entry.get("name"):
                devices.append(str(entry["name"]))
        return devices

    def read_device(self, device_path: str) -> DiskSnapshot:
        path = self.resolve_path()
        if not path:
            return DiskSnapshot(device_path=device_path, error="读取失败：未找到 smartctl.exe")
        try:
            # -x 追加 Device Statistics（Logical Sectors Read/Written），是累计读写量的权威来源；
            # 缺少 -x 时只能退回 SMART 属性 241/242，部分主控的单位不是 512 字节，会严重偏小。
            _code, out, err = self._run([path, "-x", "-a", "--json", device_path])
        except subprocess.TimeoutExpired:
            return DiskSnapshot(device_path=device_path, error="读取失败：smartctl 执行超时")
        except (OSError, subprocess.SubprocessError) as exc:
            return DiskSnapshot(device_path=device_path, error=f"读取失败：{exc}")
        try:
            payload = json.loads(out or "{}")
        except json.JSONDecodeError:
            return DiskSnapshot(device_path=device_path,
                                error=f"读取失败：smartctl 输出无法解析 {err.strip()[:120]}".strip())
        return parse_smartctl_payload(payload, device_path=device_path)

    def read_all(self) -> ProviderResult:
        path = self.resolve_path()
        if not path:
            return ProviderResult(
                smartctl_path=None, ok=False,
                status=tr("未找到 smartctl.exe（可执行 winget install smartmontools.smartmontools）"))
        devices = self.scan_devices()
        if not devices:
            return ProviderResult(smartctl_path=path, ok=False,
                                  status=tr("未发现可用磁盘（可能需要管理员权限）"))
        snapshots = [self.read_device(device) for device in devices]
        return ProviderResult(
            snapshots=snapshots, smartctl_path=path,
            status=tr("已读取 {count} 块磁盘（smartctl: {path}）",
                      count=len(snapshots), path=path))


class FixturesProvider:
    """测试用实现：从 fixtures 目录读取 smartctl JSON 样本，完全不碰真实磁盘。"""

    def __init__(self, fixtures_dir: str | os.PathLike[str],
                 pattern: str = "smartctl_*.json",
                 run_command: Callable[[Sequence[str]], tuple[int, str, str]] | None = None):
        self.fixtures_dir = Path(fixtures_dir)
        self.pattern = pattern

    def fixture_files(self) -> list[Path]:
        if not self.fixtures_dir.is_dir():
            return []
        return sorted(p for p in self.fixtures_dir.glob(self.pattern) if p.is_file())

    def read_all(self) -> ProviderResult:
        snapshots: list[DiskSnapshot] = []
        for path in self.fixture_files():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                snapshots.append(DiskSnapshot(device_path=path.name,
                                              error=f"读取失败：样本损坏 {exc}"))
                continue
            mock = payload.get("mock") if isinstance(payload, dict) else None
            device_path = ""
            if isinstance(mock, dict):
                device_path = str(mock.get("device_path") or "")
            snapshots.append(parse_smartctl_payload(payload, device_path=device_path))
        status = f"fixture provider: {self.fixtures_dir.name} ({len(snapshots)} samples)"
        return ProviderResult(snapshots=snapshots, smartctl_path=None,
                              status=status, ok=bool(snapshots))
