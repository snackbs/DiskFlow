"""界面偏好存储层（独立 settings.json，不碰 history.json 的 schema）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .history import _default_app_dir
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
}


class SettingsStore:
    """界面偏好读写：与 history.json 同目录；文件损坏或字段缺失时静默回落默认值。"""

    def __init__(self, directory: str | os.PathLike[str] | None = None,
                 filename: str = SETTINGS_FILENAME) -> None:
        self.directory = Path(directory) if directory else _default_app_dir()
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
        return data

    def save(self, data: dict[str, Any]) -> Path:
        merged = dict(DEFAULT_SETTINGS)
        for key, value in (data or {}).items():
            if isinstance(key, str):
                merged[key] = value
        merged["theme"] = normalize_theme_key(merged.get("theme"))
        merged["view"] = normalize_view_key(merged.get("view"))
        merged["show_serial"] = merged.get("show_serial") is True
        self.directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(merged, ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        return self.path
