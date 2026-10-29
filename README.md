# 磁盘流水 DiskFlow

<p align="center">
  <img src="diskflow-icons/png/diskflow-256.png" width="128" alt="DiskFlow 图标">
</p>

Windows 桌面小工具：读取本机每块物理磁盘（SATA SSD / NVMe SSD / HDD）的**累计读写量**与
**0E 介质错误**，把每次读数追加进 `history.json`，并与上一次、第一次记录做增量对比。

- 单窗口 tkinter/ttk，运行时零第三方依赖，关闭即退出——没有后台进程、没有系统服务、不开机自启。
- 产物是 PyInstaller onefile 单文件 exe，`--noconsole` + `--uac-admin`。
- 目标机**不需要装 Python，也不需要装 smartmontools**：解释器、Tk/Tcl、`smartctl.exe` 全部打在包里
  （详见[系统要求](#系统要求)）。唯一门槛是 x64 Windows 10/11。
- 测试与打包都在 GitHub Actions 的 `windows-latest` 上真实执行，使用者不需要在本地装 Python。

## 功能

| 能力 | 说明 |
| --- | --- |
| 自动记录 | 打开即读取一次全部物理盘并写入历史，之后每次打开自动与上次对比 |
| 增量对比 | 距上次的时间间隔、本次区间读写增量、相对首次记录的历史累计增量 |
| 0E 监测 | NVMe 的 *Media and Data Integrity Errors* 增加时整行标红并告警 |
| 健康度 | 只显示硬盘自己报告的剩余寿命，不估算、不拿其它指标冒充 |
| 历史可查 | 菜单「文件 → 打开历史文件(O)」直接定位 `history.json`，格式为可读 JSON |
| 主题跟随系统 | Windows 浅色/深色模式自动套用对应配色，也可在「主题」菜单里固定选一套 |
| 三种视图 | 表格（8 列，成对指标上下两行）/ 卡片 / 双列，右上角一键切换，选择会被记住 |
| 菜单栏 | 刷新、打开历史、复制表格、诊断信息、数据源状态、关于等入口都在菜单里 |

## 系统要求

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / Windows 11，**64 位（x64）** |
| Python | **不需要**。解释器、标准库、Tk/Tcl 全部打在 exe 里 |
| smartmontools | **不需要**。`smartctl.exe` 已随 exe 打包，运行时从 `sys._MEIPASS` 解析 |
| 其它运行库 | 无。VC++ 运行库、UCRT 都自带，不用另装 |
| 权限 | 每次启动都会弹 UAC（读 SMART 需要管理员） |

**关于 CPU 架构。** 发布的 exe 是 x64 单架构（PE `machine = 0x8664`）：

- **32 位 Windows 不支持**：exe 在 32 位系统上会直接报「不是有效的 Win32 应用程序」。
  Windows 11 本身只有 x64，32 位 Windows 10 也已于 2025-10 结束支持，所以没有单独出 32 位版本。
- **Windows 11 ARM 设备（骁龙 X、Surface Pro X 等）可以直接运行**：系统自带 x64 模拟层
  （Win10 ARM 不模拟 x64），无需另装版本。内嵌的 `smartctl.exe` 也是 x64，同样走模拟，
  但读盘是一次几十毫秒的 `DeviceIoControl` 调用，模拟开销可以忽略。
- 不单独出 ARM64 原生版：smartmontools 官方 Windows 包只提供 x64/x86，**没有 ARM64 构建**，
  所以即便打了 ARM64 的 exe，最重的读盘环节仍要靠那个 x64 的 `smartctl.exe` 去模拟，
  收益只有解释器和 Tk 这部分，不值得多一套 CI 与产物。

**首次运行的两个提示。** exe 没有代码签名：SmartScreen 会拦一次「已保护你的电脑」，
点「更多信息 → 仍要运行」即可；部分杀软会把 PyInstaller 单文件包误报为可疑，
需要加白名单。另外 onefile 首次启动要把约 12 MB 解压到 `%TEMP%\_MEIxxxxxx`，
冷启动会慢 1~3 秒，退出时自动清理，不留后台进程。

## 使用

1. 从 [Releases](../../releases) 下载，两种形式任选（或从 Actions 的 Artifacts 里取）：
   - `DiskFlow.exe`：便携版，双击直接用，不进注册表；
   - `DiskFlow-<版本>-windows-setup.exe`：安装版，装到 `Program Files`、带开始菜单项与卸载器，
     安装向导默认简体中文。历史数据两者都写在同一个目录 ID 下（见
     [history.json 存在哪](#historyjson-存在哪)）。
2. 双击运行，弹出 UAC 时选择「是」——读 SMART 需要管理员权限。
3. **首次打开**只建立基线：顶部出现绿色的「✓ 已记录基线（N 块盘）」，底部提示「已记录基线，下次打开将显示增量」。
4. **之后每次打开**自动读取 + 记录 + 对比，表格给出「距上次」「本次读增/写增」「历史读增/写增」。
5. 拷完大文件想立刻看效果，点底部**刷新记录**即可再记录一次。

关闭窗口程序即退出，不残留任何进程。

## 三种视图

窗口标题栏与左上角大标题只写英文名和版本号（`DiskFlow  v0.8.2`），不带中文——标题栏里中英
混排显长，而 exe 文件名、Release 标题、artifact 名用的都是 `DiskFlow`，对外只需一个名字。
中文显示名「磁盘流水」保留在「关于」框正文与本 README 里。

`工具` 菜单里有两条系统工具快捷入口：**打开磁盘管理(M)**、**打开设备管理器(V)**，
排在「打开数据目录」下面。点一下直接用系统自带的 `%SystemRoot%\System32\diskmgmt.msc` /
`devmgmt.msc` 把它叫起来，省得自己翻「计算机管理」。这两项只负责启动系统工具：不读它们的
输出、不改表格、不写历史，也不会多留任何进程（关掉那两个窗口就干净了）。打不开时（组策略禁用、
系统文件缺失）顶部红字给出系统原话，不会静默失败；绿色只在这步真的没抛异常时才亮，写的是
「已打开 磁盘管理。」。

右上角有「表格 / 卡片 / 双列」分段切换，选中的视图存进 `settings.json`，下次打开沿用。

**表格**（默认）8 列，成对的指标压成一格、上下两行显示，因此 13 项数据一屏放得下、
不用横向拖动：

| 列 | 上行 / 下行 |
| --- | --- |
| 磁盘 / 序列号 / 容量 | 型号 / 序列号 + 实际容量（默认厂商标称值，如 `512 GB`） |
| 协议 | NVMe、SATA |
| 累计读 / 写 | 累计读取量 / 累计写入量（超过 1 TB 显示 TB，否则 GB） |
| 距上次 | 距上次记录的时间间隔 |
| 本次读增 / 写增 | 本次区间读取增量 / 写入增量（自带单位，超过 1 TB 换 TB） |
| 历史读增 / 写增 | 相对首次记录的历史累计读增 / 写增（同上） |
| 0E / 健康 | 0E 原始值或「不适用」 / 硬盘自报剩余寿命 |
| 状态 | 已更新记录、读取失败原因、新盘首次记录等 |

**卡片**把每块盘的所有指标摊开成一张卡，**双列**是同样的卡片两列排布（两列等宽、各让一半
列间距），适合盘少时逐项看。双列里每张卡只有一半宽度，指标按「读 / 写 / 上下文」分三列竖着排：
第 1 列是「累计读取 → 本次读增 → 历史读增」，第 2 列是「累计写入 → 本次写增 → 历史写增」，
第 3 列放「距上次 / 0E / 健康度」。这样本次读增正上方累计、正下方历史读增，同类指标一列看下来，
不用在格子之间横着找；卡片视图仍是宽排（每行 5 格）。卡片里的四个增量格自带 `GB` 单位，不用回头找列头；型号后面
紧跟 NVMe / SATA 徽章，一眼就能看出协议；协议徽章后面还有盘符徽章（`C:`、`C:, D:`），
标明这块盘在本机对应哪个分区（拿不到盘符时这一格不显示）。卡片文字是只读输入框，
可以框选、双击选词、`Ctrl+C` 复制序列号；表格虽然不能框选文字，但支持多选后
`Ctrl+C` 连表头一起复制（见下）。

表格里的增量**自带单位**（`+348.80 GB` / `+31.96 TB`）：成对的一列只有 128px 宽、还上下叠两个
值，超过 1 TB 一律换 TB，否则一格里塞不下 `+32,728.43 GB`。顶部状态栏的时间按
`2026-04-30 08:00:00 (UTC+8)` 显示（ISO 原文里的 `T` 与 `+08:00` 不适合直接读），
时区跟随系统；写进 `history.json` 的仍然是原始 ISO 8601 串，不受显示格式影响。

首列序列号后面跟着这块盘的实际容量，写成 `512 GB`：这是厂商标称值
（1 GB = 1,000,000,000 字节，所以 1TB 的盘写 `1 TB`、4TB 的盘写 `4 TB`）。
括号里的系统口径（`512 GB (476.94 GiB)`，1 GiB = 1024^3 字节，同一块盘在「此电脑」里
看到的就是这个数）**默认不显示**——两个数说的是同一个字节数、只是进制不同，对普通用户
常常是噪音。勾选「工具 → 显示系统口径容量」才显示括号部分，开关状态存在 `settings.json`
的 `show_gib` 字段里，下次打开沿用；切换只重画界面，不重新读盘，也不改任何存盘内容。
容量直接来自 smartctl 的 `user_capacity.bytes`，读不到时（老记录、读取失败的盘）只显示
序列号，不会补一个占位的破折号。`history.json` 里不存容量，每次打开现读，所以历史文件
格式不变。

顶部状态栏左侧是这次运行的结果确认行：**绿色**的「✓ 数据已更新并保存（N 块盘）」表示这次读到的盘
已经写进历史文件（首次运行则显示「✓ 已记录基线（N 块盘）」）；如果有盘没读出来，紧跟一段**灰色**的
「M 块读取失败」。绿色只表达「成功」，失败用灰色，与表格里灰色的失败行同色。一块盘都没读成功时
绿色行不出现，避免「绿的已更新」和「读不出来」同时挂在顶部。

顶部一共分三层，各管各的，互不混淆：

| 位置 | 颜色 | 内容 | 会不会累积 |
| --- | --- | --- | --- |
| 状态栏第一行左 | 绿色 | 本次运行结果：✓ 数据已更新并保存（N 块盘） | 每次覆盖 |
| 状态栏第一行中 | 灰色 | M 块读取失败 | 每次覆盖 |
| 状态栏第一行右侧 | 黄色 | 系统状态：未提权、找不到 smartctl、历史文件损坏/已重置 | **会**累积（同样的话只留一条） |
| 状态栏第一行末尾 | 红色 | 操作失败：复制失败、打开目录失败、请先用右键…… | 只留最新一条 |
| 状态栏第二行 | 绿色 | 操作成功：已复制 N 行、已选中 N 行、已复制：+90.97 GB / +0.03 GB | 只留最新一条，没有内容时整行收起 |

红字与绿字共用同一个槽位：成功的操作会顶掉上一条失败提示，反之亦然，因此屏幕上不会同时出现
两种结论。点「刷新记录」时红、绿两条一起清掉（它们描述的是刷新前那份数据），只有黄色系统提示
原样保留。

表格与卡片共用同一份排序结果，可在「工具 → 排序」里按任意指标排序。

表格里的文字也能取出来：按住 `Ctrl` 点几行，或按住 `Shift` 拖一段选中多行，再按 `Ctrl+C`
（或右键 →「复制选中行」）就把这些行连表头一起复制成 TSV，粘进 Excel 直接是表格；粘进记事本
也是齐整的一行一块盘。没选中任何行时 `Ctrl+C` 等于复制整表。右键落在某个单元格上还能
「复制此单元格」——成对的那一列会拼成「300.00 GB / 400.00 GB」一行，适合只想要序列号或某个
数字时用；复制后的提示只写「已复制：<数值>」，不再重复一遍列名（列名在表头上已经有了）。右键菜单
另有「全选」与「复制整表」。

表格支持点击列标题排序。行的颜色含义：

- **红色**：这块盘的 0E（介质错误）比上次多了，说明介质层面出现新的不可纠正错误。
- **灰色**：这块盘本次读取失败（例如被其它程序独占、或权限不足），状态列会写明原因。
- 正常行使用系统默认前景色。

## 主题与菜单

默认**跟随系统**：Windows 浅色模式用「数据台账」（深色表头 + 斑马纹 + 顶部四张指标卡），
深色模式用「深色专业」（深底浅字 + 暗红告警行）。也可以在「主题」菜单里固定选
数据台账 / 深色专业 / 清爽仪表 / 极简留白 / 原生默认，选「跟随系统」恢复自动判断。

选择会存进 `settings.json`——与 `history.json` 同目录（便携版在 exe 同目录，安装版在
`%LOCALAPPDATA%\DiskUsageTracker`）。主题偏好是独立文件，**不写进 `history.json`**，
换皮肤不会影响历史数据。标题栏与原生菜单栏都由系统绘制、始终跟随系统配色，
不随主题变深——深色主题下窗口边框保持系统样式，避免出现「深色标题栏 + 浅色菜单栏」的割裂。

表格与卡片的滚动条是自绘的细圆角条（`ttk.Scrollbar` 画不出圆角），颜色跟着主题走。
内容一屏放得下时它不会消失，而是收成一条很淡的细轨，保证右侧留白宽度恒定、卡片右缘
始终与上方指标卡对齐；需要滚动时才变成可拖的深色滑块。

窗口底部右侧是**刷新记录**（主按钮，强调色实心）与**关于**（次级按钮，1px 描边空心）。
两个按钮的颜色都取自当前主题（强调色来自分段切换的选中色，描边色来自主题的分隔线色），
换主题时跟着变，不再像以前那样在所有主题下都是系统默认的浅灰方块、且被 ttk 拉成同宽。
点「刷新记录」后按钮变灰、文字变「读取中…」，读完恢复——同一时刻只有一个刷新的按钮可用，
避免连点触发多次 smartctl。唯一的例外是「原生默认」主题：那一套把按钮交给系统绘制，
自定义颜色会被系统忽略（实测会只剩一个空框），所以该主题下两个按钮保持系统原生外观。

菜单栏各项：

| 菜单 | 内容 |
| --- | --- |
| 文件(F) | 刷新记录(R)（Ctrl+R）、打开历史文件(O)、重置历史文件(E)、退出(X)（Ctrl+Q） |
| 主题(T) | 六个主题的单选 |
| 工具(U) | 复制选中行(C)（Ctrl+C）、复制表格到剪贴板(T)（TSV，可直接粘进 Excel）、打开数据目录(D)、排序(O)、重置排序(S)、**显示序列号**、**显示系统口径容量** |
| 硬盘(D) | 重新扫描并记录(R)、数据源状态(S)、复制诊断信息(I) |
| 帮助(H) | 如何安装数据源(S)、容量与换算说明(C)、关于(A) |
| 语言(L) | 简体中文 / English 单选，切换**立即生效**（详见下节） |

菜单文字保留 `(` `)` 和里面的字母，但**括号里不写 `&` 助记符**——tkinter 会把 `&` 后面那个
字母画上下划线来标 Alt 键，中文界面里那道横线看着像多画了一笔，所以只去 `&`、括号和字母都留着
（快捷键 Ctrl+R / Ctrl+C / Ctrl+Q 与鼠标操作完全不受影响）。

**序列号默认隐藏**：表格首列与卡片副标题里序列号遮成「前两位 + 8 个星号」，
如 `2013CV440309` 显示为 `20********`，复制单元格、复制整表（TSV）、诊断信息里同样不含
真实值。星号数量固定 8 个、不按原长度 1:1 遮——序列号长度本身也是信息，能反推型号批次。
留开头两位是为了同型号多块盘时还能一眼区分是哪一块；读不到序列号时仍显示「—」，不伪造
一个像序列号的遮盖串。勾选「工具 → 显示序列号」才显示真实值，勾选状态存在 `settings.json`
的 `show_serial` 字段里，下次打开沿用。容量后面括号里的系统口径同理，由「显示系统口径容量」
控制、默认关闭，开关只影响显示。序列号只在本地读写，不会上传；隐藏只是避免界面和
截图里顺手泄露设备身份，`history.json` 里仍按「型号 + 序列号」匹配，所以隐藏不影响增量的
正确性。

## 界面语言

「语言(L)」菜单里有 `简体中文` 与 `English` 两项，选中后界面**当场切换**：菜单栏、
表头、状态列的文案、0E 提示、顶部状态栏、底部汇总、「关于」框与各说明对话框一起变，
不用重启，也不会重新读一次盘（切换只重画，不触发 smartctl）。选择记在 `settings.json`
的 `lang` 字段里，下次打开沿用；默认是简体中文，中文用户的界面与以前逐字一致。

英文界面下有两处刻意的取舍：

- **时间间隔用紧凑写法** `2 d 2 h 30 min`，因为表格那一列只有 152px 宽，
  `2 days 2 hours 30 minutes` 会被截断；
- **状态列的部分文案偏短**（`Recorded`、`Read failed`、`N/A`），同列宽度有限。
  状态列有 24 个字符的截断上限，过长会以 `…` 结尾。

文案集中放在 `diskflow/i18n.py` 的一张翻译表里，**以中文原文为键**（gettext 风格），
所以源码里照旧直接写中文，改文案不会和 key 对不上。漏翻的条目在英文界面下原样退回中文，
不会出现空串或裸 key——宁可露一句中文，也不让界面变成 `{...}`。历史文件里的数据
（型号、序列号、时间戳、数值）不参与翻译，`history.json` 的内容与格式一个字节都不变。

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

分两种情况，按 **exe 所在位置**自动判断，不需要任何开关：

| 形式 | 位置 | 原因 |
|---|---|---|
| 便携版（exe 放在任意可写目录） | exe 同目录的 `history.json` | 拷走整个文件夹就是全套 |
| 安装版（exe 在 `Program Files` 下） | `%LOCALAPPDATA%\DiskUsageTracker\history.json` | 卸载不连数据一起删，多用户各存各的 |

安装版之所以不能写安装目录：exe 带 `requireAdministrator`，提权后「能不能写」的探测会
假性成功，历史就落到 `Program Files` 里了——卸载时被一起删除，多个 Windows 账号还会
共享同一份。所以只要探测到 exe 位于 `Program Files` / `Program Files (x86)` 下，程序
直接跳过主目录，强制走 `%LOCALAPPDATA%`。
同一目录下还有一个 `settings.json`，只存界面偏好（`{"theme": "system", "view": "table", "show_serial": false, "lang": "zh-CN"}`）。
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

需要 **x64 Windows**（PyInstaller 不能交叉编译）+ Python 3.11/3.12。产物架构跟着构建机走：
在 x64 机器上打出的是 x64 exe，在 ARM64 机器上打出的才是 ARM64 exe，两者不能互相生成。
仓库的 CI 固定用 `windows-latest`（x64），所以发布出来的永远是 x64。

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

### 安装包（可选产物）

除了便携 exe，仓库还提供 Inno Setup 安装包（CI 每次都会一并产出，本地可选）。
安装包做的事：装到 `Program Files`、建开始菜单项、注册卸载器、可选桌面快捷方式，
安装向导**默认简体中文**（也可切英文）。

脚本在 `installer/`：

| 文件 | 作用 |
|---|---|
| `DiskFlow.iss` | 安装脚本。**不含任何写死的名字/版本**，全部引用编译期宏 |
| `make_build_info.py` | 从 `diskflow/config.py` 生成 `build-info.iss`（宏文件）；`--print-setup-name` 输出安装包文件名 |
| `ChineseSimplified.isl` | 简体中文语言文件，构建时从 jrsoftware/issrc 下载（已 gitignore） |

本地编译：

```powershell
winget install JRSoftware.InnoSetup     # 只需装一次（CI 镜像里已自带）
python installer\make_build_info.py      # 生成 installer\build-info.iss
& "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe" installer\DiskFlow.iss
# 产物：dist\DiskFlow-<版本>-windows-setup.exe
```

用 winget 装 Inno Setup 时它可能落在用户目录而不是 `Program Files (x86)`，此时 ISCC 在
`%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`，把上面那行换成这个路径即可。

两个必须知道的取舍：

- **装好之后每次启动照样弹 UAC**。这是 exe 自己的 manifest（`--uac-admin`）决定的，
  与装法无关；安装包只能省掉「找目录、建快捷方式」这些手工步骤。
- **卸载不会删除历史数据**。安装版的 `history.json` / `settings.json` 强制在
  `%LOCALAPPDATA%\DiskUsageTracker`，与安装目录无关，升级、卸载、换装便携版都读得到。

安装脚本里 `AppId` 是一个**固定 GUID**，改显示名 / exe 名时不要动它——它是安装包
判断「同一个软件的旧版」的唯一依据，改了两代安装包会并存成两份。

快捷方式（开始菜单、桌面）与「安装完成后启动」的文案一律用英文 exe 名 `DiskFlow`，
中文显示名只出现在开始菜单分组名和卸载列表里；安装完成页那一项走 `shellexec`，
否则系统会直接用 `CreateProcess` 拉起要求管理员权限的 exe，报**错误代码 740**。

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
以及序列号默认隐藏（遮盖规则「前两位 + 固定 8 星号」逐值断言、长短序列号遮盖串等长、
不足两位整遮、空值仍给破折号，表格首列与卡片副标题与 TSV 与单格复制四条出口一起查，
开关状态持久化到 `settings.json` 且脏值只认真布尔）、
以及双列指标按「读 / 写 / 上下文」分列且本次与历史增量上下对齐、双列中缝与 KPI 第 2/3 张卡分界的像素级对齐、卡片区「放得下就贴顶且不能滚 / 放不下仍可滚」
两条不变量，以及历史文件 v2 的盘档案结构、折叠规则（首条末条永久保留）、连跑 90 次后的
体积上限、v1 静默迁移与升级前备份、v2 结构损坏的备份重建，以及盘符映射层（描述符/几何信息/卷盘号三处二进制解析、型号与容量容差、同型号不同容量区分、并列时拒绝猜测），还有应用图标（.ico 多尺寸、打包优先取包内副本、缺图标只降级不抛异常），
以及 `build.yml` / `build.bat` 必须把同一个图标同时交给 `--icon` 与 `--add-data`，
Release 标题只由英文 EXE 名 + 版本号组成（不得回退成带中文显示名），
还有界面语言（语言代码白名单与脏值回落、`tr()` 未收录条目退回中文且不返回空串、
`tr_message()` 只换「读取失败：」前缀而后半段保留、英文时间紧凑写法、
数据层的 0E 与状态文案跟着语言变、切语言后菜单/表头/KPI/按钮/状态栏/汇总行全部立即换语言、
切回中文完全复原、`settings.json` 的 `lang` 持久化与脏值回落）。

还有安装包相关（`Program Files` 下强制改存 `%LOCALAPPDATA%`、`settings.json` 跟随同一条规则、
`Program Files2` 这类前缀相近目录不得误判、安装脚本的固定 GUID 与全宏引用、
安装脚本里不得写死版本号、缺宏必须编译失败、卸载器不得碰用户数据目录、
快捷方式与启动项一律用英文 exe 名且必须带 `shellexec`（否则报错误代码 740）、
`make_build_info.py` 的宏全部来自 `diskflow/config.py` 且改名后跟着变、
CI 必须真的编译安装包并把它同时放进 artifact 与 Release）。

当前共 176 条用例，用 `python -m pytest -v` 一次跑完。

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
├─ installer/                  # Inno Setup 安装包：DiskFlow.iss（全宏）+ make_build_info.py
│                              # （.isl 与 build-info.iss 是构建期产物，已 gitignore）
└─ .github/workflows/build.yml # CI：test → build → artifact（exe + 安装包）→ Release
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
2. 在 `CHANGELOG.md` **顶部**加一节 `## [新版本] - YYYY-MM-DD`（CI 只认第一个匹配项），
   小节按 **新增 → 修复 → 改进**（文档 / 测试放最后）顺序写——这是 Release 正文，
   读者先想知道「多了什么」，再想知道「修了什么」，最后才是内部怎么变好的；
3. 提交并推送 `main`，等 CI 的 test + build 全绿；
4. `git tag v<APP_VERSION>` 并推送——CI 会用 `CHANGELOG.md` 里该小节作为 **Release 正文**，
   并把 `dist/<APP_EXE_NAME>.exe` 与安装包 `dist/<APP_EXE_NAME>-<版本>-windows-setup.exe`
   一起挂成 Release 附件。

顺序反过来会被 CI 拦住：先打 tag 再改 `APP_VERSION`，版本校验直接失败；只改版本不写日志，
抽取 Release 正文的步骤会因为找不到小节而失败。

软件名、exe 名、artifact 名都集中在 `diskflow/config.py` 的常量里，workflow 与 `build.bat`
都是**读 `main.py`（再导出同名常量）** 而不是各自写死，所以改名不需要到处搜字符串。

## 许可

MIT
