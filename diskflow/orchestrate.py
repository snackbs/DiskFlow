"""运行编排层：一次完整运行 = 读取 → 记录 → 对比 → 产出 RunOutcome（GUI 与测试共用同一条流水线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from .delta import any_media_alert, build_rows, summarize
from .i18n import LocalizedMessage
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
    # 这两条要显示在界面上，且必须能跟着切语言，所以存「键 + 参数」而不是成品串。
    # 旧调用方（测试、外部 provider）传裸 str 也能跑：apply_outcome 直接当路径/成品显示。
    provider_status: str | LocalizedMessage
    history_message: str | LocalizedMessage
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

    new_records = records_from_snapshots(result.snapshots, when)

    # 「本次运行之前」的判定基准。默认是锁外读到的快照，但 append() 会在锁里
    # 重新读一遍现状——期间别的实例可能又写进了记录。那时还拿锁外这份当「上一次」，
    # 界面上的「距上次 / 本次增量」就和文件里真正的前一条记录对不上，用户回头核对
    # 历史会发现数字说不通。所以 append 之后改用锁内那份（见下方）。
    baseline_records = load.records

    # 优先留结构化原件：没有它就只能显示读盘那一刻的语言，切语言不跟着变。
    message: str | LocalizedMessage = load.message_obj or load.message
    if new_records:
        # 用 append() 而不是 save(load.records + new)：在锁内重新读一次现状再追加。
        # 用的是「本次运行前」的 load.records 去拼，就等于拿自己的旧快照整体覆盖，
        # 期间别的实例写进去的读数会被抹掉（m31199 指出的 P0-1 后半）。
        # 必须接住返回值：锁内才发现的「文件在两次读之间坏了 / 旧格式要迁移」
        # 只在这个返回值里，丢了就等于把「已重建基线」这件事吞掉，用户看到的是
        # 一份好端端的空历史——上一次的读数像是被程序自己删了。
        saved = store.append(new_records)
        if saved.recovered and saved.message_obj is not None and not message:
            message = saved.message_obj
        if (saved.migrated or load.migrated) and not message:
            # 备份文件名里的 "<日期>" 是占位说明，不是真文件名，英文界面下也要跟着换。
            # 这里必须嵌结构化原件而不是 tr() 的成品串：成品串在派活那一刻就把语言
            # 焊死了，界面切到英文后外层翻了、这截仍是中文（第 2 条的后半）。
            message = LocalizedMessage(
                "历史文件已升级为新格式（v2），旧文件备份为 {name}",
                {"name": LocalizedMessage(
                    "{prefix}{date}{suffix}",
                    {"prefix": HISTORY_BACKUP_PREFIX, "date": LocalizedMessage("<日期>"),
                     "suffix": store.path.suffix})})
        # saved.records = 锁内现状 + 本次新记录，砍掉尾部才是「本次之前」。
        # 不能整份拿来当基准：那样本次快照会跟它自己刚写进去的那条比，
        # 增量恒为 0，表格一片「已更新记录」，等于把增量功能关掉了。
        appended = len(new_records)
        if 0 < appended <= len(saved.records):
            baseline_records = saved.records[:-appended]
        else:  # pragma: no cover - append 契约上一定会带上这批记录
            baseline_records = saved.records

    rows = build_rows(baseline_records, result.snapshots)

    return RunOutcome(
        rows=rows,
        totals=summarize(rows),
        media_alert=any_media_alert(rows),
        previous_stamp=latest_timestamp(baseline_records),
        current_stamp=current_stamp,
        provider_status=result.status,
        history_message=message,
        history_path=str(store.path),
        smartctl_path=result.smartctl_path,
        baseline=not baseline_records,
        recorded=len(new_records),
        failed=sum(1 for snapshot in result.snapshots if not snapshot.ok),
        records=list(baseline_records),
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
