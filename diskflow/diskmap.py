"""盘符映射层：把「物理盘」对应到「本机盘符」（只读查询，失败即降级）。
"""

from __future__ import annotations

import ctypes
import platform
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Sequence


# ---------------------------------------------------------------------------
# 盘符映射层：把「物理盘」对应到「本机盘符」（只读查询，失败即降级）
# ---------------------------------------------------------------------------
# 说明：smartctl 只给 /dev/sdX 这类设备路径，Windows 下拿不到盘号。这里的做法是
#   ① 逐个 \\.\PhysicalDriveN 取 STORAGE_DEVICE_DESCRIPTOR（厂商 + 型号 + 总线类型）
#      和 IOCTL_DISK_GET_DRIVE_GEOMETRY_EX 的总容量 —— 与 smartctl 的 user_capacity 逐字节相等；
#   ② 对每个盘符取 IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS，拿到「盘符 → 物理盘号」；
#   ③ 用「型号 + 容量」把 DiskSnapshot 与物理盘配对，再挂上该盘的盘符。
#   全部调用都不需要管理员权限；任何一步失败都只是「拿不到盘符」，不影响读写量统计。

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
STORAGE_DEVICE_PROPERTY = 0
GEOMETRY_DISK_SIZE_OFFSET = 24      # DISK_GEOMETRY_EX 里 DiskSize 的位置
DESCRIPTOR_BUFFER_SIZE = 1024
DISK_EXTENT_RECORD_SIZE = 24
PHYSICAL_DISK_LIMIT = 32           # 最多探测的 PhysicalDrive 编号
DRIVE_LETTER_JOINER = ", "
DRIVE_LETTER_UNKNOWN = ""
CAPACITY_REL_TOLERANCE = 0.02          # 容量相对容差 2%
CAPACITY_ABS_TOLERANCE = 2 * 1024 ** 3  # 绝对容差 2 GiB
BUS_TYPE_NVME = 17
BUS_TYPE_ATA = (3, 7)
BUS_TYPE_SATA = 12

_DEVICE_API_CACHE: dict[str, Any] = {}


@dataclass
class PhysicalDisk:
    """一块物理盘的只读身份信息（型号/序列号取自描述符，容量取自几何信息）。"""

    number: int
    model: str = ""
    serial: str = ""
    bus_type: int = 0
    capacity_bytes: int | None = None

    @property
    def protocol(self) -> str:
        if self.bus_type == BUS_TYPE_NVME:
            return "NVMe"
        if self.bus_type in BUS_TYPE_ATA or self.bus_type == BUS_TYPE_SATA:
            return "SATA"
        return "未知"

    @property
    def device_path(self) -> str:
        return f"\\\\.\\PhysicalDrive{self.number}"


@dataclass
class VolumeMount:
    """一个盘符：属于哪块物理盘（disk_number）+ 该盘的总容量。"""

    letter: str                     # 形如 "C:"
    disk_number: int | None = None
    capacity_bytes: int | None = None
    model: str = ""
    bus_type: int = 0

    @property
    def protocol(self) -> str:
        if self.bus_type == BUS_TYPE_NVME:
            return "NVMe"
        if self.bus_type in BUS_TYPE_ATA or self.bus_type == BUS_TYPE_SATA:
            return "SATA"
        return "未知"


def _normalize_model_text(text: Any) -> str:
    """型号归一化：只留字母数字，避免空格/多空格/大小写造成误判。"""
    return "".join(ch for ch in str(text or "").upper() if ch.isalnum())


