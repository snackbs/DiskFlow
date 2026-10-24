"""增量计算层：把本次读数与历史记录对比，算出本次区间增量与历史累计增量，并判定 0E。
"""

from __future__ import annotations

from typing import Sequence

from .config import COUNTER_SOURCE_ATTR
from .convert import _as_int
from .models import DiskRow, DiskSnapshot, MediaErrorStatus, disk_key
from .timefmt import format_interval, timestamp_seconds


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
                drives=snapshot.drives,
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
            drives=snapshot.drives,
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
