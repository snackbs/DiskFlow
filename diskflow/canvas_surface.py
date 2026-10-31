"""数据区的 Canvas 绘制层（第 7 套「图片背景」主题专用）。

前五套主题的数据区仍然是 ttk.Treeview。它的底色是实心的，压在壁纸上的
半透明面板一看就露馅，只能自己画。为了让 gui.py 里那 27 处 self.tree.*
调用一行都不用改，CanvasTable 复刻了 Treeview 的那一小撮接口：

    heading / column / tag_configure / insert / delete / get_children /
    item / exists / selection / selection_set / selection_remove /
    identify_row / identify_column / see / bbox / yview / xview /
    configure(yscrollcommand=, xscrollcommand=) / ["columns"]

所以 gui.py 只需要决定「这一套主题用哪个类建表格」，其余逻辑两种实现共用。
这也正是「先不切」的落地方式：老主题走 Treeview，新主题走 Canvas，

为什么文字要逐字符画：拖蓝选子串必须知道落点在哪个字上。tk.Entry 能做到
字符级选择，但它的底色是不透明的实色，压在壁纸上会露出一个个方块
（实测同一格内壁纸混色的最大通道差达到 90，肉眼非常明显）。逐字符
create_text 换来了无底色的字符级选择，代价全在开发期。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Any, Sequence

# 一格超过这么多字符就不再逐字符画：item 数量与字符数成正比，
# 实测 1000 个 item 要 120ms，长型号不值得为「选半个型号名」付这个代价。
CHAR_SPLIT_LIMIT = 32

# 滚轮一格滚多少像素（Windows 下 delta 是 120 的倍数）
WHEEL_STEP = 54

PALETTE_KEYS = ("bg", "fg", "line", "head_bg", "head_fg", "sel_bg", "sel_fg", "zebra")

# 单元格里最多画几行：型号/序列号、读/写都是两行，多出来的行直接丢掉，
# 否则有人往 values 里塞十行换行文本，一行能撑到看不见表头。
MAX_CELL_LINES = 3

# CharRun 的键：(行号, 列号, 格内第几行)。早先只有 (行号, 列号)，
# 一格两行文字时第二行会把第一行覆盖掉，拖选只能选到后半截。
RunKey = tuple[int, int, int]

DEFAULT_PALETTE: dict[str, str | None] = {
    "bg": None,            # None = 不画行底色，让壁纸透出来
    "fg": "#0e1216",
    "line": "#d6dbe2",
    "head_bg": "#2b333c",
    "head_fg": "#ffffff",
    "sel_bg": "#3d7ecc",
    "sel_fg": "#ffffff",
    "zebra": None,
}


def fit_text(font: tkfont.Font, text: str, max_width: float) -> str:
    """把文字截到 max_width 以内，截断处补省略号。

    二分找前缀长度，不逐字累加：型号名普遍三四十个字符，长起来很吃亏。
    """
    text = "" if text is None else str(text)
    if max_width <= 0:
        return ""
    if font.measure(text) <= max_width:
        return text
    ellipsis = "…"
    allowance = max_width - font.measure(ellipsis)
    if allowance <= 0:
        return ellipsis
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font.measure(text[:middle]) <= allowance:
            low = middle
        else:
            high = middle - 1
    return text[:low] + ellipsis


class CharRun:
    """一段逐字符绘制的文字，落点能换算成字符下标。

    spans[i] 是第 i 个字符的 [左, 右) 像素区间；hit() 用它把鼠标 x 换算成
    「插在第几个字前面」——拖选就是靠这个下标区间取子串。

    y 是本行的**行顶**（不是基线）：item 用的是 anchor="nw"，它的 y 就是文字
    包围盒的上边。早先按「基线」算 y 再配 anchor="nw"，整个人往下坠了一个
    上升高，单行还能忍，两行就直接压到下一行上（实机截图里型号和序列号糊成
    一片就是这个原因）。top/bottom 留给多行单元格按 y 判断落在第几行。
    """

    __slots__ = ("text", "spans", "items", "top", "bottom")

    def __init__(self, canvas: tk.Canvas, font: tkfont.Font, fill: str,
                 x: float, y: float, text: str, *, split_limit: int = CHAR_SPLIT_LIMIT) -> None:
        self.text = "" if text is None else str(text)
        self.spans: list[tuple[float, float]] = []
        # items[i] 是 spans[i] 对应的 canvas item id：拖选高亮要逐字改 fill
        self.items: list[int] = []
        self.top = float(y)
        self.bottom = float(y) + float(font.metrics("linespace"))
        if not self.text:
            return
        if len(self.text) > split_limit:
            width = font.measure(self.text)
            item = canvas.create_text(x, y, text=self.text, font=font, fill=fill, anchor="nw")
            self.spans = [(x, x + width)]
            self.items = [item]
            return
        cursor = x
        for char in self.text:
            advance = font.measure(char)
            item = canvas.create_text(cursor, y, text=char, font=font, fill=fill, anchor="nw")
            self.spans.append((cursor, cursor + advance))
            self.items.append(item)
            cursor += advance

    def items_for(self, start: int, end: int) -> list[int]:
        """字符区间 [start, end) 对应的 item；越界夹紧，顺序不保证（只用来改色）。"""
        low = max(0, min(int(start), len(self.items)))
        high = max(0, min(int(end), len(self.items)))
        return self.items[low:high] if low < high else []

    @staticmethod
    def split_lines(text: str) -> list[str]:
        """按换行切格；\r\n 与单独的 \r 都算换行，尾部空行丢掉。

        gui.py 的成对列用 PAIR_SEPARATOR="\n" 把「读 / 写」拼在一个格里，
        这里切开才能一行画一项。
        """
        raw = "" if text is None else str(text)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        lines = raw.split("\n")
        while len(lines) > 1 and not lines[-1].strip():
            lines.pop()
        return lines[:MAX_CELL_LINES] if lines else [""]

    def hit(self, x: float) -> int:
        """把绝对 x 换算成字符下标 0..len；落在字中间时取更近的一侧。"""
        if not self.spans:
            return 0
        if x <= self.spans[0][0]:
            return 0
        for index, (left, right) in enumerate(self.spans):
            if x < left:
                return index
            if x <= right:
                return index if x - left < right - x else index + 1
        return len(self.spans)

    def span_of(self, start: int, end: int) -> tuple[float, float] | None:
        """字符区间 [start, end) 的像素区间，越界一律夹紧。"""
        if not self.spans:
            return None
        start = max(0, min(start, len(self.spans)))
        end = max(0, min(end, len(self.spans)))
        if start >= end:
            return None
        if (start, end) == (0, len(self.spans)):
            return self.spans[0][0], self.spans[-1][1]
        if start + 1 == end:
            return self.spans[start]
        return self.spans[start][0], self.spans[end - 1][1]


class CanvasTable(tk.Canvas):
    """ttk.Treeview 的可替换替身：接口兼容，绘制换成自己画。

    只实现 gui.py 与测试真正用到的那部分接口。没实现的部分（tag_bind、
    tree 展开、stretch 的真实含义）刻意不做——用到时立刻 AttributeError，
    而不是静默画错。
    """

    HEAD_H = 30
    ROW_H = 34
    PAD_X = 8
    # 单元格里可以有换行（型号 \n 序列号、读 \n 写），行高要容得下最多那一列。
    # 上下各留一点，否则两行正好等于行高时贴边看着像被切断。
    ROW_PADDING = 3

    def __init__(self, master: Any, *, columns: Sequence[str],
                 selectmode: str = "extended", rowheight: int = ROW_H,
                 head_height: int = HEAD_H,
                 palette: dict[str, str | None] | None = None,
                 font_family: str = "Microsoft YaHei UI", font_size: int = 9,
                 **kwargs: Any) -> None:
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        self._palette = dict(DEFAULT_PALETTE)
        if palette:
            self._palette.update({k: v for k, v in palette.items() if k in PALETTE_KEYS})
        kwargs.setdefault("bg", self._palette["bg"] or "#ffffff")
        super().__init__(master, **kwargs)

        self._columns: list[str] = list(columns)
        self._headings: dict[str, str] = {key: key for key in self._columns}
        self._widths: dict[str, int] = {key: 100 for key in self._columns}
        self._anchors: dict[str, str] = {key: "w" for key in self._columns}
        self._heading_command: dict[str, Any] = {}
        self._tag_options: dict[str, dict[str, Any]] = {}
        self._rows: list[str] = []
        self._cells: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._selectmode = selectmode
        self._selection: list[str] = []
        self._sort_column = ""
        self._sort_reverse = False

        # _row_height 是"最小行高"：单元格有两行文字时这一行会长高，
        # 实际高度在 _ensure_layout() 里按内容算，存进 _row_tops/_row_heights。
        self._row_height = int(rowheight)
        self._row_tops: list[int] = []
        self._row_heights: list[int] = []
        self._content_height = 0
        self._layout_dirty = True
        self._head_height = int(head_height)
        self._offset_y = 0
        self._offset_x = 0
        self._yscroll_command: Any = None
        self._xscroll_command: Any = None
        self._char_runs: dict[RunKey, CharRun] = {}
        # 拖选区间：(起行, 起列, 起字符, 止行, 止列, 止字符)
        self._sel_range: tuple[int, int, int, int, int, int] | None = None
        # 两端各自落在格内第几行文字（一格两行时选区要能横跨）
        self._sel_lines: tuple[int, int] = (0, 0)
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._resize_column = ""
        self._resize_origin: tuple[int, int, int] = (0, 0, 0)
        self._pending_command: Any = None
        self._last_cell: tuple[int, int] = (-1, -1)
        # 壁纸底层：provider 返回 (PhotoImage, 宽, 高) 或 None。每次 redraw 都会
        # 先把整块画布清空，所以底层图必须跟着重画，不能只在建表时贴一次。
        self._background_item: int | None = None
        self.wallpaper_provider: Any = None
        # provider 给的图若是「整窗」的，这里填本控件在窗口里的左上角偏移（像素）。
        self.wallpaper_offset: tuple[int, int] = (0, 0)
        # 右键菜单的宿主回调：Canvas 自己的 <Button-3> 一定 return "break"，
        # 所以同一个控件上再 bind 一个 <Button-3> 永远不会被触发，只能由这里转出去。
        self.context_menu: Any = None

        self._font = tkfont.Font(family=font_family, size=font_size)
        self._head_font = tkfont.Font(family=font_family, size=font_size, weight="bold")

        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Button-3>", self._on_right_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<MouseWheel>", self._on_wheel)

    # ---------- Treeview 兼容接口 ----------
    def __getitem__(self, key: str) -> Any:
        """table["columns"] 必须能读：测试与 gui.py 都靠它拿列名。"""
        if key == "columns":
            return tuple(self._columns)
        if key == "displaycolumns":
            return tuple(self._columns)
        if key == "show":
            return "headings"
        if key == "selectmode":
            return self._selectmode
        raise KeyError(key)

    def cget(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError:
            return super().cget(key)

    def heading(self, column: str, **options: Any) -> Any:
        if "text" in options:
            self._headings[column] = str(options["text"])
            self.redraw()
        if "command" in options:
            self._heading_command[column] = options["command"]
        return {"text": self._headings.get(column, column)}

    def column(self, column: str, **options: Any) -> Any:
        changed = False
        if "width" in options:
            self._widths[column] = max(40, int(options["width"]))
            changed = True
        if "anchor" in options:
            self._anchors[column] = str(options["anchor"])
        if changed:
            self.redraw()
        return {"width": self._widths.get(column, 100),
                "anchor": self._anchors.get(column, "w"),
                "minwidth": 40, "stretch": column == self._columns[-1]}

    def tag_configure(self, tag: str, **options: Any) -> Any:
        if options:
            self._tag_options.setdefault(tag, {}).update(options)
            self.redraw()
        return dict(self._tag_options.get(tag, {}))

    def configure(self, **options: Any) -> Any:
        if "yscrollcommand" in options:
            self._yscroll_command = options.pop("yscrollcommand")
            self._notify_scroll()
        if "xscrollcommand" in options:
            self._xscroll_command = options.pop("xscrollcommand")
            self._notify_scroll()
        if "bg" in options:
            self._palette["bg"] = options["bg"]
        if options:
            super().configure(**options)
        return None

    config = configure

    def get_children(self, item: str | None = None) -> tuple[str, ...]:
        return tuple(self._rows) if item in (None, "") else ()

    def exists(self, iid: str) -> bool:
        return iid in self._cells

    def insert(self, parent: str, index: Any, *, iid: str,
               values: Sequence[Any] = (), tags: Sequence[str] = ()) -> str:
        del parent, index
        if iid in self._cells:
            raise ValueError('Item "%s" already exists' % iid)
        self._cells[iid] = (tuple(str(v) for v in values), tuple(tags))
        self._rows.append(iid)
        self._layout_dirty = True
        self.redraw()
        return iid

    def delete(self, *iids: str) -> None:
        for iid in iids:
            self._cells.pop(iid, None)
            if iid in self._rows:
                self._rows.remove(iid)
            if iid in self._selection:
                self._selection.remove(iid)
        self._layout_dirty = True
        self._offset_y = self._clamp_y(self._offset_y)
        self.redraw()

    def item(self, iid: str, option: str | None = None, **options: Any) -> Any:
        del options
        entry = self._cells.get(iid)
        if entry is None:
            return () if option in ("tags", "values") else ""
        values, tags = entry
        if option == "tags":
            return tags
        if option == "values":
            return values
        if option == "text":
            return values[0] if values else ""
        return {"values": values, "tags": tags}

    def selection(self) -> tuple[str, ...]:
        return tuple(self._selection)

    @staticmethod
    def _flatten_iids(iids: Sequence[Any]) -> list[str]:
        """把 selection_set(children) 与 selection_set(a, b) 两种写法都收成 id 列表。

        Treeview 的 selection_set(*items) 允许传单个 tuple（gui.select_all_rows 就是
        selection_set(get_children())），CanvasTable 若把 tuple 当成一个 id 就会全被
        过滤掉、全选变成没选。两种调用形状在这里统一展开。
        """
        flat: list[str] = []
        for item in iids:
            if isinstance(item, (list, tuple, set)):
                flat.extend(str(entry) for entry in item)
            else:
                flat.append(str(item))
        return flat

    def selection_set(self, *iids: Any) -> None:
        self._selection = [i for i in self._flatten_iids(iids) if i in self._cells]
        self._sel_range = None
        self.redraw()

    def selection_remove(self, *iids: Any) -> None:
        drop = set(self._flatten_iids(iids))
        self._selection = [i for i in self._selection if i not in drop]
        if not self._selection:
            self._sel_range = None
        self.redraw()

    def see(self, iid: str) -> None:
        if iid not in self._rows:
            return
        index = self._rows.index(iid)
        top = self._row_top(index)
        height = self._row_height_of(index)
        view_h = self._body_height()
        if top < self._offset_y:
            self._offset_y = top
        elif top + height > self._offset_y + view_h:
            self._offset_y = top + height - view_h
        self._offset_y = self._clamp_y(self._offset_y)
        self.redraw()

    def bbox(self, iid: str) -> tuple[int, int, int, int] | None:
        """行在屏幕上的矩形；滚出视野或不存在时返回 None（与 Treeview 一致）。"""
        if iid not in self._rows:
            return None
        index = self._rows.index(iid)
        height = self._row_height_of(index)
        top = self._row_top(index) - self._offset_y + self._head_height
        if top + height <= self._head_height:
            return None
        if top >= self._head_height + self._body_height():
            return None
        width = max(1, int(self.winfo_width()) - 2 * self.PAD_X)
        return self.PAD_X, top, width, height

    def identify_row(self, y: int) -> str:
        index = self._row_index_at(y)
        return self._rows[index] if 0 <= index < len(self._rows) else ""

    def identify_column(self, x: int) -> str:
        index = self._column_index_at(x)
        return "#%d" % (index + 1) if index >= 0 else ""

    def yview(self, *args: Any) -> tuple[float, float]:
        if args:
            self._apply_scroll("y", args)
        return self._y_fraction()

    def xview(self, *args: Any) -> tuple[float, float]:
        if args:
            self._apply_scroll("x", args)
        return self._x_fraction()

    # ---------- 外观 ----------
    def set_palette(self, palette: dict[str, str | None]) -> None:
        """换配色后重画；文字颜色也走调色板，不依赖 ttk 样式表。"""
        self._palette.update({k: v for k, v in palette.items() if k in PALETTE_KEYS})
        super().configure(bg=self._palette["bg"] or "#ffffff")
        self.redraw()

    def set_row_height(self, height: int) -> None:
        self._row_height = max(20, int(height))
        self._layout_dirty = True
        self.redraw()

    def set_fonts(self, family: str, size: int) -> None:
        self._font = tkfont.Font(family=family, size=size)
        self._head_font = tkfont.Font(family=family, size=size, weight="bold")
        self._layout_dirty = True
        self.redraw()

    def set_sort_indicator(self, column: str, reverse: bool) -> None:
        self._sort_column = column
        self._sort_reverse = reverse
        self.redraw()

    # ---------- 几何 ----------
    def _body_height(self) -> int:
        return max(1, int(self.winfo_height()) - self._head_height)

    def _total_width(self) -> int:
        return sum(self._widths.get(key, 100) for key in self._columns) + 2 * self.PAD_X

    def _column_left(self, index: int) -> float:
        left = self.PAD_X
        for key in self._columns[:index]:
            left += self._widths.get(key, 100)
        return left - self._offset_x

    def _column_index_at(self, x: float) -> int:
        cursor = self.PAD_X - self._offset_x
        for index, key in enumerate(self._columns):
            width = self._widths.get(key, 100)
            if cursor <= x < cursor + width:
                return index
            cursor += width
        return -1

    def _row_index_at(self, y: float) -> int:
        """y 落在第几行（从 0 数）；表头区或空白区返回 -1。

        行高不再统一（两行文字的单元格会把这一行撑高），所以只能按累计高度
        查表，不能再用 y // 行高 一步除法。
        """
        if y < self._head_height:
            return -1
        self._ensure_layout()
        probe = int((y - self._head_height) + self._offset_y)
        for index, top in enumerate(self._row_tops):
            if top <= probe < top + self._row_heights[index]:
                return index
        return -1

    def _clamp_y(self, value: int) -> int:
        self._ensure_layout()
        maximum = max(0, self._content_height - self._body_height())
        return max(0, min(int(value), maximum))

    def _clamp_x(self, value: int) -> int:
        maximum = max(0, self._total_width() - int(self.winfo_width()))
        return max(0, min(int(value), maximum))

    def _y_fraction(self) -> tuple[float, float]:
        self._ensure_layout()
        total = max(1, self._content_height)
        first = self._offset_y / total
        return first, min(1.0, first + self._body_height() / total)

    def _x_fraction(self) -> tuple[float, float]:
        total = max(1, self._total_width())
        first = self._offset_x / total
        return first, min(1.0, first + max(1, int(self.winfo_width())) / total)

    def _notify_scroll(self) -> None:
        """把滚动位置回报给细滚动条（与 Treeview 的 yscrollcommand 协议一致）。"""
        for command, fraction in ((self._yscroll_command, self._y_fraction()),
                                  (self._xscroll_command, self._x_fraction())):
            if command is None:
                continue
            try:
                command(*fraction)
            except Exception:
                pass

    def _apply_scroll(self, axis: str, args: Sequence[Any]) -> None:
        if not args:
            return
        if args[0] == "moveto":
            fraction = float(args[1])
            if axis == "y":
                self._ensure_layout()
                total = max(1, self._content_height)
                self._offset_y = self._clamp_y(int(fraction * total))
            else:
                self._offset_x = self._clamp_x(int(fraction * self._total_width()))
        elif args[0] == "scroll":
            amount = int(args[1])
            unit = str(args[2]) if len(args) > 2 else "units"
            if axis == "y":
                step = self._row_height if unit == "units" else self._body_height()
                self._offset_y = self._clamp_y(self._offset_y + amount * step)
            else:
                step = 40 if unit == "units" else max(1, int(self.winfo_width()))
                self._offset_x = self._clamp_x(self._offset_x + amount * step)
        self.redraw()

    # ---------- 行高布局 ----------
    def _cell_lines(self, iid: str, column_index: int) -> list[str]:
        values = self._cells.get(iid, ((), ()))[0]
        text = values[column_index] if column_index < len(values) else ""
        return CharRun.split_lines(text)

    def _row_text_height(self, iid: str) -> float:
        lines = 1
        for column_index in range(len(self._columns)):
            lines = max(lines, len(self._cell_lines(iid, column_index)))
        return lines * float(self._font.metrics("linespace"))

    def _ensure_layout(self) -> None:
        """算出每行的真实高度与纵向起点（像素，未扣滚动偏移）。

        多行单元格必须把行撑高，否则两行 9pt 文字（约 34px）正好等于行高，
        再有点行内留白就会压到下一行上——实机截图里型号和序列号糊成一团、
        还盖住下面的行，就是这么来的。
        """
        if not self._layout_dirty and len(self._row_tops) == len(self._rows):
            return
        min_height = max(20, int(self._row_height))
        heights: list[int] = []
        for iid in self._rows:
            need = self._row_text_height(iid) + 2 * self.ROW_PADDING
            heights.append(max(min_height, int(need + 0.5)))
        tops: list[int] = []
        cursor = 0
        for height in heights:
            tops.append(cursor)
            cursor += height
        self._row_heights = heights
        self._row_tops = tops
        self._content_height = cursor
        self._layout_dirty = False

    def _row_top(self, index: int) -> int:
        self._ensure_layout()
        if 0 <= index < len(self._row_tops):
            return self._row_tops[index]
        return self._content_height if index >= len(self._row_tops) else 0

    def _row_height_of(self, index: int) -> int:
        self._ensure_layout()
        if 0 <= index < len(self._row_heights):
            return self._row_heights[index]
        return max(20, int(self._row_height))

    # ---------- 绘制 ----------
    def _row_style(self, iid: str, index: int) -> tuple[str, str | None]:
        """一行的前景色与底色：tag 优先，其次斑马纹，都没有就 None（透出壁纸）。"""
        tags = self._cells.get(iid, ((), ()))[1]
        fg = self._palette["fg"]
        bg: str | None = None
        for tag in tags:
            options = self._tag_options.get(tag) or {}
            if options.get("foreground"):
                fg = str(options["foreground"])
            if options.get("background"):
                bg = str(options["background"])
        if bg is None and self._palette.get("zebra") and index % 2:
            bg = str(self._palette["zebra"])
        if bg is None and self._palette.get("bg"):
            bg = str(self._palette["bg"])
        return fg, bg

    def redraw(self) -> None:
        # 这里必须是 tk.Canvas.delete，不能用 self.delete("all")：
        # self.delete 是「按 iid 删行」的 Treeview 兼容方法，它自己又会调 redraw，
        # 一进来就无限递归（实测 RecursionError: maximum recursion depth exceeded）。
        tk.Canvas.delete(self, "all")
        self._char_runs.clear()
        width = int(self.winfo_width())
        height = int(self.winfo_height())
        if width <= 1 or height <= 1:
            return
        self._paint_background()
        # 先画行、后画表头：Tk 的 Canvas 没有裁剪区，滚动到半截的行会把
        # 上半截探进表头区域，压在表头文字上。后画表头就能把探出来的那部分盖住。
        self._draw_rows(width, height)
        self._draw_header(width)
        self._paint_selection()
        self._notify_scroll()

    def _paint_background(self) -> None:
        """贴壁纸底层。必须在清空之后、画表头行之前调用，靠创建顺序压在最后面。"""
        self._background_item = None
        provider = self.wallpaper_provider
        if provider is None:
            return
        try:
            layer = provider()
        except Exception:              # noqa: BLE001 - 壁纸出问题只该退化成纯色，不该崩窗口
            return
        if not layer:
            return
        photo = layer[0]
        # 整窗透图时这张图是「整个客户区」，表格只该露出自己那一块，
        # 于是按用户在窗口里的偏移把图往左上推。整套图只有一张，
        # 相邻控件贴的是同一张图的连续区域，接缝处像素天然对齐。
        offset_x, offset_y = getattr(self, "wallpaper_offset", (0, 0))
        try:
            self._background_item = self.create_image(
                -int(offset_x), -int(offset_y), image=photo, anchor="nw")
        except Exception:              # noqa: BLE001
            self._background_item = None
            return
        self.tag_lower(self._background_item)

    def _draw_header(self, width: int) -> None:
        head_bg = self._palette["head_bg"]
        if head_bg:
            self.create_rectangle(0, 0, width, self._head_height, fill=head_bg, outline="")
        for index, key in enumerate(self._columns):
            left = self._column_left(index)
            cell_width = self._widths.get(key, 100)
            if left + cell_width < 0 or left > width:
                continue
            arrow = ""
            if key == self._sort_column:
                arrow = " ▼" if self._sort_reverse else " ▲"
            arrow_width = self._head_font.measure(arrow) if arrow else 0
            label = fit_text(self._head_font, str(self._headings.get(key, key)),
                             cell_width - self.PAD_X * 2 - arrow_width)
            self.create_text(left + self.PAD_X, self._head_height // 2,
                             text=label + arrow, font=self._head_font,
                             fill=self._palette["head_fg"] or self._palette["fg"],
                             anchor="w")
            if index and self._palette["line"]:
                self.create_line(left, 6, left, self._head_height - 6,
                                 fill=self._palette["line"])
        if self._palette["line"]:
            self.create_line(0, self._head_height - 1, width, self._head_height - 1,
                             fill=self._palette["line"])

    def _draw_rows(self, width: int, height: int) -> None:
        if not self._rows:
            return
        self._ensure_layout()
        selected = set(self._selection)
        for index, iid in enumerate(self._rows):
            top = self._head_height + self._row_tops[index] - self._offset_y
            row_height = self._row_heights[index]
            if top > height:
                break
            if top + row_height < self._head_height:
                continue
            fg, bg = self._row_style(iid, index)
            if iid in selected:
                bg = self._palette["sel_bg"] or bg
                fg = self._palette["sel_fg"] or fg
            if bg:
                self.create_rectangle(0, top, width, top + row_height, fill=bg, outline="")
            for column_index, key in enumerate(self._columns):
                left = self._column_left(column_index)
                cell_width = self._widths.get(key, 100)
                if left + cell_width < 0 or left > width:
                    continue
                room = cell_width - self.PAD_X * 2
                if room <= 0:
                    continue
                lines = self._cell_lines(iid, column_index)
                self._draw_cell_text(index, column_index, left + self.PAD_X, top,
                                     row_height, lines, room, fg)

    def _draw_cell_text(self, index: int, column_index: int, left: float, top: float,
                        row_height: int, lines: Sequence[str], room: float, fg: str) -> None:
        """把一个单元格里的若干行文字画进这一行，整体纵向居中。

        每行单独一个 CharRun，键里带"格内第几行"，否则第二行会覆盖第一行，
        拖选只能选到后半截。y 传的是行顶：create_text 用的是 anchor="nw"，
        它的 y 就是文字包围盒上边，早先传基线导致整段往下坠一个上升高。
        """
        line_height = float(self._font.metrics("linespace"))
        visible = max(1, min(len(lines), MAX_CELL_LINES))
        block = visible * line_height
        y = top + (row_height - block) / 2.0
        for line_index, text in enumerate(lines[:visible]):
            y_line = y + line_index * line_height
            if not text:
                continue
            if self._font.measure(text) > room:
                text = fit_text(self._font, text, room)
            if not text:
                continue
            self._char_runs[(index, column_index, line_index)] = CharRun(
                self, self._font, fg, left, y_line, text)

    # ---------- 选区 ----------
    def _normalized_range(self) -> tuple[int, int, int, int, int, int] | None:
        """把拖选的首尾按「先按位置排序」整理成 (r0, c0, o0, r1, c1, o1)。

        排序顺手把 _sel_lines 也翻正，否则从第二行往上拖时，起点行号配的是
        终点的行列，整段选区会错位半格。
        """
        if self._sel_range is None:
            return None
        r0, c0, o0, r1, c1, o1 = self._sel_range
        l0, l1 = self._sel_lines
        if (r0, c0, l0, o0) <= (r1, c1, l1, o1):
            return r0, c0, o0, r1, c1, o1
        # 连 _sel_range 一起翻正，否则这个方法是「读一眼就改状态」的不幂等操作：
        # 第二次进来 _sel_lines 已经是翻过的，再按老顺序比一次又会翻回去，
        # 画选区（_paint_selection）和复制（selection_text）各调一次就会错位半格。
        self._sel_lines = (l1, l0)
        self._sel_range = (r1, c1, o1, r0, c0, o0)
        return self._sel_range

    def _cell_spans(self, row: int, column: int,
                    start: tuple[int, int] | None,
                    end: tuple[int, int] | None) -> list[tuple[CharRun, int, int]]:
        """一格内被选中的片段，按 (格内第几行, 起始字符) 收成 [(run, low, high)]。

        start/end 是 (格内第几行, 字符下标)，None 表示这一端不设限。一格两行
        文字时选区要能横跨两行：起点在第二行就从第二行中间开始，第一行整行算在内。
        """
        spans: list[tuple[CharRun, int, int]] = []
        for line_index in range(MAX_CELL_LINES):
            run = self._char_runs.get((row, column, line_index))
            if run is None:
                continue
            low = 0
            high = len(run.text)
            if start is not None:
                if line_index < start[0]:
                    continue
                if line_index == start[0]:
                    low = max(0, min(int(start[1]), high))
            if end is not None:
                if line_index > end[0]:
                    break
                if line_index == end[0]:
                    high = max(0, min(int(end[1]), high))
            if low < high:
                spans.append((run, low, high))
        return spans

    def _paint_selection(self) -> None:
        """把落在选区内的字符改成反白（逐字 item 能这样上色，整段 item 只能整段变）。"""
        span = self._normalized_range()
        if span is None:
            return
        r0, c0, o0, r1, c1, o1 = span
        l0, l1 = self._sel_lines
        if (r0, c0, l0, o0) == (r1, c1, l1, o1):
            return
        highlight = self._palette["sel_fg"] or "#ffffff"
        for row in range(r0, r1 + 1):
            for column in range(len(self._columns)):
                if row == r0 and column < c0:
                    continue
                if row == r1 and column > c1:
                    continue
                start = (l0, o0) if (row, column) == (r0, c0) else None
                end = (l1, o1) if (row, column) == (r1, c1) else None
                for run, low, high in self._cell_spans(row, column, start, end):
                    for item in run.items_for(low, high):
                        self.itemconfigure(item, fill=highlight)

    def selection_text(self) -> str:
        """把当前拖选导出成 TSV：跨列用制表符，跨行用换行。"""
        span = self._normalized_range()
        if span is None:
            return ""
        r0, c0, o0, r1, c1, o1 = span
        l0, l1 = self._sel_lines
        if (r0, c0, l0, o0) == (r1, c1, l1, o1):
            return ""
        lines: list[str] = []
        for row in range(r0, min(r1, len(self._rows) - 1) + 1):
            pieces: list[str] = []
            for column in range(len(self._columns)):
                if row == r0 and column < c0:
                    continue
                if row == r1 and column > c1:
                    continue
                start = (l0, o0) if (row, column) == (r0, c0) else None
                end = (l1, o1) if (row, column) == (r1, c1) else None
                cell = ""
                for run, low, high in self._cell_spans(row, column, start, end):
                    piece = run.text[low:high]
                    if piece:
                        cell += piece if not cell else " " + piece
                if cell:
                    pieces.append(cell)
            if pieces:
                lines.append("\t".join(pieces))
        return "\n".join(lines)

    def last_cell_text(self) -> str:
        """最近一次单击落到的单元格原文（右键「复制此单元格」用）。"""
        row, column = self._last_cell
        if row < 0 or row >= len(self._rows):
            return ""
        values = self._cells[self._rows[row]][0]
        return values[column] if 0 <= column < len(values) else ""

    # ---------- 鼠标与键盘 ----------
    def _hit_char(self, row: int, column: int, x: float, y: float | None = None) -> int:
        """鼠标 x 落在这一格的哪个字符上；纵向给得出 y 就先挑中的是哪一行。

        拖选以「格」为单位（不跨格选子串），所以只要取到一行的字符下标就够，
        这里按 y 挑离得最近的那行，免得点在第二行却按第一行的 x 算。
        """
        run = self._char_run_at(row, column, y)
        return 0 if run is None else run.hit(x)

    def _hit_cell(self, row: int, column: int, x: float,
                  y: float | None = None) -> tuple[int, int]:
        """鼠标落点 -> (格内第几行, 字符下标)。"""
        line = self._hit_line(row, column, y)
        return line, self._hit_char(row, column, x, y)

    def _hit_line(self, row: int, column: int, y: float | None = None) -> int:
        runs = [(line_index, self._char_runs[(row, column, line_index)])
                for line_index in range(MAX_CELL_LINES)
                if (row, column, line_index) in self._char_runs]
        if not runs:
            return 0
        if y is None or len(runs) == 1:
            return runs[0][0]
        return min(runs, key=lambda pair: abs((pair[1].top + pair[1].bottom) / 2.0
                                              - float(y)))[0]

    def _char_run_at(self, row: int, column: int, y: float | None = None) -> CharRun | None:
        line = self._hit_line(row, column, y)
        return self._char_runs.get((row, column, line))

    def _on_press(self, event: Any) -> str:
        # 键盘事件（Ctrl+C 等）只会派给有焦点的控件，Canvas 不会自己抢焦点，
        # 所以每次点进来都主动要一次，否则点完表格按 Ctrl+C 复制不动。
        try:
            self.focus_set()
        except Exception:          # noqa: BLE001 - 控件已销毁时忽略
            pass
        if event.y < self._head_height:
            self._on_header_press(event)
            return "break"
        index = self._row_index_at(event.y)
        if index < 0:
            self.selection_remove(*self._selection)
            return "break"
        column = self._column_index_at(event.x)
        line, offset = self._hit_cell(index, column, event.x, event.y)
        self._sel_lines = (line, line)
        self._sel_range = (index, column, offset, index, column, offset)
        self._drag_origin = (index, column, offset, line)
        self._last_cell = (index, column)

        iid = self._rows[index]
        state = int(getattr(event, "state", 0))
        if self._selectmode == "extended" and state & 0x0004:      # Ctrl
            chosen = list(self._selection)
            chosen.append(iid)
            self._selection = list(dict.fromkeys(chosen))
        elif self._selectmode == "extended" and state & 0x0001:    # Shift
            anchor = self._selection[-1] if self._selection else iid
            low = min(self._rows.index(anchor), index)
            high = max(self._rows.index(anchor), index)
            self._selection = self._rows[low:high + 1]
        else:
            self._selection = [iid]
        self.redraw()
        return "break"

    def _on_header_press(self, event: Any) -> None:
        """表头按下：记住列宽起点；松开时若几乎没移动，就当成排序点击。"""
        index = self._column_index_at(event.x)
        self._drag_origin = None
        if index < 0:
            return
        key = self._columns[index]
        self._resize_column = key
        self._resize_origin = (event.x, self._widths.get(key, 100), self._column_left(index))
        self._pending_command = self._heading_command.get(key)

    def _on_drag(self, event: Any) -> str:
        if self._resize_column:
            start_x, start_width, _left = self._resize_origin
            self._widths[self._resize_column] = max(40, start_width + int(event.x - start_x))
            self.redraw()
            return "break"
        if self._drag_origin is not None:
            index = self._row_index_at(event.y)
            if index < 0:
                index = len(self._rows) - 1 if event.y > self._head_height else 0
            column = self._column_index_at(event.x)
            line, offset = self._hit_cell(index, column, event.x, event.y)
            r0, c0, o0, l0 = self._drag_origin
            self._sel_range = (r0, c0, o0, index, column, offset)
            self._sel_lines = (l0, line)
            self._sync_selection_to_range()
            self.redraw()
        return "break"

    def _on_release(self, event: Any) -> str:
        if self._resize_column:
            key = self._resize_column
            self._resize_column = ""
            command = self._pending_command
            self._pending_command = None
            if command is not None and abs(event.x - self._resize_origin[0]) < 6:
                command()
            del key
            return "break"
        # 只点了一下没拖动：松开时清掉零宽选区，否则整格会被判成"已选"。
        # 比较必须带上「格内第几行」：同一格里从第一行拖到第二行时，行列和
        # 字符下标都可能一样，但那是一条有内容的跨行选区，不能当零宽清掉。
        if self._sel_range is not None and self._drag_origin is not None:
            r0, c0, o0, r1, c1, o1 = self._sel_range
            l0, l1 = self._sel_lines
            if (r0, c0, l0, o0) == (r1, c1, l1, o1):
                self._sel_range = None
                self._drag_origin = None
                self.redraw()
        self._drag_origin = None
        return "break"

    def _sync_selection_to_range(self) -> None:
        """拖选跨了多行时，行高亮也跟着走（复制的是文字，行色只是提示）。"""
        span = self._normalized_range()
        if span is None:
            return
        r0, _c0, _o0, r1, _c1, _o1 = span
        low = max(0, min(r0, r1))
        high = min(len(self._rows) - 1, max(r0, r1))
        if 0 <= low <= high:
            self._selection = self._rows[low:high + 1]

    def _on_right_click(self, event: Any) -> str:
        self._drag_origin = None
        index = self._row_index_at(event.y)
        if index < 0:
            return "break"
        self._last_cell = (index, self._column_index_at(event.x))
        iid = self._rows[index]
        if iid not in self._selection:
            self._selection = [iid]
            self._sel_range = None
            self.redraw()
        handler = self.context_menu
        if handler is not None:
            try:
                handler(event)
            except Exception:          # noqa: BLE001 - 菜单弹不出来不该影响选中状态
                pass
        return "break"

    def _on_motion(self, event: Any) -> None:
        self.configure(cursor="sb_h_double_arrow"
                       if event.y < self._head_height and self._column_index_at(event.x) >= 0
                       else "")

    def _on_wheel(self, event: Any) -> str:
        delta = int(getattr(event, "delta", 0) or 0)
        steps = int(-delta / 120) if delta else 0
        if steps:
            self._offset_y = self._clamp_y(self._offset_y + steps * WHEEL_STEP)
            self.redraw()
        return "break"

