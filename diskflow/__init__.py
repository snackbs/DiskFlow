"""磁盘流水 DiskFlow 的实现包。

按层分模块，依赖方向单向向下（越靠前越底层）：

    config      单一配置点（名称 / 版本 / 换算常量）
    convert     容量换算
    timefmt     时间格式化
    models      数据模型（层间唯一通信结构）
    parse       smartctl JSON -> DiskSnapshot（纯函数）
    providers   SmartProvider 注入点（唯一出现 smartctl 调用的地方）
    diskmap     盘符映射（只读，失败即降级）
    delta       增量计算 + 0E 判定
    history     history.json 读写 / 校验 / 损坏备份
    orchestrate 一次完整运行的编排（GUI 与测试共用）
    platformhelp 管理员权限检测
    theme       主题数据表
    viewmodel   视图数据表
    prefs       settings.json
    gui         tkinter 界面（不含任何 smartctl 调用）
    entry       窗口图标 / create_app / main

顶层 main.py 只是兼容 shim，为构建脚本和旧测试再导出这些名字。
"""

from __future__ import annotations
