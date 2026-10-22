"""test_disk_tracker.py —— 磁盘流水 DiskFlow 的 pytest 用例。

全部用例都可在无真实硬盘、无 smartctl 的环境下运行（CI windows-latest 真实执行）：
数据源通过 FixturesProvider / 假 provider 注入，历史文件写在 pytest 的 tmp_path 里。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import main as app

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TZ8 = timezone(timedelta(hours=8))

NVME_KEY = "SAMSUNG SSD 980 PRO 1TB|S6B0NL0R912345A"
SATA_KEY = "CRUCIAL MX500 1TB|2108E5A1B2C3"

# fixtures 样本的精确换算值（供断言使用）
NVME_READ_GB = 13314398 * 512000 / 1024 ** 3      # 6348.799705505371
NVME_WRITE_GB = 17120410 * 512000 / 1024 ** 3     # 8163.647651672363
# SATA 盘的累计读写以 ata_device_statistics 的 Logical Sectors 为准（512 字节扇区）；
# fixture 里故意把 SMART 241/242 写成小得多的值，用来证明解析器优先取 Device Statistics。
SATA_READ_GB = 68336000000 * 512 / 1024 ** 3      # 32585.14404296875
SATA_WRITE_GB = 41624000000 * 512 / 1024 ** 3     # 19847.869873046875
# 仅当 Device Statistics 缺失时，才退回 SMART 241/242（这里按 512 字节/LBA 假设）
SATA_ATTR_READ_GB = 2147483648 * 512 / 1024 ** 3  # 1024.0
SATA_ATTR_WRITE_GB = 3274915840 * 512 / 1024 ** 3 # 1561.6015625


# ---------------------------------------------------------------------------
# 1. 容量换算单测
# ---------------------------------------------------------------------------


def test_lba_to_gb_uses_512_bytes():
    """1 LBA = 512 字节：2048 个 LBA 恰好 1 MiB = 1/1024 GB。"""
    assert app.lbas_to_gb(2048) == pytest.approx(1 / 1024)
    assert app.lbas_to_gb(2097152) == pytest.approx(1.0)


def test_data_unit_to_gb_uses_512000_bytes():
    """1 NVMe Data Unit = 512000 字节。"""
    assert app.data_units_to_gb(1) == pytest.approx(512000 / 1024 ** 3)
    # 2097152 个 Data Unit = 2097152 * 512000 B = 1_073_741_824_000 B = 正好 1000 GiB
    assert app.data_units_to_gb(2097152) == pytest.approx(1000.0)
    assert app.data_units_to_gb(2097152) == pytest.approx(2097152 * 512000 / 1024 ** 3)
    # 1 TiB = 1024 GB 对应 2147483.648 个 Data Unit：计数器是整数，所以略低于 1024 GB
    assert app.data_units_to_gb(2147484) > 1024.0
    assert app.data_units_to_gb(2147483) < 1024.0
    assert app.data_units_to_gb(2147483) == pytest.approx(1024.0, rel=1e-6)


def test_bytes_to_gb_uses_1024_power_3():
    """界面注明 1 GB = 1024^3 字节。"""
    assert app.bytes_to_gb(1024 ** 3) == pytest.approx(1.0)
    assert app.bytes_to_gb(0) == 0.0


def test_conversion_boundaries_do_not_overflow():
    """边界值：0 与极大值都不溢出、不抛异常。"""
    huge_lba = 2 ** 63 - 1
    huge_units = 2 ** 63 - 1
    assert app.lbas_to_gb(0) == 0.0
    assert app.data_units_to_gb(0) == 0.0
    assert app.bytes_to_gb(0) == 0.0

    huge = app.lbas_to_gb(huge_lba)
    assert huge is not None and huge > 1e12
    assert app.data_units_to_gb(huge_units) is not None
    assert app.bytes_to_gb(2 ** 63) is not None

    # 非法输入统一返回 None，而不是抛异常
    for bad in (None, "", "   ", "abc", True, False):
        assert app.lbas_to_gb(bad) is None
        assert app.data_units_to_gb(bad) is None
        assert app.bytes_to_gb(bad) is None


def test_as_int_parses_smartctl_raw_strings():
    """smartctl 的 raw 可能带单位后缀，只取前导数字。"""
    assert app._as_int("1234 (1.2 TB)") == 1234
    assert app._as_int(42) == 42
    assert app._as_int("  7 ") == 7
    assert app._as_int("") is None
    assert app._as_int(True) is None


# ---------------------------------------------------------------------------
# 2. 增量计算 + 时间格式化单测
# ---------------------------------------------------------------------------


def test_format_duration_basic():
    assert app.format_duration(0) == "0 天 0 小时 0 分钟"
    assert app.format_duration(90) == "0 天 0 小时 1 分钟"
    assert app.format_duration(3600) == "0 天 1 小时 0 分钟"
    assert app.format_duration(86400 + 3600 + 60) == "1 天 1 小时 1 分钟"
    assert app.format_duration(5 * 86400 + 23 * 3600 + 59 * 60) == "5 天 23 小时 59 分钟"


def test_format_interval_handles_clock_rollback():
    """时钟回拨（本次早于上次）必须显式标注，而不是显示负数或崩溃。"""
    assert app.format_interval(None) == "—"
    assert app.format_interval(0) == "0 天 0 小时 0 分钟"
    rolled_back = app.format_interval(-3660)
    assert rolled_back.startswith("时钟回拨")
    assert "0 天 1 小时 1 分钟" in rolled_back


def test_interval_across_day_and_month_boundaries():
    """跨天、跨月的时间差。"""
    start = datetime(2026, 4, 30, 8, 0, tzinfo=TZ8)
    same_day_later = datetime(2026, 4, 30, 20, 30, tzinfo=TZ8)
    across_day = datetime(2026, 5, 1, 9, 15, tzinfo=TZ8)
    across_month = datetime(2026, 5, 2, 10, 30, tzinfo=TZ8)

    assert app.format_interval((same_day_later - start).total_seconds()) == "0 天 12 小时 30 分钟"
    assert app.format_interval((across_day - start).total_seconds()) == "1 天 1 小时 15 分钟"
    assert app.format_interval((across_month - start).total_seconds()) == "2 天 2 小时 30 分钟"


def test_timestamp_helpers_accept_iso_with_z():
    assert app.parse_timestamp("2026-04-30T08:00:00Z") is not None
    assert app.parse_timestamp("not-a-time") is None
    assert app.timestamp_seconds("2026-04-30T08:00:00+08:00") is not None
    assert app.timestamp_seconds("2026-04-30T08:00:00Z") is not None
    assert app.timestamp_seconds("bad") is None


def _history_record(key, model, serial, read_gb, write_gb, when, media=None, protocol="NVMe"):
    return {
        "timestamp": when,
        "key": key,
        "model": model,
        "serial": serial,
        "protocol": protocol,
        "device": "/dev/x",
        "read_gb": read_gb,
        "write_gb": write_gb,
        "media_errors": media,
    }


def test_delta_interval_and_history_totals():
    """区间增量 + 历史累计增量（相对首次记录）。"""
    history = [
        _history_record(NVME_KEY, "Samsung SSD 980 PRO 1TB", "S6B0NL0R912345A",
                        5000.0, 7000.0, "2026-04-01T09:00:00+08:00", media=0),
        _history_record(NVME_KEY, "Samsung SSD 980 PRO 1TB", "S6B0NL0R912345A",
                        6000.0, 7500.0, "2026-04-30T08:00:00+08:00", media=0),
    ]
    snapshot = app.DiskSnapshot(
        device_path="/dev/nvme0", protocol="NVMe",
        model="Samsung SSD 980 PRO 1TB", serial="S6B0NL0R912345A",
        read_bytes=13314398 * 512000, write_bytes=17120410 * 512000, media_errors=0,
    )
    snapshot._now_hint = "2026-05-02T10:30:00+08:00"

    rows = app.build_rows(history, [snapshot])
    assert len(rows) == 1
    row = rows[0]
    assert row.interval_text == "2 天 2 小时 30 分钟"
    assert row.read_delta_gb == pytest.approx(NVME_READ_GB - 6000.0)
    assert row.write_delta_gb == pytest.approx(NVME_WRITE_GB - 7500.0)
    assert row.hist_read_delta_gb == pytest.approx(NVME_READ_GB - 5000.0)
    assert row.hist_write_delta_gb == pytest.approx(NVME_WRITE_GB - 7000.0)

    totals = app.summarize(rows)
    assert totals["read_delta"] == pytest.approx(NVME_READ_GB - 6000.0)
    assert totals["hist_write_delta"] == pytest.approx(NVME_WRITE_GB - 7000.0)


def test_clock_rollback_between_runs_is_reported():
    """本次时间早于上次时不产生负的间隔文本，而是标注时钟回拨。"""
    history = [_history_record(app.disk_key("M", "S"), "M", "S", 100.0, 200.0,
                               "2026-05-02T10:00:00+08:00")]
    snapshot = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe", model="M", serial="S",
                                read_bytes=200 * 1024 ** 3, write_bytes=400 * 1024 ** 3,
                                media_errors=0)
    snapshot._now_hint = "2026-05-02T09:00:00+08:00"
    rows = app.build_rows(history, [snapshot])
    assert rows[0].interval_text.startswith("时钟回拨")
    # 增量本身仍然是"本次 - 上次"，不因时间回拨而变号
    assert rows[0].read_delta_gb == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 3. 0E 判定单测
# ---------------------------------------------------------------------------


def test_media_errors_stable():
    status = app.evaluate_media_errors("NVMe", 3, 3)
    assert status.text == "稳定"
    assert status.kind == "stable"
    assert status.is_alert is False


def test_media_errors_increased():
    status = app.evaluate_media_errors("NVMe", 2, 5)
    assert status.text == "⚠ 0E 新增 3"
    assert status.kind == "increased"
    assert status.is_alert is True


def test_media_errors_from_none_to_value():
    """历史没有 0E 字段（None）而本次有值 -> 从无到有，必须告警。"""
    status = app.evaluate_media_errors("NVMe", None, 4)
    assert status.text == "⚠ 0E 新增 4"
    assert status.kind == "first_seen"
    assert status.is_alert is True
    # 从无到有但值为 0：不算告警
    zero = app.evaluate_media_errors("NVMe", None, 0)
    assert zero.text == "稳定"
    assert zero.is_alert is False


def test_media_errors_not_applicable_for_sata():
    """SATA 盘没有 Media and Data Integrity Errors 字段，固定标注"不适用"。"""
    for previous, current in ((0, 0), (None, 7), (2, 5)):
        status = app.evaluate_media_errors("SATA", previous, current)
        assert status.text == "不适用"
        assert status.kind == "na"
        assert status.is_alert is False


def test_media_errors_unavailable_when_field_missing():
    status = app.evaluate_media_errors("NVMe", 1, None)
    assert status.text == "不可用"
    assert status.is_alert is False


# ---------------------------------------------------------------------------
# 4. 磁盘匹配单测
# ---------------------------------------------------------------------------


def test_same_model_different_serial_are_two_disks():
    key_a = app.disk_key("Samsung SSD 980 PRO 1TB", "SN-AAA")
    key_b = app.disk_key("Samsung SSD 980 PRO 1TB", "SN-BBB")
    assert key_a != key_b

    history = [_history_record(key_a, "Samsung SSD 980 PRO 1TB", "SN-AAA", 10.0, 20.0,
                               "2026-04-30T08:00:00+08:00")]
    snap_a = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe",
                              model="Samsung SSD 980 PRO 1TB", serial="SN-AAA",
                              read_bytes=20 * 1024 ** 3, write_bytes=30 * 1024 ** 3, media_errors=0)
    snap_b = app.DiskSnapshot(device_path="/dev/nvme1", protocol="NVMe",
                              model="Samsung SSD 980 PRO 1TB", serial="SN-BBB",
                              read_bytes=20 * 1024 ** 3, write_bytes=30 * 1024 ** 3, media_errors=0)
    snap_a._now_hint = snap_b._now_hint = "2026-05-01T08:00:00+08:00"

    rows = {r.serial: r for r in app.build_rows(history, [snap_a, snap_b])}
    assert set(rows) == {"SN-AAA", "SN-BBB"}
    # 有历史的那块算得出增量，另一块是新盘
    assert rows["SN-AAA"].read_delta_gb == pytest.approx(10.0)
    assert rows["SN-BBB"].read_delta_gb is None
    assert rows["SN-BBB"].status_text == "新盘首次记录，暂无增量对比"


def test_device_path_change_but_same_serial_matches_same_disk():
    """盘符/设备路径变化不影响匹配：只认 型号+序列号。"""
    history = [_history_record(NVME_KEY, "Samsung SSD 980 PRO 1TB", "S6B0NL0R912345A",
                               100.0, 200.0, "2026-04-30T08:00:00+08:00")]
    snapshot = app.DiskSnapshot(device_path="/dev/sdq", protocol="NVMe",
                                model="Samsung SSD 980 PRO 1TB", serial="S6B0NL0R912345A",
                                read_bytes=150 * 1024 ** 3, write_bytes=260 * 1024 ** 3,
                                media_errors=0)
    snapshot._now_hint = "2026-05-01T08:00:00+08:00"
    rows = app.build_rows(history, [snapshot])
    assert len(rows) == 1
    assert rows[0].read_delta_gb == pytest.approx(50.0)
    assert rows[0].write_delta_gb == pytest.approx(60.0)
    assert rows[0].status_text != "新盘首次记录，暂无增量对比"


def test_vanished_disk_is_listed_as_missing():
    """历史里有、本次没读到的盘要被列出来（灰色），不参与增量汇总。"""
    history = [_history_record(SATA_KEY, "Crucial MX500 1TB", "2108E5A1B2C3",
                               900.0, 1200.0, "2026-04-30T08:00:00+08:00", protocol="SATA")]
    rows = app.build_rows(history, [])
    assert len(rows) == 1
    assert rows[0].vanished is True
    assert "本次未检测到" in rows[0].status_text
    assert app.summarize(rows)["read_delta"] == 0.0


# ---------------------------------------------------------------------------
# 1b. 双单位显示（GB / TB）与健康度
# ---------------------------------------------------------------------------


def test_fmt_size_gb_switches_unit_at_one_tb():
    """>= 1 TB（1024 GB）时同时给出 TB 与 GB，否则只给 GB。"""
    assert app.fmt_size_gb(0) == "0.00 GB"
    assert app.fmt_size_gb(512.5) == "512.50 GB"
    assert app.fmt_size_gb(1023.99) == "1,023.99 GB"      # 未到 1 TB 仍是 GB
    assert app.fmt_size_gb(1024) == "1.00 TB (1,024.00 GB)"
    assert app.fmt_size_gb(32567.42) == "31.80 TB (32,567.42 GB)"
    assert app.fmt_size_gb(None) == "—"


def test_fmt_size_gb_keeps_both_units_for_huge_values():
    """极大值不溢出，且 TB/GB 两个数字都能读出来。"""
    huge = 1024.0 * 1024.0          # 1048576 GB = 1024 TB
    text = app.fmt_size_gb(huge)
    assert text == "1,024.00 TB (1,048,576.00 GB)"
    assert app.fmt_size_gb(0.0) == "0.00 GB"


def test_health_text_nvme_uses_100_minus_percentage_used():
    snapshot = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe",
                                model="M", serial="S", read_bytes=0, write_bytes=0,
                                life_percent=93)
    assert snapshot.health_text == "93%"


def test_health_text_is_not_available_when_disk_does_not_report_it():
    """硬盘没报寿命就显示"不适用"，绝不推算。"""
    snapshot = app.DiskSnapshot(device_path="/dev/sdb", protocol="SATA",
                                model="M", serial="S", read_bytes=0, write_bytes=0)
    assert snapshot.life_percent is None
    assert snapshot.health_text == app.HEALTH_NOT_AVAILABLE == "不适用"
    failed = app.DiskSnapshot(device_path="/dev/sdc", protocol="SATA", error="读取失败：x")
    assert failed.health_text == "—"


def test_sata_life_percent_prefers_raw_percent_then_normalized():
    """231/202/233/169 的 raw 就是剩余百分比；177 只有归一化值可信。"""
    table = [
        {"id": 177, "name": "Wear_Leveling_Count", "value": 88,
         "raw": {"value": 12, "string": "12"}},
    ]
    assert app._sata_life_percent(table) == 88

    table_231 = [
        {"id": 231, "name": "SSD_Life_Left", "value": 5, "raw": {"value": 92, "string": "092"}},
    ]
    assert app._sata_life_percent(table_231) == 92

    assert app._sata_life_percent([]) is None


# ---------------------------------------------------------------------------
# 解析层：fixtures 样本
# ---------------------------------------------------------------------------


def test_parse_nvme_fixture():
    payload = json.loads((FIXTURES / "smartctl_nvme_ssd.json").read_text(encoding="utf-8"))
    snapshot = app.parse_smartctl_payload(payload, device_path="/dev/nvme0")
    assert snapshot.ok
    assert snapshot.protocol == "NVMe"
    assert snapshot.model == "Samsung SSD 980 PRO 1TB"
    assert snapshot.serial == "S6B0NL0R912345A"
    assert snapshot.media_errors == 0
    assert snapshot.read_gb == pytest.approx(NVME_READ_GB)
    assert snapshot.write_gb == pytest.approx(NVME_WRITE_GB)
    assert snapshot.key == NVME_KEY
    assert snapshot.counter_source == app.COUNTER_SOURCE_NVME
    # fixtures 里 percentage_used = 7 -> 剩余寿命 93%
    assert snapshot.life_percent == 93
    assert snapshot.health_text == "93%"


def test_nvme_counter_source_set_when_only_read_is_reported():
    """NVMe 只报 data_units_read（没有 written）时，来源仍应标成 NvmeLog。"""
    payload = {
        "device": {"name": "/dev/nvme1", "protocol": "NVMe"},
        "model_name": "Partial NVMe",
        "serial_number": "PARTIAL01",
        "nvme_smart_health_information_log": {"data_units_read": 1000},
    }
    snapshot = app.parse_smartctl_payload(payload, device_path="/dev/nvme1")
    assert snapshot.counter_source == app.COUNTER_SOURCE_NVME
    assert snapshot.read_gb == pytest.approx(1000 * 512000 / 1024 ** 3)
    assert snapshot.write_gb is None


def test_parse_sata_fixture_has_no_media_errors():
    payload = json.loads((FIXTURES / "smartctl_sata_ssd.json").read_text(encoding="utf-8"))
    snapshot = app.parse_smartctl_payload(payload, device_path="/dev/sdb")
    assert snapshot.ok
    assert snapshot.protocol == "SATA"
    assert snapshot.media_errors is None
    assert snapshot.read_gb == pytest.approx(SATA_READ_GB)
    assert snapshot.write_gb == pytest.approx(SATA_WRITE_GB)
    assert snapshot.reallocated == 0
    assert snapshot.pending == 0
    # SATA 盘没有 0E；健康度取自属性 231 的 raw（92），不是"不适用"
    assert snapshot.health_text == "92%"


def test_sata_uses_device_statistics_over_smart_attributes():
    """SATA 累计读写必须优先取 ata_device_statistics，属性 241/242 只在缺失时兜底。

    回归 bug：某些主控的 241/242 不是按 512 字节 LBA 计数（按 16/32 MiB 块），
    直接乘 512 会把 32 TB 读量显示成 1 GB 量级。
    """
    payload = json.loads((FIXTURES / "smartctl_sata_ssd.json").read_text(encoding="utf-8"))
    snapshot = app.parse_smartctl_payload(payload, device_path="/dev/sdb")
    assert snapshot.counter_source == app.COUNTER_SOURCE_DEVSTAT
    # Device Statistics 的两个值确实存在且被采用
    assert app._device_statistic(payload, app.ATA_DEVSTAT_READ_NAME) == 68336000000
    assert app._device_statistic(payload, app.ATA_DEVSTAT_WRITE_NAME) == 41624000000
    assert snapshot.read_gb > SATA_ATTR_READ_GB * 10      # 远大于属性口径，证明没走 241/242
    assert snapshot.write_gb > SATA_ATTR_WRITE_GB * 10


def test_sata_falls_back_to_attributes_and_flags_it():
    """没有 ata_device_statistics 时退回 241/242，并标记来源以便界面提示"可能偏小"。"""
    payload = json.loads((FIXTURES / "sata_attr_only.json").read_text(encoding="utf-8"))
    snapshot = app.parse_smartctl_payload(payload, device_path="/dev/sdd")
    assert snapshot.ok
    assert snapshot.counter_source == app.COUNTER_SOURCE_ATTR
    assert snapshot.read_gb == pytest.approx(4096 * 512 / 1024 ** 3)
    assert snapshot.write_gb == pytest.approx(2048 * 512 / 1024 ** 3)
    # 177 的 raw 是磨损循环次数(12)，只有归一化 value(88) 才是剩余寿命
    assert snapshot.life_percent == 88
    assert snapshot.health_text == "88%"


def test_parse_real_captured_sata_fixture():
    """真机抓样的 SATA 盘（smartctl -x -a --json，仅脱敏）必须解析正确。

    这份样本固化了三条来自真实设备的约束，手造 JSON 覆盖不到：
    1. smartctl.exit_status = 64 = bit6「错误日志有记录」，**不是读取失败**，
       只有 bit0/bit1 才代表设备打不开；
    2. ata_device_statistics.pages 里会有 8 个一模一样的 "General Statistics" 页，
       取首个匹配即可，不需要去重；
    3. 每个统计项都带 offset/size/flags 六个字段，名称匹配必须容忍这些额外键。
    """
    payload = json.loads((FIXTURES / "sata_real_960gb.json").read_text(encoding="utf-8"))
    assert payload["smartctl"]["exit_status"] == 64

    snapshot = app.parse_smartctl_payload(payload, device_path="/dev/sdc")
    assert snapshot.ok is True
    assert snapshot.error is None
    assert snapshot.protocol == "SATA"
    assert snapshot.model == "Sample SATA SSD 960GB"
    assert snapshot.serial == "S5SAMPLE0001"
    assert snapshot.media_errors is None          # SATA 无 0E
    assert snapshot.reallocated == 0
    assert snapshot.pending == 0
    assert snapshot.life_percent == 100           # 只有 177 的归一化值可用
    assert snapshot.health_text == "100%"
    assert snapshot.counter_source == app.COUNTER_SOURCE_DEVSTAT
    assert snapshot.read_gb == pytest.approx(153379078144 * 512 / 1024 ** 3)
    assert snapshot.write_gb == pytest.approx(34505424896 * 512 / 1024 ** 3)
    assert app.fmt_size_gb(snapshot.read_gb) == "71.42 TB (73,136.84 GB)"
    assert app.fmt_size_gb(snapshot.write_gb) == "16.07 TB (16,453.47 GB)"


def test_real_capture_duplicate_general_statistics_pages_are_harmless():
    """真机上 General Statistics 页会重复 8 次，取首个匹配就必须拿到同一个值。"""
    payload = json.loads((FIXTURES / "sata_real_960gb.json").read_text(encoding="utf-8"))
    pages = payload["ata_device_statistics"]["pages"]
    assert len(pages) == 8
    assert {page["name"] for page in pages} == {"General Statistics"}

    values = {
        entry["name"]: entry["value"]
        for page in pages
        for entry in page["table"]
        if entry["name"] in (app.ATA_DEVSTAT_READ_NAME.title(), "Logical Sectors Read")
    }
    assert values  # 每个页里都有 Logical Sectors Read
    assert app._device_statistic(payload, app.ATA_DEVSTAT_READ_NAME) == 153379078144
    assert app._device_statistic(payload, app.ATA_DEVSTAT_WRITE_NAME) == 34505424896


def test_real_capture_shows_attr_241_242_use_32mib_blocks():
    """真实盘上 241/242 是按 32 MiB 块计数的，与 Logical Sectors 精确相差 65536 倍。

    这是「必须优先 Device Statistics、241/242 只能兜底」的实测依据：
    若按 512 字节/LBA 解释，71 TB 的读取量会被显示成 1.12 GB。
    """
    payload = json.loads((FIXTURES / "sata_real_960gb.json").read_text(encoding="utf-8"))
    table = payload["ata_smart_attributes"]["table"]
    attr_read = app._ata_attribute(table, 242)
    attr_write = app._ata_attribute(table, 241)
    devstat_read = app._device_statistic(payload, app.ATA_DEVSTAT_READ_NAME)
    devstat_write = app._device_statistic(payload, app.ATA_DEVSTAT_WRITE_NAME)

    assert attr_read == 2340379
    assert attr_write == 526511
    assert devstat_read / attr_read == pytest.approx(65536)
    assert devstat_write / attr_write == pytest.approx(65536)
    # 按 512 字节解释属性 242 会得到荒谬的小值，正是要避免的回归
    assert attr_read * 512 / 1024 ** 3 < 2


def test_parse_read_failure_fixture():
    payload = json.loads((FIXTURES / "smartctl_read_failure.json").read_text(encoding="utf-8"))
    snapshot = app.parse_smartctl_payload(payload, device_path="/dev/sdc")
    assert snapshot.ok is False
    assert "读取失败" in snapshot.error


def test_fixtures_provider_reads_three_disks():
    result = app.FixturesProvider(FIXTURES).read_all()
    assert len(result.snapshots) == 3
    ok = [s for s in result.snapshots if s.ok]
    failed = [s for s in result.snapshots if not s.ok]
    assert len(ok) == 2
    assert len(failed) == 1
    assert failed[0].protocol == "SATA"


def test_locate_smartctl_never_raises_when_missing():
    """找不到 smartctl 时返回 None（界面据此提示 winget 安装），不抛异常。"""
    located = app.locate_smartctl(extra_candidates=[r"Z:\definitely\not\here\smartctl.exe"])
    assert located is None or Path(located).is_file()


# ---------------------------------------------------------------------------
# 5. 历史存储单测
# ---------------------------------------------------------------------------


def _store(tmp_path, **kwargs):
    primary = tmp_path / "app"
    fallback = tmp_path / "localappdata" / app.APP_STORAGE_ID
    return app.HistoryStore(primary_dir=primary, fallback_dir=fallback, **kwargs)


def test_history_append_records(tmp_path):
    store = _store(tmp_path)
    assert store.load().records == []

    when = datetime(2026, 4, 30, 8, 0, tzinfo=TZ8)
    snap = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe", model="M", serial="S",
                            read_bytes=10 * 1024 ** 3, write_bytes=20 * 1024 ** 3, media_errors=0)
    store.save(app.records_from_snapshots([snap], when))

    when2 = when + timedelta(days=1)
    snap2 = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe", model="M", serial="S",
                             read_bytes=15 * 1024 ** 3, write_bytes=26 * 1024 ** 3, media_errors=0)
    loaded = store.load()
    store.save(loaded.records + app.records_from_snapshots([snap2], when2))

    records = store.load().records
    assert len(records) == 2
    assert records[0]["read_gb"] == pytest.approx(10.0)
    assert records[1]["read_gb"] == pytest.approx(15.0)
    assert records[0]["timestamp"].startswith("2026-04-30T08:00:00")
    assert records[0]["key"] == app.disk_key("M", "S")
    assert records[0]["media_errors"] == 0
    assert records[0]["protocol"] == "NVMe"


def test_history_corrupt_file_is_backed_up_and_rebuilt(tmp_path):
    store = _store(tmp_path)
    path = store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "history_corrupt.json", path)

    result = store.load()
    assert result.recovered is True
    assert result.records == []
    assert "已备份" in result.message
    backups = list(path.parent.glob("history.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0
    assert not path.exists()

    # 重建基线后可正常写入
    store.save([])
    assert store.load().records == []


def test_history_missing_fields_are_backed_up(tmp_path):
    store = _store(tmp_path)
    path = store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "history_missing_fields.json", path)

    result = store.load()
    assert result.recovered is True
    assert result.records == []
    assert "字段异常" in result.message
    assert list(path.parent.glob("history.corrupt-*.json"))


def test_history_falls_back_to_localappdata_when_primary_unwritable(tmp_path):
    """主目录不可写时回退到 %LOCALAPPDATA%\\<APP_STORAGE_ID>。

    用两种方式验证：
    1) 注入不可写的 probe（模拟 exe 目录在 Program Files 下）；
    2) 真实文件系统：主目录的父路径是一个普通文件，mkdir 必然失败。
    """
    primary = tmp_path / "readonly"
    fallback = tmp_path / "localappdata" / app.APP_STORAGE_ID

    store = app.HistoryStore(
        primary_dir=primary,
        fallback_dir=fallback,
        writable_probe=lambda candidate: Path(candidate) != primary,
    )
    resolved = store.resolve_dir()
    assert resolved == fallback
    assert store.path.parent == fallback
    store.save([])
    assert store.path.exists()

    # 真实不可写：blocker 是文件，不能在它下面建目录
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    real_store = app.HistoryStore(primary_dir=blocker / "sub", fallback_dir=fallback)
    assert real_store.resolve_dir() == fallback


def test_history_store_default_dir_uses_fixed_storage_id(tmp_path, monkeypatch):
    """历史目录使用固定内部 ID，不随显示名变化。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    store = app.HistoryStore()
    assert store.fallback_dir.name == app.APP_STORAGE_ID
    assert store.fallback_dir.parent == tmp_path / "LocalAppData"


