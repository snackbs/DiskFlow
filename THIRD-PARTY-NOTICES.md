# 第三方组件声明 / Third-Party Notices

本仓库**自己的源码**以 [MIT 许可证](LICENSE) 发布。
但发布出去的二进制（便携版 `DiskFlow.exe` 与安装包
`DiskFlow-<版本>-windows-setup.exe`）里**还包含下列第三方程序**，
它们各自适用自己的许可证，不受 MIT 约束。

---

## smartmontools（smartctl.exe）

- **打包位置**：便携版 exe 内部（PyInstaller onefile 的 `sys._MEIPASS`），
  安装包则随主程序一起装进安装目录
- **本项目使用的版本**：smartctl 7.5 2025-04-30 r5714
  （`x86_64-w64-mingw32` 构建，来自 AppVeyor）
- **许可证**：GNU General Public License v2.0
  （全文见 [licenses/GPL-2.0.txt](licenses/GPL-2.0.txt)）
- **项目主页**：https://www.smartmontools.org/
- **源码**：https://github.com/smartmontools/smartmontools
- **官方 Windows 构建下载**：
  https://github.com/smartmontools/smartmontools/releases

### 调用方式与许可边界

本程序以**独立进程**方式调用 smartctl，不链接其代码：

```python
# diskflow/providers.py —— 全项目唯一执行 smartctl 的地方
proc = subprocess.run([smartctl_path, "-x", "-a", "--json", device_path], ...)
```

主程序只是解析 smartctl 打印到 stdout 的 JSON，两者通过命令行与标准流通信，
不共享地址空间、不静态链接、不构成同一作品。因此：

- DiskFlow 自身的源码**不需要**按 GPL 授权，继续使用 MIT；
- smartctl 本身仍完整适用 GPL-2.0，包括其源码可获取的要求。

### GPL-2.0 源码可获取性

GPL-2.0 第 3 条要求分发二进制时提供对应源码的获取方式。
本项目分发的是**未经修改的官方 smartctl.exe**，对应源码可从上面「源码」
链接获取，亦可从本项目仓库的 Release 页面获得指向。若你需要，
也可以通过 https://github.com/snackbs/DiskFlow/issues 索取。

---

## Python 运行时与 Tcl/Tk

便携版 exe 内含 Python 解释器与 Tcl/Tk 运行时（由 PyInstaller 打包）：

- **Python**：PSF License Agreement — https://docs.python.org/3/license.html
- **Tcl/Tk**：BSD 风格许可 — https://www.tcl.tk/software/tcltk/license.html

两者均为宽松许可证，允许随闭源或 MIT 程序一同分发，
只需保留各自的版权与许可声明。

---

## 开发期依赖（不随产品分发）

以下仅在 CI 与本地开发/测试时使用，**不会**出现在发布出去的 exe 里：

| 包 | 用途 | 许可证 |
|---|---|---|
| pytest | 运行测试 | MIT |
| PyInstaller | 打包 exe | GPL-2.0 **附例外条款**（允许打包专有程序，产物不受 GPL 约束） |

PyInstaller 的例外条款原文见
https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt
——正是它让用 PyInstaller 打包的 MIT 程序可以保持 MIT。