def capacities_close(left: Any, right: Any) -> bool:
    """两个容量是否视为同一块盘：相对 2% 或绝对差 2 GiB 以内。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, int) or not isinstance(right, int) or left <= 0 or right <= 0:
        return False
    diff = abs(left - right)
    return diff <= CAPACITY_ABS_TOLERANCE or diff <= max(left, right) * CAPACITY_REL_TOLERANCE


def models_close(left: Any, right: Any) -> bool:
    """型号是否视为同一块盘：归一化后相等，或一方是另一方的前缀（厂商后缀差异）。"""
    a, b = _normalize_model_text(left), _normalize_model_text(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 8 and long.startswith(short)


def _wchar_buffer_text(raw: bytes, offset: int) -> str:
    """从 Descriptor 缓冲里按「相对偏移」读一个 NUL 结尾的字符串。"""
    if not isinstance(offset, int) or offset <= 0 or offset >= len(raw):
        return ""
    end = raw.find(b"\x00", offset)
    chunk = raw[offset:end if end >= 0 else len(raw)]
    try:
        return chunk.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def parse_storage_descriptor(raw: bytes) -> dict[str, Any]:
    """解析 STORAGE_DEVICE_DESCRIPTOR（纯函数，便于单测）。"""
    import struct

    if not raw or len(raw) < 32:
        return {"model": "", "bus_type": 0, "serial": ""}
    vendor = _wchar_buffer_text(raw, struct.unpack_from("<I", raw, 12)[0])
    product = _wchar_buffer_text(raw, struct.unpack_from("<I", raw, 16)[0])
    serial = _wchar_buffer_text(raw, struct.unpack_from("<I", raw, 24)[0])
    bus_type = struct.unpack_from("<I", raw, 28)[0]
    model = " ".join(part for part in (vendor.strip(), product.strip()) if part).strip()
    return {"model": " ".join(model.split()), "bus_type": bus_type, "serial": serial}


def parse_disk_geometry(raw: bytes) -> int | None:
    """解析 DISK_GEOMETRY_EX，返回总容量（字节）。"""
    import struct

    if not raw or len(raw) < GEOMETRY_DISK_SIZE_OFFSET + 8:
        return None
    size = struct.unpack_from("<q", raw, GEOMETRY_DISK_SIZE_OFFSET)[0]
    return int(size) if size > 0 else None


def parse_volume_extents(raw: bytes) -> list[int]:
    """解析 VOLUME_DISK_EXTENTS，返回该卷涉及的物理盘编号（去重保序）。

    结构：NumberOfDiskExtents(u32) + 对齐(4) + DISK_EXTENT[24] 数组，
    DISK_EXTENT = DiskNumber(u32) + 保留(4) + StartingOffset(i64) + ExtentLength(i64)。
    """
    import struct

    if not raw or len(raw) < 8:
        return []
    count = struct.unpack_from("<I", raw, 0)[0]
    numbers: list[int] = []
    for index in range(min(count, 64)):
        base = 8 + index * DISK_EXTENT_RECORD_SIZE
        if base + 4 > len(raw):
            break
        number = struct.unpack_from("<I", raw, base)[0]
        if number not in numbers:
            numbers.append(number)
    return numbers


def _win_device_api() -> tuple[Any, Any, Any] | None:
    """取 (CreateFileW, DeviceIoControl, CloseHandle)；非 Windows / 失败返回 None。"""
    if platform.system() != "Windows":
        return None
    cached = _DEVICE_API_CACHE.get("api")
    if cached is not None:
        return cached
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
        create_file.restype = ctypes.c_void_p
        device_io = kernel32.DeviceIoControl
        device_io.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                              ctypes.c_void_p]
        device_io.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = wintypes.BOOL
    except Exception:
        return None
    api = (create_file, device_io, close_handle)
    _DEVICE_API_CACHE["api"] = api
    return api


def _device_ioctl(path: str, code: int, payload: bytes, size: int) -> bytes | None:
    """对设备路径发一次 DeviceIoControl，返回输出缓冲（失败返回 None，不抛异常）。"""
    api = _win_device_api()
    if api is None:
        return None
    create_file, device_io, close_handle = api
    try:
        import ctypes
        from ctypes import wintypes

        handle = create_file(path, 0, 3, None, 3, 0, None)   # 0 = 不要求读写权限
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        try:
            in_buf = ctypes.create_string_buffer(payload, max(len(payload), 1)) if payload else None
            out_buf = ctypes.create_string_buffer(size)
            returned = wintypes.DWORD(0)
            ok = device_io(handle, code,
                           ctypes.cast(in_buf, ctypes.c_void_p) if in_buf is not None else None,
                           len(payload), out_buf, size, ctypes.byref(returned), None)
            if not ok:
                return None
            return out_buf.raw[:returned.value]
        finally:
            close_handle(handle)
    except Exception:
        return None


def _query_descriptor(path: str) -> dict[str, Any]:
    import struct

    payload = struct.pack("<I", STORAGE_DEVICE_PROPERTY).ljust(12, b"\x00")
    raw = _device_ioctl(path, IOCTL_STORAGE_QUERY_PROPERTY, payload, DESCRIPTOR_BUFFER_SIZE)
    if raw is None:
        return {"model": "", "bus_type": 0, "serial": ""}
    return parse_storage_descriptor(raw)


def query_volume_identity(letter: str) -> dict[str, Any] | None:
    """取某个盘符的型号与总线类型；失败返回 None。"""
    if platform.system() != "Windows":
        return None
    return _query_descriptor("\\\\?\\" + letter)


def query_disk_descriptor(number: int) -> dict[str, Any] | None:
    """取 \\\\.\\PhysicalDriveN 的型号/序列号/总线类型；失败返回 None。"""
    if platform.system() != "Windows":
        return None
    return _query_descriptor(f"\\\\.\\PhysicalDrive{number}")


def query_volume_disks(letter: str) -> list[int]:
    """取盘符涉及的物理盘编号；失败返回空列表。"""
    if platform.system() != "Windows":
        return []
    raw = _device_ioctl("\\\\?\\" + letter, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                        b"", 1024)
    return parse_volume_extents(raw or b"")


def volume_capacity(letter: str) -> int | None:
    """取盘符的总容量（字节）；失败返回 None。"""
    try:
        usage = shutil.disk_usage(letter + "\\")
    except (OSError, ValueError):
        return None
    total = getattr(usage, "total", None)
    return int(total) if isinstance(total, int) and total > 0 else None


def list_physical_disks(limit: int = PHYSICAL_DISK_LIMIT) -> list[PhysicalDisk]:
    """枚举物理盘（型号 + 序列号 + 总线 + 精确容量）；非 Windows / 全失败返回空列表。"""
    if platform.system() != "Windows":
        return []
    disks: list[PhysicalDisk] = []
    for number in range(max(int(limit), 1)):
        identity = query_disk_descriptor(number)
        geometry = _device_ioctl(f"\\\\.\\PhysicalDrive{number}",
                                 IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, b"", 256)
        capacity = parse_disk_geometry(geometry or b"")
        if identity is None and capacity is None:
            continue                       # 这个编号没有盘（或打不开），继续往下试
        identity = identity or {}
        disks.append(PhysicalDisk(
            number=number,
            model=str(identity.get("model") or ""),
            serial=str(identity.get("serial") or ""),
            bus_type=int(identity.get("bus_type") or 0),
            capacity_bytes=capacity,
        ))
    return disks


def list_volume_mounts() -> list[VolumeMount]:
    """枚举本机盘符及其所属物理盘；非 Windows / 全失败返回空列表。"""
    if platform.system() != "Windows":
        return []
    try:
        import ctypes

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())  # type: ignore[attr-defined]
    except Exception:
        return []
    mounts: list[VolumeMount] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        letter = f"{chr(ord('A') + index)}:"
        try:
            drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(letter + "\\"))  # type: ignore[attr-defined]
        except Exception:
            drive_type = 0
        if drive_type not in (2, 3):      # 2=可移动 3=固定磁盘（网络盘/光驱不参与）
            continue
        disks = query_volume_disks(letter)
        mounts.append(VolumeMount(
            letter=letter,
            disk_number=disks[0] if disks else None,
            capacity_bytes=volume_capacity(letter),
        ))
    mounts.sort(key=lambda item: item.letter)
    return mounts


def score_disk_match(snapshot: Any, disk: PhysicalDisk) -> int | None:
    """给「快照 × 物理盘」打分：None = 不匹配，数字越大越可信。"""
    if not models_close(getattr(snapshot, "model", ""), disk.model):
        return None
    score = 10
    capacity = getattr(snapshot, "capacity_bytes", None)
    if capacities_close(capacity, disk.capacity_bytes):
        score += 5
    elif capacity is not None and disk.capacity_bytes is not None:
        return None                     # 型号撞了但容量差太多，肯定是另一块盘
    protocol = str(getattr(snapshot, "protocol", "") or "")
    if protocol and disk.protocol != "未知":
        if protocol == disk.protocol:
            score += 3
        else:
            return None
    return score


def drive_letters_by_disk(mounts: Sequence[VolumeMount]) -> dict[int, str]:
    """盘号 -> "C:, D:"（一块盘分多个区时全部列出，按字母排序）。"""
    grouped: dict[int, list[str]] = {}
    for mount in mounts:
        if mount.disk_number is None:
            continue
        grouped.setdefault(int(mount.disk_number), []).append(mount.letter)
    return {number: DRIVE_LETTER_JOINER.join(sorted(set(letters)))
            for number, letters in grouped.items()}


def match_drive_letters(snapshots: Sequence[Any],
                        disks: Sequence[PhysicalDisk],
                        mounts: Sequence[VolumeMount]) -> dict[str, str]:
    """把盘符挂到快照上，返回 {disk_key: "C:, D:"}。匹配不上的快照不出现在结果里。"""
    letters = drive_letters_by_disk(mounts)
    result: dict[str, str] = {}
    for snapshot in snapshots:
        if not getattr(snapshot, "ok", True):
            continue
        scored = [(score, disk) for disk in disks
                  for score in [score_disk_match(snapshot, disk)] if score is not None]
        if not scored:
            continue
        best_score = max(score for score, _disk in scored)
        best = [disk for score, disk in scored if score == best_score]
        if len(best) != 1:
            continue                    # 并列（同型号同容量两块盘）时宁可不给
        text = letters.get(best[0].number, DRIVE_LETTER_UNKNOWN)
        key = str(getattr(snapshot, "key", "") or "")
        if key and text:
            result[key] = text
    return result


def locate_drive_letters(snapshots: Sequence[Any],
                         disks: Sequence[PhysicalDisk] | None = None,
                         mounts: Sequence[VolumeMount] | None = None) -> dict[str, str]:
    """真实环境入口：枚举物理盘与盘符并配对；任何异常都退化为空映射。"""
    try:
        resolved_disks = list(disks) if disks is not None else list_physical_disks()
        resolved_mounts = list(mounts) if mounts is not None else list_volume_mounts()
    except Exception:
        return {}
    try:
        return match_drive_letters(snapshots, resolved_disks, resolved_mounts)
    except Exception:
        return {}


def attach_drive_letters(snapshots: Sequence[Any],
                         resolver: Callable[[Sequence[Any]], dict[str, str]] | None = None
                         ) -> dict[str, str]:
    """给每块盘填上 drives 字段（拿不到就留空，绝不抛异常）。"""
    try:
        mapping = (resolver or locate_drive_letters)(snapshots) or {}
    except Exception:
        mapping = {}
    for snapshot in snapshots:
        try:
            setattr(snapshot, "drives", mapping.get(str(getattr(snapshot, "key", "")), ""))
        except Exception:
            pass
    return mapping
