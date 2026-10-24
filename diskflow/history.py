"""历史存储层：history.json 的读写、校验、损坏备份与 v1→v2 迁移。

优先写 exe 同目录，不可写时回退 %LOCALAPPDATA% 下的 APP_STORAGE_ID 目录；
目录 ID 固定，显示名改名不影响历史数据。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import APP_STORAGE_ID, PROJECT_DIR
from .models import DiskSnapshot, disk_key
from .timefmt import parse_timestamp, timestamp_seconds


# ---------------------------------------------------------------------------
# 历史存储层
# ---------------------------------------------------------------------------

HISTORY_FILENAME = "history.json"
SCHEMA_VERSION = 2                  # v2 = 「盘档案 + 读数数组」（见 history_payload）
SCHEMA_VERSION_LEGACY = 1           # v1 = 扁平 records 数组，读到时静默迁移
REQUIRED_RECORD_FIELDS = ("timestamp", "read_gb", "write_gb")
HISTORY_TRIM_HOURS = 24             # 最近 24 小时的读数原样保留，更早的按天折叠
HISTORY_BACKUP_PREFIX = "history.before-trim-"
HISTORY_RESET_PREFIX = "history.reset-"      # 「重置历史文件」前的自动备份前缀


def _history_schema_version(data: Any) -> int:
    """读 schema_version；非整数按最老的 v1 处理（老文件没有这个字段）。"""
    if not isinstance(data, dict):
        return SCHEMA_VERSION_LEGACY
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return SCHEMA_VERSION_LEGACY
    return version


def _reading_day(text: Any) -> str:
    """读数所属的本地日期（用于"按天折叠"）。解析不了就退回字符串前 10 位。"""
    moment = parse_timestamp(text)
    if moment is not None:
        return moment.date().isoformat()
    return str(text)[:10]


def trim_readings(readings: Sequence[dict],
                  newest_seconds: float | None = None) -> list[dict]:
    """折叠过密的历史读数：最近 HISTORY_TRIM_HOURS 小时原样保留，更早的按天只留最后一条。

    两条硬规则：
    * 每块盘的第一条与最后一条永久保留（否则"历史累计增量"和"本次区间增量"会被算错）；
    * 恰好落在边界上的读数视为"仍在窗口内"，避免相邻两次读数因为浮点噪声被折叠。
    """
    items = list(readings)
    total = len(items)
    if total <= 2:
        return items
    if newest_seconds is None:
        newest_seconds = timestamp_seconds(items[-1].get("t"))
    if newest_seconds is None:
        return items
    cutoff = float(newest_seconds) - HISTORY_TRIM_HOURS * 3600
    kept_reversed: list[dict] = []
    seen_days: set[str] = set()
    for index in range(total - 1, -1, -1):
        reading = items[index]
        seconds = timestamp_seconds(reading.get("t"))
        if index == 0 or index == total - 1 or seconds is None or seconds >= cutoff:
            kept_reversed.append(reading)
            continue
        day = _reading_day(reading.get("t"))
        if day in seen_days:
            continue
        seen_days.add(day)
        kept_reversed.append(reading)
    kept_reversed.reverse()
    return kept_reversed


def history_payload(records: Sequence[dict]) -> dict:
    """把扁平记录列表折叠成 v2 文件结构：「盘档案 + 读数数组」。

    型号/序列号/协议/设备每块盘只存一次，每次读数压成
    [{"t": 时间戳, "r": 累计读 GB, "w": 累计写 GB, "e": 0E}]，
    因此 history.json 的体积只随「天数 × 盘数」增长，与使用时长无关。
    """
    disks: dict[str, dict] = {}
    order: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        key = str(record.get("key") or disk_key(str(record.get("model") or ""),
                                                  str(record.get("serial") or "")))
        profile = disks.get(key)
        if profile is None:
            profile = {
                "key": key,
                "model": str(record.get("model") or ""),
                "serial": str(record.get("serial") or ""),
                "protocol": str(record.get("protocol") or "未知"),
                "device": str(record.get("device") or ""),
                "readings": [],
            }
            disks[key] = profile
            order.append(key)
        else:
            if record.get("model"):
                profile["model"] = str(record["model"])
            if record.get("serial"):
                profile["serial"] = str(record["serial"])
            protocol = str(record.get("protocol") or "")
            if protocol and protocol != "未知":
                profile["protocol"] = protocol
            if record.get("device"):
                profile["device"] = str(record["device"])
        profile["readings"].append({
            "t": str(record.get("timestamp") or ""),
            "r": record.get("read_gb"),
            "w": record.get("write_gb"),
            "e": record.get("media_errors"),
        })
    for key in order:
        profile = disks[key]
        profile["readings"].sort(key=lambda reading: timestamp_seconds(reading.get("t")) or 0.0)
        profile["readings"] = trim_readings(profile["readings"])
    return {
        "schema_version": SCHEMA_VERSION,
        "app": APP_STORAGE_ID,
        "disks": [disks[key] for key in order],
    }


def count_history_readings(data: Any) -> int:
    """统计历史文件里的读数条数（v1 / v2 都支持），用于诊断与测试。"""
    records, _ = validate_history(data)
    return len(records)


class HistoryLoadResult:
    def __init__(self, records: list[dict] | None = None, recovered: bool = False,
                 message: str = "", path: Path | None = None,
                 backup_path: Path | None = None,
                 migrated: bool = False) -> None:
        self.records = records or []
        self.recovered = recovered
        self.message = message
        self.path = path
        self.backup_path = backup_path
        self.migrated = migrated      # True = 读到了旧格式（v1），已就地转成 v2

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"HistoryLoadResult(records={len(self.records)}, recovered={self.recovered}, "
                f"path={self.path})")


def _default_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_DIR


def _default_fallback_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / APP_STORAGE_ID


def _try_json(path: Path) -> Any:
    """尽量读出 JSON；读不到返回 None（只用于判断文件格式，不参与校验）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


