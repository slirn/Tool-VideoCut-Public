# -*- coding: utf-8 -*-
"""调试边框绘制（REQ-20261108）——长/短视频精剪共用的红/蓝虚线框 PNG。

- 红框 = 视频展示区（用户配置的位置 + 显示尺寸，即视频应该展现的范围）
- 蓝框 = 实际落位（与 overlay 同口径复算的实际摆放矩形）
- 两框重合属正常（说明参数已生效）；分离 = 配置与实际有偏差（如越界钳制）

画布尺寸由调用方给定（短视频竖版 1080×1920、长视频横版 1920×1080），
绘制只发生在画布内（越界部分被 PIL 自然裁掉，保留「配置越界」的对照信息）。
"""

from __future__ import annotations

from pathlib import Path

DEBUG_RED = (255, 0, 0, 255)
DEBUG_BLUE = (0, 112, 255, 255)


def dashed_rect(draw, rect: dict, color, width: int,
                dash: int = 26, gap: int = 16) -> None:
    """在设计空间画轴对齐虚线矩形（调试边框用）。"""
    x0, y0 = int(rect["x"]), int(rect["y"])
    x1, y1 = x0 + int(rect["w"]), y0 + int(rect["h"])
    step = dash + gap

    def _h(py: int, xa: int, xb: int) -> None:
        t = xa
        while t < xb:
            draw.line([(t, py), (min(t + dash, xb), py)], fill=color, width=width)
            t += step

    def _v(px: int, ya: int, yb: int) -> None:
        t = ya
        while t < yb:
            draw.line([(px, t), (px, min(t + dash, yb))], fill=color, width=width)
            t += step

    _h(y0, x0, x1)
    _h(y1, x0, x1)
    _v(x0, y0, y1)
    _v(x1, y0, y1)


def border_png(path: Path, rects: list[tuple[dict, tuple, int]],
               w: int, h: int) -> None:
    """w×h 透明 PNG，逐框叠加虚线（红=视频展示区，蓝=实际落位）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for rect, color, width in rects:
        dashed_rect(draw, rect, color, width)
    img.save(path)
