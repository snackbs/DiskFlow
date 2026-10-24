"""磁盘流水 DiskFlow —— 兼容入口（shim）。

真正的实现已经拆到 diskflow/ 包里，按层分模块：

  diskflow.config       单一配置点：软件名称、版本号、容量换算常量、累计量来源标识、健康度规则。
  diskflow.convert      容量换算层：字节 / LBA / NVMe Data Unit → GB，以及各种展示字符串。
  diskflow.timefmt      时间格式化层：ISO 8601 时间戳解析、秒数比较、区间格式化（X 天 Y 小时 Z 分钟）。
  diskflow.models       数据模型：磁盘读取结果与增量对比后的行数据。
  diskflow.parse        数据读取层第一步：把 smartctl 的 JSON 输出解析成 DiskSnapshot（纯函数，便于单测）。
  diskflow.providers    数据读取层第二步：SmartProvider 注入点。
  diskflow.diskmap      盘符映射层：把「物理盘」对应到「本机盘符」（只读查询，失败即降级）。
  diskflow.delta        增量计算层：把本次读数与历史记录对比，算出本次区间增量与历史累计增量，并判定 0E。
  diskflow.history      历史存储层：history.json 的读写、校验、损坏备份与 v1→v2 迁移。
  diskflow.orchestrate  运行编排层：一次完整运行 = 读取 → 记录 → 对比 → 产出 RunOutcome（GUI 与测试共用同一条流水线）。
  diskflow.platformhelp 权限 / 平台辅助：管理员权限检测（不涉及 smartctl）。
  diskflow.theme        主题层：纯数据表 + 解析函数。
  diskflow.viewmodel    数据展示视图定义（三种）：表格 / 卡片 / 双列。纯数据表。
  diskflow.prefs        界面偏好存储层（独立 settings.json，不碰 history.json 的 schema）。
  diskflow.gui          GUI 层（不含任何 smartctl 调用）。
  diskflow.entry        入口：窗口图标、create_app、main。

本文件保留旧版 main.py 的全部公开名字（含以下划线开头的内部工具），
因为构建脚本和测试会通过  python -c "import main as m; print(m.APP_EXE_NAME)"
读取配置。名称仍然只有 diskflow/config.py 一个来源，这里只是再导出，
**禁止在本文件里定义业务逻辑或写死软件名 / 版本号**。

新增模块时：在下面的导入块里加一行。
"""

from __future__ import annotations

# 再导出：测试会沿用旧习惯 monkeypatch main.platform / main.sys；
# 这两个是标准库模块对象，改它们与改 diskflow 里的同名模块等效。
import platform  # noqa: F401
import sys  # noqa: F401

# 让 main.config / main.providers 这类属性访问可用（测试会把 patch 打到这些模块上，
# 因为常量与函数的真实归属地在那儿）。
import diskflow.config as config  # noqa: F401
import diskflow.convert as convert  # noqa: F401
import diskflow.timefmt as timefmt  # noqa: F401
import diskflow.models as models  # noqa: F401
import diskflow.parse as parse  # noqa: F401
import diskflow.providers as providers  # noqa: F401
import diskflow.diskmap as diskmap  # noqa: F401
import diskflow.delta as delta  # noqa: F401
import diskflow.history as history  # noqa: F401
import diskflow.orchestrate as orchestrate  # noqa: F401
import diskflow.platformhelp as platformhelp  # noqa: F401
import diskflow.theme as theme  # noqa: F401
import diskflow.viewmodel as viewmodel  # noqa: F401
import diskflow.prefs as prefs  # noqa: F401
import diskflow.gui as gui  # noqa: F401
import diskflow.entry as entry  # noqa: F401

# 逐模块显式再导出：务必与 diskflow/<模块>.py 的顶层定义保持一致。
from diskflow.config import (  # noqa: F401
    PROJECT_DIR, APP_DISPLAY_NAME, APP_EXE_NAME, APP_ARTIFACT_NAME, APP_STORAGE_ID,
    APP_VERSION, APP_AUTHOR, APP_AI_NOTE, LBA_BYTES, NVME_DATA_UNIT_BYTES, BYTES_PER_GB,
    BYTES_PER_GIB_TEXT, BYTES_PER_TB, ATA_DEVSTAT_READ_NAME, ATA_DEVSTAT_WRITE_NAME,
    COUNTER_SOURCE_NONE, COUNTER_SOURCE_NVME, COUNTER_SOURCE_DEVSTAT,
    COUNTER_SOURCE_ATTR, COUNTER_SOURCE_TEXT, SATA_LIFE_ATTRIBUTES,
    SATA_LIFE_RAW_PERCENT, HEALTH_NOT_AVAILABLE, APP_ICON_FILENAME, APP_ICON_DIRNAME,
    SMARTCTL_EXE, window_title, about_text,
)

from diskflow.convert import (  # noqa: F401
    bytes_to_gb, lbas_to_gb, data_units_to_gb, _as_int, fmt_gb, fmt_delta, fmt_delta_gb,
    fmt_size_gb, fmt_size_short, fmt_bytes_size,
)

