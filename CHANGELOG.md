# 更新日志

本文件是 Release 正文的唯一来源：打 `v*` tag 时，CI 会自动抽取对应版本的小节，
原样贴到 GitHub Release 上。所以要写「用户看得懂」的话，不是写给自己看的提交记录。

格式约定：
- 每个版本一个二级标题，形如 `## [0.2.0] - 2026-10-22`，方括号里必须是 main.py 的 `APP_VERSION`。
- 未发布的改动写在 `## [未发布]` 下（发版时把它改名成具体版本号）。
- 小节从该标题开始，到下一个二级标题（`## `）之前为止。

## [0.4.0] - 2026-10-23

### 新增：主题跟随系统，浅色/深色自动切换

窗口现在默认「跟随系统」——Windows 浅色模式用**数据台账**配色（深色表头 + 斑马纹 + 四张指标卡），
深色模式用**深色专业**配色（深底浅字 + 暗红告警行）。主题菜单里还留着
数据台账 / 深色专业 / 清爽仪表 / 极简留白 / 原生默认 五个固定选项，随时可覆盖自动判断。

选择会存进 exe 同目录的 `settings.json`，下次打开沿用；选回「跟随系统」就重新跟随。
主题偏好**单独存一个文件**，不碰 `history.json` 的结构，历史记录不会因为换皮肤而失效。

### 新增：菜单栏

窗口顶部加了一行标准菜单，把原来只能靠按钮或翻代码才知道的功能都摆出来：

- **文件**：刷新记录（Ctrl+R）/ 打开历史文件 / 退出（Ctrl+Q）
- **主题**：六个主题的单选
- **工具**：复制表格到剪贴板（TSV，可直接粘进 Excel）/ 打开数据目录 / 重置排序
- **硬盘**：重新扫描并记录 / 数据源状态 / 复制诊断信息（提 issue 时贴这段）
- **帮助**：如何安装数据源 / 容量与换算说明 / 关于
- **语言**：简体中文（English 标注「即将支持」并置灰，不做假入口）

深色主题下标题栏也会跟着变深（走 DWM 的 `DWMWA_USE_IMMERSIVE_DARK_MODE`），
但原生菜单栏由系统绘制，仍然是浅色——这是 Win32 的限制，不是漏做。

### 改进：13 列不再横向滚动

之前表格天然比窗口宽，最右边的「0E / 状态」必须拖动横向滚动条才看得到，
而这恰好是最该一眼看到的两列。这版按 Microsoft YaHei UI 9pt 实测的文字像素宽度
重排了列宽，并让窗口默认宽度跟着表格的自然宽度走（屏幕不够宽时回落 1280）。
现在默认窗口下 13 列一屏放得下，横向滚动条只在手动缩小窗口后才出现。

### 改进：状态列过长的文字会截断

`读取失败：Smartctl open device: /dev/sda failed: ...` 这类长错误以前会把状态列撑爆、
挤掉右边的信息。现在超出 24 个字就截断成 `读取失败：Smartctl open devi…`，
完整文本仍保留在行数据与「复制诊断信息」里。

### 修复：设备错误消息会显示成 Python 字典

smartctl 7.x 的 `messages` 字段是**对象数组**（`{"string": ..., "severity": ...}`），
旧代码用字符串拼接，界面上会直接出现 `{'string': 'Smartctl open device ...'}` 这种东西。
现在两种形态都能正确取出文字。

### 修复：把精度噪声误判成「计数重置」

历史里存的是四舍五入到 4 位小数的 GB 值，本次是原始精度，两者相减会出现
`-0.0001` 这样的噪声，被判成「累计读回退」并显示 `-0.00`。
现在增量绝对值小于 0.01 GB 直接归零，真正的回退才提示
「累计读数回退，疑似计数重置」。

### 修复：顶部提示重复堆叠

同一个提示（未提权、未找到 smartctl.exe）在每次刷新后都会再追加一遍，开几次就出现几行。
现在按内容去重，一样的话只说一次。

### 测试

