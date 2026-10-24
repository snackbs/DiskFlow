"""运行编排层：一次完整运行 = 读取 → 记录 → 对比 → 产出 RunOutcome（GUI 与测试共用同一条流水线）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from .delta import any_media_alert, build_rows, summarize
from .diskmap import attach_drive_letters
from .history import HISTORY_BACKUP_PREFIX, HistoryStore, latest_timestamp, records_from_snapshots
from .models import DiskRow


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
                now: datetime | None = None,
                resolver: Callable[[Sequence[Any]], dict[str, str]] | None = None
                ) -> RunOutcome:
    """读盘 -> 读历史 -> 计算增量 -> 追加记录。GUI 与端到端测试共用。

    resolver 为空时走真实的「物理盘 + 盘符」枚举；测试可注入假映射函数，
    这样 CI 上的结果不受 runner 自身磁盘影响。
    """
    when = now or datetime.now().astimezone()
    current_stamp = when.isoformat(timespec="seconds")

    result = provider.read_all()
    load = store.load()

    for snapshot in result.snapshots:
        if snapshot.ok:
            setattr(snapshot, "_now_hint", current_stamp)

    # 盘符徽章：只影响界面展示，拿不到就留空（不影响增量与历史）
    attach_drive_letters(result.snapshots, resolver)

    rows = build_rows(load.records, result.snapshots)
    new_records = records_from_snapshots(result.snapshots, when)

    message = load.message
    if new_records:
        store.save(list(load.records) + new_records)
        if load.migrated and not message:
            message = (f"历史文件已升级为新格式（v2），旧文件备份为 "
                       f"{HISTORY_BACKUP_PREFIX}<日期>{store.path.suffix}")

    return RunOutcome(
        rows=rows,
        totals=summarize(rows),
        media_alert=any_media_alert(rows),
        previous_stamp=latest_timestamp(load.records),
        current_stamp=current_stamp,
        provider_status=result.status,
        history_message=message,
        history_path=str(store.path),
        smartctl_path=result.smartctl_path,
        baseline=not load.records,
    )
