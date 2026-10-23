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
import unicodedata
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
APP_VERSION = "0.5.4"                              # 版本号（关于框显示；发版 tag 必须与之一致）
APP_AUTHOR = "snackbs"                             # 「关于」里显示的作者
APP_AI_NOTE = "由 AI 辅助编写"                      # 「关于」里的开发方式说明

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


def window_title() -> str:
    """窗口标题 / 界面左上角标题栏文字的唯一来源：显示名 + 版本号。

    版本号只引用 APP_VERSION，界面里不再出现任何写死的版本字面量；
    发版时改一处常量，窗口标题、左上角标题、关于框会同步跟上。
    """
    return f"{APP_DISPLAY_NAME}  v{APP_VERSION}"


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
        f"作者：{APP_AUTHOR}\n"
        f"开发方式：{APP_AI_NOTE}\n"
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


def fmt_delta_gb(value: float | None, digits: int = 2) -> str:
    """增量带单位的写法（卡片用）：+348.80 GB。

    表格列头已经写了「(GB)」，所以表格里的增量不带单位；卡片每格是独立小字，
    不带单位看的人要先回头看列头，容易误解成次数或百分比。
    """
    if value is None:
        return "—"
    return f"{fmt_delta(value, digits)} GB"


def fmt_size_gb(value: float | None, digits: int = 2) -> str:
    """累计量双单位显示：>= 1 TB 时同时给出 TB 与 GB，否则只给 GB。

    例：32567.42 -> "31.80 TB (32,567.42 GB)"；512 -> "512.00 GB"。
    """
    if value is None:
        return "—"
    if abs(value) >= 1024.0:
        return f"{value / 1024.0:,.{digits}f} TB ({value:,.{digits}f} GB)"
    return f"{value:,.{digits}f} GB"


def fmt_size_short(value: float | None, digits: int = 2) -> str:
    """累计量紧凑显示：>= 1 TB 用 TB，否则用 GB。

    表格把「累计读 / 累计写」合并成一列，双单位写法放不下，这里只给主单位；
    精确 GB 值仍可在导出 TSV、诊断信息与区块提示里看到。
    """
    if value is None:
        return "—"
    if abs(value) >= 1024.0:
        return f"{value / 1024.0:,.{digits}f} TB"
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


def _smartctl_message_text(messages: Any) -> str:
    """smartctl 的 messages 可能是字符串数组，也可能是 {"string": ...} 对象数组（7.x）。

    两种都要取到可读文本，否则界面上会出现 "{'string': '...'}" 这种 Python 字典字面量。
    """
    if not isinstance(messages, (list, tuple)):
        return ""
    parts: list[str] = []
    for item in messages:
        if isinstance(item, dict):
            text = item.get("string") or item.get("message") or ""
        else:
            text = item
        text = str(text).strip()
        if text:
            parts.append(text)
    return "; ".join(parts)


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
    message_text = _smartctl_message_text(smartctl_block.get("messages"))

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
                row.status_text = "累计读数回退，疑似计数重置"
            elif row.write_delta_gb is not None and row.write_delta_gb < 0:
                row.status_text = "累计写数回退，疑似计数重置"
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
            status_text="本次未检测到（已移除或读取失败）",
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


# 历史里存的是保留 4 位小数的 GB 值，本次是原始精度，直接相减可能出现 -0.0001 GB
# 这类"假回退"，被误判成"计数重置"。差值小于这个阈值一律归零。
DELTA_EPSILON_GB = 0.01


def _diff(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    diff = current - previous
    # 抹掉浮点/取整噪声，避免界面出现 "-0.00" 和假的"累计量回退"
    if abs(diff) < DELTA_EPSILON_GB:
        return 0.0
    return diff


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
# 主题层：纯数据表 + 解析函数
#
# 用户选定的方案是「跟随系统」：读 Windows 的「应用模式」设置，
#   系统浅色 -> 数据台账（THEME_LEDGER）
#   系统深色 -> 深色专业（THEME_DARK）
# 其余四套作为手动可选项保留在「主题」菜单里。
# 纯标准库（ctypes/winreg/ttk），不引入第三方主题包。
# ---------------------------------------------------------------------------

THEME_SYSTEM = "system"
THEME_LEDGER = "ledger"
THEME_DARK = "dark"
THEME_GAUGE = "gauge"
THEME_MINIMAL = "minimal"
THEME_NATIVE = "native"

THEME_ORDER: tuple[str, ...] = (THEME_SYSTEM, THEME_LEDGER, THEME_DARK,
                                THEME_GAUGE, THEME_MINIMAL, THEME_NATIVE)
THEME_LABELS = {
    THEME_SYSTEM: "跟随系统",
    THEME_LEDGER: "数据台账",
    THEME_DARK: "深色专业",
    THEME_GAUGE: "清爽仪表",
    THEME_MINIMAL: "极简留白",
    THEME_NATIVE: "原生默认",
}
# 「跟随系统」解析到的具体主题
THEME_SYSTEM_LIGHT = THEME_LEDGER
THEME_SYSTEM_DARK = THEME_DARK

REG_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
REG_LIGHT_VALUE = "AppsUseLightTheme"

# 主题表：全部参数集中在此，apply_theme() 只照着表 set，不再有第二处颜色字面量。
#   base        ttk 主题名（vista=系统原生；clam 支持自定义表头底色）
#   kpi         None=无指标卡，cards=普通卡片，bordered=带描边卡片
#   dark_bar    是否请求深色标题栏（Win10 1809+）
THEMES: dict[str, dict[str, Any]] = {
    THEME_NATIVE: {
        "key": THEME_NATIVE, "label": "原生默认", "base": "vista",
        "win_bg": "#F0F0F0", "tree_bg": "#FFFFFF", "tree_fg": "#1A1A1A",
        "head_bg": None, "head_fg": "#1A1A1A", "head_bold": False,
        "zebra": None, "alert_bg": None, "alert_fg": "#C00000", "muted_fg": "#808080",
        "select_bg": "#0078D7", "select_fg": "#FFFFFF",
        "rowheight": 38, "padx": 10, "pady": 8, "kpi": None, "dark_bar": False,
        "title_fg": "#000000", "title_pt": 11,
        "hint_fg": "#8A6D00", "note_fg": "#606060", "summary_fg": "#1A1A1A",
        "card_bg": "#FFFFFF", "card_border": None, "card_val": "#1A1A1A",
        "card_lbl": "#606060", "num_family": "Consolas", "card_pt": 12,
        "scroll_thumb": "#C6CDD6", "scroll_thumb_hover": "#9AA6B2",
        "scroll_idle": "#E4E8ED",
        "seg_sel_bg": "#0078D7", "seg_sel_fg": "#FFFFFF",
        "seg_hover_bg": "#E5F1FB", "seg_hover_fg": "#1A1A1A",
        "seg_border": "#B8B8B8",
    },
    THEME_LEDGER: {
        "key": THEME_LEDGER, "label": "数据台账", "base": "clam",
        "win_bg": "#FFFFFF", "tree_bg": "#FFFFFF", "tree_fg": "#1F2933",
        "head_bg": "#2B3440", "head_fg": "#FFFFFF", "head_bold": True,
        "zebra": "#F7F9FB", "alert_bg": "#FDECEA", "alert_fg": "#B32D1F",
        "muted_fg": "#8A8A8A",
        "select_bg": "#DCE9F7", "select_fg": "#1F2933",
        "rowheight": 42, "padx": 12, "pady": 10, "kpi": "cards", "dark_bar": False,
        "title_fg": "#2B3440", "title_pt": 12,
        "hint_fg": "#8A6D00", "note_fg": "#7B8794", "summary_fg": "#1F2933",
        "card_bg": "#F4F7FA", "card_border": None, "card_val": "#1F2933",
        "card_lbl": "#7B8794", "num_family": "Consolas", "card_pt": 12,
        "scroll_thumb": "#C6CDD6", "scroll_thumb_hover": "#9AA6B2",
        "scroll_idle": "#E4E8ED",
        "seg_sel_bg": "#2B3440", "seg_sel_fg": "#FFFFFF",
        "seg_hover_bg": "#E3E9F0", "seg_hover_fg": "#1F2933",
        "seg_border": "#D5DDE6",
    },
    THEME_DARK: {
        "key": THEME_DARK, "label": "深色专业", "base": "clam",
        "win_bg": "#1E2227", "tree_bg": "#23282E", "tree_fg": "#D6DEE7",
        "head_bg": "#171B20", "head_fg": "#A8C4DC", "head_bold": True,
        "zebra": "#272D34", "alert_bg": "#3A2427", "alert_fg": "#FF8A7A",
        "muted_fg": "#6B7580",
        "select_bg": "#35526B", "select_fg": "#FFFFFF",
        "rowheight": 42, "padx": 12, "pady": 10, "kpi": "cards", "dark_bar": True,
        "title_fg": "#E6EDF3", "title_pt": 12,
        "hint_fg": "#E0C070", "note_fg": "#8B97A3", "summary_fg": "#D6DEE7",
        "card_bg": "#262C33", "card_border": None, "card_val": "#7FC8A9",
        "card_lbl": "#8B97A3", "num_family": "Consolas", "card_pt": 12,
        "scroll_thumb": "#7C8A99", "scroll_thumb_hover": "#9AA9B8",
        "scroll_idle": "#2E353D",
        "seg_sel_bg": "#3E6E8E", "seg_sel_fg": "#F2F7FB",
        "seg_hover_bg": "#2F3942", "seg_hover_fg": "#D6DEE7",
        "seg_border": "#3A444D",
    },
    THEME_GAUGE: {
        "key": THEME_GAUGE, "label": "清爽仪表", "base": "clam",
        "win_bg": "#F5F8FB", "tree_bg": "#FFFFFF", "tree_fg": "#22303C",
        "head_bg": "#EAF0F6", "head_fg": "#51606F", "head_bold": True,
        "zebra": "#FAFCFE", "alert_bg": "#FDECEA", "alert_fg": "#B32D1F",
        "muted_fg": "#93A1AE",
        "select_bg": "#DCE9F7", "select_fg": "#1B6FB8",
        "rowheight": 44, "padx": 16, "pady": 14, "kpi": "bordered", "dark_bar": False,
        "title_fg": "#1B2A38", "title_pt": 13,
        "hint_fg": "#8A6D00", "note_fg": "#7C8B99", "summary_fg": "#22303C",
        "card_bg": "#FFFFFF", "card_border": "#DCE4EC", "card_val": "#1B6FB8",
        "card_lbl": "#7C8B99", "num_family": "Consolas", "card_pt": 15,
        "scroll_thumb": "#C8D3DE", "scroll_thumb_hover": "#9DB0C4",
        "scroll_idle": "#E9EFF5",
        "seg_sel_bg": "#1B6FB8", "seg_sel_fg": "#FFFFFF",
        "seg_hover_bg": "#E6EEF6", "seg_hover_fg": "#1B2A38",
        "seg_border": "#DCE4EC",
    },
    THEME_MINIMAL: {
        "key": THEME_MINIMAL, "label": "极简留白", "base": "clam",
        "win_bg": "#FFFFFF", "tree_bg": "#FFFFFF", "tree_fg": "#2A2A2A",
        "head_bg": "#FFFFFF", "head_fg": "#8A8A8A", "head_bold": False,
        "zebra": None, "alert_bg": None, "alert_fg": "#D0342C", "muted_fg": "#A0A0A0",
        "select_bg": "#EFEFEF", "select_fg": "#1A1A1A",
        "rowheight": 46, "padx": 22, "pady": 18, "kpi": None, "dark_bar": False,
        "title_fg": "#1A1A1A", "title_pt": 12,
        "hint_fg": "#8A6D00", "note_fg": "#A0A0A0", "summary_fg": "#2A2A2A",
        "card_bg": "#FFFFFF", "card_border": None, "card_val": "#1A1A1A",
        "card_lbl": "#A0A0A0", "num_family": "Consolas", "card_pt": 12,
        "scroll_thumb": "#D8D8D8", "scroll_thumb_hover": "#B8B8B8",
        "scroll_idle": "#EFEFEF",
        "seg_sel_bg": "#2A2A2A", "seg_sel_fg": "#FFFFFF",
        "seg_hover_bg": "#F0F0F0", "seg_hover_fg": "#1A1A1A",
        "seg_border": "#E2E2E2",
    },
}


def detect_system_dark_mode() -> bool | None:
    """Windows「应用模式」是否为深色。True=深色，False=浅色，None=读不到（非 Windows 等）。"""
    if platform.system() != "Windows":
        return None
    try:
        import winreg  # 仅 Windows 才导入

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PERSONALIZE) as key:
            value, _kind = winreg.QueryValueEx(key, REG_LIGHT_VALUE)
        return int(value) == 0
    except Exception:
        return None


