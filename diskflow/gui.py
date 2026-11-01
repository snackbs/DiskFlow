"""GUI 层（不含任何 smartctl 调用）。

单窗口 tkinter/ttk：顶部状态栏 + 主表格/卡片视图 + 底部汇总与按钮。
磁盘读取在后台线程里跑，界面显示「读取中…」，完成后一次性刷新表格。
"""

from __future__ import annotations

import os
import platform
import threading
import traceback
import unicodedata
from dataclasses import field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import (
    APP_DISPLAY_NAME,
    APP_EXE_NAME,
    APP_VERSION,
    BYTES_PER_GIB_TEXT,
    about_text,
    window_title,
)
from .convert import (
    CAPACITY_UNKNOWN_TEXT,
    fmt_bytes_size,
    fmt_capacity,
    fmt_delta,
    fmt_delta_gb,
    fmt_delta_short,
    fmt_size_short,
)
from .history import SCHEMA_VERSION, HistoryStore
from .i18n import (
    LANG_LABELS,
    LANG_ORDER,
    set_language,
    tr,
)
from .models import DiskRow
from .orchestrate import RunOutcome, perform_run, rebuild_rows
from .platformhelp import (
    MSC_DEVICE_MANAGER,
    MSC_DISK_MANAGEMENT,
    is_admin,
    open_management_console,
)
from .prefs import DEFAULT_SETTINGS, SettingsStore
from .providers import SmartctlProvider
from .timefmt import format_stamp
from .theme import (
    SURFACE_CANVAS,
    SURFACE_TABLE,
    THEMES,
    THEME_LABELS,
    THEME_LEDGER,
    THEME_ORDER,
    THEME_PHOTO,
    THEME_SYSTEM,
    normalize_theme_key,
    resolve_theme,
    theme_surface,
)
from .viewmodel import (
    CARD_METRICS,
    CARD_METRICS_GRID,
    CARD_METRIC_COLUMNS,
    CARD_METRIC_COLUMNS_GRID,
    VIEW_CARDS,
    VIEW_GRID,
    VIEW_LABELS,
    VIEW_ORDER,
    VIEW_TABLE,
    normalize_view_key,
)


# ---------------------------------------------------------------------------
# GUI 层（不含任何 smartctl 调用）
# ---------------------------------------------------------------------------

# 细圆角滚动条：ttk.Scrollbar 的滑块由主题元素绘制，永远是直角矩形且带上下滑轨，
# 想做成"圆角细条"只能自绘。这里用 tk.Canvas 画，并实现 Treeview/Canvas 需要的
# set()/get() 协议，因此可以直接挂在 yscrollcommand / xscrollcommand 上。
try:                                   # 无 Tk 的环境也能 import 本模块（测试可只跑逻辑层）
    import tkinter as _tk_for_scrollbar
    from tkinter import font as tkfont
except Exception:                       # pragma: no cover - 依赖系统 Tk
    _tk_for_scrollbar = None
    tkfont = None                       # type: ignore[assignment]

# 第 7 套「图片背景」主题的数据区控件与壁纸层。canvas_surface 依赖真实 Tk，
# 没有 Tk 的环境（只跑逻辑层的测试）里退化成 None，表格回落到 Treeview。
try:                                    # pragma: no cover - 依赖系统 Tk
    from .canvas_surface import CanvasTable
except Exception:
    CanvasTable = None                  # type: ignore[assignment]
from . import wallpaper as _wallpaper

# 第 7 套主题的外壳层：容器本身不动，只往里垫一层「背景切片」Canvas，
# 再把外壳上的 ttk.Label 换成 CanvasLabel（ttk 控件的底色是实色，没法透出壁纸）。
try:                                    # pragma: no cover - 依赖系统 Tk
    from .shell_canvas import (CanvasLabel, CardCanvas, ShellSurface, SliceCanvas,
                               attach_backdrop)
except Exception:
    CanvasLabel = None                  # type: ignore[assignment]
    CardCanvas = None                   # type: ignore[assignment]
    SliceCanvas = None                  # type: ignore[assignment]
    ShellSurface = None                 # type: ignore[assignment]
    attach_backdrop = None              # type: ignore[assignment]

# 第 7 套主题的卡片：卡内文字全部画在画布上。tk.Entry 的底色只能是平色，压在
# 壁纸亮斑处会露出一个个矩形补丁（实测同一格内与壁纸相差 60~86 个通道），所以
# 这一套主题不用 Entry，改用 photo_card。PhotoCard 与外壳层同生共死：没有 Tk 时
# 一起退化成 None，卡片回落到普通 Frame 版本。
try:                                    # pragma: no cover - 依赖系统 Tk
    from . import photo_card as _photo_card
    from .photo_card import PhotoCard
except Exception:
    _photo_card = None                  # type: ignore[assignment]
    PhotoCard = None                    # type: ignore[assignment]

# 外壳文字的样式表：(取哪个配色键, 是否加粗, 字号来源)。apply_theme 时按这张表
# 重新刷一遍配色——CanvasLabel 不认识 ttk 的 style，颜色必须自己喂。
# 键必须与 apply_theme 里 style.configure 的那批名字一致，否则切主题后字色不跟。
_SHELL_LABEL_STYLES: dict[str, tuple[str, bool, str]] = {
    "App.TLabel": ("tree_fg", False, "base"),
    "Title.TLabel": ("title_fg", True, "title"),
    "Hint.TLabel": ("hint_fg", False, "base"),
    "Ok.TLabel": ("ok_fg", True, "base"),
    "Muted.TLabel": ("muted_fg", False, "base"),
    "Action.TLabel": ("ok_fg", True, "base"),
    "Error.TLabel": ("alert_fg", True, "base"),
    "Note.TLabel": ("note_fg", False, "base"),
    "Summary.TLabel": ("summary_fg", False, "base"),
    "Alert.TLabel": ("alert_fg", True, "base"),
    # KPI 卡里的两行字：数值用等宽字体加粗（字号取 card_pt），说明用正文字号减一。
    # 配色分别对应原来 CardValue.TLabel / CardLabel.TLabel 的前景色。
    "CardValue.TLabel": ("card_val", True, "card"),
    "CardLabel.TLabel": ("card_lbl", False, "small"),
}

_SCROLLBAR_BASE: Any = _tk_for_scrollbar.Canvas if _tk_for_scrollbar else object
_LABEL_BASE: Any = _tk_for_scrollbar.Label if _tk_for_scrollbar else object
# 视图分段按钮的基类：优先用切片画布（能贴壁纸），没有 Tk 的环境退回 Label。
_SEG_BASE: Any = SliceCanvas if SliceCanvas is not None else _LABEL_BASE


# 数据区右侧细滚动条占用的宽度。KPI 指标行必须留出同样的右边距，
# 否则它比下面的表格/卡片宽出这一条，两行的左右边缘对不齐。
SCROLL_GUTTER = 10
# 拖动窗口时 canvas 的 <Configure> 会连着触发几十次。卡片区里每张卡二三十个控件，
# 每次都跟着重算几何会让缩放明显卡顿，所以宽度重排延迟到停手后再做一次。
CARD_RESIZE_DELAY_MS = 250
# 卡片之间的水平间距（只在列与列之间留，最右一列贴边，才能和 KPI 行右缘对齐）
CARD_GAP = 10

# 第 7 套「图片背景」主题的三个菜单文案。入口放在「工具」菜单，
# 绝不能进「主题」菜单——主题菜单必须严格等于 THEME_ORDER 那一串 radiobutton，
# 测试是逐项全量比对的，多一项就整条用例失败。
WALLPAPER_PICK_LABEL = "选择背景图…"
WALLPAPER_CLEAR_LABEL = "清除背景图"
WALLPAPER_ALPHA_LABEL = "背景图透明度(A)"


class SlimScrollbar(_SCROLLBAR_BASE):   # type: ignore[misc,valid-type]
    """极细的圆角滚动条。内容全部可见时自动隐藏，不占视觉。"""

    MIN_THUMB = 28          # 滑块最短像素，太短会点不中
    INSET = 2               # 轨道左右留白，让条看起来更细

    def __init__(self, master: Any, orient: str = "vertical", command: Any = None, *,
                 track: str = "#FFFFFF", thumb: str = "#C6CDD6",
                 thumb_hover: str = "#9AA6B2", idle: str = "#E4E8ED",
                 thickness: int = SCROLL_GUTTER,
                 radius: int = 3, **kwargs: Any) -> None:
        super().__init__(master, highlightthickness=0, bd=0, bg=track,
                         width=thickness if orient == "vertical" else 1,
                         height=thickness if orient == "horizontal" else 1, **kwargs)
        self.orient = orient
        self.command = command
        self.track_color = track
        self.thumb_color = thumb
        self.thumb_hover_color = thumb_hover
        self.idle_color = idle
        self.thickness = thickness
        self.radius = radius
        self.first = 0.0
        self.last = 1.0
        self._hover = False
        # 图片背景主题：滚动条轨道也要贴壁纸切片，否则整窗透图会在最右侧
        # 漏出一条 10px 的实色竖条（截图实测 x=1229 起就是纯 #EEF2FA）。
        self.shell_surface: Any = None
        self._surface_item: Any = None
        self._registered = False

        self._drag_origin: float | None = None
        self._drag_first = 0.0
        self._alive = True
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", lambda _event: self._redraw())
        self.bind("<Destroy>", self._on_destroy)

    # ---- 滚动条协议 ----
    def set(self, first: Any, last: Any) -> None:
        """Treeview / Canvas 通过这个回调告诉我们可见区间。"""
        try:
            self.first = max(0.0, min(1.0, float(first)))
            self.last = max(0.0, min(1.0, float(last)))
        except (TypeError, ValueError):
            self.first, self.last = 0.0, 1.0
        self._redraw()

    def get(self) -> tuple[float, float]:
        return (self.first, self.last)

    def recolor(self, track: str, thumb: str, thumb_hover: str,
                idle: str | None = None) -> None:
        """主题切换时重配颜色（不重建控件）。"""
        self.track_color = track
        self.thumb_color = thumb
        self.thumb_hover_color = thumb_hover
        if idle is not None:
            self.idle_color = idle
        try:
            self.configure(bg=track)
        except Exception:
            pass
        self._redraw()

    def set_surface(self, surface: Any) -> None:
        """挂上/摘掉壁纸切片（图片背景主题用）。不传 None 之外的东西就按切片画。"""
        if self.shell_surface is not None and self._registered:
            try:
                self.shell_surface.unregister(self)
            except Exception:               # pragma: no cover
                pass
            self._registered = False
        self.shell_surface = surface
        if surface is not None:
            try:
                surface.register(self)
                self._registered = True
            except Exception:               # pragma: no cover
                self._registered = False
        self._redraw()

    def _paint_surface(self) -> None:
        """把整窗合成图里属于自己这一块贴到最底层；没有切片时退回纯色轨道。"""
        surface = self.shell_surface
        photo = surface.image() if surface is not None else None
        if photo is None:
            if self._surface_item is not None:
                try:
                    self.delete(self._surface_item)
                except Exception:           # pragma: no cover
                    pass
                self._surface_item = None
            try:
                self.configure(bg=self.track_color)
            except Exception:               # pragma: no cover
                pass
            return
        offset_x, offset_y = surface.offset_of(self)
        try:
            if self._surface_item is None:
                self._surface_item = self.create_image(-offset_x, -offset_y,
                                                       anchor="nw", image=photo)
            else:
                self.coords(self._surface_item, -offset_x, -offset_y)
                self.itemconfigure(self._surface_item, image=photo)
            self.tag_lower(self._surface_item)
        except Exception:                   # pragma: no cover - 控件已销毁
            self._surface_item = None

    # ---- 绘制 ----
    @property
    def visible(self) -> bool:
        return (self.last - self.first) < 0.999

    def _length(self) -> int:
        try:
            value = self.winfo_height() if self.orient == "vertical" else self.winfo_width()
        except Exception:
            return 0
        return int(value or 0)

    def _thumb_span(self, length: int | None = None) -> int:
        total = self._length() if length is None else length
        span = int((self.last - self.first) * total)
        return max(span, self.MIN_THUMB)

    def _thumb_start(self, length: int | None = None) -> int:
        total = self._length() if length is None else length
        span = self._thumb_span(total)
        start = int(round(self.first * total))
        return max(self.INSET, min(start, max(total - span - self.INSET, self.INSET)))

    def _redraw(self) -> None:
        if not self._alive:
            return
        self._paint_surface()
        try:
            self.delete("thumb")
        except Exception:
            return
        length = self._length()
        if length <= self.thickness + 2 * self.INSET:
            return
        if self.visible:
            span = self._thumb_span(length)
            start = self._thumb_start(length)
            end = min(start + span, length - self.INSET)
            start = max(self.INSET, end - span)
            color = self.thumb_hover_color if self._hover else self.thumb_color
        else:
            # 内容全部可见：依然占住这一条（画成很淡的整条），不再整条消失。
            # 之前这里是「直接不画」，右侧就空出一道和滚动条等宽的留白，
            # 看起来像滚动条丢了，也会让人怀疑卡片是不是没对齐。
            start = self.INSET
            end = length - self.INSET
            color = self.thumb_hover_color if self._hover else self.idle_color
        # 用一条带圆头端帽（capstyle=round）的粗线画滑块：端帽半径正好是线宽一半，
        # 上下半圆与中间直条天然同心同宽。之前用 oval+rectangle 三个图元拼，
        # 接缝和半圆处会漏出 1 像素，看起来像圆角「溢出去」了。
        line_width = max(2, min(self.radius * 2, self.thickness - 2 * self.INSET))
        center = self.thickness / 2.0
        cap = line_width / 2.0
        near = start + cap
        far = max(near, end - cap)
        if self.orient == "vertical":
            self.create_line(center, near, center, far, width=line_width,
                             capstyle="round", fill=color, tags="thumb")
        else:
            self.create_line(near, center, far, center, width=line_width,
                             capstyle="round", fill=color, tags="thumb")

    # ---- 交互 ----
    def _on_press(self, event: Any) -> None:
        length = self._length()
        if length <= 0 or self.command is None:
            return
        position = event.y if self.orient == "vertical" else event.x
        span = self._thumb_span(length)
        start = self._thumb_start(length)
        if start <= position <= start + span:
            self._drag_origin = position
            self._drag_first = self.first
            return
        # 点空白处翻一页（翻 90% 的可视高度，和系统滚动条手感一致）
        page = 0.9 * max(self.last - self.first, 0.1)
        target = self.first + (page if position > start + span else -page)
        self.command("moveto", max(0.0, min(1.0 - (self.last - self.first), target)))

    def _on_drag(self, event: Any) -> None:
        if self._drag_origin is None or self.command is None:
            return
        length = self._length()
        if length <= 0:
            return
        position = event.y if self.orient == "vertical" else event.x
        delta = (position - self._drag_origin) / length
        limit = max(0.0, 1.0 - (self.last - self.first))
        self.command("moveto", max(0.0, min(limit, self._drag_first + delta)))

    def _on_release(self, _event: Any) -> None:
        self._drag_origin = None

    def _on_enter(self, _event: Any) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, _event: Any) -> None:
        self._hover = False
        self._drag_origin = None
        self._redraw()

    def _on_destroy(self, _event: Any) -> None:
        self._alive = False


