"""容量换算层：字节 / LBA / NVMe Data Unit → GB，以及各种展示字符串。
"""

from __future__ import annotations

from typing import Any

from .config import BYTES_PER_GB, LBA_BYTES, NVME_DATA_UNIT_BYTES


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


def fmt_bytes_size(value: Any) -> str:
    """字节数 -> 人读的文件体积（B / KB / MB / GB），用于「历史文件：…（12.3 KB）」。

    与容量换算的 GB 口径无关：这里就是磁盘上的文件体积，所以阶梯用 1024。
    None、负数、非法值一律返回「—」，绝不因为取不到大小让界面报错。
    """
    number = _as_int(value)
    if number is None or number < 0:
        return "—"
    if number < 1024:
        return f"{number} B"
    if number < 1024 ** 2:
        return f"{number / 1024:,.1f} KB"
    if number < 1024 ** 3:
        return f"{number / 1024 ** 2:,.1f} MB"
    return f"{number / 1024 ** 3:,.2f} GB"
