"""主题层：纯数据表 + 解析函数。
"""

from __future__ import annotations

import platform
from typing import Any


# ---------------------------------------------------------------------------
# 主题层：纯数据表 + 解析函数
#
# 用户选定的方案是「跟随系统」：读 Windows 的「应用模式」设置，
#   系统浅色 -> 数据台账（THEME_LEDGER）
#   系统深色 -> 深色专业（THEME_DARK）
# 其余四套作为手动可选项保留在「主题」菜单里。
# 纯标准库（ctypes/winreg/ttk），不引入第三方主题包。
# ---------------------------------------------------------------------------

THEME_SYSTEM = "system"
THEME_LEDGER = "ledger"
THEME_DARK = "dark"
THEME_GAUGE = "gauge"
THEME_MINIMAL = "minimal"
THEME_NATIVE = "native"

THEME_ORDER: tuple[str, ...] = (THEME_SYSTEM, THEME_LEDGER, THEME_DARK,
                                THEME_GAUGE, THEME_MINIMAL, THEME_NATIVE)
THEME_LABELS = {
    THEME_SYSTEM: "跟随系统",
    THEME_LEDGER: "数据台账",
    THEME_DARK: "深色专业",
    THEME_GAUGE: "清爽仪表",
    THEME_MINIMAL: "极简留白",
    THEME_NATIVE: "原生默认",
}
# 「跟随系统」解析到的具体主题
THEME_SYSTEM_LIGHT = THEME_LEDGER
THEME_SYSTEM_DARK = THEME_DARK

REG_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
REG_LIGHT_VALUE = "AppsUseLightTheme"

