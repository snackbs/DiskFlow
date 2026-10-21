"""磁盘流水 DiskFlow —— Windows 磁盘读写量记录与增量对比小工具。

设计要点
--------
1. 分层：数据读取层（SmartProvider 注入点）/ 历史存储层 / 增量计算层 / GUI 层。
   数据读取层与 GUI 层之间只通过 DiskSnapshot 数据结构通信，GUI 里不存在任何
   smartctl 调用代码，便于用 fixture provider 在无真实硬盘的环境下测试。
2. 名称集中配置：窗口标题、PyInstaller --name、artifact 名一律引用本文件顶部常量。
3. 历史目录使用固定内部 ID（APP_STORAGE_ID），显示名改名不影响历史数据。
4. 版本号集中在 APP_VERSION，「关于」对话框只引用它，不重复写死。
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ---------------------------------------------------------------------------
# 【单一配置点】软件名称。用户选定正式名称后，只改这三行 + APP_STORAGE_ID 说明即可。
# ---------------------------------------------------------------------------
APP_DISPLAY_NAME = "磁盘流水 DiskFlow"             # 窗口标题/界面用（已定名）
APP_EXE_NAME = "DiskFlow"                          # --name / exe 文件名 / artifact 名
APP_ARTIFACT_NAME = "DiskFlow-windows"             # GitHub Actions artifact 名
APP_STORAGE_ID = "DiskUsageTracker"                # 历史目录固定 ID，改名时【不要】跟着改
APP_VERSION = "0.2.0"                              # 版本号（关于框显示；发版 tag 必须与之一致）

# 容量换算常量
LBA_BYTES = 512                  # SATA: 1 LBA = 512 字节
NVME_DATA_UNIT_BYTES = 512000    # NVMe: 1 Data Unit = 1000 x 512 字节
BYTES_PER_GB = 1024 ** 3         # 界面统一 1 GB = 1024^3 字节
BYTES_PER_GIB_TEXT = "1 GB = 1024^3 字节"
BYTES_PER_TB = 1024 ** 4         # 1 TB = 1024 GB（累计量双单位显示用）

# SATA 累计读写量的来源优先级
#   Device Statistics 的 Logical Sectors 恒为 512 字节扇区，是权威来源（需要 smartctl -x）
#   SMART 属性 241/242（Total_LBAs_*）的计数单位随主控而异：多数按 512 字节 LBA 计数，
#   部分主控（Silicon Motion / Toshiba 等）按 16 MiB / 32 MiB 块计数，直接用会低几个数量级。
#   因此属性值只作为 Device Statistics 缺失时的兜底，并在界面上提示"可能偏小"。
ATA_DEVSTAT_READ_NAME = "logical sectors read"
ATA_DEVSTAT_WRITE_NAME = "logical sectors written"
COUNTER_SOURCE_NONE = ""
COUNTER_SOURCE_NVME = "NvmeLog"
COUNTER_SOURCE_DEVSTAT = "DeviceStatistics"
COUNTER_SOURCE_ATTR = "SmartAttributes"
COUNTER_SOURCE_TEXT = {
    COUNTER_SOURCE_NVME: "NVMe Data Units",
    COUNTER_SOURCE_DEVSTAT: "Device Statistics（Logical Sectors）",
    COUNTER_SOURCE_ATTR: "SMART 属性 241/242（按 512 字节/LBA）",
}

# 健康度：只显示硬盘自己报告的剩余寿命，绝不估算、绝不用其它指标代替。
#   NVMe: 100 - percentage_used
#   SATA: 按下列优先级取厂商写下的剩余寿命百分比，全都没有则显示"不适用"
SATA_LIFE_ATTRIBUTES = (231, 202, 177, 233, 169)
SATA_LIFE_RAW_PERCENT = (231, 202, 233, 169)   # 这些属性的 raw.value 通常是剩余寿命 %
# 177 Wear_Leveling_Count 的 raw 是磨损循环次数，只有归一化 value 才是剩余百分比
HEALTH_NOT_AVAILABLE = "不适用"

SMARTCTL_EXE = "smartctl.exe"


def about_text(history_path: Any = None) -> str:
    """「关于」对话框正文。

    版本号只引用 APP_VERSION，界面里不再出现任何写死的版本字面量；
    发版时改一处常量即可，避免关于框与实际 exe 版本对不上。
    """
    path_text = str(history_path) if history_path else "启动后自动确定"
    return (
        f"{APP_DISPLAY_NAME}\n"
        f"版本：v{APP_VERSION}\n"
        f"可执行文件：{APP_EXE_NAME}.exe\n"
        "\n"
        "记录本机各物理磁盘的累计读取量与累计写入量，并在每次打开时与上一次记录对比，\n"
        "给出本次区间增量与历史累计增量；同时监测 NVMe 的 0E（介质与数据完整性错误）。\n"
        "\n"
        "数据来源：smartctl -x -a --json（smartmontools），随 exe 一同打包\n"
        f"容量换算：{BYTES_PER_GIB_TEXT}；1 LBA = 512 字节，1 NVMe Data Unit = 512000 字节\n"
        "累计读写：优先取 smartctl 的 Device Statistics（Logical Sectors，恒为 512 字节扇区），\n"
        "          该字段缺失时才退回 SMART 属性 241/242；累计量超过 1 TB 时同时显示 TB 与 GB。\n"
        "健康度：只显示硬盘自报的剩余寿命（NVMe 100 − 已用百分比；\n"
        "        SATA 依次取 231/202/177/233/169 属性），没有该字段显示「不适用」，不做推算。\n"
        f"历史文件：{path_text}\n"
        f"历史目录 ID：{APP_STORAGE_ID}（显示名改名不影响历史数据）\n"
        "\n"
        "本程序为单窗口工具，关闭即退出，不驻留后台、不写系统服务、不设置开机自启。\n"
        "磁盘读取通常需要管理员权限，未提权时部分磁盘可能读取失败。"
    )


# ---------------------------------------------------------------------------
# 容量换算层
# ---------------------------------------------------------------------------


def bytes_to_gb(value: Any) -> float | None:
    """字节 -> GB（1024^3）。None/非法值返回 None。"""
    number = _as_int(value)
    if number is None:
        return None
    return number / BYTES_PER_GB


def lbas_to_gb(value: Any) -> float | None:
    """SATA LBA 计数 -> GB。1 LBA = 512 字节。"""
    number = _as_int(value)
    if number is None:
        return None
    return number * LBA_BYTES / BYTES_PER_GB


def data_units_to_gb(value: Any) -> float | None:
    """NVMe Data Unit 计数 -> GB。1 Data Unit = 512000 字节。"""
    number = _as_int(value)
    if number is None:
        return None
    return number * NVME_DATA_UNIT_BYTES / BYTES_PER_GB


def _as_int(value: Any) -> int | None:
    """把 smartctl 里的值（可能是 int / 字符串 / "1234 (1.2 TB)" 形式）转成 int。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        digits = ""
        for ch in text:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def fmt_gb(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def fmt_delta(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:,.{digits}f}"


def fmt_size_gb(value: float | None, digits: int = 2) -> str:
    """累计量双单位显示：>= 1 TB 时同时给出 TB 与 GB，否则只给 GB。

    例：32567.42 -> "31.80 TB (32,567.42 GB)"；512 -> "512.00 GB"。
    """
    if value is None:
        return "—"
    if abs(value) >= 1024.0:
        return f"{value / 1024.0:,.{digits}f} TB ({value:,.{digits}f} GB)"
    return f"{value:,.{digits}f} GB"


# ---------------------------------------------------------------------------
# 时间格式化层
# ---------------------------------------------------------------------------


def format_duration(total_seconds: float) -> str:
    """秒 -> 'X 天 Y 小时 Z 分钟'。传入负数时按绝对值格式化。"""
    seconds = int(abs(float(total_seconds)))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days} 天 {hours} 小时 {minutes} 分钟"


