"""时间格式化层：ISO 8601 时间戳解析、秒数比较、区间格式化（X 天 Y 小时 Z 分钟）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .i18n import is_english, tr


# ---------------------------------------------------------------------------
# 时间格式化层
# ---------------------------------------------------------------------------


def format_duration(total_seconds: float) -> str:
    """秒 -> 'X 天 Y 小时 Z 分钟'。传入负数时按绝对值格式化。

    英文界面用紧凑写法 '2 d 2 h 30 min'：这一列在表格里只有 152px 宽，
    '2 days 2 hours 30 minutes' 会被截断。
    """
    seconds = int(abs(float(total_seconds)))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if is_english():
        return f"{days} d {hours} h {minutes} min"
    return f"{days} 天 {hours} 小时 {minutes} 分钟"


def format_interval(seconds: float | None) -> str:
    """距上次的时间间隔。None -> '—'；时钟回拨（负数）显式标注。"""
    if seconds is None:
        return "—"
    if seconds < 0:
        return tr("时钟回拨 {duration}", duration=format_duration(-float(seconds)))
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


def format_utc_offset(dt: datetime) -> str:
    """把时区偏移写成 UTC+8 / UTC+5:30 / UTC-3 / UTC+0 这种紧凑形式。

    整点时省略「:00」，非整点时区（如印度 UTC+5:30）保留分钟；UTC 本身写 UTC+0
    而不是 UTC，这样与其它偏移放在一起看是对齐的一列。
    """
    offset = dt.utcoffset()
    if offset is None:
        return "UTC+0"
    total_minutes = int(offset.total_seconds()) // 60
    sign = "-" if total_minutes < 0 else "+"
    hours, minutes = divmod(abs(total_minutes), 60)
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    return f"UTC{sign}{hours}"


def format_stamp(value: Any) -> str:
    """ISO 时间戳 -> 界面用的可读写法：2026-04-30 08:00:00 (UTC+8)。

    ISO 原文（2026-04-30T08:00:00+08:00）里的 T 和偏移对普通用户不友好，
    顶部状态栏直接显示可就地读出日期与时间。取不到时区信息（naive）时标注
    「本地时间」，无法解析的文本原样返回，绝不抛异常也不吞掉原文。
    """
    if value is None:
        return "—"
    dt = parse_timestamp(value)
    if dt is None:
        text = str(value).strip()
        return text or "—"
    body = dt.strftime("%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None or dt.utcoffset() is None:
        prefix = " (local time)" if is_english() else " (本地时间)"
        return f"{body}{prefix}"
    return f"{body} ({format_utc_offset(dt)})"
