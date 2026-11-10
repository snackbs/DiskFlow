"""Tk 字体对象的进程级缓存。

为什么需要这一层
----------------
`tkinter.font.Font` 在 `__init__` 里向 Tcl 申请一个**具名字体**
（`font create fontN ...`），并置 `delete_font = True`；因此它的 `__del__`
会调 `font delete fontN`。两件事叠在一起就成了故障：

1. 回收时机由 CPython 的 GC 决定，**不一定是主线程**。读盘 worker 线程只要
   分配了对象就可能触发一次回收，于是 worker 线程里出现 `tkinter.font.py:121
   in __del__ → self._call("font", "delete", ...)`。
2. Tcl 解释器是单线程的，跨线程 `call` 会被排队到主线程的 `mainloop` 里。
   测试里主线程在跑断言、没进 `mainloop`，这次 `call` 就永远等不到执行，
   worker 卡死；主线程再 `_wait_until_worker_done` 就是死等 30 秒。
   （`RuntimeError: main thread is not in main loop` 也是同源。）

所以问题不在「字体怎么用」，而在「字体对象会被销毁」。这里的办法是：
**凡是本进程创建的字体对象，一律由模块级列表强引用，永不回收**，
于是 `Font.__del__` 不会在后半程或别的线程里被触发。

顺带解决另一半：度量。旧 `DiskTrackerApp._font_measure` 每量一次文字就
新建一个 `Font`，一张卡片要量两次，切换主题/卡片视图时一屏几十次——
这正是上面那堆待回收对象的来源（实测 `font create` 只要 2.5µs，但每次
都留下一个待销毁对象）。度量走 `_measure_raw` 直接问 Tcl，一个对象都不建。

用法
----
```
font = cached_font(root, family="Microsoft YaHei UI", size=9, weight="bold")
```
参数相同的调用返回同一个对象，可以放心在 `_apply_font` 这种高频路径里用。
`nametofont`（`TkDefaultFont` 等系统具名字体）本来就不产生垃圾，别走这里。
"""

from __future__ import annotations

from typing import Any, Iterable

# 与 gui.py 一致：没有 Tk 的环境（只跑逻辑层的测试/CI）里本模块仍要能 import。
# 真正要用字体时一定已经有 Tk，那时 tkfont 必然可用。
try:                                    # pragma: no cover - 依赖系统 Tk
    import tkinter.font as tkfont
    _FontType: Any = tkfont.Font
except Exception:                       # pragma: no cover
    tkfont = None                       # type: ignore[assignment]
    # isinstance(x, ()) 恒为 False，正好用来表示「没有 Font 类型可判断」。
    _FontType = ()

# 已经创建过、并且**永不释放**的字体对象。键是 (根窗口标识, 参数元组)。
# 这里刻意用普通 dict + 模块级存活，而不是 weakref：需要保证 Font 对象
# 一直活到解释器退出（或进程结束），任何「可能被回收」的方案都回到原问题。
_FONTS: dict[tuple[int, tuple[Any, ...]], tkfont.Font] = {}

# 度量缓存：(根窗口标识, 字体参数元组) -> 每个字符/每串的像素宽不好缓存，
# 但 linespace 这种与文本无关的量可以，且卡片布局里被反复问。
_LINESPACE: dict[tuple[int, tuple[Any, ...]], int] = {}


def _root_id(master: Any) -> int:
    """取一个稳定的根窗口标识，用于把不同 Tk 解释器的字体隔开。

    测试里每个用例都建新 `Toplevel`，但字体资源挂在**同一个 Tcl 解释器**上；
    跨解释器复用字体对象会拿到已销毁的名字，所以键里必须带上解释器身份。
    这里用根窗口 `tkapp` 的 id：解释器活着它就活着，而且不会在窗口销毁后
    被重新分配给另一个解释器（同一时刻至少这个对象还在缓存里指向它）。
    """
    root = master
    try:
        toplevel = root.winfo_toplevel()
    except Exception:
        toplevel = root
    try:
        return id(toplevel.tk)
    except Exception:
        return id(toplevel)


