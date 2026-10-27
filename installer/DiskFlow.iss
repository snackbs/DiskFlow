; =============================================================================
; 磁盘流水 DiskFlow —— Inno Setup 安装脚本
; =============================================================================
; 用途：把 dist\<APP_EXE_NAME>.exe 打成安装包（写 Program Files + 开始菜单快捷方式
;       + 卸载项）。便携版 exe 仍然单独发布，两者并存。
;
; 【名称与版本不写在这个文件里】
;   本脚本里出现的 App 名 / exe 名 / 版本 / 发布者 / 安装包文件名，全部来自
;   编译期宏。宏有两个来源，任一即可：
;     1) CI 与本地推荐：python installer\make_build_info.py 生成 installer\build-info.iss
;        （值从 diskflow/config.py 读，单一配置点）
;     2) 手写传给 ISCC：/DMyAppName=... /DMyAppExeName=... /DMyAppVersion=...
;   两者都没有时编译直接失败（见下面的 #ifndef/#error），不会产出名字空白的安装包。
;
; 【AppId 是固定 GUID，绝不能改】
;   安装包升级换代靠它判断「同一个软件的旧版本」。一旦改动，用户机器上会并存
;   两份程序（各自的卸载项、各自的目录）。改名（显示名 / exe 名）时也不要动它。
;
; 【历史数据】
;   exe 自身带 requireAdministrator 清单，装到 Program Files 后提权运行会让
;   「写探测」假性成功，历史就会落在安装目录里（卸载即删、多用户共用一份）。
;   所以程序侧（diskflow/history.py 的 default_storage_dir）在检测到 exe 位于
;   Program Files 下时，强制把 history.json / settings.json 放到
;   %LOCALAPPDATA%\DiskUsageTracker\。卸载本程序【不会】删除该目录。
; =============================================================================

; ---- 名称/版本：优先用命令行 /D，其次用生成的文件 --------------------------
; 路径先算好放进标识符，ifnexist 只认标识符或字符串字面量，直接塞表达式并不可靠
#define BuildInfoFile AddBackslash(SourcePath) + "build-info.iss"
#ifndef MyAppVersion
  #ifnexist BuildInfoFile
    #error 未提供版本信息：请先运行 python installer\make_build_info.py，或用 /DMyAppVersion=... 传入
  #endif
  #include "build-info.iss"
#endif
#ifndef MyAppName
  #error 缺少 MyAppName（用 /DMyAppName=... 传入，或先生成 installer\build-info.iss）
#endif
#ifndef MyAppExeName
  #error 缺少 MyAppExeName（用 /DMyAppExeName=... 传入，或先生成 installer\build-info.iss）
#endif
#ifndef MyAppPublisher
  #define MyAppPublisher "snackbs"
#endif
; 安装包文件名也只从宏来（公式只写在 installer/make_build_info.py 一处）
#ifndef MySetupBaseName
  #error 缺少 MySetupBaseName（先运行 python installer\make_build_info.py，或 /DMySetupBaseName=... 传入）
#endif

; 简体中文语言文件：CI 会下载到 installer\ChineseSimplified.isl。
; 缺失时直接编译失败，而不是悄悄退化成英文界面。
#define ChineseIslFile AddBackslash(SourcePath) + "ChineseSimplified.isl"
#ifnexist ChineseIslFile
  #error 缺少 installer\ChineseSimplified.isl（Inno Setup 的简体中文语言文件）
#endif

#define MyAppId "{{8F3C2E51-9A4B-4E7D-9C1A-6B2D5F0A7C43}"

[Setup]
; 固定 GUID：改名前后的安装包靠它互相认定为同一软件（详见文件头说明）
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

; 装到 Program Files（管理员模式下的 {autopf}），并加卸载项
DefaultDirName={autopf}\{#MyAppExeName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes

; 安装程序本身要提权（写 Program Files + HKLM 卸载项）
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=

; 只允许 x64 系统：exe 与内嵌的 smartctl.exe 都是 x64 PE
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 安装时若程序正在运行：由 Windows Restart Manager 提示关闭（不加 force，避免丢数据）
CloseApplications=yes
RestartApplications=no

; 界面语言：默认简体中文。LanguageDetectionMethod=none → 直接取 [Languages] 第一项，
; 中文系统上不再弹语言选择框；保留英文供非中文用户切换（ShowLanguageDialog=yes）。
LanguageDetectionMethod=none
ShowLanguageDialog=yes

; 外观
WizardStyle=modern
SetupIconFile=..\diskflow-icons\diskflow.ico
UninstallDisplayIcon={app}\{#MyAppExeName}.exe
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\dist
OutputBaseFilename={#MySetupBaseName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "{#ChineseIslFile}"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
; 中文其余文案由 ChineseSimplified.isl 提供，这里只补本程序专有的几条
chinesesimplified.LaunchApp=启动 {#MyAppExeName}
english.LaunchApp=Launch {#MyAppExeName}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 主程序（onefile，内含 Python 运行时 / Tk / smartctl.exe）
Source: "..\dist\{#MyAppExeName}.exe"; DestDir: "{app}"; Flags: ignoreversion
; 图标一并放进去，便于在安装目录里辨认
Source: "..\diskflow-icons\diskflow.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 快捷方式一律用英文 exe 名（DiskFlow），中文显示名只用在开始菜单分组与卸载列表
Name: "{group}\{#MyAppExeName}"; Filename: "{app}\{#MyAppExeName}.exe"
Name: "{group}\{cm:UninstallProgram,{#MyAppExeName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppExeName}"; Filename: "{app}\{#MyAppExeName}.exe"; Tasks: desktopicon

[Run]
; 必须带 shellexec：目标 exe 的清单要求 requireAdministrator，用 CreateProcess 直接起
; 会失败在错误代码 740（请求的操作需要提升）。ShellExecuteEx 会走 UAC、由系统提权启动。
Filename: "{app}\{#MyAppExeName}.exe"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent shellexec

[UninstallDelete]
; 正常情况这两个文件不会出现在安装目录（程序已强制写 %LOCALAPPDATA%）。
; 这里只兜住「早期版本曾把历史写在 exe 同目录」的情况；用户数据目录一律不删。
Type: files; Name: "{app}\history.json"
Type: files; Name: "{app}\settings.json"