def normalize_theme_key(key: Any) -> str:
    """把任意输入收敛成合法主题键，非法值回落「跟随系统」。"""
    if isinstance(key, str) and (key in THEMES or key == THEME_SYSTEM):
        return key
    return THEME_SYSTEM


def system_theme_name(dark_mode: bool | None = None) -> str:
    """「跟随系统」当前解析到的具体主题名。"""
    dark = detect_system_dark_mode() if dark_mode is None else bool(dark_mode)
    return THEME_SYSTEM_DARK if dark else THEME_SYSTEM_LIGHT


def resolve_theme(key: Any, dark_mode: bool | None = None) -> dict[str, Any]:
    """主题键 -> 具体主题参数表。key 为 system 时按系统配色落到浅色/深色两套之一。"""
    normalized = normalize_theme_key(key)
    if normalized == THEME_SYSTEM:
        return dict(THEMES[system_theme_name(dark_mode)])
    return dict(THEMES[normalized])


# ---------------------------------------------------------------------------
# 数据展示视图（三种）：表格 / 卡片 / 双列
#   视图偏好记在 settings.json 的 "view" 键里，绝不写进 history.json。
# ---------------------------------------------------------------------------

VIEW_TABLE = "table"
VIEW_CARDS = "cards"
VIEW_GRID = "grid"
VIEW_ORDER: tuple[str, ...] = (VIEW_TABLE, VIEW_CARDS, VIEW_GRID)
VIEW_LABELS = {VIEW_TABLE: "表格", VIEW_CARDS: "卡片", VIEW_GRID: "双列"}
DEFAULT_VIEW = VIEW_TABLE

# 卡片视图里每块盘展示的指标格：(标题, DiskRow 字段名)
CARD_METRICS: tuple[tuple[str, str], ...] = (
    ("累计读取", "read_gb"),
    ("累计写入", "write_gb"),
    ("距上次", "interval_text"),
    ("本次读增", "read_delta_gb"),
    ("本次写增", "write_delta_gb"),
    ("历史读增", "hist_read_delta_gb"),
    ("历史写增", "hist_write_delta_gb"),
    ("0E", "media_text"),
    ("健康度", "health_text"),
)
# 指标格每行几列：单列卡片宽，放 5 列；双列时每张卡只有一半宽度，放 3 列
CARD_METRIC_COLUMNS = 5
CARD_METRIC_COLUMNS_GRID = 3


def normalize_view_key(key: Any) -> str:
    """把任意输入收敛成合法视图键，非法值回落表格视图。"""
    if isinstance(key, str) and key in VIEW_ORDER:
        return key
    return DEFAULT_VIEW


# ---------------------------------------------------------------------------
# 界面偏好存储层（独立 settings.json，不碰 history.json 的 schema）
# ---------------------------------------------------------------------------

SETTINGS_FILENAME = "settings.json"
DEFAULT_SETTINGS: dict[str, Any] = {"theme": THEME_SYSTEM, "view": DEFAULT_VIEW}


