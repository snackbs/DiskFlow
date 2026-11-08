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
from typing import Any, Callable, Sequence

from .pngenc import VeilImage, capsule_rgba_png, solid_rgba_png  # noqa: F401  两个 PNG 编码器对外再导出

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
                 background: str = "#FFFFFF",
                 veil_rgb: Sequence[int] | None = None,
                 veil_alpha: float = 0.0, **kwargs: Any) -> None:
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("takefocus", 0)
        super().__init__(master, bg=background, **kwargs)
        self.shell_surface = surface
        self._flat_bg = background
        self._show_flat_bg = True
        self._image_item: Any = None
        self._veil_rgb: tuple[int, int, int] = (
            (int(veil_rgb[0]) & 0xFF, int(veil_rgb[1]) & 0xFF, int(veil_rgb[2]) & 0xFF)
            if veil_rgb is not None else (255, 255, 255))
        self._veil_alpha = max(0.0, min(1.0, float(veil_alpha)))
        self._veil_item: Any = None
        # 薄罩图缓存（P2-7：与 PhotoCard 共用同一份实现，不再各写一套）
        self._veil_images = VeilImage(self)
        self._registered = False
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Map>", self._on_configure, add="+")
        self._parent_repaint_id: Any = None
        try:
            self.master.bind("<Configure>", self._on_parent_configure, add="+")
        except Exception:          # pragma: no cover - 容器尚未建立
            pass
        if surface is not None:
            surface.register(self)
            self._registered = True

    # ---- 外观 ----
    def set_background(self, color: str) -> None:
        """纯色底（没有壁纸时可见；有壁纸时只在合成完成前闪一下）。"""
        self._flat_bg = str(color)
        self._apply_flat_bg()

    def _apply_flat_bg(self) -> None:
        """把纯色底落到 Tk 的 -background 上，**值没变就一个字节都不发**。

        这条「跳过同色 configure」是布局自激振荡的解药，不是微优化。
        Canvas 收到 -background 的改动时走的是 ConfigureCanvas 分支，它会用自己
        的 -width/-height 选项（默认 10c×7c = 378×265）回调 Tk_GeometryRequest；
        pack 管理器随后又用子控件 bbox 回调一次 Tk_GeometryRequest。两次调用取值
        不同（64 vs 265），谁最后调用谁说了算 —— 于是父容器的请求尺寸在 64 与
        265 之间来回摆，装在它上面的数据区被反复挤到 60 高，布局一变又触发
        重新合成，重新合成再 repaint 再 configure……实测 6 秒 89 个样本摆 9 次。

        跳过同色之后 Tk 根本不进 ConfigureCanvas，几何请求只剩 pack 那条路径，
        振荡消失（实测 168 个样本只剩 3 个状态，且都是启动瞬态）。
        """
        if not self._show_flat_bg:
            return
        try:
            if str(self.cget("bg")) == str(self._flat_bg):
                return
        except Exception:       # pragma: no cover - 控件已销毁
            return
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

    def _on_parent_configure(self, _event: Any = None) -> None:
        """父容器被布局挪动时，画布自己收不到 <Configure>，切片会停在旧坐标。

        典型场景：KPI 卡出现/消失、切换视图会把整块卡片区从 y=405 挪到
        y=140，而画布在父容器里的位置与尺寸都没变 → 一个事件也收不到 →
        切片只盖住上半截，下半截露出平色底（用户报的「卡片下方白带」）。
        这里跟着父容器重画，并延后到空闲期，避开布局尚未收敛的过渡态。
        """
        if self._parent_repaint_id is not None:
            return
        try:
            self._parent_repaint_id = self.after_idle(self._run_deferred_repaint)
        except Exception:          # pragma: no cover - 控件已销毁
            self._parent_repaint_id = None

    def _run_deferred_repaint(self) -> None:
        self._parent_repaint_id = None
        try:
            if not self.winfo_exists():
                return
        except Exception:          # pragma: no cover - 控件已销毁
            return
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
            self._drop_veil_item()
            self._apply_flat_bg()
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
        self._paint_veil()

    # ---- 薄罩：半透明白色图片，压在切片之上 ----
    def set_veil(self, *, rgb: Sequence[int] | None = None,
                 alpha: float | None = None) -> None:
        """设置薄罩颜色与不透明度（alpha=0 即不铺）。"""
        if rgb is not None:
            self._veil_rgb = (int(rgb[0]) & 0xFF, int(rgb[1]) & 0xFF,
                              int(rgb[2]) & 0xFF)
        if alpha is not None:
            self._veil_alpha = max(0.0, min(1.0, float(alpha)))
        self.repaint()

    def _drop_veil_item(self) -> None:
        if self._veil_item is not None:
            try:
                self.delete(self._veil_item)
            except Exception:       # pragma: no cover - 控件已销毁
                pass
            self._veil_item = None

    def _paint_veil(self) -> None:
        """把薄罩贴成一张 RGBA 图片，压在切片之上、其余图元之下。

        为什么要贴图片而不是画矩形：canvas 的 rectangle 只支持不透明色，
        半透明只能用 RGBA 图片（Tk 8.6 会做真正的 alpha 合成）。
        图片本身由 pngenc.VeilImage 按尺寸+颜色缓存，尺寸没变时不重新编码。
        """
        if int(round(self._veil_alpha * 255)) <= 0:
            self._drop_veil_item()
            return
        try:
            width = int(self.winfo_width())
            height = int(self.winfo_height())
        except Exception:           # pragma: no cover
            return
        if width <= 1 or height <= 1:
            return                  # 还没映射出来，等 <Configure> 再来一次
        if (self._veil_item is not None
                and self._veil_images.matches(width, height, self._veil_rgb,
                                              self._veil_alpha)):
            return                  # 尺寸与颜色都没变，复用现有 item
        try:
            photo = self._veil_images.image(width, height, self._veil_rgb,
                                            self._veil_alpha)
        except Exception:           # pragma: no cover - 控件已销毁
            self._drop_veil_item()
            return
        if photo is None:
            self._drop_veil_item()
            return
        self._drop_veil_item()
        try:
            self._veil_item = self.create_image(0, 0, anchor="nw", image=photo)
            # 压在切片之上、文字之下：文字 item 先建、薄罩后建会盖住文字，
            # 所以显式提到切片正上方一档。
            if self._image_item is not None:
                self.tag_raise(self._veil_item, self._image_item)
        except Exception:           # pragma: no cover - 控件已销毁
            self._veil_item = None

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
                 veil_rgb: Sequence[int] | None = None,
                 veil_alpha: float = 0.0, **kwargs: Any) -> None:
        super().__init__(master, surface, background=background,
                         veil_rgb=veil_rgb, veil_alpha=veil_alpha, **kwargs)
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
                      padx: int = 0, pady: int = 0,
                      veil_rgb: Sequence[int] | None = None,
                      veil_alpha: float | None = None) -> None:
        self._foreground = str(foreground)
        self._font_spec = font
        self._pad_x, self._pad_y = int(padx), int(pady)
        if veil_rgb is not None:
            self._veil_rgb = (int(veil_rgb[0]) & 0xFF, int(veil_rgb[1]) & 0xFF,
                              int(veil_rgb[2]) & 0xFF)
        if veil_alpha is not None:
            self._veil_alpha = max(0.0, min(1.0, float(veil_alpha)))
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
    """充当「卡片底座」的切片画布：切片 + 半透明薄罩 + 可选描边。

    KPI 指标卡原来用 ttk.Frame(style="Card.TFrame")：底色是实色 card_bg，
    在图片背景主题里就是四块白板，壁纸在中间断成四块。换成 Canvas 之后卡片自己
    也去合成图里取自己那块，四张卡与周围留白连成一片。

    薄罩不是装饰，是让卡片「分得开」的唯一手段：
        参照效果图里 KPI 卡面亮度 178、卡间缝隙 150。缝隙漏的是合成图的
        「对话层」（α0.55 白罩压壁纸，量出来 150）。如果卡片只贴切片不叠罩，
        卡面也是 150 → 四张卡糊成一整条（实测卡面 147 / 缝隙 146，差 1）。
        再叠一层 27% 白，0.73×150 + 0.27×255 = 178.4，与参照图逐位吻合。
        所以卡面与缝隙的 28 个亮度差**全部来自这层罩**，不是画出来的分隔线。

    薄罩与描边都在基类 SliceCanvas 里实现（CanvasLabel 也复用薄罩，
    否则卡里的字会在卡片上留下几块实色白条）。
    """

    def __init__(self, master: Any, surface: "ShellSurface" | None = None, *,
                 background: str = "#FFFFFF", border: str | None = None,
                 veil_rgb: Sequence[int] | None = None,
                 veil_alpha: float = 0.0, **kwargs: Any) -> None:
        # 必须在 super().__init__ 之前设好：SliceCanvas 会 bind <Configure>，
        # 而 repaint() 里要用到这些字段。
        self._border = border
        self._border_item: Any = None
        if veil_rgb is not None:
            kwargs["veil_rgb"] = veil_rgb
        kwargs["veil_alpha"] = veil_alpha
        super().__init__(master, surface, background=background, **kwargs)

    def set_colors(self, *, background: str, border: str | None = None,
                   veil_rgb: Sequence[int] | None = None,
                   veil_alpha: float | None = None) -> None:
        """换配色（切主题时调）：纯色底 + 描边 + 薄罩一起换，只重画一次。

        薄罩也走这里而不是单独调 set_veil()，否则一次切主题要重绘两遍，
        卡片多的时候能看到明显的逐张闪烁。
        """
        self._border = border
        if veil_rgb is not None:
            self._veil_rgb = (int(veil_rgb[0]) & 0xFF, int(veil_rgb[1]) & 0xFF,
                              int(veil_rgb[2]) & 0xFF)
        if veil_alpha is not None:
            self._veil_alpha = max(0.0, min(1.0, float(veil_alpha)))
        self.set_background(background)
        self.repaint()

    def repaint(self) -> None:
        super().repaint()       # 切片 + 薄罩
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



