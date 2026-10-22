# 磁盘流水 DiskFlow

> **命名状态：已确定。** 正式名称 **DiskFlow / 磁盘流水**。窗口标题、exe 名、artifact 名全部取自
> `main.py` 顶部的集中配置常量，改名只需改那一处（见第 6 节）。历史数据目录使用固定的内部 ID
> `DiskUsageTracker`，**不随显示名变化**。

Windows 桌面小工具：读取本机每块物理磁盘（SATA SSD / NVMe SSD / HDD）的**累计读写量**与
**0E 介质错误**，把每次读数追加进 `history.json`，并与上一次 / 第一次记录做增量对比。

- 单窗口 tkinter/ttk，无第三方运行时依赖，关闭即退出，无后台进程、无服务、无开机自启。
- 产物：PyInstaller onefile 单文件 exe，`--noconsole` + `--uac-admin`。
- 测试与打包都在 GitHub Actions 的 `windows-latest` 上真实执行，本地无需装 Python。

---

## 1. 目录结构

```
.
├─ main.py                     # 全部程序逻辑（数据读取层 / 历史存储层 / 增量计算层 / GUI 层）
├─ test_disk_tracker.py        # pytest 用例（59 个，不依赖真实硬盘）
├─ fixtures/                   # 测试样本：smartctl JSON + 各种 history.json 场景
├─ requirements.txt            # 只含测试依赖 pytest（运行时零依赖）
├─ CHANGELOG.md                # 更新日志；打 tag 时 CI 把对应小节贴成 Release 正文
├─ build.bat                   # 可选：本地 Windows 手动打包脚本
└─ .github/workflows/build.yml # CI：test -> build -> artifact -> Release
```

## 2. 分层设计（为什么 smartctl 不在 GUI 里）

| 层 | 位置 | 职责 |
| --- | --- | --- |
| 数据读取层 | `SmartctlProvider` / `FixturesProvider` | 唯一会执行 `smartctl` 的地方；对外只吐 `ProviderResult` |
| 历史存储层 | `HistoryStore` | history.json 定位、读写、损坏备份重建 |
| 增量计算层 | `build_rows` / `evaluate_media_errors` | 纯函数：历史 + 本次快照 -> 表格行 |
| GUI 层 | `DiskTrackerApp` | 只消费 `RunOutcome`，后台线程跑 `perform_run` |

GUI 构造函数接受 `provider=` 注入点，测试注入 `FixturesProvider` 或假 provider 即可脱离真实
硬盘；`test_smartctl_calls_are_confined_to_provider_class` 会自动检查 GUI 层没有 `smartctl`
调用。

## 3. 使用步骤（只需 GitHub，无需本地环境）

1. 在 GitHub 新建仓库（Public/Private 都行）。
2. 把本目录全部文件推上去，默认分支命名为 `main`。
3. 打开仓库 **Actions** 页，会看到 `build` 工作流自动运行：
   - `test (3.11)` / `test (3.12)` —— 真实跑 `pytest -v`
   - `build exe` —— 只有两个 test job 全绿才会启动（`needs: test`）
4. 全部通过后，进入该次 run 页面底部 **Artifacts**，下载 `DiskFlow-windows`，解压即得 exe。
5. 想直接发版：先写 `CHANGELOG.md`，再打 tag

   ```bash
   # 1) 在 CHANGELOG.md 顶部加一节 `## [0.2.0] - 2026-10-22`，写清给用户看的变化
   # 2) tag 必须等于 main.py 里的 APP_VERSION（当前 0.2.0）
   git tag v0.2.0
   git push origin v0.2.0
   ```

   workflow 会额外创建 GitHub Release，并把 exe 作为附件挂上去，用户从 Release 页直接下载。

   **Release 正文来自 `CHANGELOG.md`，不是 workflow 里写死的文案**：CI 抽取与 `APP_VERSION`
   同名的小节（`## [0.2.0]`）作为正文；抽不到就**直接失败**，既不发正文空白的 Release，
   也不会悄悄退化成自动生成的 "What's Changed" 提交列表。

> 手动重跑：Actions 页 → 选中 `build` → **Run workflow**（对应 `workflow_dispatch`）。

## 4. 程序使用说明

- **首次打开**：只建立基线，底部提示「已记录基线，下次打开将显示增量」。
- **之后每次打开**：自动读取 + 记录 + 对比，表格里给出
  「距上次」「本次读增/写增」「历史读增/写增」。
- **刷新记录**：底部按钮，随时再记录一次（比如刚拷完大文件）。
- **打开历史文件**：直接定位到 `history.json` 所在位置。
- **history.json 位置**：优先 exe 同目录；该目录不可写（例如装在 `Program Files`）时自动回退到
  `%LOCALAPPDATA%\DiskUsageTracker\history.json`。
  目录名是**固定内部 ID**，改显示名不会让历史记录“丢失”。
- **累计读写的单位口径（重要）**：优先取 smartctl 的 *Device Statistics → Logical Sectors
  Read/Written*，它恒按 512 字节扇区计数，是权威来源。只有该字段缺失时才退回 SMART 属性
  241/242（Total_LBAs_*）——这两个属性的计数单位随主控而异（多数按 512 字节 LBA，部分主控按
  16/32 MiB 块），会明显偏小，此时状态列会标注「累计量取自 SMART 241/242，可能偏小」。
  > v0.2.0 修的就是这个 bug：早期版本无条件把 241/242 当 512 字节 LBA，SATA 盘 32 TB 的读量
  > 可能只显示成 1 GB 量级。
