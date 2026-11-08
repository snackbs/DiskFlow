"""壁纸背景层：解码图片 -> cover 裁切 -> 两层半透明混色 -> PPM（给 Tk PhotoImage）。

第 7 套主题（图片背景）的数据区完全画在一张 tk.Canvas 上，没有真透明
（不用 wm_attributes("-alpha")，那会让整窗一起变淡、文字也跟着糊）。做法是：
把壁纸按窗口尺寸 cover 裁切，先用「对话层」整体压一遍，再把需要更实的面板区域
用「控件层」压第二遍，得到一整张已经混好的 RGB 位图贴在 Canvas 最底层。

为什么自己解码而不用 Tk：
    Tk 8.6 自带的解码器只有 GIF / PGM / PPM / PNG，没有 JPEG。用户自选的壁纸
    绝大多数是 .jpg，直接 PhotoImage(file=...) 会抛 TclError: couldn't recognize
    data in image file。纯 ctypes 调 GDI+（gdiplus.dll，系统自带，无第三方依赖）
    可以解 JPEG/PNG/BMP/GIF，顺便在解码时就直接缩放，比先解原图再裁快得多。

耗时实测（2730x1536 壁纸 -> 1244x699 窗口，本机）：
    GDI+ 缩放约 72ms；裁切 + BGRA->RGB 约 3ms；两次查表混色约 4ms；拼 PPM 约 4ms；
    PhotoImage 约 13ms。全链路约 100ms，放在后台线程做，窗口拖动时去抖。
"""

from __future__ import annotations

import atexit
import ctypes
import threading
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 混色参数（第 7 套主题的观感全由这几个数决定）
#
# 下面这组数字是从用户最终拍板的那张并排参照图左半（卡片视图）里逐像素回归
# 出来的。做法：取整图里 10x10 邻域标准差 <1.2 的「平坦色块」（避开文字与描边），
# 按 obs = (1-a)·wall + a·P 对同一张壁纸（bg.png，1244x699，视口对齐）逐通道
# 做最小二乘，得到三类区域三通道一致的解：
#   对话区（没有控件的大块留白/标题）  α≈0.55，面板本色近白 #EEF2FA
#   卡片区（KPI 卡、卡片视图底）      α≈0.65
#   表格/控件区（带控件的行、输入框） α≈0.73，面板本色 #E6E9F0
# 也就是说：这是「浅色薄罩」——面板是近白的，壁纸在后面透出约 28%~45%，
# 越靠控件越实一点点。方向与「深藏青 α0.81」恰好相反：深色版只透 19%，
# 实机就是壁纸被压死；浅色版才是参照图那种「薄薄一层奶白」的观感。
# 浅底深字：tree_fg #1F2933 对 α0.72 的 #F2F5FA 面板约 12:1（WCAG AA 要 4.5），
# 就算壁纸是最暗的纯黑，正文对比度仍在 7:1 以上。
# ---------------------------------------------------------------------------

CONVERSATION_RGB: tuple[int, int, int] = (238, 242, 250)   # #EEF2FA
CONVERSATION_ALPHA = 0.55
CONTROL_RGB: tuple[int, int, int] = (230, 233, 240)        # #E6E9F0
CONTROL_ALPHA = 0.73

# 「工具 → 背景图透明度」给出的五档（百分比）。73% 就是上面那组实测档位
# （对话层 55% / 控件层 73%），所以默认值取 73 而不是菜单里的某一档：
# 用户不动菜单时看到的就是调好的样子，动了之后才落到 0/25/50/75/100。
#
# 卡片视图的薄罩不在这里：卡片底座要「透」，靠的是每张卡片（photo_card.PhotoCard）
# 自己叠一层近白 RGBA —— Tk 8.6 会对 RGBA PNG 做真正的 alpha 合成（已实测逐通道
# 吻合），而 Canvas 矩形只能是不透明色。卡片内部全部画在画布上，没有一处分色底，
# 所以薄罩之下就是壁纸本身，不会出现一块块平色补丁。
ALPHA_LEVELS: tuple[int, ...] = (0, 25, 50, 75, 100)
DEFAULT_ALPHA_LEVEL = 73


