"""界面偏好存储层（独立 settings.json，不碰 history.json 的 schema）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .history import default_storage_dir
from .i18n import DEFAULT_LANG, normalize_lang
from .theme import THEME_SYSTEM, normalize_theme_key
from .viewmodel import DEFAULT_VIEW, normalize_view_key
from .wallpaper import DEFAULT_ALPHA_LEVEL, normalize_alpha


# ---------------------------------------------------------------------------
# 界面偏好存储层（独立 settings.json，不碰 history.json 的 schema）
# ---------------------------------------------------------------------------

SETTINGS_FILENAME = "settings.json"
# show_serial 默认 False：序列号属于设备身份信息，默认不在界面上暴露，
# 需要时应从「工具 → 显示序列号」手动打开（偏好跟着 settings.json 走）。
DEFAULT_SETTINGS: dict[str, Any] = {
    "theme": THEME_SYSTEM,
    "view": DEFAULT_VIEW,
    "show_serial": False,
    # show_gib 默认 False：容量只显示厂商标称值（960 GB）。
    # 括号里的系统可见量（894.25 GiB）对普通用户常常是噪音，需要时从
    # 「工具 → 显示系统口径容量」手动打开。
    "show_gib": False,
    # 界面语言：zh-CN / en。语言只影响界面文案，history.json 的 schema 与内容一个字节都不变。
    "lang": DEFAULT_LANG,
    # 第 7 套「图片背景」主题用的壁纸路径（绝对路径，空串表示没选）。
    # 只影响观感：读不到文件、路径失效、解码失败都只让主题退化成纯色面板，不报错。
    "wallpaper": "",
    # 内容面板的不透明度（0~100）。65 是实测调好的那档（对话层 55% / 控件层 65%），
    # 菜单里给的是 0/25/50/75/100 五档，默认值刻意不落在档位上。
    "panel_alpha": DEFAULT_ALPHA_LEVEL,
}


class SettingsStore:
    """界面偏好读写：与 history.json 同目录；文件损坏或字段缺失时静默回落默认值。

    默认目录走 default_storage_dir()，所以安装版（exe 在 Program Files 下）
    与 history.json 一样落到 %LOCALAPPDATA%\\<APP_STORAGE_ID>。
    """

    def __init__(self, directory: str | os.PathLike[str] | None = None,
                 filename: str = SETTINGS_FILENAME) -> None:
        self.directory = Path(directory) if directory else default_storage_dir()
        self.filename = filename

    @property
    def path(self) -> Path:
        return self.directory / self.filename

    def load(self) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return data
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return data
        if not isinstance(payload, dict):
            return data
        theme = payload.get("theme")
        if isinstance(theme, str):
            data["theme"] = normalize_theme_key(theme)
        view = payload.get("view")
        if isinstance(view, str):
            data["view"] = normalize_view_key(view)
        # 只认真正的布尔值：字符串 "false" 之类一律落回默认（隐藏）
        show_serial = payload.get("show_serial")
        if isinstance(show_serial, bool):
            data["show_serial"] = show_serial
        # 同上：容量括号里的系统口径也默认关掉
        show_gib = payload.get("show_gib")
        if isinstance(show_gib, bool):
            data["show_gib"] = show_gib
        # 语言：非法值（含旧版本没写过这个键）一律回落中文
        lang = payload.get("lang")
        if isinstance(lang, str):
            data["lang"] = normalize_lang(lang)
        # 壁纸：只认非空字符串；路径是否存在这里不校验——用户可能把图放在
        # 移动硬盘/网络盘上，开机时不在就退化成纯色，插回去下次就有了。
        wallpaper = payload.get("wallpaper")
        if isinstance(wallpaper, str):
            data["wallpaper"] = wallpaper.strip()
        # 不透明度：手改 settings.json 写成 "80%"、null、-5 都不该让界面出事，
        # normalize_alpha 把一切非法输入收敛回默认档。
        data["panel_alpha"] = normalize_alpha(payload.get("panel_alpha", DEFAULT_ALPHA_LEVEL))
        return data

    def save(self, data: dict[str, Any]) -> Path:
        # 从磁盘上的现状起步，不能从 DEFAULT_SETTINGS 起步：
        # GUI 每次只保存自己动过的那一个键（_save_setting("theme", ...) 这类），
        # 若从默认值起步，存「显示序列号」就会把用户之前选的主题、背景图、
        # 不透明度一起抹回默认——用户看到的是「设置自己变回去了」。
        merged = self.load()
        for key, value in (data or {}).items():
            if isinstance(key, str):
                merged[key] = value
        merged["theme"] = normalize_theme_key(merged.get("theme"))
        merged["view"] = normalize_view_key(merged.get("view"))
        merged["show_serial"] = merged.get("show_serial") is True
        merged["show_gib"] = merged.get("show_gib") is True
        merged["lang"] = normalize_lang(merged.get("lang"))
        wallpaper = merged.get("wallpaper")
        merged["wallpaper"] = wallpaper.strip() if isinstance(wallpaper, str) else ""
        merged["panel_alpha"] = normalize_alpha(merged.get("panel_alpha", DEFAULT_ALPHA_LEVEL))
        self.directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(merged, ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path
