# 磁盘流水 DiskFlow

<p align="center">
  <img src="diskflow-icons/png/diskflow-256.png" width="128" alt="DiskFlow 图标">
</p>

Windows 桌面小工具：读取本机每块物理磁盘（SATA SSD / NVMe SSD / HDD）的**累计读写量**与
**0E 介质错误**，把每次读数追加进 `history.json`，并与上一次、第一次记录做增量对比。

- 单窗口 tkinter/ttk，运行时零第三方依赖，关闭即退出——没有后台进程、没有系统服务、不开机自启。
- 产物是 PyInstaller onefile 单文件 exe，`--noconsole` + `--uac-admin`。
- 测试与打包都在 GitHub Actions 的 `windows-latest` 上真实执行，使用者不需要在本地装 Python。

## 功能

| 能力 | 说明 |
| --- | --- |
| 自动记录 | 打开即读取一次全部物理盘并写入历史，之后每次打开自动与上次对比 |
| 增量对比 | 距上次的时间间隔、本次区间读写增量、相对首次记录的历史累计增量 |
| 0E 监测 | NVMe 的 *Media and Data Integrity Errors* 增加时整行标红并告警 |
| 健康度 | 只显示硬盘自己报告的剩余寿命，不估算、不拿其它指标冒充 |
| 历史可查 | 点「打开历史文件」直接定位 `history.json`，格式为可读 JSON |
| 主题跟随系统 | Windows 浅色/深色模式自动套用对应配色，也可在「主题」菜单里固定选一套 |
| 三种视图 | 表格（8 列，成对指标上下两行）/ 卡片 / 双列，右上角一键切换，选择会被记住 |
| 菜单栏 | 刷新、打开历史、复制表格、诊断信息、数据源状态、关于等入口都在菜单里 |

## 使用

1. 从 [Releases](../../releases) 下载 `DiskFlow.exe`（或从 Actions 的 Artifacts 里取）。
2. 双击运行，弹出 UAC 时选择「是」——读 SMART 需要管理员权限。
3. **首次打开**只建立基线，底部提示「已记录基线，下次打开将显示增量」。
4. **之后每次打开**自动读取 + 记录 + 对比，表格给出「距上次」「本次读增/写增」「历史读增/写增」。
5. 拷完大文件想立刻看效果，点底部**刷新记录**即可再记录一次。

关闭窗口程序即退出，不残留任何进程。

## 三种视图

右上角有「表格 / 卡片 / 双列」分段切换，选中的视图存进 `settings.json`，下次打开沿用。

**表格**（默认）8 列，成对的指标压成一格、上下两行显示，因此 13 项数据一屏放得下、
不用横向拖动：

| 列 | 上行 / 下行 |
| --- | --- |
| 磁盘 / 序列号 | 型号 / 完整序列号 |
| 协议 | NVMe、SATA |
| 累计读 / 写 | 累计读取 GB / 累计写入 GB |
| 距上次 | 距上次记录的时间间隔 |
| 本次读增 / 写增 | 本次区间读取增量 / 写入增量 |
| 历史读增 / 写增 | 相对首次记录的历史累计读增 / 写增 |
| 0E / 健康 | 0E 原始值或「不适用」 / 硬盘自报剩余寿命 |
| 状态 | 已更新记录、读取失败原因、新盘首次记录等 |

**卡片**把每块盘的所有指标摊开成一张卡，**双列**是同样的卡片两列排布（两列等宽、各让一半
列间距），适合盘少时逐项看。卡片里的四个增量格自带 `GB` 单位，不用回头找列头；型号后面
紧跟 NVMe / SATA 徽章，一眼就能看出协议；协议徽章后面还有盘符徽章（`C:`、`C:, D:`），
标明这块盘在本机对应哪个分区（拿不到盘符时这一格不显示）。卡片文字是只读输入框，
可以框选、双击选词、`Ctrl+C` 复制序列号。

表格与卡片共用同一份排序结果，可在「工具 → 排序」里按任意指标排序。

表格支持点击列标题排序。行的颜色含义：

- **红色**：这块盘的 0E（介质错误）比上次多了，说明介质层面出现新的不可纠正错误。
- **灰色**：这块盘本次读取失败（例如被其它程序独占、或权限不足），状态列会写明原因。
- 正常行使用系统默认前景色。

