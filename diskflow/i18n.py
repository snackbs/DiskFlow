"""界面语言（i18n）：语言常量、翻译表与查表函数。

设计要点：

* 以「中文原文」作为查表键（gettext 风格）。源码里照旧直接写中文，需要英文时用 tr()
  包一层；中文永远是单一事实来源，新增文案不必先造 key，改文案也不会和 key 对不上。
* 没收录的条目在英文界面下原样退回中文——宁可露出一句中文，也不能显示空串或裸 key。
* tr() 的 lang 参数缺省时用「当前语言」。GUI 在启动与切语言时调 set_language() 把它
  设成界面语言，这样数据层（history / parse / providers）里那些没有 lang 参数的文案
  也会跟着界面一起变，不会出现「菜单是英文、状态列还是中文」的割裂。当前语言存在
  ContextVar 里而不是模块全局，后台读盘线程用 bind_language() 把界面语言带进去。
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# 界面语言
# ---------------------------------------------------------------------------

LANG_ZH = "zh-CN"
LANG_EN = "en"
LANG_ORDER: tuple[str, ...] = (LANG_ZH, LANG_EN)
DEFAULT_LANG = LANG_ZH

# 语言菜单里的名字一律用「母语写法」：中文项永远写简体中文，英文项永远写 English。
# 这样切换语言时菜单项本身不需要跟着翻译，也不会出现「English 被翻成英文」的怪事。
LANG_LABELS = {LANG_ZH: "简体中文", LANG_EN: "English"}

# 当前语言存在 ContextVar 里，不再用模块级可变全局。
#
# 为什么：读盘在后台线程里跑，数据层（parse / history / providers / delta）会在那个
# 线程里调 tr()。若它读的是全局变量，主线程此刻切语言就可能让同一条消息的一半用
# 中文、一半用英文拼出来；反过来后台线程也不该写这个全局。ContextVar 天然按执行
# 上下文隔离：主线程设的值不会漏进别的线程，后台线程要跟着界面语言就用
# bind_language() 显式把当前值带过去。
_current_lang: contextvars.ContextVar[str] = contextvars.ContextVar(
    "diskflow_lang", default=DEFAULT_LANG)


def normalize_lang(value: Any) -> str:
    """把任意输入收敛成受支持的语言代码，非法值回落中文。"""
    if isinstance(value, str):
        text = value.strip()
        if text in LANG_ORDER:
            return text
    return DEFAULT_LANG


def current_language() -> str:
    return _current_lang.get()


def set_language(value: Any) -> str:
    """设置当前语言并返回规范化后的结果（供 GUI 与测试调用）。"""
    lang = normalize_lang(value)
    _current_lang.set(lang)
    return lang


def is_english(lang: Any = None) -> bool:
    return normalize_lang(current_language() if lang is None else lang) == LANG_EN


@contextlib.contextmanager
def bind_language(lang: Any = None) -> Iterator[str]:
    """在 with 块内把「当前语言」切成 lang，退出时还原。

    给后台线程用：worker 启动时 bind_language(界面语言)，整个读盘过程里所有 tr()
    都固定在同一个语言上，主线程随后再切语言也不会让本次结果串味。
    """
    token = _current_lang.set(normalize_lang(current_language() if lang is None else lang))
    try:
        yield current_language()
    finally:
        _current_lang.reset(token)


# ---------------------------------------------------------------------------
# 翻译表：中文原文 -> 英文
# ---------------------------------------------------------------------------
_EN: dict[str, str] = {
    # ---- 一级菜单与子项（括号里的字母是 Alt 助记符对应的字母，不能丢）----
    "文件(F)": "File(F)",
    "主题(T)": "Theme(T)",
    "工具(U)": "Tools(U)",
    "硬盘(D)": "Disks(D)",
    "帮助(H)": "Help(H)",
    "语言(L)": "Language(L)",
    "刷新记录(R)": "Refresh(R)",
    "打开历史文件(O)": "Open History File(O)",
    "重置历史文件(E)": "Reset History File(E)",
    "退出(X)": "Exit(X)",
    "复制选中行(C)": "Copy Selected Rows(C)",
    "复制表格到剪贴板(T)": "Copy Table to Clipboard(T)",
    "打开数据目录(D)": "Open Data Folder(D)",
    "排序(O)": "Sort(O)",
    "恢复默认顺序(R)": "Restore Default Order(R)",
    "重置排序(S)": "Reset Sort(S)",
    "显示序列号": "Show Serial Numbers",
    "重新扫描并记录(R)": "Rescan and Record(R)",
    "数据源状态(S)": "Data Source Status(S)",
    "复制诊断信息(I)": "Copy Diagnostics(I)",
    "如何安装数据源(S)": "How to Install the Data Source(S)",
    "容量与换算说明(C)": "Capacity and Units(C)",
    "关于(A)": "About(A)",
    # ---- 工具菜单里的系统工具快捷入口 ----
    "打开磁盘管理(M)": "Open Disk Management(M)",
    "打开设备管理器(V)": "Open Device Manager(V)",
    "磁盘管理": "Disk Management",
    "设备管理器": "Device Manager",
    # ---- 工具 → 排序子菜单 ----
    "按磁盘型号": "By disk model",
    "按累计读取": "By total read",
    "按距上次时间": "By time since last record",
    "按本次读增": "By read delta",
    "按历史读增": "By historic read delta",
    "按 0E 状态": "By 0E status",
    "按状态": "By status",
    # ---- 表格右键菜单 ----
    "复制此单元格(L)": "Copy This Cell(L)",
    "全选(A)": "Select All(A)",
    "复制整表(T)": "Copy Whole Table(T)",
    # ---- 表格列头 ----
    "磁盘 / 序列号 / 容量": "Disk / Serial / Capacity",
    # 英文列头必须短：Treeview 的 heading 有内边距，列宽只有 78~136px，
    # 写成 "Historic Read / Write Delta"（167px）会被截成 "Historic Read / Wi"。
    "协议": "Protocol",
    "累计读 / 写": "Total Read / Write",
    "距上次": "Since Last",
    "本次读增 / 写增": "This Run Δ R/W",
    "历史读增 / 写增": "All-time Δ R/W",
    "0E / 健康": "0E / Health",
    "状态": "Status",
    # ---- 顶部 KPI 指标卡 ----
    "本次总读增": "Read delta (this run)",
    "本次总写增": "Write delta (this run)",
    "历史总读增": "Historic read delta",
    "历史总写增": "Historic write delta",
    # ---- 视图切换 ----
    "表格": "Table",
    "卡片": "Cards",
    "双列": "Two columns",
    # ---- 卡片里的指标格 ----
    "累计读取": "Total read",
    "累计写入": "Total write",
    "本次读增": "Read delta",
    "本次写增": "Write delta",
    "历史读增": "Historic read delta",
    "历史写增": "Historic write delta",
    "0E": "0E",
    "健康度": "Health",
    # ---- 主题名 ----
    "跟随系统": "Follow system",
    "数据台账": "Ledger",
    "深色专业": "Dark",
    "清爽仪表": "Gauge",
    "极简留白": "Minimal",
    "原生默认": "Native",
    # ---- 状态栏 ----
    "上次记录：—": "Last record: —",
    "本次记录：—": "This record: —",
    # 带时间戳的两个模板：时间本身（2026-04-30 08:00:00 (UTC+8)）不翻译，
    # 只换前面的标签，所以走 kwargs 而不是整串查表。
    "上次记录：{stamp}": "Last record: {stamp}",
    "本次记录：{stamp}": "This record: {stamp}",
    "数据源：未检测": "Data source: not checked",
    # 「数据源：」后面那截单独用（诊断信息里要自己拼标签）
    "未检测": "not checked",
    "读取异常": "read error",
    "权限：检测中": "Privilege: checking",
    "权限：管理员": "Privilege: administrator",
    "权限：未以管理员运行": "Privilege: not elevated",
    "未以管理员运行，部分磁盘可能读取失败。":
        "Not running as administrator — some disks may fail.",
    "数据源：读取中…": "Data source: reading…",
    "数据源：读取异常": "Data source: read error",
    "读取中…": "Reading…",
    # 看门狗超时后「数据源」那一行：这一轮已经放开界面了，但 worker 还在后台跑
    "读取超时，后台仍在尝试": "Read timed out; still retrying in the background",
    # ---- 底部条 ----
    "刷新记录": "Refresh",
    "关于": "About",
    "尚无记录": "No records yet",
    "已记录基线，下次打开将显示增量": "Baseline recorded. Deltas will appear next time.",
    "介质错误在累积，建议关注备份":
        "Media errors increasing — back up soon.",
    # ---- 成功/失败确认行 ----
    "已记录基线": "Baseline recorded",
    "数据已更新并保存": "Records updated",
    "✓ {verb}（{count} 块盘）": "✓ {verb} ({count} disks)",
    "{count} 块读取失败": "{count} disk(s) failed to read",
    # ---- 底部注释行 ----
    "已记录基线，下次打开将显示增量。历史文件：{path}（{size}）":
        "Baseline recorded. Deltas will appear next time. History file: {path} ({size})",
    "容量换算：{units}；历史文件：{path}（{size}）":
        "Units: {units}; history file: {path} ({size})",
    # ---- 底部汇总 ----
    # 汇总行与右侧两个按钮同处一行，英文必须压在 620px 以内，
    # 否则会被挤成 "Historic read Δ +9,657.00 G" 这种截断。
    "本次总读增 {read} ｜ 本次总写增 {write} ｜ 历史总读增 {hread} ｜ 历史总写增 {hwrite}":
        "Read Δ {read} | Write Δ {write} | "
        "All-time read Δ {hread} | All-time write Δ {hwrite}",
    # ---- 右键「复制选中行」的计数后缀 ----
    "{count} 行": "{count} rows",
    # ---- 诊断信息（复制诊断信息 / 数据源状态用）----
    "{name} v{version}（{exe}.exe）": "{name} v{version} ({exe}.exe)",
    "Python：{version} / {platform}": "Python: {version} / {platform}",
    "主题：{theme} → {label}": "Theme: {theme} → {label}",
    "权限：{value}": "Privilege: {value}",
    "数据源：{value}": "Data source: {value}",
    "历史文件：{path}（schema v{version}）": "History file: {path} (schema v{version})",
    "偏好文件：{path}": "Preferences file: {path}",
    "视图：{value}": "View: {value}",
    "序列号：{value}": "Serial: {value}",
    "磁盘行数：{count}": "Disk rows: {count}",
    # ---- 操作反馈（绿色行）----
    "已复制 {count} 行数据到剪贴板。": "Copied {count} rows to the clipboard.",
    "已选中 {count} 行，按 Ctrl+C 复制。":
        "Selected {count} rows. Press Ctrl+C to copy.",
    "已复制：{value}": "Copied: {value}",
    "已复制诊断信息到剪贴板。": "Diagnostics copied to the clipboard.",
    "已打开 {name}。": "Opened {name}.",
    # ---- 操作失败（红色行）----
    "请先用右键点一下要复制的单元格。": "Right-click a cell first.",
    "复制到剪贴板失败：{error}": "Clipboard copy failed: {error}",
    "偏好保存失败：{error}": "Could not save preferences: {error}",
    "读取失败：{error}": "Read failed: {error}",
    "打开右键菜单失败：{error}": "Could not open the context menu: {error}",
    "打开数据目录失败：{error}": "Could not open the data folder: {error}",
    "打开历史文件失败：{error}": "Could not open the history file: {error}",
    "打开工具失败：{error}": "Could not open the tool: {error}",
    "重置历史文件失败：{error}": "Could not reset the history file: {error}",
    "复制诊断信息失败：{error}": "Could not copy diagnostics: {error}",
    # ---- 提示（黄色行）----
    "未找到 smartctl.exe，请执行：winget install smartmontools.smartmontools":
        "smartctl.exe was not found. Run: winget install smartmontools.smartmontools",
    "历史文件已重置（旧文件未能备份，可能已被占用）。":
        "History file reset (the old file could not be backed up; it may be locked).",
    "历史文件已重置，旧文件备份为 {name}。":
        "History file reset; the old file was backed up as {name}.",
    # ---- 0E / 健康 ----
    "不适用": "N/A",
    "不可用": "n/a",
    "稳定": "Stable",
    "稳定（计数较上次减小）": "Stable (count decreased)",
    "⚠ 0E 新增 {delta}": "⚠ 0E +{delta}",
    # ---- 状态列（数据层生成）----
    "（未知型号）": "(unknown model)",
    "未知": "Unknown",
    "读取失败": "Read failed",
    "新盘首次记录，暂无增量对比": "First record for this disk; no delta yet",
    "累计读数回退，疑似计数重置":
        "Read counter went backwards (possible counter reset)",
    "累计写数回退，疑似计数重置":
        "Write counter went backwards (possible counter reset)",
    "已更新记录": "Recorded",
    "重映射 {reallocated} / 待映射 {pending}":
        "Reallocated {reallocated} / Pending {pending}",
    "{status}（{extra}）": "{status} ({extra})",
    "{status}（累计量取自 SMART 241/242，可能偏小）":
        "{status} (totals come from SMART 241/242 and may be understated)",
    "本次未检测到（已移除或读取失败）":
        "Not detected this time (removed or unreadable)",
    # ---- 时间 ----
    "时钟回拨 {duration}": "Clock went back {duration}",
    "（本地时间）": " (local time)",
    # ---- 历史文件消息 ----
    "首次运行：将建立基线记录": "First run: a baseline will be recorded",
    "历史文件读取失败（{error}），已重建基线":
        "Could not read the history file ({error}); the baseline was rebuilt",
    "history.json 已损坏，已备份为 {name} 并重建基线":
        "history.json was corrupt; backed up as {name} and the baseline was rebuilt",
    "history.json 字段异常（{problem}），已备份为 {name} 并重建基线":
        "history.json had unexpected fields ({problem}); backed up as {name} "
        "and the baseline was rebuilt",
    "历史文件已升级为新格式（v2），旧文件备份为 {name}":
        "History upgraded to v2; old file backed up as {name}",
    # 备份文件名模板里的日期占位（是说明文字，不是真实文件名的一部分）
    "<日期>": "<date>",
    "根节点不是对象": "the root node is not an object",
    "disks 不是数组": "disks is not a list",
    "缺少 records 数组": "the records list is missing",
    "第 {index} 块盘不是对象": "disk {index} is not an object",
    "第 {index} 块盘的读数不是数组": "disk {index} has no readings list",
    "第 {index} 块盘的第 {inner} 条读数不是对象":
        "reading {inner} of disk {index} is not an object",
    "第 {index} 块盘的第 {inner} 条读数 timestamp 无法解析":
        "reading {inner} of disk {index} has an unparsable timestamp",
    "第 {index} 块盘的第 {inner} 条读数 {field} 不是数字":
        "reading {inner} of disk {index} has a non-numeric {field}",
    "第 {index} 条记录不是对象": "record {index} is not an object",
    "第 {index} 条记录缺少字段 {field}": "record {index} is missing field {field}",
    "第 {index} 条记录 timestamp 无法解析":
        "record {index} has an unparsable timestamp",
    "第 {index} 条记录 {field} 不是数字":
        "record {index} has a non-numeric {field}",
    # ---- 数据读取层状态 ----
    "未找到 smartctl.exe（可执行 winget install smartmontools.smartmontools）":
        "smartctl.exe not found (run: winget install smartmontools.smartmontools)",
    "未发现可用磁盘（可能需要管理员权限）":
        "No usable disks found (administrator rights may be required)",
    "已读取 {count} 块磁盘（smartctl: {path}）":
        "Read {count} disks (smartctl: {path})",
    "读取失败：未找到 smartctl.exe": "Read failed: smartctl.exe not found",
    "读取失败：smartctl 执行超时": "Read failed: smartctl timed out",
    "读取超时：{seconds} 秒内未返回，后台仍在尝试，可稍后手动刷新":
        "Read timed out after {seconds}s; still retrying in the background",
    "读取失败：本次读取总预算已用尽，已跳过该磁盘":
        "Read failed: total read budget exhausted, disk skipped",
    "读取失败：smartctl 输出无法解析": "Read failed: unparsable smartctl output",
    "读取失败：smartctl 未返回磁盘信息":
        "Read failed: smartctl returned no disk information",
    "读取失败：smartctl 输出不是有效 JSON 对象":
        "Read failed: smartctl output is not a valid JSON object",
    "未知型号": "Unknown model",
    "未知序列号": "Unknown serial",
    # ---- 容量与健康度说明 ----
    "1 GB = 1024^3 字节": "1 GB = 1024^3 bytes",
    "NVMe Data Units": "NVMe data units",
    "Device Statistics（Logical Sectors）": "Device Statistics (logical sectors)",
    "SMART 属性 241/242（按 512 字节/LBA）": "SMART attributes 241/242 (512-byte LBA)",
    # ---- 提示框标题与正文 ----
    "数据源状态": "Data Source Status",
    "如何安装数据源": "How to Install the Data Source",
    "容量与换算说明": "Capacity and Units",
    "重置历史文件": "Reset History File",
    "关于 {title}": "About {title}",
    "本程序通过 smartmontools 的 smartctl 读取磁盘累计读写量与 0E。\n"
    "\n"
    "通常 exe 已自带一份，无需安装；若顶部提示未找到，可任选一种：\n"
    "1) winget install smartmontools.smartmontools\n"
    "2) 到 smartmontools 官网下载安装包，默认装到\n"
    "   C:\\Program Files\\smartmontools\\bin\\\n"
    "\n"
    "安装后点「文件 → 刷新记录」即可。读取磁盘信息通常需要管理员权限。":
        "This program reads cumulative read/write totals and 0E through "
        "smartctl from smartmontools.\n"
        "\n"
        "The exe normally bundles a copy, so nothing needs to be installed. "
        "If the top bar says it is missing, pick either way:\n"
        "1) winget install smartmontools.smartmontools\n"
        "2) Download the installer from the smartmontools website; it installs to\n"
        "   C:\\Program Files\\smartmontools\\bin\\\n"
        "\n"
        "Then click File → Refresh. Reading disks usually requires "
        "administrator rights.",
    "将清空全部历史记录并重建基线：\n{path}\n\n"
    "旧文件会改名备份为 history.reset-<时间戳>.json，不会直接删除。\n"
    "重置后立即重新记录一次。确定继续吗？":
        "This clears all history and rebuilds the baseline:\n{path}\n\n"
        "The old file is renamed to history.reset-<timestamp>.json rather than "
        "deleted.\nA new record is taken right after the reset. Continue?",
    "统一口径：{units}\n"
    "\n"
    "表格首列括号外的容量是厂商标称值（按 1 GB = 1,000,000,000 字节），\n"
    "括号内是系统显示口径（按 1 GiB = 1024^3 字节），所以同一块盘\n"
    "会同时看到「960 GB」和「894.25 GiB」，两者说的是同一个字节数。\n"
    "括号部分默认不显示，可在「工具 → 显示系统口径容量」打开或关闭；\n"
    "开关只影响显示，history.json 里存的始终是字节数与 GB 数值。\n"
    "\n"
    "累计读取 / 累计写入来自硬盘自己维护的计数器：\n"
    "· NVMe：Data Units Read / Written，1 Data Unit = 512000 字节\n"
    "· SATA：Device Statistics 的 Logical Sectors，1 LBA = 512 字节\n"
    "  （该字段缺失时才退回 SMART 属性 241/242，部分主控按块计数，数值可能偏小）\n"
    "\n"
    "0E 是 NVMe 的「介质与数据完整性错误」计数，只增不减；\n"
    "SATA 盘没有这个字段，显示「不适用」，可参考重映射扇区与待定扇区。":
        "Units: {units}\n"
        "\n"
        "The capacity outside the parentheses in the first column is the vendor "
        "rating (1 GB = 1,000,000,000 bytes).\n"
        "The value inside uses the operating system convention "
        "(1 GiB = 1024^3 bytes), so one disk\n"
        "shows both \"960 GB\" and \"894.25 GiB\" — the same byte count.\n"
        "The parentheses are hidden by default; toggle them under "
        "Tools → Show System Capacity.\n"
        "The switch only affects display — history.json always stores bytes "
        "and GB values.\n"
        "\n"
        "Cumulative read/write totals come from counters the drive maintains:\n"
        "· NVMe: Data Units Read / Written, 1 data unit = 512000 bytes\n"
        "· SATA: Logical Sectors in Device Statistics, 1 LBA = 512 bytes\n"
        "  (only when that field is missing do we fall back to SMART attributes "
        "241/242; some\n"
        "  controllers count in blocks, so those totals may be understated)\n"
        "\n"
        "0E is the NVMe media and data integrity error count; it only grows.\n"
        "SATA drives do not have that field and show N/A instead; use reallocated "
        "and pending sectors there.",
    # ---- 关于框正文（整段作为一个模板，值全部走 kwargs）----
    "{name}\n"
    "版本：v{version}\n"
    "可执行文件：{exe}.exe\n"
    "作者：{author}\n"
    "开发方式：{ai}\n"
    "\n"
    "记录本机各物理磁盘的累计读取量与累计写入量，并在每次打开时与上一次记录对比，\n"
    "给出本次区间增量与历史累计增量；同时监测 NVMe 的 0E（介质与数据完整性错误）。\n"
    "\n"
    "数据来源：smartctl -x -a --json（smartmontools），随 exe 一同打包\n"
    "容量换算：{units}；1 LBA = 512 字节，1 NVMe Data Unit = 512000 字节\n"
    "累计读写：优先取 smartctl 的 Device Statistics（Logical Sectors，恒为 512 字节扇区），\n"
    "          该字段缺失时才退回 SMART 属性 241/242；累计量超过 1 TB 时同时显示 TB 与 GB。\n"
    "健康度：只显示硬盘自报的剩余寿命（NVMe 100 − 已用百分比；\n"
    "        SATA 依次取 231/202/177/233/169 属性），没有该字段显示「不适用」，不做推算。\n"
    "历史文件：{path}\n"
    "历史目录 ID：{storage}（显示名改名不影响历史数据；安装到 Program Files 时\n"
    "           history.json 与 settings.json 都改存 %LOCALAPPDATA%\\{storage}）\n"
    "\n"
    "本程序为单窗口工具，关闭即退出，不驻留后台、不写系统服务、不设置开机自启。\n"
    "磁盘读取通常需要管理员权限，未提权时部分磁盘可能读取失败。": (
        "{name}\n"
        "Version: v{version}\n"
        "Executable: {exe}.exe\n"
        "Author: {author}\n"
        "Development: {ai}\n"
        "\n"
        "Tracks the cumulative read and write totals of every physical disk and compares "
        "them with the previous record each time it starts, showing the delta for this run "
        "and the total since the first record; it also watches the NVMe 0E "
        "(media and data integrity errors) counter.\n"
        "\n"
        "Data source: smartctl -x -a --json (smartmontools), bundled with the exe\n"
        "Units: {units}; 1 LBA = 512 bytes, 1 NVMe data unit = 512000 bytes\n"
        "Totals: Device Statistics (logical sectors, always 512-byte sectors) is preferred; "
        "SMART attributes 241/242 are only used when that field is missing. "
        "Totals above 1 TB show both TB and GB.\n"
        "Health: only the remaining life the drive reports itself (NVMe: 100 minus the used "
        "percentage; SATA: attributes 231/202/177/233/169 in that order). When the field is "
        "absent it shows N/A; nothing is extrapolated.\n"
        "History file: {path}\n"
        "History directory ID: {storage} (renaming the display name does not affect "
        "history data; when installed under Program Files, history.json and settings.json "
        "go to %LOCALAPPDATA%\\{storage} instead)\n"
        "\n"
        "This is a single-window tool: closing it exits, with no background process, "
        "no system service and no autostart.\n"
        "Reading disks usually requires administrator rights; without elevation some "
        "disks may fail to read."
    ),
    "显示系统口径容量": "Show System Capacity",
    "系统口径容量：{value}": "System capacity: {value}",
    # ---- 其他 ----
    # 中文显示名在英文界面只留英文部分：产品名不翻译，但没必要拖着一串汉字。
    "磁盘流水 DiskFlow": "DiskFlow",
    "由 AI 辅助编写": "AI-assisted",
    "启动后自动确定": "determined at startup",
    "管理员": "administrator",
    "未以管理员运行": "not elevated",
    "显示": "shown",
    "已隐藏": "hidden",
    # ---- 第 7 套「图片背景」主题 ----
    "图片背景（Beta）": "Photo Background (Beta)",
    "选择背景图…": "Choose Background Image…",
    "清除背景图": "Clear Background Image",
    "背景图透明度(A)": "Background Opacity(A)",
    "不透明度 {value}%": "Opacity {value}%",
    "背景图已载入：{name}": "Background loaded: {name}",
    "背景图读不出来，已退回纯色面板：{error}": "Could not read the background image; "
                                               "using a solid panel instead: {error}",
    "背景图已清除": "Background cleared",
    "图片文件": "Image files",
    "打开文件对话框失败：{error}": "Could not open the file dialog: {error}",
}


def tr(text: str, lang: Any = None, **kwargs: Any) -> str:
    """把一条中文界面文案翻成目标语言（默认当前语言）。

    * 中文：原样返回（但仍支持 {name} 占位符格式化，方便调用方统一写法）；
    * 英文：查表；查不到就退回中文，绝不返回空串；
    * 只在给了 kwargs 时才做 format()，避免用户数据里的花括号被当成占位符。
    """
    text = "" if text is None else str(text)
    if is_english(lang):
        translated = _EN.get(text)
        if translated is None:
            translated = text
    else:
        translated = text
    if kwargs:
        try:
            return translated.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return translated
    return translated


@dataclass(frozen=True)
class LocalizedMessage:
    """一条**还没决定用哪种语言显示**的文案：中文原文键 + 占位符参数。

    为什么不能只存成品字符串：GUI 底部的黄色提示行与「数据源：」要在切语言时原地
    重画，而 tr() 一调就把当时的语言焊死在字符串里了，切回中文只能靠重新启动。
    于是这里只留「键 + 参数」，真正取文案时再调用 text()，界面语言变了它就跟着变。

    key 一定是用中文原文作键（与 tr() 的查表方式一致）；查不到英文时退回中文。
    """

    key: str
    params: dict[str, Any] = field(default_factory=dict)

    def text(self, lang: Any = None) -> str:
        """按目标语言拼出成品文案（lang 缺省为当前语言）。

        参数值本身也可以是 LocalizedMessage（例如「旧文件备份为 {name}」里的 name
        还嵌着「<日期>」这段说明文字）：先按同一目标语言把内层拼好再代入，
        否则外层跟着语言翻了、嵌在参数里的那一截仍是派活时那门语言。
        """
        if self.params:
            params = {
                name: (value.text(lang) if isinstance(value, LocalizedMessage) else value)
                for name, value in self.params.items()
            }
        else:
            params = {}
        return tr(self.key, lang, **params)

    def __str__(self) -> str:
        return self.text()

    def __bool__(self) -> bool:
        # 让 "if message:" 这类判断与空字符串一致：没有键就等于没有话说
        return bool(self.key.strip())


ERROR_PREFIX = "读取失败："
ERROR_PREFIX_EN = "Read failed: "


def tr_message(text: Any, lang: Any = None, **kwargs: Any) -> str:
    """翻译「数据层拼好的成品串」，主要给 snapshot.error 这类文案用。

    数据层刻意保留中文原文（history.json 与测试都依赖它），显示层才翻译：
    先按整串查表（覆盖 "读取失败：未找到 smartctl.exe" 这类固定句），
    查不到且是英文时，把固定的 "读取失败：" 前缀换成 "Read failed: "，
    后半段（往往是 smartctl 自己的英文报错）原样保留。
    """
    raw = "" if text is None else str(text)
    if not raw:
        return ""
    if not is_english(lang):
        return raw
    if raw in _EN:
        return tr(raw, lang, **kwargs)
    if raw.startswith(ERROR_PREFIX):
        rest = raw[len(ERROR_PREFIX):].strip()
        translated = tr(rest, lang)
        return f"{ERROR_PREFIX_EN}{translated}" if translated else ERROR_PREFIX_EN.strip()
    return tr(raw, lang, **kwargs)


def has_translation(text: str) -> bool:
    """该条文案是否已收录英文（测试用来守住翻译表的完整性）。"""
    return text in _EN


def untranslated(keys) -> list[str]:
    """从给定文案里挑出还没有英文的条目（按原顺序）。"""
    return [key for key in keys if key not in _EN]
