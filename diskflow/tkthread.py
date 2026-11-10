"""把 Tk 对象的**析构**排到主线程执行。

为什么需要这一层
----------------
Tk 解释器是单线程的：任何 `tk.call(...)` 只能在创建它的线程（主线程）里跑。
cpython 的 `tkinter` 却给三类对象都写了会调 Tcl 的 `__del__`：

* `tkinter.Variable.__del__`  → `info exists` + `globalunsetvar`
* `tkinter.Image.__del__`     → `image delete`
* `tkinter.font.Font.__del__` → `font delete`

而「什么时候回收」由 CPython 决定，**不一定是主线程**：只要读盘 worker 线程
分配了对象就可能触发一次 GC，于是 worker 线程里开始执行这些 `__del__`。
Tcl 会把这次跨线程调用排队到主线程的 `mainloop`；测试里主线程正在跑断言、
没进 `mainloop`，这次调用就永远等不到执行，worker 卡死，主线程再等它
就是死等（伴随 `RuntimeError: main thread is not in main loop`）。

现场抓到的栈（`test_gui_poll_keeps_the_language_of_the_run_it_started`）：

    disk-reader → perform_run → store.append → json.dumps
    → <GC> → tkinter/__init__.py:414 in __del__
    → self._tk.call("info", "exists", self._name)   # 卡在这里

办法
----
不是「别用 Tk 对象」（做不到），而是**把析构动作本身挪回主线程**：
在非主线程进入 `__del__` 时，不立刻调 Tcl，而是把「真正执行原 `__del__`」
这个动作塞进队列，由主线程在既有的轮询里调用 `drain()` 执行。
对象在排队期间被 lambda 强引用，Tcl 名字仍然有效，清理不会落空。

队列有长度上限：极端情况下（主线程长时间不 drain）宁可丢最旧的清理动作、
让那几个 Tcl 名字泄漏到解释器退出，也不能让内存无限涨——泄漏一个变量名
远比卡死一个线程便宜。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Callable, Deque

# 待执行的清理动作。元素是闭包，内部强引用着对应的 Tk 对象。
_PENDING: Deque[Callable[[], None]] = deque(maxlen=1024)

# 只在入队/出队时短暂持有；动作本身一律在锁外执行，避免清理里再入队时死锁。
_LOCK = threading.Lock()

_installed = False


def install() -> bool:
    """给三类 Tk 对象的 `__del__` 套上「跨线程就排队」。可重复调用。

    返回是否已经装好（没有 Tk 的环境里会返回 False，不抛异常）。
    """
    global _installed
    if _installed:
        return True
    try:
        import tkinter
    except Exception:                       # pragma: no cover - 无 Tk 的 CI
        return False
    _patch(tkinter.Variable)
    _patch(tkinter.Image)
    try:
        import tkinter.font as tkfont
    except Exception:                       # pragma: no cover
        pass
    else:
        _patch(tkfont.Font)
    _installed = True
    return True


def _patch(cls: Any) -> None:
    original = getattr(cls, "__del__", None)
    if original is None or getattr(original, "_tkthread_wrapped", False):
        return

    def __del__(self: Any) -> None:
        try:
            main_thread = threading.main_thread()
        except Exception:                   # pragma: no cover - 解释器收尾阶段
            main_thread = None
        if main_thread is None or threading.current_thread() is main_thread:
            original(self)                  # 主线程：照旧立刻清理
            return
        # worker 线程：绝不能在这里调 Tcl。排进队列，等主线程来收。
        _defer(lambda: original(self))

    __del__._tkthread_wrapped = True        # type: ignore[attr-defined]
    cls.__del__ = __del__


def _defer(action: Callable[[], None]) -> None:
    try:
        with _LOCK:
            _PENDING.append(action)
    except Exception:                       # pragma: no cover - 解释器收尾阶段
        pass


def pending() -> int:
    """还有多少个清理动作在排队（测试与诊断用）。"""
    with _LOCK:
        return len(_PENDING)


def drain(limit: int = 64) -> int:
    """在**主线程**里执行排队的清理动作，返回本次执行了几个。

    非主线程调用直接返回 0：那正是我们要避免的情形。
    """
    try:
        if threading.current_thread() is not threading.main_thread():
            return 0
    except Exception:                       # pragma: no cover
        return 0
    done = 0
    while done < max(1, limit):
        with _LOCK:
            if not _PENDING:
                break
            action = _PENDING.popleft()
        try:
            action()
        except Exception:                   # noqa: BLE001 - 控件/解释器可能已销毁
            pass
        done += 1
    return done
