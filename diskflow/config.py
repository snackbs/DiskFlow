"""单一配置点：软件名称、版本号、容量换算常量、累计量来源标识、健康度规则。

全仓库所有出现软件名/版本号的地方都必须引用本模块的常量，
用户选定正式名称后只需改这里，窗口标题 / --name / artifact / Release 名会同步跟上。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .i18n import tr

# 项目根目录（源码运行时 = 仓库根目录）。打包后资源走 sys._MEIPASS，
# 这里只服务源码运行与测试，绝不能用来定位 exe 同目录。
PROJECT_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 【单一配置点】软件名称。用户选定正式名称后，只改这三行 + APP_STORAGE_ID 说明即可。
# ---------------------------------------------------------------------------
APP_DISPLAY_NAME = "磁盘流水 DiskFlow"             # 中文显示名（「关于」框、README 用；不进标题栏）
APP_EXE_NAME = "DiskFlow"                          # 英文名：标题栏 / --name / exe 文件名 / artifact 前缀 / Release 标题
APP_ARTIFACT_NAME = "DiskFlow-windows"             # GitHub Actions artifact 名
APP_STORAGE_ID = "DiskUsageTracker"                # 历史目录固定 ID，改名时【不要】跟着改
APP_VERSION = "0.8.2"                              # 版本号（关于框显示；发版 tag 必须与之一致）
APP_AUTHOR = "snackbs"                             # 「关于」里显示的作者
APP_AI_NOTE = "由 AI 辅助编写"                      # 「关于」里的开发方式说明

# 容量换算常量
LBA_BYTES = 512                  # SATA: 1 LBA = 512 字节
NVME_DATA_UNIT_BYTES = 512000    # NVMe: 1 Data Unit = 1000 x 512 字节
BYTES_PER_GB = 1024 ** 3         # 界面统一 1 GB = 1024^3 字节
BYTES_PER_GIB_TEXT = "1 GB = 1024^3 字节"
BYTES_PER_TB = 1024 ** 4         # 1 TB = 1024 GB（累计量双单位显示用）

# SATA 累计读写量的来源优先级
#   Device Statistics 的 Logical Sectors 恒为 512 字节扇区，是权威来源（需要 smartctl -x）
#   SMART 属性 241/242（Total_LBAs_*）的计数单位随主控而异：多数按 512 字节 LBA 计数，
#   部分主控（Silicon Motion / Toshiba 等）按 16 MiB / 32 MiB 块计数，直接用会低几个数量级。
#   因此属性值只作为 Device Statistics 缺失时的兜底，并在界面上提示"可能偏小"。
ATA_DEVSTAT_READ_NAME = "logical sectors read"
ATA_DEVSTAT_WRITE_NAME = "logical sectors written"
COUNTER_SOURCE_NONE = ""
COUNTER_SOURCE_NVME = "NvmeLog"
COUNTER_SOURCE_DEVSTAT = "DeviceStatistics"
COUNTER_SOURCE_ATTR = "SmartAttributes"
COUNTER_SOURCE_TEXT = {
    COUNTER_SOURCE_NVME: "NVMe Data Units",
    COUNTER_SOURCE_DEVSTAT: "Device Statistics（Logical Sectors）",
    COUNTER_SOURCE_ATTR: "SMART 属性 241/242（按 512 字节/LBA）",
}

# 健康度：只显示硬盘自己报告的剩余寿命，绝不估算、绝不用其它指标代替。
#   NVMe: 100 - percentage_used
#   SATA: 按下列优先级取厂商写下的剩余寿命百分比，全都没有则显示"不适用"
SATA_LIFE_ATTRIBUTES = (231, 202, 177, 233, 169)
SATA_LIFE_RAW_PERCENT = (231, 202, 233, 169)   # 这些属性的 raw.value 通常是剩余寿命 %
# 177 Wear_Leveling_Count 的 raw 是磨损循环次数，只有归一化 value 才是剩余百分比
HEALTH_NOT_AVAILABLE = "不适用"

APP_ICON_FILENAME = "diskflow.ico"                  # 应用图标文件名（随 exe 打包）
APP_ICON_DIRNAME = "diskflow-icons"                # 仓库里放图标的目录（源码运行时从这里找）
SMARTCTL_EXE = "smartctl.exe"


def window_title() -> str:
    """窗口标题 / 界面左上角大标题的唯一来源：英文名 + 版本号。

    标题栏只写英文名 APP_EXE_NAME，不带中文显示名——中英文混排在系统标题栏里显长，
    而 exe 文件名、Release 标题、artifact 名用的都是这个名字，对外的名字应保持一致。
    中文显示名仍用于「关于」框与 README，见 APP_DISPLAY_NAME。

    版本号只引用 APP_VERSION，界面里不再出现任何写死的版本字面量；
    发版时改一处常量，窗口标题、左上角标题、关于框会同步跟上。
    """
    return f"{APP_EXE_NAME}  v{APP_VERSION}"


def about_text(history_path: Any = None) -> str:
    """「关于」对话框正文。

    版本号只引用 APP_VERSION，界面里不再出现任何写死的版本字面量；
    发版时改一处常量即可，避免关于框与实际 exe 版本对不上。
    """
    path_text = str(history_path) if history_path else tr("启动后自动确定")
    # 整段作为一个模板交给 tr()：中文界面原样返回，英文界面查表换成英文正文。
    # 名字、版本、路径这些「值」走 kwargs，两边共用同一份，不会出现英文版少一行。
    return tr(
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
        "磁盘读取通常需要管理员权限，未提权时部分磁盘可能读取失败。",
        name=tr(APP_DISPLAY_NAME),
        version=APP_VERSION,
        exe=APP_EXE_NAME,
        author=APP_AUTHOR,
        ai=tr(APP_AI_NOTE),
        units=tr(BYTES_PER_GIB_TEXT),
        path=path_text,
        storage=APP_STORAGE_ID,
    )
