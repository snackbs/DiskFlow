@echo off
rem ---------------------------------------------------------------------------
rem 本地 Windows 手动构建脚本（可选）。
rem CI 已经覆盖全部流程；只有想在本地直接出 exe 时才用这个脚本。
rem 名称从 main.py 读取（APP_EXE_NAME），脚本里不写死软件名。
rem 用法：把 smartctl.exe 放在本文件同级目录，然后双击或执行 build.bat
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 python，请先安装 Python 3.11/3.12 并勾选 Add to PATH。
  exit /b 1
)

for /f "usebackq delims=" %%i in (`python -c "import main as m; print(m.APP_EXE_NAME)"`) do set "APP_EXE_NAME=%%i"
for /f "usebackq delims=" %%i in (`python -c "import main as m; print(m.APP_DISPLAY_NAME)"`) do set "APP_DISPLAY_NAME=%%i"
for /f "usebackq delims=" %%i in (`python -c "import main as m; print(m.locate_app_icon() or '')"`) do set "APP_ICON_PATH=%%i"

if "%APP_EXE_NAME%"=="" (
  echo [错误] 无法从 main.py 读取 APP_EXE_NAME。
  exit /b 1
)
echo 正在构建：%APP_DISPLAY_NAME%  ^(exe 名：%APP_EXE_NAME%.exe^)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo 正在安装 PyInstaller ...
  python -m pip install pyinstaller
)

if "%APP_ICON_PATH%"=="" (
  echo [错误] 找不到应用图标 diskflow-icons\diskflow.ico。
  exit /b 1
)
echo 应用图标：%APP_ICON_PATH%

if not exist "smartctl.exe" (
  echo [警告] 当前目录没有 smartctl.exe。
  echo        打包会失败或产出一个读不到磁盘的 exe。
  echo        获取方式：
  echo          1^) winget install smartmontools.smartmontools
  echo             然后从 "C:\Program Files\smartmontools\bin\smartctl.exe" 拷到本目录
  echo          2^) 从 https://github.com/smartmontools/smartmontools/releases 下载
  echo             smartmontools-7.5.win32-setup.exe，安装后拷出 smartctl.exe
  echo.
  pause
)

python -m PyInstaller --onefile --noconsole --uac-admin --name "%APP_EXE_NAME%" --icon "%APP_ICON_PATH%" --add-binary "smartctl.exe;." --add-data "%APP_ICON_PATH%;." main.py
if errorlevel 1 (
  echo [错误] PyInstaller 失败。
  exit /b 1
)

echo.
echo 构建完成：dist\%APP_EXE_NAME%.exe
echo 提示：程序以管理员身份启动。便携版把 history.json 写在 exe 同目录；
echo       安装版（装到 Program Files）会强制改存 %%LOCALAPPDATA%%\DiskUsageTracker。

echo.
rem --- 可选：如果本机装了 Inno Setup，顺手把安装包也编译出来 -------------------
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if defined ISCC (
  rem 语言文件与宏文件都是构建期产物（已 gitignore），这里现场准备
  if not exist "installer\ChineseSimplified.isl" (
    echo 正在下载简体中文语言文件 ...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/ChineseSimplified.isl' -OutFile 'installer\ChineseSimplified.isl' -UseBasicParsing"
  )
  python installer\make_build_info.py
  "%ISCC%" installer\DiskFlow.iss
  if errorlevel 1 (
    echo [错误] Inno Setup 编译失败。
    exit /b 1
  )
  for /f "usebackq delims=" %%i in (`python installer\make_build_info.py --print-setup-name`) do set "SETUP_NAME=%%i"
  echo 安装包完成：dist\!SETUP_NAME!.exe
) else (
  echo 未找到 Inno Setup 6，跳过安装包。
  echo   想要安装包：winget install JRSoftware.InnoSetup，然后重跑本脚本。
)
endlocal
