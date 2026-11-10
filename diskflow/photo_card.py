"""第 7 套「图片背景」主题的卡片：自绘卡片底座 + 半透明薄罩 + 字符级可选文字。

为什么不复用 ttk.Frame + 只读 Entry：
    Entry 的 readonlybackground 只能是**一个平色**。卡片里十几处文字块各自
    用平色压在壁纸纹理上，亮斑处的通道差实测 60~86，肉眼就是一块块补丁；
    而参照图（用户认可的观感）里卡片内部是连续过渡的，没有任何平色块。
    所以卡内文字必须画在画布上，由画布把「壁纸切片 + 薄罩 + 文字」合成。

薄罩参数是反解出来的（不是拍脑袋）：
    以 C4_grid.png（用户选定的参照效果图）的卡片内部为真值，只取壁纸平坦
    区块（9x9 局部标准差 < 1.5，避开文字），对「对话层之上再叠一层纯白」
    做参数搜索，得到 alpha≈0.27 时平均误差 6.2 个通道；不叠这层是 27.6。

    Tk 8.6 对 RGBA PNG 做真正的 alpha 合成（实测：50% 白压在纯蓝图片项上
    得到 (128,128,255)），所以薄罩用图片而不是矩形——矩形只能是不透明色。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Any, Callable

from . import fontcache
from .canvas_surface import CharRun, fit_text
from .pngenc import capsule_rgba_png, solid_rgba_png  # noqa: F401  对外再导出
from .pngenc import VeilImage
from .shell_canvas import SliceCanvas

# 卡片内边距（与旧 _build_card 的 inner padx=12 / pady=9 对齐）
CARD_PAD_X = 12
CARD_PAD_Y = 9
# 指标格之间的横向间隙（旧实现 padx=(0, 14)）
METRIC_GAP_X = 14
# 指标「行」之间的间隙。标签与数值是同一组的上下两行，块高就是 linespace，
# 组内没有额外留白（字形上下各有一段 linespace 余量，看起来约 7px）；
# 行与行之间必须明显大于它，否则四行等距铺开、看不出「标签-数值」成对。
# 参照图里组内 : 组间 ≈ 1 : 2.5，这里取 12（渲染出来组间约 19px）。
METRIC_GAP_Y = 12
# 标题行 / 副标题 / 指标区之间的纵向留白
TITLE_GAP = 2
SUBTITLE_GAP = 10
# 徽章：左右内边距、徽章之间的间距、型号与首个徽章的间距
BADGE_PAD_X = 6
BADGE_GAP = 6
BADGE_PROTOCOL_GAP = 8
# 状态文字与右侧徽章之间的最小留白
STATUS_GAP = 8

# 薄罩颜色与不透明度（见模块 docstring 的反解过程）
VEIL_RGB: tuple[int, int, int] = (255, 255, 255)
VEIL_ALPHA = 0.27
# 告警卡：参照图里整卡偏红，薄罩换成浅红
ALERT_VEIL_RGB: tuple[int, int, int] = (255, 224, 221)
ALERT_VEIL_ALPHA = 0.55

# 未映射时（winfo_width() <= 1）用来估算落地高度的兜底宽度
FALLBACK_WIDTH = 560


# solid_rgba_png 已收敛到 diskflow/pngenc.py（本模块顶部导入后再导出，
# 保持 main.py 的 __all__ 与既有测试的 app.solid_rgba_png 引用不变）。


class _Block:
    """一块可选中的文字：文本、画布矩形、字符条。"""

    __slots__ = ("text", "x", "y", "width", "height", "run", "fill")

    def __init__(self, text: str, x: float, y: float,
                 width: float, height: float, fill: str) -> None:
        self.text = text
        self.x = float(x)
        self.y = float(y)
        self.width = float(width)
        self.height = float(height)
        self.fill = fill
        self.run: CharRun | None = None

    def contains(self, x: float, y: float) -> bool:
        return (self.x <= x <= self.x + self.width
                and self.y <= y <= self.y + self.height)


class PhotoCard(SliceCanvas):
    """自绘卡片：壁纸切片 + 半透明薄罩 + 文字，支持拖选与 Ctrl+C。

    与 SliceCanvas 的关系：父类只负责画壁纸切片（且明确不 delete("all")），
    这里接管全部重绘，因为薄罩和文字也要画在同一块画布上。
    """

    def __init__(self, master: Any, surface: Any = None, *,
                 spec: dict[str, Any],
                 palette: dict[str, Any],
                 font_family: str = "Microsoft YaHei UI",
                 font_size: int = 9,
                 num_family: str = "Consolas",
                 background: str = "#F2F5FA",
                 on_copy: Callable[[str], Any] | None = None,
                 **kwargs: Any) -> None:
        # 这些必须在 super().__init__ 之前就位：父类初始化里就可能触发 repaint()
        self._spec = dict(spec)
        self._palette = dict(palette)
        self._on_copy = on_copy
        self._flat_bg = background
        # 字体走 fontcache：一个卡片视图一次建四个，滚动时反复重建；直接建的
        # Font 被 GC 时（可能落在 worker 线程）会跨线程调 Tcl，把线程卡住。
        self._font_small = fontcache.cached_font(master, family=font_family,
                                                 size=max(7, font_size - 1))
        self._font_title = fontcache.cached_font(master, family=font_family,
                                                 size=font_size + 1, weight="bold")
        self._font_value = fontcache.cached_font(master, family=num_family,
                                                 size=font_size + 1, weight="bold")
        self._font_text = fontcache.cached_font(master, family=font_family,
                                                size=font_size + 1, weight="bold")
        self._blocks: list[_Block] = []
        self._veil_images = VeilImage(self)
        self._sel_rects: list[int] = []
        self._sel_range: tuple[int, int, int, int] | None = None
        self._anchor: tuple[int, int] | None = None
        self._pressing = False
        self._req_height = 0
        super().__init__(master, surface, background=background, **kwargs)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", self._on_double)
        self.bind("<Control-c>", self._on_copy_event)

    # -- 主题 / 外壳 -------------------------------------------------------

    def set_colors(self, *, background: str, border: str | None = None) -> None:
        """与 CardCanvas 保持同名同签名，_apply_shell_theme 才能一视同仁。"""
        self._flat_bg = background
        self._spec["border"] = border
        self.set_background(background)
        self.repaint()

    # -- 绘制 --------------------------------------------------------------

    def repaint(self) -> None:
        """整卡重画：壁纸切片 → 薄罩 → 边框 → 文字 → 选区。

        第一行必须是 tk.Canvas.delete(self, "all")。本类下面的 delete() 是
        Treeview 兼容接口，写成 self.delete("all") 会递归爆栈。
        """
        tk.Canvas.delete(self, "all")
        self._blocks = []
        self._sel_rects = []
        width = self.winfo_width()
        if width <= 1:
            width = FALLBACK_WIDTH
        height = self.winfo_height()

        photo = self._current_photo()
        if photo is not None:
            offset_x, offset_y = 0, 0
            surface = getattr(self, "shell_surface", None)
            if surface is not None:
                offset_x, offset_y = surface.offset_of(self)
            self.create_image(-offset_x, -offset_y, anchor="nw", image=photo)

        needed = self._layout_height(width)
        if height <= 1:
            height = needed
        if abs(needed - self._req_height) > 0.5:
            self._req_height = needed
            tk.Canvas.configure(self, height=needed)
            height = needed

        if photo is not None:
            self._paint_veil(width, height)
        border = self._spec.get("border")
        if border:
            self.create_rectangle(0, 0, width - 1, height - 1,
                                  outline=border, width=1)
        self._draw_text(width)
        self._paint_selection()

    def _current_photo(self) -> Any:
        surface = getattr(self, "shell_surface", None)
        if surface is None:
            return None
        return surface.image()

    def _paint_veil(self, width: int, height: int) -> None:
        """在整卡上压一层半透明薄罩（图片由 pngenc.VeilImage 缓存，尺寸不变则复用）。"""
        rgb = tuple(self._spec.get("veil_rgb") or VEIL_RGB)
        alpha = self._spec.get("veil_alpha")
        if alpha is None:
            alpha = VEIL_ALPHA
        photo = self._veil_images.image(width, height, rgb, alpha)
        if photo is None:
            return
        self.create_image(0, 0, anchor="nw", image=photo)

    # -- 布局 --------------------------------------------------------------

    def _metric_counts(self) -> tuple[int, int]:
        metrics = self._spec.get("metrics") or ()
        columns = max(1, int(self._spec.get("columns") or 5))
        rows = (len(metrics) + columns - 1) // columns
        return columns, rows

    def _layout_height(self, width: int) -> int:
        del width  # 高度只由字体与指标行数决定，与宽度无关
        title_h = self._font_title.metrics("linespace")
        sub_h = self._font_small.metrics("linespace")
        _, rows = self._metric_counts()
        total = CARD_PAD_Y * 2 + title_h + TITLE_GAP + SUBTITLE_GAP
        if self._spec.get("subtitle"):
            total += sub_h
        # 每行是「标签 + 数值」两块，行间只在下边界后加 METRIC_GAP_Y；
        # 最后一行之后不再加，否则卡片底部会多出一条空带。
        total += rows * (sub_h + self._font_value.metrics("linespace"))
        total += max(0, rows - 1) * METRIC_GAP_Y
        return int(total)

    def _draw_text(self, width: int) -> None:
        spec = self._spec
        palette = self._palette
        small = self._font_small
        x_left = CARD_PAD_X
        x_right = width - CARD_PAD_X
        inner_width = max(40, x_right - x_left)
        title_h = self._font_title.metrics("linespace")
        y = CARD_PAD_Y

        # 标题行：型号（左，按剩余空间截断）→ 协议/盘符徽章 → 状态（右）
        status = str(spec.get("status") or "")
        status_w = small.measure(status) if status else 0
        badges = self._badge_specs()
        badge_span = 0.0
        for index, badge in enumerate(badges):
            badge_span += badge["width"]
            badge_span += BADGE_PROTOCOL_GAP if index == 0 else BADGE_GAP
        model_room = max(40.0, inner_width - status_w - STATUS_GAP - badge_span)
        model = str(spec.get("model") or "")
        model_text = fit_text(self._font_title, model, model_room)
        model_w = self._font_title.measure(model_text)
        self._add_block(model_text, x_left, y, model_w, title_h,
                        self._font_title,
                        spec.get("foreground") or palette.get("fg")
                        or "#1F2933")
        self._draw_badges(x_left + model_w, y, title_h)
        if status:
            self._add_block(status, x_right - status_w, y, status_w, title_h,
                            small, spec.get("status_fg")
                            or palette.get("note_fg") or "#5C6673")
        y += title_h + TITLE_GAP

        # 副标题
        subtitle = str(spec.get("subtitle") or "")
        if subtitle:
            sub_h = small.metrics("linespace")
            self._add_block(fit_text(small, subtitle, inner_width),
                            x_left, y, inner_width, sub_h, small,
                            palette.get("note_fg") or "#5C6673")
            y += sub_h
        y += SUBTITLE_GAP

        # 指标格
        metrics = spec.get("metrics") or ()
        if not metrics:
            return
        columns, _ = self._metric_counts()
        cell_w = (inner_width - METRIC_GAP_X * (columns - 1)) / columns
        cap_h = small.metrics("linespace")
        for index, item in enumerate(metrics):
            caption, value, color, numeric = item
            column = index % columns
            row = index // columns
            font = self._font_value if numeric else self._font_text
            val_h = font.metrics("linespace")
            cell_x = x_left + column * (cell_w + METRIC_GAP_X)
            cell_y = y + row * (cap_h + val_h + METRIC_GAP_Y)
            self._add_block(fit_text(small, caption, cell_w),
                            cell_x, cell_y, cell_w, cap_h, small,
                            palette.get("card_lbl") or "#5C6673")
            self._add_block(fit_text(font, str(value), cell_w),
                            cell_x, cell_y + cap_h, cell_w, val_h, font,
                            color or palette.get("fg") or "#1F2933")

    def _draw_badges(self, x: float, y: float, title_h: float) -> None:
        """在 (x, y) 处从左到右画徽章：实底（协议）或无底描边（盘符）。"""
        small = self._font_small
        height = title_h - 4
        top = y + 2
        text_y = top + (height - small.metrics("linespace")) / 2.0
        cursor = float(x)
        for index, badge in enumerate(self._badge_specs()):
            cursor += BADGE_PROTOCOL_GAP if index == 0 else BADGE_GAP
            width = badge["width"]
            if badge.get("fill"):
                self.create_rectangle(cursor, top, cursor + width,
                                      top + height,
                                      fill=badge["fill"], outline="")
            elif badge.get("outline"):
                self.create_rectangle(cursor, top, cursor + width,
                                      top + height,
                                      outline=badge["outline"], width=1)
            self.create_text(cursor + width / 2.0, text_y, anchor="n",
                             text=badge["text"], font=small, fill=badge["fg"])
            cursor += width

    def _badge_specs(self) -> list[dict[str, Any]]:
        small = self._font_small
        palette = self._palette
        spec = self._spec
        badges: list[dict[str, Any]] = []
        protocol = str(spec.get("protocol") or "")
        if protocol:
            badges.append({
                "text": protocol,
                "width": small.measure(protocol) + BADGE_PAD_X * 2,
                "fill": palette.get("head_bg"),
                "fg": palette.get("head_fg") or "#FFFFFF",
                "outline": None,
            })
        drive = str(spec.get("drive") or "")
        if drive:
            badges.append({
                "text": drive,
                "width": small.measure(drive) + BADGE_PAD_X * 2,
                "fill": None,
                "fg": spec.get("foreground") or palette.get("fg") or "#1F2933",
                "outline": palette.get("seg_border"),
            })
        return badges

    def _add_block(self, text: str, x: float, y: float, max_width: float,
                   height: float, font: tkfont.Font, fill: str) -> None:
        if not text:
            return
        block = _Block(text, x, y, max_width, height, fill or "#1F2933")
        block.run = CharRun(self, font, block.fill, x, y, text)
        self._blocks.append(block)

    # -- 选区 --------------------------------------------------------------

    def _hit(self, x: float, y: float) -> tuple[int, int] | None:
        for index, block in enumerate(self._blocks):
            if block.contains(x, y):
                offset = block.run.hit(x) if block.run is not None else 0
                return index, offset
        return None

    def _on_press(self, event: Any) -> str:
        self.focus_set()
        hit = self._hit(event.x, event.y)
        self._anchor = hit
        self._pressing = hit is not None
        self._sel_range = None
        self._paint_selection()
        return "break"

    def _on_drag(self, event: Any) -> str:
        if not self._pressing or self._anchor is None:
            return "break"
        index, start = self._anchor
        if index >= len(self._blocks):
            return "break"
        block = self._blocks[index]
        end = block.run.hit(event.x) if block.run is not None else start
        self._sel_range = ((index, start, index, end) if start <= end
                           else (index, end, index, start))
        self._paint_selection()
        return "break"

    def _on_release(self, event: Any) -> str:
        del event
        self._pressing = False
        if self._sel_range is not None and self._sel_range[1] == self._sel_range[3]:
            self._sel_range = None
        self._paint_selection()
        return "break"

    def _on_double(self, event: Any) -> str:
        hit = self._hit(event.x, event.y)
        if hit is None:
            return "break"
        index = hit[0]
        self._sel_range = (index, 0, index, len(self._blocks[index].text))
        self._paint_selection()
        return "break"

    def _paint_selection(self) -> None:
        for rect in self._sel_rects:
            tk.Canvas.delete(self, rect)
        self._sel_rects = []
        highlight_bg = self._palette.get("sel_bg") or "#CFE0F5"
        highlight_fg = self._palette.get("sel_fg") or "#12212F"
        chosen: list[Any] = []
        if self._sel_range is not None:
            i0, o0, i1, o1 = self._sel_range
            last = min(i1, len(self._blocks) - 1)
            for index in range(i0, last + 1):
                block = self._blocks[index]
                if block.run is None:
                    continue
                start = max(0, min(len(block.text), o0 if index == i0 else 0))
                end = max(0, min(len(block.text),
                                 o1 if index == i1 else len(block.text)))
                for item in block.run.items_for(start, end):
                    chosen.append(item)
        selected = set(chosen)
        for block in self._blocks:
            if block.run is None:
                continue
            for item in block.run.items:
                if item in selected:
                    box = self.bbox(item)
                    if box is not None:
                        rect = self.create_rectangle(
                            box[0] - 0.5, box[1] - 0.5,
                            box[2] + 0.5, box[3] + 0.5,
                            fill=highlight_bg, outline="")
                        self._sel_rects.append(rect)
                self.itemconfigure(
                    item, fill=(highlight_fg if item in selected else block.fill))
        for item in chosen:
            self.tag_raise(item)

    def selection_text(self) -> str:
        if self._sel_range is None:
            return ""
        i0, o0, i1, o1 = self._sel_range
        parts: list[str] = []
        last = min(i1, len(self._blocks) - 1)
        for index in range(i0, last + 1):
            block = self._blocks[index]
            start = max(0, min(len(block.text), o0 if index == i0 else 0))
            end = max(0, min(len(block.text),
                             o1 if index == i1 else len(block.text)))
            if end > start:
                parts.append(block.text[start:end])
        return " ".join(part for part in parts if part)

    def _on_copy_event(self, event: Any) -> str:
        text = self.selection_text()
        if text and self._on_copy is not None:
            self._on_copy(text)
        return "break"

    # -- Treeview 风格的小兼容面（滚动条/滚轮/测试用） ----------------------

    def get_children(self) -> tuple:
        return ()