class SettingsStore:
    """界面偏好读写：与 history.json 同目录；文件损坏或字段缺失时静默回落默认值。"""

    def __init__(self, directory: str | os.PathLike[str] | None = None,
                 filename: str = SETTINGS_FILENAME) -> None:
        self.directory = Path(directory) if directory else _default_app_dir()
        self.filename = filename

    @property
    def path(self) -> Path:
        return self.directory / self.filename

    def load(self) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return data
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return data
        if not isinstance(payload, dict):
            return data
        theme = payload.get("theme")
        if isinstance(theme, str):
            data["theme"] = normalize_theme_key(theme)
        view = payload.get("view")
        if isinstance(view, str):
            data["view"] = normalize_view_key(view)
        return data

    def save(self, data: dict[str, Any]) -> Path:
        merged = dict(DEFAULT_SETTINGS)
        for key, value in (data or {}).items():
            if isinstance(key, str):
                merged[key] = value
        merged["theme"] = normalize_theme_key(merged.get("theme"))
        merged["view"] = normalize_view_key(merged.get("view"))
        self.directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(merged, ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path


# ---------------------------------------------------------------------------
# GUI 层（不含任何 smartctl 调用）
# ---------------------------------------------------------------------------

# 细圆角滚动条：ttk.Scrollbar 的滑块由主题元素绘制，永远是直角矩形且带上下滑轨，
# 想做成"圆角细条"只能自绘。这里用 tk.Canvas 画，并实现 Treeview/Canvas 需要的
# set()/get() 协议，因此可以直接挂在 yscrollcommand / xscrollcommand 上。
try:                                   # 无 Tk 的环境也能 import 本模块（测试可只跑逻辑层）
    import tkinter as _tk_for_scrollbar
except Exception:                       # pragma: no cover - 依赖系统 Tk
    _tk_for_scrollbar = None

_SCROLLBAR_BASE: Any = _tk_for_scrollbar.Canvas if _tk_for_scrollbar else object
_LABEL_BASE: Any = _tk_for_scrollbar.Label if _tk_for_scrollbar else object


# 数据区右侧细滚动条占用的宽度。KPI 指标行必须留出同样的右边距，
# 否则它比下面的表格/卡片宽出这一条，两行的左右边缘对不齐。
SCROLL_GUTTER = 10
# 拖动窗口时 canvas 的 <Configure> 会连着触发几十次。卡片区里每张卡二三十个控件，
# 每次都跟着重算几何会让缩放明显卡顿，所以宽度重排延迟到停手后再做一次。
CARD_RESIZE_DELAY_MS = 250
# 卡片之间的水平间距（只在列与列之间留，最右一列贴边，才能和 KPI 行右缘对齐）
CARD_GAP = 10


class SlimScrollbar(_SCROLLBAR_BASE):   # type: ignore[misc,valid-type]
    """极细的圆角滚动条。内容全部可见时自动隐藏，不占视觉。"""

    MIN_THUMB = 28          # 滑块最短像素，太短会点不中
    INSET = 2               # 轨道左右留白，让条看起来更细

    def __init__(self, master: Any, orient: str = "vertical", command: Any = None, *,
                 track: str = "#FFFFFF", thumb: str = "#C6CDD6",
                 thumb_hover: str = "#9AA6B2", idle: str = "#E4E8ED",
                 thickness: int = SCROLL_GUTTER,
                 radius: int = 3, **kwargs: Any) -> None:
        super().__init__(master, highlightthickness=0, bd=0, bg=track,
                         width=thickness if orient == "vertical" else 1,
                         height=thickness if orient == "horizontal" else 1, **kwargs)
        self.orient = orient
        self.command = command
        self.track_color = track
        self.thumb_color = thumb
        self.thumb_hover_color = thumb_hover
        self.idle_color = idle
        self.thickness = thickness
        self.radius = radius
        self.first = 0.0
        self.last = 1.0
        self._hover = False
        self._drag_origin: float | None = None
        self._drag_first = 0.0
        self._alive = True
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", lambda _event: self._redraw())
        self.bind("<Destroy>", self._on_destroy)

    # ---- 滚动条协议 ----
    def set(self, first: Any, last: Any) -> None:
        """Treeview / Canvas 通过这个回调告诉我们可见区间。"""
        try:
            self.first = max(0.0, min(1.0, float(first)))
            self.last = max(0.0, min(1.0, float(last)))
        except (TypeError, ValueError):
            self.first, self.last = 0.0, 1.0
        self._redraw()

    def get(self) -> tuple[float, float]:
        return (self.first, self.last)

    def recolor(self, track: str, thumb: str, thumb_hover: str,
                idle: str | None = None) -> None:
        """主题切换时重配颜色（不重建控件）。"""
        self.track_color = track
        self.thumb_color = thumb
        self.thumb_hover_color = thumb_hover
        if idle is not None:
            self.idle_color = idle
        try:
            self.configure(bg=track)
        except Exception:
            pass
        self._redraw()

    # ---- 绘制 ----
    @property
    def visible(self) -> bool:
        return (self.last - self.first) < 0.999

    def _length(self) -> int:
        try:
            value = self.winfo_height() if self.orient == "vertical" else self.winfo_width()
        except Exception:
            return 0
        return int(value or 0)

    def _thumb_span(self, length: int | None = None) -> int:
        total = self._length() if length is None else length
        span = int((self.last - self.first) * total)
        return max(span, self.MIN_THUMB)

    def _thumb_start(self, length: int | None = None) -> int:
        total = self._length() if length is None else length
        span = self._thumb_span(total)
        start = int(round(self.first * total))
        return max(self.INSET, min(start, max(total - span - self.INSET, self.INSET)))

    def _redraw(self) -> None:
        if not self._alive:
            return
        try:
            self.delete("thumb")
        except Exception:
            return
        length = self._length()
        if length <= self.thickness + 2 * self.INSET:
            return
        if self.visible:
            span = self._thumb_span(length)
            start = self._thumb_start(length)
            end = min(start + span, length - self.INSET)
            start = max(self.INSET, end - span)
            color = self.thumb_hover_color if self._hover else self.thumb_color
        else:
            # 内容全部可见：依然占住这一条（画成很淡的整条），不再整条消失。
            # 之前这里是「直接不画」，右侧就空出一道和滚动条等宽的留白，
            # 看起来像滚动条丢了，也会让人怀疑卡片是不是没对齐。
            start = self.INSET
            end = length - self.INSET
            color = self.thumb_hover_color if self._hover else self.idle_color
        # 用一条带圆头端帽（capstyle=round）的粗线画滑块：端帽半径正好是线宽一半，
        # 上下半圆与中间直条天然同心同宽。之前用 oval+rectangle 三个图元拼，
        # 接缝和半圆处会漏出 1 像素，看起来像圆角「溢出去」了。
        line_width = max(2, min(self.radius * 2, self.thickness - 2 * self.INSET))
        center = self.thickness / 2.0
        cap = line_width / 2.0
        near = start + cap
        far = max(near, end - cap)
        if self.orient == "vertical":
            self.create_line(center, near, center, far, width=line_width,
                             capstyle="round", fill=color, tags="thumb")
        else:
            self.create_line(near, center, far, center, width=line_width,
                             capstyle="round", fill=color, tags="thumb")

    # ---- 交互 ----
    def _on_press(self, event: Any) -> None:
        length = self._length()
        if length <= 0 or self.command is None:
            return
        position = event.y if self.orient == "vertical" else event.x
        span = self._thumb_span(length)
        start = self._thumb_start(length)
        if start <= position <= start + span:
            self._drag_origin = position
            self._drag_first = self.first
            return
        # 点空白处翻一页（翻 90% 的可视高度，和系统滚动条手感一致）
        page = 0.9 * max(self.last - self.first, 0.1)
        target = self.first + (page if position > start + span else -page)
        self.command("moveto", max(0.0, min(1.0 - (self.last - self.first), target)))

    def _on_drag(self, event: Any) -> None:
        if self._drag_origin is None or self.command is None:
            return
        length = self._length()
        if length <= 0:
            return
        position = event.y if self.orient == "vertical" else event.x
        delta = (position - self._drag_origin) / length
        limit = max(0.0, 1.0 - (self.last - self.first))
        self.command("moveto", max(0.0, min(limit, self._drag_first + delta)))

    def _on_release(self, _event: Any) -> None:
        self._drag_origin = None

    def _on_enter(self, _event: Any) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, _event: Any) -> None:
        self._hover = False
        self._drag_origin = None
        self._redraw()

    def _on_destroy(self, _event: Any) -> None:
        self._alive = False


class SegButton(_LABEL_BASE):   # type: ignore[misc,valid-type]
    """顶栏「表格 / 卡片 / 双列」分段切换按钮——自绘的 tk.Label。

    不用 ttk.Radiobutton 的原因：vista 主题下 radiobutton 由系统 element 绘制，
    style.map() 里配的 active 配色会被系统配色盖掉，鼠标悬停就变成系统蓝 + 虚线焦点框，
    和自定义主题冲突（原生默认主题下最明显）。改用 tk.Label 完全自绘后，
    常态 / 悬停 / 选中三种状态的颜色都只取自主题表，跨 ttk 主题表现一致。
    接口保留 invoke()，与 ttk.Radiobutton 兼容，便于测试直接触发。
    """

    def __init__(self, master: Any, text: str, value: str, command: Any = None,
                 **kwargs: Any) -> None:
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("takefocus", 0)
        kwargs.setdefault("cursor", "hand2")
        kwargs.setdefault("anchor", "center")
        super().__init__(master, text=text, highlightthickness=1, **kwargs)
        self.value = value
        self.command = command
        self.selected = False
        self._hover = False
        self._palette: dict[str, str] = {
            "bg": "#FFFFFF", "fg": "#1A1A1A",
            "sel_bg": "#0078D7", "sel_fg": "#FFFFFF",
            "hover_bg": "#E5F1FB", "hover_fg": "#1A1A1A",
            "border": "#B8B8B8",
        }
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    # ---- 外观 ----
    def apply_palette(self, theme: dict[str, Any], base_font: tuple[str, int]) -> None:
        """按主题重配配色与字体（不重建控件，选中态不变）。"""
        self._palette = {
            "bg": theme["card_bg"] if theme["kpi"] else theme["win_bg"],
            "fg": theme["tree_fg"],
            "sel_bg": theme["seg_sel_bg"],
            "sel_fg": theme["seg_sel_fg"],
            "hover_bg": theme["seg_hover_bg"],
            "hover_fg": theme["seg_hover_fg"],
            "border": theme["seg_border"],
        }
        family, size = base_font
        self.configure(font=(family, size), padx=11, pady=4)
        self.refresh()

    def set_selected(self, selected: bool) -> None:
        self.selected = bool(selected)
        self.refresh()

    def refresh(self) -> None:
        palette = self._palette
        if self.selected:
            background, foreground = palette["sel_bg"], palette["sel_fg"]
        elif self._hover:
            background, foreground = palette["hover_bg"], palette["hover_fg"]
        else:
            background, foreground = palette["bg"], palette["fg"]
        border = palette["sel_bg"] if self.selected else palette["border"]
        try:
            self.configure(background=background, foreground=foreground,
                           highlightbackground=border, highlightcolor=border)
        except Exception:       # pragma: no cover - 控件已销毁
            pass

    # ---- 交互 ----
    def invoke(self) -> None:
        """与 ttk.Radiobutton 同名：触发一次选择（测试也走这条路径）。"""
        if self.command is not None:
            self.command()

    def _on_click(self, _event: Any = None) -> str:
        self.invoke()
        return "break"

    def _on_enter(self, _event: Any = None) -> None:
        self._hover = True
        self.refresh()

    def _on_leave(self, _event: Any = None) -> None:
        self._hover = False
        self.refresh()


# 工具 → 排序 子菜单（卡片视图没有表头，排序必须也能从这里触发）
SORT_MENU_ITEMS: tuple[tuple[str, str], ...] = (
    ("model", "按磁盘型号"),
    ("rw", "按累计读取"),
    ("interval", "按距上次时间"),
    ("cur", "按本次读增"),
    ("hist", "按历史读增"),
    ("media", "按 0E 状态"),
    ("status", "按状态"),
)

# 表格视图固定 8 列：把成对的读/写指标合进同一格（"左  /  右"），
# 型号与序列号也合并到首列，这样默认窗口下一屏放得下，不需要横向拖动。
COLUMN_SPECS: tuple[tuple[str, str, int, str], ...] = (
    ("model", "磁盘 / 序列号", 250, "text"),
    ("protocol", "协议", 62, "text"),
    ("rw", "累计读 / 写", 120, "num"),
    ("interval", "距上次", 152, "text"),
    ("cur", "本次读增 / 写增", 108, "num"),
    ("hist", "历史读增 / 写增", 108, "num"),
    ("media", "0E / 健康", 104, "text"),
    ("status", "状态", 200, "text"),
)
# 表格自然宽度（列宽合计），用于按屏幕挑选默认窗口大小
TABLE_NATURAL_WIDTH = sum(spec[2] for spec in COLUMN_SPECS)