## 主题与菜单

默认**跟随系统**：Windows 浅色模式用「数据台账」（深色表头 + 斑马纹 + 顶部四张指标卡），
深色模式用「深色专业」（深底浅字 + 暗红告警行）。也可以在「主题」菜单里固定选
数据台账 / 深色专业 / 清爽仪表 / 极简留白 / 原生默认，选「跟随系统」恢复自动判断。

选择会存进 exe 同目录的 `settings.json`。主题偏好是独立文件，**不写进 `history.json`**，
换皮肤不会影响历史数据。标题栏与原生菜单栏都由系统绘制、始终跟随系统配色，
不随主题变深——深色主题下窗口边框保持系统样式，避免出现「深色标题栏 + 浅色菜单栏」的割裂。

表格与卡片的滚动条是自绘的细圆角条（`ttk.Scrollbar` 画不出圆角），颜色跟着主题走。
内容一屏放得下时它不会消失，而是收成一条很淡的细轨，保证右侧留白宽度恒定、卡片右缘
始终与上方指标卡对齐；需要滚动时才变成可拖的深色滑块。

菜单栏各项：

| 菜单 | 内容 |
| --- | --- |
| 文件(&F) | 刷新记录（Ctrl+R）、打开历史文件、重置历史文件、退出（Ctrl+Q） |
| 主题(&T) | 六个主题的单选 |
| 工具(&U) | 复制表格到剪贴板（TSV，可直接粘进 Excel）、打开数据目录、排序、重置排序 |
| 硬盘(&D) | 重新扫描并记录、数据源状态、复制诊断信息 |
| 帮助(&H) | 如何安装数据源、容量与换算说明、关于 |
| 语言(&L) | 简体中文（English 标注「即将支持」并置灰） |

## 数据口径（重要）

- **跨次匹配用「型号 + 序列号」**，不用盘符。盘符会变，序列号不会；同型号不同序列号的盘
  一定被当成两块不同的盘。盘符只用于**显示**，是另一条独立通路：先取每块物理盘的型号、
  容量与总线类型，再取每个盘符属于哪块物理盘，两者用「型号 + 容量 + 总线类型」配对；
  容量对不上、总线类型对不上、或两块盘同型号同容量无法区分时，宁可不显示也不猜。
- **容量换算**：1 GB = 1024³ 字节。界面同时给 GB 与 TB 双单位，避免口径歧义。
- **累计读取量**取自 smartctl 的 *Device Statistics → Logical Sectors Read*（NVMe 取
  *Data Units Read*）。SATA 盘只在 Device Statistics 缺失时才退回 SMART 属性 241/242
  （Total_LBAs_*），因为这两个属性的计数单位随主控而异，此时状态列会标注「可能偏小」。
- **0E**：NVMe 显示 *Media and Data Integrity Errors* 的原始值。SATA 盘没有这个字段，
  显示「不适用」，并另附 *Reallocated_Sector_Ct* / *Pending_Sector_Ct* 作为辅助健康信息。
- **健康度**：NVMe 用 100 − `percentage_used`；SATA 依次尝试厂商写在属性
  231 / 202 / 177 / 233 / 169 里的剩余寿命百分比（177 的 raw 是磨损循环次数，只采信归一化值）。
  硬盘没报就显示「不适用」。
- **管理员权限**：exe 已用 `--uac-admin` 打包，双击会弹 UAC。未提权时顶部提示
  「未以管理员运行，部分磁盘可能读取失败」——SATA 盘常见的表现就是打不开、抓不到数据。

## history.json 存在哪

优先存在 **exe 同目录**；该目录不可写（例如装进 `Program Files`）时自动回退到
`%LOCALAPPDATA%\DiskUsageTracker\history.json`。

同一目录下还有一个 `settings.json`，只存主题与视图偏好（`{"theme": "system", "view": "table"}`）。
它是最容易重建的配置，删掉就等于回到「跟随系统」，不会影响历史。

目录名是**固定的内部 ID**，与界面显示名无关，所以将来换名字也不会让历史记录「丢失」。
文件损坏或字段缺失时，程序会把坏文件备份成 `history.corrupt-<时间戳>.json` 再重建基线，
不会崩溃。界面底部的注释行会在路径后面直接给出这个文件当前的体积，方便判断它有没有在长。