from diskflow.timefmt import (  # noqa: F401
    format_duration, format_interval, parse_timestamp, timestamp_seconds,
)

from diskflow.models import (  # noqa: F401
    DiskSnapshot, disk_key, ProviderResult, MediaErrorStatus, DiskRow,
)

from diskflow.parse import (  # noqa: F401
    _ata_attribute, _ata_normalized, _device_statistic, _sata_life_percent,
    _normalize_protocol, _smartctl_message_text, parse_smartctl_payload,
)

from diskflow.providers import (  # noqa: F401
    locate_smartctl, _default_run_command, locate_app_icon, SmartctlProvider,
    FixturesProvider,
)

from diskflow.diskmap import (  # noqa: F401
    IOCTL_STORAGE_QUERY_PROPERTY, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
    IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, STORAGE_DEVICE_PROPERTY,
    GEOMETRY_DISK_SIZE_OFFSET, DESCRIPTOR_BUFFER_SIZE, DISK_EXTENT_RECORD_SIZE,
    PHYSICAL_DISK_LIMIT, DRIVE_LETTER_JOINER, DRIVE_LETTER_UNKNOWN,
    CAPACITY_REL_TOLERANCE, CAPACITY_ABS_TOLERANCE, BUS_TYPE_NVME, BUS_TYPE_ATA,
    BUS_TYPE_SATA, _DEVICE_API_CACHE, PhysicalDisk, VolumeMount, _normalize_model_text,
    capacities_close, models_close, _wchar_buffer_text, parse_storage_descriptor,
    parse_disk_geometry, parse_volume_extents, _win_device_api, _device_ioctl,
    _query_descriptor, query_volume_identity, query_disk_descriptor, query_volume_disks,
    volume_capacity, list_physical_disks, list_volume_mounts, score_disk_match,
    drive_letters_by_disk, match_drive_letters, locate_drive_letters,
    attach_drive_letters,
)

from diskflow.delta import (  # noqa: F401
    evaluate_media_errors, _record_gb, _index_history, build_rows, _record_ts_hint,
    _record_0e, DELTA_EPSILON_GB, _diff, summarize, any_media_alert,
)

from diskflow.history import (  # noqa: F401
    HISTORY_FILENAME, SCHEMA_VERSION, SCHEMA_VERSION_LEGACY, REQUIRED_RECORD_FIELDS,
    HISTORY_TRIM_HOURS, HISTORY_BACKUP_PREFIX, HISTORY_RESET_PREFIX,
    _history_schema_version, _reading_day,
    trim_readings, history_payload, count_history_readings, HistoryLoadResult,
    _default_app_dir, _default_fallback_dir, _try_json, HistoryStore,
    _flatten_v1_records, _flatten_v2_disks, validate_history, records_from_snapshots,
    latest_timestamp,
)

from diskflow.orchestrate import (  # noqa: F401
    RunOutcome, perform_run,
)

from diskflow.platformhelp import (  # noqa: F401
    is_admin,
)

from diskflow.theme import (  # noqa: F401
    THEME_SYSTEM, THEME_LEDGER, THEME_DARK, THEME_GAUGE, THEME_MINIMAL, THEME_NATIVE,
    THEME_ORDER, THEME_LABELS, THEME_SYSTEM_LIGHT, THEME_SYSTEM_DARK, REG_PERSONALIZE,
    REG_LIGHT_VALUE, THEMES, detect_system_dark_mode, normalize_theme_key,
    system_theme_name, resolve_theme,
)

from diskflow.viewmodel import (  # noqa: F401
    VIEW_TABLE, VIEW_CARDS, VIEW_GRID, VIEW_ORDER, VIEW_LABELS, DEFAULT_VIEW,
    CARD_METRICS, CARD_METRIC_COLUMNS, CARD_METRIC_COLUMNS_GRID, normalize_view_key,
)

from diskflow.prefs import (  # noqa: F401
    SETTINGS_FILENAME, DEFAULT_SETTINGS, SettingsStore,
)

from diskflow.gui import (  # noqa: F401
    _SCROLLBAR_BASE, _LABEL_BASE, SCROLL_GUTTER, CARD_RESIZE_DELAY_MS, CARD_GAP,
    SlimScrollbar, SegButton, SORT_MENU_ITEMS, COLUMN_SPECS, TABLE_NATURAL_WIDTH,
    ROW_ALERT_TAG, ROW_MUTED_TAG, ROW_ZEBRA_TAG, ALERT_COLOR, MUTED_COLOR, BASE_FONT,
    SMARTCTL_INSTALL_HINT, KPI_SPECS, fmt_delta_compact, _default_geometry,
    STATUS_TEXT_LIMIT, _truncate, PAIR_SEPARATOR, TSV_PAIR_SEPARATOR, _pair,
    _short_serial, _row_values, _row_sort_value, DiskTrackerApp,
)

from diskflow.entry import (  # noqa: F401
    apply_window_icon, create_app, main,
)