# 主题表：全部参数集中在此，apply_theme() 只照着表 set，不再有第二处颜色字面量。
#   base        ttk 主题名（vista=系统原生；clam 支持自定义表头底色）
#   kpi         None=无指标卡，cards=普通卡片，bordered=带描边卡片
THEMES: dict[str, dict[str, Any]] = {
    THEME_NATIVE: {
        "key": THEME_NATIVE, "label": "原生默认", "base": "vista",
        "win_bg": "#F0F0F0", "tree_bg": "#FFFFFF", "tree_fg": "#1A1A1A",
        "head_bg": None, "head_fg": "#1A1A1A", "head_bold": False,
        "zebra": None, "alert_bg": None, "alert_fg": "#C00000", "muted_fg": "#808080",
        "select_bg": "#0078D7", "select_fg": "#FFFFFF",
        "rowheight": 38, "padx": 10, "pady": 8, "kpi": None,
        "title_fg": "#000000", "title_pt": 11,
        "hint_fg": "#8A6D00", "ok_fg": "#127A32", "note_fg": "#606060", "summary_fg": "#1A1A1A",
        "card_bg": "#FFFFFF", "card_border": None, "card_val": "#1A1A1A",
        "card_lbl": "#606060", "num_family": "Consolas", "card_pt": 12,
        "scroll_thumb": "#C6CDD6", "scroll_thumb_hover": "#9AA6B2",
        "scroll_idle": "#E4E8ED",
        "seg_sel_bg": "#0078D7", "seg_sel_fg": "#FFFFFF",
        "seg_hover_bg": "#E5F1FB", "seg_hover_fg": "#1A1A1A",
        "seg_border": "#B8B8B8",
    },
    THEME_LEDGER: {
        "key": THEME_LEDGER, "label": "数据台账", "base": "clam",
        "win_bg": "#FFFFFF", "tree_bg": "#FFFFFF", "tree_fg": "#1F2933",
        "head_bg": "#2B3440", "head_fg": "#FFFFFF", "head_bold": True,
        "zebra": "#F7F9FB", "alert_bg": "#FDECEA", "alert_fg": "#B32D1F",
        "muted_fg": "#8A8A8A",
        "select_bg": "#DCE9F7", "select_fg": "#1F2933",
        "rowheight": 42, "padx": 12, "pady": 10, "kpi": "cards",
        "title_fg": "#2B3440", "title_pt": 12,
        "hint_fg": "#8A6D00", "ok_fg": "#1B7F49", "note_fg": "#7B8794", "summary_fg": "#1F2933",
        "card_bg": "#F4F7FA", "card_border": None, "card_val": "#1F2933",
        "card_lbl": "#7B8794", "num_family": "Consolas", "card_pt": 12,
        "scroll_thumb": "#C6CDD6", "scroll_thumb_hover": "#9AA6B2",
        "scroll_idle": "#E4E8ED",
        "seg_sel_bg": "#2B3440", "seg_sel_fg": "#FFFFFF",
        "seg_hover_bg": "#E3E9F0", "seg_hover_fg": "#1F2933",
        "seg_border": "#D5DDE6",
    },
    THEME_DARK: {
        "key": THEME_DARK, "label": "深色专业", "base": "clam",
        "win_bg": "#1E2227", "tree_bg": "#23282E", "tree_fg": "#D6DEE7",
        "head_bg": "#171B20", "head_fg": "#A8C4DC", "head_bold": True,
        "zebra": "#272D34", "alert_bg": "#3A2427", "alert_fg": "#FF8A7A",
        "muted_fg": "#6B7580",
        "select_bg": "#35526B", "select_fg": "#FFFFFF",
        "rowheight": 42, "padx": 12, "pady": 10, "kpi": "cards",
        "title_fg": "#E6EDF3", "title_pt": 12,
        "hint_fg": "#E0C070", "ok_fg": "#6FCF97", "note_fg": "#8B97A3", "summary_fg": "#D6DEE7",
        "card_bg": "#262C33", "card_border": None, "card_val": "#7FC8A9",
        "card_lbl": "#8B97A3", "num_family": "Consolas", "card_pt": 12,
        "scroll_thumb": "#7C8A99", "scroll_thumb_hover": "#9AA9B8",
        "scroll_idle": "#2E353D",
        "seg_sel_bg": "#3E6E8E", "seg_sel_fg": "#F2F7FB",
        "seg_hover_bg": "#2F3942", "seg_hover_fg": "#D6DEE7",
        "seg_border": "#3A444D",
    },
    THEME_GAUGE: {
        "key": THEME_GAUGE, "label": "清爽仪表", "base": "clam",
        "win_bg": "#F5F8FB", "tree_bg": "#FFFFFF", "tree_fg": "#22303C",
        "head_bg": "#EAF0F6", "head_fg": "#51606F", "head_bold": True,
        "zebra": "#FAFCFE", "alert_bg": "#FDECEA", "alert_fg": "#B32D1F",
        "muted_fg": "#93A1AE",
        "select_bg": "#DCE9F7", "select_fg": "#1B6FB8",
        "rowheight": 44, "padx": 16, "pady": 14, "kpi": "bordered",
        "title_fg": "#1B2A38", "title_pt": 13,
        "hint_fg": "#8A6D00", "ok_fg": "#1B7F49", "note_fg": "#7C8B99", "summary_fg": "#22303C",
        "card_bg": "#FFFFFF", "card_border": "#DCE4EC", "card_val": "#1B6FB8",
        "card_lbl": "#7C8B99", "num_family": "Consolas", "card_pt": 15,
        "scroll_thumb": "#C8D3DE", "scroll_thumb_hover": "#9DB0C4",
        "scroll_idle": "#E9EFF5",
        "seg_sel_bg": "#1B6FB8", "seg_sel_fg": "#FFFFFF",
        "seg_hover_bg": "#E6EEF6", "seg_hover_fg": "#1B2A38",
        "seg_border": "#DCE4EC",
    },
    THEME_MINIMAL: {
        "key": THEME_MINIMAL, "label": "极简留白", "base": "clam",
        "win_bg": "#FFFFFF", "tree_bg": "#FFFFFF", "tree_fg": "#2A2A2A",
        "head_bg": "#FFFFFF", "head_fg": "#8A8A8A", "head_bold": False,
        "zebra": None, "alert_bg": None, "alert_fg": "#D0342C", "muted_fg": "#A0A0A0",
        "select_bg": "#EFEFEF", "select_fg": "#1A1A1A",
        "rowheight": 46, "padx": 22, "pady": 18, "kpi": None,
        "title_fg": "#1A1A1A", "title_pt": 12,
        "hint_fg": "#8A6D00", "ok_fg": "#1B7F49", "note_fg": "#A0A0A0", "summary_fg": "#2A2A2A",
        "card_bg": "#FFFFFF", "card_border": None, "card_val": "#1A1A1A",
        "card_lbl": "#A0A0A0", "num_family": "Consolas", "card_pt": 12,
        "scroll_thumb": "#D8D8D8", "scroll_thumb_hover": "#B8B8B8",
        "scroll_idle": "#EFEFEF",
        "seg_sel_bg": "#2A2A2A", "seg_sel_fg": "#FFFFFF",
        "seg_hover_bg": "#F0F0F0", "seg_hover_fg": "#1A1A1A",
        "seg_border": "#E2E2E2",
    },
}


def detect_system_dark_mode() -> bool | None:
    """Windows「应用模式」是否为深色。True=深色，False=浅色，None=读不到（非 Windows 等）。"""
    if platform.system() != "Windows":
        return None
    try:
        import winreg  # 仅 Windows 才导入

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PERSONALIZE) as key:
            value, _kind = winreg.QueryValueEx(key, REG_LIGHT_VALUE)
        return int(value) == 0
    except Exception:
        return None


def normalize_theme_key(key: Any) -> str:
    """把任意输入收敛成合法主题键，非法值回落「跟随系统」。"""
    if isinstance(key, str) and (key in THEMES or key == THEME_SYSTEM):
        return key
    return THEME_SYSTEM


def system_theme_name(dark_mode: bool | None = None) -> str:
    """「跟随系统」当前解析到的具体主题名。"""
    dark = detect_system_dark_mode() if dark_mode is None else bool(dark_mode)
    return THEME_SYSTEM_DARK if dark else THEME_SYSTEM_LIGHT


def resolve_theme(key: Any, dark_mode: bool | None = None) -> dict[str, Any]:
    """主题键 -> 具体主题参数表。key 为 system 时按系统配色落到浅色/深色两套之一。"""
    normalized = normalize_theme_key(key)
    if normalized == THEME_SYSTEM:
        return dict(THEMES[system_theme_name(dark_mode)])
    return dict(THEMES[normalized])