class SegButton(_SEG_BASE):   # type: ignore[misc,valid-type]
    """顶栏「表格 / 卡片 / 双列」分段切换按钮——自绘控件。

    不用 ttk.Radiobutton 的原因：vista 主题下 radiobutton 由系统 element 绘制，
    style.map() 里配的 active 配色会被系统配色盖掉，鼠标悬停就变成系统蓝 + 虚线焦点框，
    和自定义主题冲突（原生默认主题下最明显）。

    也不用普通的 tk.Label：Label 的底色只能是平色，第 7 套「图片背景」主题里
    它会在壁纸上戳出两个实色白块（截图实测未选中按钮是纯 #F2F5FA，壁纸完全透不出来）。
    所以这里改成画布：有切片时先贴自己那块壁纸，再按状态决定要不要盖底色——
    未选中就完全让壁纸透出来，选中才铺主题强调色（那是当前视图的标识，本来就该实）。

    对外接口与原来的 Label 版一致：value / command / selected / apply_palette /
    set_text / set_selected / refresh / invoke / cget("text"/"background"/"foreground")，
    测试与 gui 的调用点都不用改。
    """

    def __init__(self, master: Any, text: str, value: str, command: Any = None,
                 **kwargs: Any) -> None:
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("takefocus", 0)
        kwargs.setdefault("cursor", "hand2")
        super().__init__(master, background="#FFFFFF", **kwargs)
        self.value = value
        self.command = command
        self.selected = False
        self._hover = False
        self._text = str(text)
        self._bg_flat = "#FFFFFF"
        self._fg = "#1A1A1A"
        self._palette: dict[str, str] = {
            "bg": "#FFFFFF", "fg": "#1A1A1A",
            "sel_bg": "#0078D7", "sel_fg": "#FFFFFF",
            "hover_bg": "#E5F1FB", "hover_fg": "#1A1A1A",
            "border": "#B8B8B8",
        }
        # 切片层与配色层各自一个 item：重画时只换填充，不重建控件
        self._bg_item: Any = None
        self._text_item: Any = None
        self._font_spec: Any = ("Microsoft YaHei UI", 9)
        self._pad_x = 11
        self._pad_y = 4
        self._font = None
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", lambda _e: self.refresh(), add="+")
        self._fit()

    # ---- 尺寸：Canvas 默认 10c×7c，不按文字定宽就会撑成一大块 ----
    def _apply_font(self) -> None:
        spec = self._font_spec
        if spec is None:
            self._font = tkfont.nametofont("TkDefaultFont")
        elif isinstance(spec, str):
            self._font = tkfont.nametofont(spec)
        else:
            self._font = tkfont.Font(font=spec)

    def _fit(self) -> None:
        """按文字宽度 + 内边距定宽、按行高定高：pack 时只占文字大小。"""
        self._apply_font()
        try:
            text_w = int(self._font.measure(self._text or ""))
        except Exception:                   # pragma: no cover
            text_w = 0
        try:
            line_h = int(self._font.metrics("linespace"))
        except Exception:                   # pragma: no cover
            line_h = 16
        try:
            super().configure(width=text_w + self._pad_x * 2,
                              height=line_h + self._pad_y * 2)
        except Exception:                   # pragma: no cover
            pass
        # 不在这里 repaint：此时尺寸刚设、_bg_item 还没建，提前画一次只会把
        # 底色矩形留在画布上挡住后面贴的切片。<Configure> 会带着真实尺寸再来一次。

    # ---- 兼容原 Label 版的取值接口 ----
    def cget(self, key: str) -> Any:                # type: ignore[override]
        if key == "text":
            return self._text
        if key == "background" or key == "bg":
            return self._current_background()
        if key == "foreground" or key == "fg":
            return self._current_foreground()
        if key == "font":
            return self._font_spec
        return super().cget(key)

    def configure(self, **kwargs: Any) -> Any:      # type: ignore[override]
        if "text" in kwargs:
            self._text = str(kwargs.pop("text"))
        if "font" in kwargs:
            self._font_spec = kwargs.pop("font")
        if kwargs:
            super().configure(**kwargs)
        self.refresh()
        return None

    config = configure

    def _current_background(self) -> str:
        palette = self._palette
        if self.selected:
            return palette["sel_bg"]
        if self._hover:
            return palette["hover_bg"]
        # 未选中且挂着切片时是「透」的；对外仍报一个颜色串，调用方拿到的语义不变。
        return palette["bg"] if self._surface_photo() is None else palette.get("see_through", palette["bg"])

    def _current_foreground(self) -> str:
        palette = self._palette
        if self.selected:
            return palette["sel_fg"]
        if self._hover:
            return palette["hover_fg"]
        return palette["fg"]

    def _surface_photo(self) -> Any:
        surface = self.shell_surface
        if surface is None:
            return None
        try:
            return surface.image()
        except Exception:       # pragma: no cover
            return None

    # ---- 外观 ----
    def apply_palette(self, theme: dict[str, Any], base_font: tuple[str, int]) -> None:
        """按主题重配配色与字体（不重建控件，选中态不变）。"""
        self._palette = {
            "bg": theme["card_bg"] if theme["kpi"] else theme["win_bg"],
            "fg": theme["tree_fg"],
            "sel_bg": theme["seg_sel_bg"],
            "sel_fg": theme["seg_sel_fg"],
            "hover_bg": theme["seg_hover_bg"],
            "hover_fg": theme["seg_hover_fg"],
            "border": theme["seg_border"],
            # 图片背景主题：未选中的按钮不铺底色，让壁纸直接透出来。
            "see_through": theme["seg_border"],
        }
        family, size = base_font
        self._font_spec = (family, size)
        self._bg_flat = str(self._palette["bg"])
        self.set_background(self._bg_flat)
        self._fit()

    def set_text(self, text: str) -> None:
        """只改文字（切语言时用）：字体、内边距、配色都保持 apply_palette 设好的。"""
        self._text = str(text)
        if self._text_item is not None:
            try:
                self.itemconfigure(self._text_item, text=self._text)
            except Exception:       # pragma: no cover
                pass
        self._fit()

    def set_selected(self, selected: bool) -> None:
        self.selected = bool(selected)
        self.refresh()

    def repaint(self) -> None:
        """重画整块按钮：壁纸切片 → 状态底色 → 文字。

        顺序不能反。上一版把「铺底色」写在 _fit() 里、把「贴切片」交给基类的
        repaint()，结果底色矩形先建、切片后建又被 tag_lower 压到底下，
        未选中的按钮就永远是纯底色（实机截图里那两块白板就是这么来的）。
        现在只有这一个绘制入口，Configure / set_surface / 配色变化全走它。
        """
        try:
            width = int(self.winfo_width())
            height = int(self.winfo_height())
        except Exception:       # pragma: no cover
            return
        if width <= 1 or height <= 1:
            return
        surface = self.shell_surface
        photo = None
        offset_x = offset_y = 0
        if surface is not None:
            try:
                photo = surface.image()
                offset_x, offset_y = surface.offset_of(self)
            except Exception:       # pragma: no cover
                photo = None
        # 选中/悬停时铺实色（当前视图要有明确标识），否则让壁纸透出来
        solid = self.selected or self._hover
        # 1) 切片/纯色底
        if photo is not None and not solid:
            if self._image_item is None:
                self._image_item = self.create_image(
                    -offset_x, -offset_y, anchor="nw", image=photo)
            else:
                self.coords(self._image_item, -offset_x, -offset_y)
                self.itemconfigure(self._image_item, image=photo)
            self.tag_lower(self._image_item)
        elif self._image_item is not None:
            try:
                self.delete(self._image_item)
            except Exception:       # pragma: no cover
                pass
            self._image_item = None
        # 2) 状态底色矩形
        if solid or photo is None:
            if self._bg_item is None:
                self._bg_item = self.create_rectangle(
                    0, 0, width, height, outline="", fill=self._current_background(),
                    tags="seg_bg")
            else:
                self.coords(self._bg_item, 0, 0, width, height)
                self.itemconfigure(self._bg_item, fill=self._current_background())
            self.tag_lower(self._bg_item)
            if self._image_item is not None:
                self.tag_lower(self._image_item, self._bg_item)
        elif self._bg_item is not None:
            try:
                self.delete(self._bg_item)
            except Exception:       # pragma: no cover
                pass
            self._bg_item = None
        # 3) 文字
        if self._text_item is None:
            self._text_item = self.create_text(
                width // 2, height // 2, text=self._text,
                fill=self._current_foreground(), anchor="center", tags="seg_text")
        else:
            self.coords(self._text_item, width // 2, height // 2)
            self.itemconfigure(self._text_item, text=self._text,
                               fill=self._current_foreground())
        self.tag_raise(self._text_item)

    def refresh(self) -> None:
        """兼容既有调用点：等价于重画一次。"""
        self.repaint()

    # ---- 交互 ----
    def invoke(self) -> None:
        """与 ttk.Radiobutton 同名：触发一次选择（测试也走这条路径）。"""
        if self.command is not None:
            self.command()

    def _on_click(self, _event: Any = None) -> str:
        self.invoke()
        return "break"

    def _on_enter(self, _event: Any = None) -> None:
        self._hover = True
        self.refresh()

    def _on_leave(self, _event: Any = None) -> None:
        self._hover = False
        self.refresh()




SORT_MENU_ITEMS: tuple[tuple[str, str], ...] = (
    ("model", "按磁盘型号"),
    ("rw", "按累计读取"),
    ("interval", "按距上次时间"),
    ("cur", "按本次读增"),
    ("hist", "按历史读增"),
    ("media", "按 0E 状态"),
    ("status", "按状态"),
)

# 表格视图固定 8 列：把成对的读/写指标合进同一格（"左  /  右"），
# 型号与序列号也合并到首列，这样默认窗口下一屏放得下，不需要横向拖动。
COLUMN_SPECS: tuple[tuple[str, str, int, str], ...] = (
    ("model", "磁盘 / 序列号 / 容量", 270, "text"),
    # 列宽按英文表头留够余量：Heading 有内边距，"Protocol"（49px 字宽）在 62px
    # 的列里会被截成 "Protoce"，"Total Read / Write"（107px）在 120px 里会被截一节。
    ("protocol", "协议", 78, "text"),
    ("rw", "累计读 / 写", 134, "num"),
    ("interval", "距上次", 152, "text"),
    ("cur", "本次读增 / 写增", 128, "num"),
    ("hist", "历史读增 / 写增", 128, "num"),
    ("media", "0E / 健康", 104, "text"),
    ("status", "状态", 200, "text"),
)
# 表格自然宽度（列宽合计），用于按屏幕挑选默认窗口大小
TABLE_NATURAL_WIDTH = sum(spec[2] for spec in COLUMN_SPECS)



ROW_ALERT_TAG = "alert_row"
ROW_MUTED_TAG = "muted_row"
ROW_ZEBRA_TAG = "zebra_row"
# 原生默认主题下的颜色常量（保留为模块级常量，主题表里共用同一组取值）
ALERT_COLOR = "#C00000"
MUTED_COLOR = "#808080"
BASE_FONT = ("Microsoft YaHei UI", 9)
SMARTCTL_INSTALL_HINT = ("未找到 smartctl.exe，请执行："
                         "winget install smartmontools.smartmontools")

# 「工具」菜单里的系统工具快捷入口：(要打开的 .msc, 名词, 菜单项文案)。
# 这两项点下去只是叫 Windows 自己开一个自带工具，不读它的输出、不进表格也不写历史，
# 所以只是两条普通菜单命令；名词单独留着，成功提示里拼成「已打开 磁盘管理。」。
TOOL_SHORTCUTS: tuple[tuple[str, str, str], ...] = (
    (MSC_DISK_MANAGEMENT, "磁盘管理", "打开磁盘管理(M)"),
    (MSC_DEVICE_MANAGER, "设备管理器", "打开设备管理器(V)"),
)

KPI_SPECS: tuple[tuple[str, str], ...] = (
    ("read_delta", "本次总读增"),
    ("write_delta", "本次总写增"),
    ("hist_read_delta", "历史总读增"),
    ("hist_write_delta", "历史总写增"),
)


def fmt_delta_compact(value: float | None, digits: int = 2) -> str:
    """指标卡用的紧凑增量：超过 1 TB 时用 TB，否则用 GB。"""
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    if abs(value) >= 1024.0:
        return f"{sign}{value / 1024.0:,.{digits}f} TB"
    return f"{sign}{value:,.{digits}f} GB"


def _default_geometry(root: Any) -> str:
    """按屏幕尺寸挑一个默认窗口大小。

    13 列的自然宽度约 1450px，常见 1920 宽屏可以一屏放下；窄屏则回落到
    1280x620 并保留横向滚动条（Treeview 自带），不会因为窗口过宽跑到屏幕外。
    """
    try:
        screen_w = int(root.winfo_screenwidth())
        screen_h = int(root.winfo_screenheight())
    except Exception:
        screen_w, screen_h = 1280, 800
    want_w = TABLE_NATURAL_WIDTH + 84
    width = want_w if screen_w >= want_w + 80 else 1280
    width = min(width, max(screen_w - 80, 980))
    width = max(width, 980)
    height = max(min(640, screen_h - 120), 460)
    return f"{width}x{height}"


# 状态列过长的报错文本按这个宽度截断（完整文本仍保留在 DiskRow 与诊断信息里）
STATUS_TEXT_LIMIT = 24


def _truncate(text: str, limit: int = STATUS_TEXT_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# 表格右键菜单的标签。tk.Menu 的索引可以直接用标签，所以这里把标签抽成常量，
# 供 _sync_tree_menu() 按标签改状态（不用记住 add_command 的下标，改顺序也不会错位）。
# 菜单文字的括号保留，但括号里只写字母、不写 "&" 助记符：
# 中文界面里 Alt 助记符会把那个字母画上一条下划线，看起来像多了一道横线，
# 用户明确要求只去掉 & 号本身（右键菜单与主菜单一致）。
TREE_MENU_COPY_ROWS = "复制选中行(C)"
TREE_MENU_COPY_CELL = "复制此单元格(L)"
TREE_MENU_SELECT_ALL = "全选(A)"
TREE_MENU_COPY_TABLE = "复制整表(T)"

# 合并列在同一格里上下两行显示：上面的值对应表头「/」左边，下面的对应右边。
PAIR_SEPARATOR = "\n"
# 导出 TSV 时换行会破坏表格结构，所以剪贴板里用斜杠分隔
TSV_PAIR_SEPARATOR = " / "


def _pair(left: str, right: str) -> str:
    """把一对读/写（或 0E/健康）值上下叠在同一格：第一行左值，第二行右值。"""
    return f"{left}{PAIR_SEPARATOR}{right}"


# 序列号默认隐藏（隐私）：界面与所有复制出口都遮成「前两位 + 8 个星号」，
# 但 history.json 里依旧写真实序列号——跨次匹配必须靠「型号 + 序列号」，
# 隐藏只是显示层的事，不能影响数据层。
SERIAL_MASK_CHARS = 8          # 星号数量固定，不随原序列号长度变化（长度本身也是信息）
SERIAL_MASK_VISIBLE_PREFIX = 2  # 保留开头两位，方便一眼区分是哪块盘
SERIAL_EMPTY_TEXT = "—"        # 读不到序列号时不伪造遮盖串，仍用破折号占位


def _mask_serial(serial: str) -> str:
    """把序列号遮成「前两位 + 8 个星号」，如 ZK7Y4B2K91XD -> ZK********。

    星号数量固定而非按位 1:1，避免从长度反推原序列号；不足两位时整个遮掉，
    一位都不露（宁可多遮，也不能靠「只剩一位」猜出型号批次）。
    """
    text = (serial or "").strip()
    if not text:
        return SERIAL_EMPTY_TEXT
    prefix = text[:SERIAL_MASK_VISIBLE_PREFIX] if len(text) >= SERIAL_MASK_VISIBLE_PREFIX else ""
    return f"{prefix}{'*' * SERIAL_MASK_CHARS}"


def _short_serial(serial: str, show: bool = True) -> str:
    """序列号只做清洗，不再截断——首列改成上下两行后放得下完整序列号。

    show=False 时返回遮盖串（前两位 + 8 个星号），真实序列号不出现在界面
    与剪贴板上；读不到序列号时仍显示破折号，不伪造一个像序列号的遮盖值。
    """
    if not show:
        return _mask_serial(serial)
    return (serial or "").strip() or SERIAL_EMPTY_TEXT


def _serial_line(row: DiskRow, show_serial: bool = True,
                 show_gib: bool = True) -> str:
    """首列第二行 / 卡片副标题的第一段：序列号 [+ 容量]。

    show_gib 决定容量后面要不要跟括号里的系统口径（"960 GB (894.25 GiB)"）：
    关掉时只留厂商标称值（"960 GB"），这是默认状态——两个数并排对普通用户
    常常只是噪音。

    容量读不到（老记录、读取失败的盘）时只留序列号，不追加「 · —」，
    否则每块读不到容量的盘都会多出一个没有信息量的破折号。
    """
    serial_text = _short_serial(row.serial, show_serial)
    capacity_text = fmt_capacity(row.capacity_bytes, show_system=show_gib)
    if capacity_text == CAPACITY_UNKNOWN_TEXT:
        return serial_text
    return f"{serial_text}  ·  {capacity_text}"


def _row_values(row: DiskRow, show_serial: bool = True,
                show_gib: bool = True) -> tuple[str, ...]:
    """表格 8 列的一行文本。合并列里两个值上下两行，与表头「/」的左右一一对应。

    增量格必须自带单位（fmt_delta_short）：表头写的是「本次读增 / 写增」，
    没有「(GB)」三个字，光一个「+348.80」看不出是容量还是次数。

    show_serial 由调用方传入当前偏好（默认隐藏），表格渲染、TSV 导出、
    单格复制三条出口共用这一份实现，不会出现「界面藏了、复制又漏出去」。
    同理 show_gib 也只在调用方传当前偏好，四条出口口径一致。
    """
    return (
        f"{row.model}\n{_serial_line(row, show_serial, show_gib)}",
        row.protocol,
        _pair(fmt_size_short(row.read_gb), fmt_size_short(row.write_gb)),
        row.interval_text,
        _pair(fmt_delta_short(row.read_delta_gb), fmt_delta_short(row.write_delta_gb)),
        _pair(fmt_delta_short(row.hist_read_delta_gb),
              fmt_delta_short(row.hist_write_delta_gb)),
        _pair(row.media_text, row.health_text),
        _truncate(row.status_text),
    )


def _row_sort_value(row: DiskRow, column: str) -> Any:
    # 8 列视图的排序键。合并列按「左值优先」排序（读增优先于写增），
    # 快捷列名（serial/read_delta/…）保留下来，旧调用点与菜单排序仍然可用。
    mapping = {
        "model": row.model,
        "serial": row.serial,
        "protocol": row.protocol,
        "rw": row.read_gb,
        "read_gb": row.read_gb,
        "capacity": row.capacity_bytes,
        "write_gb": row.write_gb,
        "interval": row.interval_text,
        "cur": row.read_delta_gb,
        "read_delta": row.read_delta_gb,
        "write_delta": row.write_delta_gb,
        "hist": row.hist_read_delta_gb,
        "hist_read_delta": row.hist_read_delta_gb,
        "hist_write_delta": row.hist_write_delta_gb,
        "media": row.media_text,
        "health": row.health_text,
        "status": row.status_text,
    }
    value = mapping.get(column)
    if value is None:
        return float("-inf")
    return value


class DiskTrackerApp:
    """单窗口 GUI。数据读取全部委托给注入的 provider（后台线程执行）。"""

    def __init__(self, root: Any, provider: Any = None, store: HistoryStore | None = None,
                 autostart: bool = True, now_func: Callable[[], datetime] | None = None,
                 settings: SettingsStore | None = None,
                 theme: str | None = None,
                 resolver: Callable[[Sequence[Any]], dict[str, str]] | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.provider = provider if provider is not None else SmartctlProvider()
        self.store = store if store is not None else HistoryStore()
        self.settings = (settings if settings is not None
                         else SettingsStore(self.store.resolve_dir()))
        self.now_func = now_func or (lambda: datetime.now().astimezone())
        # None = 运行时自动枚举本机盘符；测试注入假盘符列表，结果与 runner 无关
        self.resolver = resolver
        self.rows: list[DiskRow] = []
        self._sort_column: str | None = None
        self._sort_reverse = False
        self._busy = False
        # 表格选区：iid -> DiskRow。复制选中行时按 iid 反查数据，顺序取屏幕顺序
        # （get_children 的顺序），不能按点选顺序或 iid 字典序，否则粘出来是乱序的。
        self._iid_to_row: dict[str, DiskRow] = {}
        # 最后一次右键/双击落在的单元格（「复制此单元格」「查看全文」用它定位）
        self._cell_iid = ""
        self._cell_column_key = ""
        self.base_font = BASE_FONT
        self.theme_key = THEME_SYSTEM
        self.theme: dict[str, Any] = dict(THEMES[THEME_LEDGER])

        root.title(window_title())
        root.geometry(_default_geometry(root))
        root.minsize(980, 460)

        saved = self._load_settings()
        self.theme_key = normalize_theme_key(theme if theme is not None else saved.get("theme"))
        self.view_key = normalize_view_key(saved.get("view"))
        # 序列号默认隐藏；菜单「工具 → 显示序列号」可打开，偏好写进 settings.json
        self.show_serial = saved.get("show_serial") is True
        # 容量括号里的系统口径（894.25 GiB）默认也关掉，只显示厂商标称值
        self.show_gib = saved.get("show_gib") is True
        # 界面语言必须先落到 i18n 的模块级状态，再建菜单与控件，
        # 否则首屏文案还是上一次（默认中文）的语言，切完语言才变。
        self.lang = set_language(saved.get("lang"))
        # 第 7 套「图片背景」主题的两项偏好：壁纸路径与内容面板不透明度。
        # 只影响观感，一个字节都不进 history.json；路径失效、格式不支持、
        # GDI+ 不可用都只让主题退化成纯色面板，不弹错、不崩窗口。
        self.wallpaper_path = str(saved.get("wallpaper") or "")
        self.panel_alpha = _wallpaper.normalize_alpha(saved.get("panel_alpha"))
        # 壁纸合成缓存：key = (路径, 量化后的宽高, 不透明度)，photo = (PhotoImage, w, h)。
        # PhotoImage 必须由 app 持引用——交给 Canvas 之后本地变量一回收，
        # 图上就只剩一块背景色（Tk 不会替你保住图片对象）。
        self._wallpaper_cache: dict[str, Any] = {"key": None, "photo": None}
        # 当前表格底座（Treeview / CanvasTable），由 _rebuild_table_surface 维护
        self._surface = SURFACE_TABLE
        # 最近一次运行结果：切语言时用它重算表格文案（不读盘、不写历史）
        self.last_outcome: RunOutcome | None = None
        # 状态栏那几行的**原始值**（不带「数据源：」这类前缀）。
        # 诊断信息要按语言重新拼标签，直接读 Label 上的成品串会把前缀翻两遍。
        self.prev_stamp_text = "—"
        self.cur_stamp_text = "—"
        self.source_text = tr("未检测")
        self._card_rows: list[Any] = []
        self._card_columns = 0
        self._cards_width = -1          # 上一次同步给内部 Frame 的宽度
        self._pending_cards_width = -1  # 最近一次 canvas 报告的宽度（延迟下发）
        self._cards_resize_id: str | None = None  # 待执行的宽度下发（去抖用）
        self._cards_bbox: tuple[int, ...] | None = None
        self._scroll_sync_id: str | None = None   # 待执行的滚动区域同步（去抖用）
        # 第 7 套主题的整窗外壳层：所有外壳 Canvas 共享一张「整窗合成图」，
        # 各自贴自己矩形内的那一块，于是顶栏/提示行/KPI/底栏一起透出同一张壁纸。
        # 只有图片背景主题 + 真设了背景图时才启用，其余主题/没选图时保持原样。
        self._shell: Any = None
        self._shell_backdrops: dict[str, Any] = {}
        self._shell_labels: list[Any] = []
        self._shell_refresh_id: Any = None
        self._wallpaper_size: tuple[int, int] = (0, 0)
        # KPI 卡的画布底座（CardCanvas）；卡片里的两行字也是外壳文字控件的同类。
        self._card_shells: list[Any] = []
        self._card_labels: list[Any] = []
        # 第 7 套主题的自绘卡片（PhotoCard）。与 _card_shells 分开存：那些是常驻
        # 的 KPI 卡底座，这些是每轮 render_cards 重建、随时会被销毁的。
        self._photo_cards: list[Any] = []

        self._build_menubar()
        self._build_widgets()
        self.apply_theme(self.theme_key, persist=False)
        self.set_view(self.view_key, persist=False)
        if autostart:
            self.refresh_async()

    # ---------- 偏好 ----------
    def _load_settings(self) -> dict[str, Any]:
        try:
            return self.settings.load()
        except Exception:  # 偏好坏了不能影响启动
            return dict(DEFAULT_SETTINGS)

    # ---------- 菜单栏 ----------
    def _build_menubar(self) -> None:
        tk = self.tk
        menubar = tk.Menu(self.root, tearoff=False)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label=tr("刷新记录(R)"), accelerator="Ctrl+R",
                              command=self.refresh_async)
        file_menu.add_command(label=tr("打开历史文件(O)"), command=self.open_history_file)
        file_menu.add_command(label=tr("重置历史文件(E)"), command=self.reset_history_file)
        file_menu.add_separator()
        file_menu.add_command(label=tr("退出(X)"), accelerator="Alt+F4", command=self.close)

        view_menu = tk.Menu(menubar, tearoff=False)
        self._theme_var = tk.StringVar(master=self.root, value=self.theme_key)
        for key in THEME_ORDER:
            view_menu.add_radiobutton(label=tr(THEME_LABELS[key]), value=key,
                                      variable=self._theme_var,
                                      command=lambda k=key: self.apply_theme(k))

        tools_menu = tk.Menu(menubar, tearoff=False)
        tools_menu.add_command(label=tr("复制选中行(C)"), accelerator="Ctrl+C",
                               command=self.copy_selection)
        tools_menu.add_command(label=tr("复制表格到剪贴板(T)"), command=self.copy_table)
        tools_menu.add_command(label=tr("打开数据目录(D)"), command=self.open_data_dir)
        tools_menu.add_separator()
        # 系统自带管理工具的快捷入口：只是启动 diskmgmt.msc / devmgmt.msc，
        # 不读它们的输出、不进表格、不写历史，所以没有自己的状态，
        # 成功/失败只在顶部那行操作反馈里说一句。
        for filename, caption, menu_text in TOOL_SHORTCUTS:
            tools_menu.add_command(
                label=tr(menu_text),
                command=lambda f=filename, c=caption: self._open_tool(f, c))
        tools_menu.add_separator()
        # 卡片视图没有表头，排序必须也能从菜单触发
        sort_menu = tk.Menu(tools_menu, tearoff=False)
        for column, title in SORT_MENU_ITEMS:
            sort_menu.add_command(label=tr(title), command=lambda c=column: self.sort_by(c))
        sort_menu.add_separator()
        sort_menu.add_command(label=tr("恢复默认顺序(R)"), command=self.reset_sort)
        tools_menu.add_cascade(label=tr("排序(O)"), menu=sort_menu)
        tools_menu.add_command(label=tr("重置排序(S)"), command=self.reset_sort)
        tools_menu.add_separator()
        # 序列号属于设备身份信息，默认隐藏；勾选后表格首列、卡片副标题、
        # 复制（含 TSV 与单格）一起显示真实序列号。放在「工具」而不是「硬盘」，
        # 因为它只影响显示，不影响读取与记录。
        self._serial_var = tk.BooleanVar(master=self.root, value=self.show_serial)
        tools_menu.add_checkbutton(label=tr("显示序列号"), variable=self._serial_var,
                                   command=self.toggle_serial)
        # 容量后面括号里的系统口径（894.25 GiB）：同一块盘标称 1 TB、系统里
        # 却写 931 GB，两个数并排对普通用户常常只是噪音，所以默认关掉。
        # 同样只影响显示，history.json 里存的一直是字节数与 GB 数值。
        self._gib_var = tk.BooleanVar(master=self.root, value=self.show_gib)
        tools_menu.add_checkbutton(label=tr("显示系统口径容量"), variable=self._gib_var,
                                   command=self.toggle_gib)
        tools_menu.add_separator()
        # 背景图入口只放在「工具」菜单：主题菜单必须严格等于 THEME_ORDER 那一串
        # radiobutton（测试逐项全量比对），混进别的项会让「当前主题」的语义变形。
        tools_menu.add_command(label=tr(WALLPAPER_PICK_LABEL), command=self.choose_wallpaper)
        tools_menu.add_command(label=tr(WALLPAPER_CLEAR_LABEL), command=self.clear_wallpaper)
        # 透明度做成子菜单而不是滑杆：滑杆每次拖动都会重算整屏像素，
        # 五档离散值既省 CPU，也让「回到默认观感」有确定的落点。
        alpha_menu = tk.Menu(tools_menu, tearoff=False)
        self._alpha_var = tk.IntVar(master=self.root, value=self.panel_alpha)
        for level in _wallpaper.ALPHA_LEVELS:
            alpha_menu.add_radiobutton(
                label=tr("不透明度 {value}%", value=level), value=level,
                variable=self._alpha_var,
                command=lambda v=level: self.set_panel_alpha(v))
        tools_menu.add_cascade(label=tr(WALLPAPER_ALPHA_LABEL), menu=alpha_menu)
        self.alpha_menu = alpha_menu
        self.sort_menu = sort_menu

        disk_menu = tk.Menu(menubar, tearoff=False)
        disk_menu.add_command(label=tr("重新扫描并记录(R)"), command=self.refresh_async)
        disk_menu.add_command(label=tr("数据源状态(S)"), command=self.show_source_status)
        disk_menu.add_command(label=tr("复制诊断信息(I)"), command=self.copy_diagnostics)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label=tr("如何安装数据源(S)"), command=self.show_source_help)
        help_menu.add_command(label=tr("容量与换算说明(C)"), command=self.show_capacity_help)
        help_menu.add_separator()
        help_menu.add_command(label=tr("关于(A)"), command=self.show_about)

        # 语言菜单：两个语言项都用母语写法（中文项写「简体中文」、英文项写 English），
        # 名词本身不走 tr()，切完语言菜单项不会自己变成另一种语言。
        lang_menu = tk.Menu(menubar, tearoff=False)
        self._lang_var = tk.StringVar(master=self.root, value=self.lang)
        for code in LANG_ORDER:
            lang_menu.add_radiobutton(label=LANG_LABELS[code], value=code,
                                      variable=self._lang_var,
                                      command=lambda c=code: self.set_language(c))

        menubar.add_cascade(label=tr("文件(F)"), menu=file_menu)
        menubar.add_cascade(label=tr("主题(T)"), menu=view_menu)
        menubar.add_cascade(label=tr("工具(U)"), menu=tools_menu)
        menubar.add_cascade(label=tr("硬盘(D)"), menu=disk_menu)
        menubar.add_cascade(label=tr("帮助(H)"), menu=help_menu)
        menubar.add_cascade(label=tr("语言(L)"), menu=lang_menu)

        self.menubar = menubar
        self.file_menu = file_menu
        self.theme_menu = view_menu
        self.tools_menu = tools_menu
        self.disk_menu = disk_menu
        self.help_menu = help_menu
        self.lang_menu = lang_menu

        self.root.configure(menu=menubar)
        self.root.bind("<Control-r>", lambda _event: self.refresh_async())
        self.root.bind("<Control-q>", lambda _event: self.close())

    # ---------- 外壳控件工厂 ----------
    def _shell_label(self, parent: Any, *, text: str = "",
                     style: str = "App.TLabel") -> Any:
        """建一个外壳文字控件：优先 CanvasLabel（能透壁纸），否则退回 ttk.Label。

        为什么外壳必须自绘：ttk.Label 的底色是实色、没有 alpha 通道，只要外壳
        还是 ttk，壁纸就永远不可能从顶栏、提示行、时间行、底栏这些地方透出来，
        用户看到的「整窗透图」就只剩中间那块数据区。

        CanvasLabel 只在有壁纸时才贴图，没壁纸时画纯色底，观感与 ttk.Label 一致，
        所以这里**无条件**用它（避免切主题时重建控件——重建会丢 pack 顺序、
        丢事件绑定，还会让测试里那些 cget("text") 拿到旧对象）。
        """
        widget: Any
        if CanvasLabel is not None:
            widget = CanvasLabel(parent, text=text, style=style, anchor="w",
                                 foreground=str(self.theme.get("tree_fg") or "#1F2933"),
                                 background=str(self.theme.get("win_bg") or "#FFFFFF"))
            self._shell_labels.append(widget)
        else:                               # pragma: no cover - 依赖系统 Tk
            widget = self.ttk.Label(parent, text=text, style=style, anchor="w")
        return widget

    def _shell_font(self, style: str) -> tuple[str, int, str] | tuple[str, int]:
        """外壳文字的字号：标题用 title_pt 加粗，其余跟正文。"""
        family, size = self.base_font
        entry = _SHELL_LABEL_STYLES.get(style)
        if entry is None:
            return (family, size)
        color_key, bold, source = entry
        if source == "title":
            return (family, int(self.theme.get("title_pt") or size + 3), "bold")
        if source == "card":
            # KPI 数值原来用 CardValue.TLabel：等宽字体 + card_pt 字号 + 加粗
            num_family = str(self.theme.get("num_family") or "Consolas")
            return (num_family, int(self.theme.get("card_pt") or 12), "bold")
        if source == "small":
            # KPI 说明文字原来用 CardLabel.TLabel：正文字号减一、不加粗
            return (family, max(7, size - 1))
        return (family, size, "bold") if bold else (family, size)

    def _card_label(self, parent: Any, *, text: str = "",
                    style: str = "CardValue.TLabel") -> Any:
        """建一张 KPI 卡里的文字控件（同样是能透壁纸的 CanvasLabel）。

        卡片底座换成 CardCanvas 之后，卡里的字如果还是 ttk.Label，就会在透出壁纸的
        卡片上留下两块实色白条——必须一起自绘。
        """
        if CanvasLabel is None:             # pragma: no cover - 依赖系统 Tk
            return self.ttk.Label(parent, text=text, style=style, anchor="w")
        entry = _SHELL_LABEL_STYLES.get(style, ("card_val", False, "base"))
        widget = CanvasLabel(parent, text=text, style=style, anchor="w",
                             foreground=str(self.theme.get(entry[0])
                                            or self.theme.get("tree_fg") or "#1F2933"),
                             background=str(self.theme.get("card_bg") or "#FFFFFF"))
        self._card_labels.append(widget)
        return widget

    def _all_shell_labels(self) -> list[Any]:
        """全部自绘文字控件：13 个窗体外壳 + KPI 卡里那两行字。"""
        return list(self._shell_labels) + list(self._card_labels)

    def _apply_shell_theme(self) -> None:
        """把当前主题的配色刷到外壳 CanvasLabel 上（CanvasLabel 不认识 ttk style）。"""
        win_bg = str(self.theme.get("win_bg") or "#FFFFFF")
        card_bg = str(self.theme.get("card_bg") or win_bg)
        groups = ((self._shell_labels, win_bg), (self._card_labels, card_bg))
        for widgets, background in groups:
            for widget in widgets:
                if CanvasLabel is None or not isinstance(widget, CanvasLabel):
                    continue
                style = str(widget.cget("style") or "App.TLabel")
                entry = _SHELL_LABEL_STYLES.get(style, ("tree_fg", False, "base"))
                color = str(self.theme.get(entry[0])
                            or self.theme.get("tree_fg") or "#1F2933")
                try:
                    widget.apply_palette(foreground=color, font=self._shell_font(style),
                                         background=background, padx=0, pady=0)
                except Exception:           # pragma: no cover - 控件已销毁
                    pass
        # KPI 卡的底座是 CardCanvas：底色 + 可选描边也要跟着主题走
        border = self.theme.get("card_border")
        for card in self._card_shells:
            try:
                card.set_colors(background=card_bg, border=border)
            except Exception:               # pragma: no cover - 控件已销毁
                pass

    # ---------- 控件 ----------
    def _build_widgets(self) -> None:
        ttk = self.ttk
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(10, 8, 10, 8))
        outer.pack(fill="both", expand=True)
        self.outer = outer

        # 顶栏：左侧程序名 + 版本号（文字来自 window_title()，与窗口标题同源），
        # 右侧数据视图分段切换（表格 / 卡片 / 双列），切换不重建窗口。
        head = ttk.Frame(outer, style="App.TFrame")
        head.pack(fill="x")
        self.head = head
        self.title_label = self._shell_label(head, text=window_title(), style="Title.TLabel")
        self.title_label.pack(side="left")

        self._view_var = self.tk.StringVar(master=self.root, value=self.view_key)
        self.seg_frame = ttk.Frame(head, style="App.TFrame")
        self.seg_frame.pack(side="right")
        self.seg_buttons: dict[str, SegButton] = {}
        for index, key in enumerate(VIEW_ORDER):
            button = SegButton(self.seg_frame, text=tr(VIEW_LABELS[key]), value=key,
                               command=lambda k=key: self.set_view(k))
            button.pack(side="left", padx=(0 if index == 0 else 2, 0))
            self.seg_buttons[key] = button

        # 提示栏：左边是「读取成功」的绿色确认（数据真读到并写进 history.json 才出现），
        # 右边是黄底的注意事项（未提权、找不到 smartctl、历史文件损坏……）。
        # 两者分开两个 Label，因为颜色不同，一个 Label 里塞两种色是做不到的。
        self.status_bar = ttk.Frame(outer, style="App.TFrame")
        self.status_bar.pack(fill="x")
        self.ok_parts: list[str] = []
        self.ok_label = self._shell_label(self.status_bar, text="", style="Ok.TLabel")
        self.ok_label.pack(side="left", padx=(0, 8))
        # 失败数单独一个灰 Label：绿色只表达「成功」，失败用灰（与表格灰色行同色），
        # 免得一句全绿的话里夹着「1 块读取失败」，颜色语义自相矛盾。
        self.fail_label = self._shell_label(self.status_bar, text="", style="Muted.TLabel")
        self.fail_label.pack(side="left", padx=(0, 12))
        self.hint_parts: list[str] = []
        self.hint_label = self._shell_label(self.status_bar, text="", style="Hint.TLabel")
        self.hint_label.pack(side="left")
        # 操作失败（复制到剪贴板失败、打开目录失败、请先用右键……）接在黄字后面，
        # 但必须是红字：ttk.Label 一个控件只能有一种前景色，所以另起一个 Label 紧跟其后。
        # 它只保留最新一条，且与下面的绿色反馈共用同一个槽位（见 _set_error / _set_action）。
        self.error_parts: list[str] = []
        self.error_label = self._shell_label(self.status_bar, text="", style="Error.TLabel")
        self.error_label.pack(side="left", fill="x")

        # 绿色操作反馈单独占一行（在提示栏下面、时间行上面），有内容才出现，没有就整行收起。
        # 它取代旧版把「已复制……」塞进黄字提示栏的做法：那些提示带行数/数值，
        # 每一条都不一样，去重拦不住，于是一行行往上堆（用户反馈的「复制提示会堆积」）。
        self.action_parts: list[str] = []
        self.action_label = self._shell_label(outer, text="", style="Action.TLabel")
        self.action_visible = False

        top = ttk.Frame(outer, style="App.TFrame")
        top.pack(fill="x", pady=(2, 6))
        self.top_row = top
        self.prev_label = self._shell_label(top, text=tr("上次记录：—"), style="App.TLabel")
        self.prev_label.pack(side="left", padx=(0, 16))
        self.cur_label = self._shell_label(top, text=tr("本次记录：—"), style="App.TLabel")
        self.cur_label.pack(side="left", padx=(0, 16))
        self.smartctl_label = self._shell_label(
            top, text=self._source_label_text(), style="App.TLabel")
        self.smartctl_label.pack(side="left", padx=(0, 16))
        self.admin_label = self._shell_label(top, text=tr("权限：检测中"), style="App.TLabel")
        self.admin_label.pack(side="left")

        # 指标卡（B/C/D 主题显示，A/E 主题隐藏；控件只建一次，切换主题时 pack/pack_forget）
        # 右边距必须等于数据区右侧细滚动条的宽度：KPI 行与下面的表格/卡片共用
        # 同一条右边界，否则最右一张卡会伸出去，和卡片右缘对不齐（v0.5.1 的观感问题）。
        self.kpi_frame = ttk.Frame(outer, style="App.TFrame", padding=(0, 0, SCROLL_GUTTER, 0))
        self.kpi_values: dict[str, Any] = {}
        self._kpi_labels: dict[str, Any] = {}
        # 间距必须两侧对称（各 CARD_GAP//2），否则第一张卡比后面每张宽 CARD_GAP，
        # 四张卡宽度就会 290/272 交替；叠加 uniform 后每张卡严格等宽，
        # 第 2、3 张卡的分界线正好落在整行宽度的 50%，与双列卡片的中缝重合。
        kpi_half_gap = CARD_GAP // 2
        kpi_count = len(KPI_SPECS)
        for index, (key, caption) in enumerate(KPI_SPECS):
            if index == 0:
                kpi_padx = (0, kpi_half_gap)
            elif index == kpi_count - 1:
                kpi_padx = (kpi_half_gap, 0)
            else:
                kpi_padx = (kpi_half_gap, kpi_half_gap)
            if CardCanvas is not None:
                # 卡片自己也是一块画布：图片背景主题里它去合成图上取自己那一块，
                # 四张卡才不会在壁纸中间戳出四块白板。原来 padding=(12,8) 的内边距
                # 改成挂在两个子标签上——画布没有内边距选项，而它自己的请求尺寸
                # 正是由子控件的 pack 几何决定的（实测与 ttk.Frame 一致）。
                card = CardCanvas(
                    self.kpi_frame,
                    background=str(self.theme.get("card_bg") or "#FFFFFF"),
                    border=self.theme.get("card_border"),
                )
                self._card_shells.append(card)
                value = self._card_label(card, text="—", style="CardValue.TLabel")
                value.pack(fill="x", padx=12, pady=(8, 0))
                label = self._card_label(card, text=tr(caption), style="CardLabel.TLabel")
                label.pack(fill="x", padx=12, pady=(0, 8))
            else:                           # pragma: no cover - 依赖系统 Tk
                card = ttk.Frame(self.kpi_frame, style="Card.TFrame", padding=(12, 8))
                value = ttk.Label(card, text="—", style="CardValue.TLabel", anchor="w")
                value.pack(fill="x")
                label = ttk.Label(card, text=tr(caption), style="CardLabel.TLabel",
                                  anchor="w")
                label.pack(fill="x")
            card.grid(row=0, column=index, sticky="ew", padx=kpi_padx)
            self.kpi_frame.columnconfigure(index, weight=1, uniform="kpi")
            self.kpi_values[key] = value
            self._kpi_labels[key] = label
        self.kpi_visible = False

        # 数据区：三种视图叠在同一个容器里，按需 pack/pack_forget（控件只建一次）。
        # 注意这里先不 pack —— pack 按调用顺序分配空间，expand 的数据区必须先让
        # 底部条与注释行占好位置，最后再把它塞进剩余空间。
        body = ttk.Frame(outer, style="App.TFrame")
        self.body = body

        table_frame = ttk.Frame(body, style="App.TFrame")
        table_frame.pack(fill="both", expand=True)
        self.table_frame = table_frame

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # 表格底座按主题分流：普通 6 套主题用 ttk.Treeview（原生观感、Win 主题控件的
        # 一切细节都别动），图片背景主题用 CanvasTable 自绘（Treeview 的底色不透明，
        # 没法透出壁纸）。控件只在这里建一次，切主题时由 _ensure_table_surface 判断
        # 是否需要换底座。
        self._build_table_surface(table_frame)
        menu = self.tk.Menu(self.root, tearoff=False)
        # 菜单项一律按数字下标定位，不能按标签——「复制选中行」的标签会带上「N 行」，
        # 一旦改名就再也查不到原标签（TclError: bad menu entry index），第二次右键
        # 就弹不出菜单了。下标在建菜单时就记下来，以后增删项也只改这里。
        self._tree_menu_index: dict[str, int] = {}
        next_index = 0
        for entry_label, entry_command in (
            (TREE_MENU_COPY_ROWS, self.copy_selection),
            (TREE_MENU_COPY_CELL, self.copy_cell),
        ):
            # 下标以「中文常量」为键，标签才按当前语言翻译：切语言时靠这些下标回改文案
            menu.add_command(label=tr(entry_label), command=entry_command)
            self._tree_menu_index[entry_label] = next_index
            next_index += 1
        menu.add_separator()
        next_index += 1  # 分隔线也占一个下标，跳过它
        for entry_label, entry_command in (
            (TREE_MENU_SELECT_ALL, self.select_all_rows),
            (TREE_MENU_COPY_TABLE, self.copy_table),
        ):
            menu.add_command(label=tr(entry_label), command=entry_command)
            self._tree_menu_index[entry_label] = next_index
            next_index += 1
        self.tree_menu = menu
        # 还没落点过任何单元格，「复制此单元格」先置灰
        self._sync_tree_menu()

        # 卡片视图（单列 / 双列共用同一个 Canvas 容器）
        cards_frame = ttk.Frame(body, style="App.TFrame")
        self.cards_frame = cards_frame
        self.cards_canvas = self.tk.Canvas(cards_frame, highlightthickness=0, bd=0,
                                           bg=self.theme["win_bg"])
        self.cards_scroll = SlimScrollbar(cards_frame, orient="vertical",
                                          command=self.cards_canvas.yview)
        self.cards_canvas.configure(yscrollcommand=self.cards_scroll.set)
        self.cards_canvas.grid(row=0, column=0, sticky="nsew")
        self.cards_scroll.grid(row=0, column=1, sticky="ns")
        cards_frame.rowconfigure(0, weight=1)
        cards_frame.columnconfigure(0, weight=1)
        self.cards_inner = self.tk.Frame(self.cards_canvas, bg=self.theme["win_bg"])
        self._cards_window = self.cards_canvas.create_window((0, 0), window=self.cards_inner,
                                                             anchor="nw")
        self.cards_inner.bind("<Configure>", self._sync_cards_scrollregion)
        self.cards_canvas.bind("<Configure>", self._resize_cards)
        # 鼠标滚轮在卡片区也能滚（Windows 下 delta 是 120 的倍数）。
        # 只绑 canvas / inner 不够：指针落在卡片里的 Label、Entry 这些子控件上时
        # Windows 的滚轮事件不会冒泡到父控件，看起来就是"只有背景能滚"。
        # 每张卡建好后由 _bind_card_wheel() 递归绑到卡内所有控件。
        self._bind_card_wheel(self.cards_canvas)
        self._bind_card_wheel(self.cards_inner)

        # 底部条与注释行先按 side="bottom" 占位，再让上面的数据区吃剩余空间；
        # 否则 expand=True 的数据区会把底部条压成几像素高，汇总文字被裁掉
        note = self._shell_label(
            outer, text=self._history_note_text(self.store.path, baseline=False),
            style="Note.TLabel")
        note.pack(side="bottom", fill="x", pady=(4, 0))
        self.note_label = note

        bottom = ttk.Frame(outer, style="App.TFrame")
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        self.bottom = bottom
        self.summary_label = self._shell_label(bottom, text=tr("尚无记录"), style="Summary.TLabel")
        self.summary_label.pack(side="left")
        self.alert_label = self._shell_label(bottom, text="", style="Alert.TLabel")
        self.alert_label.pack(side="left", padx=(12, 0))

        # width=0：按文字自身宽度排，不走 ttk 的 width=-11（按字符数算最小宽，
        # 那会把两个字的「关于」和四个字的「刷新记录」拉成一样宽）。
        self.refresh_button = ttk.Button(bottom, text=tr("刷新记录"), style="Primary.TButton",
                                         width=0, command=self.refresh_async)
        self.refresh_button.pack(side="right")
        # 「打开历史文件」只在菜单「文件 → 打开历史文件(O)」里留一个入口，
        # 底部条不再重复放按钮：同一动作两个落点只会让用户想「到底哪个不一样」。
        # 「关于」只显示 APP_VERSION，版本号不在界面任何地方重复写死。
        # 它用 Quiet.TButton：有边框、无填充，和主按钮同高但不同分量——
        # 低频次操作不该和主操作抢同样的视觉重量，也不该看起来像一段纯文字。
        self.about_button = ttk.Button(bottom, text=tr("关于"), style="Quiet.TButton",
                                       width=0, command=self.show_about)
        self.about_button.pack(side="right", padx=(0, 6))

        # 底部条与注释行已占位，数据区吃剩下的高度
        self.body.pack(fill="both", expand=True)

        self._refresh_admin_label()

    # ---- 表格底座：Treeview（原生）/ CanvasTable（图片背景主题）------------------------

    def _build_table_surface(self, container: Any) -> None:
        """在 container 里建当前主题该用的表格底座与两条滚动条。

        两套底座（ttk.Treeview / CanvasTable）对外暴露同一组接口，所以 gui 里所有
        逻辑——渲染、排序、复制、右键菜单、切语言改表头——都只认 self.tree，
        不关心底下到底是哪一个。
        """
        # ttk 是 __init__ 里的局部导入（self.ttk），这个方法单独调用时必须自己取一次，
        # 否则建 Treeview 那一行会 NameError —— 而它正好只在非图片主题下执行，
        # 只在真实 runner 上第一次建窗口时才会炸。
        ttk = self.ttk
        wanted = theme_surface(self.theme_key)
        if wanted == SURFACE_CANVAS and CanvasTable is None:
            # CanvasTable 导入失败（极老的 Tk）就退回 Treeview：宁可底色不透明，
            # 也不能让整个窗口起不来。
            wanted = SURFACE_TABLE
        columns = [spec[0] for spec in COLUMN_SPECS]
        if wanted == SURFACE_CANVAS:
            tree: Any = CanvasTable(container, columns=columns, selectmode="extended",
                                    rowheight=self.theme["rowheight"],
                                    font_family=self.base_font[0], font_size=self.base_font[1])
            # CanvasTable 自己接右键、自己记单元格，命中行后回调这里弹菜单
            tree.context_menu = self._on_tree_context_menu
            # 壁纸由 app 合成并缓存在 app 上：PhotoImage 必须有人持引用，
            # 交给 Canvas 之后本地变量一回收，图上就只剩背景色。
            tree.wallpaper_provider = self._wallpaper_layer
            self._surface = SURFACE_CANVAS
        else:
            # selectmode="extended"：像资源管理器那样 Ctrl/Shift 多选，
            # 再靠 Ctrl+C / 右键菜单把选中行导成 TSV（粘进 Excel 就是表格）。
            tree = ttk.Treeview(container, columns=columns, show="headings",
                                selectmode="extended")
            tree.bind("<Button-3>", self._on_tree_context_menu)
            self._surface = SURFACE_TABLE
        self.tree = tree
        for key, title, width, _kind in COLUMN_SPECS:
            tree.heading(key, text=tr(title), command=lambda c=key: self.sort_by(c))
            tree.column(key, width=width, minwidth=60, anchor="w", stretch=(key == "status"))
        # 细圆角滚动条（自绘，ttk.Scrollbar 做不出圆角），竖向常显、横向按需
        self.yscroll = SlimScrollbar(container, orient="vertical", command=tree.yview)
        self.xscroll = SlimScrollbar(container, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=self.yscroll.set, xscrollcommand=self.xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        self.yscroll.grid(row=0, column=1, sticky="ns")
        self.xscroll.grid(row=1, column=0, sticky="ew")
        # 表格的复制入口：Ctrl+C 复制选中行，右键菜单可复制单元格 / 全选 / 复制整表
        tree.bind("<Control-c>", self.copy_selection_event)

    def _ensure_table_surface(self) -> None:
        """主题换了底座类型才重建表格；类型没变就只改配色（不重建 = 保住选中行）。"""
        wanted = theme_surface(self.theme_key)
        if wanted == SURFACE_CANVAS and CanvasTable is None:
            wanted = SURFACE_TABLE
        if wanted == self._surface:
            return
        rows = list(getattr(self, "rows", []) or [])
        for bar_name in ("yscroll", "xscroll"):
            bar = getattr(self, bar_name, None)
            if bar is not None:
                try:
                    bar.destroy()
                except Exception:
                    pass
                setattr(self, bar_name, None)
        old = getattr(self, "tree", None)
        if old is not None:
            try:
                old.destroy()
            except Exception:
                pass
        self._build_table_surface(self.table_frame)
        self.render_rows(rows)

    # ---- 第 7 套主题的壁纸层 ---------------------------------------------------------

    def _invalidate_wallpaper(self) -> None:
        """丢掉合成缓存（换图、改透明度、改窗口大小都要走这里）。"""
        self._wallpaper_cache = {"key": None, "photo": None}

    # 参与整窗透图的外壳容器（都是 __init__ / _build_widgets 里存下来的属性名）。
    # 每个容器里都 place 一层铺满的切片 Canvas，各自贴同一张合成图的一小块，
    # 于是相邻容器的接缝处是同一张图的连续像素，拼起来就是一整张壁纸。
    #
    # 只收「底色真正露在外面」的容器：body 被自绘表格（自己铺壁纸）或 cards_canvas
    # 完全盖住，table_frame / cards_frame 同理，垫了也看不见；kpi_frame 里的 KPI 卡
    # 是实色 Card.TFrame，垫一层只会让卡缝透出壁纸、显得斑驳，而且测试要数它的子控件。
    # kpi_frame 不在这里：它里面放的是自绘 CardCanvas，垫层要跟卡片一起挂/撤
    # （见 _sync_kpi_backdrop），混进这个元组会被 _sync_shell_surface 当普通容器处理。
    _SHELL_TARGETS = ("outer", "head", "seg_frame", "status_bar", "top_row", "bottom")
    # 除上面这批外壳容器外，还要单独垫切片的两块：
    #   kpi_frame  —— 四张 KPI 卡之间的 10px 缝隙露的是它的底色；
    #   cards_inner—— 双列视图最后一行不满时，右边那一大片露的是它的底色。
    # 它们里面装的是自绘卡片/画布，垫层要跟卡片同生共死，所以不进 _SHELL_TARGETS；
    # 名字集中在这里，测试按这个元组断言，加垫层时不用改测试里的魔数。
    _SHELL_EXTRA_BACKDROPS = ("kpi_frame", "cards_inner")

    def _shell_containers(self) -> list[tuple[str, Any]]:
        """(键, 容器) 列表，顺序无关，只用于去重与排查。

        不垫 root：outer 是 fill=both+expand 的实色 ttk.Frame，整块盖住客户区，
        垫在 root 上的切片一个像素都露不出来，白占一张整窗大小的画布。
        outer 自己那圈 (10,8) 内边距仍是 win_bg（place 只能铺到内边距以内），
        在浅色薄罩下就是一圈同色调的浅边，观感上无碍。
        """
        found: list[tuple[str, Any]] = []
        for name in self._SHELL_TARGETS:
            container = getattr(self, name, None)
            if container is not None:
                found.append((name, container))
        return found

    def _client_rect(self, widget: Any) -> tuple[int, int, int, int] | None:
        """控件相对窗口客户区的矩形 (x, y, w, h)；量不出来返回 None。"""
        if widget is None:
            return None
        try:
            x = int(widget.winfo_rootx()) - int(self.root.winfo_rootx())
            y = int(widget.winfo_rooty()) - int(self.root.winfo_rooty())
            width = int(widget.winfo_width())
            height = int(widget.winfo_height())
        except Exception:                   # pragma: no cover - 控件已销毁
            return None
        if width <= 1 or height <= 1:
            return None
        return (x, y, width, height)

    def _panel_regions(self, width: int, height: int) -> list[tuple[int, int, int, int]]:
        """哪些矩形用「更实」的控件层，其余用较透的对话层。

        控件层 = 数据区、KPI 卡、底栏、提示栏：这些地方压着文字和按钮，底色实一点
        才看得清；窗口里剩下的留白用对话层，壁纸透得更彻底。两层之差只有十几个
        百分点（0.73 / 0.55），观感上是同一块薄罩的深浅，不是两种风格。
        """
        regions: list[tuple[int, int, int, int]] = []
        # status_bar 刻意不在列：它只有一行提示文字，上下都是对话层。
        # 单独给它铺一层更实的控件层，会让这一行在截图上显出一条明显的浅色横带
        # （实测 y=92..106 的 RGB 从 139,143,164 跳到 176,179,195），
        # 用户看到的就是「数据已更新并保存那行带白底」。
        for name in ("table_frame", "kpi_frame", "bottom"):
            rect = self._client_rect(getattr(self, name, None))
            if rect is not None:
                # _client_rect 给的是 (x, y, w, h)，而 assemble 要的是半开区间
                # (x0, y0, x1, y1)。直接把 w/h 当 x1/y1 传进去，蒙层只会铺到
                # 矩形的左上角去——数据区高 479 却只有前 479 行是实层，下面
                # 一大片会突然回落到更透的对话层，观感上就是上下两截。
                x, y, rect_w, rect_h = rect
                regions.append((x, y, x + rect_w, y + rect_h))
        if not regions:
            regions.append((0, 0, width, height))
        return regions

    def _window_layer(self, width: int | None = None,
                      height: int | None = None) -> Any:
        """整窗合成图（壁纸 + 薄罩）。外壳切片与数据区共用这一张。

        返回 PhotoImage；没设背景图、读不到文件、GDI+ 不可用、尺寸还没量出来，
        一律返回 None，所有控件退回各自的纯色底。
        """
        path = self.wallpaper_path
        if not path or not _wallpaper.gdiplus_available():
            return None
        if not width or not height:
            try:
                width = int(self.root.winfo_width())
                height = int(self.root.winfo_height())
            except Exception:               # pragma: no cover
                return None
        width, height = int(width), int(height)
        if width <= 4 or height <= 4:
            return None
        tree_bg = str(self.theme.get("tree_bg") or "#E6E9F0")
        regions = self._panel_regions(width, height)
        key = (path, width, height, int(self.panel_alpha), tree_bg, tuple(regions))
        cached = self._wallpaper_cache
        if cached.get("key") == key and cached.get("photo"):
            layer = cached["photo"]
            self._wallpaper_size = (int(layer[1]), int(layer[2]))
            return layer[0]
        _conv_alpha, panel_alpha = _wallpaper.alpha_pair(self.panel_alpha)
        try:
            ppm = _wallpaper.compose_ppm(
                path, width, height, regions,
                panel_color=_wallpaper.hex_rgb(tree_bg),
                panel_alpha=panel_alpha,
            )
        except Exception:                   # noqa: BLE001 - 壁纸出问题只该退化成纯色
            ppm = None
        if not ppm:
            return None
        try:
            photo = self.tk.PhotoImage(data=ppm.decode("latin-1"), format="ppm")
        except Exception:
            return None
        self._wallpaper_cache = {"key": key, "photo": (photo, width, height)}
        self._wallpaper_size = (width, height)
        return photo

    def _wallpaper_layer(self) -> Any:
        """CanvasTable 的壁纸钩子：返回 (整窗合成图, 宽, 高)。

        表格只该露出自己那一块，于是把整窗图按它在客户区里的偏移往左上推，
        相邻控件贴的是同一张图的连续区域，接缝处像素天然对齐。
        """
        tree = getattr(self, "tree", None)
        if tree is None:
            return None
        photo = self._window_layer()
        if photo is None:
            return None
        rect = self._client_rect(tree)
        try:
            tree.wallpaper_offset = (rect[0], rect[1]) if rect is not None else (0, 0)
        except Exception:                   # pragma: no cover - 控件已销毁
            pass
        width, height = getattr(self, "_wallpaper_size", (0, 0))
        return (photo, int(width), int(height))

    # ---- 外壳切片层 -----------------------------------------------------------

    def _shell_wanted(self) -> bool:
        """当前是否该启用整窗外壳透图。"""
        return (ShellSurface is not None
                and self.theme_key == THEME_PHOTO
                and bool(self.wallpaper_path)
                and _wallpaper.gdiplus_available())

    def _sync_shell_surface(self) -> None:
        """按当前主题/壁纸，挂上或撤下外壳切片层。不重建任何控件。"""
        win_bg = str(self.theme.get("win_bg") or "#FFFFFF")
        if not self._shell_wanted():
            if self._shell is not None:
                try:
                    self._shell.set_provider(None)
                    self._shell.invalidate()
                except Exception:           # pragma: no cover
                    pass
            # 切片要从合成层上摘掉，只换底色是不够的：留在 _clients 里的话，
            # 之后任何一次 invalidate 都还会去 repaint 一个已经不属于当前主题的控件。
            for canvas in self._shell_backdrops.values():
                try:
                    canvas.set_surface(None)
                    canvas.set_background(win_bg)
                except Exception:           # pragma: no cover
                    pass
            # 文字控件也要摘掉切片：留着会继续贴旧图的碎片，切回浅色主题就花了
            for widget in self._all_shell_labels():
                try:
                    if isinstance(widget, CanvasLabel):
                        widget.set_surface(None)
                        widget.set_background(win_bg)
                except Exception:           # pragma: no cover
                    pass
            # 卡片区内层垫层
            cards_backdrop = self._shell_backdrops.get("cards_inner")
            if cards_backdrop is not None:
                try:
                    cards_backdrop.set_surface(None)
                    cards_backdrop.set_background(win_bg)
                except Exception:           # pragma: no cover
                    pass
            # 三个视图分段按钮同样是切片客户端，不摘会残留旧图碎片
            for button in getattr(self, "seg_buttons", {}).values():
                try:
                    button.set_surface(None)
                except Exception:           # pragma: no cover
                    pass
            # 三条细滚动条也要摘：它们的轨道是 Canvas 底，留着会贴旧图
            for bar in (getattr(self, "yscroll", None), getattr(self, "xscroll", None),
                        getattr(self, "cards_scroll", None)):
                if bar is not None:
                    try:
                        bar.set_surface(None)
                    except Exception:       # pragma: no cover
                        pass
            # KPI 行自己的垫层：卡与卡之间的 10px 缝隙露出的是它，
            # 不摘的话切回浅色主题会留一条贴过壁纸的细条。
            kpi_backdrop = self._shell_backdrops.get("kpi_frame")
            if kpi_backdrop is not None:
                try:
                    kpi_backdrop.set_surface(None)
                    kpi_backdrop.set_background(win_bg)
                except Exception:           # pragma: no cover
                    pass
            # KPI 卡的画布底座同理：摘掉后回落到 card_bg 纯色，与其余主题一致
            for card in self._card_shells:
                try:
                    card.set_surface(None)
                except Exception:           # pragma: no cover
                    pass
            # 自绘卡片同样要摘：它们也是合成层的客户端，留着会继续贴旧图碎片
            for card in self._photo_cards:
                try:
                    card.set_surface(None)
                except Exception:           # pragma: no cover - 控件已销毁
                    pass
            return
        if self._shell is None:
            self._shell = ShellSurface(self.root)
        try:
            self._shell.set_provider(self._window_layer)
        except Exception:                   # pragma: no cover
            pass
        for name, container in self._shell_containers():
            canvas = self._shell_backdrops.get(name)
            if canvas is None:
                canvas = attach_backdrop(container, self._shell, background=win_bg)
                if canvas is None:
                    continue
                self._shell_backdrops[name] = canvas
            else:
                # 切走过一次再切回来：控件还在原处，但已经从合成层摘掉了，
                # 必须重新挂上，否则这六块会一直停在纯色底上。
                try:
                    canvas.set_surface(self._shell)
                except Exception:           # pragma: no cover
                    pass
            try:
                canvas.set_background(win_bg)
            except Exception:               # pragma: no cover
                pass
        # 三个视图分段按钮：它们是画布，未选中时不铺底色，直接透出壁纸
        for button in getattr(self, "seg_buttons", {}).values():
            try:
                button.set_surface(self._shell)
                button.set_background(str(self.theme.get("win_bg") or "#FFFFFF"))
            except Exception:               # pragma: no cover
                pass
        # 三条细滚动条：轨道同样是画布底，不贴切片的话整窗最右侧会漏一条
        # 10px 的实色竖条（截图实测 x=1229 起是纯 #EEF2FA）。
        for bar in (getattr(self, "yscroll", None), getattr(self, "xscroll", None),
                    getattr(self, "cards_scroll", None)):
            if bar is not None:
                try:
                    bar.set_surface(self._shell)
                except Exception:           # pragma: no cover
                    pass
        # 卡片区的内层 Frame（cards_inner）也要垫一层：双列视图最后一行不满时，
        # 右边那一大片露的就是它的 win_bg 实色（截图实测 (700,600) 是纯 #EEF2FA、
        # stdev 0），壁纸完全透不出来——用户说的「双列模式大片留白」就是这一块。
        cards_inner = getattr(self, "cards_inner", None)
        if cards_inner is not None:
            cards_canvas = self._shell_backdrops.get("cards_inner")
            if cards_canvas is None:
                cards_canvas = attach_backdrop(cards_inner, self._shell, background=win_bg)
                if cards_canvas is not None:
                    self._shell_backdrops["cards_inner"] = cards_canvas
            else:
                try:
                    cards_canvas.set_surface(self._shell)
                except Exception:               # pragma: no cover
                    pass
            if cards_canvas is not None:
                try:
                    cards_canvas.set_background(win_bg)
                except Exception:               # pragma: no cover
                    pass
        # KPI 行（kpi_frame）自己也要垫一层：四张卡之间留 10px 缝隙，缝隙里露的是
        # kpi_frame 的底色。实测卡0 x=12..317、卡1 x=328..628，中间 x=318..327 那条
        # 10px 在截图里就是一条纯 #EEF2FA 竖条，正是用户说的「卡片中间有白色的块」。
        # 卡片本身（CardCanvas）已经各自贴了切片，只有这层还空着。
        kpi_canvas = self._shell_backdrops.get("kpi_frame")
        if kpi_canvas is None:
            kpi_canvas = attach_backdrop(self.kpi_frame, self._shell, background=win_bg)
            if kpi_canvas is not None:
                self._shell_backdrops["kpi_frame"] = kpi_canvas
        else:
            try:
                kpi_canvas.set_surface(self._shell)
            except Exception:           # pragma: no cover
                pass
        if kpi_canvas is not None:
            try:
                kpi_canvas.set_background(win_bg)
            except Exception:           # pragma: no cover
                pass
        # 13 个外壳文字控件也挂到同一张合成图上：文字控件自己那块也要贴壁纸，
        # 否则它们会在切片层上面留下一小块一小块的实色补丁。
        # KPI 卡里的两行字同理，底色用卡片色而不是窗口色。
        card_bg = str(self.theme.get("card_bg") or win_bg)
        for widget, background in [(w, win_bg) for w in self._shell_labels] +                                   [(w, card_bg) for w in self._card_labels]:
            try:
                if isinstance(widget, CanvasLabel):
                    widget.set_background(background)
                    widget.set_surface(self._shell)
            except Exception:               # pragma: no cover - 控件已销毁
                pass
        # KPI 卡底座：图表也要透，否则四张卡还是四块实色
        for card in self._card_shells:
            try:
                card.set_surface(self._shell)
            except Exception:               # pragma: no cover - 控件已销毁
                pass
        # 自绘卡片：构造时就带上了 self._shell，但那时可能还没有壁纸，
        # 选图/改透明度走到这里要重新挂一遍才会重画。
        for card in self._photo_cards:
            try:
                card.set_surface(self._shell)
            except Exception:               # pragma: no cover - 控件已销毁
                pass
        try:
            self._shell.invalidate()
        except Exception:                   # pragma: no cover
            pass

    def _schedule_shell_refresh(self, delay: int = 120) -> None:
        """布局稳定后再重算一次控件层矩形。

        KPI 卡出现/消失、切换视图都会挪动 body 的上下边界，而合成图是按这些
        矩形切出「控件层 / 对话层」的。apply_theme 里那一刻 pack 还没走完，
        winfo_rooty 拿到的还是旧值，所以延后一帧再量，量完只重合成、不动控件。
        """
        if self._shell is None:
            return
        pending = getattr(self, "_shell_refresh_id", None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except Exception:               # pragma: no cover - 已执行过
                pass
            self._shell_refresh_id = None
        try:
            self._shell_refresh_id = self.root.after(delay, self._run_shell_refresh)
        except Exception:                   # pragma: no cover - 窗口已销毁
            self._shell_refresh_id = None

    def _run_shell_refresh(self) -> None:
        """重算控件层矩形，并让外壳切片与自绘表格各贴一次新图。"""
        self._shell_refresh_id = None
        if self._shell is None:
            return
        self._invalidate_wallpaper()
        try:
            self._shell.invalidate()
        except Exception:                   # pragma: no cover
            pass
        painter = getattr(self.tree, "redraw", None)
        if callable(painter):
            try:
                painter()
            except Exception:               # pragma: no cover - 控件已销毁
                pass

    def _refresh_wallpaper(self) -> None:
        """缓存作废后让自绘表格与外壳切片都重画一次（Treeview 下只重画外壳）。

        这里必须再跑一次 _sync_shell_surface：换图/清除/改不透明度都可能让
        「该不该有外壳切片」翻面（原来是空路径、现在有图了 → 要现挂一层），
        只对已存在的 _shell 调 invalidate 是不够的。
        """
        self._invalidate_wallpaper()
        try:
            self._sync_shell_surface()
        except Exception:                   # pragma: no cover - 合成层绝不该拖垮界面
            pass
        painter = getattr(self.tree, "redraw", None)
        if callable(painter):
            try:
                painter()
            except Exception:
                pass
        if self._shell is not None:
            try:
                self._shell.invalidate()
            except Exception:               # pragma: no cover
                pass

    def choose_wallpaper(self) -> str:
        """选一张图片当背景，选完直接切到图片背景主题。返回新路径（取消则原样返回）。"""
        try:
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                parent=self.root,
                title=tr(WALLPAPER_PICK_LABEL),
                filetypes=[
                    (tr("图片文件"), "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                    ("*", "*.*"),
                ],
            )
        except Exception as exc:
            self._set_error(tr("打开文件对话框失败：{error}", error=exc))
            return self.wallpaper_path
        if not path:
            return self.wallpaper_path
        self.wallpaper_path = str(path)
        self._save_setting("wallpaper", self.wallpaper_path)
        self._refresh_wallpaper()
        if self.theme_key != THEME_PHOTO:
            self.apply_theme(THEME_PHOTO)
        self._set_action(tr("背景图已载入：{name}", name=Path(self.wallpaper_path).name))
        return self.wallpaper_path

    def clear_wallpaper(self) -> str:
        """清除背景图设置：路径记为空，主题本身不变（仍然是一套可选的主题）。"""
        self.wallpaper_path = ""
        self._save_setting("wallpaper", "")
        self._refresh_wallpaper()
        self._set_action(tr("背景图已清除"))
        return self.wallpaper_path

    def set_panel_alpha(self, level: Any) -> int:
        """设定内容面板不透明度（0~100）。只影响观感，不进 history.json。"""
        self.panel_alpha = _wallpaper.normalize_alpha(level)
        self._save_setting("panel_alpha", self.panel_alpha)
        alpha_var = getattr(self, "_alpha_var", None)
        if alpha_var is not None:
            try:
                alpha_var.set(self.panel_alpha)
            except Exception:
                pass
        self._refresh_wallpaper()
        self._set_action(tr("不透明度 {value}%", value=self.panel_alpha))
        return self.panel_alpha

    @staticmethod
    def _history_size_text(path: Any) -> str:
        """历史文件的体积（人读）；文件还不存在或取不到大小就给「—」。"""
        try:
            return fmt_bytes_size(Path(str(path)).stat().st_size)
        except OSError:
            return "—"

    def _history_note_text(self, path: Any, *, baseline: bool) -> str:
        """底部注释行文案：换算说明 / 基线提示 + 历史文件路径 + 文件体积。"""
        size_text = self._history_size_text(path)
        if baseline:
            return tr("已记录基线，下次打开将显示增量。历史文件：{path}（{size}）",
                      path=path, size=size_text)
        return tr("容量换算：{units}；历史文件：{path}（{size}）",
                  units=tr(BYTES_PER_GIB_TEXT), path=path, size=size_text)

    def _source_label_text(self) -> str:
        """「数据源：xxx」整行文案；原始值单独存着，切语言才能重拼（见 diagnostics_text）。"""
        return tr("数据源：{value}", value=self.source_text)

    def _refresh_admin_label(self) -> None:
        admin = is_admin()
        self.admin_label.configure(text=tr("权限：管理员") if admin
                                   else tr("权限：未以管理员运行"))
        if not admin:
            self._append_hint(tr("未以管理员运行，部分磁盘可能读取失败。"))

    def _append_hint(self, text: str) -> None:
        """追加一条**系统状态**提示（未提权 / 缺 smartctl / 历史文件异常……）。

        这类提示描述的是环境，不是某次点击的结果，所以按去重累积：同一句只留一条，
        反复刷新也不会越堆越长。
        """
        text = (text or "").strip()
        if not text:
            return
        if text not in self.hint_parts:
            self.hint_parts.append(text)
        self.hint_label.configure(text="  ".join(self.hint_parts))

    def _set_action(self, text: str) -> None:
        """设置绿色操作反馈（复制成功、全选……）：覆盖式，只留最新一条。

        单独一行、不再是黄字提示栏里的一条，因为这类文案都带行数或数值，一条一个样，
        靠去重根本拦不住（旧版就是这样一行行堆上去的）。
        """
        text = (text or "").strip()
        # 红绿共用同一个槽位：新的成功反馈要把上一次的失败红字顶掉，
        # 否则一个字「请先用右键点一下」和一个「已复制」会同时挂在顶上自相矛盾。
        self.error_parts = []
        self.error_label.configure(text="")
        self.action_parts = [text] if text else []
        self.action_label.configure(text=text)
        if text and not self.action_visible:
            # 插在时间行之前：反馈属于「刚发生了什么」，时间与状态是背景信息，排在它后面
            self.action_label.pack(fill="x", before=self.top_row)
            self.action_visible = True
            self._schedule_shell_refresh()
        elif not text and self.action_visible:
            self.action_label.pack_forget()
            self.action_visible = False
            self._schedule_shell_refresh()

    def _set_error(self, text: str) -> None:
        """设置红色操作失败提示：只留最新一条，且顶掉上一条绿色反馈。

        用红色是因为它和「数据已更新并保存」是两回事——绿色永远只代表做成了。
        """
        text = (text or "").strip()
        self.action_parts = []
        self.action_label.configure(text="")
        if self.action_visible:
            self.action_label.pack_forget()
            self.action_visible = False
        self.error_parts = [text] if text else []
        self.error_label.configure(text=text)

    def _clear_action(self) -> None:
        """清空绿、红两条操作反馈（刷新记录时调用：旧提示说的是旧数据）。"""
        self._set_action("")

    @staticmethod
    def _ok_text(outcome: RunOutcome) -> str:
        """绿色确认行的文案；一块盘都没读成功就返回空串（顶部只留黄/红提示）。"""
        if outcome.recorded <= 0:
            return ""
        verb = tr("已记录基线") if outcome.baseline else tr("数据已更新并保存")
        return tr("✓ {verb}（{count} 块盘）", verb=verb, count=outcome.recorded)

    @staticmethod
    def _fail_text(outcome: RunOutcome) -> str:
        """灰色的失败计数；一块都没失败就返回空串。绿行只讲成功，失败单独挂在灰色 Label 上。"""
        if outcome.recorded <= 0 or outcome.failed <= 0:
            return ""
        return tr("{count} 块读取失败", count=outcome.failed)

    def _set_ok(self, outcome: RunOutcome | None = None) -> None:
        """设置顶部的成功/失败确认行（传 None = 两行都清掉）。

        绿色只表达「成功」，失败数用灰色（与表格灰色失败行同色）。一句话全绿里
        夹个「1 块读取失败」会让颜色语义自相矛盾，所以拆成两个 Label。
        """
        ok = self._ok_text(outcome) if outcome is not None else ""
        fail = self._fail_text(outcome) if outcome is not None else ""
        self.ok_parts = [ok] if ok else []
        self.ok_label.configure(text="  ".join(self.ok_parts))
        self.fail_label.configure(text=fail)

    # ---------- 主题 ----------
    def apply_theme(self, key: Any, persist: bool = True,
                    dark_mode: bool | None = None) -> dict[str, Any]:
        """应用一套主题。控件不重建，只重配样式与指标卡可见性。"""
        theme = resolve_theme(key, dark_mode=dark_mode)
        self.theme_key = normalize_theme_key(key)
        self.theme = theme
        style = self.ttk.Style(self.root)
        names = set(style.theme_names())
        base = theme["base"] if theme["base"] in names else ("clam" if "clam" in names else "default")
        try:
            style.theme_use(base)
        except Exception:
            pass

        family, size = self.base_font
        num_family = theme["num_family"]
        padx, pady = theme["padx"], theme["pady"]

        style.configure(".", font=self.base_font)
        style.configure("App.TFrame", background=theme["win_bg"])
        style.configure("App.TLabel", background=theme["win_bg"], foreground=theme["tree_fg"])
        style.configure("Title.TLabel", background=theme["win_bg"], foreground=theme["title_fg"],
                        font=(family, theme["title_pt"], "bold"))
        style.configure("Hint.TLabel", background=theme["win_bg"], foreground=theme["hint_fg"])
        style.configure("Ok.TLabel", background=theme["win_bg"], foreground=theme["ok_fg"],
                        font=(family, size, "bold"))
        style.configure("Muted.TLabel", background=theme["win_bg"], foreground=theme["muted_fg"])
        # 绿色操作反馈行与红色失败提示：颜色沿用既有语义键，
        # 不新造主题键（否则五套主题都得跟着补一遍）。
        style.configure("Action.TLabel", background=theme["win_bg"], foreground=theme["ok_fg"],
                        font=(family, size, "bold"))
        style.configure("Error.TLabel", background=theme["win_bg"], foreground=theme["alert_fg"],
                        font=(family, size, "bold"))
        style.configure("Note.TLabel", background=theme["win_bg"], foreground=theme["note_fg"])
        style.configure("Summary.TLabel", background=theme["win_bg"],
                        foreground=theme["summary_fg"])
        style.configure("Alert.TLabel", background=theme["win_bg"],
                        foreground=theme["alert_fg"], font=(family, size, "bold"))

        # 底部条两个按钮：一个是主操作（刷新记录），一个是低频次操作（关于）。
        # 之前两个都是 clam 的默认 TButton——同一块灰底、同一套系统配色，切到深色主题
        # 也还是浅灰，还会因为 clam 的 width=-11（按字符数算最小宽）被拉成一样宽。
        # 这里只取主题表里已有的键：强调色用 seg_*（本来就是给「选中/主操作」用的），
        # 底色文字用 win_bg / tree_fg / muted_fg，不新造主题键（否则五套主题都得补）。
        accent_bg = theme["seg_sel_bg"]
        # vista / winnative / xpnative 这几个原生主题由系统画 TButton，background 之类
        # 的配置会被忽略（实测 Primary.TButton 在 vista 下只剩一个空框，文字都不画）。
        # 原生主题的意义就是「跟系统一致」，所以在这几套主题下直接退回默认 TButton。
        native_base = base in ("vista", "winnative", "xpnative")
        if hasattr(self, "refresh_button"):
            self.refresh_button.configure(style="TButton" if native_base else "Primary.TButton")
        if hasattr(self, "about_button"):
            self.about_button.configure(style="TButton" if native_base else "Quiet.TButton")
        style.configure("Primary.TButton", background=accent_bg,
                        foreground=theme["seg_sel_fg"], borderwidth=0, relief="flat",
                        padding=(14, 5), font=(family, size, "bold"),
                        bordercolor=accent_bg, lightcolor=accent_bg,
                        darkcolor=accent_bg, focuscolor=accent_bg)
        style.map("Primary.TButton",
                  background=[("disabled", theme["seg_border"]),
                              ("pressed", theme["seg_hover_bg"]),
                              ("active", theme["seg_hover_bg"])],
                  foreground=[("disabled", theme["muted_fg"]),
                              ("pressed", theme["seg_hover_fg"]),
                              ("active", theme["seg_hover_fg"])])
        # 次级按钮：描边 + 透明填充。clam 的 TButton 画的是 border 元素，
        # borderwidth/bordercolor/lightcolor/darkcolor 四个一起设才有一条颜色可控的细边
        # （只设 bordercolor 时上下会留 lightcolor/darkcolor 的立体高光）。
        style.configure("Quiet.TButton", background=theme["win_bg"],
                        foreground=theme["tree_fg"], borderwidth=1, relief="solid",
                        padding=(10, 4), font=(family, size),
                        bordercolor=theme["seg_border"], lightcolor=theme["seg_border"],
                        darkcolor=theme["seg_border"], focuscolor=theme["seg_border"])
        style.map("Quiet.TButton",
                  background=[("disabled", theme["win_bg"]),
                              ("pressed", theme["seg_hover_bg"]),
                              ("active", theme["seg_hover_bg"])],
                  foreground=[("disabled", theme["muted_fg"]),
                              ("pressed", theme["seg_hover_fg"]),
                              ("active", theme["seg_hover_fg"])])

        border = 1 if theme["card_border"] else 0
        style.configure("Card.TFrame", background=theme["card_bg"], borderwidth=border,
                        relief="solid" if border else "flat",
                        bordercolor=theme["card_border"] or theme["card_bg"])
        style.configure("CardValue.TLabel", background=theme["card_bg"],
                        foreground=theme["card_val"],
                        font=(num_family, theme["card_pt"], "bold"))
        style.configure("CardLabel.TLabel", background=theme["card_bg"],
                        foreground=theme["card_lbl"], font=(family, size - 1))

        head_font = (family, size, "bold") if theme["head_bold"] else (family, size)
        if theme["head_bg"]:
            style.configure("Treeview.Heading", background=theme["head_bg"],
                            foreground=theme["head_fg"], font=head_font,
                            relief="flat", padding=(6, 4))
        else:
            style.configure("Treeview.Heading", font=head_font)
        style.configure("Treeview", background=theme["tree_bg"],
                        fieldbackground=theme["tree_bg"], foreground=theme["tree_fg"],
                        rowheight=theme["rowheight"], borderwidth=0)
        style.map("Treeview",
                  background=[("selected", theme["select_bg"])],
                  foreground=[("selected", theme["select_fg"])])

        # 顶栏视图分段切换按钮：自绘 tk.Label，配色全部来自主题表
        # （ttk.Radiobutton 在 vista 主题下由系统 element 绘制，悬停会被系统蓝盖掉）
        for button in getattr(self, "seg_buttons", {}).values():
            button.apply_palette(theme, self.base_font)
            button.set_selected(button.value == self.view_key)

        self.outer.configure(padding=(padx, pady, padx, pady))
        try:
            self.root.configure(bg=theme["win_bg"])
        except Exception:
            pass

        # 底座类型可能随主题变化：Treeview ↔ CanvasTable 只在跨越这里时重建一次。
        # 必须排在「滚动条换色」之前——重建会连两条滚动条一起换掉，先换色的话
        # 新建出来的那条会顶着默认白底，跟新主题对不上。
        self._ensure_table_surface()

        # 细滚动条跟着主题换色（不重建控件）
        for bar in (getattr(self, "yscroll", None), getattr(self, "xscroll", None),
                    getattr(self, "cards_scroll", None)):
            if bar is not None:
                bar.recolor(theme["win_bg"], theme["scroll_thumb"],
                            theme["scroll_thumb_hover"], theme["scroll_idle"])
        if hasattr(self, "cards_canvas"):
            self.cards_canvas.configure(bg=theme["win_bg"])
            self.cards_inner.configure(bg=theme["win_bg"])
        # 图片背景主题走自绘表格时，底色交给壁纸层合成的那张图，palette 的 bg 置空
        # 才不会每行盖一块实色矩形把壁纸糊掉；没选图时仍给实色，免得透出白底。
        on_canvas = self._surface == SURFACE_CANVAS
        canvas_bg: str | None = theme["tree_bg"]
        if on_canvas:
            canvas_bg = None if self.wallpaper_path else theme["tree_bg"]
            try:
                self.tree.set_palette({
                    "bg": canvas_bg,
                    "fg": theme["tree_fg"],
                    "line": theme["head_bg"] or theme["tree_bg"],
                    "head_bg": theme["head_bg"],
                    "head_fg": theme["head_fg"],
                    "sel_bg": theme["select_bg"],
                    "sel_fg": theme["select_fg"],
                    "zebra": theme["zebra"],
                })
                self.tree.set_row_height(theme["rowheight"])
                self.tree.set_fonts(family, size)
                self.tree.set_sort_indicator(self._sort_column, self._sort_reverse)
            except Exception:
                pass

        zebra_bg = theme["zebra"] or theme["tree_bg"]
        self.tree.tag_configure(ROW_ALERT_TAG,
                                foreground=theme["alert_fg"],
                                background=theme["alert_bg"] or theme["tree_bg"])
        self.tree.tag_configure(ROW_MUTED_TAG,
                                foreground=theme["muted_fg"],
                                background=theme["tree_bg"])
        if on_canvas and canvas_bg is None:
            # 自绘表格 + 有壁纸：斑马纹不画实色块，否则等于给每行铺一张不透明纸条
            self.tree.tag_configure(ROW_ZEBRA_TAG, background=None)
        else:
            self.tree.tag_configure(ROW_ZEBRA_TAG, background=zebra_bg)

        # 图变了、透明度变了，之前的合成缓存一律作废，下次重画时重新贴
        self._invalidate_wallpaper()

        # 外壳：先把配色刷到 CanvasLabel（它们不认 ttk style），再决定挂不挂切片层。
        # 顺序不能反——_sync_shell_surface 会拿 theme 里的 win_bg 当兜底色。
        self._apply_shell_theme()
        self._sync_shell_surface()

        self._set_kpi_visible(theme["kpi"] is not None)
        # KPI 卡的显隐会挪动 body 的位置，控件层矩形要等布局稳定后重算一次
        self._schedule_shell_refresh()
        if hasattr(self, "_theme_var"):
            self._theme_var.set(self.theme_key)
        # 卡片视图的颜色是直接画在 tk 控件上的，主题切换后必须重画
        if getattr(self, "view_key", VIEW_TABLE) != VIEW_TABLE and hasattr(self, "cards_inner"):
            self.render_cards()
        if persist:
            self._save_theme(self.theme_key)
        return theme

    def _set_kpi_visible(self, visible: bool) -> None:
        if visible and not self.kpi_visible:
            self.kpi_frame.pack(fill="x", pady=(0, 8), before=self.body)
            self.kpi_visible = True
        elif not visible and self.kpi_visible:
            self.kpi_frame.pack_forget()
            self.kpi_visible = False

    def _save_theme(self, key: str) -> None:
        self._save_setting("theme", key)

    # ---------- 序列号显示 ----------
    def toggle_serial(self, show: Any = None) -> bool:
        """切换序列号显示；不传参时读菜单勾选状态。返回切换后的状态。"""
        if show is None:
            show = bool(self._serial_var.get())
        self.show_serial = bool(show)
        if hasattr(self, "_serial_var"):
            self._serial_var.set(self.show_serial)
        self._save_setting("show_serial", self.show_serial)
        # 三条出口都从 _row_values / _build_card 走，重画一遍就一致了
        self.render_rows(self.rows)
        return self.show_serial

    # ---------- 容量系统口径显示 ----------
    def toggle_gib(self, show: Any = None) -> bool:
        """切换容量括号里的系统口径（894.25 GiB）；不传参时读菜单勾选状态。"""
        if show is None:
            show = bool(self._gib_var.get())
        self.show_gib = bool(show)
        if hasattr(self, "_gib_var"):
            self._gib_var.set(self.show_gib)
        self._save_setting("show_gib", self.show_gib)
        # 与 toggle_serial 同一条路：表格、卡片、TSV、单格复制都从这里重画
        self.render_rows(self.rows)
        return self.show_gib

    # ---------- 界面语言 ----------
    def set_language(self, code: Any, persist: bool = True) -> str:
        """切换界面语言并**立即重画**当前窗口（不重启、不重读磁盘）。

        文案分散在四个地方，逐个处理：
        1) 菜单栏：整条重建（Tk 的菜单项没有批量改标签的接口，重建最省事）；
        2) 固定控件（表头、视图切换、按钮、各状态栏标签）：直接 configure；
        3) 数据层拼好的行文案（状态列、0E、时间间隔）：用上次的运行结果重算；
        4) 底部注释与汇总：跟着第 3 步一起重跑。
        """
        self.lang = set_language(code)
        if hasattr(self, "_lang_var"):
            self._lang_var.set(self.lang)
        if persist:
            self._save_setting("lang", self.lang)

        # 1) 菜单：先销毁旧的再重建，避免两套菜单叠在一起
        old_menubar = getattr(self, "menubar", None)
        if old_menubar is not None:
            try:
                self.root.configure(menu="")
                old_menubar.destroy()
            except Exception:            # noqa: BLE001 - 菜单已销毁/未建好都不影响后续
                pass
        self._build_menubar()

        # 2) 固定文案控件
        for key, button in getattr(self, "seg_buttons", {}).items():
            button.set_text(tr(VIEW_LABELS[key]))
        for key, title, _width, _kind in COLUMN_SPECS:
            self.tree.heading(key, text=tr(title))
        for key, holder in getattr(self, "_kpi_labels", {}).items():
            holder.configure(text=tr(dict(KPI_SPECS)[key]))
        self._sync_refresh_button()
        self.about_button.configure(text=tr("关于"))
        self.title_label.configure(text=window_title())
        self._refresh_admin_label()
        self.smartctl_label.configure(text=self._source_label_text())
        self.prev_label.configure(text=tr("上次记录：{stamp}", stamp=self.prev_stamp_text))
        self.cur_label.configure(text=tr("本次记录：{stamp}", stamp=self.cur_stamp_text))
        self._refresh_tree_menu_labels()

        # 3) 数据层文案 + 4) 底部汇总：有上次结果就原地重算，没有就只换静态文案
        if self.last_outcome is not None:
            rebuilt = RunOutcome(
                rows=rebuild_rows(self.last_outcome),
                totals=dict(self.last_outcome.totals),
                media_alert=self.last_outcome.media_alert,
                previous_stamp=self.last_outcome.previous_stamp,
                current_stamp=self.last_outcome.current_stamp,
                provider_status=self.last_outcome.provider_status,
                history_message=self.last_outcome.history_message,
                history_path=self.last_outcome.history_path,
                smartctl_path=self.last_outcome.smartctl_path,
                baseline=self.last_outcome.baseline,
                recorded=self.last_outcome.recorded,
                failed=self.last_outcome.failed,
                records=list(self.last_outcome.records),
                snapshots=list(self.last_outcome.snapshots),
            )
            fresh = self.apply_outcome(rebuilt)
            self.last_outcome = fresh
        else:
            self.summary_label.configure(text=tr("尚无记录"))
            self.note_label.configure(
                text=self._history_note_text(self.store.path, baseline=False)
            )
            self._set_ok(None)
        return self.lang

    def _refresh_tree_menu_labels(self) -> None:
        """右键菜单的四个标签跟着语言走（下标定位，不能用标签查，见 _sync_tree_menu）。"""
        menu = getattr(self, "tree_menu", None)
        indices = getattr(self, "_tree_menu_index", None)
        if menu is None or not indices:
            return
        menu.entryconfigure(indices[TREE_MENU_COPY_ROWS], label=self._copy_rows_label())
        menu.entryconfigure(indices[TREE_MENU_COPY_CELL], label=tr(TREE_MENU_COPY_CELL))
        menu.entryconfigure(indices[TREE_MENU_SELECT_ALL], label=tr(TREE_MENU_SELECT_ALL))
        menu.entryconfigure(indices[TREE_MENU_COPY_TABLE], label=tr(TREE_MENU_COPY_TABLE))

    # ---------- 表格 ----------
    def clear_rows(self) -> None:
        self._iid_to_row.clear()
        self._cell_iid = ""
        self._cell_column_key = ""
        for item in self.tree.get_children():
            self.tree.delete(item)

    def render_rows(self, rows: Sequence[DiskRow]) -> None:
        self.rows = list(rows)
        self.clear_rows()
        ordered = self._sorted_rows(self.rows)
        striped = bool(self.theme.get("zebra"))
        for index, row in enumerate(ordered):
            tags: tuple[str, ...] = ()
            if row.failed or row.vanished:
                tags = (ROW_MUTED_TAG,)
            elif row.alert:
                tags = (ROW_ALERT_TAG,)
            elif striped and index % 2:
                tags = (ROW_ZEBRA_TAG,)
            iid = row.key or row.device_path or row.model
            base = iid
            suffix = 1
            while self.tree.exists(iid):
                suffix += 1
                iid = f"{base}#{suffix}"
            self.tree.insert("", "end", iid=iid,
                             values=_row_values(row, self.show_serial, self.show_gib),
                             tags=tags)
            self._iid_to_row[iid] = row
        if self.view_key != VIEW_TABLE:
            self.render_cards()

    def _sorted_rows(self, rows: Sequence[DiskRow]) -> list[DiskRow]:
        if not self._sort_column:
            return list(rows)
        column = self._sort_column
        return sorted(rows, key=lambda r: _row_sort_value(r, column), reverse=self._sort_reverse)

    def sort_by(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._sync_sort_indicator()
        self.render_rows(self.rows)

    def reset_sort(self) -> None:
        self._sort_column = None
        self._sort_reverse = False
        self._sync_sort_indicator()
        self.render_rows(self.rows)

    def _sync_sort_indicator(self) -> None:
        """把排序方向告诉自绘表头（Treeview 的箭头由 style 自己管，这里是空操作）。"""
        setter = getattr(self.tree, "set_sort_indicator", None)
        if not callable(setter):
            return
        try:
            setter(self._sort_column or "", self._sort_reverse)
        except Exception:
            pass

    def row_tags(self) -> list[tuple[str, ...]]:
        return [tuple(self.tree.item(iid, "tags")) for iid in self.tree.get_children()]

    def card_rows(self) -> list[DiskRow]:
        """卡片视图当前渲染的盘（顺序与表格一致，用于排序/断言的统一入口）。"""
        return list(self._card_rows)

    # ---------- 视图切换：表格 / 卡片 / 双列 ----------
    def set_view(self, key: Any, persist: bool = True) -> str:
        """切换数据展示视图。只 pack/pack_forget，不重建控件，选中行与滚动位置不丢。"""
        key = normalize_view_key(key)
        self.view_key = key
        if hasattr(self, "_view_var"):
            self._view_var.set(key)
        for button_key, button in getattr(self, "seg_buttons", {}).items():
            button.set_selected(button_key == key)
        if key == VIEW_TABLE:
            self.cards_frame.pack_forget()
            self.table_frame.pack(fill="both", expand=True)
        else:
            self.table_frame.pack_forget()
            self.cards_frame.pack(fill="both", expand=True)
            self.render_cards()
            # 切视图是一次性动作，不必等去抖，立刻把布局算准
            self.flush_card_layout()
        if persist:
            self._save_setting("view", key)
        # 表格区与卡片区的矩形变了，外壳切片要重新切
        self._schedule_shell_refresh()
        return key

    def _save_setting(self, name: str, value: Any) -> None:
        try:
            self.settings.save({name: value})
        except OSError as exc:      # 只读目录等，不影响使用
            self._set_error(tr("偏好保存失败：{error}", error=exc))

    def _bind_card_wheel(self, widget: Any) -> None:
        """把滚轮事件绑到卡片里的每个控件上（含后续动态创建的子控件）。"""
        try:
            widget.bind("<MouseWheel>", self._on_cards_wheel)
            children = widget.winfo_children()
        except Exception:
            return
        for child in children:
            self._bind_card_wheel(child)

    def _sync_cards_scrollregion(self, _event: Any = None) -> None:
        """内容尺寸变化后重算滚动区域。

        拖动窗口时 inner 的 <Configure> 会连着触发几十次，每次都 configure
        scrollregion 会让 canvas 反复重排，形成「重排→再次 Configure」的反馈环，
        缩放明显卡顿。这里合并成一次 after_idle 处理，并且只有 bbox 真变了才下发。
        """
        if self._scroll_sync_id is not None:
            return
        try:
            self._scroll_sync_id = self.root.after_idle(self._apply_cards_scrollregion)
        except Exception:
            self._scroll_sync_id = None

    def _cards_viewport(self) -> tuple[int, int, int, int]:
        """(宽, 高, 内容宽, 内容高)：内容尺寸取内部 Frame 的请求尺寸。"""
        try:
            width = max(int(self.cards_canvas.winfo_width()), 1)
            height = max(int(self.cards_canvas.winfo_height()), 1)
            content_w = max(int(self.cards_inner.winfo_reqwidth()), 1)
            content_h = max(int(self.cards_inner.winfo_reqheight()), 1)
        except Exception:
            return (1, 1, 1, 1)
        return (width, height, content_w, content_h)

    def _cards_fit(self) -> bool:
        """内容是否完全放得下（放得下就不该能滚动，也不该出现空白带）。"""
        _, height, _, content_h = self._cards_viewport()
        return content_h <= height

    def _apply_cards_scrollregion(self) -> None:
        """重算滚动区域，并保证「放得下 ⇒ 贴顶且不能滚」这条不变量。

        两个曾经踩过的坑：
        1) 缓存只比 bbox，窗口变高时 bbox 不变会直接早退，滚动区域停在旧值；
           所以缓存的键改成 (内容尺寸, 视口尺寸)。
        2) 内容比视口矮时，滚动区域若比视口高，滚轮就能把卡片推下去，
           露出上方一大片空白（用户截图里的"可以滑下去"）。
           这里把区域高度补到视口高度并强制回到顶部，从根上消掉。
        """
        self._scroll_sync_id = None
        width, height, content_w, content_h = self._cards_viewport()
        key = (content_w, content_h, width, height)
        if key == self._cards_bbox:
            return
        self._cards_bbox = key
        region_w = max(content_w, width)
        region_h = max(content_h, height)
        try:
            # 内部 Frame 永远贴 (0,0)：任何历史遗留的纵向偏移都在这里被抹平。
            self.cards_canvas.coords(self._cards_window, 0, 0)
            self.cards_canvas.configure(scrollregion=(0, 0, region_w, region_h))
            if content_h <= height:
                self.cards_canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _resize_cards(self, event: Any) -> None:
        """canvas 宽度变化时同步内部 Frame 宽度（延迟合并，拖动时不再卡顿）。

        两个坑叠在一起：
        1) 宽度没变就重设一次会连锁触发 inner 的 <Configure>，形成反馈环；
        2) 卡片里控件多，每次设宽度都要整块重算几何（实测 10 张卡约 250ms/步）。
        所以先记下最新宽度、把「真正下发」延后 CARD_RESIZE_DELAY_MS：
        连续拖动时定时器一直被重置，拖动过程只做 canvas 自适应（很轻），
        松手后一次性重排，手感和流畅度都正常。
        """
        width = int(getattr(event, "width", 0) or 0)
        if width <= 0 or width == self._pending_cards_width:
            return
        self._pending_cards_width = width
        if self._cards_resize_id is not None:
            try:
                self.root.after_cancel(self._cards_resize_id)
            except Exception:
                pass
        try:
            self._cards_resize_id = self.root.after(
                CARD_RESIZE_DELAY_MS, self._apply_cards_width)
        except Exception:
            self._cards_resize_id = None

    def flush_card_layout(self) -> None:
        """立刻执行挂起的宽度重排与滚动区域同步（切视图、测试断言前调用）。"""
        if self._cards_resize_id is not None:
            try:
                self.root.after_cancel(self._cards_resize_id)
            except Exception:
                pass
            self._cards_resize_id = None
        self._apply_cards_width()
        if self._scroll_sync_id is not None:
            try:
                self.root.after_cancel(self._scroll_sync_id)
            except Exception:
                pass
            self._scroll_sync_id = None
        self._apply_cards_scrollregion()

    def _apply_cards_width(self) -> None:
        """把延迟期间攒下的最新宽度真正下发给 canvas 内部 Frame。"""
        self._cards_resize_id = None
        width = self._pending_cards_width
        if width <= 0 or width == getattr(self, "_cards_width", -1):
            return
        self._cards_width = width
        try:
            self.cards_canvas.itemconfigure(self._cards_window, width=width)
        except Exception:
            pass
        # 宽度变了必然要重算滚动区域：只靠 inner 的 <Configure> 不够，
        # 窗口放大时 inner 尺寸常不变（宽度被 itemconfigure 拉满但内容不换行），
        # 滚动区域就会停在旧值上，缩小窗口后便凭空多出一段能滚动的空白。
        self._apply_cards_scrollregion()

    def _on_cards_wheel(self, event: Any) -> str:
        # 内容放得下时滚轮什么都不做：否则滚动区域一旦被撑大，
        # 滚轮就能把整块卡片推下去，露出上方一大片空白。
        if self._cards_fit():
            return "break"
        step = -1 * int(getattr(event, "delta", 0) // 120 or 0)
        if step:
            try:
                self.cards_canvas.yview_scroll(step, "units")
            except Exception:
                return "break"
        return "break"

    def render_cards(self) -> None:
        """按当前视图重建卡片（单列=卡片视图，双列=两列网格）。"""
        columns = 1 if self.view_key == VIEW_CARDS else 2
        theme = self.theme
        # 垫在最底层的切片画布不是卡片，必须留着：它是用 place 铺满 cards_inner 的
        # 普通子控件，一起 destroy 掉的话，双列视图最后一行右侧那片留白就会
        # 回落到 cards_inner 的实色底（用户截图里的「双列模式大片留白」）。
        cards_backdrop = self._shell_backdrops.get("cards_inner")
        for child in self.cards_inner.winfo_children():
            if child is cards_backdrop:
                continue
            child.destroy()
        self._photo_cards = []
        # 先把上一种视图留下的列配置清干净：双列给每列配了 weight/uniform，
        # 切回单列时若不清，卡片只会占一半宽度（看起来仍像双列）。
        widest = max(columns, getattr(self, "_card_columns", 0))
        for column in range(widest):
            self.cards_inner.columnconfigure(column, weight=0, uniform="")
        self._card_columns = columns
        self._card_rows = self._sorted_rows(self.rows)
        # 列间距要两边各让一半：如果只在左列卡片右侧留 CARD_GAP，
        # 左列卡片就比右列窄 CARD_GAP 像素（实测 567 vs 577），双列看着就是歪的。
        half_gap = CARD_GAP // 2
        for index, row in enumerate(self._card_rows):
            if self._use_photo_cards():
                card = self._build_photo_card(row, theme)
            else:
                card = self._build_card(row, theme)
            column = index % columns
            if columns == 1:
                padx = (0, 0)                       # 单列：卡片贴满整行，右缘与 KPI 行对齐
            elif column == 0:
                padx = (0, half_gap)                # 最左列
            elif column == columns - 1:
                padx = (half_gap, 0)                # 最右列：右侧不留白，贴住滚动条
            else:
                padx = (half_gap, half_gap)
            card.grid(row=index // columns, column=column, sticky="nsew",
                      padx=padx, pady=(0, 8))
            self._bind_card_wheel(card)
        for column in range(columns):
            self.cards_inner.columnconfigure(column, weight=1, uniform="card", minsize=0)
        self.cards_canvas.configure(bg=theme["win_bg"])
        self.cards_inner.configure(bg=theme["win_bg"])
        self.cards_scroll.recolor(theme["win_bg"], theme["scroll_thumb"],
                                  theme["scroll_thumb_hover"], theme["scroll_idle"])
        # 新卡片是 grid 进来的，会盖在垫层上面；把垫层重新压回最底层。
        if cards_backdrop is not None:
            try:
                self.tk.call("lower", cards_backdrop._w)
            except Exception:                   # pragma: no cover
                pass
            try:
                self._shell.invalidate()
            except Exception:                   # pragma: no cover
                pass
        self.root.after_idle(self._sync_cards_scrollregion)

    def _font_measure(self, font: tuple, text: str | None = None) -> int:
        """用 Tk 字体度量取真实像素宽（text=None 时取行高）。

        Tk 的 Entry「width」单位是该字体里 "0" 字形的宽度，与中英混排的真实宽度
        没有对应关系：短型号按 _display_width()+1 定宽会凭空多出二十多像素空白，
        协议徽章就被推得离型号很远。所以宽度必须用 font.measure() 实测。
        取不到字体时退回粗估，绝不因为度量失败让界面崩掉。
        """
        try:
            import tkinter.font as tkfont

            metrics = tkfont.Font(root=self.root, font=font)
            if text is None:
                return int(metrics.metrics("linespace"))
            return int(metrics.measure(text))
        except Exception:
            if text is None:
                return (self.base_font[1] + 4) * 2
            return self._display_width(text) * 8

    def _card_fixed_text(self, master: Any, text: str, *, bg: str, fg: str, font: tuple,
                         side: str = "left", extra: int = 2) -> Any:
        """按文字真实像素宽锁宽的只读 Entry（外层 Frame 定宽、不随内容伸缩）。

        返回外层 Frame：它紧跟着下一个控件，中间只剩设计好的间距，
        不会出现「型号后面一大段空白、徽章被推到远处」的观感问题。
        """
        tk = self.tk
        width = self._font_measure(font, text) + extra
        holder = tk.Frame(master, bg=bg, width=width, height=self._font_measure(font))
        holder.pack(side=side)
        holder.pack_propagate(False)
        self._card_text(holder, text, bg=bg, fg=fg, font=font, width=1
                        ).pack(fill="both", expand=True)
        return holder

    @staticmethod
    def _display_width(text: str) -> int:
        """按显示列数估算 tk.Entry 的 width（中文/全角按 2 列算）。

        Entry 的 width 单位是「平均字符宽度」，一个汉字实际占两列，
        所以 len("已更新记录") + 1 只有 6 列，中文会被截成「已更新记」。
        """
        return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                   for ch in str(text))

    def _card_text(self, master: Any, text: str, *, bg: str, fg: str, font: tuple,
                   anchor: str = "w", **kwargs: Any) -> Any:
        """卡片里的文字控件：只读 Entry，鼠标可以选中并 Ctrl+C 复制。

        用 tk.Label 时文字完全选不中（用户反馈过），换只读 Entry 后
        选择、拖选、Ctrl+C 全都是原生行为；readonlybackground 让底色与卡片一致，
        relief/flat + 无高亮边框让外观和 Label 看不出差别。
        """
        tk = self.tk
        kwargs.setdefault("width", 1)   # 默认 width=20 会把卡片撑宽，靠 fill 拉伸即可
        entry = tk.Entry(master, state="readonly", readonlybackground=bg, fg=fg,
                         relief="flat", bd=0, highlightthickness=0, takefocus=0,
                         font=font, **kwargs)
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        entry.configure(state="readonly")
        entry.configure(justify=anchor if anchor in ("left", "right", "center") else "left")
        return entry

    def _use_photo_cards(self) -> bool:
        """第 7 套主题才用自绘卡片：只有这套主题整窗透壁纸。

        其它主题维持 Frame + 只读 Entry 的老结构——卡片里的文字靠 Entry 提供
        原生拖选与 Ctrl+C，而这套主题的卡内文字改由 PhotoCard 自己画。
        """
        return PhotoCard is not None and self.theme_key == THEME_PHOTO

    def _card_metric_items(self, row: DiskRow,
                           theme: dict[str, Any]) -> list[tuple[str, str, str, bool]]:
        """把 CARD_METRICS 翻成 (标题, 文本, 颜色, 是否等宽) 四元组。

        与 _build_card 里的取值规则保持一致：容量用 fmt_size_short、四个增量
        带 GB 单位、时长与 0E 用界面字体（中英混排塞进 Consolas 会很别扭）。
        """
        alert = row.alert
        foreground = (theme["alert_fg"] if alert
                      else (theme["muted_fg"] if (row.failed or row.vanished)
                            else theme["tree_fg"]))
        if self.view_key == VIEW_GRID:
            metrics = CARD_METRICS_GRID
        else:
            metrics = CARD_METRICS
        items: list[tuple[str, str, str, bool]] = []
        for caption, field in metrics:
            value = getattr(row, field, None)
            if field == "read_gb":
                text = fmt_size_short(row.read_gb)
            elif field == "write_gb":
                text = fmt_size_short(row.write_gb)
            elif field in ("read_delta_gb", "write_delta_gb",
                           "hist_read_delta_gb", "hist_write_delta_gb"):
                text = fmt_delta_gb(value)
            elif field == "interval_text":
                text = row.interval_text
            else:
                text = str(value if value is not None else "—")
            color = foreground
            if alert and field == "media_text":
                color = theme["alert_fg"]
            numeric = field not in ("interval_text", "media_text")
            items.append((tr(caption), text, color, numeric))
        return items

    def _photo_card_palette(self, theme: dict[str, Any]) -> dict[str, Any]:
        """PhotoCard 只认这几个颜色键，缺的色调各自兜底。"""
        return {
            "fg": theme["tree_fg"],
            "note_fg": theme["note_fg"],
            "card_lbl": theme["card_lbl"],
            "head_bg": theme["head_bg"],
            "head_fg": theme["head_fg"],
            "seg_border": theme["seg_border"],
            "sel_bg": theme["select_bg"],
            "sel_fg": theme["select_fg"],
        }

    def _build_photo_card(self, row: DiskRow, theme: dict[str, Any]) -> Any:
        """第 7 套主题的卡片：壁纸切片 + 近白薄罩 + 画布上的文字。

        为什么不用 _build_card：tk.Entry 的 readonlybackground 只能是一种平色，
        压到壁纸亮斑处与周围差 60~86 个通道，一张卡上会浮出十几个矩形补丁。
        自绘之后卡面与壁纸连续，拖选与 Ctrl+C 由 PhotoCard 自己实现。
        """
        alert = row.alert
        muted = row.failed or row.vanished
        background = (theme["alert_bg"] if alert and theme["alert_bg"]
                      else theme["card_bg"])
        foreground = (theme["alert_fg"] if alert
                      else (theme["muted_fg"] if muted else theme["tree_fg"]))
        border = theme["alert_fg"] if alert else theme["card_border"]
        if self.view_key == VIEW_GRID:
            columns = CARD_METRIC_COLUMNS_GRID
        else:
            columns = CARD_METRIC_COLUMNS
        spec = {
            "status": _truncate(row.status_text, 30),
            "status_fg": theme["alert_fg"] if alert else theme["note_fg"],
            "model": row.model,
            "foreground": foreground,
            "protocol": row.protocol,
            "drive": row.drives or "",
            "subtitle": (f"{_serial_line(row, self.show_serial, self.show_gib)}"
                         f"  ·  {row.device_path}"),
            "metrics": self._card_metric_items(row, theme),
            "columns": columns,
            "border": border,
        }
        if alert:
            spec["veil_rgb"] = _photo_card.ALERT_VEIL_RGB
            spec["veil_alpha"] = _photo_card.ALERT_VEIL_ALPHA
        else:
            spec["veil_rgb"] = _photo_card.VEIL_RGB
            spec["veil_alpha"] = _photo_card.VEIL_ALPHA
        card = PhotoCard(self.cards_inner, self._shell, spec=spec,
                         palette=self._photo_card_palette(theme),
                         font_family=self.base_font[0],
                         font_size=self.base_font[1],
                         num_family=str(theme.get("num_family") or "Consolas"),
                         background=background,
                         on_copy=self._copy_card_text)
        self._photo_cards.append(card)
        return card

    def _copy_card_text(self, text: str) -> None:
        """自绘卡片里的 Ctrl+C：与表格走同一条剪贴板出口。"""
        if not text:
            return
        self._copy_text(text, tr("已复制：{value}", value=text))

    def _build_card(self, row: DiskRow, theme: dict[str, Any]) -> Any:
        """一块盘一张卡片：标题行（型号 + 协议 + 状态）+ 序列号行 + 指标格。"""
        tk = self.tk
        alert = row.alert
        muted = row.failed or row.vanished
        background = theme["alert_bg"] if alert and theme["alert_bg"] else theme["card_bg"]
        foreground = (theme["alert_fg"] if alert
                      else (theme["muted_fg"] if muted else theme["tree_fg"]))
        border = theme["alert_fg"] if alert else (theme["card_border"] or theme["win_bg"])
        outer = tk.Frame(self.cards_inner, bg=background, highlightbackground=border,
                         highlightthickness=1)
        inner = tk.Frame(outer, bg=background, padx=12, pady=9)
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=background)
        title_row.pack(fill="x")
        status_text = _truncate(row.status_text, 30)
        self._card_fixed_text(title_row, status_text, bg=background,
                              fg=theme["alert_fg"] if alert else theme["note_fg"],
                              font=(self.base_font[0], self.base_font[1] - 1),
                              side="right")
        name_row = tk.Frame(title_row, bg=background)
        name_row.pack(side="left", fill="x", expand=True)
        # 型号用「按真实字宽锁宽、不填充」的只读 Entry：它会紧挨着后面的协议徽章，
        # 而不是被 fill="x" 拉到整行最右、把徽章挤到状态文字跟前（v0.5.2 的观感问题）。
        # 宽度必须用 font.measure 实测：Entry 的 width 单位是 "0" 字形宽，
        # 按中文占两列的估算定宽会给短型号凭空多出二十来像素空白，徽章被推远。
        self._card_fixed_text(name_row, row.model, bg=background, fg=foreground,
                              font=(self.base_font[0], self.base_font[1] + 1, "bold"))
        # 协议徽章紧跟型号（保持 Label：它是色块标签，不是需要复制的文字）
        tk.Label(name_row, text=row.protocol, bg=theme["head_bg"] or background,
                 fg=theme["head_fg"], font=(self.base_font[0], self.base_font[1] - 1),
                 padx=6).pack(side="left", padx=(8, 0))
        # 盘符徽章紧跟在协议徽章后面：实心协议章 + 空心盘符章，一眼分得清两者
        # （拿不到盘符时整块不渲染，而不是显示一个空的框）
        if row.drives:
            tk.Label(name_row, text=row.drives, bg=background, fg=foreground,
                     font=(self.base_font[0], self.base_font[1] - 1), padx=6,
                     highlightthickness=1,
                     highlightbackground=theme["seg_border"],
                     highlightcolor=theme["seg_border"]
                     ).pack(side="left", padx=(6, 0))

        subtitle = (f"{_serial_line(row, self.show_serial, self.show_gib)}"
                    f"  ·  {row.device_path}")
        self._card_text(inner, subtitle, bg=background,
                        fg=theme["note_fg"],
                        font=(self.base_font[0], self.base_font[1] - 1), width=1
                        ).pack(fill="x", pady=(2, 7))

        metric_row = tk.Frame(inner, bg=background)
        metric_row.pack(fill="x")
        # 等宽网格：标签与数值按列对齐，行与行之间不会错位
        # 双列用单独一份排布：每列只放同一类指标（读 / 写 / 上下文），
        # 「本次读增」正下方就是「历史读增」，不用横着找。
        if self.view_key == VIEW_GRID:
            columns, metrics = CARD_METRIC_COLUMNS_GRID, CARD_METRICS_GRID
        else:
            columns, metrics = CARD_METRIC_COLUMNS, CARD_METRICS
        for column in range(columns):
            metric_row.columnconfigure(column, weight=1, uniform="metric")
        for index, (caption, field) in enumerate(metrics):
            value = getattr(row, field, None)
            if field == "read_gb":
                text = fmt_size_short(row.read_gb)
            elif field == "write_gb":
                text = fmt_size_short(row.write_gb)
            elif field in ("read_delta_gb", "write_delta_gb",
                           "hist_read_delta_gb", "hist_write_delta_gb"):
                # 卡片每格是独立小字，没有列头写「(GB)」，所以这里必须带单位
                text = fmt_delta_gb(value)
            elif field == "interval_text":
                text = row.interval_text
            else:
                text = str(value if value is not None else "—")
            color = foreground
            if alert and field == "media_text":
                color = theme["alert_fg"]
            # 只有纯数字（容量/增量/健康度）用等宽字体对齐；时长与状态文字用界面字体，
            # 否则「22 天 12 小时 15 分钟」这种中英混排在 Consolas 里会很别扭
            numeric = field not in ("interval_text", "media_text")
            value_font = ((theme["num_family"], self.base_font[1] + 1, "bold") if numeric
                          else (self.base_font[0], self.base_font[1] + 1, "bold"))
            cell = tk.Frame(metric_row, bg=background)
            cell.grid(row=index // columns, column=index % columns,
                      sticky="ew", padx=(0, 14), pady=(0, 2))
            self._card_text(cell, tr(caption), bg=background, fg=theme["card_lbl"],
                            font=(self.base_font[0], self.base_font[1] - 1)).pack(fill="x")
            self._card_text(cell, text, bg=background, fg=color,
                            font=value_font).pack(fill="x")
        return outer

    def rows_to_tsv(self, rows: Sequence[DiskRow] | None = None) -> str:
        """把行导成 TSV。与当前视图无关，卡片视图下复制表格也不会报错。"""
        headers = [tr(spec[1]) for spec in COLUMN_SPECS]
        lines = ["	".join(headers)]
        for row in (self.rows if rows is None else rows):
            lines.append("	".join(
                str(value).replace(PAIR_SEPARATOR, TSV_PAIR_SEPARATOR)
                for value in _row_values(row, self.show_serial, self.show_gib)))
        return "\n".join(lines)

    # ---------- 数据刷新 ----------
    def _sync_refresh_button(self) -> None:
        """刷新按钮的文字跟着忙碌态走。

        调用方负责 state（禁用/恢复），这里只管文字：读取时写「读取中…」，
        完成后回到「刷新记录」。切语言时也要走一遍，否则英文界面会卡在
        上一次留下的中文「读取中…」（set_language 只 configure 文字）。
        """
        self.refresh_button.configure(text=tr("读取中…") if self._busy else tr("刷新记录"))

    def refresh_async(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.refresh_button.configure(state="disabled")
        self._sync_refresh_button()
        # 刷新是把整张表换掉，旧的「已复制 / 已选中 / 请先用右键」说的是刷新前那份数据，
        # 留着只会误导，所以绿红两条一起清掉；黄色系统提示（未提权、缺 smartctl）不清。
        self._clear_action()
        self.source_text = tr("读取中…")
        self.smartctl_label.configure(text=self._source_label_text())
        self.status_queue: list[RunOutcome | Exception] = []

        def worker() -> None:
            try:
                outcome = perform_run(self.provider, self.store, now=self.now_func(),
                                      resolver=self.resolver)
            except Exception as exc:  # 不让后台线程把异常带进主循环
                traceback.print_exc()
                self.status_queue.append(exc)
            else:
                self.status_queue.append(outcome)
            finally:
                try:
                    self.root.after(60, self._poll)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True, name="disk-reader").start()

    def _poll(self) -> None:
        if not self.status_queue:
            self.root.after(60, self._poll)
            return
        item = self.status_queue.pop()
        self._busy = False
        self.refresh_button.configure(state="normal")
        self._sync_refresh_button()
        if isinstance(item, Exception):
            self.source_text = tr("读取异常")
            self.smartctl_label.configure(text=self._source_label_text())
            self._set_ok(None)
            self._set_error(tr("读取失败：{error}", error=item))
            return
        self.apply_outcome(item)

    def apply_outcome(self, outcome: RunOutcome) -> RunOutcome:
        self._clear_action()
        self.clear_rows()
        self.rows = list(outcome.rows)
        self.render_rows(self.rows)

        # 时间戳在界面上按「2026-04-30 08:00:00 (UTC+8)」显示；入库的仍是原始 ISO 串
        self.prev_stamp_text = format_stamp(outcome.previous_stamp)
        self.cur_stamp_text = format_stamp(outcome.current_stamp)
        self.prev_label.configure(
            text=tr("上次记录：{stamp}", stamp=self.prev_stamp_text)
        )
        self.cur_label.configure(
            text=tr("本次记录：{stamp}", stamp=self.cur_stamp_text)
        )
        # 有路径就显示 smartctl 路径，没有就显示数据源状态
        # （status 本身在 providers.read_all 里已按当前语言生成，不再二次翻译）
        self.source_text = outcome.smartctl_path or outcome.provider_status
        self.smartctl_label.configure(text=self._source_label_text())
        if outcome.smartctl_path is None and outcome.provider_status:
            self._append_hint(tr(SMARTCTL_INSTALL_HINT))

        totals = outcome.totals
        if outcome.baseline:
            self.summary_label.configure(text=tr("已记录基线，下次打开将显示增量"))
        else:
            # 括号里的数值与单位不进翻译表：英文用同一串模板，只换标签词
            self.summary_label.configure(
                text=tr("本次总读增 {read} GB ｜ 本次总写增 {write} GB ｜ "
                        "历史总读增 {hread} GB ｜ 历史总写增 {hwrite} GB",
                        read=fmt_delta(totals.get("read_delta")),
                        write=fmt_delta(totals.get("write_delta")),
                        hread=fmt_delta(totals.get("hist_read_delta")),
                        hwrite=fmt_delta(totals.get("hist_write_delta")))
            )
        for key, label in self.kpi_values.items():
            label.configure(text="—" if outcome.baseline else fmt_delta_compact(totals.get(key)))
        self.alert_label.configure(
            text=tr("介质错误在累积，建议关注备份") if outcome.media_alert else ""
        )
        self._set_ok(outcome)
        if outcome.history_message:
            self._append_hint(outcome.history_message)
        self.note_label.configure(
            text=self._history_note_text(outcome.history_path, baseline=outcome.baseline)
        )
        # 留一份给「切换语言」用：里面存着本次的历史记录与快照，能原地重算行文案
        self.last_outcome = outcome
        return outcome

    def run_pipeline_sync(self) -> RunOutcome:
        """同步跑一遍流水线（GUI 冒烟测试用，不启动线程）。"""
        outcome = perform_run(self.provider, self.store, now=self.now_func(),
                              resolver=self.resolver)
        return self.apply_outcome(outcome)

    # ---------- 菜单动作 ----------
    def close(self) -> None:
        """关闭窗口即退出：不留后台线程、不写服务、不设自启。"""
        try:
            self.root.destroy()
        except Exception:
            pass

    def copy_table(self) -> None:
        """把当前数据导成 TSV 放进剪贴板。

        直接由 rows 生成（不读 Treeview 的 values），因此卡片/双列视图下同样可用。
        """
        text = self.rows_to_tsv()
        self._copy_text(text, tr("已复制 {count} 行数据到剪贴板。", count=len(self.rows)))

    # ---------- 表格选区的复制 ----------
    def selected_rows(self) -> list[DiskRow]:
        """当前选中的行，按屏幕顺序返回。

        用 get_children() 的顺序而不是 selection() 的顺序：按住 Shift 从上往下选和
        从下往上选，selection() 给的顺序都是点击顺序，粘出来会是倒的。
        """
        chosen = set(self.tree.selection())
        return [self._iid_to_row[iid] for iid in self.tree.get_children()
                if iid in chosen and iid in self._iid_to_row]

    def _copy_text(self, text: str, hint: str) -> bool:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except Exception as exc:
            self._set_error(tr("复制到剪贴板失败：{error}", error=exc))
            return False
        self._set_action(hint)
        return True

    def copy_selection(self) -> None:
        """复制选中行（含表头）；没有选中任何行时退化为复制整表。"""
        rows = self.selected_rows()
        if not rows:
            self.copy_table()
            return
        self._copy_text(self.rows_to_tsv(rows),
                        tr("已复制 {count} 行数据到剪贴板。", count=len(rows)))

    def copy_selection_event(self, _event: Any = None) -> str:
        """Ctrl+C。返回 "break"，避免 Treeview 的默认处理把事件再吃掉一次。"""
        self.copy_selection()
        return "break"

    def select_all_rows(self) -> None:
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children)
            self._set_action(tr("已选中 {count} 行，按 Ctrl+C 复制。", count=len(children)))

    def copy_cell(self) -> None:
        """复制最近一次右键/双击落在的单元格。"""
        row = self._iid_to_row.get(self._cell_iid)
        keys = [spec[0] for spec in COLUMN_SPECS]
        if row is None or self._cell_column_key not in keys:
            self._set_error(tr("请先用右键点一下要复制的单元格。"))
            return
        value = _row_values(row, self.show_serial,
                            self.show_gib)[keys.index(self._cell_column_key)]
        text = str(value).replace(PAIR_SEPARATOR, TSV_PAIR_SEPARATOR)
        # 只给数值：列名在表头里已经写着，再重复一遍「「本次读增 / 写增」单元格：」
        # 除了占地方没别的用处（用户截图里圈出来要去掉的就是这一段）。
        self._copy_text(text, tr("已复制：{value}", value=text))

    def _remember_cell(self, x: int, y: int) -> bool:
        """记住右键落点：命中的行加入选区，命中的列记下来给「复制此单元格」用。

        抽成独立方法是为了能在没有真实布局（窗口未映射、bbox 拿不到）时单独测试——
        直接调 _on_tree_context_menu 会真的弹菜单并 grab，测试里不好收场。
        """
        iid = self.tree.identify_row(y)
        if not iid:
            return False
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self._cell_iid = iid
        keys = [spec[0] for spec in COLUMN_SPECS]
        column = self.tree.identify_column(x)
        index = int(column[1:]) - 1 if column.startswith("#") and column[1:].isdigit() else -1
        self._cell_column_key = keys[index] if 0 <= index < len(keys) else ""
        return True

    def _sync_tree_menu(self) -> None:
        """按当前选区刷新右键菜单：没命中单元格时禁用「复制此单元格」。

        定位菜单项一律用下标（self._tree_menu_index），不能用标签：下面第一个
        entryconfigure 会把「复制选中行」的标签改成带「N 行」的形式，若再用原
        标签去查就会抛 TclError，菜单从此弹不出来（第二次右键失效的根因）。
        """
        menu = getattr(self, "tree_menu", None)
        indices = getattr(self, "_tree_menu_index", None)
        if menu is None or not indices:
            return
        menu.entryconfigure(indices[TREE_MENU_COPY_CELL],
                            state="normal" if self._cell_column_key else "disabled")
        menu.entryconfigure(indices[TREE_MENU_COPY_ROWS], label=self._copy_rows_label())

    def _copy_rows_label(self) -> str:
        """右键第一项的标签；有选中行时在标签后补计数（\t 让 Tk 把数字右对齐）。"""
        label = tr(TREE_MENU_COPY_ROWS)
        count = len(self.tree.selection())
        if count == 0:
            return label
        return f"{label}\t{tr('{count} 行', count=count)}"

    def _on_tree_context_menu(self, event: Any) -> str:
        """右键：先把光标下的行加入选中（点在选区外时），再弹菜单。

        这里必须自己兜住异常：绑定在 Tk 上的回调抛异常只会打到 stderr，而打包成
        --noconsole 后 stderr 看不见，表现就是「右键没反应」，很难查。
        """
        if not self._remember_cell(event.x, event.y):
            return "break"
        try:
            self._sync_tree_menu()
            self.tree_menu.tk_popup(event.x_root, event.y_root)
        except Exception as exc:                      # noqa: BLE001 - 不让菜单故障炸窗口
            self._set_error(tr("打开右键菜单失败：{error}", error=exc))
        finally:
            try:
                self.tree_menu.grab_release()
            except Exception:                          # noqa: BLE001 - 没 grab 过时释放会报错
                pass
        return "break"

    def open_data_dir(self) -> None:
        """打开 history.json / settings.json 所在目录。"""
        try:
            directory = self.store.resolve_dir()
            directory.mkdir(parents=True, exist_ok=True)
            if platform.system() == "Windows":
                os.startfile(str(directory))  # type: ignore[attr-defined]
            else:
                import webbrowser
                webbrowser.open(directory.as_uri())
        except Exception as exc:
            self._set_error(tr("打开数据目录失败：{error}", error=exc))

    def _open_tool(self, filename: str, caption: str) -> None:
        """打开一个 Windows 自带管理工具（磁盘管理 / 设备管理器）。

        失败不弹窗、也不假装成功：直接走红色操作行，把系统原话带上
        （最常见的是「没有提权」，用户看到原话自己就知道该右键以管理员运行）。
        绿色的「已打开」只在这一步真的没抛异常时才写。
        """
        try:
            open_management_console(filename)
        except Exception as exc:  # noqa: BLE001 - 打不开就是打不开，原样报给用户
            self._set_error(tr("打开工具失败：{error}", error=exc))
            return
        self._set_action(tr("已打开 {name}。", name=tr(caption)))

    def diagnostics_text(self) -> str:
        """诊断信息：出问题时让用户能一键复制出来。"""
        return "\n".join((
            tr("{name} v{version}（{exe}.exe）", name=tr(APP_DISPLAY_NAME),
               version=APP_VERSION, exe=APP_EXE_NAME),
            tr("Python：{version} / {platform}", version=platform.python_version(),
               platform=platform.platform()),
            tr("主题：{theme} → {label}",
               theme=tr(THEME_LABELS.get(self.theme_key, self.theme_key)),
               label=tr(self.theme["label"])),
            tr("权限：{value}", value=tr("管理员") if is_admin() else tr("未以管理员运行")),
            self._source_label_text(),
            tr("上次记录：{stamp}", stamp=self.prev_stamp_text),
            tr("本次记录：{stamp}", stamp=self.cur_stamp_text),
            tr("历史文件：{path}（schema v{version}）", path=self.store.path,
               version=SCHEMA_VERSION),
            tr("偏好文件：{path}", path=self.settings.path),
            tr("视图：{value}", value=tr(VIEW_LABELS.get(self.view_key, self.view_key))),
            tr("序列号：{value}", value=tr("显示") if self.show_serial else tr("已隐藏")),
            tr("系统口径容量：{value}", value=tr("显示") if self.show_gib else tr("已隐藏")),
            tr("磁盘行数：{count}", count=len(self.rows)),
        ))

    def copy_diagnostics(self) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.diagnostics_text())
            self._set_action(tr("已复制诊断信息到剪贴板。"))
        except Exception as exc:
            self._set_error(tr("复制诊断信息失败：{error}", error=exc))

    def show_source_status(self) -> None:
        from tkinter import messagebox

        messagebox.showinfo(tr("数据源状态"), self.diagnostics_text(), parent=self.root)

    def show_source_help(self) -> None:
        from tkinter import messagebox

        messagebox.showinfo(
            tr("如何安装数据源"),
            tr("本程序通过 smartmontools 的 smartctl 读取磁盘累计读写量与 0E。\n"
               "\n"
               "通常 exe 已自带一份，无需安装；若顶部提示未找到，可任选一种：\n"
               "1) winget install smartmontools.smartmontools\n"
               "2) 到 smartmontools 官网下载安装包，默认装到\n"
               "   C:\\Program Files\\smartmontools\\bin\\\n"
               "\n"
               "安装后点「文件 → 刷新记录」即可。读取磁盘信息通常需要管理员权限。"),
            parent=self.root,
        )

    def show_capacity_help(self) -> None:
        from tkinter import messagebox

        messagebox.showinfo(
            tr("容量与换算说明"),
            tr("统一口径：{units}\n"
               "\n"
               "表格首列括号外的容量是厂商标称值（按 1 GB = 1,000,000,000 字节），\n"
               "括号内是系统显示口径（按 1 GiB = 1024^3 字节），所以同一块盘\n"
               "会同时看到「960 GB」和「894.25 GiB」，两者说的是同一个字节数。\n"
               "括号部分默认不显示，可在「工具 → 显示系统口径容量」打开或关闭；\n"
               "开关只影响显示，history.json 里存的始终是字节数与 GB 数值。\n"
               "\n"
               "累计读取 / 累计写入来自硬盘自己维护的计数器：\n"
               "· NVMe：Data Units Read / Written，1 Data Unit = 512000 字节\n"
               "· SATA：Device Statistics 的 Logical Sectors，1 LBA = 512 字节\n"
               "  （该字段缺失时才退回 SMART 属性 241/242，部分主控按块计数，数值可能偏小）\n"
               "\n"
               "0E 是 NVMe 的「介质与数据完整性错误」计数，只增不减；\n"
               "SATA 盘没有这个字段，显示「不适用」，可参考重映射扇区与待定扇区。",
               units=tr(BYTES_PER_GIB_TEXT)),
            parent=self.root,
        )

    # ---------- 关于 ----------
    def show_about(self) -> None:
        """弹出版本/来源说明。正文完全由 about_text() 生成，只引用 APP_VERSION。"""
        from tkinter import messagebox

        messagebox.showinfo(tr("关于 {title}", title=window_title()),
                            about_text(self.store.path), parent=self.root)

    def reset_history_file(self) -> None:
        """重置历史文件：二次确认 → 旧文件改名备份 → 清空重建基线 → 重新记录一次。

        备份用改名实现（history.reset-<时间戳>.json），不直接删：点错了还能找回。
        """
        from tkinter import messagebox

        path = self.store.path
        confirmed = messagebox.askyesno(
            tr("重置历史文件"),
            tr("将清空全部历史记录并重建基线：\n{path}\n\n"
               "旧文件会改名备份为 history.reset-<时间戳>.json，不会直接删除。\n"
               "重置后立即重新记录一次。确定继续吗？", path=path),
            parent=self.root,
        )
        if not confirmed:
            return
        try:
            backup = self.store.reset()
        except OSError as exc:
            self._set_error(tr("重置历史文件失败：{error}", error=exc))
            return
        if backup is None:
            self._append_hint(tr("历史文件已重置（旧文件未能备份，可能已被占用）。"))
        else:
            self._append_hint(tr("历史文件已重置，旧文件备份为 {name}。", name=backup.name))
        self.refresh_async()

    def open_history_file(self) -> None:
        path = self.store.path
        try:
            if not path.exists():
                self.store.save([])
            if platform.system() == "Windows":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                import webbrowser
                webbrowser.open(path.as_uri())
        except Exception as exc:
            self._set_error(tr("打开历史文件失败：{error}", error=exc))
