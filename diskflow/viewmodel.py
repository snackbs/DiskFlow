"""数据展示视图定义（三种）：表格 / 卡片 / 双列。纯数据表。
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# 数据展示视图（三种）：表格 / 卡片 / 双列
#   视图偏好记在 settings.json 的 "view" 键里，绝不写进 history.json。
# ---------------------------------------------------------------------------

VIEW_TABLE = "table"
VIEW_CARDS = "cards"
VIEW_GRID = "grid"
VIEW_ORDER: tuple[str, ...] = (VIEW_TABLE, VIEW_CARDS, VIEW_GRID)
VIEW_LABELS = {VIEW_TABLE: "表格", VIEW_CARDS: "卡片", VIEW_GRID: "双列"}
DEFAULT_VIEW = VIEW_TABLE

# 卡片视图里每块盘展示的指标格：(标题, DiskRow 字段名)
CARD_METRICS: tuple[tuple[str, str], ...] = (
    ("累计读取", "read_gb"),
    ("累计写入", "write_gb"),
    ("距上次", "interval_text"),
    ("本次读增", "read_delta_gb"),
    ("本次写增", "write_delta_gb"),
    ("历史读增", "hist_read_delta_gb"),
    ("历史写增", "hist_write_delta_gb"),
    ("0E", "media_text"),
    ("健康度", "health_text"),
)
# 双列视图里每张卡只有一半宽度，按行排 9 项时「本次读增」落在第 2 行第 1 格、
# 「历史读增」却跑到第 3 行第 3 格，同类指标被拆开，眼睛得来回跳。
# 所以双列单用一份按列成组的排布：第 1 列全是读取（累计 → 本次 → 历史，时间倒序往下走），
# 第 2 列全是写入（同样三段），第 3 列放时长 / 0E / 健康度这些上下文。
# 这样「本次读增」正对「历史读增」，「本次写增」正对「历史写增」。
CARD_METRICS_GRID: tuple[tuple[str, str], ...] = (
    ("累计读取", "read_gb"),
    ("累计写入", "write_gb"),
    ("距上次", "interval_text"),
    ("本次读增", "read_delta_gb"),
    ("本次写增", "write_delta_gb"),
    ("0E", "media_text"),
    ("历史读增", "hist_read_delta_gb"),
    ("历史写增", "hist_write_delta_gb"),
    ("健康度", "health_text"),
)
# 指标格每行几列：单列卡片宽，放 5 列；双列时每张卡只有一半宽度，放 3 列
CARD_METRIC_COLUMNS = 5
CARD_METRIC_COLUMNS_GRID = 3


def normalize_view_key(key: Any) -> str:
    """把任意输入收敛成合法视图键，非法值回落表格视图。"""
    if isinstance(key, str) and key in VIEW_ORDER:
        return key
    return DEFAULT_VIEW
