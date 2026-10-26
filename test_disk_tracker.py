"""test_disk_tracker.py —— 磁盘流水 DiskFlow 的 pytest 用例。

全部用例都可在无真实硬盘、无 smartctl 的环境下运行（CI windows-latest 真实执行）：
数据源通过 FixturesProvider / 假 provider 注入，历史文件写在 pytest 的 tmp_path 里。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tkinter
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


def test_fmt_bytes_size_steps_through_b_kb_mb_gb():
    """字节→人读体积：按 1024 阶梯换单位，边界与异常值都不抛异常。"""
    assert app.fmt_bytes_size(0) == "0 B"
    assert app.fmt_bytes_size(1023) == "1023 B"
    assert app.fmt_bytes_size(1024) == "1.0 KB"
    assert app.fmt_bytes_size(1536) == "1.5 KB"
    assert app.fmt_bytes_size(1024 * 1024 - 1) == "1,024.0 KB"
    assert app.fmt_bytes_size(1024 * 1024) == "1.0 MB"
    assert app.fmt_bytes_size(12 * 1024 * 1024 + 512 * 1024) == "12.5 MB"
    assert app.fmt_bytes_size(1024 ** 3) == "1.00 GB"
    assert app.fmt_bytes_size(12254196) == "11.7 MB"      # 真实 exe 体积的量级
    assert app.fmt_bytes_size(2 * 1024 ** 3) == "2.00 GB"
    # 不好读的输入一律给「—」，绝不把界面文案变成异常
    assert app.fmt_bytes_size(None) == "—"
    assert app.fmt_bytes_size("not-a-number") == "—"
    assert app.fmt_bytes_size(-5) == "—"


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


def test_parse_real_captured_nvme_fixture():
    """真机抓取的 NVMe 样本（已脱敏）：锁住完整 nvme_smart_health_information_log 的解析。

    真机上 NVMe 的健康信息是一个整块，data_units_read/written、media_errors、
    percentage_used 全在里面；这里断言解析器把四个关键字段都取对了。
    """
    payload = json.loads((FIXTURES / "nvme_real_256gb.json").read_text(encoding="utf-8"))
    snapshot = app.parse_smartctl_payload(payload, device_path=payload["device"]["name"])
    assert snapshot.ok
    assert snapshot.protocol == "NVMe"
    assert snapshot.model == "Sample NVMe SSD 256GB"
    assert snapshot.serial == "N2SAMPLE0001"
    assert snapshot.counter_source == app.COUNTER_SOURCE_NVME
    assert snapshot.read_gb == pytest.approx(68636716 * 512000 / 1024 ** 3)
    assert snapshot.write_gb == pytest.approx(41713767 * 512000 / 1024 ** 3)
    assert snapshot.read_gb == pytest.approx(32728.536606, rel=1e-9)
    assert snapshot.write_gb == pytest.approx(19890.674114, rel=1e-9)
    assert snapshot.media_errors == 0
    assert snapshot.life_percent == 95           # percentage_used = 5
    assert snapshot.health_text == "95%"
    # TB / GB 双单位：31.96 TB (32,728.54 GB)
    assert app.fmt_size_gb(snapshot.read_gb) == "31.96 TB (32,728.54 GB)"


def test_parse_real_captured_nvme_fixture_second_controller():
    """第二块真机 NVMe（NVMe 2.0 控制器、双温度传感器）的解析结果。"""
    payload = json.loads((FIXTURES / "nvme_real_512gb.json").read_text(encoding="utf-8"))
    snapshot = app.parse_smartctl_payload(payload, device_path=payload["device"]["name"])
    assert snapshot.ok
    assert snapshot.model == "Sample NVMe SSD 512GB"
    assert snapshot.serial == "N2SAMPLE0002"
    assert snapshot.read_gb == pytest.approx(124883089 * 512000 / 1024 ** 3)
    assert snapshot.write_gb == pytest.approx(55508360 * 512000 / 1024 ** 3)
    assert snapshot.read_gb == pytest.approx(59548.897266, rel=1e-9)
    assert snapshot.write_gb == pytest.approx(26468.448639, rel=1e-9)
    assert snapshot.media_errors == 0
    assert snapshot.life_percent == 93           # percentage_used = 7
    assert app.fmt_size_gb(snapshot.read_gb) == "58.15 TB (59,548.90 GB)"


# 真机样本脱敏回归测试的敏感词表。这里**故意不写明文**：把这些型号、序列号直接写成
# 字面量，测试文件本身就成了泄漏源（仓库是公开的）。改成小写后的 SHA-256 之后再比对，
# 既能精确防护，又不会把硬件标识带进源码。新增一条时用：
#   python -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].lower().encode()).hexdigest())" <敏感词>
ANONYMIZATION_FORBIDDEN_SHA256 = {
    "e754a8485451bd160a0d61c1f83bc0bbb1b7e8ffcb660827319ac4ca0cf79fb7": "盘 1 型号片段（NVMe）",
    "e9bca60b333d4f1a86d0fb1fb093ddf28d9bf840f9cb2daf557d1ee87e93133a": "盘 1 型号片段（NVMe）",
    "acd0785eea980b6df4351382ed61a8656460782b5f05225ac1de9bc7dd8c90ef": "盘 1 序列号",
    "98343d9d7d9ba59d2f3d3f08157f3130c17fe3b4da18e8b0961867618b8477b0": "盘 1 固件版本",
    "63d20c658ee95769ff8b099b5843b5265f47dd31dc41379b3c54ca01955ad03d": "盘 2 型号片段（NVMe）",
    "955d355feefd859f59b1aee299181faefcc22085abb7c72acc1938e9593bbf3b": "盘 2 序列号",
    "3e6928b5b19570221abad3ab4399928bb65a430d00c790bb116c5267007627a0": "盘 2 固件版本",
    "22040cde5778d72e2ab800aac3450070953ab8b68f9e3fe4f6779717734f9c2f": "SATA 盘厂商名",
    "b61c55b39df4ed28e51a3120f1db0290c49e4a74dad9f4fafe3d9d9a349f9188": "SATA 盘型号片段",
    "deb2f9d84252c2b54027f07bb86e5a0befa932fd4549ef0d1c363e8959fdb417": "SATA 盘序列号",
    "167898d1425dbe417720ed3ba2c5d67447480df6648ec11c9e593d5902f30289": "SATA 盘固件版本",
    "64eea25fd0fd214e6543fc57946ee64085da1246697deb32c154485f513ca58d": "采集机的系统 build 号",
}

_ANON_MIN_PREFIX = 5   # 敏感词最短 5 字符；再短（如 4 位年份 "2024"）会误报
_ANON_MAX_PREFIX = 24  # 最长的是 14 位序列号；取到 24 留足余量


def _find_forbidden_hits(text):
    """在 text 里找出命中敏感词表的片段。

    不能直接做反向查表（哈希不可逆），所以按"分词 + 逐前缀哈希"来探：
    先把文本切成字母数字词，再对每个词的前 5..24 个字符分别求 SHA-256，
    与敏感词表对照。这样既能命中整词（一整串序列号），也能命中词前缀
    （型号长串开头的型号片段），而开销只有 词数 × 20 次哈希。
    """
    hits = []
    for token in re.findall(r"[A-Za-z0-9]{%d,}" % _ANON_MIN_PREFIX, text):
        lowered = token.lower()
        for length in range(_ANON_MIN_PREFIX, min(_ANON_MAX_PREFIX, len(lowered)) + 1):
            digest = hashlib.sha256(lowered[:length].encode("utf-8")).hexdigest()
            label = ANONYMIZATION_FORBIDDEN_SHA256.get(digest)
            if label:
                hits.append((label, f"命中前缀长度 {length} 的词"))
    return hits


def test_anonymization_scanner_actually_detects_leaks(monkeypatch):
    """防"扫描器坏了但测试一直绿"：用运行时生成的假敏感词验证扫描器真的能命中。

    这里不写任何真实硬件标识 —— 只在内存里造一个随机 token，算出哈希塞进词表，
    然后确认 _find_forbidden_hits 能把它从一段文本里揪出来；同时确认干净文本不误报。
    """
    secret = "ZZ" + "Q7" * 6          # 14 字符，形如序列号
    digest = hashlib.sha256(secret.lower().encode("utf-8")).hexdigest()
    patched = dict(ANONYMIZATION_FORBIDDEN_SHA256)
    patched[digest] = "测试用假敏感词"
    monkeypatch.setattr(sys.modules[__name__], "ANONYMIZATION_FORBIDDEN_SHA256", patched)
    # 整词命中
    assert _find_forbidden_hits(f"model XYZ serial {secret} firmware A1") != []
    # 大小写不敏感
    assert _find_forbidden_hits(secret.lower()) != []
    # 干净文本不误报
    assert _find_forbidden_hits("Sample NVMe SSD 256GB N2SAMPLE0001") == []


def test_real_capture_fixtures_are_anonymized():
    """真机样本必须脱敏：仓库里不能出现任何品牌型号、序列号、固件、PCI/EUI 标识。

    敏感词以 SHA-256 形式保存（见 ANONYMIZATION_FORBIDDEN_SHA256），测试文件本身
    不含明文硬件标识；比对时把 fixture 文本分词后对各前缀求哈希。
    """
    names = ("nvme_real_256gb.json", "nvme_real_512gb.json", "sata_real_960gb.json")
    for name in names:
        text = (FIXTURES / name).read_text(encoding="utf-8")
        hits = _find_forbidden_hits(text)
        assert not hits, f"{name} 里残留了可识别信息：{hits}"
    # 结构性检查：这几个字段本身就不该存在于 fixture 中（不涉及具体值）
    for name in names:
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        for field in ("nvme_pci_vendor", "nvme_ieee_oui_identifier", "nvme_controller_id"):
            assert field not in payload, f"{name} 里应已删除 {field}"
        namespaces = payload.get("nvme_namespaces") or []
        for ns in namespaces:
            assert "eui64" not in ns, f"{name} 的命名空间里应已删除 eui64"
    # 型号/序列号统一用 Sample / SAMPLE 占位，其余字段（容量、计数、能力位）保持真机原值，
    # 这样换算与格式化路径仍被真机数据验证过
    payloads = [
        json.loads((FIXTURES / n).read_text(encoding="utf-8"))
        for n in ("nvme_real_256gb.json", "nvme_real_512gb.json", "sata_real_960gb.json")
    ]
    for payload in payloads:
        assert "sample" in payload["model_name"].lower()
        assert "sample" in payload["serial_number"].lower()


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


def test_history_reset_backs_up_old_file_and_rebuilds_empty_baseline(tmp_path):
    """重置历史：旧文件改名备份（不删），新文件是空基线，仍能正常追加。"""
    store = _store(tmp_path)
    when = datetime(2026, 4, 30, 8, 0, tzinfo=TZ8)
    snap = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe", model="M", serial="S",
                            read_bytes=10 * 1024 ** 3, write_bytes=20 * 1024 ** 3,
                            media_errors=0)
    store.save(app.records_from_snapshots([snap], when))
    path = store.path
    assert store.load().records

    backup = store.reset()

    assert backup is not None
    assert backup.exists(), "重置必须把旧文件改名留档，而不是直接删除"
    assert backup.name.startswith(app.HISTORY_RESET_PREFIX)
    assert backup.name.endswith(".json")
    assert backup.parent == path.parent
    assert backup.stat().st_size > 0
    # 旧内容确实进了备份
    assert "M" in backup.read_text(encoding="utf-8")
    # 新文件是干净的空基线，且还能继续追加
    assert path.exists()
    assert store.load().records == []
    store.save(app.records_from_snapshots([snap], when))
    assert len(store.load().records) == 1


def test_history_reset_without_existing_file_returns_none(tmp_path):
    """本来就没有历史文件时，重置不报错、不产生备份，只建空基线。"""
    store = _store(tmp_path)
    assert not store.path.exists()

    assert store.reset() is None

    assert store.path.exists()
    assert store.load().records == []
    assert not list(store.path.parent.glob(app.HISTORY_RESET_PREFIX + "*.json"))


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
    monkeypatch.setattr(app.config, "APP_DISPLAY_NAME", "Renamed Display Name")
    monkeypatch.setattr(app.config, "APP_EXE_NAME", "RenamedExe")
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


@pytest.fixture(scope="session")
def _tk_session():
    """整个会话只创建一个 Tk 解释器。

    反复 `tk.Tk()` / `destroy()` 会偶发 `TclError: invalid command name
    "tcl_findLibrary"`（上一个解释器销毁后 Tcl 库路径变量失效），表现为随机 skip，
    所以这里只建一次，每个用例改用它的 Toplevel 子窗口当 root。
    """
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


@pytest.fixture()
def tk_root(_tk_session):
    """每个用例一个独立子窗口；用例里 destroy() 它不会影响其它用例。"""
    import tkinter

    child = tkinter.Toplevel(_tk_session)
    child.withdraw()
    try:
        yield child
    finally:
        try:
            child.destroy()
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

    # 表格视图固定 8 列（成对指标合并成「左 / 右」一格），默认视图就是表格
    assert window.view_key == app.VIEW_TABLE
    assert len(window.tree["columns"]) == 8
    assert len(window.tree.get_children()) == 2

    tags_by_iid = {iid: tuple(window.tree.item(iid, "tags")) for iid in window.tree.get_children()}
    alert_rows = [iid for iid, tags in tags_by_iid.items() if app.ROW_ALERT_TAG in tags]
    muted_rows = [iid for iid, tags in tags_by_iid.items() if app.ROW_MUTED_TAG in tags]
    assert len(alert_rows) == 1, f"0E 新增行应整行标红，实际 tags={tags_by_iid}"
    assert len(muted_rows) == 1, f"读取失败行应整行标灰，实际 tags={tags_by_iid}"

    # 颜色来自当前主题（主题可切换，因此断言「与当前主题一致」而不是写死某个色值）
    alert_tag = window.tree.tag_configure(app.ROW_ALERT_TAG)
    muted_tag = window.tree.tag_configure(app.ROW_MUTED_TAG)
    assert alert_tag["foreground"] == window.theme["alert_fg"]
    assert muted_tag["foreground"] == window.theme["muted_fg"]
    # 告警行有可见底色（数据台账/深色/仪表主题），极简主题才是纯文字色
    assert alert_tag["background"] == (window.theme["alert_bg"] or window.theme["tree_bg"])
    # 两套颜色必须能互相区分，否则「红/灰」在视觉上不成立
    assert window.theme["alert_fg"] != window.theme["muted_fg"]

    assert outcome.media_alert is True
    assert "介质错误在累积" in window.alert_label.cget("text")

    # 表头/列标题与需求一致
    headings = [window.tree.heading(col)["text"] for col in window.tree["columns"]]
    assert headings == [spec[1] for spec in app.COLUMN_SPECS]
    assert headings[:3] == ["磁盘 / 序列号", "协议", "累计读 / 写"]
    assert headings[-2:] == ["0E / 健康", "状态"]   # 0E 与健康度合成一格
    # 合并列里读/写两个值都要出现，且共用同一个分隔符
    first_values = window.tree.item(window.tree.get_children()[0], "values")
    # 首列型号与序列号上下两行，序列号不再被截断
    assert app.PAIR_SEPARATOR in first_values[0]
    model_line, serial_line = first_values[0].split(app.PAIR_SEPARATOR)
    assert model_line and serial_line and "..." not in serial_line
    assert app.PAIR_SEPARATOR in first_values[2]      # 累计读 / 写
    assert app.PAIR_SEPARATOR in first_values[4]      # 本次读增 / 写增
    assert app.PAIR_SEPARATOR in first_values[5]      # 历史读增 / 写增
    assert app.PAIR_SEPARATOR in first_values[6]      # 0E / 健康

    # 增量格必须自带单位：表头没有「(GB)」，光一个「+500.00」看不出是容量还是次数
    for index, caption in ((4, "本次读增 / 写增"), (5, "历史读增 / 写增")):
        cells = first_values[index].split(app.PAIR_SEPARATOR)
        assert len(cells) == 2, f"{caption} 应上下两行，实际 {cells}"
        for cell in cells:
            assert cell.endswith((" GB", " TB")) or cell == "—", (
                f"{caption} 的增量缺单位：{cell!r}"
            )
    # 本例：历史有一条 100/200 GB 的记录，本次读到 300/400 GB
    assert first_values[4].split(app.PAIR_SEPARATOR) == ["+200.00 GB", "+200.00 GB"]
    # 历史里只有那一条记录，所以历史累计增量与本次区间增量相同
    assert first_values[5].split(app.PAIR_SEPARATOR) == ["+200.00 GB", "+200.00 GB"]

    # 顶部时间按「2026-05-02 10:30:00 (UTC+8)」显示，不是 ISO 原文
    assert window.cur_label.cget("text") == "本次记录：2026-05-02 10:30:00 (UTC+8)"
    assert window.prev_label.cget("text") == "上次记录：2026-04-30 08:00:00 (UTC+8)"
    assert "T08:00:00" not in window.diagnostics_text()

    # 顶部绿色确认行：读成功 + 写进 history.json 之后才出现
    ok_text = window.ok_label.cget("text")
    assert "数据已更新并保存" in ok_text
    assert "2 块盘" not in ok_text          # 只有 1 块读成功，失败的那块不算
    assert "1 块盘" in ok_text
    # 失败数不能写进绿色那句里，必须单独挂在灰色 Label 上（颜色语义）
    assert "读取失败" not in ok_text
    assert window.fail_label.cget("text") == "1 块读取失败"
    assert window.fail_label.cget("style") == "Muted.TLabel"
    assert window.ok_label.cget("style") == "Ok.TLabel"
    ok_tag = window.ttk.Style(window.root).configure("Ok.TLabel")
    assert ok_tag["foreground"] == window.theme["ok_fg"]
    assert ok_tag["foreground"] != window.theme["hint_fg"]

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
    # 首次运行建基线：绿色确认行说的是「已记录基线」，不是「数据已更新」
    assert "已记录基线" in window.ok_label.cget("text")
    assert "数据已更新" not in window.ok_label.cget("text")
    # fixtures 里有一块读失败的盘，基线这一轮也要如实报出来（绿行仍是「已记录基线」）
    assert window.fail_label.cget("text") == "1 块读取失败"
    note_text = window.note_label.cget("text")
    assert "已记录基线，下次打开将显示增量" in note_text
    # 底部注释行还要给出历史文件的实际体积，方便判断文件有没有在长
    assert str(store.path) in note_text
    assert app.fmt_bytes_size(store.path.stat().st_size) in note_text
    window.root.destroy()


def test_gui_ok_label_stays_empty_when_nothing_read(tk_root, tmp_path):
    """一块盘都没读成功时不能出现绿色「已更新」——那会和红色读取失败自相矛盾。"""
    store = _gui_store(tmp_path)
    provider = _FakeProvider([
        app.DiskSnapshot(device_path="/dev/sdz", protocol="SATA",
                         model="Ghost", serial="SN9", error="读取失败：not found"),
    ])
    window = app.DiskTrackerApp(tk_root, provider=provider, store=store,
                                autostart=False,
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    outcome = window.run_pipeline_sync()
    tk_root.update_idletasks()
    assert outcome.recorded == 0
    assert outcome.failed == 1
    assert window.ok_label.cget("text") == ""
    assert window.fail_label.cget("text") == ""
    assert window.ok_parts == []
    window.root.destroy()


def test_gui_ok_text_counts_recorded_and_failed(tk_root, tmp_path):
    """绿色确认行的文案由 recorded / failed 决定，切换刷新不会残留上一轮的数字。"""
    store = _gui_store(tmp_path)
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False,
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    window.run_pipeline_sync()
    tk_root.update_idletasks()
    # fixtures 里是 3 块盘：2 块读成功（NVMe + SATA SSD）、1 块读取失败
    outcome = window.run_pipeline_sync()
    tk_root.update_idletasks()
    assert window._ok_text(outcome) == "✓ 数据已更新并保存（2 块盘）"
    # 失败数走另一个函数、另一个 Label：绿行只讲成功，灰行只讲失败
    assert window._fail_text(outcome) == "1 块读取失败"
    assert window.ok_label.cget("text") == "✓ 数据已更新并保存（2 块盘）"
    assert window.fail_label.cget("text") == "1 块读取失败"

    # 把 provider 换成两块全失败：绿行必须清空，不能留着上一轮的「已更新」
    window.provider = _FakeProvider([
        app.DiskSnapshot(device_path="/dev/sdx", model="A", serial="1", error="boom"),
        app.DiskSnapshot(device_path="/dev/sdy", model="B", serial="2", error="boom"),
    ])
    outcome = window.run_pipeline_sync()
    tk_root.update_idletasks()
    assert outcome.recorded == 0
    assert outcome.failed == 2
    assert window.ok_label.cget("text") == ""
    # recorded == 0 时灰行也不显示失败数——顶部一个盘都没读到的失败已经由 hint 讲了
    assert window.fail_label.cget("text") == ""
    window.root.destroy()

    # 再直接验一次：失败数为 0 时灰行必须是空（不能常年挂个「0 块读取失败」）
    ok_outcome = app.RunOutcome(rows=[], totals={}, media_alert=False, previous_stamp=None,
                                current_stamp="2026-05-02T10:30:00+08:00", provider_status="ok",
                                history_message="", history_path="h.json", smartctl_path=None,
                                baseline=False, recorded=2, failed=0)
    assert window._fail_text(ok_outcome) == ""
    assert window._ok_text(ok_outcome) == "✓ 数据已更新并保存（2 块盘）"


def test_gui_note_label_shows_history_file_size(tk_root, tmp_path, monkeypatch):
    """底部注释行：历史文件路径后面必须带文件大小；文件读不到时给「—」而不是抛异常。"""
    store = _gui_store(tmp_path)
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False,
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    window.run_pipeline_sync()
    tk_root.update_idletasks()

    size_text = app.fmt_bytes_size(store.path.stat().st_size)
    note_text = window.note_label.cget("text")
    assert str(store.path) in note_text
    assert size_text in note_text
    assert "历史文件：" in note_text

    # 取不到大小（文件被删/被占用）时退回「—」，不能让界面文案抛异常
    monkeypatch.setattr(app.gui.Path, "stat",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert window._history_size_text(store.path) == "—"
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
    # 关于框标题也要带版本号，与窗口标题、左上角标题同源（window_title()）
    assert f"v{app.APP_VERSION}" in captured["title"]
    assert captured["title"] == f"关于 {app.window_title()}"
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


def test_window_title_carries_display_name_and_version():
    """窗口标题与左上角标题必须带上版本号，且文字只来自 window_title()。"""
    title = app.window_title()
    assert app.APP_DISPLAY_NAME in title
    assert f"v{app.APP_VERSION}" in title
    assert title == f"{app.APP_DISPLAY_NAME}  v{app.APP_VERSION}"


def test_window_title_follows_app_version(monkeypatch):
    """改 APP_VERSION 后窗口标题必须跟着变（证明没有第二处写死）。"""
    original = app.APP_VERSION
    monkeypatch.setattr(app.config, "APP_VERSION", "9.9.9")
    assert "v9.9.9" in app.window_title()
    assert f"v{original}" not in app.window_title()


def test_gui_title_bar_and_header_show_version(tk_root, tmp_path):
    """GUI 既要有带版本号的窗口标题，也要有左上角带版本号的标题文本。"""
    store = _gui_store(tmp_path)
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False)
    tk_root.update_idletasks()
    assert window.root.title() == app.window_title()
    assert f"v{app.APP_VERSION}" in window.root.title()
    assert window.title_label.cget("text") == app.window_title()
    assert f"v{app.APP_VERSION}" in window.title_label.cget("text")
    window.root.destroy()


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
    monkeypatch.setattr(app.config, "APP_VERSION", "9.9.9")
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
    """第一个**版本**小节必须是当前版本：避免发了新版却忘了写日志。

    `## [未发布]` 是发版前的暂存区（发版时把它改名成具体版本号），
    它不算版本小节，所以这里跳过它再看第一个 x.y.z 小节。
    """
    headers = [line for line in CHANGELOG.read_text(encoding="utf-8").splitlines()
               if line.startswith("## ")]
    versioned = [line for line in headers
                 if re.match(r"^##\s+\[?\d+\.\d+\.\d+", line)]
    assert versioned, "CHANGELOG.md 里没有任何 '## [x.y.z]' 形式的版本小节"
    first_header = versioned[0]
    assert app.APP_VERSION in first_header, (
        f"CHANGELOG.md 最新的版本小节是 {first_header!r}，与 APP_VERSION {app.APP_VERSION} 不一致"
    )


# 版本内小节顺序：新增 → 修复 → 改进 → 其它（文档 / 测试）。用户读 Release 正文时
# 最关心「多了什么」，其次「修了什么」，最后才是内部变化，所以顺序写死并在这里锁住。
SECTION_ORDER = ["新增", "修复", "改进"]
SECTION_ORDER_TAIL = ["文档", "测试"]


def _section_rank(heading_line):
    """从 "### 新增：xxx" 这样的标题里取出分类名。"""
    match = re.match(r"^###\s+([^：:\s]+)", heading_line)
    return match.group(1) if match else None


def test_changelog_sections_follow_fixed_order():
    """每个版本小节内部必须按 新增 → 修复 → 改进 → 文档/测试 排列。

    历史版本（0.5.x 及更早）用「一件事一个小标题」的旧格式，标题带分类前缀，
    同样适用这套顺序，所以这里对所有版本一视同仁。
    """
    blocks = {}
    current = None
    for line in CHANGELOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            match = re.match(r"^##\s+\[?([0-9A-Za-z.\-]+)\]?", line)
            current = match.group(1) if match else None
            if current:
                blocks[current] = []
        elif current is not None and line.startswith("### "):
            blocks[current].append(line)

    assert blocks, "CHANGELOG.md 里没有任何版本小节"
    canonical = SECTION_ORDER + SECTION_ORDER_TAIL
    for version, headings in blocks.items():
        ranks = []
        for heading in headings:
            name = _section_rank(heading)
            assert name in canonical, (
                f"CHANGELOG 版本 {version} 的标题 {heading!r} 不在约定分类 "
                f"{canonical} 里"
            )
            ranks.append(canonical.index(name))
        assert ranks == sorted(ranks), (
            f"CHANGELOG 版本 {version} 的小节顺序不对，应先是 {SECTION_ORDER}，"
            f"文档/测试放最后；实际顺序：{[_section_rank(h) for h in headings]}"
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
    # 拆包后 subprocess 只应出现在 providers 模块里（shim 只是再导出）
    assert "subprocess" in inspect.getsource(app.providers)


# ---------------------------------------------------------------------------
# 8. 主题与菜单栏
# ---------------------------------------------------------------------------


REQUIRED_THEME_KEYS = (
    "key", "label", "base", "win_bg", "tree_bg", "tree_fg", "head_bg", "head_fg",
    "head_bold", "zebra", "alert_bg", "alert_fg", "muted_fg", "select_bg", "select_fg",
    "rowheight", "padx", "pady", "kpi", "title_fg", "title_pt",
    "hint_fg", "ok_fg", "note_fg", "summary_fg", "card_bg", "card_border", "card_val",
    "card_lbl", "num_family", "card_pt", "scroll_thumb", "scroll_thumb_hover",
    "scroll_idle",
    "seg_sel_bg", "seg_sel_fg", "seg_hover_bg", "seg_hover_fg", "seg_border",
)


def test_theme_table_covers_every_theme_with_same_keys():
    """五套主题 + 「跟随系统」都必须在表里，且键集合一致（apply_theme 不会 KeyError）。"""
    assert set(app.THEME_ORDER) == {
        app.THEME_SYSTEM, app.THEME_LEDGER, app.THEME_DARK,
        app.THEME_GAUGE, app.THEME_MINIMAL, app.THEME_NATIVE,
    }
    assert len(app.THEME_ORDER) == 6
    for key in app.THEME_ORDER:
        assert key in app.THEME_LABELS, key
    assert set(app.THEMES) == set(app.THEME_ORDER) - {app.THEME_SYSTEM}
    for name, table in app.THEMES.items():
        for field in REQUIRED_THEME_KEYS:
            assert field in table, f"{name} 缺少字段 {field}"
        assert table["key"] == name
        assert table["alert_fg"] != table["muted_fg"], f"{name} 的红/灰无法区分"
        # 绿色「已更新」与黄色「注意事项」必须能区分，否则两句提示混成一片
        assert table["ok_fg"] != table["hint_fg"], f"{name} 的成功色与提示色相同"
        assert isinstance(table["rowheight"], int) and table["rowheight"] >= 20
        # 分段按钮：选中/悬停配色必须与常态可区分，否则用户看不出当前视图
        assert table["seg_sel_bg"] != table["seg_hover_bg"], f"{name} 的选中/悬停底色相同"
        for seg_field in ("seg_sel_bg", "seg_sel_fg", "seg_hover_bg",
                          "seg_hover_fg", "seg_border"):
            assert isinstance(table[seg_field], str) and table[seg_field].startswith("#"), \
                f"{name} 的 {seg_field} 不是颜色值"
        # 空闲态细条必须存在且与窗口底色不同（否则「滚动条消失」那个观感问题会回来）
        assert isinstance(table["scroll_idle"], str) and table["scroll_idle"].startswith("#")
        assert table["scroll_idle"] != table["win_bg"], f"{name} 的空闲滚动条与背景同色"


def test_system_theme_follows_light_and_dark():
    """「跟随系统」：浅色 -> 数据台账，深色 -> 深色专业。"""
    light = app.resolve_theme(app.THEME_SYSTEM, dark_mode=False)
    dark = app.resolve_theme(app.THEME_SYSTEM, dark_mode=True)
    assert light["key"] == app.THEME_SYSTEM_LIGHT == app.THEME_LEDGER
    assert dark["key"] == app.THEME_SYSTEM_DARK == app.THEME_DARK
    assert light["win_bg"] != dark["win_bg"]
    assert app.system_theme_name(dark_mode=False) == app.THEME_LEDGER
    assert app.system_theme_name(dark_mode=True) == app.THEME_DARK

    # 显式选定的主题不受系统配色影响
    assert app.resolve_theme(app.THEME_MINIMAL, dark_mode=True)["key"] == app.THEME_MINIMAL


def test_theme_key_normalization_and_registry_detection(monkeypatch):
    """非法主题键回落「跟随系统」；注册表读不到时 detect 返回 None 而不是抛异常。"""
    assert app.normalize_theme_key("nope") == app.THEME_SYSTEM
    assert app.normalize_theme_key(None) == app.THEME_SYSTEM
    assert app.normalize_theme_key(123) == app.THEME_SYSTEM
    assert app.normalize_theme_key(app.THEME_DARK) == app.THEME_DARK
    assert app.normalize_theme_key(app.THEME_SYSTEM) == app.THEME_SYSTEM

    if app.platform.system() == "Windows":
        assert app.detect_system_dark_mode() in (True, False, None)

    def boom(key, name):  # 模拟注册表键缺失
        raise OSError("no such value")

    monkeypatch.setattr(app.platform, "system", lambda: "Windows")
    import winreg
    monkeypatch.setattr(winreg, "OpenKey", boom)
    assert app.detect_system_dark_mode() is None


def test_settings_store_roundtrip_defaults_and_corruption(tmp_path):
    """主题偏好独立存 settings.json：读写往返、损坏/缺失都回落默认值。"""
    store = app.SettingsStore(tmp_path)
    assert store.path.name == app.SETTINGS_FILENAME
    assert store.load() == dict(app.DEFAULT_SETTINGS)

    store.save({"theme": app.THEME_DARK})
    assert json.loads(store.path.read_text(encoding="utf-8"))["theme"] == app.THEME_DARK
    assert store.load()["theme"] == app.THEME_DARK

    # 非法主题写入后被收敛
    store.save({"theme": "bogus"})
    assert store.load()["theme"] == app.THEME_SYSTEM

    # 损坏文件不抛异常，回落默认值
    store.path.write_text("{ not json", encoding="utf-8")
    assert store.load() == dict(app.DEFAULT_SETTINGS)

    # 非 dict 内容同样回落
    store.path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load() == dict(app.DEFAULT_SETTINGS)


def test_gui_menubar_has_six_menus_and_theme_radios(tk_root, tmp_path):
    """精简版菜单栏：6 个一级菜单，主题菜单 6 个单选，语言菜单英文项禁用。"""
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path))
    tk_root.update_idletasks()

    labels = [window.menubar.entrycget(i, "label") for i in range(6)]
    assert labels == ["文件(&F)", "主题(&T)", "工具(&U)", "硬盘(&D)", "帮助(&H)", "语言(&L)"]

    theme_entries = [window.theme_menu.entrycget(i, "label")
                     for i in range(window.theme_menu.index("end") + 1)]
    assert theme_entries == [app.THEME_LABELS[k] for k in app.THEME_ORDER]
    assert "跟随系统" in theme_entries

    assert window.lang_menu.entrycget(1, "state") == "disabled"
    window.root.destroy()


def test_gui_reset_history_file_menu_entry_and_confirm_flow(tk_root, tmp_path, monkeypatch):
    """「重置历史文件」挂在文件菜单下：取消则不动文件，确认则备份改名并重新记录。"""
    import tkinter.messagebox as mb

    store = _gui_store(tmp_path)
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False,
                                settings=app.SettingsStore(tmp_path))
    tk_root.update_idletasks()

    file_entries = [window.file_menu.entrycget(i, "label")
                    for i in range(window.file_menu.index("end") + 1)
                    if window.file_menu.type(i) == "command"]
    assert "重置历史文件(&E)" in file_entries
    assert "打开历史文件(&O)" in file_entries

    # 先取消：文件必须原样保留，也不触发重新记录
    refreshed = []
    monkeypatch.setattr(window, "refresh_async", lambda: refreshed.append(True))
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: False)
    before_text = store.path.read_text(encoding="utf-8")
    window.reset_history_file()
    assert store.path.read_text(encoding="utf-8") == before_text
    assert refreshed == []
    assert not list(store.path.parent.glob(app.HISTORY_RESET_PREFIX + "*.json"))

    # 再确认：旧内容进备份，新文件重建基线，并自动刷新记录
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    window.reset_history_file()
    backups = list(store.path.parent.glob(app.HISTORY_RESET_PREFIX + "*.json"))
    assert len(backups) == 1
    assert "TEST NVME" in backups[0].read_text(encoding="utf-8")
    assert store.load().records == []
    assert refreshed == [True]
    assert any("已重置" in part for part in window.hint_parts)
    window.root.destroy()


def test_gui_theme_switch_updates_colors_and_kpi_visibility(tk_root, tmp_path):
    """切换主题：表格配色/行高/表头、指标卡可见性都跟着变，控件不重建。"""
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path), theme=app.THEME_LEDGER)
    window.run_pipeline_sync()
    tk_root.update_idletasks()
    tree_before = window.tree
    rows_before = len(window.tree.get_children())

    ledger = app.THEMES[app.THEME_LEDGER]
    assert window.kpi_visible is True          # 数据台账有指标卡
    assert window.tree.tag_configure(app.ROW_ALERT_TAG)["foreground"] == ledger["alert_fg"]
    assert window.tree.tag_configure(app.ROW_ALERT_TAG)["background"] == ledger["alert_bg"]
    assert int(float(window.ttk.Style(window.root).lookup("Treeview", "rowheight"))) == ledger["rowheight"]

    window.apply_theme(app.THEME_MINIMAL)
    tk_root.update_idletasks()
    minimal = app.THEMES[app.THEME_MINIMAL]
    assert window.kpi_visible is False         # 极简留白无指标卡
    assert window.tree is tree_before          # 表格控件没被重建
    assert len(window.tree.get_children()) == rows_before
    assert window.tree.tag_configure(app.ROW_ALERT_TAG)["foreground"] == minimal["alert_fg"]
    assert window.theme_key == app.THEME_MINIMAL

    window.apply_theme(app.THEME_DARK)
    tk_root.update_idletasks()
    dark = app.THEMES[app.THEME_DARK]
    assert window.kpi_visible is True
    assert window.tree.tag_configure(app.ROW_MUTED_TAG)["foreground"] == dark["muted_fg"]
    # 标题栏不再跟着主题切深：一律交给系统画，避免深色主题下出现「深色标题栏 + 浅色菜单栏」的割裂
    assert "dark_bar" not in window.theme
    assert not hasattr(window, "_apply_titlebar")
    window.root.destroy()


def test_gui_theme_choice_is_persisted_and_restored(tk_root, tmp_path):
    """菜单里选主题会写进 settings.json，下次启动沿用（不碰 history.json）。"""
    settings = app.SettingsStore(tmp_path)
    store = _gui_store(tmp_path)
    history_before = store.path.read_text(encoding="utf-8")

    import tkinter

    # 用 Toplevel 承载：第一个窗口销毁后还要再开第二个（验证偏好被沿用）
    first_root = tkinter.Toplevel(tk_root)
    window = app.DiskTrackerApp(first_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False, settings=settings)
    index = app.THEME_ORDER.index(app.THEME_GAUGE)
    window.theme_menu.invoke(index)            # 等同用户点菜单
    tk_root.update_idletasks()
    assert window.theme_key == app.THEME_GAUGE
    assert window._theme_var.get() == app.THEME_GAUGE
    assert settings.load()["theme"] == app.THEME_GAUGE
    # 主题偏好不得写进 history.json
    assert store.path.read_text(encoding="utf-8") == history_before
    first_root.destroy()

    second_root = tkinter.Toplevel(tk_root)
    second = app.DiskTrackerApp(second_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False, settings=settings)
    tk_root.update_idletasks()
    assert second.theme_key == app.THEME_GAUGE
    assert second.theme["key"] == app.THEME_GAUGE
    second_root.destroy()


def test_settings_store_persists_view_choice(tmp_path):
    """视图偏好与主题一样存 settings.json：往返正常、非法值收敛到表格。"""
    store = app.SettingsStore(tmp_path)
    assert store.load()["view"] == app.DEFAULT_VIEW == app.VIEW_TABLE

    store.save({"view": app.VIEW_GRID})
    assert json.loads(store.path.read_text(encoding="utf-8"))["view"] == app.VIEW_GRID
    assert store.load()["view"] == app.VIEW_GRID

    store.save({"view": "hologram"})
    assert store.load()["view"] == app.VIEW_TABLE
    assert app.normalize_view_key(None) == app.VIEW_TABLE
    assert app.normalize_view_key(app.VIEW_CARDS) == app.VIEW_CARDS


def test_column_specs_are_eight_merged_columns():
    """表格合并成 8 列，合并列必须成对出现（读/写、0E/健康）。"""
    assert len(app.COLUMN_SPECS) == 8
    keys = [spec[0] for spec in app.COLUMN_SPECS]
    assert keys == ["model", "protocol", "rw", "interval", "cur", "hist", "media", "status"]
    # 合并不代表丢数据：读与写、0E 与健康都必须还在
    assert app.fmt_size_short(1024) == "1.00 TB"
    assert app.fmt_size_short(512) == "512.00 GB"
    assert app.fmt_size_short(None) == "—"
    # 自然宽度必须小于合并前的 1515px，否则「8 列」没有意义
    assert app.TABLE_NATURAL_WIDTH < 1515


def test_slim_scrollbar_set_get_and_hide(tk_root):
    """细条滚动条实现滚动条协议：set/get 可用，全可见时自动隐藏。"""
    bar = app.SlimScrollbar(tk_root, orient="vertical", command=lambda *a: None,
                            track="#FFFFFF", thumb="#C6CDD6")
    bar.pack(fill="y")
    tk_root.update_idletasks()

    bar._length = lambda: 300                   # withdraw 状态没有真实高度，给个虚拟长度
    bar.set("0.0", "1.0")                       # 全部可见
    assert bar.get() == (0.0, 1.0)
    assert bar.visible is False
    # 全部可见时不画可拖的滑块，但依然画一条很淡的整条，右侧不会空出一道留白
    idle_items = bar.find_withtag("thumb")
    assert len(idle_items) == 1
    assert bar.itemcget(idle_items[0], "fill") == bar.idle_color

    bar.set("0.25", "0.5")                      # 有滚动空间
    assert bar.get() == (0.25, 0.5)
    assert bar.visible is True
    bar.recolor("#000000", "#7C8A99", "#9AA9B8")   # 换主题不重建控件
    assert bar.thumb_color == "#7C8A99"

    # 非法输入不能抛异常（滚动条回调在 Tk 内部被调用，抛了就崩）
    bar.set("abc", None)
    assert bar.get() == (0.0, 1.0)


def test_gui_view_switch_keeps_single_widget_set(tk_root, tmp_path):
    """三视图切换：默认表格，切卡片/双列不重建 Treeview，切回表格数据还在。"""
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    window.run_pipeline_sync()
    tk_root.update_idletasks()

    tree_before = window.tree
    rows_before = len(window.tree.get_children())
    assert window.view_key == app.VIEW_TABLE
    assert window.seg_buttons[app.VIEW_TABLE].cget("text") == "表格"
    assert set(window.seg_buttons) == set(app.VIEW_ORDER)

    window.set_view(app.VIEW_CARDS)
    tk_root.update_idletasks()
    assert window.view_key == app.VIEW_CARDS
    assert window.tree is tree_before                     # Treeview 没被重建
    assert len(window.card_rows()) == rows_before         # 卡片数量与行数一致
    # 卡片是真实控件：每块盘一张卡，卡里带型号文本
    assert len(window.cards_inner.winfo_children()) == rows_before

    window.set_view(app.VIEW_GRID)
    tk_root.update_idletasks()
    assert window.view_key == app.VIEW_GRID
    assert len(window.cards_inner.winfo_children()) == rows_before

    window.set_view(app.VIEW_TABLE)
    tk_root.update_idletasks()
    assert window.view_key == app.VIEW_TABLE
    assert len(window.tree.get_children()) == rows_before
    # 切回表格：table_frame 重新被 pack，cards_frame 被移出布局
    # （tk_root 是 withdraw 状态，winfo_ismapped() 恒为 0，所以用 winfo_manager() 判定）
    assert window.table_frame.winfo_manager() == "pack"
    assert window.cards_frame.winfo_manager() == ""


def test_gui_card_view_marks_alert_and_muted(tk_root, tmp_path):
    """卡片视图同样要区分告警盘与读取失败的盘（颜色取自当前主题）。"""
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
    window = app.DiskTrackerApp(tk_root, provider=provider, store=store, autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    window.run_pipeline_sync()
    window.set_view(app.VIEW_CARDS)
    tk_root.update_idletasks()

    rows = window.card_rows()
    assert any(row.alert for row in rows)
    assert any(row.failed for row in rows)
    cards = window.cards_inner.winfo_children()
    assert len(cards) == len(rows)
    colors = {card.cget("bg") for card in cards}
    theme = window.theme
    assert theme["alert_bg"] in colors        # 告警卡用告警底色
    assert theme["card_bg"] in colors         # 普通/失败卡用常规底色
    # 卡片视图下复制表格也不能崩（不依赖 Treeview 的 values）
    window.copy_table()
    assert "磁盘 / 序列号" in tk_root.clipboard_get()


def test_gui_view_choice_is_persisted_and_restored(tk_root, tmp_path):
    """在顶栏切到双列后，下次启动仍是双列；切视图不得改写 history.json。"""
    settings = app.SettingsStore(tmp_path)
    store = _gui_store(tmp_path)

    import tkinter

    first_root = tkinter.Toplevel(tk_root)
    window = app.DiskTrackerApp(first_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False, settings=settings)
    window.run_pipeline_sync()
    # 记录「本次运行写入完成后」的历史快照，之后只切视图不该再动它
    history_after_run = store.path.read_text(encoding="utf-8")
    records_after_run = app.count_history_readings(json.loads(history_after_run))

    window.seg_buttons[app.VIEW_GRID].invoke()      # 等同用户点顶栏的分段按钮
    tk_root.update_idletasks()
    assert window.view_key == app.VIEW_GRID
    assert settings.load()["view"] == app.VIEW_GRID
    assert settings.path.exists()
    # 视图偏好写进 settings.json，不写 history.json
    assert store.path.read_text(encoding="utf-8") == history_after_run
    assert app.count_history_readings(json.loads(store.path.read_text(encoding="utf-8"))) == records_after_run
    first_root.destroy()

    second_root = tkinter.Toplevel(tk_root)
    second = app.DiskTrackerApp(second_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False, settings=settings)
    tk_root.update_idletasks()
    assert second.view_key == app.VIEW_GRID
    second_root.destroy()




def test_gui_segment_buttons_are_self_drawn_and_theme_driven(tk_root, tmp_path):
    """顶栏分段按钮必须是自绘控件：悬停/选中配色只来自主题表，不能被系统主题盖掉。

    回归 v0.5.0 的 bug：用 ttk.Radiobutton + 自定义 style 时，vista 主题下
    鼠标悬停会被系统 element 画成系统蓝 + 虚线焦点框。
    """
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                theme=app.THEME_NATIVE)
    tk_root.update_idletasks()

    for button in window.seg_buttons.values():
        assert isinstance(button, app.SegButton)
        assert not isinstance(button, window.ttk.Radiobutton)

    theme = window.theme
    table_button = window.seg_buttons[app.VIEW_TABLE]
    assert table_button.selected is True
    assert table_button.cget("background") == theme["seg_sel_bg"]
    assert table_button.cget("foreground") == theme["seg_sel_fg"]

    # 未选中的按钮：常态配色来自主题，不是系统灰
    cards_button = window.seg_buttons[app.VIEW_CARDS]
    assert cards_button.selected is False
    assert cards_button.cget("background") != theme["seg_sel_bg"]

    # 模拟鼠标悬停：底色换成主题的 hover 色，且绝不是系统蓝 #0078D7（原生主题除外）
    cards_button._on_enter()
    if theme["seg_hover_bg"] != theme["seg_sel_bg"]:
        assert cards_button.cget("background") == theme["seg_hover_bg"]
    cards_button._on_leave()
    assert cards_button.cget("background") != theme["seg_hover_bg"] or \
        theme["seg_hover_bg"] == theme["seg_sel_bg"]

    # 切换主题后按钮配色跟着换（控件不重建）
    buttons_before = dict(window.seg_buttons)
    window.apply_theme(app.THEME_DARK)
    tk_root.update_idletasks()
    dark = app.THEMES[app.THEME_DARK]
    assert window.seg_buttons == buttons_before
    assert window.seg_buttons[app.VIEW_TABLE].cget("background") == dark["seg_sel_bg"]
    # 每个主题的悬停色都与选中色不同，否则用户分不清「当前视图」和「鼠标在这」
    for name, table in app.THEMES.items():
        assert table["seg_hover_bg"] != table["seg_sel_bg"], name
    window.root.destroy()


def test_gui_switch_grid_then_cards_restores_full_width(tk_root, tmp_path):
    """双列 -> 卡片必须回到满宽。

    回归 v0.5.0 的 bug：render_cards() 只给当前视图的列配 weight/uniform，
    切回单列时第二列的 weight=1、uniform="card" 还留着，卡片只占一半宽度，
    看起来仍然像双列。
    """
    import tkinter

    window_root = tkinter.Toplevel(tk_root)
    window = app.DiskTrackerApp(window_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path))
    window.run_pipeline_sync()
    window_root.geometry("1100x640")
    window_root.update()

    def weights():
        return [window.cards_inner.columnconfigure(index)["weight"] for index in range(3)]

    window.set_view(app.VIEW_CARDS)
    window_root.update()
    assert weights()[0] == 1 and weights()[1] == 0

    window.set_view(app.VIEW_GRID)
    window_root.update()
    assert weights()[0] == 1 and weights()[1] == 1

    window.set_view(app.VIEW_CARDS)
    window_root.update()
    # 回到单列：第 2 列的 weight 与 uniform 必须清掉
    assert weights()[1] == 0, "切回卡片视图后第 2 列仍有 weight，卡片会只占一半宽度"
    assert window.cards_inner.columnconfigure(1)["uniform"] in ("", None)
    cards = window.cards_inner.winfo_children()
    assert len(cards) == len(window.card_rows()) >= 2
    inner_width = window.cards_inner.winfo_width()
    if inner_width > 100:          # withdraw/无真实布局时不比宽度
        for card in cards:
            assert card.winfo_width() > inner_width * 0.8, "卡片没有铺满整行，仍在双列布局"
    # 再切回双列也要正常
    window.set_view(app.VIEW_GRID)
    window_root.update()
    assert weights()[0] == 1 and weights()[1] == 1
    window_root.destroy()


def test_gui_menu_actions_do_not_crash(tk_root, tmp_path):
    """复制表格 / 诊断信息 / 重置排序 / 关闭 都应该安全（不弹真对话框）。"""
    store = _gui_store(tmp_path)
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=store, autostart=False,
                                settings=app.SettingsStore(tmp_path))
    window.run_pipeline_sync()
    tk_root.update_idletasks()

    window.copy_table()
    assert "磁盘 / 序列号" in tk_root.clipboard_get()

    text = window.diagnostics_text()
    assert app.APP_DISPLAY_NAME in text
    assert str(store.path) in text
    assert str(window.settings.path) in text

    window.sort_by("read_gb")
    window.reset_sort()
    assert window._sort_column is None

    window.close()
    # 关闭后窗口必须已经不存在（Tk 根窗口销毁后再访问会抛 TclError，
    # Toplevel 子窗口销毁后 winfo_exists() 返回 0；两种都算「确实关掉了」）
    try:
        still_there = bool(window.root.winfo_exists())
    except Exception:
        still_there = False
    assert still_there is False, "关闭后窗口仍然存在，说明有残留窗口"


# ---------------------------------------------------------------------------
# 9. 卡片细节与布局打磨（v0.5.2）
# ---------------------------------------------------------------------------
def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _pixels(value):
    """把 Tk 的像素值统一成 int 列表。

    同一份代码在不同 Tk 版本上可能拿到 `4`、`"0 0 10 0"` 或
    `(<pixel object: '0'>, ..., <pixel object: '10'>)`，测试里必须都能解析。
    """
    if isinstance(value, (tuple, list)):
        return [int(str(item)) for item in value]
    text = str(value).replace("{", "").replace("}", "")
    if "pixel object" in text:
        import re

        return [int(match) for match in re.findall(r"'(-?\d+)'", text)]
    return [int(part) for part in text.split()]


def test_card_status_width_counts_cjk_as_two_columns():
    """Entry 的 width 单位是平均字符宽度，中文必须按 2 列算，否则状态被截断。"""
    assert app.DiskTrackerApp._display_width("已更新记录") == 10
    assert app.DiskTrackerApp._display_width("ABC") == 3
    assert app.DiskTrackerApp._display_width("") == 0
    # 中文与英文混排：3 个汉字 + 2 个字符 = 6 + 2
    assert app.DiskTrackerApp._display_width("新盘AB") == 6


def test_gui_card_text_is_readonly_entry_and_wheel_reaches_canvas(tk_root, tmp_path):
    """卡片文字必须是可选中复制的只读 Entry；滚轮在卡片内任意位置都能滚动。"""
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    window.run_pipeline_sync()
    window.set_view(app.VIEW_CARDS)
    tk_root.update_idletasks()

    widgets = list(_descendants(window.cards_inner))
    assert widgets, "卡片视图应该建出控件"

    entries = [w for w in widgets if w.winfo_class() == "Entry"]
    assert entries, "卡片里的文字必须用只读 Entry，否则无法选中复制"
    for entry in entries:
        assert entry.cget("state") == "readonly"
        assert entry.cget("relief") == "flat"
        assert int(entry.cget("highlightthickness")) == 0

    sample = entries[0]
    text = sample.get()
    assert text
    # 只读 Entry 仍允许选择：选前几个字符并断言确实选上了
    sample.configure(state="normal")
    sample.selection_range(0, min(3, len(text)))
    assert sample.selection_present() is True
    sample.configure(state="readonly")

    # 滚轮：卡片内每一个控件都要绑上滚轮处理，否则鼠标停在卡片上滚不动
    missing = [w.winfo_class() for w in widgets if not w.bind("<MouseWheel>")]
    assert missing == [], f"这些控件没绑滚轮，卡片内滚动会失灵：{missing}"


def test_gui_card_resize_is_debounced_until_flush(tk_root, tmp_path):
    """拖动窗口时宽度重排要延后合并，flush_card_layout 才立即下发。"""
    from types import SimpleNamespace

    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path))
    window.run_pipeline_sync()
    window.set_view(app.VIEW_CARDS)
    tk_root.update_idletasks()

    before = window._cards_width
    target = before + 137
    window._resize_cards(SimpleNamespace(width=target))
    assert window._pending_cards_width == target
    assert window._cards_resize_id is not None, "应该挂起一个定时器而不是立刻重排"
    assert window._cards_width == before, "拖动过程中不应立刻下发宽度"

    window.flush_card_layout()
    assert window._cards_resize_id is None
    assert window._cards_width == target
    assert int(float(window.cards_canvas.itemcget(window._cards_window, "width"))) == target

    # 宽度没变化时不应再排一次重排（否则 <Configure> 会形成反馈环）
    window._resize_cards(SimpleNamespace(width=target))
    assert window._cards_resize_id is None


def test_slim_scrollbar_thumb_is_single_rounded_capsule(tk_root):
    """滑块用一条圆头粗线画，只有一个图元，才不会在圆角处漏出 1 像素。"""
    bar = app.SlimScrollbar(tk_root, orient="vertical", command=lambda *args: None)
    bar.pack(fill="y")
    tk_root.update_idletasks()
    # tk_root 是 withdraw 状态，控件没有真实像素高度；直接给一个虚拟长度，
    # 这样断言的才是「绘制逻辑」本身，而不是宿主窗口有没有显示出来。
    bar._length = lambda: 300
    bar.set("0.1", "0.4")

    items = bar.find_withtag("thumb")
    assert len(items) == 1, "滑块必须是单个图元，多个图元拼接会露缝"
    assert bar.type(items[0]) == "line"
    assert bar.itemcget(items[0], "capstyle") == "round"
    assert bar.thickness == app.SCROLL_GUTTER


def test_kpi_row_and_card_row_share_right_edge(tk_root, tmp_path):
    """KPI 指标行与下面卡片的右边缘必须对齐（都让出滚动条那一条宽度）。"""
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                theme=app.THEME_LEDGER)
    window.run_pipeline_sync()
    tk_root.update_idletasks()
    assert window.kpi_visible is True

    # ttk 的 padding 顺序是 (left, top, right, bottom)
    padding = _pixels(window.kpi_frame.cget("padding"))
    assert len(padding) == 4
    assert padding[2] == app.SCROLL_GUTTER, "KPI 行右侧必须给滚动条留位"
    assert int(window.cards_scroll.cget("width")) == app.SCROLL_GUTTER

    window.set_view(app.VIEW_CARDS)
    tk_root.update_idletasks()
    cards = window.cards_inner.winfo_children()
    assert cards
    # 单列视图：每张卡都独占一行，右侧不留间距（贴住滚动条，与 KPI 行右缘齐平）
    for card in cards:
        padx = _pixels(card.grid_info().get("padx", "0"))
        assert padx[-1] == 0, f"最右列卡片的右边距应为 0，实际 {padx}"

    window.set_view(app.VIEW_GRID)
    tk_root.update_idletasks()
    grid_cards = window.cards_inner.winfo_children()
    assert len(grid_cards) >= 2
    # 双列：左列右侧留半个列间距，右列左侧留半个，两列等宽
    padx_first = _pixels(grid_cards[0].grid_info().get("padx", "0"))
    assert padx_first[-1] == app.CARD_GAP // 2, "双列视图左列应留半个列间距"
    padx_second = _pixels(grid_cards[1].grid_info().get("padx", "0"))
    assert padx_second[0] == app.CARD_GAP // 2
    assert padx_second[-1] == 0


# ---------------------------------------------------------------------------
# 10. 双列对齐 / 卡片细节 / 关于（v0.5.3）
# ---------------------------------------------------------------------------
def test_fmt_delta_gb_always_carries_unit():
    """卡片里的增量必须带单位，否则「+348.80」看不出是 GB 还是次数。"""
    assert app.fmt_delta_gb(348.8) == "+348.80 GB"
    assert app.fmt_delta_gb(0.0) == "+0.00 GB"
    assert app.fmt_delta_gb(-12.345) == "-12.35 GB"
    assert app.fmt_delta_gb(None) == "—"
    # fmt_delta 是「只有数字」的底层写法，调用方负责补单位（底部汇总就是这么用的）
    assert app.fmt_delta(348.8) == "+348.80"
    assert "GB" in f"本次总读增 {app.fmt_delta(348.8)} GB"
    assert any(field.endswith("_delta_gb") for _, field in app.CARD_METRICS)


def test_fmt_delta_short_carries_unit_and_switches_to_tb():
    """表格合并列的增量必须自带单位：小于 1 TB 用 GB，超过用 TB，空值给破折号。"""
    assert app.fmt_delta_short(348.8) == "+348.80 GB"
    assert app.fmt_delta_short(0.0) == "+0.00 GB"
    assert app.fmt_delta_short(-12.345) == "-12.35 GB"
    assert app.fmt_delta_short(None) == "—"
    # 108px 宽的格子放不下「+32,728.43 GB」，超过 1 TB 换 TB
    assert app.fmt_delta_short(32728.43) == "+31.96 TB"
    assert app.fmt_delta_short(-1024.0) == "-1.00 TB"
    assert app.fmt_delta_short(1023.99) == "+1,023.99 GB"
    # 每一种非空写法都必须带单位，绝不能只剩数字
    for value in (0.0, 1.5, 348.8, 1023.99, 1024.0, 32728.43, -5.0):
        assert app.fmt_delta_short(value).split("+")[-1].split("-")[-1].strip() != ""
        assert app.fmt_delta_short(value).endswith((" GB", " TB"))


def test_format_stamp_is_human_readable_with_utc_offset():
    """ISO 串在界面上显示成「2026-04-30 08:00:00 (UTC+8)」，偏移不能写成 +08:00。"""
    assert app.format_stamp("2026-04-30T08:00:00+08:00") == "2026-04-30 08:00:00 (UTC+8)"
    # 整点时区省略「:00」；UTC 本身写 UTC+0
    assert app.format_stamp("2026-04-30T08:00:00Z") == "2026-04-30 08:00:00 (UTC+0)"
    assert app.format_stamp("2026-04-30T08:00:00+00:00") == "2026-04-30 08:00:00 (UTC+0)"
    # 负偏移与非整点时区（印度 +5:30 / 尼泊尔 +5:45）
    assert app.format_stamp("2026-04-30T08:00:00-05:00") == "2026-04-30 08:00:00 (UTC-5)"
    assert app.format_stamp("2026-04-30T08:00:00+05:30") == "2026-04-30 08:00:00 (UTC+5:30)"
    assert app.format_stamp("2026-04-30T08:00:00+05:45") == "2026-04-30 08:00:00 (UTC+5:45)"
    # 毫秒 / 无时区 / 空值 / 垃圾输入都不能崩
    assert app.format_stamp("2026-04-30T08:00:00.123456+08:00") == "2026-04-30 08:00:00 (UTC+8)"
    assert app.format_stamp("2026-04-30T08:00:00") == "2026-04-30 08:00:00 (本地时间)"
    assert app.format_stamp(None) == "—"
    assert app.format_stamp("") == "—"
    assert app.format_stamp("not-a-time") == "not-a-time"


def test_format_utc_offset_variants():
    """偏移格式化单独可测：整点省略分钟，非整点保留两位分钟，UTC 写 UTC+0。"""
    def at(offset):
        return datetime(2026, 4, 30, 8, 0, tzinfo=timezone(offset))

    assert app.format_utc_offset(at(timedelta(hours=8))) == "UTC+8"
    assert app.format_utc_offset(at(timedelta(hours=0))) == "UTC+0"
    assert app.format_utc_offset(at(timedelta(hours=-3))) == "UTC-3"
    assert app.format_utc_offset(at(timedelta(hours=5, minutes=30))) == "UTC+5:30"
    assert app.format_utc_offset(at(timedelta(hours=-9, minutes=-30))) == "UTC-9:30"
    assert app.format_utc_offset(datetime(2026, 4, 30, 8, 0)) == "UTC+0"


def test_gui_card_deltas_show_unit_and_badge_follows_model(tk_root, tmp_path):
    """卡片增量带 GB；协议徽章紧跟型号，不被挤到状态文字跟前。"""
    # 历史里有一条 100/200 GB 的记录，本次读到 300/400 GB，增量才有具体数字
    provider = _FakeProvider([
        app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe",
                         model="Test NVMe", serial="SN1",
                         read_bytes=300 * 1024 ** 3, write_bytes=400 * 1024 ** 3,
                         media_errors=5),
    ])
    window = app.DiskTrackerApp(tk_root, provider=provider, store=_gui_store(tmp_path),
                                autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    window.run_pipeline_sync()
    window.set_view(app.VIEW_CARDS)
    tk_root.update_idletasks()

    entries = [w for w in _descendants(window.cards_inner) if w.winfo_class() == "Entry"]
    texts = [entry.get() for entry in entries]
    unit_texts = [text for text in texts if text.endswith(" GB")]
    assert len(unit_texts) >= 4, f"卡片里的增量没有全部带单位：{texts}"
    assert any(text.startswith("+") for text in unit_texts)

    # 协议徽章必须与型号同处标题行（紧跟在型号后面，而不是被推到最右）
    card = window.cards_inner.winfo_children()[0]
    models = {row.model for row in window.card_rows()}
    model_entry = None
    for widget in _descendants(card):
        if widget.winfo_class() == "Entry" and widget.get() in models:
            model_entry = widget
            break
    assert model_entry is not None, "卡片里应该有型号（只读 Entry）"
    badges = [w for w in _descendants(card)
              if w.winfo_class() == "Label" and w.cget("text") in ("NVMe", "SATA")]
    assert badges, "卡片里应该有协议徽章"
    holder = model_entry.master
    assert holder.winfo_class() == "Frame", "型号 Entry 应该装在一个定宽 Frame 里"
    assert all(badge.master is holder.master for badge in badges), \
        "协议徽章必须与型号同处标题行"
    # 定宽 Frame 必须按实测字宽锁宽（+2px 余量）：Entry 的 width 单位是 "0" 字形宽，
    # 按中英列数估算会给短型号凭空多出二十来像素空白，把协议徽章推远。
    tkfont = pytest.importorskip("tkinter.font")
    measured = tkfont.Font(root=tk_root, font=model_entry.cget("font")).measure(
        model_entry.get())
    assert abs(int(holder.winfo_reqwidth()) - (measured + 2)) <= 2, \
        f"型号外层宽度应与实测字宽一致：holder={holder.winfo_reqwidth()} text={measured}"
    # 定宽 Frame 自己不能被 fill 拉伸：否则徽章又会被挤到状态文字旁边
    assert holder.pack_info().get("fill") in ("none", "", None)


def test_grid_metric_layout_aligns_read_and_write_columns():
    """双列视图的指标必须按「读 / 写 / 上下文」分列，同类增量上下对齐。

    按行排（CARD_METRICS + 3 列）时，「本次读增」在第 2 行第 1 格、「历史读增」在
    第 3 行第 3 格，读的两个增量被别的指标隔开。双列专用排布要求：第 1 列全是读取、
    第 2 列全是写入、第 3 列是时长/0E/健康度，且本次与历史紧挨着。
    """
    columns = app.CARD_METRIC_COLUMNS_GRID
    fields = [field for _, field in app.CARD_METRICS_GRID]
    # 9 项一个不少，只是换了顺序（不能因为改排布丢指标）
    assert sorted(fields) == sorted(field for _, field in app.CARD_METRICS)
    position = {field: (index // columns, index % columns)
                for index, field in enumerate(fields)}

    read_column = {position[f][1] for f in ("read_gb", "read_delta_gb", "hist_read_delta_gb")}
    write_column = {position[f][1] for f in ("write_gb", "write_delta_gb", "hist_write_delta_gb")}
    assert read_column == {0}, f"读取类指标应集中在第 1 列：{read_column}"
    assert write_column == {1}, f"写入类指标应集中在第 2 列：{write_column}"
    # 第 3 列放上下文，不夹读取/写入
    for field in ("interval_text", "media_text", "health_text"):
        assert position[field][1] == 2, f"{field} 应在第 3 列：{position[field]}"

    # 读完 / 写增在同一行，本次与上一次的增量紧挨着（行号只差 1）
    assert position["read_delta_gb"][0] == position["write_delta_gb"][0]
    assert position["hist_read_delta_gb"][0] == position["hist_write_delta_gb"][0]
    assert position["hist_read_delta_gb"][0] == position["read_delta_gb"][0] + 1
    # 累计量在最上面，先看总量再看增量
    assert position["read_gb"][0] == 0 and position["write_gb"][0] == 0


def test_gui_grid_columns_have_equal_width(tk_root, tmp_path):
    """双列视图两列必须等宽（列间距对称），否则左右两列看着一宽一窄。"""
    import tkinter

    window_root = tkinter.Toplevel(tk_root)
    window = app.DiskTrackerApp(window_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                theme=app.THEME_LEDGER)
    window.run_pipeline_sync()
    window_root.geometry("1100x640")
    window.set_view(app.VIEW_GRID)
    window_root.update()
    window.flush_card_layout()
    window_root.update()

    cards = window.cards_inner.winfo_children()
    assert len(cards) >= 2
    widths = [card.winfo_width() for card in cards]
    assert max(widths) - min(widths) <= 2, f"双列卡片宽度不一致：{widths}"
    lefts = {card.winfo_rootx() for card in cards}
    assert len(lefts) == 2, f"双列应该只有两个起始 x：{lefts}"
    window_root.destroy()


def test_gui_scrollbar_keeps_a_faint_track_when_everything_fits(tk_root, tmp_path):
    """内容全部可见时右侧仍有一条淡色细条，不能整条消失（看起来像滚动条丢了）。"""
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                theme=app.THEME_LEDGER)
    window.run_pipeline_sync()
    window.set_view(app.VIEW_CARDS)
    tk_root.update_idletasks()
    bar = window.cards_scroll
    assert bar.winfo_width() == app.SCROLL_GUTTER
    assert bar.idle_color == window.theme["scroll_idle"]
    assert bar.idle_color != window.theme["win_bg"]
    bar._length = lambda: 300
    bar.set("0.0", "1.0")
    items = bar.find_withtag("thumb")
    assert len(items) == 1
    assert bar.itemcget(items[0], "fill") == window.theme["scroll_idle"]


def test_about_text_mentions_author_and_ai_assistance():
    """关于框要写清作者与开发方式（版本号仍只来自 APP_VERSION）。"""
    text = app.about_text("D:/tmp/history.json")
    assert app.APP_DISPLAY_NAME in text
    assert f"v{app.APP_VERSION}" in text
    assert app.APP_AUTHOR in text
    assert "AI" in text
    assert app.APP_EXE_NAME in text
    assert app.APP_STORAGE_ID in text


# ---------------------------------------------------------------------------
# 11. 双列中缝对齐 KPI 分界 / 卡片区"放得下就不能滚"（v0.5.4）
# ---------------------------------------------------------------------------
def test_kpi_cards_use_symmetric_gaps_and_equal_columns(tk_root, tmp_path):
    """KPI 四张卡必须等宽、间距两侧对称：否则第 2/3 张的分界线不在 50% 上。"""
    window = app.DiskTrackerApp(tk_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                theme=app.THEME_LEDGER)
    window.run_pipeline_sync()
    tk_root.update_idletasks()

    cards = window.kpi_frame.winfo_children()
    assert len(cards) == len(app.KPI_SPECS)
    gaps = [_pixels(card.grid_info().get("padx", "0")) for card in cards]
    half_gap = app.CARD_GAP // 2
    for index, gap in enumerate(gaps):
        left, right = gap[0], gap[-1]
        if index == 0:
            assert (left, right) == (0, half_gap), f"首张卡间距应为 (0,{half_gap})：{gap}"
        elif index == len(gaps) - 1:
            assert (left, right) == (half_gap, 0), f"末张卡间距应为 ({half_gap},0)：{gap}"
        else:
            assert (left, right) == (half_gap, half_gap), f"中间卡间距应两侧对称：{gap}"
        # 每列权重相同且共用 uniform，卡宽才会严格一致
        assert int(window.kpi_frame.columnconfigure(index, "weight")) == 1
        assert str(window.kpi_frame.columnconfigure(index, "uniform")) == "kpi"


def test_gui_kpi_boundary_matches_card_gutter(tk_root, tmp_path):
    """双列卡片的中缝必须落在 KPI 第 2、3 张卡的分界线上（允许 1px 误差）。"""
    import tkinter

    window_root = tkinter.Toplevel(tk_root)
    window = app.DiskTrackerApp(window_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                theme=app.THEME_LEDGER)
    window.run_pipeline_sync()
    window_root.geometry("1240x830")
    window.set_view(app.VIEW_GRID)
    window_root.update()
    window.flush_card_layout()
    window_root.update()

    assert window.kpi_visible is True
    # KPI：用 grid_bbox 取每列的精确占位（含 padx），第 2 列右缘与第 3 列左缘的中点
    first = window.kpi_frame.grid_bbox(1, 0)
    second = window.kpi_frame.grid_bbox(2, 0)
    assert first and second, "KPI 两列都应该有几何信息"
    kpi_boundary = window.kpi_frame.winfo_rootx() + (first[0] + first[2] + second[0]) / 2.0

    cards = window.cards_inner.winfo_children()
    assert len(cards) >= 4, "需要至少 4 张卡才构成两列两行"
    left_card, right_card = cards[0], cards[1]
    card_gutter = (left_card.winfo_rootx() + left_card.winfo_width()
                   + right_card.winfo_rootx()) / 2.0

    assert abs(kpi_boundary - card_gutter) <= 1.0, (
        f"卡片中缝 {card_gutter} 与 KPI 2/3 分界 {kpi_boundary} 没对齐"
    )
    # 中缝还必须落在整行宽度的正中，否则"对齐"只是巧合
    kpi_span = window.kpi_frame.winfo_width() - app.SCROLL_GUTTER
    assert abs((kpi_boundary - window.kpi_frame.winfo_rootx()) - kpi_span / 2.0) <= 1.0
    window_root.destroy()


def test_gui_cards_stay_pinned_when_content_fits(tk_root, tmp_path):
    """内容放得下时：滚轮不能把卡片推下去，内容永远贴顶（消掉大片空白）。"""
    import tkinter

    window_root = tkinter.Toplevel(tk_root)
    window = app.DiskTrackerApp(window_root, provider=app.FixturesProvider(FIXTURES),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                theme=app.THEME_DARK)
    window.run_pipeline_sync()
    window_root.geometry("1240x900")     # 足够高，双列 3 张卡全部可见
    window.set_view(app.VIEW_GRID)
    window_root.update()
    window.flush_card_layout()
    window_root.update()

    canvas = window.cards_canvas
    assert window._cards_fit() is True
    assert canvas.yview() == (0.0, 1.0)
    assert canvas.canvasy(0) == 0
    region = [int(v) for v in str(canvas.cget("scrollregion")).split()]
    assert region[3] >= canvas.winfo_height(), "滚动区域高度至少要覆盖视口"

    # 在卡片最深的控件上滚 8 格，位置必须一点不动
    targets = [w for w in _descendants(window.cards_inner) if w.winfo_class() == "Entry"]
    assert targets
    deepest = targets[-1]
    for _ in range(8):
        deepest.event_generate("<MouseWheel>", delta=-120)
    window_root.update()
    assert canvas.yview() == (0.0, 1.0)
    assert canvas.canvasy(0) == 0
    assert window.cards_inner.winfo_y() == 0
    window_root.destroy()


def test_gui_cards_still_scroll_when_content_overflows(tk_root, tmp_path):
    """内容确实放不下时必须还能滚，否则多盘用户看不到下面的卡片。"""
    import tkinter

    snapshots = [
        app.DiskSnapshot(device_path=f"/dev/nvme{i}", protocol="NVMe",
                         model=f"Fake Disk {i} 1TB", serial=f"SN{i:04d}",
                         capacity_bytes=1024 ** 4,
                         read_bytes=int(6000 * 1024 ** 3),
                         write_bytes=int(7500 * 1024 ** 3),
                         media_errors=0)
        for i in range(8)
    ]
    window_root = tkinter.Toplevel(tk_root)
    window = app.DiskTrackerApp(window_root, provider=_FakeProvider(snapshots),
                                store=_gui_store(tmp_path), autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                theme=app.THEME_DARK)
    window.run_pipeline_sync()
    window_root.geometry("1000x560")
    window.set_view(app.VIEW_CARDS)      # 单列 8 张卡，一定超出视口
    window_root.update()
    window.flush_card_layout()
    window_root.update()

    canvas = window.cards_canvas
    assert window._cards_fit() is False
    start = canvas.yview()[0]
    assert start == 0.0
    for _ in range(10):
        window._on_cards_wheel(type("E", (), {"delta": -120})())
    window_root.update()
    assert canvas.yview()[0] > start, "内容放不下时滚轮必须能滚"
    assert canvas.canvasy(0) > 0
    window_root.destroy()


# ---------------------------------------------------------------------------
# 12. history.json v2：盘档案 + 读数数组（体积只随「天数 × 盘数」增长）
# ---------------------------------------------------------------------------


def _reading(stamp, read_gb, write_gb, key="M|S", media=0, model="M", serial="S"):
    return {
        "timestamp": stamp, "key": key, "model": model, "serial": serial,
        "protocol": "NVMe", "device": "/dev/nvme0",
        "read_gb": read_gb, "write_gb": write_gb, "media_errors": media,
    }


def test_history_v2_file_stores_disk_profile_once(tmp_path):
    """v2 文件结构：型号/序列号/协议/设备每块盘只存一次，每条读数只存 4 个短字段。"""
    store = _store(tmp_path)
    base = datetime(2026, 5, 1, 8, 0, tzinfo=TZ8)
    store.save([_reading((base + timedelta(hours=hour)).isoformat(timespec="seconds"),
                         10.0 + hour, 20.0 + hour)
                for hour in range(3)])

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == app.SCHEMA_VERSION == 2
    assert "records" not in payload, "v2 不再有随运行次数线性膨胀的扁平 records 数组"
    assert len(payload["disks"]) == 1
    disk = payload["disks"][0]
    assert (disk["model"], disk["serial"]) == ("M", "S")
    assert disk["protocol"] == "NVMe" and disk["device"] == "/dev/nvme0"
    assert len(disk["readings"]) == 3
    assert set(disk["readings"][0]) == {"t", "r", "w", "e"}, "读数键名必须压到最短"
    assert disk["readings"][0]["r"] == pytest.approx(10.0)
    assert disk["readings"][0]["e"] == 0

    # 上层拿到的仍然是扁平记录列表，增量计算层完全不用改
    loaded = store.load()
    assert [record["read_gb"] for record in loaded.records] == [10.0, 11.0, 12.0]
    assert loaded.records[0]["model"] == "M"
    assert loaded.records[0]["device"] == "/dev/nvme0"
    assert loaded.migrated is False


def test_history_v2_trim_keeps_first_last_and_one_reading_per_old_day(tmp_path):
    """最近 24 小时原样保留；更早的每天只留最后一条；首条与末条永久保留。"""
    store = _store(tmp_path)
    base = datetime(2026, 1, 1, 0, 0, tzinfo=TZ8)
    written = []
    for day in range(30):
        for hour in (0, 6, 12, 18):
            stamp = (base + timedelta(days=day, hours=hour)).isoformat(timespec="seconds")
            written.append((stamp, 1000.0 + day * 10 + hour))
    store.save([_reading(stamp, read_gb, read_gb / 2) for stamp, read_gb in written])

    readings = json.loads(store.path.read_text(encoding="utf-8"))["disks"][0]["readings"]
    # 最后一天 4 条全在 24h 窗口内；前一天 18 点刚好等于边界（按"仍在窗口内"处理）；
    # 更早的 29 天各折叠成 1 条，但第 1 天因"首条永久保留"多留一条（0 点 + 18 点）。
    assert len(readings) == 35, f"折叠后应剩 35 条，实际 {len(readings)}"
    assert len(readings) < len(written)
    assert readings[0]["t"] == written[0][0], "第一条读数必须永久保留，否则历史累计增量会算错"
    assert readings[-1]["t"] == written[-1][0], "最后一条读数必须永久保留，否则本次区间增量会算错"
    # 更早的日子里同一天最多留两条（首条 + 当天最后一条）
    per_day = {}
    for reading in readings:
        per_day.setdefault(reading["t"][:10], []).append(reading)
    for day_text, items in per_day.items():
        if day_text < "2026-01-29":
            assert len(items) <= 2, f"{day_text} 折叠后仍保留 {len(items)} 条"

    assert len(store.load().records) == 35, "读回来应展开成同样多的扁平记录"


def test_history_v2_growth_is_bounded_and_totals_survive(tmp_path):
    """连跑 90 次（30 天）后，文件体积有上限，且首次记录仍能算出历史累计增量。"""
    store = _store(tmp_path)
    base = datetime(2026, 1, 1, 0, 0, tzinfo=TZ8)
    records: list[dict] = []
    for day in range(30):
        for hour in (2, 10, 18):
            moment = base + timedelta(days=day, hours=hour)
            records.append(_reading(moment.isoformat(timespec="seconds"),
                                    1000.0 + day * 10 + hour, 500.0 + day * 5))
            store.save(list(records))

    first_read = records[0]["read_gb"]
    last_read = records[-1]["read_gb"]
    loaded = store.load()
    assert len(loaded.records) < 90, "v2 必须折叠，否则体积仍随使用时长线性增长"
    assert loaded.records[0]["read_gb"] == pytest.approx(first_read), "首次记录必须还在"
    assert loaded.records[-1]["read_gb"] == pytest.approx(last_read), "最后一条必须还在"
    assert store.path.stat().st_size < 20000, "90 次运行后文件仍应是几十 KB 量级"

    # 增量仍然正确：历史累计相对首次记录，本次区间相对上一条
    snapshot = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe", model="M", serial="S",
                                read_bytes=2000 * 1024 ** 3, write_bytes=900 * 1024 ** 3,
                                media_errors=0)
    rows = {row.key: row for row in app.build_rows(loaded.records, [snapshot])}
    row = rows[app.disk_key("M", "S")]
    assert row.hist_read_delta_gb == pytest.approx(2000.0 - first_read)
    assert row.read_delta_gb == pytest.approx(2000.0 - last_read)


def test_history_v1_file_migrates_to_v2_silently_with_backup(tmp_path):
    """读到 v1 不当作损坏：直接可用；下次写盘前先备份成 history.before-trim-<日期>.json。"""
    store = _store(tmp_path)
    path = store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / "history_with_history.json", path)
    before = path.read_text(encoding="utf-8")
    assert json.loads(before)["schema_version"] == 1

    loaded = store.load()
    assert loaded.recovered is False, "v1 是旧格式而不是损坏，不能备份重建"
    assert loaded.migrated is True
    assert len(loaded.records) == 3
    assert path.read_text(encoding="utf-8") == before, "只读不该改写文件"

    store.save(loaded.records)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert isinstance(payload.get("disks"), list) and len(payload["disks"]) == 2
    backups = list(path.parent.glob(app.HISTORY_BACKUP_PREFIX + "*.json"))
    assert len(backups) == 1, "升级前必须留一份旧文件备份"
    assert json.loads(backups[0].read_text(encoding="utf-8"))["schema_version"] == 1
    assert store.load().migrated is False


def test_history_v2_broken_disk_entry_is_backed_up_and_rebuilt(tmp_path):
    """v2 结构本身坏掉（readings 不是数组）也要备份 + 重建基线，而不是崩溃。"""
    store = _store(tmp_path)
    path = store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 2, "app": app.APP_STORAGE_ID,
        "disks": [{"model": "M", "serial": "S", "readings": "oops"}],
    }, ensure_ascii=False), encoding="utf-8")

    result = store.load()
    assert result.recovered is True
    assert result.records == []
    assert "字段异常" in result.message
    assert len(list(path.parent.glob("history.corrupt-*.json"))) == 1
    assert not path.exists()


def test_v2_reading_without_timestamp_is_rejected(tmp_path):
    """读数缺 t（无法解析）必须按"字段异常"处理。"""
    store = _store(tmp_path)
    path = store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 2, "app": app.APP_STORAGE_ID,
        "disks": [{"model": "M", "serial": "S",
                   "readings": [{"t": "not-a-time", "r": 1.0, "w": 2.0, "e": 0}]}],
    }, ensure_ascii=False), encoding="utf-8")

    result = store.load()
    assert result.recovered is True
    assert "timestamp 无法解析" in result.message


# ---------------------------------------------------------------------------
# 13. 盘符映射层（v0.6.0）
# ---------------------------------------------------------------------------
def _descriptor_bytes(vendor, product, serial, bus_type):
    """按实测偏移拼一个 STORAGE_DEVICE_DESCRIPTOR 缓冲：12/16/24 为字符串偏移，28 为 BusType。"""
    import struct

    buf = bytearray(128)
    offset = 32
    positions = {}
    for name, text in (("vendor", vendor), ("product", product), ("serial", serial)):
        positions[name] = offset
        raw = text.encode("utf-8") + b"\x00"
        buf[offset:offset + len(raw)] = raw
        offset += len(raw)
    struct.pack_into("<I", buf, 0, 40)                    # Version
    struct.pack_into("<I", buf, 8, 40)                    # Size
    struct.pack_into("<I", buf, 12, positions["vendor"])
    struct.pack_into("<I", buf, 16, positions["product"])
    struct.pack_into("<I", buf, 24, positions["serial"])
    struct.pack_into("<I", buf, 28, bus_type)
    return bytes(buf)


def _geometry_bytes(size):
    """DISK_GEOMETRY_EX 的 DiskSize 在偏移 24。"""
    import struct

    buf = bytearray(112)
    struct.pack_into("<q", buf, 24, size)
    return bytes(buf)


def _extents_bytes(*numbers):
    """VOLUME_DISK_EXTENTS：NumberOfDiskExtents(u32) + 保留(4) + DISK_EXTENT[24]。"""
    import struct

    buf = bytearray(8 + 24 * len(numbers))
    struct.pack_into("<I", buf, 0, len(numbers))
    for index, number in enumerate(numbers):
        struct.pack_into("<I", buf, 8 + index * 24, number)
        struct.pack_into("<q", buf, 8 + index * 24 + 8, 0)
        struct.pack_into("<q", buf, 8 + index * 24 + 16, 1024)
    return bytes(buf)


def test_parse_storage_descriptor_reads_model_and_bus_type():
    """描述符偏移固定为 12/16/24/28；厂商与型号要拼成一个型号串并归一化空格。"""
    raw = _descriptor_bytes("Colorful", " SL500 960GB    ", "E2E438EE1A62", app.BUS_TYPE_ATA[1])
    parsed = app.parse_storage_descriptor(raw)
    assert parsed["model"] == "Colorful SL500 960GB", "厂商 + 型号，且多余空格要合并"
    assert parsed["serial"] == "E2E438EE1A62"
    assert parsed["bus_type"] == 7

    nvme = app.parse_storage_descriptor(
        _descriptor_bytes("", "WDC PC SN730 SDBPNTY-256G-1101", "001B_444A_44DD_7B03.", 17))
    assert nvme["model"] == "WDC PC SN730 SDBPNTY-256G-1101", "厂商为空时只留型号"
    assert nvme["bus_type"] == app.BUS_TYPE_NVME

    # 缓冲太短 / 偏移越界都不能抛异常
    assert app.parse_storage_descriptor(b"")["model"] == ""
    assert app.parse_storage_descriptor(bytes(16))["bus_type"] == 0


def test_parse_disk_geometry_and_volume_extents():
    """几何信息取偏移 24 的 DiskSize；卷的盘号来自 extents 记录。"""
    assert app.parse_disk_geometry(_geometry_bytes(960197124096)) == 960197124096
    assert app.parse_disk_geometry(b"") is None
    assert app.parse_disk_geometry(_geometry_bytes(0)) is None

    assert app.parse_volume_extents(_extents_bytes(2)) == [2]
    assert app.parse_volume_extents(_extents_bytes(0, 1)) == [0, 1], "跨盘卷要列出全部盘号"
    assert app.parse_volume_extents(b"") == []
    assert app.parse_volume_extents(bytes(8)) == []


def test_models_close_and_capacities_close_tolerances():
    """型号归一化后相等或互为前缀算同一块盘；容量允许 2 GiB / 2% 的差异。"""
    assert app.models_close("Colorful SL500 960GB", "COLORFUL  SL500  960GB")
    assert app.models_close("WDC PC SN730 SDBPNTY-256G-1101", "WDC PC SN730 SDBPNTY-256G")
    assert not app.models_close("Samsung SSD 980 PRO 1TB", "Samsung SSD 970 EVO 1TB")
    assert not app.models_close("", "Samsung SSD 980 PRO 1TB")

    assert app.capacities_close(960197124096, 960197124096)
    assert app.capacities_close(256060514304, 256010000000), "2 GiB 内视为同一块盘"
    assert not app.capacities_close(256060514304, 512110190592)
    assert not app.capacities_close(0, 0)
    assert not app.capacities_close(None, 100)


def test_match_drive_letters_pairs_by_model_and_capacity():
    """同型号不同容量必须区分开；容量对不上时宁可不给盘符，也不能张冠李戴。"""
    model = "WDC PC SN730 SDBPNTY-256G-1101"
    small = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe", model=model,
                             serial="SN-A", capacity_bytes=256060514304)
    big = app.DiskSnapshot(device_path="/dev/nvme1", protocol="NVMe", model=model,
                           serial="SN-B", capacity_bytes=1024204886016)
    disks = [
        app.PhysicalDisk(number=0, model=model, serial="S0", bus_type=17,
                         capacity_bytes=256060514304),
        app.PhysicalDisk(number=1, model=model, serial="S1", bus_type=17,
                         capacity_bytes=1024204886016),
    ]
    mounts = [
        app.VolumeMount(letter="C:", disk_number=0, capacity_bytes=250000000000),
        app.VolumeMount(letter="D:", disk_number=1, capacity_bytes=1000000000000),
    ]
    mapping = app.match_drive_letters([small, big], disks, mounts)
    assert mapping[app.disk_key(model, "SN-A")] == "C:"
    assert mapping[app.disk_key(model, "SN-B")] == "D:"

    # 型号对得上但容量差一倍以上：不给盘符（避免把另一块盘的数据标上来）
    odd = app.DiskSnapshot(device_path="/dev/sdb", protocol="NVMe", model=model,
                           serial="SN-C", capacity_bytes=512110190592)
    assert app.match_drive_letters([odd], disks, mounts) == {}

    # 总线类型对不上（NVMe 快照 vs ATA 物理盘）也不能匹配
    sata_snapshot = app.DiskSnapshot(device_path="/dev/sda", protocol="SATA", model=model,
                                     serial="SN-D", capacity_bytes=256060514304)
    assert app.match_drive_letters([sata_snapshot], disks, mounts) == {}


def test_match_drive_letters_lists_every_partition_of_one_disk():
    """一块盘分两个区（C: 与 D:）时两个盘符都要列出来，且按字母排序。"""
    model = "Colorful SL500 960GB"
    snapshot = app.DiskSnapshot(device_path="/dev/sda", protocol="SATA", model=model,
                                serial="SN-E", capacity_bytes=960197124096)
    disks = [app.PhysicalDisk(number=2, model=model, serial="E2E438EE1A62", bus_type=7,
                              capacity_bytes=960197124096)]
    mounts = [
        app.VolumeMount(letter="D:", disk_number=2, capacity_bytes=400000000000),
        app.VolumeMount(letter="C:", disk_number=2, capacity_bytes=560000000000),
    ]
    mapping = app.match_drive_letters([snapshot], disks, mounts)
    assert mapping[app.disk_key(model, "SN-E")] == "C:, D:"
    assert app.DRIVE_LETTER_JOINER == ", "


def test_match_drive_letters_skips_ambiguous_and_failed_disks():
    """同型号同容量的两块盘无法区分时宁可不标；读取失败的盘不参与匹配。"""
    model = "Sample NVMe 512GB"
    snapshot = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe", model=model,
                                serial="SN-F", capacity_bytes=512110190592)
    disks = [
        app.PhysicalDisk(number=0, model=model, serial="X", bus_type=17,
                         capacity_bytes=512110190592),
        app.PhysicalDisk(number=1, model=model, serial="Y", bus_type=17,
                         capacity_bytes=512110190592),
    ]
    mounts = [app.VolumeMount(letter="C:", disk_number=0),
              app.VolumeMount(letter="D:", disk_number=1)]
    assert app.match_drive_letters([snapshot], disks, mounts) == {}, "并列时不能猜"

    failed = app.DiskSnapshot(device_path="/dev/sdc", protocol="SATA", model=model,
                              serial="SN-G", error="读取失败", capacity_bytes=512110190592)
    assert app.match_drive_letters([failed], disks[:1], mounts[:1]) == {}


def test_attach_drive_letters_degrades_to_empty_when_no_mounts():
    """枚举不到盘符（非 Windows / 权限不足）时整层静默降级，drives 留空且不抛异常。"""
    snapshot = app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe", model="M", serial="S",
                                read_bytes=1024 ** 3, write_bytes=1024 ** 3)

    def broken(_snapshots):
        raise RuntimeError("枚举失败")

    assert app.attach_drive_letters([snapshot], broken) == {}
    assert snapshot.drives == ""

    def fake(_snapshots):
        return {app.disk_key("M", "S"): "C:"}

    assert app.attach_drive_letters([snapshot], fake) == {app.disk_key("M", "S"): "C:"}
    assert snapshot.drives == "C:"


def test_perform_run_injected_resolver_reaches_cards(tk_root, tmp_path):
    """盘符从 provider -> perform_run -> DiskRow -> 卡片徽章一路贯通（结果与 runner 无关）。"""
    store = _gui_store(tmp_path)
    provider = _FakeProvider([
        app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe",
                         model="Test NVMe", serial="SN1", capacity_bytes=256060514304,
                         read_bytes=300 * 1024 ** 3, write_bytes=400 * 1024 ** 3,
                         media_errors=0),
    ])
    window = app.DiskTrackerApp(
        tk_root, provider=provider, store=store,
        resolver=lambda snapshots: {app.disk_key("Test NVMe", "SN1"): "C:"},
        autostart=False,
        now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8),
        settings=app.SettingsStore(tmp_path / "pref"),
        theme=app.THEME_LEDGER)
    window.run_pipeline_sync()
    window.set_view(app.VIEW_CARDS, persist=False)
    tk_root.update_idletasks()

    assert window.rows[0].drives == "C:"
    labels = [w for w in _descendants(window.cards_inner) if w.winfo_class() == "Label"]
    badge_texts = [label.cget("text") for label in labels]
    assert "NVMe" in badge_texts
    assert "C:" in badge_texts, f"卡片上必须有盘符徽章，实际={badge_texts}"
    protocol_badge = labels[badge_texts.index("NVMe")]
    letter_badge = labels[badge_texts.index("C:")]
    assert protocol_badge.master is letter_badge.master, "盘符徽章要跟协议徽章同一行"
    siblings = protocol_badge.master.winfo_children()
    assert siblings.index(protocol_badge) < siblings.index(letter_badge), \
        "盘符徽章必须排在协议徽章后面"
    window.root.destroy()


def test_cards_hide_drive_badge_when_letter_unknown(tk_root, tmp_path):
    """拿不到盘符时不渲染空徽章（避免出现一个空白小方块）。"""
    store = _gui_store(tmp_path)
    provider = _FakeProvider([
        app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe",
                         model="Test NVMe", serial="SN1",
                         read_bytes=300 * 1024 ** 3, write_bytes=400 * 1024 ** 3,
                         media_errors=0),
    ])
    window = app.DiskTrackerApp(tk_root, provider=provider, store=store,
                                resolver=lambda snapshots: {},
                                autostart=False,
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8),
                                settings=app.SettingsStore(tmp_path / "pref"),
                                theme=app.THEME_LEDGER)
    window.run_pipeline_sync()
    window.set_view(app.VIEW_CARDS, persist=False)
    tk_root.update_idletasks()
    assert window.rows[0].drives == ""
    texts = [str(w.cget("text")) for w in _descendants(window.cards_inner)
             if w.winfo_class() == "Label"]
    assert "NVMe" in texts
    assert not any(text.endswith(":") and len(text) <= 3 for text in texts), \
        f"不该出现空盘符徽章，实际={texts}"
    window.root.destroy()


# ---------------------------------------------------------------------------
# 14. 应用图标（v0.6.0）
# ---------------------------------------------------------------------------
def test_app_icon_exists_and_is_a_multi_size_ico():
    """仓库里必须有可用的 .ico：exe 图标与窗口图标都靠它，缺了构建就该失败。"""
    path = app.locate_app_icon()
    assert path is not None, "找不到 diskflow.ico（应从 diskflow-icons/ 下取到）"
    icon = Path(path)
    assert icon.is_file()
    assert icon.name == app.APP_ICON_FILENAME
    raw = icon.read_bytes()
    assert raw[:4] == b"\x00\x00\x01\x00", ".ico 文件头不对"
    count = int.from_bytes(raw[4:6], "little")
    assert count >= 3, f"图标里至少要有多个尺寸，实际 {count} 个"

    sizes = set()
    for index in range(count):
        offset = 6 + index * 16
        width = raw[offset] or 256
        height = raw[offset + 1] or 256
        assert width == height, "图标条目必须是正方形"
        sizes.add(width)
    assert {16, 32, 256} <= sizes, f"缺少常用尺寸，实际 {sorted(sizes)}"


def test_locate_app_icon_prefers_the_bundled_copy(tmp_path, monkeypatch):
    """打包后图标在 sys._MEIPASS 里，必须优先用它（而不是源码目录那份）。"""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    copied = bundle / app.APP_ICON_FILENAME
    copied.write_bytes(Path(app.locate_app_icon()).read_bytes())
    monkeypatch.setattr(app.providers.sys, "_MEIPASS", str(bundle), raising=False)
    assert app.locate_app_icon() == str(copied)


def test_locate_app_icon_returns_none_instead_of_raising(monkeypatch):
    """图标缺失只返回 None：界面照常启动，绝不能因为图标让程序起不来。"""
    # 拆包后 locate_app_icon 在 providers 模块里，改的是它自己的名字绑定
    monkeypatch.setattr(app.providers, "APP_ICON_FILENAME", "no-such-icon.ico", raising=False)
    assert app.locate_app_icon() is None
    assert app.locate_app_icon([str(Path("missing") / "x.ico")]) is None


def test_apply_window_icon_sets_title_bar_icon(tk_root):
    """窗口图标要真的设上：Windows 下走 iconbitmap(.ico)，任务栏与标题栏同时生效。"""
    window = tkinter.Toplevel(tk_root)
    try:
        used = app.apply_window_icon(window)
        assert used is not None
        assert Path(used).name == app.APP_ICON_FILENAME
        # 路径无效时静默降级，窗口必须还活着
        assert app.apply_window_icon(window, str(Path("missing") / "nope.ico")) is None
        assert window.winfo_exists() == 1
    finally:
        window.destroy()


def test_build_workflow_passes_icon_to_pyinstaller():
    """CI 打包必须带 --icon 并把 .ico 用 --add-binary 打进 onefile 包，否则 exe 会没图标。"""
    workflow = (Path(__file__).resolve().parent / ".github" / "workflows" / "build.yml").read_text(
        encoding="utf-8")
    assert "--icon \"$env:APP_ICON_PATH\"" in workflow, "缺少 --icon"
    assert "--add-data \"$env:APP_ICON_PATH;.\"" in workflow, "缺少图标的 --add-data"
    assert "APP_ICON_PATH" in workflow
    # 图标路径同样从 main.py 读，不在 workflow 里写死文件名
    assert "locate_app_icon" in workflow
    assert "--name \"$env:APP_EXE_NAME\"" in workflow, "名称仍必须是单一配置点"


def test_release_title_is_ascii_exe_name_and_version_only():
    """Release 标题只能是「英文 EXE 名 + 版本号」，不得带中文显示名。"""
    workflow = (Path(__file__).resolve().parent / ".github" / "workflows" / "build.yml").read_text(
        encoding="utf-8")
    title_lines = [line.strip() for line in workflow.splitlines()
                   if line.strip().startswith("name: ") and "app_version" in line]
    assert title_lines, "找不到 Release 标题行"
    for line in title_lines:
        assert "display_name" not in line, "Release 标题不能再用中文显示名"
        assert "steps.names.outputs.exe_name" in line, "Release 标题必须用英文 EXE 名"
        assert line.endswith("app_version " + "}}"), line


def test_build_bat_uses_the_same_single_source_of_names():
    """本地 build.bat 与 CI 必须同源：名称与图标都从 main.py 读，不能各自写死。"""
    script = (Path(__file__).resolve().parent / "build.bat").read_text(encoding="utf-8")
    assert "locate_app_icon" in script
    assert "--icon \"%APP_ICON_PATH%\"" in script
    assert "--add-data \"%APP_ICON_PATH%;.\"" in script
    assert "--name \"%APP_EXE_NAME%\"" in script


# ---------------------------------------------------------------------------
# 10. 表格选区与复制（v0.7.2）
# ---------------------------------------------------------------------------
def _three_disk_window(tk_root, tmp_path):
    """三块盘（其中一块读取失败），用于验证选中行的顺序与范围。"""
    store = _gui_store(tmp_path)
    provider = _FakeProvider([
        app.DiskSnapshot(device_path="/dev/nvme0", protocol="NVMe",
                         model="Alpha NVMe", serial="SN-A",
                         read_bytes=300 * 1024 ** 3, write_bytes=400 * 1024 ** 3,
                         media_errors=0),
        app.DiskSnapshot(device_path="/dev/sdb", protocol="SATA",
                         model="Beta SATA", serial="SN-B",
                         read_bytes=50 * 1024 ** 3, write_bytes=60 * 1024 ** 3,
                         media_errors=None),
        app.DiskSnapshot(device_path="/dev/sdc", protocol="SATA",
                         model="Gamma HDD", serial="SN-C",
                         error="读取失败：Permission denied"),
    ])
    window = app.DiskTrackerApp(tk_root, provider=provider, store=store, autostart=False,
                                settings=app.SettingsStore(tmp_path),
                                now_func=lambda: datetime(2026, 5, 2, 10, 30, tzinfo=TZ8))
    window.run_pipeline_sync()
    tk_root.update_idletasks()
    return window


def test_tree_view_allows_multi_row_selection(tk_root, tmp_path):
    """表格必须支持多选：browse 单选模式下没法按住 Ctrl/Shift 挑几行一起复制。"""
    window = _three_disk_window(tk_root, tmp_path)
    assert str(window.tree.cget("selectmode")) == "extended"
    children = window.tree.get_children()
    # 3 块本次读到的盘 + 1 条历史里存在、本次消失的盘
    assert len(children) == 4
    window.tree.selection_set(children[0], children[2])
    tk_root.update_idletasks()
    assert len(window.tree.selection()) == 2
    window.root.destroy()


def test_copy_selection_keeps_screen_order_not_click_order(tk_root, tmp_path):
    """选中的行按屏幕顺序导出：从下往上选时粘出来不能是倒序。"""
    window = _three_disk_window(tk_root, tmp_path)
    children = window.tree.get_children()
    # 故意倒着选（模拟从下往上拖选）
    window.tree.selection_set(children[2], children[0])
    tk_root.update_idletasks()
    window.copy_selection()

    lines = tk_root.clipboard_get().splitlines()
    assert lines[0] == "\t".join(spec[1] for spec in app.COLUMN_SPECS), "首行必须是表头"
    assert len(lines) == 3, f"选中 2 行 + 表头应 3 行，实际 {len(lines)}"
    assert "Alpha NVMe" in lines[1] and "SN-A" in lines[1]
    assert "Gamma HDD" in lines[2] and "SN-C" in lines[2]
    assert "Beta SATA" not in "".join(lines), "没选中的行不能被复制"
    window.root.destroy()


def test_copy_selection_falls_back_to_whole_table_when_empty(tk_root, tmp_path):
    """没选中任何行时 Ctrl+C 退化为复制整表，不必先框选。"""
    window = _three_disk_window(tk_root, tmp_path)
    window.tree.selection_remove(*window.tree.get_children())
    tk_root.update_idletasks()
    assert window.selected_rows() == []
    assert window.copy_selection_event(None) == "break"
    text = tk_root.clipboard_get()
    # 表头 + 3 块本次读到的盘 + 1 条「本次未检测到」的历史盘（退化为整表时一起带上）
    assert len(text.splitlines()) == 5
    assert "Gamma HDD" in text
    # 复制成功的反馈走单独的绿色行，不再往黄字提示栏里堆
    assert window.action_parts == ["已复制 4 行数据到剪贴板。"]
    assert window.action_label.cget("text") == "已复制 4 行数据到剪贴板。"
    assert not any("已复制" in part for part in window.hint_parts)
    window.root.destroy()


def test_select_all_rows_selects_every_row(tk_root, tmp_path):
    """右键菜单「全选」选中表格里所有行。"""
    window = _three_disk_window(tk_root, tmp_path)
    window.select_all_rows()
    tk_root.update_idletasks()
    # 3 块本次读到的盘 + 1 条历史里存在、本次消失的盘
    assert len(window.tree.selection()) == 4
    assert len(window.selected_rows()) == 4
    assert window.action_parts == ["已选中 4 行，按 Ctrl+C 复制。"]
    assert not any("已选中" in part for part in window.hint_parts)
    window.root.destroy()


def test_copy_cell_copies_only_the_clicked_cell(tk_root, tmp_path):
    """右键落在某个单元格上时只复制那一格，成对列按「 / 」拼成一行。"""
    window = _three_disk_window(tk_root, tmp_path)
    children = window.tree.get_children()
    window._cell_iid = children[0]
    window._cell_column_key = "rw"
    window.copy_cell()
    text = tk_root.clipboard_get()
    assert "\t" not in text, "单元格复制不能带列分隔符"
    assert app.TSV_PAIR_SEPARATOR in text, f"成对列应拼成「读 / 写」，实际 {text!r}"
    # 提示只留「已复制：<数值>」，不再重复一遍「「本次读增 / 写增」单元格：」
    rw_title = dict((spec[0], spec[1]) for spec in app.COLUMN_SPECS)["rw"]
    assert window.action_parts == [f"已复制：{text}"]
    assert rw_title not in "".join(window.action_parts)
    assert "单元格" not in window.action_parts[0]

    # 落点无效时不崩，只给可操作提示
    window._cell_column_key = ""
    window._cell_iid = ""
    window.copy_cell()
    # 落点无效是操作失败，进红字那条，不进绿色反馈行
    assert window.error_parts == ["请先用右键点一下要复制的单元格。"]
    assert window.error_label.cget("text") == "请先用右键点一下要复制的单元格。"
    assert window.action_parts == []
    window.root.destroy()


def test_iid_to_row_map_tracks_rendered_rows(tk_root, tmp_path):
    """_iid_to_row 必须与 Treeview 的 iid 一一对应，复制选中行才取得到数据。"""
    window = _three_disk_window(tk_root, tmp_path)
    children = window.tree.get_children()
    assert list(window._iid_to_row) == list(children)
    for iid in children:
        assert window._iid_to_row[iid].model in window.tree.item(iid, "values")[0]
    # 重新渲染（换排序）后映射必须跟着换，不能留下旧 iid
    window.sort_by("model")
    tk_root.update_idletasks()
    assert list(window._iid_to_row) == list(window.tree.get_children())
    window.root.destroy()


def test_context_menu_binds_cell_and_enables_copy(tk_root, tmp_path):
    """右键处理要记住落点单元格，并据是否命中单元格来开关「复制此单元格」。"""
    window = _three_disk_window(tk_root, tmp_path)
    children = window.tree.get_children()
    # 让它真的映射到屏幕上：Treeview 的 identify_*/bbox 在未映射窗口上恒返回空，
    # 那样就测不到真正的单击命中。真起不来时下面的 skip 会说明原因。
    window.root.deiconify()
    window.root.update()
    window.tree.selection_set(children[1])
    window.tree.see(children[1])
    tk_root.update()

    bbox = window.tree.bbox(children[1])
    if not bbox:
        # 窗口没被映射时 bbox 恒为空，命中测试无从做起 —— 明确 skip，不静默通过
        pytest.skip("无真实布局（窗口未映射），拿不到单元格坐标，跳过右键命中测试")
    x, y, _width, _height = bbox
    event = type("Event", (), {"x": x + 40, "y": y + 5,
                               "x_root": 100, "y_root": 100})()
    # 弹菜单会 grab，测试里不让它真的弹出来
    window.tree_menu.tk_popup = lambda *a, **k: None
    window._on_tree_context_menu(event)
    assert window._cell_iid == children[1]
    assert window._cell_column_key in [spec[0] for spec in app.COLUMN_SPECS]
    assert window.tree_menu.entrycget(window._tree_menu_index[app.TREE_MENU_COPY_CELL],
                                     "state") == "normal"
    window.root.destroy()


def test_context_menu_opens_repeatedly(tk_root, tmp_path):
    """连续右键多次都要能弹菜单。

    回归用：第一次右键会把「复制选中行」的标签改成带「N 行」的样子，若随后仍按
    原标签去 entryconfigure，就会抛 TclError: bad menu entry index，而 Tk 把回调
    异常打到 stderr 后菜单再也不弹——表现正是「首次右键有用，之后无响应」。
    """
    window = _three_disk_window(tk_root, tmp_path)
    children = window.tree.get_children()
    window.root.deiconify()
    window.root.update()
    window.tree.selection_set(children[0])
    window.tree.see(children[0])
    tk_root.update()
    bbox = window.tree.bbox(children[0])
    if not bbox:
        pytest.skip("无真实布局（窗口未映射），拿不到单元格坐标，跳过连续右键测试")

    _x, y, _w, _h = bbox
    popped: list[tuple[int, int]] = []
    window.tree_menu.tk_popup = lambda x_root, y_root: popped.append((x_root, y_root))
    event = type("Event", (), {"x": _x + 40, "y": y + 5, "x_root": 100, "y_root": 100})()
    for round_no in range(3):
        window._on_tree_context_menu(event)
        assert len(popped) == round_no + 1, f"第 {round_no + 1} 次右键没弹出菜单"
    # 标签已带上行数，且「复制此单元格」仍是可用状态
    rows_index = window._tree_menu_index[app.TREE_MENU_COPY_ROWS]
    assert "行" in window.tree_menu.entrycget(rows_index, "label")
    assert window.tree_menu.entrycget(window._tree_menu_index[app.TREE_MENU_COPY_CELL],
                                     "state") == "normal"
    # 弹菜单失败不能静默：异常要变成可见提示
    assert not any("打开右键菜单失败" in part for part in window.hint_parts)
    assert window.error_parts == []
    window.root.destroy()


def test_action_feedback_does_not_accumulate(tk_root, tmp_path):
    """操作反馈只留最新一条：连点三次复制，顶部不能一行行往上堆。

    回归用：旧版把这些提示塞进黄字提示栏并按整串去重，而「已复制 N 行」每次都带
    不同的行数，去重永远拦不住，于是刷新几次后顶栏变成一长串。
    """
    window = _three_disk_window(tk_root, tmp_path)
    for _ in range(3):
        window.copy_table()
        window.select_all_rows()
    tk_root.update_idletasks()
    assert len(window.action_parts) == 1, f"反馈只能有一条，实际 {window.action_parts}"
    assert window.action_parts == ["已选中 4 行，按 Ctrl+C 复制。"]
    text = window.action_label.cget("text")
    assert text.count("已") == 1, f"文案里不该出现多条拼在一起：{text!r}"
    assert not any("已选中" in part for part in window.hint_parts)
    assert not any("已复制" in part for part in window.hint_parts)
    window.root.destroy()


def test_action_row_shows_only_when_present(tk_root, tmp_path):
    """绿色反馈行有内容才占位置，清空后整行收起，不留一条空白带。"""
    window = _three_disk_window(tk_root, tmp_path)
    tk_root.update_idletasks()
    assert window.action_visible is False
    assert window.action_label.cget("text") == ""
    window.select_all_rows()
    tk_root.update_idletasks()
    assert window.action_visible is True
    window._clear_action()
    tk_root.update_idletasks()
    assert window.action_visible is False
    assert window.action_parts == []
    window.root.destroy()


def test_refresh_clears_action_and_failure_feedback(tk_root, tmp_path):
    """刷新记录换掉整张表，旧的「已复制／请先用右键」必须一起清掉。"""
    window = _three_disk_window(tk_root, tmp_path)
    window.select_all_rows()
    tk_root.update_idletasks()
    assert window.action_parts
    outcome = window.run_pipeline_sync()
    tk_root.update_idletasks()
    assert window.action_parts == []
    assert window.action_label.cget("text") == ""
    assert window.action_visible is False
    # 红字那条同样清掉；系统黄字（未提权等）不受影响
    window._set_error("请先用右键点一下要复制的单元格。")
    assert window.error_parts
    window.run_pipeline_sync()
    tk_root.update_idletasks()
    assert window.error_parts == []
    assert window.error_label.cget("text") == ""
    assert outcome is not None
    window.root.destroy()


def test_action_and_error_share_one_slot(tk_root, tmp_path):
    """成功反馈顶掉上一条红字，失败提示顶掉上一条绿字：屏幕上不会同时出现两种结论。"""
    window = _three_disk_window(tk_root, tmp_path)
    window._set_error("请先用右键点一下要复制的单元格。")
    assert window.error_parts == ["请先用右键点一下要复制的单元格。"]
    assert window.action_parts == []
    window.select_all_rows()
    assert window.error_parts == [], "新的成功反馈要把上一条红字顶掉"
    assert window.error_label.cget("text") == ""
    window.tree.selection_remove(*window.tree.get_children())
    window.copy_cell()          # 落点无效 -> 红字
    assert window.action_parts == [], "新的失败提示要把上一条绿字顶掉"
    assert window.action_label.cget("text") == ""
    assert window.error_parts == ["请先用右键点一下要复制的单元格。"]
    window.root.destroy()


def test_action_and_error_labels_use_theme_colors(tk_root, tmp_path):
    """绿色反馈与「✓ 数据已更新」同色；红色失败用 alert 色，二者不能混用。"""
    window = _three_disk_window(tk_root, tmp_path)
    style = window.ttk.Style(window.root)
    action_tag = style.configure("Action.TLabel")
    error_tag = style.configure("Error.TLabel")
    ok_tag = style.configure("Ok.TLabel")
    assert action_tag["foreground"] == window.theme["ok_fg"]
    assert action_tag["foreground"] == ok_tag["foreground"]
    assert error_tag["foreground"] == window.theme["alert_fg"]
    assert error_tag["foreground"] != window.theme["ok_fg"]
    assert error_tag["foreground"] != window.theme["hint_fg"]
    assert window.action_label.cget("style") == "Action.TLabel"
    assert window.error_label.cget("style") == "Error.TLabel"
    window.root.destroy()



def test_tree_menu_index_mapping_survives_label_change(tk_root, tmp_path):
    """_tree_menu_index 里的下标必须和菜单实际项一一对上（分隔线占位也算）。"""
    window = _three_disk_window(tk_root, tmp_path)
    menu = window.tree_menu
    for label, index in window._tree_menu_index.items():
        assert menu.type(index) == "command", f"{label} 的下标 {index} 不是菜单项"
        assert menu.entrycget(index, "label").split("\t")[0] == label
    # 反复同步若干次都不该抛异常（原来从第二次起必抛）
    for count in (1, 2, 3):
        window.tree.selection_set(window.tree.get_children()[:count])
        window._sync_tree_menu()
        label = window.tree_menu.entrycget(window._tree_menu_index[app.TREE_MENU_COPY_ROWS],
                                           "label")
        assert label == f"{app.TREE_MENU_COPY_ROWS}\t{count} 行", f"标签应带行数，实际 {label!r}"
    # 清空选区后退回不带行数的原始标签
    window.tree.selection_remove(*window.tree.get_children())
    window._sync_tree_menu()
    assert window.tree_menu.entrycget(window._tree_menu_index[app.TREE_MENU_COPY_ROWS],
                                     "label") == app.TREE_MENU_COPY_ROWS
    window.root.destroy()


def test_remember_cell_hit_test_without_real_layout(tk_root, tmp_path):
    """命中测试本身要能在无布局时跑：identify_row/identify_column 的输入直接给坐标。

    Treeview 在窗口未映射时 identify_* 都返回空串，所以这里用 monkeypatch 换掉
    这两个方法，专测「命中后记下 iid 与列键」这一层逻辑。
    """
    window = _three_disk_window(tk_root, tmp_path)
    children = window.tree.get_children()
    window.tree.selection_remove(*children)
    window.tree.identify_row = lambda _y: children[1]
    window.tree.identify_column = lambda _x: "#3"      # 第 3 列 = 累计读 / 写（rw）
    assert window._remember_cell(10, 10) is True
    assert window._cell_iid == children[1]
    assert window._cell_column_key == "rw"
    assert window.tree.selection() == (children[1],), "右键应把该行纳入选区"

    # 没点在任何行上：不改变落点、不弹菜单
    window.tree.identify_row = lambda _y: ""
    assert window._remember_cell(10, 9999) is False
    assert window._cell_iid == children[1]
    window.root.destroy()

