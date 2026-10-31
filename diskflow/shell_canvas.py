"""第 7 套「图片背景」主题的整窗外壳层。

背景
----
ttk.Frame / ttk.Label 的底色是**实色**，没有任何 alpha 通道；只要外壳还是 ttk，
壁纸就只可能在数据区（本来就是 Canvas）透出来，顶栏、提示行、KPI、底栏永远是一块
白板。用户要的「整窗透图」在 Tk 里只有一条路：外壳自己画。

但把整窗重写成自绘布局引擎（自己算每个文字/按钮的绝对坐标）风险极高——gui 里
pack/grid 的层次关系、内边距、以及几十个测试断言都建立在容器语义上。所以这里
不重写布局，只把**容器与文字**换成 Canvas，并让每个 Canvas 画「自己那一块」：

    所有外壳 Canvas 共享同一张「整窗合成图」（壁纸 + 薄罩，见 wallpaper.compose_ppm），
    每个控件用 create_image(-x, -y) 把自己矩形内的那一块贴出来，x/y 是它相对
    窗口客户区的偏移。Canvas 会自动裁掉自己矩形外的部分，于是相邻控件贴的是
    同一张图的连续区域，接缝处像素完全对齐，看起来就是一整张壁纸。

这样 pack/grid 一行不用改，控件尺寸也照旧由 Tk 布局算，重排逻辑零成本。

容器语义
--------
tk.Canvas 本身就是合法容器（可以 pack/grid 子控件），所以 SliceCanvas 直接顶替
ttk.Frame；CanvasLabel 顶替 ttk.Label，对外保留 cget("text") / cget("style") /
configure(text=...) 这些 gui 和测试都在用的接口。

没有壁纸（其余 6 套主题、或图片主题还没选图）时，Canvas 就退回纯色底，
与非图片主题的观感一致。
"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import font as tkfont
from typing import Any, Callable

# 重排合成图的防抖：拖动窗口时 <Configure> 会连发几十次，每次重新解码整张壁纸
# 要 100ms 量级，不防抖会让缩放明显卡顿。只延后「重新合成」，
# 平移已经合成好的图（贴片）是纯坐标操作，可以立即做。
RECOMPOSE_DELAY_MS = 140


class ShellSurface:
    """整窗合成图的持有者与分发者。

    PhotoImage 必须有人持引用：交给 Canvas 之后局部变量一回收，图上就只剩背景色。
    所有 SliceCanvas 都通过这里取同一张图，保证接缝对齐。
    """

    def __init__(self, root: Any, provider: Callable[[int, int], Any] | None = None) -> None:
        self.root = root
        self.provider = provider
        self.width = 0
        self.height = 0
        self.photo: Any = None
        self._clients: list[Any] = []
        self._after_id: Any = None
        self._alive = True
        try:
            root.bind("<Configure>", self._on_root_configure, add="+")
            root.bind("<Destroy>", self._on_root_destroy, add="+")
        except Exception:       # pragma: no cover - 极老的 Tk
            pass

    # ---- 客户端登记 ----
    def register(self, widget: Any) -> None:
        if widget not in self._clients:
            self._clients.append(widget)

    def unregister(self, widget: Any) -> None:
        try:
            self._clients.remove(widget)
        except ValueError:
            pass

    # ---- 取图 ----
    def set_provider(self, provider: Callable[[int, int], Any] | None) -> None:
        """换合成函数（通常在建完控件、拿到全部几何信息之后才装上）。

        装上新函数后立即安排一次重合成：之前 provider 为 None，客户端拿不到图，
        只画了纯色底，不重合成的话整窗会一直停在纯色。
        """
        self.provider = provider
        if provider is not None:
            self._schedule()

    def image(self) -> Any:
        return self.photo

    def client_size(self) -> tuple[int, int]:
        """窗口客户区尺寸（不含标题栏与边框，与 winfo_rootx 同一坐标系）。"""
        try:
            return int(self.root.winfo_width()), int(self.root.winfo_height())
        except Exception:       # pragma: no cover - 控件已销毁
            return 0, 0

    def offset_of(self, widget: Any) -> tuple[int, int]:
        """控件左上角相对窗口客户区左上角的偏移。"""
        try:
            return (int(widget.winfo_rootx()) - int(self.root.winfo_rootx()),
                    int(widget.winfo_rooty()) - int(self.root.winfo_rooty()))
        except Exception:       # pragma: no cover
            return 0, 0

    # ---- 合成 ----
    def invalidate(self) -> None:
        """壁纸/透明度/主题变了：作废旧图，稍后重新合成。"""
        self.photo = None
        for widget in list(self._clients):
            try:
                widget.repaint()
            except Exception:
                pass
        self._schedule()

    def _schedule(self) -> None:
        if not self._alive or self.provider is None:
            return
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
        try:
            self._after_id = self.root.after(RECOMPOSE_DELAY_MS, self._recompose)
        except Exception:       # pragma: no cover
            self._after_id = None

    def _from_root(self, event: Any) -> bool:
        """事件是不是根窗口自己发的。

        Tk 的 bindtags 里第三个标签是**顶层窗口名**，所以 bind(root, ...) 绑到的
        回调会被**每一个子控件**的同类事件触发。不判这一下：任何一个子控件被销毁
        （切主题重建表格、pack_forget 掉反馈行）都会把整个合成层判死，壁纸当场消失。
        """
        widget = getattr(event, "widget", None)
        if widget is None:
            return True
        return str(widget) == str(self.root)

    def _on_root_configure(self, event: Any = None) -> None:
        if not self._from_root(event):
            return
        width, height = self.client_size()
        if width <= 4 or height <= 4:
            return
        if (width, height) != (self.width, self.height):
            self.width, self.height = width, height
            self._schedule()
        else:
            self._repaint_all()

    def _on_root_destroy(self, event: Any = None) -> None:
        if not self._from_root(event):
            return
        self._alive = False
        self._clients = []
        self.photo = None
        pending = self._after_id
        self._after_id = None
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except Exception:       # pragma: no cover
                pass

    def _recompose(self) -> None:
        self._after_id = None
        if not self._alive or self.provider is None:
            return
        width, height = self.client_size()
        if width <= 4 or height <= 4:
            return
        self.width, self.height = width, height
        try:
            photo = self.provider(width, height)
        except Exception:
            photo = None
        self.photo = photo
        self._repaint_all()

    def _repaint_all(self) -> None:
        for widget in list(self._clients):
            try:
                widget.repaint()
            except Exception:
                pass


class SliceCanvas(tk.Canvas):
    """以「整窗合成图的一小块」为底的外壳画布。

    没有壁纸时就是一块纯色 Canvas（等价于原来的 ttk.Frame），有壁纸时贴自己的切片。
    """

    def __init__(self, master: Any, surface: ShellSurface | None = None, *,
                 background: str = "#FFFFFF", **kwargs: Any) -> None:
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("takefocus", 0)
        super().__init__(master, bg=background, **kwargs)
        self.shell_surface = surface
        self._flat_bg = background
        self._image_item: Any = None
        self._registered = False
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Map>", self._on_configure, add="+")
        if surface is not None:
            surface.register(self)
            self._registered = True

    # ---- 外观 ----
    def set_background(self, color: str) -> None:
        """纯色底（没有壁纸时可见；有壁纸时只在合成完成前闪一下）。"""
        self._flat_bg = str(color)
        try:
            self.configure(bg=self._flat_bg)
        except Exception:       # pragma: no cover
            pass

    def set_surface(self, surface: ShellSurface | None) -> None:
        if self.shell_surface is not None and self._registered:
            self.shell_surface.unregister(self)
            self._registered = False
        self.shell_surface = surface
        if surface is not None:
            surface.register(self)
            self._registered = True
        self.repaint()

    def _on_configure(self, _event: Any = None) -> None:
        self.repaint()

    def repaint(self) -> None:
        surface = self.shell_surface
        photo = surface.image() if surface is not None else None
        if photo is None:
            if self._image_item is not None:
                try:
                    self.delete(self._image_item)
                except Exception:
                    pass
                self._image_item = None
            try:
                self.configure(bg=self._flat_bg)
            except Exception:   # pragma: no cover
                pass
            return
        offset_x, offset_y = surface.offset_of(self)
        try:
            if self._image_item is None:
                self._image_item = self.create_image(-offset_x, -offset_y,
                                                     anchor="nw", image=photo)
                self.tag_lower(self._image_item)
            else:
                self.coords(self._image_item, -offset_x, -offset_y)
                self.itemconfigure(self._image_item, image=photo)
                self.tag_lower(self._image_item)
        except Exception:       # pragma: no cover - 控件已销毁
            self._image_item = None

    def destroy(self) -> None:      # pragma: no cover - 由 Tk 生命周期覆盖
        if self.shell_surface is not None and self._registered:
            self.shell_surface.unregister(self)
            self._registered = False
        super().destroy()


class CanvasLabel(SliceCanvas):
    """顶替 ttk.Label 的外壳文字控件。

    对外接口只保留 gui 与测试实际用到的部分：
      configure(text=/style=/foreground=/font=)、cget("text"/"style")、anchor、
      pack/grid/pack_forget（继承自 Canvas）。
    文字画成 canvas text item，居中贴在自己矩形里的左端（anchor="w"）。
    """

    def __init__(self, master: Any, surface: ShellSurface | None = None, *,
                 text: str = "", style: str = "App.TLabel", anchor: str = "w",
                 font: Any = None, foreground: str = "#1A1A1A",
                 padx: int = 0, pady: int = 0, background: str = "#FFFFFF",
                 **kwargs: Any) -> None:
        super().__init__(master, surface, background=background, **kwargs)
        self._text = str(text)
        self._style = str(style)
        self._anchor = anchor
        self._font_spec = font
        self._foreground = str(foreground)
        self._pad_x = int(padx)
        self._pad_y = int(pady)
        self._text_item = self.create_text(self._pad_x, 0, anchor="w", text=self._text,
                                           fill=self._foreground)
        self._apply_font()
        self._fit()

    # ---- 兼容 ttk.Label 的接口 ----
    def configure(self, **kwargs: Any) -> Any:      # type: ignore[override]
        handled = False
        if "text" in kwargs:
            self._text = str(kwargs.pop("text"))
            self.itemconfigure(self._text_item, text=self._text)
            handled = True
        if "style" in kwargs:
            self._style = str(kwargs.pop("style"))
            handled = True
        if "anchor" in kwargs:
            self._anchor = kwargs.pop("anchor")
            handled = True
        if "foreground" in kwargs:
            self._foreground = str(kwargs.pop("foreground"))
            self.itemconfigure(self._text_item, fill=self._foreground)
            handled = True
        if "font" in kwargs:
            self._font_spec = kwargs.pop("font")
            self._apply_font()
            handled = True
        if "padding" in kwargs:
            padding = kwargs.pop("padding")
            value = padding[0] if isinstance(padding, (tuple, list)) else padding
            self._pad_x = self._pad_y = int(value)
            handled = True
        if handled:
            self._fit()
        if kwargs:
            super().configure(**kwargs)
        return None

    config = configure

    def cget(self, key: str) -> Any:                # type: ignore[override]
        if key == "text":
            return self._text
        if key == "style":
            return self._style
        if key == "anchor":
            return self._anchor
        return super().cget(key)

    def __getitem__(self, key: str) -> Any:
        return self.cget(key)

    # ---- 外观 ----
    def apply_palette(self, *, foreground: str, font: Any, background: str,
                      padx: int = 0, pady: int = 0) -> None:
        self._foreground = str(foreground)
        self._font_spec = font
        self._pad_x, self._pad_y = int(padx), int(pady)
        self.set_background(background)
        self.itemconfigure(self._text_item, fill=self._foreground)
        self._apply_font()
        self._fit()
        self.repaint()

    def set_text(self, text: str) -> None:
        self._text = str(text)
        self.itemconfigure(self._text_item, text=self._text)
        self._fit()

    def _apply_font(self) -> None:
        spec = self._font_spec
        if spec is None:
            self._font = tkfont.nametofont("TkDefaultFont")
        elif isinstance(spec, str):
            self._font = tkfont.nametofont(spec)
        else:
            self._font = tkfont.Font(font=spec)
        self.itemconfigure(self._text_item, font=self._font)

    def _text_width(self) -> int:
        try:
            return int(self._font.measure(self._text or ""))
        except Exception:       # pragma: no cover
            return 0

    def _line_height(self) -> int:
        try:
            return int(self._font.metrics("linespace"))
        except Exception:       # pragma: no cover
            return 16

    def _fit(self) -> None:
        """按文字宽度定宽、按行高定高，pack 时才能和 ttk.Label 一样只占文字大小。"""
        width = self._text_width() + self._pad_x * 2
        height = self._line_height() + self._pad_y * 2
        try:
            super().configure(width=width, height=height)
        except Exception:       # pragma: no cover
            pass
        try:
            self.coords(self._text_item, self._pad_x, height / 2.0)
        except Exception:       # pragma: no cover
            pass


class CardCanvas(SliceCanvas):
    """充当「卡片底座」的切片画布：比 SliceCanvas 多一层可选描边。

    KPI 指标卡原来用 ttk.Frame(style="Card.TFrame")：底色是实色 card_bg，
    在图片背景主题里就是四块白板，壁纸在中间断成四块。换成 Canvas 之后卡片自己
    也去合成图里取自己那块，四张卡与周围留白连成一片。

    唯一不能白丢的是 Card.TFrame 的描边（清爽仪表主题 card_border 非空），
    所以在切片上面再画一圈 1px 矩形。描边画在画布 item 层，卡片里的文字是
    独立子控件（子窗口永远盖在父画布的 item 之上），不会被描边压住。
    """

    def __init__(self, master: Any, surface: "ShellSurface" | None = None, *,
                 background: str = "#FFFFFF", border: str | None = None,
                 **kwargs: Any) -> None:
        # 必须在 super().__init__ 之前设好：SliceCanvas 会 bind <Configure>，
        # 而 repaint() 里要用到这两个字段。
        self._border = border
        self._border_item: Any = None
        super().__init__(master, surface, background=background, **kwargs)

    def set_colors(self, *, background: str, border: str | None = None) -> None:
        """换配色（切主题时调）：纯色底 + 描边色一起换，并立刻重画一次。"""
        self._border = border
        self.set_background(background)
        self.repaint()

    def repaint(self) -> None:
        super().repaint()
        self._paint_border()

    def _paint_border(self) -> None:
        if self._border_item is not None:
            try:
                self.delete(self._border_item)
            except Exception:       # pragma: no cover - 控件已销毁
                pass
            self._border_item = None
        if not self._border:
            return
        try:
            width = int(self.winfo_width())
            height = int(self.winfo_height())
        except Exception:           # pragma: no cover
            return
        if width <= 1 or height <= 1:
            # 还没映射出来，等 <Configure> 再来一次
            return
        try:
            self._border_item = self.create_rectangle(0, 0, width - 1, height - 1,
                                                      outline=self._border, width=1)
        except Exception:           # pragma: no cover - 控件已销毁
            self._border_item = None


def attach_backdrop(container: Any, surface: "ShellSurface" | None, *,
                    background: str = "#FFFFFF") -> Any:
    """在 container 里铺一层「背景切片」Canvas，返回这块 Canvas。

    为什么不把容器本身换成 Canvas：ttk.Frame 会把子控件的请求尺寸向上传播，
    而 tk.Canvas 的请求尺寸由它自己的 width/height 选项决定（默认 10c×7c），
    换成 Canvas 后这些"高度由内容决定"的横条会整条塌掉。所以容器一律不动，
    只在它内部 place 一块铺满的 Canvas 并压到最底层：place 不参与 pack/grid
    的几何协商，容器的管理方式、内边距、winfo_class 全部原样保留。

    调用时机必须早于容器里其它子控件的创建（Tk 后创建的控件默认在上层），
    这里仍然显式压到最底层一次，防止调用方顺序写反。

    注意压底层**不能**写 canvas.lower()：tkinter.Canvas 把 Misc.lower 覆盖成了
    tag_lower 的别名（"把画布里的某个 item 压到另一个 item 下面"），无参调用会抛
    TclError: wrong # args: should be "... lower tagOrId ?belowThis?"。
    要把控件本身压到底层，只能走 Tcl 原语 canvas.tk.call("lower", canvas._w)。
    """
    if surface is None:
        return None
    canvas = SliceCanvas(container, surface, background=background)
    try:
        inset_x, inset_y = container_inset(container)
        if inset_x or inset_y:
            # ttk 的 padding 圈是容器自己画的实色底，place 默认只能铺到内容区以内
            # （bordermode="inside"，实测 relwidth=1 拿到的是内核 padding 之后的宽度）。
            # 于是窗口最外圈会留一条 win_bg 的纯色白边，而参照图那圈是壁纸薄罩的延续，
            # 观感上就是「整窗透图漏了一圈」。把切片按负偏移顶到容器原点、尺寸按容器
            # 外框给，正好盖住 padding；超出容器的部分本来就会被父窗口裁掉。
            canvas.place(x=-inset_x, y=-inset_y, width=1, height=1)
            _cover_container(canvas, container)
        else:
            canvas.place(x=0, y=0, relwidth=1, relheight=1)
        send_to_back(canvas)
    except Exception:           # pragma: no cover - 控件已销毁
        return None
    return canvas


def container_inset(container: Any) -> tuple[int, int]:
    """容器 ttk padding 的左/上像素数；没有 padding（或不是 ttk）时返回 (0, 0)。"""
    try:
        padding = container.cget("padding")
    except Exception:       # pragma: no cover - tk.Frame 没有 padding 选项
        return (0, 0)
    values = pixel_values(padding)
    if not values:
        return (0, 0)
    left = values[0]
    top = values[1] if len(values) > 1 else left
    return (max(0, left), max(0, top))


def pixel_values(value: Any) -> list[int]:
    """把 Tk 的像素值统一成 int 列表。

    同一个 `cget("padding")` 在不同 Tk 版本上可能返回 4、`"0 0 10 0"`、
    `(<pixel object: '0'>, ..., <pixel object: '10'>)` 或 `"0.0"`，
    解析失败一律当空列表（当作没有内边距），不能让异常冒到布局里。
    """
    if isinstance(value, (tuple, list)):
        items: list[Any] = list(value)
    else:
        text = str(value).replace("{", "").replace("}", "").strip()
        if not text:
            return []
        if "pixel object" in text:
            items = re.findall(r"'(-?\d+)'", text)
        else:
            items = text.split()
    out: list[int] = []
    for item in items:
        try:
            out.append(int(str(item)))
        except (TypeError, ValueError):
            try:
                out.append(int(float(str(item))))
            except (TypeError, ValueError):
                return []
    return out


def _cover_container(canvas: Any, container: Any) -> None:
    """让切片始终按容器外框（而非内容区）的尺寸铺满，并跟着容器一起伸缩。"""
    def resize(_event: Any = None) -> None:
        try:
            width = int(container.winfo_width())
            height = int(container.winfo_height())
            if width <= 1 or height <= 1:
                return
            canvas.place_configure(width=width, height=height)
        except Exception:       # pragma: no cover - 控件已销毁
            pass

    resize()
    try:
        container.bind("<Configure>", resize, add="+")
    except Exception:           # pragma: no cover
        pass


def send_to_back(widget: Any) -> None:
    """把**控件本身**压到同级控件的最底层。

    tkinter 的 Misc 基类本来有 lower()（即 tkraise/lower 那对），但 Canvas、
    Text 等可滚动画布类会把它覆盖成 tag_lower 的别名，于是 widget.lower()
    在没有 tagOrId 参数时直接抛 TclError。这里统一走 Tcl 原语绕开这个坑。
    """
    widget.tk.call("lower", widget._w)


def descendant_children(widget: Any) -> list[Any]:
    """递归取全部子控件（测试与布局自检用）。"""
    out: list[Any] = []
    try:
        children = widget.winfo_children()
    except Exception:           # pragma: no cover
        return out
    for child in children:
        out.append(child)
        out.extend(descendant_children(child))
    return out