def test_display_name_change_does_not_move_history_dir(tmp_path, monkeypatch):
    """改显示名/EXE 名后，历史目录仍固定为 APP_STORAGE_ID。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    before = app.HistoryStore().fallback_dir
    monkeypatch.setattr(app, "APP_DISPLAY_NAME", "Renamed Display Name")
    monkeypatch.setattr(app, "APP_EXE_NAME", "RenamedExe")
    after = app.HistoryStore().fallback_dir
    assert before == after
    assert app.APP_STORAGE_ID == "DiskUsageTracker"


# ---------------------------------------------------------------------------
# 6. 端到端：首次运行 -> 第二次运行
# ---------------------------------------------------------------------------


def _prepare_store(tmp_path, fixture_name=None):
    primary = tmp_path / "app"
    fallback = tmp_path / "localappdata" / app.APP_STORAGE_ID
    store = app.HistoryStore(primary_dir=primary, fallback_dir=fallback)
    primary.mkdir(parents=True, exist_ok=True)
    if fixture_name:
        shutil.copyfile(FIXTURES / fixture_name, primary / app.HISTORY_FILENAME)
    return store


def test_end_to_end_first_run_creates_baseline(tmp_path):
    store = _prepare_store(tmp_path)
    outcome = app.perform_run(app.FixturesProvider(FIXTURES), store,
                              now=datetime(2026, 4, 30, 8, 0, tzinfo=TZ8))

    assert outcome.baseline is True
    assert outcome.previous_stamp is None
    assert len(outcome.rows) == 3              # 2 块可读 + 1 块读取失败
    assert len(store.load().records) == 2      # 读取失败的盘不写历史
    healthy = [r for r in outcome.rows if not r.failed]
    assert all(r.read_delta_gb is None for r in healthy)
    assert all(r.status_text == "新盘首次记录，暂无增量对比" for r in healthy)
    assert any(r.failed for r in outcome.rows)


def test_end_to_end_second_run_reports_interval_and_deltas(tmp_path):
    store = _prepare_store(tmp_path, "history_single.json")
    outcome = app.perform_run(app.FixturesProvider(FIXTURES), store,
                              now=datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))

    assert outcome.baseline is False
    assert outcome.previous_stamp == "2026-04-30T08:00:00+08:00"
    assert outcome.current_stamp.startswith("2026-05-02T10:30:00")

    rows = {r.key: r for r in outcome.rows}
    nvme = rows[NVME_KEY]
    sata = rows[SATA_KEY]

    assert nvme.interval_text == "2 天 2 小时 30 分钟"
    assert nvme.read_delta_gb == pytest.approx(NVME_READ_GB - 6000.0)
    assert nvme.write_delta_gb == pytest.approx(NVME_WRITE_GB - 7500.0)
    assert nvme.hist_read_delta_gb == pytest.approx(NVME_READ_GB - 6000.0)
    assert nvme.media_text == "稳定"

    assert sata.interval_text == "2 天 2 小时 30 分钟"
    assert sata.read_delta_gb == pytest.approx(SATA_READ_GB - 900.0)
    assert sata.write_delta_gb == pytest.approx(SATA_WRITE_GB - 1200.0)
    assert sata.media_text == "不适用"

    totals = outcome.totals
    assert totals["read_delta"] == pytest.approx((NVME_READ_GB - 6000.0) + (SATA_READ_GB - 900.0))
    assert totals["write_delta"] == pytest.approx((NVME_WRITE_GB - 7500.0) + (SATA_WRITE_GB - 1200.0))
    assert outcome.media_alert is False

    # 第二次运行把新记录追加进历史：2 条旧 + 2 条新
    assert len(store.load().records) == 4


def test_end_to_end_history_accumulates_across_three_runs(tmp_path):
    store = _prepare_store(tmp_path, "history_with_history.json")
    first = app.perform_run(app.FixturesProvider(FIXTURES), store,
                            now=datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    second = app.perform_run(app.FixturesProvider(FIXTURES), store,
                             now=datetime(2026, 5, 3, 10, 30, tzinfo=TZ8))

    # 历史首次记录是 5000 / 7000，历史累计增量始终相对它
    rows1 = {r.key: r for r in first.rows}
    rows2 = {r.key: r for r in second.rows}
    assert rows1[NVME_KEY].hist_read_delta_gb == pytest.approx(NVME_READ_GB - 5000.0)
    assert rows2[NVME_KEY].hist_read_delta_gb == pytest.approx(NVME_READ_GB - 5000.0)
    # 但本次区间增量相对"上一次记录"（即第一次运行刚写入的 2026-05-02T10:30）
    assert rows2[NVME_KEY].interval_text == "1 天 0 小时 0 分钟"
    # 历史以 4 位小数的 GB 保存，因此同一累计值往返后允许 1e-4 GB 的量化误差
    assert rows2[NVME_KEY].read_delta_gb == pytest.approx(0.0, abs=1e-4)
    assert len(store.load().records) == 3 + 2 + 2


def test_end_to_end_new_disk_and_vanished_disk(tmp_path):
    """历史里有盘但本次消失 / 本次出现历史里没有的新盘。"""
    store = _prepare_store(tmp_path, "history_with_vanished_disk.json")
    outcome = app.perform_run(app.FixturesProvider(FIXTURES), store,
                              now=datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    rows = {r.key: r for r in outcome.rows}
    assert rows[NVME_KEY].read_delta_gb is not None
    assert rows[SATA_KEY].read_delta_gb is not None
    vanished = rows["WDC WD40EFRX-68N32N0|WD-WCC7K1234567"]
    assert vanished.vanished is True

    store2 = _prepare_store(tmp_path / "second", "history_with_new_disk.json")
    outcome2 = app.perform_run(app.FixturesProvider(FIXTURES), store2,
                               now=datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    rows2 = {r.key: r for r in outcome2.rows}
    assert rows2[NVME_KEY].status_text == "新盘首次记录，暂无增量对比"
    assert rows2[NVME_KEY].read_delta_gb is None


class _FakeProvider:
    """测试专用 provider：直接返回构造好的快照，完全不调用 smartctl。"""

    def __init__(self, snapshots, status="fake provider"):
        self._snapshots = list(snapshots)
        self._status = status

    def read_all(self):
        return app.ProviderResult(snapshots=list(self._snapshots), smartctl_path=None,
                                  status=self._status, ok=True)


def test_end_to_end_media_error_increase_triggers_alert(tmp_path):
    store = _prepare_store(tmp_path, "history_single.json")
    provider = _FakeProvider([
        app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe",
                         model="Samsung SSD 980 PRO 1TB", serial="S6B0NL0R912345A",
                         read_bytes=13314398 * 512000, write_bytes=17120410 * 512000,
                         media_errors=3),
    ])
    outcome = app.perform_run(provider, store, now=datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    row = outcome.rows[0]
    assert row.media_text == "⚠ 0E 新增 3"
    assert row.alert is True
    assert outcome.media_alert is True


# ---------------------------------------------------------------------------
# 7. GUI 冒烟测试
# ---------------------------------------------------------------------------


@pytest.fixture()
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception as exc:  # 无显示 / 无桌面会话
        pytest.skip(f"CI 环境无法启动 Tk GUI，跳过 GUI 冒烟测试：{exc!r}")
    try:
        root.withdraw()
        yield root
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _gui_store(tmp_path):
    primary = tmp_path / "app"
    primary.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "app": app.APP_STORAGE_ID,
        "records": [
            {
                "timestamp": "2026-04-30T08:00:00+08:00",
                "key": "TEST NVME|SN1",
                "model": "Test NVMe",
                "serial": "SN1",
                "protocol": "NVMe",
                "device": "/dev/nvme0",
                "read_gb": 100.0,
                "write_gb": 200.0,
                "media_errors": 0,
            }
        ],
    }
    (primary / app.HISTORY_FILENAME).write_text(json.dumps(payload, ensure_ascii=False),
                                                encoding="utf-8")
    return app.HistoryStore(primary_dir=primary,
                            fallback_dir=tmp_path / "fallback" / app.APP_STORAGE_ID)


def test_gui_smoke_rows_columns_and_row_colors(tk_root, tmp_path):
    store = _gui_store(tmp_path)
    provider = _FakeProvider([
        app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe",
                         model="Test NVMe", serial="SN1",
                         read_bytes=300 * 1024 ** 3, write_bytes=400 * 1024 ** 3,
                         media_errors=5),
        app.DiskSnapshot(device_path="/dev/sdc", protocol="SATA",
                         model="Broken HDD", serial="SN2",
                         error="读取失败：Permission denied"),
    ])
    window = app.DiskTrackerApp(tk_root, provider=provider, store=store,
                                autostart=False,
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    outcome = window.run_pipeline_sync()
    tk_root.update_idletasks()

    assert len(window.tree["columns"]) == 13
    assert len(window.tree.get_children()) == 2

    tags_by_iid = {iid: tuple(window.tree.item(iid, "tags")) for iid in window.tree.get_children()}
    alert_rows = [iid for iid, tags in tags_by_iid.items() if app.ROW_ALERT_TAG in tags]
    muted_rows = [iid for iid, tags in tags_by_iid.items() if app.ROW_MUTED_TAG in tags]
    assert len(alert_rows) == 1, f"0E 新增行应整行标红，实际 tags={tags_by_iid}"
    assert len(muted_rows) == 1, f"读取失败行应整行标灰，实际 tags={tags_by_iid}"

    assert window.tree.tag_configure(app.ROW_ALERT_TAG)["foreground"] == app.ALERT_COLOR
    assert window.tree.tag_configure(app.ROW_MUTED_TAG)["foreground"] == app.MUTED_COLOR

    assert outcome.media_alert is True
    assert "介质错误在累积" in window.alert_label.cget("text")

    # 表头/列标题与需求一致
    headings = [window.tree.heading(col)["text"] for col in window.tree["columns"]]
    assert headings[:3] == ["磁盘型号", "序列号(截断)", "协议"]
    assert headings[-3:] == ["健康度", "0E", "状态"]   # 健康度列紧挨在 0E 之前

    window.root.destroy()


def test_gui_baseline_message_on_first_run(tk_root, tmp_path):
    primary = tmp_path / "fresh"
    primary.mkdir(parents=True, exist_ok=True)
    store = app.HistoryStore(primary_dir=primary,
                             fallback_dir=tmp_path / "fb" / app.APP_STORAGE_ID)
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False,
                                now_func=lambda: datetime(2026, 4, 30, 8, 0, tzinfo=TZ8))
    outcome = window.run_pipeline_sync()
    tk_root.update_idletasks()
    assert outcome.baseline is True
    assert len(window.tree.get_children()) == 3
    assert "已记录基线，下次打开将显示增量" in window.note_label.cget("text")
    window.root.destroy()


def test_gui_about_button_shows_version(tk_root, tmp_path, monkeypatch):
    """GUI 层「关于」按钮必须存在，且正文版本号来自 APP_VERSION（不弹真对话框）。"""
    store = _gui_store(tmp_path)
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False)
    tk_root.update_idletasks()
    assert window.about_button.cget("text") == "关于"

    captured = {}

    def fake_showinfo(title, message, **kwargs):
        captured["title"] = title
        captured["message"] = message
        return "ok"

    import tkinter.messagebox as mb
    monkeypatch.setattr(mb, "showinfo", fake_showinfo)
    window.show_about()
    assert app.APP_VERSION in captured["message"]
    assert app.APP_DISPLAY_NAME in captured["title"]
    assert str(store.path) in captured["message"]
    window.root.destroy()


def test_gui_sorting_does_not_crash(tk_root, tmp_path):
    store = _gui_store(tmp_path)
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False,
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    window.run_pipeline_sync()
    before = len(window.tree.get_children())
    for column in ("read_gb", "write_delta", "model", "health", "media"):
        window.sort_by(column)
        window.sort_by(column)   # 再点一次反向
        assert len(window.tree.get_children()) == before
    window.root.destroy()


# ---------------------------------------------------------------------------
# 工程约束：配置集中、无 GUI 层 smartctl 调用
# ---------------------------------------------------------------------------


def test_names_are_configured_in_one_place():
    assert app.APP_DISPLAY_NAME == "磁盘流水 DiskFlow"
    assert app.APP_EXE_NAME == "DiskFlow"
    assert app.APP_ARTIFACT_NAME == "DiskFlow-windows"
    assert app.APP_STORAGE_ID == "DiskUsageTracker"
    assert app.APP_EXE_NAME.isalnum()


def test_app_version_is_configured_and_has_single_source():
    """版本号必须有唯一来源 APP_VERSION，且是点分数字（发版 tag 用它对齐）。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", app.APP_VERSION), app.APP_VERSION
    assert app.APP_VERSION == app.APP_VERSION.strip()