### 重置历史

想从零开始记录（例如换了工作机、或者想丢掉早期口径不对的读数），用
**文件 → 重置历史文件**。它会先弹一次确认，确认后：

1. 把现有 `history.json` **改名**备份成 `history.reset-<YYYYmmdd-HHMMSS>.json`——是改名不是删除，
   后悔了直接把备份改回 `history.json` 即可；
2. 立刻写一份空的历史文件，并自动重新记录一次，界面回到「已记录基线」的状态。

本来就没有历史文件时，重置不会生成备份，只建空基线。

### 文件格式（schema v2）

从 0.5.5 起历史文件改成「盘档案 + 读数数组」：型号/序列号/协议/设备每块盘只写一次，
每次读数压成 `{"t": 时间戳, "r": 累计读 GB, "w": 累计写 GB, "e": 0E}`。

```json
{
  "schema_version": 2,
  "app": "DiskUsageTracker",
  "disks": [
    {
      "key": "SAMSUNG SSD 980 PRO 1TB|S6B0NL0R912345A",
      "model": "Samsung SSD 980 PRO 1TB",
      "serial": "S6B0NL0R912345A",
      "protocol": "NVMe",
      "device": "/dev/nvme0",
      "readings": [
        {"t": "2026-10-01T09:00:00+08:00", "r": 6000.0, "w": 7500.0, "e": 0}
      ]
    }
  ]
}
```

**最近 24 小时的读数原样保留，更早的按天折叠**（每天只留最后一条）。每块盘的第一条与
最后一条永久保留，因为「历史累计增量」要拿第一次记录做基准、「本次区间增量」要拿上一次
记录做基准。结果是文件体积只随「用过的天数 × 盘数」增长，与一天跑几次无关：
每天 3 次、连着用 1 年，旧格式约 1 MB，新格式约 120 KB。

打开旧版本写的 `history.json` 不必手动处理：程序读到 v1 会照常使用（只读不改写文件），
下一次写盘时自动转成 v2，转换前先留一份 `history.before-trim-<日期>.json`。转换时顶部提示
里会出现一行说明，历史增量不会断。

## 从源码构建

需要 Windows（PyInstaller 不能交叉编译）+ Python 3.11/3.12。

```bat
REM 1) 取 smartctl.exe 放到项目根目录（见下节）
REM 2) 打包
python -m pip install pyinstaller
python -m PyInstaller --onefile --noconsole --uac-admin ^
  --name DiskFlow --icon diskflow-icons\diskflow.ico ^
  --add-binary "smartctl.exe;." ^
  --add-data "diskflow-icons\diskflow.ico;." main.py
```

也可以直接跑仓库里的 `build.bat`。产物在 `dist\DiskFlow.exe`。

图标用仓库里的 `diskflow-icons/diskflow.ico`（16~256 共七种尺寸）：`--icon` 决定 exe 在资源管理器、
任务栏、快捷方式上显示的图标；`--add-data` 把同一个 .ico 打进单文件包，程序运行时的窗口图标
也从包里取（先找 `sys._MEIPASS`，再找 exe 同目录与源码目录）。两个参数都要有：只给 `--icon` 时
exe 有图标但窗口没有，只给 `--add-data` 时窗口有图标但 exe 没有。图标缺失只会退回系统默认图标，
不会让程序起不来。`build.bat` 与 CI 都从 `locate_app_icon()`（由 `main.py` 再导出）取路径，不写死。

### smartctl.exe 从哪来

程序通过 `--add-binary` 把 `smartctl.exe` 打进 exe，运行时从 `sys._MEIPASS` 解析路径；
如果没打进（例如本地手动运行时），会再按「系统 PATH → `C:\Program Files\smartmontools\bin`」
顺序查找。三处都找不到时，界面顶部会提示可执行的修复命令，程序不会崩溃：

```powershell
winget install smartmontools.smartmontools
```

手动准备 `smartctl.exe`（放项目根目录）有两种方式：

