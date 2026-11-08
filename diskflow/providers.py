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
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .config import APP_ICON_DIRNAME, APP_ICON_FILENAME, PROJECT_DIR, SMARTCTL_EXE
from .i18n import tr
from .models import DiskSnapshot, ProviderResult
from .parse import parse_smartctl_payload


# ---------------------------------------------------------------------------
# 数据读取层：provider 接口 + 真实实现 + fixture 实现
# ---------------------------------------------------------------------------


# 单块盘的 smartctl 超时，以及整次读取的总预算。
#
# 为什么需要「总预算」：--scan-open 与每块盘的 -x -a 是串行的，异常盘（USB 桥、
# 已休眠的外置盘）会一直挂到单盘超时才返回。盘一多，最坏耗时就是 N × 单盘超时，
# 窗口会长时间停在「读取中…」，用户只能强杀进程。总预算到点后不再启动新的盘，
# 直接给出明确的失败行，保证界面一定能恢复。
PER_DEVICE_TIMEOUT_SECONDS = 25.0
TOTAL_READ_BUDGET_SECONDS = 90.0
# 剩余预算低于这个值时不再把剩余全给单块盘：给太少等于必然超时，
# 不如按这个下限再试一次（超了也只是多等这么久，仍有单盘超时兜底）。
MIN_DEVICE_TIMEOUT_SECONDS = 3.0


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


def _default_run_command(args: Sequence[str],
                         timeout: float = PER_DEVICE_TIMEOUT_SECONDS) -> tuple[int, str, str]:
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
                 run_command: Callable[[Sequence[str]], tuple[int, str, str]] | None = None,
                 total_timeout: float | None = TOTAL_READ_BUDGET_SECONDS,
                 per_device_timeout: float = PER_DEVICE_TIMEOUT_SECONDS):
        self._smartctl_path = smartctl_path
        self._run = run_command or _default_run_command
        # None 或 <=0 表示不限总预算（测试与特殊场景用）
        self._total_timeout = total_timeout
        self._per_device_timeout = per_device_timeout

    def _invoke(self, args: Sequence[str], timeout: float | None = None) -> tuple[int, str, str]:
        """调用注入的命令执行器。

        注入的替身只接受一个参数（这是历史契约），所以只有在用默认执行器时
        才把 timeout 传下去；替身自己决定耗时，由 read_all 的预算在盘与盘之间
        把关（超时的替身最多拖一块盘那么久）。
        """
        if timeout is not None and self._run is _default_run_command:
            return self._run(args, timeout=timeout)
        return self._run(args)

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
            code, out, _err = self._invoke([path, "--scan-open", "--json"],
                                           timeout=self._per_device_timeout)
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

    def read_device(self, device_path: str, timeout: float | None = None) -> DiskSnapshot:
        path = self.resolve_path()
        if not path:
            return DiskSnapshot(device_path=device_path, error="读取失败：未找到 smartctl.exe")
        try:
            # -x 追加 Device Statistics（Logical Sectors Read/Written），是累计读写量的权威来源；
            # 缺少 -x 时只能退回 SMART 属性 241/242，部分主控的单位不是 512 字节，会严重偏小。
            _code, out, err = self._invoke([path, "-x", "-a", "--json", device_path],
                                           timeout=timeout or self._per_device_timeout)
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
        snapshots = self._read_devices(devices)
        return ProviderResult(
            snapshots=snapshots, smartctl_path=path,
            status=tr("已读取 {count} 块磁盘（smartctl: {path}）",
                      count=len(snapshots), path=path))

    def _read_devices(self, devices: Sequence[str]) -> list[DiskSnapshot]:
        """逐块读取，但受整次读取的总预算约束（P2-5）。

        预算到点后不再启动新的盘，而是给一行明确的「已跳过」失败记录：
        用户看到的是「哪几块盘没读到、为什么」，而不是窗口一直转圈。
        正在跑的那一块不会被中断（subprocess 没法安全跨线程喊停），
        所以最坏超出预算是「一块盘的单盘超时」，有上界。
        """
        budget = self._total_timeout
        deadline = None
        if budget is not None and float(budget) > 0:
            deadline = time.monotonic() + float(budget)
        snapshots: list[DiskSnapshot] = []
        for device in devices:
            timeout = self._per_device_timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    snapshots.append(DiskSnapshot(
                        device_path=device,
                        error=tr("读取失败：本次读取总预算已用尽，已跳过该磁盘")))
                    continue
                timeout = min(timeout, max(MIN_DEVICE_TIMEOUT_SECONDS, remaining))
            snapshots.append(self.read_device(device, timeout=timeout))
        return snapshots


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