用例从 66 增至 **74**，新增 8 条：主题表结构一致性、跟随系统在浅色/深色下的解析、
非法主题键回落、`settings.json` 的读写与损坏容错、菜单栏 6 项与主题单选项、
切换主题后颜色与指标卡可见性真的变了、主题选择能持久化并恢复、菜单动作不崩溃。

同时修掉一个会让 CI 随机挂的问题：GUI 用例以前每个用例都新建并销毁 `tk.Tk()`，
偶发 `TclError: invalid command name "tcl_findLibrary"` 让用例被跳过。现在整个会话
只建一个隐藏的根窗口，每个用例开一个 `Toplevel`，连跑 10 轮 74 条全绿、无跳过。

## [0.3.0] - 2026-10-23

### 测试：用真机抓取的 SATA 样本锁住解析行为

新增 `fixtures/sata_real_960gb.json`——一份从真实 SATA 固态硬盘上 `smartctl -x -a --json -d sat`
抓下来的原始输出，只把型号、序列号、固件版本、WWN 换成假值，其余结构一字未改。

之前 SATA 分支只有手写的样本，形状和真机差得远；这份样本把三个真机上才会出现的坑钉成了回归测试：

- `smartctl.exit_status` 真机上经常是 **64**（bit6 = 错误日志里有历史记录），
  这不代表读取失败。只有 bit0/bit1 才表示设备打不开，旧逻辑放过 64 是对的，现在有测试守着。
- `ata_device_statistics.pages` 在真机上会把同一个 "General Statistics" 页**重复列 8 次**，
  每个统计项还都带 `offset`/`size`/`flags` 等额外字段。解析器按名字取首个匹配，结果正确。
- 属性 241/242 与 Device Statistics 的比值在这块盘上**精确等于 65536**，
  即按 32 MiB 块计数。这条实测数据让「必须优先 Device Statistics」有了铁证：
  若按 512 字节解释，71 TB 的累计读取会被显示成 1.12 GB。

这块真实盘解析结果：累计读 71.42 TB、累计写 16.07 TB、健康度 100%、0E 不适用、
重映射扇区 0、待映射扇区 0——全部符合预期。

### 新增：窗口标题与界面左上角显示版本号

标题栏和界面左上角的标题行现在都写作 `磁盘流水 DiskFlow  v0.3.0`，两处文字都来自
`main.py` 的 `window_title()`，版本号只引用 `APP_VERSION` 这一处，不在界面里重复写死。
「关于」对话框的标题也走同一个函数，用户截图反馈问题时一眼能看出跑的哪个版本。

### 新增：两份真机抓取的 NVMe 样本

`fixtures/nvme_real_256gb.json` 和 `fixtures/nvme_real_512gb.json` 是从真机上用
`smartctl -x -a --json -d nvme` 抓下来的原始输出，只把型号、序列号、固件版本换成假值，
并删掉了能定位到具体硬件/厂商的 `nvme_pci_vendor`、`nvme_ieee_oui_identifier`、
`nvme_controller_id` 与命名空间 `eui64`。保留的是解析器真正依赖的形态：
`nvme_smart_health_information_log` 的完整字段、`nvme_power_states`、`lba_formats`、
`endurance_used` 等。

### 文档：README 改为面向使用者的发布说明

去掉了开发过程中的改名记录、内部待办和作者口吻，改成「这个工具做什么 / 怎么用 / 数据口径
是什么 / 怎么自己构建」的结构，并补上从 Actions 与 Releases 两个渠道取 exe 的路径。

### 测试

pytest 用例从 56 增至 66，新增：
`test_parse_real_captured_sata_fixture`（真样本端到端解析）、
`test_real_capture_duplicate_general_statistics_pages_are_harmless`（重复页）、
`test_real_capture_shows_attr_241_242_use_32mib_blocks`（65536 倍关系）、
`test_parse_real_captured_nvme_fixture` / `test_parse_real_captured_nvme_fixture_second_controller`
（两份真机 NVMe 样本的累计量与健康度）、
`test_real_capture_fixtures_are_anonymized`（扫描所有 fixture，确认真实型号/序列号/固件/
EUI 一个都不剩；敏感词在测试源码里以 SHA-256 保存，避免测试文件自己成为泄漏源）、
`test_anonymization_scanner_actually_detects_leaks`（用运行时生成的假敏感词反证扫描器
确实能命中，防止"扫描器失效但测试一直绿"）、
`test_window_title_carries_display_name_and_version` / `test_window_title_follows_app_version` /
`test_gui_title_bar_and_header_show_version`（标题与左上角标题行同源且带版本号）。

