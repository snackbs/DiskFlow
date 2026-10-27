#!/usr/bin/env python3
"""生成 Inno Setup 预处理器宏文件 installer/build-info.iss。

安装包脚本 installer/DiskFlow.iss 里【不写】任何名字和版本，全部用编译期宏：
    AppName / AppVersion / 安装包文件名 / exe 名 / 发布者
这些宏的唯一来源是 diskflow/config.py 的常量，所以改名或升版本时只改那一处，
然后重跑本脚本即可（CI 的 build job 也会自动跑）：

    python installer/make_build_info.py

另一个用途是让 workflow 得到安装包文件名，避免文件名公式散落在 .iss 与 yml 里：

    python installer/make_build_info.py --print-setup-name
    -> DiskFlow-<版本>-windows-setup

本脚本只用标准库，可在任意 Python 3.11+ 下运行；不依赖 Inno Setup 是否已安装。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diskflow import config  # noqa: E402  （必须在 sys.path 调整之后导入）

BUILD_INFO_FILENAME = "build-info.iss"


def iss_string(value: str) -> str:
    """把任意字符串变成 Inno 脚本里的双引号字面量（内部双引号翻倍）。"""
    return '"' + str(value).replace('"', '""') + '"'


def setup_base_name(exe_name: str, version: str) -> str:
    """安装包文件名（不含目录与扩展名）。

    形如 DiskFlow-<版本>-windows-setup。公式只在这里写一次：
    .iss 只引用 {#MySetupBaseName} 宏，workflow 只调用 --print-setup-name。
    """
    return f"{exe_name}-{version}-windows-setup"


def build_info_lines(
    display_name: str | None = None,
    exe_name: str | None = None,
    version: str | None = None,
    publisher: str | None = None,
) -> list[str]:
    """生成 build-info.iss 的全文（每行一个字符串，不含换行符）。"""
    display = config.APP_DISPLAY_NAME if display_name is None else display_name
    exe = config.APP_EXE_NAME if exe_name is None else exe_name
    ver = config.APP_VERSION if version is None else version
    pub = config.APP_AUTHOR if publisher is None else publisher
    return [
        "; 本文件由 installer/make_build_info.py 从 diskflow/config.py 生成，请勿手改。",
        "; 改名或升版本只改 diskflow/config.py 里的常量，然后重跑那个脚本。",
        f"#define MyAppName {iss_string(display)}",
        f"#define MyAppExeName {iss_string(exe)}",
        f"#define MyAppVersion {iss_string(ver)}",
        f"#define MyAppPublisher {iss_string(pub)}",
        f"#define MySetupBaseName {iss_string(setup_base_name(exe, ver))}",
    ]


def render(
    display_name: str | None = None,
    exe_name: str | None = None,
    version: str | None = None,
    publisher: str | None = None,
) -> str:
    """生成 build-info.iss 全文（带行尾换行的字符串）。"""
    return "\n".join(build_info_lines(display_name, exe_name, version, publisher)) + "\n"


def write_build_info(target: Path | None = None) -> Path:
    """把 build-info.iss 写到 installer/ 下（默认），返回写入路径。"""
    path = Path(target) if target else Path(__file__).resolve().parent / BUILD_INFO_FILENAME
    path.write_text(render(), encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--print-setup-name" in args:
        print(setup_base_name(config.APP_EXE_NAME, config.APP_VERSION))
        return 0
    path = write_build_info()
    print(f"已写入 {path}")
    print(f"  AppName        = {config.APP_DISPLAY_NAME}")
    print(f"  AppExeName     = {config.APP_EXE_NAME}")
    print(f"  AppVersion     = {config.APP_VERSION}")
    print(f"  MySetupBaseName= {setup_base_name(config.APP_EXE_NAME, config.APP_VERSION)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