class QuietButton(SliceCanvas):
    """底部「关于」按钮——自绘的描边次级按钮。

    原来用 ttk.Button(style="Quiet.TButton")：clam 版本的按钮底色由 style 的
    background 决定，在图片背景主题里被设成 win_bg 的实色，于是壁纸透不过去，
    屏幕上就是一块 50x31 的浅灰白块（截图实测那块区域是纯 #EEF2FA、stdev 0）。
    ttk 按钮没有「透明底色」这个选项，所以和顶栏的分段按钮一样改成画布：
    有切片时先贴自己那块壁纸，再画一条 1px 描边，最后写文字。

    对外保留原 ttk.Button 调用点用到的全部接口：
      cget("text"/"style")、configure(text=/style=/state=/command=)、
      winfo_reqwidth()（按文字 + 内边距算）、pack(side=/padx=)、invoke()。
    "style" 只是存下来的名字（"Quiet.TButton" / "TButton"），供测试与切主题时
    判断该用哪套外观；真正的配色由 apply_palette 给。
    """

    STYLE_QUIET = "Quiet.TButton"
    STYLE_NATIVE = "TButton"

    def __init__(self, master: Any, surface: "ShellSurface" | None = None, *,
                 text: str = "", style: str = "Quiet.TButton", command: Any = None,
                 background: str = "#FFFFFF", padx: int = 10, pady: int = 4,
                 font: Any = None, **kwargs: Any) -> None:
        # 这两个字段必须在 super().__init__ 之前设好：SliceCanvas 会 bind <Configure>，
        # repaint() 立刻就可能被调用。
        self._border_item: Any = None
        self._bg_item: Any = None
        self._text_item: Any = None
        super().__init__(master, surface, background=background, **kwargs)
        self._text = str(text)
        self._style = str(style)
        self.command = command
        self._state = "normal"
        self._hover = False
        self._pad_x = int(padx)
        self._pad_y = int(pady)
        self._font_spec = font
        self._font = None
        self._fg = "#1A1A1A"
        self._border = "#B9C6D6"
        self._fill = str(background)
        self._hover_fill: str | None = None
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._apply_font()
        self._fit()
        self.repaint()

    # ---- 尺寸 ----
    def _apply_font(self) -> None:
        spec = self._font_spec
        if spec is None:
            self._font = tkfont.nametofont("TkDefaultFont")
        elif isinstance(spec, str):
            self._font = tkfont.nametofont(spec)
        else:
            self._font = tkfont.Font(font=spec)

    def _fit(self) -> None:
        """按文字宽度 + 内边距定宽、按行高定高。

        width=0 的语义原样保留：ttk 的 width 按字符数算、默认 -11 会把两个字的
        「关于」拉成和「刷新记录」一样宽，这里一律按文字实际宽度排。
        """
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

    def winfo_reqwidth(self) -> int:        # type: ignore[override]
        """按文字算的请求宽度（测试用它断言「关于」比「刷新记录」窄）。"""
        try:
            return int(self._font.measure(self._text or "")) + self._pad_x * 2
        except Exception:                   # pragma: no cover
            return super().winfo_reqwidth()

    # ---- 兼容 ttk.Button 的接口 ----
    def cget(self, key: str) -> Any:        # type: ignore[override]
        if key == "text":
            return self._text
        if key == "style":
            return self._style
        if key == "state":
            return self._state
        if key == "command":
            return self.command
        if key == "background" or key == "bg":
            return self._fill
        if key == "foreground" or key == "fg":
            return self._fg
        return super().cget(key)

    def configure(self, **kwargs: Any) -> Any:      # type: ignore[override]
        repaint = False
        if "text" in kwargs:
            self._text = str(kwargs.pop("text"))
            repaint = True
        if "style" in kwargs:
            self._style = str(kwargs.pop("style"))
            repaint = True
        if "command" in kwargs:
            self.command = kwargs.pop("command")
        if "state" in kwargs:
            self._state = str(kwargs.pop("state"))
            repaint = True
        if "font" in kwargs:
            self._font_spec = kwargs.pop("font")
            self._apply_font()
            repaint = True
        if repaint:
            self._fit()
            self.repaint()
        if kwargs:
            super().configure(**kwargs)
        return None

    config = configure

    # ---- 外观 ----
    def apply_palette(self, *, foreground: str, background: str, border: str,
                      font: Any, hover_fill: str | None = None,
                      hover_foreground: str | None = None) -> None:
        """切主题时调：描边 / 文字 / 悬停色一起换，不重建控件。"""
        self._fg = str(foreground)
        self._fill = str(background)
        self._border = str(border)
        self._hover_fill = str(hover_fill) if hover_fill else None
        if hover_foreground:
            self._hover_fg = str(hover_foreground)
        self._font_spec = font
        self.set_background(self._fill)
        self._apply_font()
        self._fit()
        self.repaint()

    def _enabled(self) -> bool:
        return self._state != "disabled"

    def _line_color(self) -> str:
        return self._border if self._enabled() else self._fill

    def _text_color(self) -> str:
        if not self._enabled():
            return self._border
        if self._hover and getattr(self, "_hover_fg", None):
            return self._hover_fg
        return self._fg

    def repaint(self) -> None:
        """重画：壁纸切片 → 悬停底色（仅悬停时）→ 描边 → 文字。"""
        try:
            width = int(self.winfo_width())
            height = int(self.winfo_height())
        except Exception:                   # pragma: no cover
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
            except Exception:               # pragma: no cover
                photo = None
        # 1) 底：有切片就贴切片（悬停时才盖一层实色），没有切片就是纯色底
        if photo is not None and not self._hover:
            if self._bg_item is not None:
                try:
                    self.delete(self._bg_item)
                except Exception:           # pragma: no cover
                    pass
                self._bg_item = None
            if self._image_item is None:
                self._image_item = self.create_image(
                    -offset_x, -offset_y, anchor="nw", image=photo)
            else:
                self.coords(self._image_item, -offset_x, -offset_y)
                self.itemconfigure(self._image_item, image=photo)
            self.tag_lower(self._image_item)
        else:
            if self._image_item is not None:
                try:
                    self.delete(self._image_item)
                except Exception:           # pragma: no cover
                    pass
                self._image_item = None
            fill = self._hover_fill if (self._hover and self._hover_fill) else self._fill
            if self._bg_item is None:
                self._bg_item = self.create_rectangle(0, 0, width, height,
                                                      outline="", fill=fill,
                                                      tags="quiet_bg")
            else:
                self.coords(self._bg_item, 0, 0, width, height)
                self.itemconfigure(self._bg_item, fill=fill)
            self.tag_lower(self._bg_item)
        # 2) 描边
        if self._border_item is None:
            self._border_item = self.create_rectangle(
                0, 0, width - 1, height - 1, outline=self._line_color(), width=1,
                tags="quiet_border")
        else:
            self.coords(self._border_item, 0, 0, width - 1, height - 1)
            self.itemconfigure(self._border_item, outline=self._line_color())
        # 3) 文字
        if self._text_item is None:
            self._text_item = self.create_text(
                width // 2, height // 2, text=self._text,
                fill=self._text_color(), anchor="center", font=self._font,
                tags="quiet_text")
        else:
            self.coords(self._text_item, width // 2, height // 2)
            self.itemconfigure(self._text_item, text=self._text,
                               fill=self._text_color(), font=self._font)
        self.tag_raise(self._border_item)
        self.tag_raise(self._text_item)

    # ---- 交互 ----
    def invoke(self) -> None:
        if self._enabled() and self.command is not None:
            self.command()

    def _on_click(self, _event: Any = None) -> str:
        self.invoke()
        return "break"

    def _on_enter(self, _event: Any = None) -> None:
        if not self._enabled():
            return
        self._hover = True
        self.repaint()

    def _on_leave(self, _event: Any = None) -> None:
        self._hover = False
        self.repaint()


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
        inset_x, inset_y, inset_right, inset_bottom = container_padding(container)
        if inset_x or inset_y or inset_right or inset_bottom:
            # ttk 的 padding 圈是容器自己画的实色底，place 默认只能铺到内容区以内
            # （bordermode="inside"，实测 relwidth=1 拿到的是内核 padding 之后的宽度）。
            # 于是窗口最外圈会留一条 win_bg 的纯色白边，而参照图那圈是壁纸薄罩的延续，
            # 观感上就是「整窗透图漏了一圈」。把切片按负偏移顶到容器原点、尺寸按容器
            # 外框给，正好盖住 padding；超出容器的部分本来就会被父窗口裁掉。
            #
            # 判断条件必须四条 padding 全看：KPI 行只有右边 padding 10px（给滚动条
            # 让位），左边和上边都是 0。早先这里只查 left/top，于是它走了 relwidth=1
            # 那条路，切片实际只有 1206 宽而容器 1216 —— 右边整整漏出 10px 的
            # win_bg 实色竖条，就是截图里 KPI 行右端那条白边。
            canvas.place(x=-inset_x, y=-inset_y, width=1, height=1)
            _cover_container(canvas, container)
        else:
            canvas.place(x=0, y=0, relwidth=1, relheight=1)
        send_to_back(canvas)
    except Exception:           # pragma: no cover - 控件已销毁
        return None
    return canvas


def container_padding(container: Any) -> tuple[int, int, int, int]:
    """容器 ttk padding 的 (左, 上, 右, 下) 像素数；没有 padding 时全 0。

    ttk 的 padding 支持 1/2/3/4 个值的简写（1 值四边相同、2 值上下/左右对称、
    3 值上/左右/下、4 值上右下左顺时针），这里按同一套规则展开成四元组。
    """
    try:
        padding = container.cget("padding")
    except Exception:       # pragma: no cover - tk.Frame 没有 padding 选项
        return (0, 0, 0, 0)
    values = pixel_values(padding)
    if not values:
        return (0, 0, 0, 0)
    if len(values) == 1:
        left, top, right, bottom = values[0], values[0], values[0], values[0]
    elif len(values) == 2:
        top, right = values[0], values[1]
        left, bottom = right, top
    elif len(values) == 3:
        top, right, bottom = values[0], values[1], values[2]
        left = right
    else:
        top, right, bottom, left = values[0], values[1], values[2], values[3]
    return (max(0, left), max(0, top), max(0, right), max(0, bottom))


def container_inset(container: Any) -> tuple[int, int]:
    """容器 ttk padding 的左/上像素数（早期接口，保留给调用方与测试）。"""
    left, top, _right, _bottom = container_padding(container)
    return (left, top)


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