def normalize_alpha(value: object) -> int:
    """把任意输入收敛成 0~100 的整数不透明度。非数字、None、越界都给默认档。

    settings.json 是用户可手改的文件，读到 "80%"、80.4、None 之类都不能让界面炸，
    统一在这里兜底：非整型/浮点一律回落 DEFAULT_ALPHA_LEVEL，越界按边界截断。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_ALPHA_LEVEL
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):        # pragma: no cover - 上面的类型检查已挡住
        return DEFAULT_ALPHA_LEVEL


def alpha_pair(level: int) -> tuple[float, float]:
    """把「不透明度百分比」换算成 (对话层 alpha, 控件层 alpha)。

    默认档（DEFAULT_ALPHA_LEVEL = 73）时控件层正好落在实测的 CONTROL_ALPHA，
    对话层按固定比例（0.55/0.73）跟随；其余档位按同一比例缩放：0% 完全透出
    壁纸，100% 控件层完全不透。超出 0~100 的输入按边界截断。
    """
    percent = max(0, min(100, int(level)))
    panel = percent / 100.0
    ratio = (CONVERSATION_ALPHA / CONTROL_ALPHA) if CONTROL_ALPHA else 1.0
    return min(1.0, panel * ratio), panel


def hex_rgb(text: Any,
            default: tuple[int, int, int] = CONVERSATION_RGB) -> tuple[int, int, int]:
    """'#F2F5FA' -> (242, 245, 250)；解析不了就返回 default（纯色兜底用）。

    兜底色跟着 CONVERSATION_RGB 走：浅色薄罩体系下必须回落成同样偏浅的面板
    本色，若回落成深色，一旦主题色读坏，界面上会突然出现一块墨斑。
    """
    if not isinstance(text, str):
        return default
    value = text.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return default
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return default

# 解码后每个像素按 BGRA 四字节交错（GDI+ 的 32bppARGB 在内存里就是 BGRA）
_BGRA = 4


# ---------------------------------------------------------------------------
# 纯函数层（不碰文件系统、不碰 Tk，可直接单测）
# ---------------------------------------------------------------------------


def merge_layers(c1: Sequence[int], a1: float,
                 c2: Sequence[int], a2: float) -> tuple[tuple[int, int, int], float]:
    """把「色 c1 不透明度 a1」上再盖「色 c2 不透明度 a2」合并成一层等效的 (色, 不透明度)。

    合成公式：总不透明度 a = 1-(1-a1)(1-a2)；
    等效颜色按预乘 alpha 求 c = (a2*c2 + (1-a2)*a1*c1) / a。
    合并的好处是面板区只需查一次表，而不是「先整图混一遍、再按行混一遍」——
    后者的实测耗时是 75ms，合并后只要 3.8ms。
    """
    a = 1.0 - (1.0 - float(a1)) * (1.0 - float(a2))
    if a <= 0.0:
        return (0, 0, 0), 0.0
    color = [int(round((a2 * c2[k] + (1.0 - a2) * a1 * c1[k]) / a)) for k in range(3)]
    return (color[0], color[1], color[2]), a


def panel_coat(conv_alpha: float, panel_alpha: float) -> float:
    """把「面板区最终要不透明的比例」换算成第二遍混色要用的那层不透明度。

    panel_alpha 说的是「面板区一共遮住多少」（实测 0.73），而它是在对话层
    （0.55）之上再盖一层得到的，所以要解 1-(1-conv)(1-coat) = panel：
        coat = 1 - (1-panel) / (1-conv)
    conv 已经接近 1（或用户把档位拉到 100%）时直接返回 panel，
    避免除零，也让「100% 完全不透」这一端仍然成立。
    """
    panel = min(1.0, max(0.0, float(panel_alpha)))
    conv = min(1.0, max(0.0, float(conv_alpha)))
    if conv >= 1.0 or panel <= conv:
        return panel
    return max(0.0, min(1.0, 1.0 - (1.0 - panel) / (1.0 - conv)))


def alpha_lut(color: int, alpha: float) -> bytes:
    """256 字节查表：把 0..255 的原始通道值混成「色 color 不透明度 alpha」。"""
    a = min(1.0, max(0.0, float(alpha)))
    c = min(255, max(0, int(color)))
    return bytes(int(round(a * c + (1.0 - a) * v)) for v in range(256))


def cover_geometry(src_w: int, src_h: int,
                   want_w: int, want_h: int) -> tuple[int, int, int, int]:
    """cover 裁切几何：等比放大到刚好盖满目标框，再居中裁掉多余部分。

    返回 (缩放后宽, 缩放后高, 裁切偏移 x, 裁切偏移 y)。
    目标比源图还大时同样放大（cover 语义），绝不留下空白边。
    """
    if src_w <= 0 or src_h <= 0 or want_w <= 0 or want_h <= 0:
        return (max(1, int(want_w)), max(1, int(want_h)), 0, 0)
    scale = max(want_w / src_w, want_h / src_h)
    dw = max(int(want_w), int(round(src_w * scale)))
    dh = max(int(want_h), int(round(src_h * scale)))
    return dw, dh, max(0, (dw - int(want_w)) // 2), max(0, (dh - int(want_h)) // 2)


def tint_planes(rgb: bytes | bytearray, color: Sequence[int],
                alpha: float) -> tuple[bytes, bytes, bytes]:
    """把 RGB 交错字节流按 (color, alpha) 混色，返回三个独立通道平面。

    用 bytes.translate 走查表（C 速度）。在 Python 里逐像素算的话，
    1244x699 需要 87 万次循环，实测 300ms 以上，完全不可用。
    """
    r = bytes(rgb[0::3]).translate(alpha_lut(color[0], alpha))
    g = bytes(rgb[1::3]).translate(alpha_lut(color[1], alpha))
    b = bytes(rgb[2::3]).translate(alpha_lut(color[2], alpha))
    return r, g, b


def assemble(width: int, height: int,
             conv_planes: tuple[bytes, bytes, bytes],
             panel_planes: tuple[bytes, bytes, bytes],
             regions: Iterable[tuple[int, int, int, int]]) -> bytearray:
    """按矩形区域把两个已混色的版本拼成整图：区间内用 panel，区间外仍用 conv。

    regions 是半开区间 (x0, y0, x1, y1)，会被裁到窗口范围内。
    按行处理：同一行里只有 panel 的列段需要替换，其余整行直接拷对话层。
    regions 重叠时不重复混色——重叠的列段只会被写一次（后者覆盖前者，颜色相同）。
    """
    conv_r, conv_g, conv_b = conv_planes
    panel_r, panel_g, panel_b = panel_planes
    span = width * 3
    out = bytearray(span * height)

    rows: dict[int, list[tuple[int, int]]] = {}
    for x0, y0, x1, y1 in regions:
        left = max(0, min(width, int(x0)))
        right = max(0, min(width, int(x1)))
        top = max(0, min(height, int(y0)))
        bottom = max(0, min(height, int(y1)))
        if right <= left:
            continue
        for y in range(top, bottom):
            rows.setdefault(y, []).append((left, right))

    for y in range(height):
        base = y * span
        start_x = y * width
        end_x = start_x + width
        segs = rows.get(y)
        if not segs:
            out[base:base + span:3] = conv_r[start_x:end_x]
            out[base + 1:base + span:3] = conv_g[start_x:end_x]
            out[base + 2:base + span:3] = conv_b[start_x:end_x]
            continue
        for channel, conv_plane, panel_plane in ((0, conv_r, panel_r),
                                                 (1, conv_g, panel_g),
                                                 (2, conv_b, panel_b)):
            line = bytearray(conv_plane[start_x:end_x])
            for left, right in segs:
                line[left:right] = panel_plane[start_x + left:start_x + right]
            out[base + channel:base + span:3] = line
    return out


def ppm_bytes(width: int, height: int, rgb: bytes | bytearray) -> bytes:
    """RGB 交错字节流 -> P6 格式 PPM 字节串（Tk 的 PhotoImage 认得这个格式）。"""
    return b"P6\n%d %d\n255\n" % (int(width), int(height)) + bytes(rgb)


# ---------------------------------------------------------------------------
# GDI+ 解码层（纯 ctypes，系统自带 gdiplus.dll，无第三方依赖）
# ---------------------------------------------------------------------------

_gdiplus_lock = threading.Lock()
_gdiplus_module: Any = None
_gdiplus_token: Any = None
_gdiplus_tried = False


class _StartupInput(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("callback", ctypes.c_void_p),
                ("suppress_bg", ctypes.c_int), ("suppress_codecs", ctypes.c_int)]


class _GpRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("width", ctypes.c_int), ("height", ctypes.c_int)]


class _GpBitmapData(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
                ("stride", ctypes.c_int), ("pixel_format", ctypes.c_int),
                ("scan0", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]


_PIXEL_32BPP_ARGB = 0x0026200A
_LOCK_READ = 1
_INTERP_BICUBIC = 7


def _load_gdiplus() -> Any:
    """懒加载 gdiplus.dll 并完成 GdiplusStartup（全进程只做一次）。

    非 Windows 或加载失败时返回 None，调用方据此退化成纯色背景，绝不抛异常到 GUI。
    """
    global _gdiplus_module, _gdiplus_tried
    with _gdiplus_lock:
        if _gdiplus_tried:
            return _gdiplus_module
        _gdiplus_tried = True
        try:
            lib = ctypes.WinDLL("gdiplus.dll")       # type: ignore[attr-defined]
        except Exception:
            _gdiplus_module = None
            return None

        def proto(name: str, argtypes: list[Any], restype: Any = ctypes.c_int) -> Any:
            fn = getattr(lib, name)
            fn.argtypes = argtypes
            fn.restype = restype
            return fn

        try:
            startup = proto("GdiplusStartup",
                            [ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(_StartupInput), ctypes.c_void_p])
            token = ctypes.c_void_p()
            status = startup(ctypes.byref(token),
                             ctypes.byref(_StartupInput(1, None, 0, 0)), None)
            if status != 0:
                _gdiplus_module = None
                return None
            proto("GdipCreateBitmapFromFile",
                  [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)])
            proto("GdipCreateBitmapFromScan0",
                  [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
            proto("GdipGetImageWidth", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)])
            proto("GdipGetImageHeight", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)])
            proto("GdipGetImageGraphicsContext",
                  [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
            proto("GdipSetInterpolationMode", [ctypes.c_void_p, ctypes.c_int])
            proto("GdipDrawImageRectI",
                  [ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int])
            proto("GdipFlush", [ctypes.c_void_p, ctypes.c_int])
            proto("GdipDeleteGraphics", [ctypes.c_void_p])
            proto("GdipBitmapLockBits",
                  [ctypes.c_void_p, ctypes.POINTER(_GpRect), ctypes.c_int, ctypes.c_int,
                   ctypes.POINTER(_GpBitmapData)])
            proto("GdipBitmapUnlockBits",
                  [ctypes.c_void_p, ctypes.POINTER(_GpBitmapData)])
            proto("GdipDisposeImage", [ctypes.c_void_p])
            proto("GdiplusShutdown", [ctypes.c_void_p], None)
            # token 必须活着，GDI+ 才能用；进程退出前由 shutdown_gdiplus() 归还
            global _gdiplus_token
            _gdiplus_token = token
            _gdiplus_module = lib
            return lib
        except Exception:
            _gdiplus_module = None
            return None


def shutdown_gdiplus() -> None:
    """成对归还 GdiplusStartup（幂等，只在本进程退出时调用）。

    为什么放在 atexit 而不是窗口关闭时：decode_cover 每次调用都会在 finally 里
    释放自己创建的 bitmap/graphics，退出时不可能还有存活的 GDI+ 对象；而窗口
    可能被反复销毁重建（切主题、GUI 测试），在窗口关闭时 shoutdown 会让下一次
    解码拿到失效的 token 直接崩。注册在 atexit 上，只跑一次，且一定晚于所有
    Tk 控件销毁（Tk 的 PhotoImage 是 Tcl 侧内存，不依赖 GDI+）。
    """
    global _gdiplus_module, _gdiplus_token, _gdiplus_tried
    with _gdiplus_lock:
        lib = _gdiplus_module
        token = _gdiplus_token
        _gdiplus_module = None
        _gdiplus_token = None
        # 关掉之后不再重开：能走到这里已经是进程退出路径了
        _gdiplus_tried = True
    if lib is None or token is None:
        return
    try:
        lib.GdiplusShutdown(token)
    except Exception:       # pragma: no cover - 退出路径不允许抛异常
        pass


atexit.register(shutdown_gdiplus)


def gdiplus_available() -> bool:
    """GDI+ 是否可用（Windows 上几乎总是 True；其它平台/精简系统为 False）。"""
    return _load_gdiplus() is not None


def decode_cover(path: str | Any, want_w: int, want_h: int) -> bytes | None:
    """解码任意 GDI+ 支持的图片，等比缩放到盖满 want_w x want_h 并居中裁切。

    返回 RGB 交错字节（长度 = want_w*want_h*3），失败返回 None。
    缩放在 GDI+ 里做（一次高质量双三次缩放），Python 侧只做 BGRA->RGB 转置，
    这是全链路里最省的做法：3ms 转置 vs 87 万次 Python 循环。
    """
    lib = _load_gdiplus()
    if lib is None or want_w <= 0 or want_h <= 0:
        return None
    src = ctypes.c_void_p()
    canvas = ctypes.c_void_p()
    graphics = ctypes.c_void_p()
    data = _GpBitmapData()
    locked = False
    try:
        if lib.GdipCreateBitmapFromFile(str(path), ctypes.byref(src)) != 0:
            return None
        sw = ctypes.c_uint32()
        sh = ctypes.c_uint32()
        if lib.GdipGetImageWidth(src, ctypes.byref(sw)) != 0:
            return None
        if lib.GdipGetImageHeight(src, ctypes.byref(sh)) != 0:
            return None
        dw, dh, ox, oy = cover_geometry(sw.value, sh.value, want_w, want_h)

        if lib.GdipCreateBitmapFromScan0(dw, dh, dw * _BGRA,
                                         _PIXEL_32BPP_ARGB, None,
                                         ctypes.byref(canvas)) != 0:
            return None
        if lib.GdipGetImageGraphicsContext(canvas, ctypes.byref(graphics)) != 0:
            return None
        lib.GdipSetInterpolationMode(graphics, _INTERP_BICUBIC)
        if lib.GdipDrawImageRectI(graphics, src, 0, 0, dw, dh) != 0:
            return None
        lib.GdipFlush(graphics, 0)
        lib.GdipDeleteGraphics(graphics)
        graphics = ctypes.c_void_p()

        rect = _GpRect(ox, oy, want_w, want_h)
        if lib.GdipBitmapLockBits(canvas, ctypes.byref(rect), _LOCK_READ,
                                  _PIXEL_32BPP_ARGB, ctypes.byref(data)) != 0:
            return None
        locked = True
        stride = data.stride
        raw = ctypes.string_at(data.scan0, stride * want_h)
        lib.GdipBitmapUnlockBits(canvas, ctypes.byref(data))
        locked = False

        rgb = bytearray(want_w * want_h * 3)
        for y in range(want_h):
            row = raw[y * stride:y * stride + want_w * _BGRA]
            base = y * want_w * 3
            rgb[base:base + want_w * 3:3] = row[2::_BGRA]
            rgb[base + 1:base + want_w * 3:3] = row[1::_BGRA]
            rgb[base + 2:base + want_w * 3:3] = row[0::_BGRA]
        return bytes(rgb)
    except Exception:
        return None
    finally:
        if locked:
            try:
                lib.GdipBitmapUnlockBits(canvas, ctypes.byref(data))
            except Exception:
                pass
        if graphics:
            try:
                lib.GdipDeleteGraphics(graphics)
            except Exception:
                pass
        for handle in (canvas, src):
            if handle:
                try:
                    lib.GdipDisposeImage(handle)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 组装层：把「壁纸 + 两层混色 + 面板区域」一次做成 PPM
# ---------------------------------------------------------------------------


def compose_ppm(path: str | Any, width: int, height: int,
                regions: Iterable[tuple[int, int, int, int]],
                *, conv_color: Sequence[int] = CONVERSATION_RGB,
                conv_alpha: float = CONVERSATION_ALPHA,
                panel_color: Sequence[int] = CONTROL_RGB,
                panel_alpha: float = CONTROL_ALPHA) -> bytes | None:
    """完整流程：解码+cover -> 对话层混色 -> 面板区合并混色 -> PPM 字节串。

    壁纸读不到（文件被删、格式不支持、GDI+ 不可用）时返回 None，
    由 GUI 退化成纯色背景（仍然是同一套主题配色），不会出现黑块或崩溃。
    """
    rgb = decode_cover(path, width, height)
    if rgb is None:
        return None
    conv_planes = tint_planes(rgb, conv_color, conv_alpha)
    coat = panel_coat(conv_alpha, panel_alpha)
    merged_color, merged_alpha = merge_layers(conv_color, conv_alpha,
                                              panel_color, coat)
    panel_planes = tint_planes(rgb, merged_color, merged_alpha)
    out = assemble(width, height, conv_planes, panel_planes, regions)
    return ppm_bytes(width, height, out)


def compose_png_fallback(width: int, height: int,
                         color: Sequence[int] = CONVERSATION_RGB,
                         regions: Iterable[tuple[int, int, int, int]] = ()) -> bytes:
    """纯色退化背景（壁纸不可用时）。返回同样格式的 PPM 字节串。"""
    base = bytes(int(max(0, min(255, c))) for c in color) * (int(width) * int(height))
    return ppm_bytes(width, height, base)