ROW_ALERT_TAG = "alert_row"
ROW_MUTED_TAG = "muted_row"
ROW_ZEBRA_TAG = "zebra_row"
# 原生默认主题下的颜色常量（保留为模块级常量，主题表里共用同一组取值）
ALERT_COLOR = "#C00000"
MUTED_COLOR = "#808080"
BASE_FONT = ("Microsoft YaHei UI", 9)
SMARTCTL_INSTALL_HINT = ("未找到 smartctl.exe，请执行："
                         "winget install smartmontools.smartmontools")

KPI_SPECS: tuple[tuple[str, str], ...] = (
    ("read_delta", "本次总读增"),
    ("write_delta", "本次总写增"),
    ("hist_read_delta", "历史总读增"),
    ("hist_write_delta", "历史总写增"),
)


def fmt_delta_compact(value: float | None, digits: int = 2) -> str:
    """指标卡用的紧凑增量：超过 1 TB 时用 TB，否则用 GB。"""
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    if abs(value) >= 1024.0:
        return f"{sign}{value / 1024.0:,.{digits}f} TB"
    return f"{sign}{value:,.{digits}f} GB"


def _default_geometry(root: Any) -> str:
    """按屏幕尺寸挑一个默认窗口大小。

    13 列的自然宽度约 1450px，常见 1920 宽屏可以一屏放下；窄屏则回落到
    1280x620 并保留横向滚动条（Treeview 自带），不会因为窗口过宽跑到屏幕外。
    """
    try:
        screen_w = int(root.winfo_screenwidth())
        screen_h = int(root.winfo_screenheight())
    except Exception:
        screen_w, screen_h = 1280, 800
    want_w = TABLE_NATURAL_WIDTH + 84
    width = want_w if screen_w >= want_w + 80 else 1280
    width = min(width, max(screen_w - 80, 980))
    width = max(width, 980)
    height = max(min(640, screen_h - 120), 460)
    return f"{width}x{height}"


# 状态列过长的报错文本按这个宽度截断（完整文本仍保留在 DiskRow 与诊断信息里）
STATUS_TEXT_LIMIT = 24


