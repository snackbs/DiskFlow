"""数据读取层第一步：把 smartctl 的 JSON 输出解析成 DiskSnapshot（纯函数，便于单测）。
"""

from __future__ import annotations

from typing import Any

from .config import (
    ATA_DEVSTAT_READ_NAME,
    ATA_DEVSTAT_WRITE_NAME,
    COUNTER_SOURCE_ATTR,
    COUNTER_SOURCE_DEVSTAT,
    COUNTER_SOURCE_NONE,
    COUNTER_SOURCE_NVME,
    LBA_BYTES,
    NVME_DATA_UNIT_BYTES,
    SATA_LIFE_ATTRIBUTES,
    SATA_LIFE_RAW_PERCENT,
)
from .convert import _as_int
from .models import DiskSnapshot


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