def test_about_text_references_app_version_and_names():
    text = app.about_text()
    # 版本号来自 APP_VERSION，不是写死的字面量
    assert app.APP_VERSION in text
    assert f"v{app.APP_VERSION}" in text
    assert app.APP_DISPLAY_NAME in text
    assert f"{app.APP_EXE_NAME}.exe" in text
    # 历史目录 ID 固定，改名不影响历史数据，关于框里要讲清楚
    assert app.APP_STORAGE_ID in text
    # 关键实现说明：换算规则不能只有代码里知道
    assert "1024^3" in text
    assert "512000" in text


def test_about_text_reflects_changed_version(monkeypatch):
    """改 APP_VERSION 后关于框内容必须跟着变（证明没有第二个写死来源）。"""
    original = app.APP_VERSION
    monkeypatch.setattr(app, "APP_VERSION", "9.9.9")
    text = app.about_text()
    assert "v9.9.9" in text
    # 原来的版本号必须消失（否则说明某处又写死了一份）
    assert f"v{original}" not in text


def test_about_text_uses_given_history_path(tmp_path):
    target = tmp_path / "somewhere" / "history.json"
    text = app.about_text(target)
    assert str(target) in text
    # 不传路径时给出可读占位，而不是 "None"
    assert "None" not in app.about_text()
    assert "None" not in text