class HistoryStore:
    """history.json 读写：优先 exe 同目录，不可写时回退 %LOCALAPPDATA%\\<APP_STORAGE_ID>。"""

    def __init__(self, primary_dir: str | os.PathLike[str] | None = None,
                 fallback_dir: str | os.PathLike[str] | None = None,
                 filename: str = HISTORY_FILENAME,
                 writable_probe: Callable[[Path], bool] | None = None) -> None:
        self.primary_dir = Path(primary_dir) if primary_dir else _default_app_dir()
        self.fallback_dir = Path(fallback_dir) if fallback_dir else _default_fallback_dir()
        self.filename = filename
        self._probe = writable_probe or self._default_probe
        self._resolved_dir: Path | None = None

    @staticmethod
    def _default_probe(directory: Path) -> bool:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".write_probe-{os.getpid()}.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False

    def resolve_dir(self) -> Path:
        if self._resolved_dir is not None:
            return self._resolved_dir
        for candidate in (self.primary_dir, self.fallback_dir):
            try:
                if candidate and self._probe(candidate):
                    self._resolved_dir = candidate
                    return candidate
            except OSError:
                continue
        self._resolved_dir = self.fallback_dir
        return self._resolved_dir

    @property
    def path(self) -> Path:
        return self.resolve_dir() / self.filename

    def load(self) -> HistoryLoadResult:
        path = self.path
        if not path.exists():
            return HistoryLoadResult(records=[], recovered=False,
                                     message="首次运行：将建立基线记录", path=path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return HistoryLoadResult(records=[], recovered=True,
                                     message=f"历史文件读取失败（{exc}），已重建基线", path=path)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            backup = self._backup_broken(path)
            return HistoryLoadResult(records=[], recovered=True, path=path, backup_path=backup,
                                     message=f"history.json 已损坏，已备份为 {backup.name} 并重建基线")

        records, problem = validate_history(data)
        if problem:
            backup = self._backup_broken(path)
            return HistoryLoadResult(records=[], recovered=True, path=path, backup_path=backup,
                                     message=f"history.json 字段异常（{problem}），已备份为 {backup.name} 并重建基线")
        migrated = _history_schema_version(data) < SCHEMA_VERSION
        return HistoryLoadResult(records=records, recovered=False, path=path, message="",
                                 migrated=migrated)

    def _backup_before_trim(self, path: Path) -> Path | None:
        """格式迁移/折叠前先留一份原始文件，出问题用户还能找回旧数据。"""
        stamp = datetime.now().strftime("%Y%m%d")
        backup = path.with_name(f"{HISTORY_BACKUP_PREFIX}{stamp}{path.suffix}")
        try:
            shutil.copyfile(path, backup)
        except OSError:
            return None
        return backup

    def _backup_broken(self, path: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
        try:
            os.replace(path, backup)
        except OSError:
            try:
                backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            except OSError:
                pass
        return backup

    def save(self, records: Sequence[dict]) -> Path:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if path.exists() and _history_schema_version(_try_json(path)) < SCHEMA_VERSION:
            self._backup_before_trim(path)
        payload = history_payload(records)
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return path

    def reset(self) -> Path | None:
        """重置历史文件：先把现有文件改名备份，再写一份空历史。

        返回备份文件路径；原本就没有历史文件时返回 None（那就只是写入空历史）。
        用改名而不是删除：用户点错了还能在原目录里找回 history.reset-<时间戳>.json。
        """
        path = self.path
        backup: Path | None = None
        if path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{HISTORY_RESET_PREFIX}{stamp}{path.suffix}")
            try:
                os.replace(path, backup)
            except OSError:
                # 改名失败（被别的程序占用等）就退回「复制一份再清空」，至少留得住数据
                try:
                    shutil.copyfile(path, backup)
                except OSError:
                    backup = None
        self.save([])
        return backup


def _flatten_v1_records(records: list, prefix: str = "") -> tuple[list[dict], str]:
    """v1：扁平 records 数组，每条就是一次读数。"""
    cleaned: list[dict] = []
    for index, record in enumerate(records):
        label = f"{prefix}第 {index + 1} 条记录"
        if not isinstance(record, dict):
            return [], f"{label}不是对象"
        for name in REQUIRED_RECORD_FIELDS:
            if name not in record:
                return [], f"{label}缺少字段 {name}"
        if timestamp_seconds(record.get("timestamp")) is None:
            return [], f"{label} timestamp 无法解析"
        for name in ("read_gb", "write_gb"):
            value = record.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return [], f"{label} {name} 不是数字"
        item = dict(record)
        if not item.get("key"):
            item["key"] = disk_key(str(item.get("model") or ""), str(item.get("serial") or ""))
        item.setdefault("protocol", "未知")
        item.setdefault("device", "")
        item.setdefault("media_errors", None)
        cleaned.append(item)
    return cleaned, ""


def _flatten_v2_disks(disks: list) -> tuple[list[dict], str]:
    """v2：盘档案 + 读数数组，这里展开回「每条读数一行」的扁平结构。"""
    cleaned: list[dict] = []
    for index, disk in enumerate(disks):
        if not isinstance(disk, dict):
            return [], f"第 {index + 1} 块盘不是对象"
        label = f"第 {index + 1} 块盘的"
        readings = disk.get("readings")
        if not isinstance(readings, list):
            return [], f"{label}读数不是数组"
        model = str(disk.get("model") or "")
        serial = str(disk.get("serial") or "")
        key = str(disk.get("key") or "") or disk_key(model, serial)
        protocol = str(disk.get("protocol") or "未知")
        device = str(disk.get("device") or "")
        for inner, reading in enumerate(readings):
            where = f"{label}第 {inner + 1} 条读数"
            if not isinstance(reading, dict):
                return [], f"{where}不是对象"
            if timestamp_seconds(reading.get("t")) is None:
                return [], f"{where} timestamp 无法解析"
            for name, field_name in (("r", "read_gb"), ("w", "write_gb")):
                value = reading.get(name)
                if value is not None and (isinstance(value, bool)
                                          or not isinstance(value, (int, float))):
                    return [], f"{where} {field_name} 不是数字"
            media = reading.get("e")
            if isinstance(media, bool) or not isinstance(media, (int, float)):
                media = None
            cleaned.append({
                "timestamp": str(reading.get("t")),
                "key": key,
                "model": model,
                "serial": serial,
                "protocol": protocol,
                "device": device,
                "read_gb": reading.get("r"),
                "write_gb": reading.get("w"),
                "media_errors": media,
            })
    return cleaned, ""


def validate_history(data: Any) -> tuple[list[dict], str]:
    """校验 history.json 结构，统一返回扁平记录列表。

    同时接受两种格式：
    * v1：records 数组，每条记录自带型号/序列号；
    * v2：disks 数组，每块盘一份档案 + readings 读数数组。
    返回 (记录列表, 问题描述)，问题描述非空时调用方会备份并重建基线。
    """
    if not isinstance(data, dict):
        return [], "根节点不是对象"
    disks = data.get("disks")
    if disks is not None:
        if not isinstance(disks, list):
            return [], "disks 不是数组"
        return _flatten_v2_disks(disks)
    records = data.get("records")
    if not isinstance(records, list):
        return [], "缺少 records 数组"
    return _flatten_v1_records(records)


def records_from_snapshots(snapshots: Sequence[DiskSnapshot], when: datetime) -> list[dict]:
    """把本次读数转换成 history.json 记录（跳过读取失败的盘）。"""
    stamp = when.isoformat(timespec="seconds")
    records: list[dict] = []
    for snapshot in snapshots:
        if not snapshot.ok:
            continue
        read_gb = snapshot.read_gb
        write_gb = snapshot.write_gb
        records.append({
            "timestamp": stamp,
            "key": snapshot.key,
            "model": snapshot.model,
            "serial": snapshot.serial,
            "protocol": snapshot.protocol,
            "device": snapshot.device_path,
            "read_gb": None if read_gb is None else round(read_gb, 4),
            "write_gb": None if write_gb is None else round(write_gb, 4),
            "media_errors": snapshot.media_errors,
        })
    return records


def latest_timestamp(records: Sequence[dict]) -> str | None:
    best: tuple[float, str] | None = None
    for record in records:
        stamp = record.get("timestamp")
        seconds = timestamp_seconds(stamp)
        if seconds is None:
            continue
        if best is None or seconds > best[0]:
            best = (seconds, str(stamp))
    return best[1] if best else None
