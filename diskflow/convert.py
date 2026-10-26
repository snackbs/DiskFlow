"""容量换算层：字节 / LBA / NVMe Data Unit → GB，以及各种展示字符串。
"""

from __future__ import annotations

from typing import Any

from .config import BYTES_PER_GB, LBA_BYTES, NVME_DATA_UNIT_BYTES


# 容量读不到时的占位符（与表格、卡片里的其它占位保持一致，都用全角破折号）
CAPACITY_UNKNOWN_TEXT = "—"


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
    """只有数字的增量：+348.80。

    调用方**必须**在同一句里补上单位（底部汇总就是这么用的：
    `f"本次总读增 {fmt_delta(v)} GB"`）。凡是独立成格、不跟列头文字的地方，
    一律改用 fmt_delta_gb / fmt_delta_short，否则看的人不知道是 GB 还是次数。
    """
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:,.{digits}f}"


def fmt_delta_gb(value: float | None, digits: int = 2) -> str:
    """增量带单位的写法（卡片用）：+348.80 GB。

    卡片与表格的增量格是独立小字，没有列头替它写「(GB)」，所以这里必须自带单位。
    """
    if value is None:
        return "—"
    return f"{fmt_delta(value, digits)} GB"


def fmt_delta_short(value: float | None, digits: int = 2) -> str:
    """表格合并列用的增量：带单位，大数换 TB，保证一格放得下。

    表格一列只有 108px 宽、还要上下叠两个值，所以不能像卡片那样一律写 GB：
    增量超过 1 TB 时改用 TB（+31.96 TB），否则用 GB（+348.80 GB）。
    两种写法都带单位，不会出现「只有数字」的情况。
    """
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    if abs(value) >= 1024.0:
        return f"{sign}{value / 1024.0:,.{digits}f} TB"
    return f"{sign}{value:,.{digits}f} GB"


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


def fmt_capacity(value: Any) -> str:
    """硬盘标称容量双单位显示：厂商标称（十进制）+ 系统可见（二进制）。

    同一个字节数有两种常见口径，界面上把两个都给出来，避免用户对不上：

    * 括号外：厂商标称容量，按 1 GB = 1,000,000,000 字节计算，
      所以 960197124096 字节写成 "960 GB"、4000787030016 写成 "4 TB"；
    * 括号内：操作系统里的可用量，按 1 GiB = 1024^3 字节计算，
      写成 "894.25 GiB" / "3.64 TiB"。

    实测样本：960197124096 -> "960 GB (894.25 GiB)"；
    4000787030016 -> "4 TB (3.64 TiB)"；512110190592 -> "512 GB (476.94 GiB)"；
    1000204886016 -> "1 TB (931.51 GiB)"（标称 1TB，但没到 1 TiB，括号仍走 GiB）。

    括号用 ASCII 半角：这个串会进表格、卡片、TSV 与剪贴板，半角括号在
    等宽字体与 Excel 里都不会撑宽对齐。

    None、0、负数一律返回「—」，绝不因为取不到容量让界面报错。
    """
    number = _as_int(value)
    if number is None or number <= 0:
        return CAPACITY_UNKNOWN_TEXT

    # 括号外：厂商标称值。GB 档取整（960、512），TB 档保留两位并去掉末尾 0（4、1.5）。
    gb = number / 1_000_000_000
    if round(gb) >= 1000:
        vendor = f"{gb / 1000:,.2f}".rstrip("0").rstrip(".") + " TB"
    else:
        vendor = f"{round(gb):,} GB"

    # 括号内：二进制口径。1024 GiB 以上才升到 TiB。
    gib = number / BYTES_PER_GB
    if gib >= 1024:
        binary = f"{gib / 1024:,.2f} TiB"
    else:
        binary = f"{gib:,.2f} GiB"

    return f"{vendor} ({binary})"