```powershell
# 方式 A：官方 NSIS 安装包 + 7-Zip 解包，不需要管理员
$ver = '7.5'
$url = "https://github.com/smartmontools/smartmontools/releases/download/RELEASE_7_5/smartmontools-$ver.win32-setup.exe"
Invoke-WebRequest -Uri $url -OutFile "$env:TEMP\smartctl-setup.exe" -UseBasicParsing
& 'C:\Program Files\7-Zip\7z.exe' x "$env:TEMP\smartctl-setup.exe" "-o$env:TEMP\smartctl" -y
Copy-Item (Get-ChildItem "$env:TEMP\smartctl" -Recurse -Filter smartctl.exe |
           Select-Object -First 1).FullName .\smartctl.exe -Force

# 方式 B：用包管理器装完再拷出来
choco install smartmontools -y
Copy-Item 'C:\Program Files\smartmontools\bin\smartctl.exe' .\smartctl.exe -Force
```

## 测试

```bash
python -m pip install -r requirements.txt
python -m pytest -v
```

测试**不碰真实硬盘**：数据源是可注入的 `FixturesProvider`（读 `fixtures/` 下的 smartctl JSON
样本），历史文件写在 pytest 的临时目录里。覆盖范围包括容量换算、增量计算、0E 判定、
磁盘匹配、历史存储（含损坏文件与目录不可写回退）、首次/第二次运行的端到端流程，
以及 GUI 冒烟测试（Treeview 行列数、0E 新增行红色、读取失败行灰色），
还有主题表一致性、跟随系统解析、`settings.json` 容错、菜单栏结构与切换主题的实际效果，
以及三种视图切换（列结构、控件不重建、卡片视图的告警/置灰标色、视图偏好持久化）
和细滚动条的 `set()` 协议（全可见时保留淡色细轨），以及卡片细节（只读 Entry 可选中复制、
卡片内任意位置都能滚轮滚动、拖动缩放时宽度重排延后合并、KPI 行与卡片行右缘对齐、
双列两列等宽、卡片增量自带 GB 单位、型号按实测字宽锁宽因而协议徽章紧跟其后），
还有历史文件体积的格式化与底部注释行的展示、重置历史（确认/取消两条路径、备份改名而非删除、
重置后自动重新记录），
以及双列中缝与 KPI 第 2/3 张卡分界的像素级对齐、卡片区「放得下就贴顶且不能滚 / 放不下仍可滚」
两条不变量，以及历史文件 v2 的盘档案结构、折叠规则（首条末条永久保留）、连跑 90 次后的
体积上限、v1 静默迁移与升级前备份、v2 结构损坏的备份重建，以及盘符映射层（描述符/几何信息/卷盘号三处二进制解析、型号与容量容差、同型号不同容量区分、并列时拒绝猜测），还有应用图标（.ico 多尺寸、打包优先取包内副本、缺图标只降级不抛异常），
以及 `build.yml` / `build.bat` 必须把同一个图标同时交给 `--icon` 与 `--add-data`，
Release 标题只由英文 EXE 名 + 版本号组成（不得回退成带中文显示名）。

当前共 123 条用例，用 `python -m pytest -v` 一次跑完。

GUI 用例在没有图形环境的机器上会以明确的 `skip` 上报原因，不会静默通过。CI 的两个
Windows job 会真实跑完整套用例，任何一个失败都不会进入打包步骤。

`fixtures/` 里的样本包括：

- 手写的 NVMe（含 `media_errors` 与 Data Units Read/Written）与 SATA SSD（含 Total_LBAs_*、
  无 0E 字段）小样本，用于边界与异常分支
- **三份真机抓取**的样本（两块 NVMe + 一块 SATA SSD），只脱敏了型号/序列号/固件/WWN，
  并删掉了能定位到具体硬件的 PCI ID、OUI、EUI64。它们保留了真机上才会出现的形态：
  SATA 那份 `exit_status=64`（bit6 = 错误日志有记录，**不是**读取失败）、`ata_device_statistics`
  把同一个 General Statistics 页重复列 8 次、属性 241/242 与 Logical Sectors 精确相差 65536 倍
  （即按 32 MiB 块计数）；NVMe 那两份带完整的 `nvme_smart_health_information_log`、
  `nvme_power_states`、`lba_formats`、`endurance_used`。这些坑都被钉成了回归测试。
- 一个脱敏扫描用例：把真实型号/序列号/固件的 SHA-256 存在用例里（而不是明文），
  扫遍所有 fixture 确认一个都不剩；另一个用例用运行时生成的假敏感词反证扫描器真的会命中。