__all__ = [
    "PROJECT_DIR", "APP_DISPLAY_NAME", "APP_EXE_NAME", "APP_ARTIFACT_NAME",
    "APP_STORAGE_ID", "APP_VERSION", "APP_AUTHOR", "APP_AI_NOTE", "LBA_BYTES",
    "NVME_DATA_UNIT_BYTES", "BYTES_PER_GB", "BYTES_PER_GIB_TEXT", "BYTES_PER_TB",
    "ATA_DEVSTAT_READ_NAME", "ATA_DEVSTAT_WRITE_NAME", "COUNTER_SOURCE_NONE",
    "COUNTER_SOURCE_NVME", "COUNTER_SOURCE_DEVSTAT", "COUNTER_SOURCE_ATTR",
    "COUNTER_SOURCE_TEXT", "SATA_LIFE_ATTRIBUTES", "SATA_LIFE_RAW_PERCENT",
    "HEALTH_NOT_AVAILABLE", "APP_ICON_FILENAME", "APP_ICON_DIRNAME", "SMARTCTL_EXE",
    "window_title", "about_text", "bytes_to_gb", "lbas_to_gb", "data_units_to_gb",
    "fmt_gb", "fmt_delta", "fmt_delta_gb", "fmt_size_gb", "fmt_size_short",
    "fmt_bytes_size",
    "format_duration", "format_interval", "parse_timestamp", "timestamp_seconds",
    "DiskSnapshot", "disk_key", "ProviderResult", "MediaErrorStatus", "DiskRow",
    "parse_smartctl_payload", "locate_smartctl", "locate_app_icon", "SmartctlProvider",
    "FixturesProvider", "IOCTL_STORAGE_QUERY_PROPERTY",
    "IOCTL_DISK_GET_DRIVE_GEOMETRY_EX", "IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS",
    "STORAGE_DEVICE_PROPERTY", "GEOMETRY_DISK_SIZE_OFFSET", "DESCRIPTOR_BUFFER_SIZE",
    "DISK_EXTENT_RECORD_SIZE", "PHYSICAL_DISK_LIMIT", "DRIVE_LETTER_JOINER",
    "DRIVE_LETTER_UNKNOWN", "CAPACITY_REL_TOLERANCE", "CAPACITY_ABS_TOLERANCE",
    "BUS_TYPE_NVME", "BUS_TYPE_ATA", "BUS_TYPE_SATA", "PhysicalDisk", "VolumeMount",
    "capacities_close", "models_close", "parse_storage_descriptor",
    "parse_disk_geometry", "parse_volume_extents", "query_volume_identity",
    "query_disk_descriptor", "query_volume_disks", "volume_capacity",
    "list_physical_disks", "list_volume_mounts", "score_disk_match",
    "drive_letters_by_disk", "match_drive_letters", "locate_drive_letters",
    "attach_drive_letters", "evaluate_media_errors", "build_rows", "DELTA_EPSILON_GB",
    "summarize", "any_media_alert", "HISTORY_FILENAME", "SCHEMA_VERSION",
    "SCHEMA_VERSION_LEGACY", "REQUIRED_RECORD_FIELDS", "HISTORY_TRIM_HOURS",
    "HISTORY_BACKUP_PREFIX", "HISTORY_RESET_PREFIX", "trim_readings", "history_payload",
    "count_history_readings", "HistoryLoadResult", "HistoryStore", "validate_history",
    "records_from_snapshots", "latest_timestamp", "RunOutcome", "perform_run",
    "is_admin", "THEME_SYSTEM", "THEME_LEDGER", "THEME_DARK", "THEME_GAUGE",
    "THEME_MINIMAL", "THEME_NATIVE", "THEME_ORDER", "THEME_LABELS", "THEME_SYSTEM_LIGHT",
    "THEME_SYSTEM_DARK", "REG_PERSONALIZE", "REG_LIGHT_VALUE", "THEMES",
    "detect_system_dark_mode", "normalize_theme_key", "system_theme_name",
    "resolve_theme", "VIEW_TABLE", "VIEW_CARDS", "VIEW_GRID", "VIEW_ORDER",
    "VIEW_LABELS", "DEFAULT_VIEW", "CARD_METRICS", "CARD_METRIC_COLUMNS",
    "CARD_METRIC_COLUMNS_GRID", "normalize_view_key", "SETTINGS_FILENAME",
    "DEFAULT_SETTINGS", "SettingsStore", "SCROLL_GUTTER", "CARD_RESIZE_DELAY_MS",
    "CARD_GAP", "SlimScrollbar", "SegButton", "SORT_MENU_ITEMS", "COLUMN_SPECS",
    "TABLE_NATURAL_WIDTH", "ROW_ALERT_TAG", "ROW_MUTED_TAG", "ROW_ZEBRA_TAG",
    "ALERT_COLOR", "MUTED_COLOR", "BASE_FONT", "SMARTCTL_INSTALL_HINT", "KPI_SPECS",
    "fmt_delta_compact", "STATUS_TEXT_LIMIT", "PAIR_SEPARATOR", "TSV_PAIR_SEPARATOR",
    "DiskTrackerApp", "apply_window_icon", "create_app", "main",
]


if __name__ == "__main__":  # pragma: no cover - 直接 python main.py 时进 GUI
    raise SystemExit(main())
