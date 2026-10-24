"""入口：窗口图标、create_app、main。
"""

from __future__ import annotations

from typing import Any, Sequence

from .gui import DiskTrackerApp
from .history import HistoryStore
from .prefs import SettingsStore
from .providers import SmartctlProvider, locate_app_icon


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def apply_window_icon(window: Any, icon_path: str | None = None) -> str | None:
    """给窗口设置应用图标，返回实际使用的 .ico 路径（没设上返回 None）。

    Windows 下优先 iconbitmap（.ico 多尺寸，任务栏/标题栏/Alt+Tab 都正确）；
    非 Windows 或 .ico 不可用时退回 iconphoto。失败一律静默：图标是锦上添花，
    绝不能因为图标缺失或格式问题让主窗口起不来。
    """
    path = icon_path if icon_path else locate_app_icon()
    if not path:
        return None
    try:
        window.iconbitmap(default=path)
        return path
    except Exception:
        pass
    try:
        import tkinter as tk

        photo = tk.PhotoImage(file=path)
        window.iconphoto(True, photo)
        # 必须留住引用，否则 PhotoImage 被回收后图标会变空白
        window._app_icon_photo = photo  # type: ignore[attr-defined]
        return path
    except Exception:
        return None


def create_app(provider: Any = None, store: HistoryStore | None = None,
               root: Any = None, settings: SettingsStore | None = None,
               theme: str | None = None, icon_path: str | None = None) -> DiskTrackerApp:
    import tkinter as tk

    owns_root = root is None
    window = root if root is not None else tk.Tk()
    app = DiskTrackerApp(window, provider=provider, store=store, autostart=False,
                         settings=settings, theme=theme)
    apply_window_icon(window, icon_path)
    if owns_root:
        app.owns_root = True
    return app


def main(argv: Sequence[str] | None = None) -> int:
    import tkinter as tk

    root = tk.Tk()
    apply_window_icon(root)
    store = HistoryStore()
    DiskTrackerApp(root, provider=SmartctlProvider(), store=store, autostart=True,
                   settings=SettingsStore(store.resolve_dir()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