def _truncate(text: str, limit: int = STATUS_TEXT_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# 合并列在同一格里上下两行显示：上面的值对应表头「/」左边，下面的对应右边。
PAIR_SEPARATOR = "\n"
# 导出 TSV 时换行会破坏表格结构，所以剪贴板里用斜杠分隔
TSV_PAIR_SEPARATOR = " / "


def _pair(left: str, right: str) -> str:
    """把一对读/写（或 0E/健康）值上下叠在同一格：第一行左值，第二行右值。"""
    return f"{left}{PAIR_SEPARATOR}{right}"


def _short_serial(serial: str) -> str:
    """序列号只做清洗，不再截断——首列改成上下两行后放得下完整序列号。"""
    return (serial or "").strip() or "—"


def _row_values(row: DiskRow) -> tuple[str, ...]:
    """表格 8 列的一行文本。合并列里两个值上下两行，与表头「/」的左右一一对应。"""
    return (
        f"{row.model}\n{_short_serial(row.serial)}",
        row.protocol,
        _pair(fmt_size_short(row.read_gb), fmt_size_short(row.write_gb)),
        row.interval_text,
        _pair(fmt_delta(row.read_delta_gb), fmt_delta(row.write_delta_gb)),
        _pair(fmt_delta(row.hist_read_delta_gb), fmt_delta(row.hist_write_delta_gb)),
        _pair(row.media_text, row.health_text),
        _truncate(row.status_text),
    )


def _row_sort_value(row: DiskRow, column: str) -> Any:
    # 8 列视图的排序键。合并列按「左值优先」排序（读增优先于写增），
    # 快捷列名（serial/read_delta/…）保留下来，旧调用点与菜单排序仍然可用。
    mapping = {
        "model": row.model,
        "serial": row.serial,
        "protocol": row.protocol,
        "rw": row.read_gb,
        "read_gb": row.read_gb,
        "write_gb": row.write_gb,
        "interval": row.interval_text,
        "cur": row.read_delta_gb,
        "read_delta": row.read_delta_gb,
        "write_delta": row.write_delta_gb,
        "hist": row.hist_read_delta_gb,
        "hist_read_delta": row.hist_read_delta_gb,
        "hist_write_delta": row.hist_write_delta_gb,
        "media": row.media_text,
        "health": row.health_text,
        "status": row.status_text,
    }
    value = mapping.get(column)
    if value is None:
        return float("-inf")
    return value


class DiskTrackerApp:
    """单窗口 GUI。数据读取全部委托给注入的 provider（后台线程执行）。"""

    def __init__(self, root: Any, provider: Any = None, store: HistoryStore | None = None,
                 autostart: bool = True, now_func: Callable[[], datetime] | None = None,
                 settings: SettingsStore | None = None,
                 theme: str | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.provider = provider if provider is not None else SmartctlProvider()
        self.store = store if store is not None else HistoryStore()
        self.settings = (settings if settings is not None
                         else SettingsStore(self.store.resolve_dir()))
        self.now_func = now_func or (lambda: datetime.now().astimezone())
        self.rows: list[DiskRow] = []
        self._sort_column: str | None = None
        self._sort_reverse = False
        self._busy = False
        self.base_font = BASE_FONT
        self.theme_key = THEME_SYSTEM
        self.theme: dict[str, Any] = dict(THEMES[THEME_LEDGER])

        root.title(window_title())
        root.geometry(_default_geometry(root))
        root.minsize(980, 460)

        saved = self._load_settings()
        self.theme_key = normalize_theme_key(theme if theme is not None else saved.get("theme"))
        self.view_key = normalize_view_key(saved.get("view"))
        self._card_rows: list[Any] = []
        self._card_columns = 0
        self._cards_width = -1          # 上一次同步给内部 Frame 的宽度
        self._pending_cards_width = -1  # 最近一次 canvas 报告的宽度（延迟下发）
        self._cards_resize_id: str | None = None  # 待执行的宽度下发（去抖用）
        self._cards_bbox: tuple[int, ...] | None = None
        self._scroll_sync_id: str | None = None   # 待执行的滚动区域同步（去抖用）

        self._build_menubar()
        self._build_widgets()
        self.apply_theme(self.theme_key, persist=False)
        self.set_view(self.view_key, persist=False)
        if autostart:
            self.refresh_async()

    # ---------- 偏好 ----------
    def _load_settings(self) -> dict[str, Any]:
        try:
            return self.settings.load()
        except Exception:  # 偏好坏了不能影响启动
            return dict(DEFAULT_SETTINGS)

    # ---------- 菜单栏 ----------
    def _build_menubar(self) -> None:
        tk = self.tk
        menubar = tk.Menu(self.root, tearoff=False)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="刷新记录(&R)", accelerator="Ctrl+R",
                              command=self.refresh_async)
        file_menu.add_command(label="打开历史文件(&O)", command=self.open_history_file)
        file_menu.add_separator()
        file_menu.add_command(label="退出(&X)", accelerator="Alt+F4", command=self.close)

        view_menu = tk.Menu(menubar, tearoff=False)
        self._theme_var = tk.StringVar(master=self.root, value=self.theme_key)
        for key in THEME_ORDER:
            view_menu.add_radiobutton(label=THEME_LABELS[key], value=key,
                                      variable=self._theme_var,
                                      command=lambda k=key: self.apply_theme(k))

        tools_menu = tk.Menu(menubar, tearoff=False)
        tools_menu.add_command(label="复制表格到剪贴板(&C)", command=self.copy_table)
        tools_menu.add_command(label="打开数据目录(&D)", command=self.open_data_dir)
        tools_menu.add_separator()
        # 卡片视图没有表头，排序必须也能从菜单触发
        sort_menu = tk.Menu(tools_menu, tearoff=False)
        for column, title in SORT_MENU_ITEMS:
            sort_menu.add_command(label=title, command=lambda c=column: self.sort_by(c))
        sort_menu.add_separator()
        sort_menu.add_command(label="恢复默认顺序(&R)", command=self.reset_sort)
        tools_menu.add_cascade(label="排序(&O)", menu=sort_menu)
        tools_menu.add_command(label="重置排序(&S)", command=self.reset_sort)
        self.sort_menu = sort_menu

        disk_menu = tk.Menu(menubar, tearoff=False)
        disk_menu.add_command(label="重新扫描并记录(&R)", command=self.refresh_async)
        disk_menu.add_command(label="数据源状态(&S)", command=self.show_source_status)
        disk_menu.add_command(label="复制诊断信息(&I)", command=self.copy_diagnostics)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="如何安装数据源(&S)", command=self.show_source_help)
        help_menu.add_command(label="容量与换算说明(&C)", command=self.show_capacity_help)
        help_menu.add_separator()
        help_menu.add_command(label="关于(&A)", command=self.show_about)

        lang_menu = tk.Menu(menubar, tearoff=False)
        self._lang_var = tk.StringVar(master=self.root, value="zh-CN")
        lang_menu.add_radiobutton(label="简体中文", value="zh-CN", variable=self._lang_var)
        lang_menu.add_radiobutton(label="English（即将支持）", value="en",
                                  variable=self._lang_var, state="disabled")

        menubar.add_cascade(label="文件(&F)", menu=file_menu)
        menubar.add_cascade(label="主题(&T)", menu=view_menu)
        menubar.add_cascade(label="工具(&U)", menu=tools_menu)
        menubar.add_cascade(label="硬盘(&D)", menu=disk_menu)
        menubar.add_cascade(label="帮助(&H)", menu=help_menu)
        menubar.add_cascade(label="语言(&L)", menu=lang_menu)

        self.menubar = menubar
        self.file_menu = file_menu
        self.theme_menu = view_menu
        self.tools_menu = tools_menu
        self.disk_menu = disk_menu
        self.help_menu = help_menu
        self.lang_menu = lang_menu

        self.root.configure(menu=menubar)
        self.root.bind("<Control-r>", lambda _event: self.refresh_async())
        self.root.bind("<Control-q>", lambda _event: self.close())

    # ---------- 控件 ----------
    def _build_widgets(self) -> None:
        ttk = self.ttk
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(10, 8, 10, 8))
        outer.pack(fill="both", expand=True)
        self.outer = outer

        # 顶栏：左侧程序名 + 版本号（文字来自 window_title()，与窗口标题同源），
        # 右侧数据视图分段切换（表格 / 卡片 / 双列），切换不重建窗口。
        head = ttk.Frame(outer, style="App.TFrame")
        head.pack(fill="x")
        self.title_label = ttk.Label(head, text=window_title(), style="Title.TLabel", anchor="w")
        self.title_label.pack(side="left")

        self._view_var = self.tk.StringVar(master=self.root, value=self.view_key)
        self.seg_frame = ttk.Frame(head, style="App.TFrame")
        self.seg_frame.pack(side="right")
        self.seg_buttons: dict[str, SegButton] = {}
        for index, key in enumerate(VIEW_ORDER):
            button = SegButton(self.seg_frame, text=VIEW_LABELS[key], value=key,
                               command=lambda k=key: self.set_view(k))
            button.pack(side="left", padx=(0 if index == 0 else 2, 0))
            self.seg_buttons[key] = button

        self.hint_parts: list[str] = []
        self.hint_label = ttk.Label(outer, text="", style="Hint.TLabel", anchor="w")
        self.hint_label.pack(fill="x")

        top = ttk.Frame(outer, style="App.TFrame")
        top.pack(fill="x", pady=(2, 6))
        self.prev_label = ttk.Label(top, text="上次记录：—", style="App.TLabel", anchor="w")
        self.prev_label.pack(side="left", padx=(0, 16))
        self.cur_label = ttk.Label(top, text="本次记录：—", style="App.TLabel", anchor="w")
        self.cur_label.pack(side="left", padx=(0, 16))
        self.smartctl_label = ttk.Label(top, text="数据源：未检测", style="App.TLabel", anchor="w")
        self.smartctl_label.pack(side="left", padx=(0, 16))
        self.admin_label = ttk.Label(top, text="权限：检测中", style="App.TLabel", anchor="w")
        self.admin_label.pack(side="left")

        # 指标卡（B/C/D 主题显示，A/E 主题隐藏；控件只建一次，切换主题时 pack/pack_forget）
        # 右边距必须等于数据区右侧细滚动条的宽度：KPI 行与下面的表格/卡片共用
        # 同一条右边界，否则最右一张卡会伸出去，和卡片右缘对不齐（v0.5.1 的观感问题）。
        self.kpi_frame = ttk.Frame(outer, style="App.TFrame", padding=(0, 0, SCROLL_GUTTER, 0))
        self.kpi_values: dict[str, Any] = {}
        # 间距必须两侧对称（各 CARD_GAP//2），否则第一张卡比后面每张宽 CARD_GAP，
        # 四张卡宽度就会 290/272 交替；叠加 uniform 后每张卡严格等宽，
        # 第 2、3 张卡的分界线正好落在整行宽度的 50%，与双列卡片的中缝重合。
        kpi_half_gap = CARD_GAP // 2
        kpi_count = len(KPI_SPECS)
        for index, (key, caption) in enumerate(KPI_SPECS):
            card = ttk.Frame(self.kpi_frame, style="Card.TFrame", padding=(12, 8))
            if index == 0:
                kpi_padx = (0, kpi_half_gap)
            elif index == kpi_count - 1:
                kpi_padx = (kpi_half_gap, 0)
            else:
                kpi_padx = (kpi_half_gap, kpi_half_gap)
            card.grid(row=0, column=index, sticky="ew", padx=kpi_padx)
            self.kpi_frame.columnconfigure(index, weight=1, uniform="kpi")
            value = ttk.Label(card, text="—", style="CardValue.TLabel", anchor="w")
            value.pack(fill="x")
            label = ttk.Label(card, text=caption, style="CardLabel.TLabel", anchor="w")
            label.pack(fill="x")
            self.kpi_values[key] = value
        self.kpi_visible = False

        # 数据区：三种视图叠在同一个容器里，按需 pack/pack_forget（控件只建一次）。
        # 注意这里先不 pack —— pack 按调用顺序分配空间，expand 的数据区必须先让
        # 底部条与注释行占好位置，最后再把它塞进剩余空间。
        body = ttk.Frame(outer, style="App.TFrame")
        self.body = body

        table_frame = ttk.Frame(body, style="App.TFrame")
        table_frame.pack(fill="both", expand=True)
        self.table_frame = table_frame

        columns = [spec[0] for spec in COLUMN_SPECS]
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for key, title, width, _kind in COLUMN_SPECS:
            self.tree.heading(key, text=title, command=lambda c=key: self.sort_by(c))
            self.tree.column(key, width=width, minwidth=60, anchor="w", stretch=(key == "status"))
        # 细圆角滚动条（自绘，ttk.Scrollbar 做不出圆角），竖向常显、横向按需
        self.yscroll = SlimScrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.xscroll = SlimScrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=self.yscroll.set, xscrollcommand=self.xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.yscroll.grid(row=0, column=1, sticky="ns")
        self.xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # 卡片视图（单列 / 双列共用同一个 Canvas 容器）
        cards_frame = ttk.Frame(body, style="App.TFrame")
        self.cards_frame = cards_frame
        self.cards_canvas = self.tk.Canvas(cards_frame, highlightthickness=0, bd=0,
                                           bg=self.theme["win_bg"])
        self.cards_scroll = SlimScrollbar(cards_frame, orient="vertical",
                                          command=self.cards_canvas.yview)
        self.cards_canvas.configure(yscrollcommand=self.cards_scroll.set)
        self.cards_canvas.grid(row=0, column=0, sticky="nsew")
        self.cards_scroll.grid(row=0, column=1, sticky="ns")
        cards_frame.rowconfigure(0, weight=1)
        cards_frame.columnconfigure(0, weight=1)
        self.cards_inner = self.tk.Frame(self.cards_canvas, bg=self.theme["win_bg"])
        self._cards_window = self.cards_canvas.create_window((0, 0), window=self.cards_inner,
                                                             anchor="nw")
        self.cards_inner.bind("<Configure>", self._sync_cards_scrollregion)
        self.cards_canvas.bind("<Configure>", self._resize_cards)
        # 鼠标滚轮在卡片区也能滚（Windows 下 delta 是 120 的倍数）。
        # 只绑 canvas / inner 不够：指针落在卡片里的 Label、Entry 这些子控件上时
        # Windows 的滚轮事件不会冒泡到父控件，看起来就是"只有背景能滚"。
        # 每张卡建好后由 _bind_card_wheel() 递归绑到卡内所有控件。
        self._bind_card_wheel(self.cards_canvas)
        self._bind_card_wheel(self.cards_inner)

        # 底部条与注释行先按 side="bottom" 占位，再让上面的数据区吃剩余空间；
        # 否则 expand=True 的数据区会把底部条压成几像素高，汇总文字被裁掉
        note = ttk.Label(outer, text=f"容量换算：{BYTES_PER_GIB_TEXT}；"
                                     f"历史文件：{self.store.path}",
                         style="Note.TLabel", anchor="w")
        note.pack(side="bottom", fill="x", pady=(4, 0))
        self.note_label = note

        bottom = ttk.Frame(outer, style="App.TFrame")
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        self.summary_label = ttk.Label(bottom, text="尚无记录", style="Summary.TLabel", anchor="w")
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

        # 底部条与注释行已占位，数据区吃剩下的高度
        self.body.pack(fill="both", expand=True)

        self._refresh_admin_label()

    def _refresh_admin_label(self) -> None:
        admin = is_admin()
        self.admin_label.configure(text="权限：管理员" if admin else "权限：未以管理员运行")
        if not admin:
            self._append_hint("未以管理员运行，部分磁盘可能读取失败。")

    def _append_hint(self, text: str) -> None:
        """追加一条顶部提示。同样的提示只保留一条，重复触发不会越堆越长。"""
        text = (text or "").strip()
        if not text:
            return
        if text not in self.hint_parts:
            self.hint_parts.append(text)
        self.hint_label.configure(text="  ".join(self.hint_parts))

    # ---------- 主题 ----------
    def apply_theme(self, key: Any, persist: bool = True,
                    dark_mode: bool | None = None) -> dict[str, Any]:
        """应用一套主题。控件不重建，只重配样式与指标卡可见性。"""
        theme = resolve_theme(key, dark_mode=dark_mode)
        self.theme_key = normalize_theme_key(key)
        self.theme = theme
        style = self.ttk.Style(self.root)
        names = set(style.theme_names())
        base = theme["base"] if theme["base"] in names else ("clam" if "clam" in names else "default")
        try:
            style.theme_use(base)
        except Exception:
            pass

        family, size = self.base_font
        num_family = theme["num_family"]
        padx, pady = theme["padx"], theme["pady"]

        style.configure(".", font=self.base_font)
        style.configure("App.TFrame", background=theme["win_bg"])
        style.configure("App.TLabel", background=theme["win_bg"], foreground=theme["tree_fg"])
        style.configure("Title.TLabel", background=theme["win_bg"], foreground=theme["title_fg"],
                        font=(family, theme["title_pt"], "bold"))
        style.configure("Hint.TLabel", background=theme["win_bg"], foreground=theme["hint_fg"])
        style.configure("Note.TLabel", background=theme["win_bg"], foreground=theme["note_fg"])
        style.configure("Summary.TLabel", background=theme["win_bg"],
                        foreground=theme["summary_fg"])
        style.configure("Alert.TLabel", background=theme["win_bg"],
                        foreground=theme["alert_fg"], font=(family, size, "bold"))

        border = 1 if theme["card_border"] else 0
        style.configure("Card.TFrame", background=theme["card_bg"], borderwidth=border,
                        relief="solid" if border else "flat",
                        bordercolor=theme["card_border"] or theme["card_bg"])
        style.configure("CardValue.TLabel", background=theme["card_bg"],
                        foreground=theme["card_val"],
                        font=(num_family, theme["card_pt"], "bold"))
        style.configure("CardLabel.TLabel", background=theme["card_bg"],
                        foreground=theme["card_lbl"], font=(family, size - 1))

        head_font = (family, size, "bold") if theme["head_bold"] else (family, size)
        if theme["head_bg"]:
            style.configure("Treeview.Heading", background=theme["head_bg"],
                            foreground=theme["head_fg"], font=head_font,
                            relief="flat", padding=(6, 4))
        else:
            style.configure("Treeview.Heading", font=head_font)
        style.configure("Treeview", background=theme["tree_bg"],
                        fieldbackground=theme["tree_bg"], foreground=theme["tree_fg"],
                        rowheight=theme["rowheight"], borderwidth=0)
        style.map("Treeview",
                  background=[("selected", theme["select_bg"])],
                  foreground=[("selected", theme["select_fg"])])

        # 顶栏视图分段切换按钮：自绘 tk.Label，配色全部来自主题表
        # （ttk.Radiobutton 在 vista 主题下由系统 element 绘制，悬停会被系统蓝盖掉）
        for button in getattr(self, "seg_buttons", {}).values():
            button.apply_palette(theme, self.base_font)
            button.set_selected(button.value == self.view_key)

        self.outer.configure(padding=(padx, pady, padx, pady))
        try:
            self.root.configure(bg=theme["win_bg"])
        except Exception:
            pass

        # 细滚动条跟着主题换色（不重建控件）
        for bar in (getattr(self, "yscroll", None), getattr(self, "xscroll", None),
                    getattr(self, "cards_scroll", None)):
            if bar is not None:
                bar.recolor(theme["win_bg"], theme["scroll_thumb"],
                            theme["scroll_thumb_hover"], theme["scroll_idle"])
        if hasattr(self, "cards_canvas"):
            self.cards_canvas.configure(bg=theme["win_bg"])
            self.cards_inner.configure(bg=theme["win_bg"])

        zebra_bg = theme["zebra"] or theme["tree_bg"]
        self.tree.tag_configure(ROW_ALERT_TAG,
                                foreground=theme["alert_fg"],
                                background=theme["alert_bg"] or theme["tree_bg"])
        self.tree.tag_configure(ROW_MUTED_TAG,
                                foreground=theme["muted_fg"],
                                background=theme["tree_bg"])
        self.tree.tag_configure(ROW_ZEBRA_TAG, background=zebra_bg)

        self._set_kpi_visible(theme["kpi"] is not None)
        self._apply_titlebar(theme)
        if hasattr(self, "_theme_var"):
            self._theme_var.set(self.theme_key)
        # 卡片视图的颜色是直接画在 tk 控件上的，主题切换后必须重画
        if getattr(self, "view_key", VIEW_TABLE) != VIEW_TABLE and hasattr(self, "cards_inner"):
            self.render_cards()
        if persist:
            self._save_theme(self.theme_key)
        return theme

    def _set_kpi_visible(self, visible: bool) -> None:
        if visible and not self.kpi_visible:
            self.kpi_frame.pack(fill="x", pady=(0, 8), before=self.body)
            self.kpi_visible = True
        elif not visible and self.kpi_visible:
            self.kpi_frame.pack_forget()
            self.kpi_visible = False

    def _save_theme(self, key: str) -> None:
        self._save_setting("theme", key)

    def _apply_titlebar(self, theme: dict[str, Any]) -> None:
        """深色主题时把 Windows 标题栏也切深（Win10 1809+，失败静默忽略）。"""
        if platform.system() != "Windows":
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())  # type: ignore[attr-defined]
            if not hwnd:
                return
            flag = ctypes.c_int(1 if theme["dark_bar"] else 0)
            for attr in (20, 19):   # 20=新接口，19=旧版本回退
                try:
                    rc = ctypes.windll.dwmapi.DwmSetWindowAttribute(  # type: ignore[attr-defined]
                        hwnd, attr, ctypes.byref(flag), ctypes.sizeof(flag))
                except Exception:
                    rc = -1
                if rc == 0:
                    break
            # 触发非客户区重绘，否则要等下次改变窗口大小才变色
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)  # type: ignore[attr-defined]
        except Exception:
            pass

    # ---------- 表格 ----------
    def clear_rows(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

    def render_rows(self, rows: Sequence[DiskRow]) -> None:
        self.rows = list(rows)
        self.clear_rows()
        ordered = self._sorted_rows(self.rows)
        striped = bool(self.theme.get("zebra"))
        for index, row in enumerate(ordered):
            tags: tuple[str, ...] = ()
            if row.failed or row.vanished:
                tags = (ROW_MUTED_TAG,)
            elif row.alert:
                tags = (ROW_ALERT_TAG,)
            elif striped and index % 2:
                tags = (ROW_ZEBRA_TAG,)
            iid = row.key or row.device_path or row.model
            base = iid
            suffix = 1
            while self.tree.exists(iid):
                suffix += 1
                iid = f"{base}#{suffix}"
            self.tree.insert("", "end", iid=iid, values=_row_values(row), tags=tags)
        if self.view_key != VIEW_TABLE:
            self.render_cards()

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

    def reset_sort(self) -> None:
        self._sort_column = None
        self._sort_reverse = False
        self.render_rows(self.rows)

    def row_tags(self) -> list[tuple[str, ...]]:
        return [tuple(self.tree.item(iid, "tags")) for iid in self.tree.get_children()]

    def card_rows(self) -> list[DiskRow]:
        """卡片视图当前渲染的盘（顺序与表格一致，用于排序/断言的统一入口）。"""
        return list(self._card_rows)

    # ---------- 视图切换：表格 / 卡片 / 双列 ----------
    def set_view(self, key: Any, persist: bool = True) -> str:
        """切换数据展示视图。只 pack/pack_forget，不重建控件，选中行与滚动位置不丢。"""
        key = normalize_view_key(key)
        self.view_key = key
        if hasattr(self, "_view_var"):
            self._view_var.set(key)
        for button_key, button in getattr(self, "seg_buttons", {}).items():
            button.set_selected(button_key == key)
        if key == VIEW_TABLE:
            self.cards_frame.pack_forget()
            self.table_frame.pack(fill="both", expand=True)
        else:
            self.table_frame.pack_forget()
            self.cards_frame.pack(fill="both", expand=True)
            self.render_cards()
            # 切视图是一次性动作，不必等去抖，立刻把布局算准
            self.flush_card_layout()
        if persist:
            self._save_setting("view", key)
        return key

    def _save_setting(self, name: str, value: Any) -> None:
        try:
            self.settings.save({name: value})
        except OSError as exc:      # 只读目录等，不影响使用
            self._append_hint(f"偏好保存失败：{exc}")

    def _bind_card_wheel(self, widget: Any) -> None:
        """把滚轮事件绑到卡片里的每个控件上（含后续动态创建的子控件）。"""
        try:
            widget.bind("<MouseWheel>", self._on_cards_wheel)
            children = widget.winfo_children()
        except Exception:
            return
        for child in children:
            self._bind_card_wheel(child)

    def _sync_cards_scrollregion(self, _event: Any = None) -> None:
        """内容尺寸变化后重算滚动区域。

        拖动窗口时 inner 的 <Configure> 会连着触发几十次，每次都 configure
        scrollregion 会让 canvas 反复重排，形成「重排→再次 Configure」的反馈环，
        缩放明显卡顿。这里合并成一次 after_idle 处理，并且只有 bbox 真变了才下发。
        """
        if self._scroll_sync_id is not None:
            return
        try:
            self._scroll_sync_id = self.root.after_idle(self._apply_cards_scrollregion)
        except Exception:
            self._scroll_sync_id = None

    def _cards_viewport(self) -> tuple[int, int, int, int]:
        """(宽, 高, 内容宽, 内容高)：内容尺寸取内部 Frame 的请求尺寸。"""
        try:
            width = max(int(self.cards_canvas.winfo_width()), 1)
            height = max(int(self.cards_canvas.winfo_height()), 1)
            content_w = max(int(self.cards_inner.winfo_reqwidth()), 1)
            content_h = max(int(self.cards_inner.winfo_reqheight()), 1)
        except Exception:
            return (1, 1, 1, 1)
        return (width, height, content_w, content_h)

    def _cards_fit(self) -> bool:
        """内容是否完全放得下（放得下就不该能滚动，也不该出现空白带）。"""
        _, height, _, content_h = self._cards_viewport()
        return content_h <= height

    def _apply_cards_scrollregion(self) -> None:
        """重算滚动区域，并保证「放得下 ⇒ 贴顶且不能滚」这条不变量。

        两个曾经踩过的坑：
        1) 缓存只比 bbox，窗口变高时 bbox 不变会直接早退，滚动区域停在旧值；
           所以缓存的键改成 (内容尺寸, 视口尺寸)。
        2) 内容比视口矮时，滚动区域若比视口高，滚轮就能把卡片推下去，
           露出上方一大片空白（用户截图里的"可以滑下去"）。
           这里把区域高度补到视口高度并强制回到顶部，从根上消掉。
        """
        self._scroll_sync_id = None
        width, height, content_w, content_h = self._cards_viewport()
        key = (content_w, content_h, width, height)
        if key == self._cards_bbox:
            return
        self._cards_bbox = key
        region_w = max(content_w, width)
        region_h = max(content_h, height)
        try:
            # 内部 Frame 永远贴 (0,0)：任何历史遗留的纵向偏移都在这里被抹平。
            self.cards_canvas.coords(self._cards_window, 0, 0)
            self.cards_canvas.configure(scrollregion=(0, 0, region_w, region_h))
            if content_h <= height:
                self.cards_canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _resize_cards(self, event: Any) -> None:
        """canvas 宽度变化时同步内部 Frame 宽度（延迟合并，拖动时不再卡顿）。

        两个坑叠在一起：
        1) 宽度没变就重设一次会连锁触发 inner 的 <Configure>，形成反馈环；
        2) 卡片里控件多，每次设宽度都要整块重算几何（实测 10 张卡约 250ms/步）。
        所以先记下最新宽度、把「真正下发」延后 CARD_RESIZE_DELAY_MS：
        连续拖动时定时器一直被重置，拖动过程只做 canvas 自适应（很轻），
        松手后一次性重排，手感和流畅度都正常。
        """
        width = int(getattr(event, "width", 0) or 0)
        if width <= 0 or width == self._pending_cards_width:
            return
        self._pending_cards_width = width
        if self._cards_resize_id is not None:
            try:
                self.root.after_cancel(self._cards_resize_id)
            except Exception:
                pass
        try:
            self._cards_resize_id = self.root.after(
                CARD_RESIZE_DELAY_MS, self._apply_cards_width)
        except Exception:
            self._cards_resize_id = None

    def flush_card_layout(self) -> None:
        """立刻执行挂起的宽度重排与滚动区域同步（切视图、测试断言前调用）。"""
        if self._cards_resize_id is not None:
            try:
                self.root.after_cancel(self._cards_resize_id)
            except Exception:
                pass
            self._cards_resize_id = None
        self._apply_cards_width()
        if self._scroll_sync_id is not None:
            try:
                self.root.after_cancel(self._scroll_sync_id)
            except Exception:
                pass
            self._scroll_sync_id = None
        self._apply_cards_scrollregion()

    def _apply_cards_width(self) -> None:
        """把延迟期间攒下的最新宽度真正下发给 canvas 内部 Frame。"""
        self._cards_resize_id = None
        width = self._pending_cards_width
        if width <= 0 or width == getattr(self, "_cards_width", -1):
            return
        self._cards_width = width
        try:
            self.cards_canvas.itemconfigure(self._cards_window, width=width)
        except Exception:
            pass
        # 宽度变了必然要重算滚动区域：只靠 inner 的 <Configure> 不够，
        # 窗口放大时 inner 尺寸常不变（宽度被 itemconfigure 拉满但内容不换行），
        # 滚动区域就会停在旧值上，缩小窗口后便凭空多出一段能滚动的空白。
        self._apply_cards_scrollregion()

    def _on_cards_wheel(self, event: Any) -> str:
        # 内容放得下时滚轮什么都不做：否则滚动区域一旦被撑大，
        # 滚轮就能把整块卡片推下去，露出上方一大片空白。
        if self._cards_fit():
            return "break"
        step = -1 * int(getattr(event, "delta", 0) // 120 or 0)
        if step:
            try:
                self.cards_canvas.yview_scroll(step, "units")
            except Exception:
                return "break"
        return "break"

    def render_cards(self) -> None:
        """按当前视图重建卡片（单列=卡片视图，双列=两列网格）。"""
        columns = 1 if self.view_key == VIEW_CARDS else 2
        theme = self.theme
        for child in self.cards_inner.winfo_children():
            child.destroy()
        # 先把上一种视图留下的列配置清干净：双列给每列配了 weight/uniform，
        # 切回单列时若不清，卡片只会占一半宽度（看起来仍像双列）。
        widest = max(columns, getattr(self, "_card_columns", 0))
        for column in range(widest):
            self.cards_inner.columnconfigure(column, weight=0, uniform="")
        self._card_columns = columns
        self._card_rows = self._sorted_rows(self.rows)
        # 列间距要两边各让一半：如果只在左列卡片右侧留 CARD_GAP，
        # 左列卡片就比右列窄 CARD_GAP 像素（实测 567 vs 577），双列看着就是歪的。
        half_gap = CARD_GAP // 2
        for index, row in enumerate(self._card_rows):
            card = self._build_card(row, theme)
            column = index % columns
            if columns == 1:
                padx = (0, 0)                       # 单列：卡片贴满整行，右缘与 KPI 行对齐
            elif column == 0:
                padx = (0, half_gap)                # 最左列
            elif column == columns - 1:
                padx = (half_gap, 0)                # 最右列：右侧不留白，贴住滚动条
            else:
                padx = (half_gap, half_gap)
            card.grid(row=index // columns, column=column, sticky="nsew",
                      padx=padx, pady=(0, 8))
            self._bind_card_wheel(card)
        for column in range(columns):
            self.cards_inner.columnconfigure(column, weight=1, uniform="card", minsize=0)
        self.cards_canvas.configure(bg=theme["win_bg"])
        self.cards_inner.configure(bg=theme["win_bg"])
        self.cards_scroll.recolor(theme["win_bg"], theme["scroll_thumb"],
                                  theme["scroll_thumb_hover"], theme["scroll_idle"])
        self.root.after_idle(self._sync_cards_scrollregion)

    @staticmethod
    def _display_width(text: str) -> int:
        """按显示列数估算 tk.Entry 的 width（中文/全角按 2 列算）。

        Entry 的 width 单位是「平均字符宽度」，一个汉字实际占两列，
        所以 len("已更新记录") + 1 只有 6 列，中文会被截成「已更新记」。
        """
        return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                   for ch in str(text))

    def _card_text(self, master: Any, text: str, *, bg: str, fg: str, font: tuple,
                   anchor: str = "w", **kwargs: Any) -> Any:
        """卡片里的文字控件：只读 Entry，鼠标可以选中并 Ctrl+C 复制。

        用 tk.Label 时文字完全选不中（用户反馈过），换只读 Entry 后
        选择、拖选、Ctrl+C 全都是原生行为；readonlybackground 让底色与卡片一致，
        relief/flat + 无高亮边框让外观和 Label 看不出差别。
        """
        tk = self.tk
        kwargs.setdefault("width", 1)   # 默认 width=20 会把卡片撑宽，靠 fill 拉伸即可
        entry = tk.Entry(master, state="readonly", readonlybackground=bg, fg=fg,
                         relief="flat", bd=0, highlightthickness=0, takefocus=0,
                         font=font, **kwargs)
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        entry.configure(state="readonly")
        entry.configure(justify=anchor if anchor in ("left", "right", "center") else "left")
        return entry

    def _build_card(self, row: DiskRow, theme: dict[str, Any]) -> Any:
        """一块盘一张卡片：标题行（型号 + 协议 + 状态）+ 序列号行 + 指标格。"""
        tk = self.tk
        alert = row.alert
        muted = row.failed or row.vanished
        background = theme["alert_bg"] if alert and theme["alert_bg"] else theme["card_bg"]
        foreground = (theme["alert_fg"] if alert
                      else (theme["muted_fg"] if muted else theme["tree_fg"]))
        border = theme["alert_fg"] if alert else (theme["card_border"] or theme["win_bg"])
        outer = tk.Frame(self.cards_inner, bg=background, highlightbackground=border,
                         highlightthickness=1)
        inner = tk.Frame(outer, bg=background, padx=12, pady=9)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=background)
        title_row.pack(fill="x")
        status_text = _truncate(row.status_text, 30)
        self._card_text(title_row, status_text, bg=background,
                        fg=theme["alert_fg"] if alert else theme["note_fg"],
                        font=(self.base_font[0], self.base_font[1] - 1),
                        width=self._display_width(status_text) + 1
                        ).pack(side="right")
        name_row = tk.Frame(title_row, bg=background)
        name_row.pack(side="left", fill="x", expand=True)
        # 型号用「按显示宽度定宽、不填充」的只读 Entry：它会紧挨着后面的协议徽章，
        # 而不是被 fill="x" 拉到整行最右、把徽章挤到状态文字跟前（v0.5.2 的观感问题）。
        self._card_text(name_row, row.model, bg=background, fg=foreground,
                        font=(self.base_font[0], self.base_font[1] + 1, "bold"),
                        width=self._display_width(row.model) + 1).pack(side="left")
        # 协议徽章紧跟型号（保持 Label：它是色块标签，不是需要复制的文字）
        tk.Label(name_row, text=row.protocol, bg=theme["head_bg"] or background,
                 fg=theme["head_fg"], font=(self.base_font[0], self.base_font[1] - 1),
                 padx=6).pack(side="left", padx=(8, 0))

        self._card_text(inner, f"{row.serial}  ·  {row.device_path}", bg=background,
                        fg=theme["note_fg"],
                        font=(self.base_font[0], self.base_font[1] - 1), width=1
                        ).pack(fill="x", pady=(2, 7))

        metric_row = tk.Frame(inner, bg=background)
        metric_row.pack(fill="x")
        # 等宽网格：标签与数值按列对齐，行与行之间不会错位
        columns = (CARD_METRIC_COLUMNS_GRID if self.view_key == VIEW_GRID
                   else CARD_METRIC_COLUMNS)
        for column in range(columns):
            metric_row.columnconfigure(column, weight=1, uniform="metric")
        for index, (caption, field) in enumerate(CARD_METRICS):
            value = getattr(row, field, None)
            if field == "read_gb":
                text = fmt_size_short(row.read_gb)
            elif field == "write_gb":
                text = fmt_size_short(row.write_gb)
            elif field in ("read_delta_gb", "write_delta_gb",
                           "hist_read_delta_gb", "hist_write_delta_gb"):
                # 卡片每格是独立小字，没有列头写「(GB)」，所以这里必须带单位
                text = fmt_delta_gb(value)
            elif field == "interval_text":
                text = row.interval_text
            else:
                text = str(value if value is not None else "—")
            color = foreground
            if alert and field == "media_text":
                color = theme["alert_fg"]
            # 只有纯数字（容量/增量/健康度）用等宽字体对齐；时长与状态文字用界面字体，
            # 否则「22 天 12 小时 15 分钟」这种中英混排在 Consolas 里会很别扭
            numeric = field not in ("interval_text", "media_text")
            value_font = ((theme["num_family"], self.base_font[1] + 1, "bold") if numeric
                          else (self.base_font[0], self.base_font[1] + 1, "bold"))
            cell = tk.Frame(metric_row, bg=background)
            cell.grid(row=index // columns, column=index % columns,
                      sticky="ew", padx=(0, 14), pady=(0, 2))
            self._card_text(cell, caption, bg=background, fg=theme["card_lbl"],
                            font=(self.base_font[0], self.base_font[1] - 1)).pack(fill="x")
            self._card_text(cell, text, bg=background, fg=color,
                            font=value_font).pack(fill="x")
        return outer

    def rows_to_tsv(self, rows: Sequence[DiskRow] | None = None) -> str:
        """把行导成 TSV。与当前视图无关，卡片视图下复制表格也不会报错。"""
        headers = [spec[1] for spec in COLUMN_SPECS]
        lines = ["	".join(headers)]
        for row in (self.rows if rows is None else rows):
            lines.append("	".join(str(value).replace(PAIR_SEPARATOR, TSV_PAIR_SEPARATOR) for value in _row_values(row)))
        return "\n".join(lines)

    # ---------- 数据刷新 ----------
    def refresh_async(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.refresh_button.configure(state="disabled")
        self.smartctl_label.configure(text="数据源：读取中…")
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
            self.smartctl_label.configure(text="数据源：读取异常")
            self._append_hint(f"读取失败：{item}")
            return
        self.apply_outcome(item)

    def apply_outcome(self, outcome: RunOutcome) -> RunOutcome:
        self.clear_rows()
        self.rows = list(outcome.rows)
        self.render_rows(self.rows)

        self.prev_label.configure(text=f"上次记录：{outcome.previous_stamp or '—'}")
        self.cur_label.configure(text=f"本次记录：{outcome.current_stamp}")
        self.smartctl_label.configure(
            text=f"数据源：{outcome.smartctl_path}" if outcome.smartctl_path
            else f"数据源：{outcome.provider_status}"
        )
        if outcome.smartctl_path is None and outcome.provider_status:
            self._append_hint(SMARTCTL_INSTALL_HINT)

        totals = outcome.totals
        if outcome.baseline:
            self.summary_label.configure(text="已记录基线，下次打开将显示增量")
        else:
            self.summary_label.configure(
                text=(f"本次总读增 {fmt_delta(totals.get('read_delta'))} GB ｜ "
                      f"本次总写增 {fmt_delta(totals.get('write_delta'))} GB ｜ "
                      f"历史总读增 {fmt_delta(totals.get('hist_read_delta'))} GB ｜ "
                      f"历史总写增 {fmt_delta(totals.get('hist_write_delta'))} GB")
            )
        for key, label in self.kpi_values.items():
            label.configure(text="—" if outcome.baseline else fmt_delta_compact(totals.get(key)))
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

    # ---------- 菜单动作 ----------
    def close(self) -> None:
        """关闭窗口即退出：不留后台线程、不写服务、不设自启。"""
        try:
            self.root.destroy()
        except Exception:
            pass

    def copy_table(self) -> None:
        """把当前数据导成 TSV 放进剪贴板。

        直接由 rows 生成（不读 Treeview 的 values），因此卡片/双列视图下同样可用。
        """
        text = self.rows_to_tsv()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._append_hint(f"已复制 {len(self.rows)} 行数据到剪贴板。")
        except Exception as exc:
            self._append_hint(f"复制到剪贴板失败：{exc}")

    def open_data_dir(self) -> None:
        """打开 history.json / settings.json 所在目录。"""
        try:
            directory = self.store.resolve_dir()
            directory.mkdir(parents=True, exist_ok=True)
            if platform.system() == "Windows":
                os.startfile(str(directory))  # type: ignore[attr-defined]
            else:
                import webbrowser
                webbrowser.open(directory.as_uri())
        except Exception as exc:
            self._append_hint(f"打开数据目录失败：{exc}")

    def diagnostics_text(self) -> str:
        """诊断信息：出问题时让用户能一键复制出来。"""
        return "\n".join((
            f"{APP_DISPLAY_NAME} v{APP_VERSION}（{APP_EXE_NAME}.exe）",
            f"Python：{platform.python_version()} / {platform.platform()}",
            f"主题：{THEME_LABELS.get(self.theme_key, self.theme_key)}"
            f" → {self.theme['label']}",
            f"权限：{'管理员' if is_admin() else '未以管理员运行'}",
            f"数据源：{self.smartctl_label.cget('text')}",
            f"上次记录：{self.prev_label.cget('text')}",
            f"本次记录：{self.cur_label.cget('text')}",
            f"历史文件：{self.store.path}",
            f"偏好文件：{self.settings.path}",
            f"视图：{VIEW_LABELS.get(self.view_key, self.view_key)}",
            f"磁盘行数：{len(self.rows)}",
        ))

    def copy_diagnostics(self) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.diagnostics_text())
            self._append_hint("已复制诊断信息到剪贴板。")
        except Exception as exc:
            self._append_hint(f"复制诊断信息失败：{exc}")

    def show_source_status(self) -> None:
        from tkinter import messagebox

        messagebox.showinfo("数据源状态", self.diagnostics_text(), parent=self.root)

    def show_source_help(self) -> None:
        from tkinter import messagebox

        messagebox.showinfo(
            "如何安装数据源",
            "本程序通过 smartmontools 的 smartctl 读取磁盘累计读写量与 0E。\n"
            "\n"
            "通常 exe 已自带一份，无需安装；若顶部提示未找到，可任选一种：\n"
            "1) winget install smartmontools.smartmontools\n"
            "2) 到 smartmontools 官网下载安装包，默认装到\n"
            "   C:\\Program Files\\smartmontools\\bin\\\n"
            "\n"
            "安装后点「文件 → 刷新记录」即可。读取磁盘信息通常需要管理员权限。",
            parent=self.root,
        )

    def show_capacity_help(self) -> None:
        from tkinter import messagebox

        messagebox.showinfo(
            "容量与换算说明",
            f"统一口径：{BYTES_PER_GIB_TEXT}\n"
            "\n"
            "累计读取 / 累计写入来自硬盘自己维护的计数器：\n"
            "· NVMe：Data Units Read / Written，1 Data Unit = 512000 字节\n"
            "· SATA：Device Statistics 的 Logical Sectors，1 LBA = 512 字节\n"
            "  （该字段缺失时才退回 SMART 属性 241/242，部分主控按块计数，数值可能偏小）\n"
            "\n"
            "0E 是 NVMe 的「介质与数据完整性错误」计数，只增不减；\n"
            "SATA 盘没有这个字段，显示「不适用」，可参考重映射扇区与待定扇区。",
            parent=self.root,
        )

    # ---------- 关于 ----------
    def show_about(self) -> None:
        """弹出版本/来源说明。正文完全由 about_text() 生成，只引用 APP_VERSION。"""
        from tkinter import messagebox

        messagebox.showinfo(f"关于 {window_title()}", about_text(self.store.path), parent=self.root)

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
               root: Any = None, settings: SettingsStore | None = None,
               theme: str | None = None) -> DiskTrackerApp:
    import tkinter as tk

    owns_root = root is None
    window = root if root is not None else tk.Tk()
    app = DiskTrackerApp(window, provider=provider, store=store, autostart=False,
                         settings=settings, theme=theme)
    if owns_root:
        app.owns_root = True
    return app


def main(argv: Sequence[str] | None = None) -> int:
    import tkinter as tk

    root = tk.Tk()
    store = HistoryStore()
    DiskTrackerApp(root, provider=SmartctlProvider(), store=store, autostart=True,
                   settings=SettingsStore(store.resolve_dir()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
