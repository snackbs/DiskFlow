"""时间格式化层：ISO 8601 时间戳解析、秒数比较、区间格式化（X 天 Y 小时 Z 分钟）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


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