- 一块读取失败的盘（非零退出码 + 错误消息）
- 多种 `history.json` 场景：空历史、一条历史、损坏历史、历史里有但本次消失的盘、本次新出现的盘

### 自己抓一份真实样本

```powershell
# 必须管理员运行，否则 SATA / USB 桥接盘会 Error=5 打不开
# -x 不能省：Device Statistics 那一节靠它
& 'C:\Program Files\smartmontools\bin\smartctl.exe' -x -a --json /dev/sd0 | Out-File -Encoding utf8 dump.json
```

注意 PowerShell 的 `>` 重定向会把输出写成 UTF-16，`Out-File -Encoding utf8` 会带 BOM；
想让 JSON 可以直接被解析，用 `[IO.File]::WriteAllText($path, $out, (New-Object Text.UTF8Encoding($false)))`
写成无 BOM 的 UTF-8。

## 图标

<p>
  <img src="diskflow-icons/png/diskflow-16.png" width="16" alt="16px">
  <img src="diskflow-icons/png/diskflow-24.png" width="24" alt="24px">
  <img src="diskflow-icons/png/diskflow-32.png" width="32" alt="32px">
  <img src="diskflow-icons/png/diskflow-48.png" width="48" alt="48px">
  <img src="diskflow-icons/png/diskflow-64.png" width="64" alt="64px">
  <img src="diskflow-icons/png/diskflow-128.png" width="128" alt="128px">
  <img src="diskflow-icons/png/diskflow-256.png" width="256" alt="256px">
</p>

一枚白色磁盘被两道青色数据流穿过——左箭头流入（写入）、右箭头流出（读取），
表达「累计读写量与增量监测」；主轴孔镂空透出背景，蓝色渐变方底对齐 Windows 11 应用图标规范。

| 文件 | 用途 |
| --- | --- |
| `diskflow.ico` | 多尺寸图标（16/24/32/48/64/128/256），用于 `--icon` |
| `png/diskflow-*.png` | 各尺寸透明 PNG，README 与发布页用 |
| `diskflow-icon.svg` | 主图标矢量源，256 viewBox，可无限缩放 |
| `diskflow-mark.svg` | 透明底标识（白/青），适合深色或彩色背景 |
| `diskflow-mark-light.svg` | 透明底标识（蓝），适合浅色背景 |
| `diskflow-mark-24.svg` | 24×24 界面线性标，描边用 `currentColor` 跟随文字色 |

尺寸档位（取自图标设计稿的交付规范）：

| 尺寸 | 典型场景 |
| --- | --- |
| 16 | 任务栏、列表视图、favicon |
| 24 | 菜单、工具栏、行内图标 |
| 32 | 设置页、对话框标题栏 |
| 48 | 桌面、文件管理器 |
| 64 | 高 DPI 桌面 |
| 128 | 关于对话框、应用商店 |
| 256 | 安装程序、启动画面、超高 DPI |

绘制基准：画布 256×256、圆角 `rx=58`（约 22.7%）；内容居中且四周留白 ≥ 10%（约 26px），
任何尺寸下都不裁切圆角；SVG 不写死 `width`/`height`，由使用方控制尺寸。

颜色语义：

| 元素 | 色值 | 语义 |
| --- | --- | --- |
| 背景 | `#3B82F6` → `#2563EB` → `#1E3A8A` | 科技蓝渐变 |
| 盘片 | `#FFFFFF` | 磁盘本体，最高对比 |
| 数据流 | `#67E8F9` → `#22D3EE` | 青色=数据，箭头表读写方向 |
| 磁道弧 | `#BFDBFE` | 盘片细节层次 |
| 主轴孔 | 镂空 | 透出底色，「透视」质感 |

改图标时注意：按组整组替换（背景 → 数据流 → 磁盘，SVG id 用 `df-` 前缀），
不要单个图层调色；不要非等比拉伸、不要在盘片上叠文字、不要转格式时丢掉 `viewBox`。

## 项目结构

