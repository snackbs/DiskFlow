"""数据模型：磁盘读取结果与增量对比后的行数据。

数据读取层与 GUI 层之间只通过这里的结构体通信，GUI 里不存在任何 smartctl 调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import HEALTH_NOT_AVAILABLE
from .convert import bytes_to_gb


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
    drives: str = ""                   # 本机挂载的盘符（如 "C:, D:"），由盘符映射层填充

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
    drives: str = ""          # 本机挂载的盘符（卡片上的盘符徽章）