# ---------------------------------------------------------------------------
# 发布流程：CHANGELOG.md 是 Release 正文的唯一来源，必须与 APP_VERSION 对齐
# ---------------------------------------------------------------------------

CHANGELOG = Path(__file__).resolve().parent / "CHANGELOG.md"


def _changelog_sections():
    """返回 {版本号: 小节正文}，按 "## [x.y.z]" 二级标题切分。"""
    assert CHANGELOG.exists(), "缺少 CHANGELOG.md：Release 正文必须有出处"
    sections = {}
    current = None
    buffer = []
    for line in CHANGELOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            match = re.match(r"^##\s+\[?([0-9A-Za-z.\-]+)\]?", line)
            current = match.group(1) if match else None
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def test_changelog_has_section_for_current_version():
    """APP_VERSION 必须有对应小节，否则 CI 抽不到 Release 正文（会直接失败）。"""
    sections = _changelog_sections()
    assert app.APP_VERSION in sections, (
        f"CHANGELOG.md 缺少 v{app.APP_VERSION} 的小节，现有：{sorted(sections)}"
    )
    body = sections[app.APP_VERSION]
    # 正文必须是给用户看的说明，不是占位符
    assert len(body) > 100, "该版本的更新日志过于敷衍"
    assert "###" in body, "更新日志应分小节说明，而不是一句话带过"


def test_changelog_newest_entry_matches_app_version():
    """第一个版本小节必须是当前版本：避免发了新版却忘了写日志。"""
    first_header = next(
        line for line in CHANGELOG.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    )
    assert app.APP_VERSION in first_header, (
        f"CHANGELOG.md 最新的小节是 {first_header!r}，与 APP_VERSION {app.APP_VERSION} 不一致"
    )


def test_gui_layer_does_not_call_smartctl():
    """GUI 层不得出现 smartctl 调用：类里不应引用 subprocess。"""
    import inspect

    source = inspect.getsource(app.DiskTrackerApp)
    assert "subprocess" not in source
    assert "smartctl -a" not in source
    assert "smartctl.exe" not in source.replace("smartctl.exe，请执行", "")


def test_smartctl_calls_are_confined_to_provider_class():
    import inspect

    provider_source = inspect.getsource(app.SmartctlProvider)
    assert '"-a"' in provider_source or "'-a'" in provider_source
    assert "subprocess" in inspect.getsource(app)
