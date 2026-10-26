"""运行编排层：一次完整运行 = 读取 → 记录 → 对比 → 产出 RunOutcome（GUI 与测试共用同一条流水线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from .delta import any_media_alert, build_rows, summarize
from .i18n import tr
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
    recorded: int = 0          # 本次成功写进 history.json 的盘数
    failed: int = 0            # 本次读取失败的盘数（不进历史，也不参与增量）
    # 重算用的原始输入（切换界面语言时要按新语言重跑一遍对比）。
    # 只保存「本次运行之前」的历史记录，与 perform_run 里 build_rows 的入参一致。
    records: list[dict] = field(default_factory=list)
    snapshots: list[Any] = field(default_factory=list)


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
            # 备份文件名里的 "<日期>" 是占位说明，不是真文件名，英文界面下也要跟着换
            message = tr("历史文件已升级为新格式（v2），旧文件备份为 {name}",
                         name=f"{HISTORY_BACKUP_PREFIX}{tr('<日期>')}{store.path.suffix}")

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
        recorded=len(new_records),
        failed=sum(1 for snapshot in result.snapshots if not snapshot.ok),
        records=list(load.records),
        snapshots=list(result.snapshots),
    )


def rebuild_rows(outcome: RunOutcome) -> list[DiskRow]:
    """按当前界面语言重算表格行（切换语言时用，不动 history.json）。

    DiskRow 里的状态文案、0E 文案是 build_rows 当场拼好的成品串，语言一变就得重拼。
    这里用 RunOutcome 存下的「本次运行前的历史 + 本次快照」原样重跑一遍对比，
    纯计算、无副作用：不读盘、不写历史、不产生新的时间戳。
    """
    if not outcome.snapshots:
        return list(outcome.rows)
    return build_rows(outcome.records, outcome.snapshots)