def _spec_key(font: Any) -> tuple[Any, ...]:
    """把各种「字体描述」压成可哈希的键。

    Tk 里字体的写法有好几种：`("Microsoft YaHei UI", 9, "bold")` 元组、
    `"{Microsoft YaHei UI} 9 bold"` 字符串、`"TkDefaultFont"` 具名名字，
    也可能已经是 `Font` 对象（例如从 `cget("font")` 读回来的）。
    """

    if isinstance(font, _FontType):
        return ("object", font.name)
    if isinstance(font, str):
        return ("str", font)
    if isinstance(font, Iterable):
        parts = []
        for item in font:
            if isinstance(item, int):
                parts.append(("int", item))
            else:
                parts.append(("str", str(item)))
        return ("seq", tuple(parts))
    return ("repr", repr(font))


def cached_font(master: Any, *, family: str, size: int,
                weight: str | None = None, slant: str | None = None,
                underline: bool = False, overstrike: bool = False) -> tkfont.Font:
    """按参数取（或首次创建）一个**永不回收**的具名字体。

    同一个根窗口下参数相同即同一个对象，可以直接赋给 `itemconfigure(font=...)`。
    """
    key = (_root_id(master), ("new", str(family), int(size), weight, slant,
                              bool(underline), bool(overstrike)))
    font = _FONTS.get(key)
    if font is not None:
        return font
    options: dict[str, Any] = {"family": family, "size": int(size)}
    if weight:
        options["weight"] = weight
    if slant:
        options["slant"] = slant
    if underline:
        options["underline"] = True
    if overstrike:
        options["overstrike"] = True
    font = tkfont.Font(root=master, **options)
    _FONTS[key] = font
    return font


def font_for(master: Any, spec: Any) -> tkfont.Font:
    """把任意字体描述变成一个可复用的 `Font` 对象（`None` 退回默认字体）。

    与 `tkfont.Font(font=spec)` 等价，但结果被缓存：调用方可以每帧调用，
    不会每帧留下一个待回收对象。
    """
    if spec is None:
        return tkfont.nametofont("TkDefaultFont")
    if isinstance(spec, _FontType):
        return spec
    key = (_root_id(master), ("spec", _spec_key(spec)))
    font = _FONTS.get(key)
    if font is not None:
        return font
    font = tkfont.Font(root=master, font=spec)
    _FONTS[key] = font
    return font


def _measure_raw(master: Any, spec: Any, text: str | None,
                 option: str | None = None) -> int:
    """直接在 Tcl 层量字体，**不创建 `Font` 对象**。

    `font measure` / `font metrics` 都接受「字体描述」而不是必须具名，
    例如 `font measure {Microsoft YaHei UI 9} hello`。这样高频度量路径上
    一个待回收对象都不产生。
    """
    tk = getattr(master, "tk", master)
    font = spec if spec is not None else "TkDefaultFont"
    if option is not None:
        return int(tk.call("font", "metrics", font, option))
    return int(tk.call("font", "measure", font, "" if text is None else str(text)))


def measure(master: Any, spec: Any, text: str) -> int:
    """等价于 `Font.measure(text)`，但不建对象、失败退回粗估。"""
    try:
        return _measure_raw(master, spec, text)
    except Exception:
        return _fallback_measure(spec, text)


def linespace(master: Any, spec: Any) -> int:
    """等价于 `Font.metrics("linespace")`，带缓存。"""
    key = (_root_id(master), ("ls", _spec_key(spec)))
    value = _LINESPACE.get(key)
    if value is not None:
        return value
    try:
        value = _measure_raw(master, spec, None, "-linespace")
    except Exception:
        value = _fallback_measure(spec, None)
    _LINESPACE[key] = value
    return value


def _fallback_measure(spec: Any, text: str | None) -> int:
    """度量失败时的粗估：字号 * 2 作行高、全角 2 列 * 半字号作宽度。

    只在 Tcl 调用异常时走这里（控件已销毁、字体名不存在等），
    宁可尺寸估得糙一点，也不能让布局代码抛异常。
    """
    size = 9
    try:
        items = list(spec) if isinstance(spec, Iterable) and not isinstance(spec, str) else []
        for item in items:
            if isinstance(item, int):
                size = item
                break
    except Exception:
        size = 9
    if text is None:
        return (size + 4) * 2
    width = 0
    for char in str(text):
        width += 2 if ord(char) > 0x2E7F else 1
    return width * max(4, size - 1)
