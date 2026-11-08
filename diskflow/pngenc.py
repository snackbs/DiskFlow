"""半透明图元编码器：纯标准库生成 RGBA PNG，供画布贴「薄罩」与「胶囊滑块」。

为什么需要这个模块
------------------
tk.Canvas 的 rectangle / line 只能是不透明色，而第 7 套「图片背景」主题要在壁纸
切片上压半透明薄罩（不铺就看不见字、铺成实色就把壁纸闷死）。Tk 8.6 对 RGBA 图片
做真正的 alpha 合成（实测 50% 白压在纯蓝图片项上得到 (128,128,255)），所以薄罩只能
是图片。

历史上 solid_rgba_png 在 shell_canvas 与 photo_card 各有一份逐字相同的实现，
capsule_rgba_png 里又抄了第三份 chunk 写头逻辑。三份代码任何一处改动都要同步三次，
是明确的重复来源，这里收敛成唯一真源。两个模块都从这里导入并继续对外导出同名函数，
所以 `main.py` 的 __all__ 与既有测试（app.solid_rgba_png / app.capsule_rgba_png）不受影响。
"""

from __future__ import annotations

import struct
import tkinter as tk
import zlib
from typing import Any, Sequence


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    """一个 PNG 数据块：长度 + 类型 + 数据 + CRC32（CRC 覆盖类型与数据）。"""
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _write_png(width: int, height: int, raw: bytes) -> bytes:
    """把「每行前加 filter 字节 0」的扫描行数据封成 8 位 RGBA PNG。"""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (PNG_SIGNATURE + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))


def solid_rgba_png(width: int, height: int,
                   rgb: Sequence[int], alpha: int) -> bytes:
    """生成 width×height 的纯色 RGBA PNG 字节（纯标准库，不依赖 Pillow）。

    Tk 8.6 会对 RGBA 图片做真正的 alpha 合成，所以「半透明薄罩」只能做成图片——
    canvas 的 rectangle 只支持不透明色。photo_card 与 CardCanvas 共用这一个编码器。

    width/height 下限为 1：0 或负数在 PNG 里非法，Tk 会直接抛 TclError。
    """
    width = max(1, int(width))
    height = max(1, int(height))
    color = bytes((int(rgb[0]) & 0xFF, int(rgb[1]) & 0xFF,
                   int(rgb[2]) & 0xFF, max(0, min(255, int(alpha)))))
    raw = (b"\x00" + color * width) * height
    return _write_png(width, height, raw)


def capsule_rgba_png(width: int, height: int, rgb: Sequence[int], alpha: int,
                     radius: int = 0, orient: str = "vertical") -> bytes:
    """生成一个半透明圆角胶囊的 RGBA PNG（滚动条滑块的半透明版本）。

    solid_rgba_png 只能出直角矩形。滑块压在壁纸切片上时是个圆头粗条，用直角矩形
    会把两端的圆角糊成方块，比不透明时更难看。这里按「到中轴线的距离」算每个像素
    的覆盖率：中段满幅，两端是半径 radius 的半圆，边缘一像素做抗锯齿。

    orient="vertical" 时胶囊沿 y 轴（上下两端圆），"horizontal" 时沿 x 轴。
    半径默认取短边的一半（正圆头），并且绝不会超过短边的一半——否则画出来是椭圆。
    """
    width = max(1, int(width))
    height = max(1, int(height))
    vertical = str(orient) != "horizontal"
    short_side = width if vertical else height
    corner = int(radius) if radius else max(1, short_side // 2)
    corner = max(1, min(corner, max(short_side // 2, 1)))
    red = int(rgb[0]) & 0xFF
    green = int(rgb[1]) & 0xFF
    blue = int(rgb[2]) & 0xFF
    peak = max(0, min(255, int(alpha)))
    center_x = width / 2.0
    center_y = height / 2.0
    rows = []
    for y in range(height):
        if vertical:
            # 竖直胶囊：两端（y 方向）收圆，中段 dy=0
            if y < corner:
                dy = corner - y - 0.5
            elif y >= height - corner:
                dy = y - (height - corner) + 0.5
            else:
                dy = 0.0
        else:
            dy = y + 0.5 - center_y
        row = bytearray()
        for x in range(width):
            dx = x + 0.5 - center_x
            if not vertical:
                # 水平胶囊：两端（x 方向）收圆
                if x < corner:
                    dx = corner - x - 0.5
                elif x >= width - corner:
                    dx = x - (width - corner) + 0.5
                else:
                    dx = 0.0
            distance = (dx * dx + dy * dy) ** 0.5
            if distance <= corner - 0.5:
                cover = 255
            elif distance >= corner + 0.5:
                cover = 0
            else:
                cover = int(round((corner + 0.5 - distance) * 255))
            row += bytes((red, green, blue, peak * cover // 255))
        rows.append(b"\x00" + bytes(row))
    return _write_png(width, height, b"".join(rows))


class VeilImage:
    """半透明薄罩的单图缓存：尺寸与颜色不变就复用同一张 PhotoImage。

    薄罩每次 repaint 都要重建，而重建意味着一次 zlib 压缩（1244x699 约 3ms）加一次
    Tk 图片解码。拖动窗口时 repaint 一秒几十次，不复用会明显卡顿，所以按
    (宽, 高, RGB, 透明度) 做 key 缓存。SliceCanvas 与 PhotoCard 共用这个类，
    两处薄罩逻辑从此只有一份。

    alpha<=0 时 image() 返回 None，调用方据此不铺（而不是铺一张全透明的图占内存）。
    """

    __slots__ = ("_master", "_key", "_photo")

    @staticmethod
    def _key_for(width: int, height: int, rgb: Sequence[int],
                 alpha: float) -> tuple[int, int, bytes, int] | None:
        """归一化后的缓存键；alpha<=0（不铺）时返回 None。"""
        level = int(round(max(0.0, min(1.0, float(alpha))) * 255))
        if level <= 0:
            return None
        return (max(1, int(width)), max(1, int(height)),
                bytes((int(rgb[0]) & 0xFF, int(rgb[1]) & 0xFF,
                       int(rgb[2]) & 0xFF)), level)

    def __init__(self, master: Any) -> None:
        self._master = master
        self._key: tuple[int, int, bytes, int] | None = None
        self._photo: Any = None

    def matches(self, width: int, height: int, rgb: Sequence[int],
                alpha: float) -> bool:
        """这张尺寸+颜色是否已有缓存（有的话调用方可以直接复用现有 item）。"""
        return self._key == self._key_for(width, height, rgb, alpha)

    def image(self, width: int, height: int, rgb: Sequence[int],
              alpha: float) -> Any:
        """取（必要时新建）一张 width×height 的薄罩图；alpha<=0 返回 None。"""
        key = self._key_for(width, height, rgb, alpha)
        if key is None:
            return None
        if key == self._key and self._photo is not None:
            return self._photo
        width, height, color, level = key
        data = solid_rgba_png(width, height, color, level)
        self._photo = tk.PhotoImage(master=self._master, data=data)
        self._key = key
        return self._photo