def format_interval(seconds: float | None) -> str:
    """距上次的时间间隔。None -> '—'；时钟回拨（负数）显式标注。"""
    if seconds is None:
        return "—"
    if seconds < 0:
        return f"时钟回拨 {format_duration(-float(seconds))}"
    return format_duration(seconds)


def parse_timestamp(text: Any) -> datetime | None:
    """解析 ISO 8601 时间戳，兼容末尾 Z。失败返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def timestamp_seconds(value: datetime | str | None) -> float | None:
    """把 datetime / ISO 字符串转成可比较的 epoch 秒（naive 视为本地时间）。"""
    dt = value if isinstance(value, datetime) else parse_timestamp(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.timestamp()
    return dt.timestamp()


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class DiskSnapshot:
    """单块盘的一次读数（数据读取层的输出，GUI 只消费它）。"""

    device_path: str
    protocol: str = "未知"
    model: str = ""
    serial: str = ""
    capacity_bytes: int | None = None
    read_bytes: int | None = None
    write_bytes: int | None = None
    media_errors: int | None = None
    reallocated: int | None = None
    pending: int | None = None
    life_percent: int | None = None    # 硬盘自报的剩余寿命百分比，None = 未提供
    counter_source: str = ""           # 累计读写量取自哪个字段（见 COUNTER_SOURCE_*）
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def key(self) -> str:
        return disk_key(self.model, self.serial, self.device_path)

    @property
    def read_gb(self) -> float | None:
        return bytes_to_gb(self.read_bytes)

    @property
    def write_gb(self) -> float | None:
        return bytes_to_gb(self.write_bytes)

    @property
    def health_text(self) -> str:
        """健康度显示文本：只反映硬盘自报值，读不到就是"不适用"。"""
        if not self.ok:
            return "—"
        if self.life_percent is None:
            return HEALTH_NOT_AVAILABLE
        return f"{self.life_percent}%"


def disk_key(model: str, serial: str, device_path: str = "") -> str:
    """跨次匹配键：型号 + 序列号（绝不使用盘符/设备路径）。"""
    model_norm = (model or "").strip().upper()
    serial_norm = (serial or "").strip().upper()
    if model_norm or serial_norm:
        return f"{model_norm}|{serial_norm}"
    return f"UNKNOWN|{(device_path or '').strip().upper()}"


@dataclass
class ProviderResult:
    snapshots: list[DiskSnapshot] = field(default_factory=list)
    smartctl_path: str | None = None
    status: str = "未读取"
    ok: bool = True


@dataclass
class MediaErrorStatus:
    text: str
    kind: str          # "stable" | "increased" | "first_seen" | "na" | "unavailable" | "reset"
    delta: int = 0

    @property
    def is_alert(self) -> bool:
        return self.kind in ("increased", "first_seen")


@dataclass
class DiskRow:
    key: str
    model: str
    serial: str
    protocol: str
    device_path: str
    read_gb: float | None = None
    write_gb: float | None = None
    interval_text: str = "—"
    read_delta_gb: float | None = None
    write_delta_gb: float | None = None
    hist_read_delta_gb: float | None = None
    hist_write_delta_gb: float | None = None
    health_text: str = "—"     # 健康度列：硬盘自报剩余寿命，读不到显示"不适用"
    media_text: str = "—"
    media_kind: str = "unavailable"
    status_text: str = ""
    alert: bool = False       # 0E 新增 -> 整行标红
    failed: bool = False      # 读取失败 -> 整行标灰
    vanished: bool = False    # 历史有、本次没有


# ---------------------------------------------------------------------------
# 数据读取层：smartctl JSON 解析（纯函数，便于单测）
# ---------------------------------------------------------------------------


def _ata_attribute(table: list[dict], attr_id: int) -> int | None:
    for entry in table or []:
        if not isinstance(entry, dict):
            continue
        if _as_int(entry.get("id")) == attr_id:
            raw = entry.get("raw") or {}
            value = _as_int(raw.get("value"))
            if value is None:
                value = _as_int(raw.get("string"))
            return value
    return None


def _ata_normalized(table: list[dict], attr_id: int) -> int | None:
    """取 SMART 属性的归一化值（0-100），177 等属性的剩余寿命只存在这里。"""
    for entry in table or []:
        if not isinstance(entry, dict):
            continue
        if _as_int(entry.get("id")) == attr_id:
            return _as_int(entry.get("value"))
    return None


def _device_statistic(payload: dict, name: str) -> int | None:
    """从 ata_device_statistics 里按（小写）名称取一项统计值。

    smartctl -x 输出形如：
        "ata_device_statistics": {"pages": [{"number": 1, "name": "General Statistics",
            "table": [{"offset": 8, "name": "Logical Sectors Read", "value": 123, ...}]}]}
    这些计数值恒为 512 字节扇区，与主控无关。
    """
    block = payload.get("ata_device_statistics")
    if not isinstance(block, dict):
        return None
    target = name.strip().lower()
    for page in block.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for entry in page.get("table") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name") or "").strip().lower() == target:
                value = _as_int(entry.get("value"))
                if value is None:
                    value = _as_int(entry.get("string"))
                return value
    return None


def _sata_life_percent(table: list[dict]) -> int | None:
    """SATA 盘自报的剩余寿命百分比；没有任何厂商属性时返回 None（界面显示"不适用"）。

    177 Wear_Leveling_Count 的 raw 是磨损循环次数，只有归一化 value 才是剩余寿命；
    其余（231/202/233/169）的 raw 通常就是剩余百分比，但个别厂商会放进别的单位，
    所以只在 0..100 之间才采信，否则退回归一化值。
    """
    for attr_id in SATA_LIFE_ATTRIBUTES:
        normalized = _ata_normalized(table, attr_id)
        if attr_id in SATA_LIFE_RAW_PERCENT:
            raw = _ata_attribute(table, attr_id)
            if raw is not None and 0 <= raw <= 100:
                return int(raw)
        if normalized is not None and 0 <= normalized <= 100:
            return int(normalized)
    return None


def _normalize_protocol(raw: Any, payload: dict) -> str:
    text = str(raw or "").strip().lower()
    if "nvme" in text:
        return "NVMe"
    if text in ("ata", "sata", "sat", "scsi") or text.startswith("ata"):
        return "SATA" if text != "scsi" else "SCSI"
    if "nvme_smart_health_information_log" in payload:
        return "NVMe"
    if "ata_smart_attributes" in payload:
        return "SATA"
    return "未知"


def parse_smartctl_payload(payload: Any, device_path: str = "") -> DiskSnapshot:
    """把 smartctl --json 输出解析成 DiskSnapshot。纯函数，不触碰磁盘。"""
    if not isinstance(payload, dict):
        return DiskSnapshot(device_path=device_path, error="smartctl 输出不是有效 JSON 对象")

    device_block = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    path = device_path or str(device_block.get("name") or payload.get("device_name") or "")
    mock = payload.get("mock") if isinstance(payload.get("mock"), dict) else {}
    if not path:
        path = str(mock.get("device_path") or "")

    smartctl_block = payload.get("smartctl") if isinstance(payload.get("smartctl"), dict) else {}
    exit_status = _as_int(smartctl_block.get("exit_status")) or 0
    messages = smartctl_block.get("messages") or []
    message_text = "; ".join(str(m) for m in messages if m)

    protocol = _normalize_protocol(device_block.get("protocol") or device_block.get("type"), payload)
    model = str(payload.get("model_name") or device_block.get("model_name")
                or payload.get("scsi_model_name") or "").strip()
    serial = str(payload.get("serial_number") or device_block.get("serial_number") or "").strip()

    # 硬失败判定：exit_status bit0（命令行错误）或 bit1（设备打开失败）
    if exit_status & 0b11:
        reason = message_text or f"smartctl exit_status={exit_status}"
        return DiskSnapshot(device_path=path, protocol=protocol, model=model,
                            serial=serial, error=f"读取失败：{reason}")

    capacity = _as_int((payload.get("user_capacity") or {}).get("bytes"))

    read_bytes: int | None = None
    write_bytes: int | None = None
    media_errors: int | None = None
    reallocated: int | None = None
    pending: int | None = None
    life_percent: int | None = None
    counter_source = COUNTER_SOURCE_NONE

    nvme_log = payload.get("nvme_smart_health_information_log")
    if isinstance(nvme_log, dict):
        units_read = _as_int(nvme_log.get("data_units_read"))
        units_written = _as_int(nvme_log.get("data_units_written"))
        if units_read is not None:
            read_bytes = units_read * NVME_DATA_UNIT_BYTES
        if units_written is not None:
            write_bytes = units_written * NVME_DATA_UNIT_BYTES
        if units_read is not None or units_written is not None:
            counter_source = COUNTER_SOURCE_NVME
        media_errors = _as_int(nvme_log.get("media_errors"))
        # NVMe 的 percentage_used 是"已消耗寿命百分比"，剩余寿命 = 100 - 已用
        used = _as_int(nvme_log.get("percentage_used"))
        if used is not None:
            life_percent = max(0, min(100, 100 - used))

    # Device Statistics 的 Logical Sectors 恒为 512 字节扇区，是累计读写量的权威来源。
    # 它不依赖 ata_smart_attributes 是否存在，所以先单独尝试。
    if read_bytes is None:
        devstat_read = _device_statistic(payload, ATA_DEVSTAT_READ_NAME)
        if devstat_read is not None:
            read_bytes = devstat_read * LBA_BYTES
            counter_source = COUNTER_SOURCE_DEVSTAT
    if write_bytes is None:
        devstat_write = _device_statistic(payload, ATA_DEVSTAT_WRITE_NAME)
        if devstat_write is not None:
            write_bytes = devstat_write * LBA_BYTES
            counter_source = COUNTER_SOURCE_DEVSTAT

    ata_attrs = payload.get("ata_smart_attributes")
    if isinstance(ata_attrs, dict):
        table = ata_attrs.get("table") or []
        # 兜底：SMART 属性 241/242。计数单位随主控而异（512 字节 LBA 或 16/32 MiB 块），
        # 只能在拿不到 Device Statistics 时用，界面上会注明来源。
        if read_bytes is None:
            read_lba = _ata_attribute(table, 242)      # Total_LBAs_Read
            if read_lba is not None:
                read_bytes = read_lba * LBA_BYTES
                counter_source = COUNTER_SOURCE_ATTR
        if write_bytes is None:
            write_lba = _ata_attribute(table, 241)     # Total_LBAs_Written
            if write_lba is not None:
                write_bytes = write_lba * LBA_BYTES
                counter_source = COUNTER_SOURCE_ATTR
        reallocated = _ata_attribute(table, 5)         # Reallocated_Sector_Ct
        pending = _ata_attribute(table, 197)           # Current_Pending_Sector
        if life_percent is None:
            life_percent = _sata_life_percent(table)

    if read_bytes is None:
        fallback = payload.get("total_lbas_read")
        if fallback is not None:
            read_bytes = (_as_int(fallback) or 0) * LBA_BYTES
            counter_source = COUNTER_SOURCE_ATTR
    if write_bytes is None:
        fallback = payload.get("total_lbas_written")
        if fallback is not None:
            write_bytes = (_as_int(fallback) or 0) * LBA_BYTES
            counter_source = COUNTER_SOURCE_ATTR

    if not model and not serial and not payload.get("device"):
        return DiskSnapshot(device_path=path, protocol=protocol, model=model, serial=serial,
                            error="读取失败：smartctl 未返回磁盘信息")

    return DiskSnapshot(
        device_path=path,
        protocol=protocol,
        model=model or "未知型号",
        serial=serial or "未知序列号",
        capacity_bytes=capacity,
        read_bytes=read_bytes,
        write_bytes=write_bytes,
        media_errors=media_errors,
        reallocated=reallocated,
        pending=pending,
        life_percent=life_percent,
        counter_source=counter_source,
    )


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
            return ProviderResult(smartctl_path=None, ok=False,
                                  status="未找到 smartctl.exe（可执行 winget install smartmontools.smartmontools）")
        devices = self.scan_devices()
        if not devices:
            return ProviderResult(smartctl_path=path, ok=False,
                                  status="未发现可用磁盘（可能需要管理员权限）")
        snapshots = [self.read_device(device) for device in devices]
        return ProviderResult(snapshots=snapshots, smartctl_path=path,
                              status=f"已读取 {len(snapshots)} 块磁盘（smartctl: {path}）")


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
        status = f"fixture provider: {self.fixtures_dir.name}（{len(snapshots)} 个样本）"
        return ProviderResult(snapshots=snapshots, smartctl_path=None,
                              status=status, ok=bool(snapshots))


# ---------------------------------------------------------------------------
# 增量计算层
# ---------------------------------------------------------------------------


def evaluate_media_errors(protocol: str, previous: int | None, current: int | None) -> MediaErrorStatus:
    """0E（Media and Data Integrity Errors）判定。

    - SATA 盘没有 0E 字段 -> 不适用
    - NVMe 本次无该字段 -> 不可用
    - 历史无记录（None）且本次 > 0 -> 从无到有，告警
    - 本次 > 上次 -> ⚠ 0E 新增 N
    - 相等 -> 稳定
    """
    if (protocol or "").upper() == "SATA":
        return MediaErrorStatus("不适用", "na", 0)
    if current is None:
        return MediaErrorStatus("不可用", "unavailable", 0)
    if previous is None:
        if current > 0:
            return MediaErrorStatus(f"⚠ 0E 新增 {current}", "first_seen", current)
        return MediaErrorStatus("稳定", "stable", 0)
    if current > previous:
        return MediaErrorStatus(f"⚠ 0E 新增 {current - previous}", "increased", current - previous)
    if current < previous:
        return MediaErrorStatus("稳定（计数较上次减小）", "reset", 0)
    return MediaErrorStatus("稳定", "stable", 0)


def _record_gb(record: dict, field_name: str) -> float | None:
    value = record.get(field_name)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parsed = _as_int(value)
    return float(parsed) if parsed is not None else None


def _index_history(records: Sequence[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """返回 (每块盘的首次记录, 每块盘的最近一次记录)。"""
    first: dict[str, dict] = {}
    last: dict[str, dict] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        key = str(record.get("key") or disk_key(record.get("model", ""), record.get("serial", "")))
        if key not in first:
            first[key] = record
        last[key] = record
    return first, last


def build_rows(history_records: Sequence[dict],
               snapshots: Sequence[DiskSnapshot]) -> list[DiskRow]:
    """把「历史记录 + 本次快照」换算成界面表格行（纯函数）。"""
    first_map, last_map = _index_history(history_records)
    rows: list[DiskRow] = []
    seen_keys: set[str] = set()

    for snapshot in snapshots:
        key = snapshot.key
        seen_keys.add(key)
        if not snapshot.ok:
            rows.append(DiskRow(
                key=key,
                model=snapshot.model or "（未知型号）",
                serial=snapshot.serial or "—",
                protocol=snapshot.protocol,
                device_path=snapshot.device_path,
                health_text="—",
                media_text="—",
                media_kind="unavailable",
                status_text=snapshot.error or "读取失败",
                failed=True,
            ))
            continue

        current_read = snapshot.read_gb
        current_write = snapshot.write_gb
        prior = last_map.get(key)
        earliest = first_map.get(key)

        row = DiskRow(
            key=key,
            model=snapshot.model,
            serial=snapshot.serial,
            protocol=snapshot.protocol,
            device_path=snapshot.device_path,
            read_gb=current_read,
            write_gb=current_write,
            health_text=snapshot.health_text,
        )

        if prior is None:
            row.status_text = "新盘首次记录，暂无增量对比"
            row.interval_text = "—"
            row.read_delta_gb = None
            row.write_delta_gb = None
            row.hist_read_delta_gb = None
            row.hist_write_delta_gb = None
        else:
            prior_read = _record_gb(prior, "read_gb")
            prior_write = _record_gb(prior, "write_gb")
            prev_seconds = timestamp_seconds(prior.get("timestamp"))
            cur_seconds = timestamp_seconds(_record_ts_hint(snapshot))
            if prev_seconds is not None and cur_seconds is not None:
                row.interval_text = format_interval(cur_seconds - prev_seconds)
            else:
                row.interval_text = "—"
            row.read_delta_gb = _diff(current_read, prior_read)
            row.write_delta_gb = _diff(current_write, prior_write)

            first_read = _record_gb(earliest, "read_gb") if earliest else prior_read
            first_write = _record_gb(earliest, "write_gb") if earliest else prior_write
            row.hist_read_delta_gb = _diff(current_read, first_read)
            row.hist_write_delta_gb = _diff(current_write, first_write)

            if row.read_delta_gb is not None and row.read_delta_gb < 0:
                row.status_text = "累计读数小于历史值（计数重置或硬件变动）"
            elif row.write_delta_gb is not None and row.write_delta_gb < 0:
                row.status_text = "累计写数小于历史值（计数重置或硬件变动）"
            else:
                row.status_text = "已更新记录"

        media = evaluate_media_errors(
            snapshot.protocol,
            None if prior is None else _record_0e(prior),
            snapshot.media_errors,
        )
        row.media_text = media.text
        row.media_kind = media.kind
        row.alert = media.is_alert
        if media.is_alert:
            row.status_text = media.text
        if snapshot.protocol == "SATA" and (snapshot.reallocated or snapshot.pending):
            extra = f"重映射 {snapshot.reallocated or 0} / 待映射 {snapshot.pending or 0}"
            row.status_text = f"{row.status_text}（{extra}）"
        if snapshot.counter_source == COUNTER_SOURCE_ATTR:
            # 拿不到 Device Statistics，累计量只能按 SMART 241/242 的 512 字节假设换算，
            # 个别主控按 16/32 MiB 块计数，会明显偏小，必须在界面上说明。
            row.status_text = f"{row.status_text}（累计量取自 SMART 241/242，可能偏小）"

        rows.append(row)

    # 历史里有、本次没读到的盘
    for key, record in last_map.items():
        if key in seen_keys:
            continue
        rows.append(DiskRow(
            key=key,
            model=str(record.get("model") or "（未知型号）"),
            serial=str(record.get("serial") or "—"),
            protocol=str(record.get("protocol") or "未知"),
            device_path=str(record.get("device") or "—"),
            read_gb=_record_gb(record, "read_gb"),
            write_gb=_record_gb(record, "write_gb"),
            interval_text="—",
            health_text="—",
            media_text="—",
            media_kind="unavailable",
            status_text="本次未检测到该磁盘（可能已移除或读取失败）",
            vanished=True,
        ))

    return rows


def _record_ts_hint(snapshot: DiskSnapshot) -> str | None:
    """快照本身不携带时间，这里让 build_rows 可以在测试中通过 setattr 注入 _now_hint。"""
    return getattr(snapshot, "_now_hint", None)


def _record_0e(record: dict) -> int | None:
    value = record.get("media_errors")
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return _as_int(value)


def _diff(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def summarize(rows: Sequence[DiskRow]) -> dict[str, float]:
    """底部汇总：本次读增/写增、历史读增/写增（只累加有增量的健康行）。"""
    totals = {"read_delta": 0.0, "write_delta": 0.0,
              "hist_read_delta": 0.0, "hist_write_delta": 0.0}
    for row in rows:
        if row.failed or row.vanished:
            continue
        for name, attr in (("read_delta", "read_delta_gb"), ("write_delta", "write_delta_gb"),
                           ("hist_read_delta", "hist_read_delta_gb"),
                           ("hist_write_delta", "hist_write_delta_gb")):
            value = getattr(row, attr)
            if value is not None:
                totals[name] += value
    return totals


def any_media_alert(rows: Sequence[DiskRow]) -> bool:
    return any(row.alert for row in rows)


# ---------------------------------------------------------------------------
# 历史存储层
# ---------------------------------------------------------------------------

HISTORY_FILENAME = "history.json"
SCHEMA_VERSION = 1
REQUIRED_RECORD_FIELDS = ("timestamp", "read_gb", "write_gb")


class HistoryLoadResult:
    def __init__(self, records: list[dict] | None = None, recovered: bool = False,
                 message: str = "", path: Path | None = None,
                 backup_path: Path | None = None) -> None:
        self.records = records or []
        self.recovered = recovered
        self.message = message
        self.path = path
        self.backup_path = backup_path

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"HistoryLoadResult(records={len(self.records)}, recovered={self.recovered}, "
                f"path={self.path})")


def _default_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _default_fallback_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / APP_STORAGE_ID


class HistoryStore:
    """history.json 读写：优先 exe 同目录，不可写时回退 %LOCALAPPDATA%\\<APP_STORAGE_ID>。"""

    def __init__(self, primary_dir: str | os.PathLike[str] | None = None,
                 fallback_dir: str | os.PathLike[str] | None = None,
                 filename: str = HISTORY_FILENAME,
                 writable_probe: Callable[[Path], bool] | None = None) -> None:
        self.primary_dir = Path(primary_dir) if primary_dir else _default_app_dir()
        self.fallback_dir = Path(fallback_dir) if fallback_dir else _default_fallback_dir()
        self.filename = filename
        self._probe = writable_probe or self._default_probe
        self._resolved_dir: Path | None = None

    @staticmethod
    def _default_probe(directory: Path) -> bool:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".write_probe-{os.getpid()}.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False

    def resolve_dir(self) -> Path:
        if self._resolved_dir is not None:
            return self._resolved_dir
        for candidate in (self.primary_dir, self.fallback_dir):
            try:
                if candidate and self._probe(candidate):
                    self._resolved_dir = candidate
                    return candidate
            except OSError:
                continue
        self._resolved_dir = self.fallback_dir
        return self._resolved_dir

    @property
    def path(self) -> Path:
        return self.resolve_dir() / self.filename

    def load(self) -> HistoryLoadResult:
        path = self.path
        if not path.exists():
            return HistoryLoadResult(records=[], recovered=False,
                                     message="首次运行：将建立基线记录", path=path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return HistoryLoadResult(records=[], recovered=True,
                                     message=f"历史文件读取失败（{exc}），已重建基线", path=path)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            backup = self._backup_broken(path)
            return HistoryLoadResult(records=[], recovered=True, path=path, backup_path=backup,
                                     message=f"history.json 已损坏，已备份为 {backup.name} 并重建基线")

        records, problem = validate_history(data)
        if problem:
            backup = self._backup_broken(path)
            return HistoryLoadResult(records=[], recovered=True, path=path, backup_path=backup,
                                     message=f"history.json 字段异常（{problem}），已备份为 {backup.name} 并重建基线")
        return HistoryLoadResult(records=records, recovered=False, path=path, message="")

    def _backup_broken(self, path: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
        try:
            os.replace(path, backup)
        except OSError:
            try:
                backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            except OSError:
                pass
        return backup

    def save(self, records: Sequence[dict]) -> Path:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        payload = {"schema_version": SCHEMA_VERSION, "app": APP_STORAGE_ID, "records": list(records)}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return path


def validate_history(data: Any) -> tuple[list[dict], str]:
    """校验 history.json 结构。返回 (记录列表, 问题描述)。"""
    if not isinstance(data, dict):
        return [], "根节点不是对象"
    records = data.get("records")
    if not isinstance(records, list):
        return [], "缺少 records 数组"
    cleaned: list[dict] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            return [], f"第 {index + 1} 条记录不是对象"
        for name in REQUIRED_RECORD_FIELDS:
            if name not in record:
                return [], f"第 {index + 1} 条记录缺少字段 {name}"
        if timestamp_seconds(record.get("timestamp")) is None:
            return [], f"第 {index + 1} 条记录 timestamp 无法解析"
        for name in ("read_gb", "write_gb"):
            value = record.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return [], f"第 {index + 1} 条记录 {name} 不是数字"
        item = dict(record)
        if not item.get("key"):
            item["key"] = disk_key(str(item.get("model") or ""), str(item.get("serial") or ""))
        item.setdefault("protocol", "未知")
        item.setdefault("device", "")
        item.setdefault("media_errors", None)
        cleaned.append(item)
    return cleaned, ""


def records_from_snapshots(snapshots: Sequence[DiskSnapshot], when: datetime) -> list[dict]:
    """把本次读数转换成 history.json 记录（跳过读取失败的盘）。"""
    stamp = when.isoformat(timespec="seconds")
    records: list[dict] = []
    for snapshot in snapshots:
        if not snapshot.ok:
            continue
        read_gb = snapshot.read_gb
        write_gb = snapshot.write_gb
        records.append({
            "timestamp": stamp,
            "key": snapshot.key,
            "model": snapshot.model,
            "serial": snapshot.serial,
            "protocol": snapshot.protocol,
            "device": snapshot.device_path,
            "read_gb": None if read_gb is None else round(read_gb, 4),
            "write_gb": None if write_gb is None else round(write_gb, 4),
            "media_errors": snapshot.media_errors,
        })
    return records


def latest_timestamp(records: Sequence[dict]) -> str | None:
    best: tuple[float, str] | None = None
    for record in records:
        stamp = record.get("timestamp")
        seconds = timestamp_seconds(stamp)
        if seconds is None:
            continue
        if best is None or seconds > best[0]:
            best = (seconds, str(stamp))
    return best[1] if best else None


# ---------------------------------------------------------------------------
# 运行编排层（GUI 与测试共用同一条流水线）
# ---------------------------------------------------------------------------


@dataclass
class RunOutcome:
    rows: list[DiskRow]
    totals: dict[str, float]
    media_alert: bool
    previous_stamp: str | None
    current_stamp: str
    provider_status: str
    history_message: str
    history_path: str
    smartctl_path: str | None
    baseline: bool


def perform_run(provider: Any, store: HistoryStore,
                now: datetime | None = None) -> RunOutcome:
    """读盘 -> 读历史 -> 计算增量 -> 追加记录。GUI 与端到端测试共用。"""
    when = now or datetime.now().astimezone()
    current_stamp = when.isoformat(timespec="seconds")

    result = provider.read_all()
    load = store.load()

    for snapshot in result.snapshots:
        if snapshot.ok:
            setattr(snapshot, "_now_hint", current_stamp)

    rows = build_rows(load.records, result.snapshots)
    new_records = records_from_snapshots(result.snapshots, when)
    if new_records:
        store.save(list(load.records) + new_records)

    return RunOutcome(
        rows=rows,
        totals=summarize(rows),
        media_alert=any_media_alert(rows),
        previous_stamp=latest_timestamp(load.records),
        current_stamp=current_stamp,
        provider_status=result.status,
        history_message=load.message,
        history_path=str(store.path),
        smartctl_path=result.smartctl_path,
        baseline=not load.records,
    )


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
# GUI 层（不含任何 smartctl 调用）
# ---------------------------------------------------------------------------

COLUMN_SPECS: tuple[tuple[str, str, int, str], ...] = (
    ("model", "磁盘型号", 190, "text"),
    ("serial", "序列号(截断)", 130, "text"),
    ("protocol", "协议", 70, "text"),
    ("read_gb", "累计读取(GB/TB)", 190, "num"),
    ("write_gb", "累计写入(GB/TB)", 190, "num"),
    ("interval", "距上次", 150, "text"),
    ("read_delta", "本次读增(GB)", 110, "num"),
    ("write_delta", "本次写增(GB)", 110, "num"),
    ("hist_read_delta", "历史读增(GB)", 110, "num"),
    ("hist_write_delta", "历史写增(GB)", 110, "num"),
    ("health", "健康度", 80, "text"),
    ("media", "0E", 130, "text"),
    ("status", "状态", 260, "text"),
)

ROW_ALERT_TAG = "alert_row"
ROW_MUTED_TAG = "muted_row"
ALERT_COLOR = "#C00000"
MUTED_COLOR = "#808080"
BASE_FONT = ("Microsoft YaHei UI", 9)


def _row_values(row: DiskRow) -> tuple[str, ...]:
    serial = row.serial if len(row.serial) <= 16 else row.serial[:13] + "..."
    return (
        row.model,
        serial,
        row.protocol,
        fmt_size_gb(row.read_gb),
        fmt_size_gb(row.write_gb),
        row.interval_text,
        fmt_delta(row.read_delta_gb),
        fmt_delta(row.write_delta_gb),
        fmt_delta(row.hist_read_delta_gb),
        fmt_delta(row.hist_write_delta_gb),
        row.health_text,
        row.media_text,
        row.status_text,
    )


def _row_sort_value(row: DiskRow, column: str) -> Any:
    mapping = {
        "model": row.model,
        "serial": row.serial,
        "protocol": row.protocol,
        "read_gb": row.read_gb,
        "write_gb": row.write_gb,
        "interval": row.interval_text,
        "read_delta": row.read_delta_gb,
        "write_delta": row.write_delta_gb,
        "hist_read_delta": row.hist_read_delta_gb,
        "hist_write_delta": row.hist_write_delta_gb,
        "health": row.health_text,
        "media": row.media_text,
        "status": row.status_text,
    }
    value = mapping.get(column)
    if value is None:
        return float("-inf")
    return value


class DiskTrackerApp:
    """单窗口 GUI。数据读取全部委托给注入的 provider（后台线程执行）。"""

    def __init__(self, root: Any, provider: Any = None, store: HistoryStore | None = None,
                 autostart: bool = True, now_func: Callable[[], datetime] | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.provider = provider if provider is not None else SmartctlProvider()
        self.store = store if store is not None else HistoryStore()
        self.now_func = now_func or (lambda: datetime.now().astimezone())
        self.rows: list[DiskRow] = []
        self._sort_column: str | None = None
        self._sort_reverse = False
        self._busy = False

        root.title(APP_DISPLAY_NAME)
        root.geometry("1280x600")
        root.minsize(900, 420)
        try:
            style = ttk.Style(root)
            style.configure(".", font=BASE_FONT)
            style.configure("Treeview", rowheight=24)
            style.configure("Alert.TLabel", foreground=ALERT_COLOR)
            style.configure("Hint.TLabel", foreground="#8A6D00")
        except Exception:
            pass

        self._build_widgets()
        if autostart:
            self.refresh_async()

    # ---------- 控件 ----------
    def _build_widgets(self) -> None:
        ttk = self.ttk
        outer = ttk.Frame(self.root, padding=(10, 8, 10, 8))
        outer.pack(fill="both", expand=True)

        self.hint_label = ttk.Label(outer, text="", style="Hint.TLabel", anchor="w")
        self.hint_label.pack(fill="x")

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(2, 6))
        self.prev_label = ttk.Label(top, text="上次记录：—", anchor="w")
        self.prev_label.pack(side="left", padx=(0, 16))
        self.cur_label = ttk.Label(top, text="本次记录：—", anchor="w")
        self.cur_label.pack(side="left", padx=(0, 16))
        self.smartctl_label = ttk.Label(top, text="smartctl：未检测", anchor="w")
        self.smartctl_label.pack(side="left", padx=(0, 16))
        self.admin_label = ttk.Label(top, text="权限：检测中", anchor="w")
        self.admin_label.pack(side="left")

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)

        columns = [spec[0] for spec in COLUMN_SPECS]
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for key, title, width, _kind in COLUMN_SPECS:
            self.tree.heading(key, text=title, command=lambda c=key: self.sort_by(c))
            self.tree.column(key, width=width, minwidth=60, anchor="w", stretch=(key == "status"))
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree.tag_configure(ROW_ALERT_TAG, foreground=ALERT_COLOR)
        self.tree.tag_configure(ROW_MUTED_TAG, foreground=MUTED_COLOR)

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.summary_label = ttk.Label(bottom, text="尚无记录", anchor="w")
        self.summary_label.pack(side="left")
        self.alert_label = ttk.Label(bottom, text="", style="Alert.TLabel", anchor="w")
        self.alert_label.pack(side="left", padx=(12, 0))

        self.refresh_button = ttk.Button(bottom, text="刷新记录", command=self.refresh_async)
        self.refresh_button.pack(side="right")
        self.open_button = ttk.Button(bottom, text="打开历史文件", command=self.open_history_file)
        self.open_button.pack(side="right", padx=(0, 8))
        # 「关于」只显示 APP_VERSION，版本号不在界面任何地方重复写死
        self.about_button = ttk.Button(bottom, text="关于", command=self.show_about)
        self.about_button.pack(side="right", padx=(0, 8))

        self.note_label = ttk.Label(outer, text=f"容量换算：{BYTES_PER_GIB_TEXT}；"
                                               f"历史文件：{self.store.path}",
                                    anchor="w", foreground="#606060")
        self.note_label.pack(fill="x", pady=(4, 0))

        admin_text = "权限：管理员" if is_admin() else "权限：未以管理员运行"
        self.admin_label.configure(text=admin_text)
        if not is_admin():
            self._append_hint("未以管理员运行，部分磁盘可能读取失败。")

    def _append_hint(self, text: str) -> None:
        existing = self.hint_label.cget("text")
        self.hint_label.configure(text=(existing + "  " if existing else "") + text)

    # ---------- 表格 ----------
    def clear_rows(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

    def render_rows(self, rows: Sequence[DiskRow]) -> None:
        self.rows = list(rows)
        self.clear_rows()
        ordered = self._sorted_rows(self.rows)
        for row in ordered:
            tags: tuple[str, ...] = ()
            if row.failed or row.vanished:
                tags = (ROW_MUTED_TAG,)
            elif row.alert:
                tags = (ROW_ALERT_TAG,)
            iid = row.key or row.device_path or row.model
            base = iid
            suffix = 1
            while self.tree.exists(iid):
                suffix += 1
                iid = f"{base}#{suffix}"
            self.tree.insert("", "end", iid=iid, values=_row_values(row), tags=tags)

    def _sorted_rows(self, rows: Sequence[DiskRow]) -> list[DiskRow]:
        if not self._sort_column:
            return list(rows)
        column = self._sort_column
        return sorted(rows, key=lambda r: _row_sort_value(r, column), reverse=self._sort_reverse)

    def sort_by(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self.render_rows(self.rows)

    def row_tags(self) -> list[tuple[str, ...]]:
        return [tuple(self.tree.item(iid, "tags")) for iid in self.tree.get_children()]

    # ---------- 数据刷新 ----------
    def refresh_async(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.refresh_button.configure(state="disabled")
        self.smartctl_label.configure(text="读取中…")
        self.status_queue: list[RunOutcome | Exception] = []

        def worker() -> None:
            try:
                outcome = perform_run(self.provider, self.store, now=self.now_func())
            except Exception as exc:  # 不让后台线程把异常带进主循环
                traceback.print_exc()
                self.status_queue.append(exc)
            else:
                self.status_queue.append(outcome)
            finally:
                try:
                    self.root.after(60, self._poll)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True, name="disk-reader").start()

    def _poll(self) -> None:
        if not self.status_queue:
            self.root.after(60, self._poll)
            return
        item = self.status_queue.pop()
        self._busy = False
        self.refresh_button.configure(state="normal")
        if isinstance(item, Exception):
            self.smartctl_label.configure(text="smartctl：读取异常")
            self._append_hint(f"读取失败：{item}")
            return
        self.apply_outcome(item)

    def apply_outcome(self, outcome: RunOutcome) -> RunOutcome:
        self.clear_rows()
        before_sort = self._sort_column
        self._sort_column = before_sort
        self.rows = list(outcome.rows)
        self.render_rows(self.rows)

        self.prev_label.configure(text=f"上次记录：{outcome.previous_stamp or '—'}")
        self.cur_label.configure(text=f"本次记录：{outcome.current_stamp}")
        self.smartctl_label.configure(
            text=f"smartctl：{outcome.smartctl_path}" if outcome.smartctl_path
            else f"smartctl：{outcome.provider_status}"
        )
        if outcome.smartctl_path is None and outcome.provider_status:
            self._append_hint("未找到 smartctl.exe，请执行：winget install smartmontools.smartmontools")

        totals = outcome.totals
        self.summary_label.configure(
            text=(f"本次总读增 {fmt_delta(totals.get('read_delta'))} GB ｜ "
                  f"本次总写增 {fmt_delta(totals.get('write_delta'))} GB ｜ "
                  f"历史总读增 {fmt_delta(totals.get('hist_read_delta'))} GB ｜ "
                  f"历史总写增 {fmt_delta(totals.get('hist_write_delta'))} GB")
        )
        self.alert_label.configure(
            text="介质错误在累积，建议关注备份" if outcome.media_alert else ""
        )
        if outcome.history_message:
            self._append_hint(outcome.history_message)
        if outcome.baseline:
            self.note_label.configure(
                text=f"已记录基线，下次打开将显示增量。历史文件：{outcome.history_path}"
            )
        else:
            self.note_label.configure(
                text=f"容量换算：{BYTES_PER_GIB_TEXT}；历史文件：{outcome.history_path}"
            )
        return outcome

    def run_pipeline_sync(self) -> RunOutcome:
        """同步跑一遍流水线（GUI 冒烟测试用，不启动线程）。"""
        outcome = perform_run(self.provider, self.store, now=self.now_func())
        return self.apply_outcome(outcome)

    # ---------- 关于 ----------
    def show_about(self) -> None:
        """弹出版本/来源说明。正文完全由 about_text() 生成，只引用 APP_VERSION。"""
        from tkinter import messagebox

        messagebox.showinfo(f"关于 {APP_DISPLAY_NAME}", about_text(self.store.path), parent=self.root)

    def open_history_file(self) -> None:
        path = self.store.path
        try:
            if not path.exists():
                self.store.save([])
            if platform.system() == "Windows":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                import webbrowser
                webbrowser.open(path.as_uri())
        except Exception as exc:
            self._append_hint(f"打开历史文件失败：{exc}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def create_app(provider: Any = None, store: HistoryStore | None = None,
               root: Any = None) -> DiskTrackerApp:
    import tkinter as tk

    owns_root = root is None
    window = root if root is not None else tk.Tk()
    app = DiskTrackerApp(window, provider=provider, store=store, autostart=False)
    if owns_root:
        app.owns_root = True
    return app


def main(argv: Sequence[str] | None = None) -> int:
    import tkinter as tk

    root = tk.Tk()
    DiskTrackerApp(root, provider=SmartctlProvider(), store=HistoryStore(), autostart=True)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
