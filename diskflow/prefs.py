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
        return data

    def save(self, data: dict[str, Any]) -> Path:
        merged = dict(DEFAULT_SETTINGS)
        for key, value in (data or {}).items():
            if isinstance(key, str):
                merged[key] = value
        merged["theme"] = normalize_theme_key(merged.get("theme"))
        merged["view"] = normalize_view_key(merged.get("view"))
        merged["show_serial"] = merged.get("show_serial") is True
        merged["show_gib"] = merged.get("show_gib") is True
        merged["lang"] = normalize_lang(merged.get("lang"))
        self.directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(merged, ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path