```
.
├─ main.py                     # 兼容入口（shim）：只再导出 diskflow/ 的名字，不含业务逻辑
├─ diskflow/                   # 实现包，按层分模块（见下表）
│  ├─ config.py                #   单一配置点：名称 / 版本 / 换算常量
│  ├─ convert.py               #   容量换算
│  ├─ timefmt.py               #   时间格式化
│  ├─ models.py                #   数据模型（层间通信结构）
│  ├─ parse.py                 #   smartctl JSON → DiskSnapshot（纯函数）
│  ├─ providers.py             #   SmartProvider 注入点（唯一出现 smartctl 调用的地方）
│  ├─ diskmap.py               #   盘符映射（只读，失败即降级）
│  ├─ delta.py                 #   增量计算 + 0E 判定
│  ├─ history.py               #   history.json 读写 / 校验 / 损坏备份
│  ├─ orchestrate.py           #   一次完整运行的编排（GUI 与测试共用）
│  ├─ platformhelp.py          #   管理员权限检测
│  ├─ theme.py / viewmodel.py  #   主题与视图数据表
│  ├─ prefs.py                 #   settings.json
│  ├─ gui.py                   #   tkinter 界面（不含任何 smartctl 调用）
│  └─ entry.py                 #   窗口图标 / create_app / main
├─ test_disk_tracker.py        # pytest 用例
├─ fixtures/                   # 测试样本：smartctl JSON + 各种 history.json 场景
├─ requirements.txt            # 只含测试依赖 pytest（运行时零依赖）
├─ CHANGELOG.md                # 更新日志；打 tag 时 CI 把它贴成 Release 正文
├─ diskflow-icons/             # 应用图标：.ico（exe / 窗口图标）+ 各尺寸 PNG + SVG 源
│                              # （设计稿的预览 html 只在本地，不进仓库）
├─ build.bat                   # 本地 Windows 手动打包脚本（PyInstaller 入口仍是 main.py）
└─ .github/workflows/build.yml # CI：test → build → artifact → Release
```

> `main.py` 保留为兼容入口，是为了让构建脚本、`build.bat` 和测试里的
> `python -c "import main as m; print(m.APP_EXE_NAME)"` 继续有效——名称与版本仍然只有
> `diskflow/config.py` 一个来源。新增模块时记得在 `main.py` 的再导出清单里加一行。

分层设计保证了「smartctl 调用」只存在于 `SmartctlProvider` 里：

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 数据读取层 | `diskflow/providers.py`：`SmartctlProvider` / `FixturesProvider` | 唯一会执行 `smartctl` 的地方，对外只吐 `ProviderResult` |
| 历史存储层 | `diskflow/history.py`：`HistoryStore` | history.json 定位、读写、损坏备份重建 |
| 增量计算层 | `diskflow/delta.py`：`build_rows` / `evaluate_media_errors` | 纯函数：历史 + 本次快照 → 表格行 |
| GUI 层 | `diskflow/gui.py`：`DiskTrackerApp` | 只消费 `RunOutcome`，后台线程跑 `perform_run` |

依赖方向是单向的（`config → convert/timefmt/models → parse → providers → … → gui → entry`），
越靠前的模块越底层，不会反向 import 上层。

GUI 构造函数接受 `provider=` 注入点，测试注入 fixture provider 即可完全脱离真实硬盘。
测试里有一条用例专门检查 GUI 层没有出现 `smartctl` 调用，防止这条边界被悄悄破坏。

## 发布流程

1. 改 `diskflow/config.py` 里的 `APP_VERSION`；
2. 在 `CHANGELOG.md` **顶部**加一节 `## [新版本] - YYYY-MM-DD`（CI 只认第一个匹配项）；
3. 提交并推送 `main`，等 CI 的 test + build 全绿；
4. `git tag v<APP_VERSION>` 并推送——CI 会用 `CHANGELOG.md` 里该小节作为 **Release 正文**，
   并把 `dist/<APP_EXE_NAME>.exe` 挂成 Release 附件。

顺序反过来会被 CI 拦住：先打 tag 再改 `APP_VERSION`，版本校验直接失败；只改版本不写日志，
抽取 Release 正文的步骤会因为找不到小节而失败。

软件名、exe 名、artifact 名都集中在 `diskflow/config.py` 的常量里，workflow 与 `build.bat`
都是**读 `main.py`（再导出同名常量）** 而不是各自写死，所以改名不需要到处搜字符串。

## 许可

MIT