- **GB / TB 双单位**：累计读写超过 1 TB（1024 GB）时显示成 `31.80 TB (32,567.42 GB)`，
  两种口径都摆出来，避免换算歧义。
- **健康度**：只显示硬盘自己报告的剩余寿命百分比，不估算、不拿 0E 或重映射数冒充。
  NVMe 用 100 − percentage_used；SATA 依次尝试属性 231 / 202 / 177 / 233 / 169
  （177 的 raw 是磨损循环次数，只采信归一化值）。硬盘没报就显示「不适用」。
- **0E 告警**：0E = NVMe 的 *Media and Data Integrity Errors*。数值上升说明介质层面出现了
  新的不可纠正错误，程序会把整行标红并提示「介质错误在累积，建议关注备份」。稳定则显示
  「稳定」；SATA 盘没有这个字段，显示「不适用」，另附重映射 / 待映射扇区数作为辅助健康信息。
- **管理员权限**：读 SMART 需要提权。exe 已用 `--uac-admin` 打包，双击会弹 UAC。没提权时
  顶部会提示「未以管理员运行，部分磁盘可能读取失败」。
- **找不到 smartctl**：顶部会提示可操作命令 `winget install smartmontools.smartmontools`，
  程序不会崩溃。正常情况下 smartctl.exe 已随 exe 打包，走 `sys._MEIPASS` 解析，无需另装。

## 5. 本地运行 / 测试（可选）

```bash
python -m pip install -r requirements.txt
python -m pytest -v
```

测试不碰真实硬盘：数据来源是 `FixturesProvider` 读 `fixtures/smartctl_*.json`，
历史文件写在 pytest 的 `tmp_path` 里。其中 `fixtures/sata_real_960gb.json` 是**真机抓取**的
SATA 样本（只脱敏了型号/序列号/固件/WWN），保留了 `exit_status=64`、重复的
"General Statistics" 页、241/242 与 Logical Sectors 相差 65536 倍这些真机才会出现的形态。

想给自己机器上的盘补一份真实样本，见 `_local/dump_smartctl.ps1`（自动提权、自动找/装
smartmontools、按盘导出无 BOM 的 UTF-8 JSON）。注意必须管理员运行，否则 SATA/USB 盘会
`Error=5` 打不开；`-x` 不能省，Device Statistics 靠它。

打包（需要 Windows + smartctl.exe 放在项目根目录）：

```bat
build.bat
```

或直接用 CI 里的同一条命令：

```bat
python -m PyInstaller --onefile --noconsole --uac-admin --name DiskFlow ^
  --add-binary "smartctl.exe;." main.py
```

`app.ico` 可选：放在项目根目录并加 `--icon app.ico` 即可。

## 6. 名称与版本集中配置（改名 / 升版本只改这里）

```python
# main.py:31-35
APP_DISPLAY_NAME = "磁盘流水 DiskFlow"             # 窗口标题 / 界面（已定名）
APP_EXE_NAME = "DiskFlow"                          # --name / exe 文件名 / artifact 名
APP_ARTIFACT_NAME = "DiskFlow-windows"             # GitHub Actions artifact 名
APP_STORAGE_ID = "DiskUsageTracker"                # 【不要改】历史目录固定 ID
APP_VERSION = "0.2.0"                              # 版本号：关于框 + 发版校验
```

- 窗口标题、PyInstaller `--name`、artifact 名、Release 附件名都引用这几个常量，
  workflow 与 `build.bat` 都是**读 main.py** 而不是写死，所以不需要改别处。
- 同步改 `test_disk_tracker.py` 中 `test_names_are_configured_in_one_place` 的断言值，否则 CI 会红。
- `APP_STORAGE_ID` 保持不动，历史数据不会搬家。
- 已发布的 tag 里的 asset 名是旧的；改名后需要打一个新 tag 才能拿到新文件名。

### 版本号（`APP_VERSION`）

- 唯一来源就是 `APP_VERSION`，界面上没有任何写死的版本字面量。
  底部「关于」按钮弹出的正文由 `about_text()` 生成，其中 `版本：v{APP_VERSION}` 直接引用该常量。
- **发版流程**：先把 `APP_VERSION` 改成新版本 → 提交推送 → 打同名 tag（`v` + `APP_VERSION`）。
- CI 的 `Resolve app names from main.py` 步骤会校验：
  - `APP_VERSION` 必须是 `X.Y.Z` 点分数字；
  - tag 触发时 `refs/tags/v<tag>` 必须与 `APP_VERSION` **完全一致**，否则直接 fail，
    避免出现「exe 里写着 0.1.0，Release 却叫 v0.2.0」的错位。
- 对应测试：`test_app_version_is_configured_and_has_single_source`、
  `test_about_text_references_app_version_and_names`、
  `test_about_text_reflects_changed_version`、`test_gui_about_button_shows_version`。

### 发版清单（顺序不能反）

1. 改 `main.py` 的 `APP_VERSION`；
2. 在 `CHANGELOG.md` **顶部**加一节 `## [新版本] - YYYY-MM-DD`（CI 只认第一个匹配项，
   且 `test_changelog_newest_entry_matches_app_version` 会强制「最新小节 == APP_VERSION」）；
3. 提交并推送 `main`，等 CI 的 `test` + `build exe` 全绿；
4. `git tag v<APP_VERSION>` 并推送 —— CI 用 `CHANGELOG.md` 该小节作为 **Release 正文**。

这四步的顺序反过来会被 CI 拦住：先打 tag 再改 `APP_VERSION`，版本校验直接 fail；
只改 `APP_VERSION` 不写日志，`Extract release notes` 步骤会因为找不到小节而 fail。