## [0.2.0] - 2026-10-22

### 修复：SATA 固态硬盘的累计读写量显示严重偏小

0.1.0 在 SATA 盘上把「累计读取 / 累计写入」算小了成千上万倍（例如实际 32567 GB 只显示 1.11 GB）。
原因是 SMART 属性 241/242（`Total_LBAs_Read` / `Total_LBAs_Written`）虽然名字叫 LBA，
但**计数单位由硬盘主控决定**：多数盘是 512 字节，而 Silicon Motion、Toshiba HK4R 等主控按 32 MiB
一个计数。旧代码对所有盘都按 512 字节乘，遇到非 512 字节的盘就会低估 65536 倍。

现在改为优先读取 `smartctl -x` 输出里的 `ata_device_statistics` → `Logical Sectors Read / Written`，
这个字段始终是 512 字节扇区，与 CrystalDiskInfo 等工具同一口径。
只有当硬盘不提供 Device Statistics 时，才退回 241/242，并在该行的「状态」列标注
「累计量取自 SMART 241/242，可能偏小」——不再默默给出可疑数字。

**NVMe 盘不受此问题影响**（`Data Units Read/Written` 单位本来就是固定的 512000 字节）。

### 新增：健康度列

在「0E」列前面新增「健康度」列，显示硬盘自己报告的剩余寿命百分比：

| 协议 | 取值来源 |
| --- | --- |
| NVMe | `100 - percentage_used` |
| SATA | 依次尝试 SMART 231 / 202 / 177 / 233 / 169；231、202、233、169 的原始值在 0–100 内优先取原始值（属性 177 的原始值是磨损循环次数，只能取归一化值） |
| 硬盘不提供该字段 | 显示「不适用」 |
| 读取失败 | 显示「—」 |

健康度**只取硬盘自报值，不做任何推算**——宁可显示「不适用」，也不给一个编出来的数字。

### 改进：累计容量超过 1 TB 时同时显示 TB 与 GB

单看「32567.42 GB」不好快速判断量级。现在 ≥1024 GB 会显示成
`31.81 TB (32,567.42 GB)`，低于 1 TB 仍显示 `512.50 GB`。换算始终是 1024 进制（1 GB = 1024³ 字节）。

### 改进：窗口默认宽度 1100 → 1280

多了「健康度」一列，加宽以容纳新的列宽，仍然可以自由缩放。

### 测试

pytest 用例从 46 增至 56，新增覆盖：GB/TB 双单位切换与极大值、健康度的四种取值路径、
SATA 优先 Device Statistics 与退回 241/242 的两条分支、NVMe 只上报读取量时的来源标记，
另加两个守护用例，强制 `CHANGELOG.md` 必须有与 `APP_VERSION` 同名的小节。

## [0.1.0] - 2026-10-21

首个可用版本。

- 通过 `smartctl --json` 读取本机全部物理磁盘（NVMe / SATA SSD / HDD）的型号、序列号、
  累计读写量、0E 介质错误、协议类型与设备路径。
- 以「型号 + 序列号」为跨次匹配键（不用盘符），每次打开自动记录并对比上一次：
  距上次时间间隔、本次区间读写增量、历史累计读写增量。
- 0E 新增时整行标红并提示「介质错误在累积，建议关注备份」。
- `history.json` 优先存 exe 同目录，不可写时回退到 `%LOCALAPPDATA%\DiskUsageTracker\history.json`；
  文件损坏时自动备份并重建基线，不崩溃。
- smartctl 调用在后台线程执行，界面不假死；smartctl 随 exe 一同打包，无需另装 smartmontools。
