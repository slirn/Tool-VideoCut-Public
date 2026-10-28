"""短视频 Stage 6 精剪混编渲染器（REQ-20261018-176）。

用户需求：生成预览/导出时，先按背景图说明检测各区域，再把各区域参数与
内容对应到背景图上混编；参考长视频精剪合成（``app.py::_assemble_fine_filter``
纯 ffmpeg 滤镜图），在其基础上加短视频特有素材（头像 / 导师文字 / 公共
背景 / BGM）。方法独立成模块，**不 import app.py、不改长视频任何代码**。

图层序（1080×1920 设计空间构建，尾部按 output.resolution 缩放）：

    L0 黑底画布（防 RGBA 背景图透明区渲染成白框）
    L1 背景图 → layout.bg 区域（铺满拉伸填充，REQ-20261018-192）
    L2 主视频 → 源裁剪 crop_*（1920×1080 设计空间）→ contain 进 layout.video
    L3 导师头像 → layout.avatar（circle 走 geq 圆形 alpha 掩码）
    L4 文字单层烧录 ass=（字幕 Default + 导师 MentorNick/MentorTitle 三样式）
    L5 片头封面 concat 前缀（全屏 N 秒 + 音频静音前缀）
    L6 尾部分辨率 scale（≠1080×1920 才有）
    A1 音频精剪（导出按 SRT 段去缝；预览整窗直通）
    A2 封面静音前缀 concat
    A3 BGM（volume/afade + amix duration=first:normalize=0）

导出在滤镜图内做 SRT 精剪（替代 cut_by_srt.py moviepy 子进程）：单次编码、
连续时间轴可精确分段映射。旧 ``short_video_finalize.run_stage6_one`` 代码
保留不删（不再被渲染路径调用）。
"""

from __future__ import annotations

import copy
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from slirn_home import short_video_service as svc
from slirn_home import hw_encode

log = logging.getLogger(__name__)

# 竖屏设计空间（对偶长视频 1920×1080）；所有 layout.* 坐标以它为准
DESIGN_W, DESIGN_H = 1080, 1920
# 源画面裁剪设计空间（源是横屏课程视频；REQ-20261018-174）
SRC_DESIGN_W, SRC_DESIGN_H = 1920, 1080

# 公开版：字体不入库，渲染时按需获取（存在则用 fontsdir，否则省略走系统回退）
from slirn_home.font_provider import ensure_font_dir as _ensure_font_dir
_FONT_DIR = Path(__file__).resolve().parent.parent / "font"
# font/STHeitiMedium.ttc 内部真实家族名是 "Heiti SC Medium"（fontTools 实测）；
# 长视频 _ass_style_line 映射到 "STHeiti Medium" 实际匹配不到，一直靠系统
# fontconfig 回退 —— 本模块用正确家族名 + ass 滤镜 fontsdir 摆脱系统依赖。
_FAMILY_MAP = {
    "STHeitiMedium": "Heiti SC Medium",
    "STHeitiMedium.ttc": "Heiti SC Medium",
    "stheitimedium": "Heiti SC Medium",
    "Noto Sans CJK SC": "Noto Sans CJK SC",
}
_RESOLUTION_OUT: dict[str, tuple[int, int]] = {
    "1080p": (1080, 1920),
    "720p": (720, 1280),
    "960x1280": (960, 1280),
    # REQ-20261018-199：3:4 第二档（用户口径 1086×1448，与 960×1280 同族
    # 同比例，仅更大；异比例输出走 REQ-190 整体拉伸铺满）
    "1086x1448": (1086, 1448),
    # REQ-20261018-187：16:9 横屏两档（设计空间 1080×1920 是竖版 —— 渲染
    # 尾部 scale 等比 contain + pad 黑边，内容不变形）
    "1280x720": (1280, 720),
    "1920x1080": (1920, 1080),
    # source：竖屏画布与横屏源无自然映射，语义定为「保持设计空间、不降档」
    "source": (1080, 1920),
}
_FALLBACK_OUT = (720, 1280)
# fallback 检测结果 10 分钟内不重试（防渲染路径被 LLM 故障拖死）
_DETECT_RETRY_COOLDOWN_SEC = 600
# SRT 相邻段缝隙 ≤0.1s 合并 —— 连续 coarse SRT（段段相接）合并成单段，
# 滤镜图不随字幕行数爆炸
_MERGE_GAP_SEC = 0.1
# 超大图预缩阈值（长视频 _maybe_prescale_image 同款口径）
_PRESCALE_MAX = 4096

_SRT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


# ---------- 纯函数层（不跑 ffmpeg、不落盘，全部可单测） ----------

def _filter_path(p: Path | str) -> str:
    """滤镜参数里的 Windows 路径转义：`\\`→`/`、`:`→`\\:`（app.py 同款，模块内复制）。"""
    return str(p).replace("\\", "/").replace(":", r"\:")


def _family_name(raw: str) -> str:
    """字体存储值（文件名形）→ libass 家族名。"""
    key = str(raw or "").strip()
    return _FAMILY_MAP.get(key, _FAMILY_MAP.get(key.lower(), key or "Heiti SC Medium"))


def _output_size(resolution: str, bg_path: Path | None) -> tuple[int, int]:
    """output.resolution → 输出像素。bg_res 读背景图原生尺寸（偶数钳制 +
    长边 ≤4096）；探测失败 / 无 bg / 白名单外 → 720×1280。"""
    res = str(resolution or "").strip()
    if res == "bg_res":
        if bg_path and Path(bg_path).is_file():
            from slirn_home import short_video_fine as fine

            size = fine._probe_video_size(Path(bg_path))
            if size:
                w = min(int(size[0]) or _FALLBACK_OUT[0], _PRESCALE_MAX)
                h = min(int(size[1]) or _FALLBACK_OUT[1], _PRESCALE_MAX)
                return (max(2, w - (w % 2)), max(2, h - (h % 2)))
        return _FALLBACK_OUT
    return _RESOLUTION_OUT.get(res, _FALLBACK_OUT)


def _rect(layout: dict, key: str) -> dict:
    r = (layout or {}).get(key) or {}
    return {
        "x": max(0, int(r.get("x") or 0)),
        "y": max(0, int(r.get("y") or 0)),
        "w": max(1, int(r.get("w") or 1)),
        "h": max(1, int(r.get("h") or 1)),
    }


def _clamp_rect(rect: dict) -> dict:
    """区域钳进设计空间。"""
    x = max(0, min(DESIGN_W - 1, int(rect["x"])))
    y = max(0, min(DESIGN_H - 1, int(rect["y"])))
    w = max(1, min(DESIGN_W - x, int(rect["w"])))
    h = max(1, min(DESIGN_H - y, int(rect["h"])))
    return {"x": x, "y": y, "w": w, "h": h}


def _parse_srt(path: Path | str) -> list[dict]:
    """SRT → [{start, end, text}]（秒）。时间行非法的块跳过。"""
    try:
        raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    entries: list[dict] = []
    for block in re.split(r"\r?\n\r?\n", raw):
        m = _SRT_TIME_RE.search(block)
        if not m:
            continue
        g = [int(v) for v in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        if end <= start:
            continue
        text = re.sub(r"^\d+\s*$", "", block[m.end():], count=1).strip()
        entries.append({"start": start, "end": end, "text": text})
    return entries


def map_segments(
    entries: list[dict],
    probe_sec: float | None,
) -> tuple[list[tuple[float, float]], list[float]]:
    """SRT 条目 → 精剪保留区间（排序 / 去重叠 / 缝隙≤0.1s 合并 / 截 probe 尾）
    + 前缀和时间轴（prefix[i] = 第 i 段在新时间轴上的起点）。
    """
    spans = sorted(
        (max(0.0, float(e.get("start") or 0.0)), max(0.0, float(e.get("end") or 0.0)))
        for e in entries
        if float(e.get("end") or 0.0) > float(e.get("start") or 0.0)
    )
    if probe_sec:
        spans = [(s, min(e, float(probe_sec))) for s, e in spans if s < float(probe_sec)]
    merged: list[tuple[float, float]] = []
    for s, e in spans:
        if merged and s - merged[-1][1] <= _MERGE_GAP_SEC:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    prefix: list[float] = []
    acc = 0.0
    for s, e in merged:
        prefix.append(acc)
        acc += e - s
    return merged, prefix


def remap_srt_time(t: float, segs: list[tuple[float, float]],
                   prefix: list[float]) -> float:
    """源时间 → 去缝后新时间轴。缝隙里的时刻贴段尾（字幕不悬空）。"""
    for (s, e), base in zip(segs, prefix):
        if t <= e:
            return base + max(0.0, min(t, e) - s)
    return (prefix[-1] + (segs[-1][1] - segs[-1][0])) if segs else 0.0


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\n", r"\N")
    )


def _ass_color(hex_color: str, alpha: float = 1.0) -> str:
    """#RRGGBB + 不透明度（1=实心）→ &HAABBGGRR（ASS 色序 BGR，AA=透明度）。"""
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", str(hex_color or "").strip())
    if not m:
        m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", "#FFFFFF")
    hx = m.group(1) if m else "FFFFFF"
    r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    aa = max(0, min(255, round((1.0 - max(0.0, min(1.0, alpha))) * 255)))
    return f"&H{aa:02X}{b:02X}{g:02X}{r:02X}"


_ALIGN_MAP = {"left": 1, "center": 2, "right": 3}
# REQ-20261018-177：标题顶锚（7/8/9 = 顶左/顶中/顶右；Margin 盒 = title 区域）
_ALIGN_MAP_TITLE = {"left": 7, "center": 8, "right": 9}


def _title_style_line(cfg: dict, rect: dict, name: str = "Title",
                      scale_x: float = 100.0) -> str:
    """REQ-20261018-177：标题样式 —— Outline=0 + Shadow=深度（BorderStyle=1
    下 BackColour 即阴影色；区别于字幕的描边口径）。Margin 盒 = 标题区域，
    libass 在盒宽内自动折行（WrapStyle: 0）。REQ-20261018-185：样式名
    参数化（封面标题用 CoverTitle，Dialogue 引用必须与 Style 名一致）。"""
    size = max(8, min(200, int(float(cfg.get("size") or 64))))
    depth = max(0, min(20, int(float(cfg.get("shadow_depth") or 0))))
    try:
        op = float(cfg.get("shadow_opacity") if cfg.get("shadow_opacity") is not None else 0.6)
    except (TypeError, ValueError):
        op = 0.6
    bold = -1 if cfg.get("bold", True) else 0
    return (
        f"Style: {name},{_family_name(cfg.get('family'))},{size},"
        f"{_ass_color(cfg.get('color'))},&H00FFFFFF,"
        f"&H00000000,{_ass_color(cfg.get('shadow_color'), max(0.0, min(1.0, op)))},"
        f"{bold},0,0,0,{_fmt_scale(scale_x)},100,0,0,1,0,{depth},"
        f"{_ALIGN_MAP_TITLE.get(str(cfg.get('align') or 'center'), 8)},"
        f"{max(0, rect['x'])},{max(0, DESIGN_W - (rect['x'] + rect['w']))},"
        f"{max(0, rect['y'])},1"
    )


# REQ-20261018-181：折行 token 口径 —— 拉丁/数字连串（含紧邻的 + - * / > < = . _
# 等半角符号，如 "1+1>2"、"WaytoAGI"）整串原子不可拆；空格串独立成 token
# （用户敲的空格是内容，折行时保留、必要时带到下一行行首成可见缩进）；
# 其余（CJK 等）单字成 token。
_WRAP_TOKEN_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z+\-*/><=._]*|\s+|.")


def _wrap_title_lines(text: str, rect: dict, cfg: dict,
                      width_factor: float = 1.0) -> list:
    """REQ-20261018-179：标题超宽**预折行**。libass 对无空格的 CJK 长句
    不折行（顶中对齐向两侧溢出、左缘出画被裁 —— 实测），渲染前按
    「区域宽 / 字号」估算每行容量贪婪断行；显式 ``\\n`` 保留为硬换行。
    全角≈1.0、半角≈0.55 字宽，合成加粗留 4% 余量。

    REQ-20261018-181：空格**不丢**——折行断口落在空格后时，行尾空格移到
    下一行行首（渲染成可见缩进）；拉丁/数字串不跨行拆半（"AI" 不许拆成
    "A / I"，"1+1>2" 不许拆成 "1+1> / 2"）。"""
    # REQ-20261018-202：width_factor = kx/ky（异比例输出 ScaleX 压缩的反向
    # 因子）——字形步进压 ky/kx 后单行装更多字，容量同步放宽，满行屏幕
    # 占比与 9:16 一致；默认 1.0 口径字节级不变
    size = max(8, min(200, int(float(cfg.get("size") or 64))))
    max_units = max(1.0, rect["w"] * width_factor / size * 0.96)

    def _units(s: str) -> float:
        return sum(1.0 if ord(c) > 0x2E7F else 0.55 for c in s)

    out: list = []
    for raw in str(text or "").split("\n"):
        cur, units = "", 0.0
        for tok in _WRAP_TOKEN_RE.findall(raw):
            u = _units(tok)
            if cur and units + u > max_units:
                # 行尾空格随断口移到下一行行首（保住可见性，不删）
                tail = ""
                while cur and cur[-1] in " \t":
                    tail = cur[-1] + tail
                    cur = cur[:-1]
                if cur:
                    out.append(cur)
                cur, units = tail, _units(tail)
            # 超长 token 兜底硬拆（连续无空格超一行宽；含行首 token
            # 本身就超宽的情况）
            while u > max_units - units and len(tok) > 1:
                take, take_u = "", 0.0
                for ch in tok:
                    cu = 1.0 if ord(ch) > 0x2E7F else 0.55
                    if take and take_u + cu > max_units - units:
                        break
                    take += ch
                    take_u += cu
                if not take:
                    take, take_u = tok[0], _units(tok[0])
                out.append(cur + take)
                cur, units = "", 0.0
                tok = tok[len(take):]
                u = _units(tok)
            cur, units = cur + tok, units + u
        out.append(cur)
    return out


# REQ-20261018-195：标题文字行间距（px）。libass 的 \N 行距由字体行高
# 决定、ASS 无行距样式字段（Style 的 Spacing 是字间距）——实测标定
# Heiti SC Medium 行距 pitch == 字号（64/88/100 全档 k=1.0，粗体同），
# 且 Dialogue 行级 MarginV 覆盖精确生效（行顶差 == MarginV 差）。
# 由此：间距 ≠0 时改逐行独立 Dialogue，行顶 pitch = 字号 + 行间距；
# 间距 = 0 保持原单事件 \N 口径（老项目观感零变化）
def _line_spacing(cfg: dict, key: str = "line_spacing") -> int:
    """行间距读取 + 钳制（-20..60；None/异常兜底 0）。"""
    raw = cfg.get(key)
    if raw is None or raw == "":
        return 0
    try:
        return max(-20, min(60, int(float(raw))))
    except (TypeError, ValueError):
        return 0


def _title_line_events(wrapped: list, base_y: int, size: int,
                       spacing: int) -> list:
    """标题行事件 [(MarginV, 已转义文本)]。

    spacing=0 或单行 → 单事件 \\N 拼接、MarginV=0（继承样式，ASS 串与
    原口径逐字节一致）；否则逐行独立事件，MarginV = base_y +
    i*(size+spacing)（Dialogue 的 MarginL/R 留 0 = 继承样式水平锚，
    行首空格缩进不受影响——独立事件实测保留）。
    """
    if spacing == 0 or len(wrapped) <= 1:
        return [(0, "\\N".join(wrapped))]
    return [(base_y + i * (size + spacing), seg)
            for i, seg in enumerate(wrapped)]


def _mentor_stroke(mentor: dict, field: str) -> int:
    """REQ-20261018-182：导师文字描边宽度 —— enabled 开关关掉或宽度 0
    都返回 0（不描边）；异常值兜底默认 2。"""
    if not mentor.get(f"{field}_outline_enabled", True):
        return 0
    raw = mentor.get(f"{field}_outline_width")
    if raw is None or raw == "":
        return 2
    try:
        return max(0, min(10, int(float(raw))))
    except (TypeError, ValueError):
        return 2


def _fmt_scale(v: float) -> str:
    """REQ-20261018-202：ScaleX 百分比格式 —— 整值去小数点（100/75），
    否则 4 位小数去尾零（16:9 族会出现 31.6 之类的值）。"""
    i = round(v)
    if abs(v - i) < 0.005:
        return str(int(i))
    s = "%.4f" % v
    return s.rstrip("0").rstrip(".")


def _style_line(name: str, font: dict, *, align: int,
                margin_l: int, margin_r: int, margin_v: int,
                box: bool = False, scale_x: float = 100.0) -> str:
    """22 列 ASS V4+ Style（盒子约束语义：Margin 盒出区域，Alignment 定锚点）。"""
    size = max(8, min(200, int(float(font.get("size") or 48))))
    # 描边宽度 0 是合法值（不描边）——is not None 判断，避开 0 or 2 falsy 陷阱
    sw = font.get("stroke_width")
    stroke = max(0, min(10, int(float(sw))) ) if (sw is not None and sw != "") else 2
    bold = -1 if font.get("bold") else 0
    border = 4 if box else 1          # BorderStyle=4 = libass 背景盒（长视频口径）
    back = _ass_color(font.get("bg_color"), float(font.get("bg_opacity") or 0.5)) \
        if box else "&H80000000"
    # REQ-20261018-182：描边宽度 0 = 真不描边（此前 max(1,·) 钳到 1，
    # UI min=0 的承诺不生效；盒子模式仍强制 3）
    outline = 3 if box else (0 if stroke <= 0 else max(1, stroke))
    return (
        f"Style: {name},{_family_name(font.get('family'))},{size},"
        f"{_ass_color(font.get('color'))},&H00FFFFFF,"
        f"{_ass_color(font.get('stroke_color'))},{back},"
        f"{bold},0,0,0,{_fmt_scale(scale_x)},100,0,0,{border},{outline},0,"
        f"{align},{max(0, margin_l)},{max(0, margin_r)},{max(0, margin_v)},1"
    )


def build_ass_text(spec: "ComposeSpec") -> str:
    """三样式 ASS：字幕 Default（底缘贴 subtitle 区域底）+ 导师昵称/标题
    （顶左锚，Margin 盒 = mentor_text 区域，区域内自动换行）。
    """
    sub = _clamp_rect(spec.subtitle_rect)
    mt = _clamp_rect(spec.mentor_rect)
    font = spec.font or {}
    # REQ-20261018-202：异比例输出下文字保持正常比例 —— 设计画布（1080×1920）
    # 里烧录的字形先横向预压 ky/kx（Style ScaleX），尾部 scale={ow}:{oh} 的
    # 各向异性拉伸正好抵消，屏上字宽:字高 = 字体设计比例。折行容量按 kx/ky
    # 放宽。kx==ky（9:16 族 720p/1080p）ScaleX=100、factor=1 → ASS 串字节
    # 级不变。字幕 Default 的 libass 自动折行天然自适应（步进窄了每行多装
    # 字，屏幕满行占比不变），无需动 Margin 盒
    _kx = (spec.out_w / DESIGN_W) or 1.0
    _ky = (spec.out_h / DESIGN_H) or 1.0
    sx = 100.0 * _ky / _kx
    wfac = _kx / _ky
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {DESIGN_W}",
        f"PlayResY: {DESIGN_H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        _style_line(
            "Default", font,
            align=_ALIGN_MAP.get(str(font.get("align") or "center"), 2),
            margin_l=sub["x"],
            margin_r=DESIGN_W - (sub["x"] + sub["w"]),
            margin_v=DESIGN_H - (sub["y"] + sub["h"]),
            box=bool(font.get("bg_enabled")),
            scale_x=sx,
        ),
    ]
    mentor = spec.mentor or {}
    nick = str(mentor.get("nickname") or "").strip()
    title = str(mentor.get("title") or "").strip()
    if nick or title:
        nick_size = max(8, min(200, int(float(mentor.get("nickname_size") or 42))))
        # REQ-20261018-182：昵称/头衔 颜色 + 描边可选（enabled=False 或
        # 宽度 0 = 不描边；默认值即此前硬编码口径，老项目观感不变）
        lines.append(_style_line(
            "MentorNick",
            {"family": mentor.get("nickname_font"), "size": nick_size,
             "color": mentor.get("nickname_color") or "#FFFFFF",
             "stroke_color": mentor.get("nickname_outline_color") or "#000000",
             "stroke_width": _mentor_stroke(mentor, "nickname")},
            align=7, margin_l=mt["x"], margin_r=DESIGN_W - (mt["x"] + mt["w"]),
            margin_v=mt["y"],
            scale_x=sx,
        ))
    if title:
        nick_size = max(8, min(200, int(float(mentor.get("nickname_size") or 42))))
        lines.append(_style_line(
            "MentorTitle",
            {"family": mentor.get("title_font"), "size": mentor.get("title_size"),
             "color": mentor.get("title_color") or "#FFE9B8",
             "stroke_color": mentor.get("title_outline_color") or "#000000",
             "stroke_width": _mentor_stroke(mentor, "title")},
            align=7, margin_l=mt["x"], margin_r=DESIGN_W - (mt["x"] + mt["w"]),
            margin_v=mt["y"] + nick_size + 12,   # v1 一行估高：昵称下方
            scale_x=sx,
        ))
    # REQ-20261018-177：视频标题样式（spec.title 为空 dict = 不渲染）。
    # 文字里的换行保留为硬换行；超宽由 _wrap_title_lines 预折行（见下）
    tc = spec.title or {}
    title_main = str(tc.get("text") or "").strip()
    if title_main:
        lines.append(_title_style_line(tc, _clamp_rect(spec.title_rect),
                                       scale_x=sx))
    lines += ["", "[Events]",
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
              "MarginV, Effect, Text"]
    total = spec.expected_output_sec or 0.0
    for ev in spec.ass_events:
        text = _ass_escape(ev.get("text") or "")
        if not text:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_time(ev['start'])},{_ass_time(ev['end'])},"
            f"{ev.get('style') or 'Default'},,0,0,0,,{text}"
        )
    # REQ-20261018-181：导师文字/视频标题 = 「持续到结尾」的常驻层 —— 结束
    # 时间给 expected+1s 安全余量（烧录发生在主段上、流尾自然截断，多给不
    # 亏）；时长未知（probe 失败且无精剪段）时兜底 1 小时，保证文字绝不
    # 因时序估算偏短而在结束点前消失（用户实测反馈过）
    overlay_end = (total + 1.0) if total else 3600.0
    if nick:
        lines.append(
            f"Dialogue: 1,{_ass_time(0)},{_ass_time(overlay_end)},MentorNick,,0,0,0,,{_ass_escape(nick)}"
        )
    if title:
        # REQ-20261018-180：导师头衔多行（一个导师多个 title）——显式换行
        # 保留 + 超宽预折行（同视频标题 _wrap_title_lines 口径，CJK 长行
        # libass 不自动折）
        # REQ-20261018-195：头衔行间距（mentor.title_line_spacing）——
        # ≠0 时逐行独立 Dialogue（行级 MarginV，与样式 MarginV 同基准：
        # mt.y + 昵称字号 + 12）
        mentor_title_cfg = {"size": mentor.get("title_size")}
        mt_size = max(8, min(200, int(float(mentor.get("title_size") or 36))))
        # 与 MentorTitle 样式 MarginV 同基准（样式无条件用 昵称字号+12 偏移）
        mt_base = mt["y"] + nick_size + 12
        for mv, seg in _title_line_events(
                [_ass_escape(s) for s in _wrap_title_lines(
                    title, mt, mentor_title_cfg, width_factor=wfac)],
                mt_base, mt_size, _line_spacing(mentor, "title_line_spacing")):
            lines.append(
                f"Dialogue: 1,{_ass_time(0)},{_ass_time(overlay_end)},MentorTitle,,0,0,{mv},,{seg}"
            )
    if title_main:
        # REQ-20261018-179：超宽预折行（libass 不折无空格 CJK，见 _wrap_title_lines）
        # REQ-20261018-195：行间距（title.line_spacing）——≠0 时逐行独立
        # Dialogue（基准 = 标题区域 y）
        t_rect = _clamp_rect(spec.title_rect)
        t_size = max(8, min(200, int(float(tc.get("size") or 64))))
        for mv, seg in _title_line_events(
                [_ass_escape(s) for s in _wrap_title_lines(
                    title_main, t_rect, tc, width_factor=wfac)],
                t_rect["y"], t_size, _line_spacing(tc)):
            lines.append(
                f"Dialogue: 2,{_ass_time(0)},{_ass_time(overlay_end)},Title,,0,0,{mv},,{seg}"
            )
    return "\n".join(lines) + "\n"


def build_cover_title_ass(spec: "ComposeSpec") -> str:
    """REQ-20261018-185：封面段标题 ASS（单独烧在 [intro] 封面流上，
    时间轴 0..cover_dur——不与主段 ASS 混流，主段文字仍从封面结束后起）。

    样式复用 _title_style_line（顶锚 Margin 盒 = layout.cover_title 区域），
    文字与视频标题同源（该条卡片标题），超宽预折行同口径。
    """
    ct = spec.cover_title or {}
    text = str(ct.get("text") or "").strip()
    if not text or not (spec.cover_path and spec.cover_dur > 0):
        return ""
    rect = _clamp_rect(spec.cover_title_rect)
    # REQ-20261018-202：封面标题同主段文字口径（封面流过 REQ-200 预拉伸回
    # 设计画布后同样吃尾部各向异性拉伸，字形预压 ky/kx 抵消）
    _kx = (spec.out_w / DESIGN_W) or 1.0
    _ky = (spec.out_h / DESIGN_H) or 1.0
    sx = 100.0 * _ky / _kx
    wfac = _kx / _ky
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {DESIGN_W}",
        f"PlayResY: {DESIGN_H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        _title_style_line(ct, rect, name="CoverTitle", scale_x=sx),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text",
    ]
    # +0.5s 余量：封面 concat 边界处不提前消失（同 REQ-181 口径）
    # REQ-20261018-195：封面标题行间距（cover_title.line_spacing，语义同
    # 视频标题；基准 = 封面标题区域 y）
    ct_size = max(8, min(200, int(float(ct.get("size") or 64))))
    for mv, seg in _title_line_events(
            [_ass_escape(s) for s in _wrap_title_lines(text, rect, ct,
                                                       width_factor=wfac)],
            rect["y"], ct_size, _line_spacing(ct)):
        lines.append(
            "Dialogue: 0,%s,%s,CoverTitle,,0,0,%d,,%s" % (
                _ass_time(0.0), _ass_time(spec.cover_dur + 0.5), mv, seg)
        )
    return "\n".join(lines) + "\n"


# ---------- ComposeSpec：一次解析、预览/导出共用 ----------

@dataclass
class ComposeSpec:
    video_path: Path
    probe_sec: float | None                 # 源时长（None=未知 → -shortest 兜底）
    subtitle_src: Path | None = None        # 实际采用的字幕文件（连续 SRT 回写用）
    segments: list[tuple[float, float]] = field(default_factory=list)  # 精剪区间（导出）
    ass_events: list[dict] = field(default_factory=list)               # 已映射最终时间轴
    bg_path: Path | None = None
    bg_rect: dict = field(default_factory=lambda: {"x": 0, "y": 0, "w": DESIGN_W, "h": DESIGN_H})
    cover_path: Path | None = None
    cover_dur: float = 0.0
    avatar_path: Path | None = None
    avatar_shape: str = "circle"
    avatar_rect: dict = field(default_factory=lambda: {"x": 850, "y": 80, "w": 220, "h": 220})
    mentor: dict = field(default_factory=dict)
    mentor_rect: dict = field(default_factory=lambda: {"x": 60, "y": 100, "w": 740, "h": 240})
    # REQ-20261018-177：视频标题（params.title；空 dict = 不渲染标题）
    title: dict = field(default_factory=dict)
    title_rect: dict = field(default_factory=lambda: {"x": 90, "y": 380, "w": 900, "h": 340})
    # REQ-20261018-185：封面内标题（params.cover_title 样式 + 文字；
    # 空 dict = 封面段不叠标题。文字与视频标题同源（该条卡片标题））
    cover_title: dict = field(default_factory=dict)
    cover_title_rect: dict = field(default_factory=lambda: {"x": 90, "y": 380, "w": 900, "h": 340})
    font: dict = field(default_factory=dict)
    subtitle_rect: dict = field(default_factory=lambda: {"x": 60, "y": 1620, "w": 960, "h": 240})
    # video_rect：主画面放置区（设计空间）；video_crop：源裁剪窗（1920×1080
    # 源设计空间，REQ-20261018-174）——渲染时按比例换算成 iw/ih 表达式
    video_rect: dict = field(default_factory=lambda: {"x": 60, "y": 240, "w": 960, "h": 1080})
    video_crop: dict = field(default_factory=lambda: {"x": 0, "y": 0, "w": SRC_DESIGN_W, "h": SRC_DESIGN_H})
    bgm_path: Path | None = None
    volume_db: float = -6.0
    fade_in: float = 0.5
    fade_out: float = 1.0
    out_w: int = 720
    out_h: int = 1280
    preview_start: float = 0.0              # 仅预览：输入级 -ss（秒）
    preview_duration: float | None = None   # 仅预览：窗口秒；导出 None

    @property
    def is_preview(self) -> bool:
        return self.preview_duration is not None

    @property
    def video_len(self) -> float | None:
        """主画面（不含封面）时长；probe 未知 → None。"""
        if self.is_preview:
            return float(self.preview_duration or 0.0)
        if self.segments:
            return sum(e - s for s, e in self.segments)
        return self.probe_sec

    @property
    def expected_output_sec(self) -> float | None:
        v = self.video_len
        if v is None:
            return None
        return v + (self.cover_dur if self.cover_path and self.cover_dur > 0 else 0.0)


def build_compose_command(spec: ComposeSpec, output_path: Path,
                          ass_path: Path | None,
                          cover_ass_path: Path | None = None) -> dict:
    """组装 ffmpeg 命令（不执行）。返回
    {cmd, input_args, filter_complex, expected_output_sec, out_w, out_h}。

    cover_ass_path：REQ-20261018-185 封面段标题 ASS（烧在封面 [intro] 流上）。
    """
    input_args: list[str] = []
    chain: list[str] = []
    idx = 0

    # ---- 输入 0：主视频（预览输入级 -ss/-t；导出无 -ss 保 trim 精度）----
    if spec.is_preview:
        input_args += ["-ss", f"{spec.preview_start:.3f}",
                       "-t", f"{float(spec.preview_duration):.3f}"]
    input_args += ["-i", str(spec.video_path)]
    video_idx = idx
    idx += 1

    expected = spec.expected_output_sec
    cover_on = bool(spec.cover_path) and spec.cover_dur > 0
    cover_dur = spec.cover_dur if cover_on else 0.0
    # 画布/循环图只承载主画面段（+0.5 余量），封面前缀单独 concat
    canvas_bound = (expected - cover_dur + 0.5) if expected is not None else None

    def _finite() -> str:
        return f":duration={canvas_bound:.3f}" if canvas_bound else ""

    # ---- L0 黑底画布（RGBA 背景图透明区防白框）----
    chain.append(
        f"color=size={DESIGN_W}x{DESIGN_H}:color=black:rate=30{_finite()}[bgb]"
    )

    # ---- L1 背景图 → bg 区域 ----
    cur = "[bgb]"
    if spec.bg_path and Path(spec.bg_path).is_file():
        input_args += ["-loop", "1"]
        if canvas_bound:
            input_args += ["-t", f"{canvas_bound:.3f}"]
        input_args += ["-i", str(spec.bg_path)]
        bg_idx = idx
        idx += 1
        bg_rect = _clamp_rect(spec.bg_rect)
        # REQ-20261018-192：背景图铺满 bg 区域（拉伸填充）——与视频区域
        # REQ-189 同语义，也与 bg_detect 的坐标系一致（检测把图拉满 270×480
        # 再 ×4，本就假设 bg 铺满画布）。旧 contain 居中在非 9:16 背景图
        # （如 3:4 960×1280）下双重变形：design 画布上下留黑边，尾部整体
        # 拉伸后背景压扁、画布上画的槽位错位——视频区域坐标怎么调都对不上
        bx, by = int(bg_rect["x"]), int(bg_rect["y"])
        bw, bh = int(bg_rect["w"]), int(bg_rect["h"])
        chain.append(f"[{bg_idx}:v]scale={bw}:{bh}:flags=lanczos,setsar=1[bgimg]")
        chain.append(f"{cur}[bgimg]overlay=x={bx}:y={by}[bg]")
        cur = "[bg]"

    # ---- L2 主视频：导出先按 SRT 段去缝，再源裁剪 + 铺满 video 区域（REQ-189） ----
    vsrc = f"[{video_idx}:v]"
    if not spec.is_preview and len(spec.segments) >= 1:
        n = len(spec.segments)
        if n == 1:
            s, e = spec.segments[0]
            chain.append(f"{vsrc}trim=start={s:.3f}:end={e:.3f},"
                         f"setpts=PTS-STARTPTS[cutv]")
        else:
            labels = "".join(f"[sv{i}]" for i in range(n))
            chain.append(f"{vsrc}split={n}{labels}")
            cuts = []
            for i, (s, e) in enumerate(spec.segments):
                chain.append(f"[sv{i}]trim=start={s:.3f}:end={e:.3f},"
                             f"setpts=PTS-STARTPTS[cv{i}]")
                cuts.append(f"[cv{i}]")
            chain.append("".join(cuts) + f"concat=n={n}:v=1:a=0[cutv]")
        vsrc = "[cutv]"
    vc = spec.video_crop
    video_rect = _clamp_rect(spec.video_rect)
    # REQ-20261018-189：视频区域语义 =「截取的视频自动适应区域的宽度和高度」
    # —— 直接铺满区域矩形（适配方式选择已移除，不再按源宽高比 contain 居中）
    # REQ-20261018-207：自动适配宽高 —— 用户裁剪矩形先取下，再按「区域映射
    # 到**输出帧**后的屏幕宽高比」中心 cover 二次裁剪（表达式内联 iw/ih，
    # 源分辨率无关；3:4 输出下区域屏幕比 = 设计比×kx/ky），最后铺满区域。
    # 屏上视频永不变形、永不出杆；裁剪比已等于屏幕比（9:16 族 + REQ-198
    # 区域锁）时二次裁剪恒等（min 两侧相等）
    vx, vy = int(video_rect["x"]), int(video_rect["y"])
    vw, vh = int(video_rect["w"]), int(video_rect["h"])
    _kxv = (spec.out_w / DESIGN_W) or 1.0
    _kyv = (spec.out_h / DESIGN_H) or 1.0
    r_out = (vw * _kxv) / (vh * _kyv)
    _rs = f"{r_out:.6f}"
    chain.append(
        f"{vsrc}crop=iw*{int(vc['w'])}/{SRC_DESIGN_W}:ih*{int(vc['h'])}/{SRC_DESIGN_H}:"
        f"iw*{int(vc['x'])}/{SRC_DESIGN_W}:ih*{int(vc['y'])}/{SRC_DESIGN_H},"
        f"crop=w='min(iw\\,ih*{_rs})':h='min(ih\\,iw/{_rs})':"
        f"x='(iw-ow)/2':y='(ih-oh)/2',"
        f"scale=vwPLACE:vhPLACE:flags=lanczos,setsar=1[vfit]"
    )
    chain[-1] = chain[-1].replace("vwPLACE", str(vw)).replace("vhPLACE", str(vh))
    chain.append(f"{cur}[vfit]overlay=x={vx}:y={vy}[v1]")
    cur = "[v1]"

    # ---- L3 导师头像（circle → geq 圆形 alpha 掩码）----
    if spec.avatar_path and Path(spec.avatar_path).is_file():
        input_args += ["-loop", "1"]
        if canvas_bound:
            input_args += ["-t", f"{canvas_bound:.3f}"]
        input_args += ["-i", str(spec.avatar_path)]
        av_idx = idx
        idx += 1
        ar = _clamp_rect(spec.avatar_rect)
        if str(spec.avatar_shape or "circle") == "circle":
            # REQ-20261018-178：圆形头像必须是正圆 —— 以区域短边为直径裁出
            # 正方形（cover 缩放不拉伸、区域内居中）再套圆掩码；直接拉到
            # w×h 会在非正方形区域画出扁圆
            # REQ-20261018-201：正圆还要落在**输出帧**上 —— 尾部会把设计
            # 画布各向异性拉伸到输出帧（3:4 ×0.889/×0.667），设计空间的
            # 正圆被拉成横长椭圆（用户实测）。改为按映射到输出帧后的区域
            # 短边定直径，图像与掩码按 (1/kx,1/ky) 预补偿；kx=ky（9:16 族
            # 720p/1080p）时与旧链恒等
            kx = spec.out_w / DESIGN_W
            ky = spec.out_h / DESIGN_H
            d_out = min(ar["w"] * kx, ar["h"] * ky)
            dw = max(1, round(d_out / kx))
            dh = max(1, round(d_out / ky))
            ax = ar["x"] + (ar["w"] - dw) // 2
            ay = ar["y"] + (ar["h"] - dh) // 2
            # REQ-20261018-208：内容比例不变形 —— 先从源采「正方形」（cover
            # 到 S=max(dw,dh) 居中裁方，采样内容无压缩），再各向异性
            # scale=dw:dh 预补偿。净效果：源内容均匀缩放 d_out/S（无拉伸），
            # 掩码椭圆照旧 → 输出正圆 + 原比例内容。旧链 cover 到 dw×dh
            # 直接裁非方区域，正方形源图被取竖条 → 屏上内容横胖 1.33×
            # （用户实测「头像特别胖」）。kx==ky 时 dw==dh==S 第三跳恒等
            sq = max(dw, dh)
            chain.append(
                f"[{av_idx}:v]scale={sq}:{sq}:"
                f"force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={sq}:{sq},"
                f"scale={dw}:{dh}:flags=lanczos,setsar=1[avbase]"
            )
            # 实测可用（ffmpeg 9.0.1）：表达式内逗号必须转义。
            # 掩码 = 设计空间的椭圆：坐标乘 kx/ky 归一到输出帧后判圆，
            # 尾部拉伸后恰为输出帧上的正圆；kx=ky=1 时退化为旧圆公式
            kxs = f"{kx:.6f}"
            kys = f"{ky:.6f}"
            ds = f"{d_out / 2:.6f}"
            chain.append(
                "[avbase]format=rgba,"
                "geq=r='r(X\\,Y)':g='g(X\\,Y)':b='b(X\\,Y)':"
                "a='if(lte(pow((X-W/2)*" + kxs + "\\,2)+pow((Y-H/2)*" + kys
                + "\\,2)\\,pow(" + ds + "\\,2))\\,255\\,0)'[av]"
            )
        else:
            ax, ay = ar["x"], ar["y"]
            chain.append(f"[{av_idx}:v]scale={ar['w']}:{ar['h']}:flags=lanczos,setsar=1[avbase]")
            chain.append("[avbase]anull[av]")
        chain.append(f"{cur}[av]overlay=x={ax}:y={ay}[v2]")
        cur = "[v2]"

    # ---- L4 文字单层烧录（字幕 + 导师文字共用一个 ass 文件）----
    # REQ-20261018-195：ass_path 存在即烧录（调用方已保证文件里有对白；
    # 此前 `and spec.ass_events` 会把「只有标题/导师文字」的 ASS 整份跳过）
    if ass_path is not None:
        ass_arg = f"ass=filename='{_filter_path(ass_path)}'"
        _fd = _ensure_font_dir()
        if _fd is not None:
            ass_arg += f":fontsdir='{_filter_path(_fd)}'"
        chain.append(f"{cur}{ass_arg}[v3]")
        cur = "[v3]"

    # ---- A1/A2 音频 ----
    if spec.is_preview:
        chain.append(f"[{video_idx}:a]asetpts=PTS-STARTPTS[a0]")
        asrc = "[a0]"
    else:
        if len(spec.segments) == 1:
            s, e = spec.segments[0]
            chain.append(f"[{video_idx}:a]atrim=start={s:.3f}:end={e:.3f},"
                         f"asetpts=PTS-STARTPTS[a0]")
            asrc = "[a0]"
        elif len(spec.segments) > 1:
            n = len(spec.segments)
            labels = "".join(f"[sa{i}]" for i in range(n))
            chain.append(f"[{video_idx}:a]asplit={n}{labels}")
            cuts = []
            for i, (s, e) in enumerate(spec.segments):
                chain.append(f"[sa{i}]atrim=start={s:.3f}:end={e:.3f},"
                             f"asetpts=PTS-STARTPTS[ca{i}]")
                cuts.append(f"[ca{i}]")
            chain.append("".join(cuts) + f"concat=n={n}:v=0:a=1[cuta]")
            asrc = "[cuta]"
        else:
            chain.append(f"[{video_idx}:a]asetpts=PTS-STARTPTS[a0]")
            asrc = "[a0]"
    # REQ-20261018-181：封面进预览后，静音前缀对预览/导出统一补
    # （封面段无声，主段音频在封面结束后才起播 —— 音画不错位）
    aformat = ("aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo")
    if cover_on:
        input_args += ["-f", "lavfi", "-t", f"{cover_dur:.3f}",
                       "-i", "anullsrc=r=48000:cl=stereo"]
        sil_idx = idx
        idx += 1
        chain.append(f"[{sil_idx}:a]{aformat}[ssil]")
        chain.append(f"{asrc}{aformat}[vraw]")
        chain.append("[ssil][vraw]concat=n=2:v=0:a=1[voice]")
    else:
        chain.append(f"{asrc}{aformat}[voice]")

    # ---- L5 封面片头 concat ----
    if cover_on:
        input_args += ["-loop", "1", "-framerate", "30",
                       "-t", f"{cover_dur:.3f}", "-i", str(spec.cover_path)]
        cover_idx = idx
        idx += 1
        # REQ-20261018-185：封面段标题烧在封面流上（时间轴 0..cover_dur，
        # 与主段文字（封面结束后起）互不干扰）
        intro_ass = ""
        if cover_ass_path is not None:
            intro_ass = f",ass=filename='{_filter_path(cover_ass_path)}'"
            _fd = _ensure_font_dir()
            if _fd is not None:
                intro_ass += f":fontsdir='{_filter_path(_fd)}'"
        # REQ-20261018-200：封面先 contain 进**输出帧**再预拉伸回设计画布。
        # 此前 contain 进设计画布（9:16）后被尾部各向异性拉伸 —— 3:4 输出下
        # 3:4 封面 net 变正方形（横向拉宽 1.33×，用户实测变形）。改后尾部
        # 变换正好抵消预拉伸：net = 封面无变形 contain 进输出帧；封面比 ==
        # 输出比时零黑边整幅铺满；out==design（1080p）时第二段 scale 恒等。
        ow, oh = spec.out_w, spec.out_h
        cover_pre = (
            f"scale={ow}:{oh}:force_original_aspect_ratio=decrease,"
            f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
        if (ow, oh) != (DESIGN_W, DESIGN_H):
            cover_pre += f",scale={DESIGN_W}:{DESIGN_H}"
        chain.append(
            f"[{cover_idx}:v]{cover_pre},"
            f"setpts=PTS-STARTPTS,fps=30,setsar=1{intro_ass}[intro]"
        )
        chain.append(f"[intro]{cur}concat=n=2:v=1:a=0[v4]")
        cur = "[v4]"

    # ---- A3 BGM ----
    # REQ-20261018-181：预览/导出统一走 [voice]（预览含封面时带静音前缀）
    voice = "[voice]"
    if spec.bgm_path and Path(spec.bgm_path).is_file():
        input_args += ["-stream_loop", "-1"]
        if expected is not None:
            input_args += ["-t", f"{expected + 0.5:.3f}"]
        input_args += ["-i", str(spec.bgm_path)]
        bgm_idx = idx
        idx += 1
        bgm_chain = f"[{bgm_idx}:a]volume={float(spec.volume_db):.1f}dB"
        if spec.fade_in > 0:
            bgm_chain += f",afade=t=in:st=0:d={spec.fade_in:.3f}"
        if expected is not None and spec.fade_out > 0:
            # fade_out 起点必须含封面前缀（长视频实战坑：错算整段静音）
            bgm_chain += f",afade=t=out:st={max(0.0, expected - spec.fade_out):.3f}:d={spec.fade_out:.3f}"
        chain.append(bgm_chain + "[bgm]")
        chain.append(f"{voice}[bgm]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        chain.append(f"{voice}anull[aout]")

    # ---- L6 尾部分辨率 ----
    # REQ-20261018-190：异比例目标整体拉伸铺满（用户确认，取代 REQ-187 的
    # contain + pad 黑边）——设计空间 1080×1920 宽高各自拉到目标分辨率：
    # 无黑边、不裁内容；同比例目标（720p/1080p 竖屏）等比缩放无变形，
    # 异比例目标（16:9 横屏、3:4）画面宽高比有变形
    final_map = cur
    if (spec.out_w, spec.out_h) != (DESIGN_W, DESIGN_H):
        chain.append(
            f"{cur}scale={spec.out_w}:{spec.out_h}:flags=lanczos,"
            f"setsar=1[vfinal]"
        )
        final_map = "[vfinal]"

    # 硬件编码（hw_encode 探测：nvenc→qsv→libx264 自动降级，无显卡机器
    # 走原 libx264；quality 22 保持本阶段原 crf 22 口径）
    _venc_args, _ = hw_encode.encoder_args(quality=22)
    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", ";\n".join(chain),
        "-map", final_map,
        "-map", "[aout]",
        *_venc_args,
        "-c:a", "aac", "-b:a", "160k",
    ]
    if expected is not None:
        cmd += ["-t", f"{expected + 0.05:.3f}"]      # 确定性上界（非 -shortest）
    else:
        cmd += ["-shortest"]
    cmd += ["-movflags", "+faststart", str(output_path)]
    return {
        "cmd": cmd,
        "input_args": input_args,
        "filter_complex": ";\n".join(chain),
        "expected_output_sec": expected,
        "out_w": spec.out_w,
        "out_h": spec.out_h,
    }


# ---------- 编排层（跑 ffmpeg、写文件） ----------

def _run_ffmpeg(cmd: list[str], timeout: int = 1800) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise svc.ShortVideoError(f"ffmpeg 渲染超时（>{timeout}s）") from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1200:]
        raise svc.ShortVideoError(f"ffmpeg 渲染失败：{tail}")


def _atomic_render(spec: ComposeSpec, out_path: Path, work_dir: Path) -> float:
    """写 ass → 渲染到 _work 临时文件 → os.replace 原子落位。返回 ffprobe 时长。"""
    from slirn_home import short_video_fine as fine

    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path: Path | None = None
    # REQ-20261018-195：只要有对白就落 ASS——此前仅看 ass_events（字幕），
    # 字幕区域关掉后（无事件）标题/导师文字的对白也一起被跳过、静默消失
    ass_text = build_ass_text(spec)
    if "Dialogue:" in ass_text:
        ass_path = work_dir / f"compose_{int(time.time() * 1000) % 100000}.ass"
        ass_path.write_text(ass_text, encoding="utf-8")
    # REQ-20261018-185：封面段标题 ASS（独立文件，烧在封面流上）
    cover_ass_path: Path | None = None
    cover_ass_text = build_cover_title_ass(spec)
    if cover_ass_text:
        cover_ass_path = work_dir / f"cover_{int(time.time() * 1000) % 100000}.ass"
        cover_ass_path.write_text(cover_ass_text, encoding="utf-8")
    tmp = work_dir / (out_path.name + ".tmp.mp4")
    try:
        built = build_compose_command(spec, tmp, ass_path, cover_ass_path)
        log.info("[sv-compose] %s", " ".join(built["cmd"])[:2000])
        _run_ffmpeg(built["cmd"])
        if not tmp.is_file() or tmp.stat().st_size < 1024:
            raise svc.ShortVideoError("ffmpeg 渲染产物缺失/过小")
        os.replace(tmp, out_path)
    finally:
        for _p in (ass_path, cover_ass_path):
            if _p is not None:
                try:
                    _p.unlink(missing_ok=True)
                except OSError:
                    pass
    return float(_ffprobe_duration_fallback(out_path) or 0.0)


def _ffprobe_duration_fallback(path: Path) -> float | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(proc.stdout.strip()) if proc.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def resolve_compose_spec(root: Path, project: dict, index: int, *,
                         preview: bool,
                         start_ms: int = 0,
                         duration_sec: int | None = None) -> ComposeSpec:
    """项目 + 参数 → ComposeSpec（预览/导出共用解析）。"""
    from slirn_home import short_video_fine as fine

    params = fine._get_params(project)
    layout = params.get("layout") or {}

    src = fine.resolved_clip_sources(root, project, int(index))
    video_path = src["video_abs"]
    probe_sec = _ffprobe_duration_fallback(video_path)

    mats = fine.resolved_clip_materials(root, project, int(index))
    # REQ-20261018-184：区域未勾选（enabled=False）→ 不使用该区域的内容
    # （bg/avatar/mentor_text/subtitle；cover/title 此前已门控。视频区域
    # 恒用——它是内容本体，勾选框对它无意义）
    bg_abs = (mats["bg_abs"]
              if (mats.get("bg_abs") and Path(mats["bg_abs"]).is_file()
                   and (layout.get("bg") or {}).get("enabled", True)) else None)
    audio_abs = mats["audio_abs"] if mats.get("audio_abs") and Path(mats["audio_abs"]).is_file() else None

    pm = params.get("materials") or {}
    cover_cfg = layout.get("cover") or {}
    cover_enabled = bool(cover_cfg.get("enabled"))
    cover_abs = None
    # REQ-20261018-181：封面进预览也进导出（用户口径：封面在视频开播前
    # 单独展示一段、只显示封面信息；其余素材从封面结束后起算）。预览音频
    # 同样补 anullsrc 静音前缀，含封面不再音画错位
    if cover_enabled and pm.get("cover"):
        cand = svc._abs(root, str(pm["cover"]))
        cover_abs = cand if cand.is_file() else None
    try:
        cover_dur = max(0.0, min(10.0, float(cover_cfg.get("duration") or 0.0)))
    except (TypeError, ValueError):
        cover_dur = 0.0

    avatar_abs = None
    # REQ-20261018-184：导师头像区域未勾选 → 不叠加头像
    if pm.get("avatar") and (layout.get("avatar") or {}).get("enabled", True):
        cand = svc._abs(root, str(pm["avatar"]))
        avatar_abs = cand if cand.is_file() else None

    # BGM 优先级：per-clip audio 覆盖 > audio.bgm_path（系统库）> 公共 materials.audio
    audio_cfg = params.get("audio") or {}
    bgm_abs = audio_abs
    if not bgm_abs and audio_cfg.get("enabled") and audio_cfg.get("bgm_path"):
        cand = Path(str(audio_cfg["bgm_path"]))
        bgm_abs = cand if cand.is_file() else None
    volume_db = -6.0
    fade_in = 0.5
    fade_out = 1.0
    if audio_cfg.get("enabled"):
        try:
            volume_db = max(-40.0, min(0.0, float(audio_cfg.get("volume_db") or -6)))
        except (TypeError, ValueError):
            pass
        try:
            fade_in = max(0.0, min(5.0, float(audio_cfg.get("fade_in_ms") or 500) / 1000.0))
            fade_out = max(0.0, min(5.0, float(audio_cfg.get("fade_out_ms") or 1000) / 1000.0))
        except (TypeError, ValueError):
            pass

    # REQ-20261018-179（修订二）：标题文字**只**来自「精剪混编」区该条卡片
    # 顶部的标题（每条各自，与 UI 输入框同源 = src["title"]：per-clip 覆盖
    # stage6_fine.clips["N"].title → stage5_coarse 标题兜底）；参数区不再
    # 统一设置文字，遗留 params.title.text 一律忽略
    tcfg = dict(params.get("title") or {})
    tcfg["text"] = str(src.get("title") or "").strip()

    spec = ComposeSpec(
        video_path=video_path,
        probe_sec=probe_sec,
        bg_path=bg_abs,
        bg_rect=_rect(layout, "bg"),
        cover_path=cover_abs,
        cover_dur=cover_dur if cover_abs else 0.0,
        avatar_path=avatar_abs,
        avatar_shape=str(layout.get("avatar_shape") or "circle"),
        avatar_rect=_rect(layout, "avatar"),
        # REQ-20261018-184：导师文字区域未勾选 → 整组不渲染（空 dict）
        mentor=((params.get("mentor") or {})
                if (layout.get("mentor_text") or {}).get("enabled", True) else {}),
        mentor_rect=_rect(layout, "mentor_text"),
        # REQ-20261018-177：标题（空文字或区域 enabled=False → 空 dict 不渲染）
        title=(
            tcfg
            if str(tcfg.get("text") or "").strip()
            and (layout.get("title") or {}).get("enabled", True)
            else {}
        ),
        title_rect=_rect(layout, "title"),
        # REQ-20261018-185：封面内标题（样式独立于视频标题；文字同源该条
        # 卡片标题；区域未勾选或无封面 → 空 dict 不渲染）
        cover_title=(
            {**dict(params.get("cover_title") or {}),
             "text": str(src.get("title") or "").strip()}
            if (cover_abs and cover_dur > 0
                and str(src.get("title") or "").strip()
                and (layout.get("cover_title") or {}).get("enabled", True))
            else {}
        ),
        cover_title_rect=_rect(layout, "cover_title"),
        font=params.get("font") or {},
        subtitle_rect=_rect(layout, "subtitle"),
        bgm_path=bgm_abs,
        volume_db=volume_db,
        fade_in=fade_in,
        fade_out=fade_out,
        out_w=720, out_h=1280,
        preview_start=max(0.0, int(start_ms) / 1000.0),
        preview_duration=(float(duration_sec) if preview else None),
    )
    spec.out_w, spec.out_h = _output_size((params.get("output") or {}).get("resolution"), bg_abs)
    spec.video_crop = {
        "x": int((layout.get("video") or {}).get("crop_x") or 0),
        "y": int((layout.get("video") or {}).get("crop_y") or 0),
        "w": max(1, int((layout.get("video") or {}).get("crop_w") or SRC_DESIGN_W)),
        "h": max(1, int((layout.get("video") or {}).get("crop_h") or SRC_DESIGN_H)),
    }
    spec.video_rect = _rect(layout, "video")

    # 字幕事件映射。REQ-20261018-184：字幕区域未勾选 → 只跳过**烧录**
    # （ass_events 不加），导出的精剪切分（segments）仍按字幕时间轴算——
    # 关的是「显示」，不是「剪辑区间」
    spec.subtitle_src = src["subtitle_abs"] if src["subtitle_abs"].is_file() else None
    entries = _parse_srt(src["subtitle_abs"]) if spec.subtitle_src else []
    sub_on = (layout.get("subtitle") or {}).get("enabled", True)
    offset = float(params.get("font", {}).get("offset_ms") or 0) / 1000.0
    if preview:
        win_s = spec.preview_start
        for e in entries:
            s = e["start"] + offset - win_s
            t = e["end"] + offset - win_s
            if t <= 0 or s >= float(duration_sec or 0):
                continue
            if sub_on:
                spec.ass_events.append({
                    "start": max(0.0, s), "end": max(0.1, t),
                    "text": e["text"], "style": "Default",
                })
    else:
        segs, prefix = map_segments(entries, probe_sec)
        spec.segments = segs
        for e in entries:
            s = remap_srt_time(e["start"] + offset, segs, prefix)
            t = remap_srt_time(e["end"] + offset, segs, prefix)
            if t > s + 0.01 and sub_on:
                spec.ass_events.append({
                    "start": s, "end": t, "text": e["text"], "style": "Default",
                })
    return spec


def compose_preview_one(repo_root: Path | str, task_id: str, project_id: str,
                        index: int, *, start_ms: int = 0,
                        duration_sec: int = 5) -> dict:
    """预览一条（同步 ffmpeg，落 stage6/preview_NN.mp4）。

    REQ-20261018-176 用户口径：先按背景图说明检测各区域，再混编 ——
    ensure_layout_detected 在这里先行（cached/cooldown 逻辑防拖慢）。
    """
    ensure_layout_detected(repo_root, task_id, project_id)
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    spec = resolve_compose_spec(root, project, int(index), preview=True,
                                start_ms=start_ms, duration_sec=int(duration_sec))
    if not spec.video_path.is_file():
        raise svc.ShortVideoError(f"源视频不存在: {spec.video_path}")
    out = svc.stage6_dir(root, task_id, project_id) / f"preview_{int(index):02d}.mp4"
    dur = _atomic_render(spec, out, svc.stage6_dir(root, task_id, project_id) / "_work")
    return {"preview_path": str(out), "size": out.stat().st_size,
            "duration_sec": round(dur or float(duration_sec), 1),
            "index": int(index)}


def export_cover_image_one(repo_root: Path | str, task_id: str, project_id: str,
                           index: int) -> dict:
    """REQ-20261019-233：导出「合成标题后」的封面静态图（stage6/cover_NN.png）。

    与成片封面段同一套解析（``resolve_compose_spec``）+ 同一份封面标题
    ASS（``build_cover_title_ass``），滤镜链复刻 [intro] 流的封面处理
    （REQ-200 contain → 预拉伸回设计画布 → 烧字），再补一次回拉输出帧 ——
    静态图 net 效果与成片首帧一致：封面无变形 contain 进输出帧、标题
    字形不畸变（ScaleX 预压被回拉抵消）。单帧渲染，同步返回。
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    # preview=True 免主视频 probe/segments 映射（封面图只吃封面素材+标题+参数）
    spec = resolve_compose_spec(root, project, int(index), preview=True,
                                duration_sec=1)
    if not spec.cover_path:
        raise svc.ShortVideoError("未配置封面素材（或封面区域未勾选）")
    cover_ass_text = build_cover_title_ass(spec)
    if not cover_ass_text:
        raise svc.ShortVideoError(
            "封面标题为空（该条卡片标题为空或封面标题区域未勾选）")

    stage6 = svc.stage6_dir(root, task_id, project_id)
    work_dir = stage6 / "_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path = stage6 / f"cover_{int(index):02d}.png"
    ass_path = work_dir / f"coverimg_{int(time.time() * 1000) % 100000}.ass"
    ow, oh = spec.out_w, spec.out_h
    # REQ-200 同款封面链：contain 进输出帧 + 黑边 pad；out≠design 时先预拉
    # 伸到设计画布（ASS PlayRes = 设计坐标系）烧字，烧完再回拉输出帧
    stretch = (ow, oh) != (DESIGN_W, DESIGN_H)
    vf = (f"scale={ow}:{oh}:force_original_aspect_ratio=decrease,"
          f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:color=black")
    if stretch:
        vf += f",scale={DESIGN_W}:{DESIGN_H}"
    tmp = work_dir / (out_path.name + ".tmp.png")
    try:
        ass_path.write_text(cover_ass_text, encoding="utf-8")
        vf += f",ass=filename='{_filter_path(ass_path)}'"
        if _FONT_DIR.is_dir():
            vf += f":fontsdir='{_filter_path(_FONT_DIR)}'"
        if stretch:
            vf += f",scale={ow}:{oh}"
        cmd = ["ffmpeg", "-y", "-i", str(spec.cover_path),
               "-vf", vf, "-frames:v", "1", str(tmp)]
        log.info("[sv-compose] cover export #%s: %s", index, " ".join(cmd)[:1200])
        _run_ffmpeg(cmd, timeout=120)
        if not tmp.is_file() or tmp.stat().st_size < 1024:
            raise svc.ShortVideoError("封面图渲染产物缺失/过小")
        os.replace(tmp, out_path)
    finally:
        try:
            ass_path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"cover_path": str(out_path), "size": out_path.stat().st_size,
            "index": int(index)}


def compose_export_one(repo_root: Path | str, task_id: str, project_id: str,
                       index: int, *, coarse_mp4: Path | str = "",
                       refined_srt: Path | str = "") -> "ComposeResult":
    """导出一条：滤镜图内精剪 + 全要素混编，产 final_NN.mp4 + final_NN.srt。

    kwargs 刻意对齐 short_video_finalize.run_stage6_one（显式路径注入点，
    测试 monkeypatch 只换模块名）。
    """
    root = Path(repo_root)
    coarse = Path(coarse_mp4).resolve() if coarse_mp4 else Path()
    refined = Path(refined_srt).resolve() if refined_srt else Path()
    if coarse and not coarse.is_file():
        return ComposeResult(index=index, final_mp4=Path(), final_srt=Path(),
                             status="failed", error=f"粗剪 mp4 不存在: {coarse}")
    if refined and not refined.is_file():
        return ComposeResult(index=index, final_mp4=Path(), final_srt=Path(),
                             status="failed", error=f"精剪字幕不存在: {refined}")
    project = svc.load_project(root, task_id, project_id)
    spec = resolve_compose_spec(root, project, int(index), preview=False)
    # kwargs 显式注入的 coarse/refined 优先（run_fine_one 传真实值、测试
    # monkeypatch 走这里）；否则用 resolve 阶段解析到的继承源
    srt_src = refined if refined.is_file() else (spec.subtitle_src or Path())
    if coarse:
        spec.video_path = coarse
        spec.probe_sec = _ffprobe_duration_fallback(coarse)
    if srt_src.is_file():
        entries = _parse_srt(srt_src)
        segs, prefix = map_segments(entries, spec.probe_sec)
        spec.segments = segs
        offset = float((spec.font or {}).get("offset_ms") or 0) / 1000.0
        spec.ass_events = [
            {"start": s, "end": t, "text": e["text"], "style": "Default"}
            for e in entries
            for s, t in [(remap_srt_time(e["start"] + offset, segs, prefix),
                          remap_srt_time(e["end"] + offset, segs, prefix))]
            if t > s + 0.01
        ]

    final_mp4, final_srt = svc.stage6_paths(root, task_id, project_id, int(index))
    final_mp4 = final_mp4.resolve()
    final_srt = final_srt.resolve()
    t0 = time.time()
    try:
        duration = _atomic_render(spec, final_mp4,
                                  final_mp4.parent / "_work")
    except svc.ShortVideoError as e:
        return ComposeResult(index=index, final_mp4=final_mp4, final_srt=final_srt,
                             status="failed", error=str(e)[:500],
                             elapsed_sec=int(time.time() - t0))
    _write_continuous_srt(srt_src, final_srt, spec, duration)
    return ComposeResult(index=index, final_mp4=final_mp4, final_srt=final_srt,
                         status="done", duration_sec=duration,
                         elapsed_sec=int(time.time() - t0))


@dataclass
class ComposeResult:
    """与 short_video_finalize.Stage6HighlightResult 字段同形（duck-typing 兼容）。"""
    index: int
    final_mp4: Path
    final_srt: Path
    status: str
    duration_sec: float = 0.0
    error: str = ""
    elapsed_sec: int = 0


def _write_continuous_srt(src_srt: Path, out_srt: Path, spec: ComposeSpec,
                          duration_sec: float) -> None:
    """连续时间轴 SRT：字幕时间经 remap_srt_time 精确映射到去缝时间轴。"""
    entries = _parse_srt(src_srt)
    prefix = _prefix_of(spec)
    rows: list[str] = []
    for i, e in enumerate(entries, 1):
        s = remap_srt_time(e["start"], spec.segments, prefix)
        t = remap_srt_time(e["end"], spec.segments, prefix)
        if duration_sec:
            s = min(s, duration_sec)
            t = min(t, duration_sec)
        if t <= s:
            continue
        rows.append(f"{i}\n{_srt_ts(s)} --> {_srt_ts(t)}\n{e['text']}\n")
    out_srt.write_text("\n".join(rows), encoding="utf-8")


def _prefix_of(spec: ComposeSpec) -> list[float]:
    prefix: list[float] = []
    acc = 0.0
    for s, e in spec.segments:
        prefix.append(acc)
        acc += e - s
    return prefix


def _srt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        ms -= 1000
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------- 检测先行流（REQ-20261018-176：预览/导出前按 bg_desc 检测区域） ----------

def _parse_dt(raw: str) -> float | None:
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None


def ensure_layout_detected(repo_root: Path | str, task_id: str,
                           project_id: str) -> dict:
    """渲染前确保区域参数新鲜：

    - bg_desc 为空 → 不检测（no_desc）
    - ai_layout_result.desc == 当前 desc 且 source==llm → 缓存命中（cached）
    - fallback 结果在 10 分钟冷却期内 → 不重试（cooldown）
    - 需要跑 → run_ai_layout；source==llm 才 apply_detected_to_layout
      （fallback 不覆盖手动参数：先快照 layout，失败后恢复落盘）
    """
    root = Path(repo_root)
    from slirn_home import short_video_ai_layout as ai_layout
    from slirn_home import short_video_bg_detect as bg_detect
    from slirn_home import short_video_fine as fine

    project = svc.load_project(root, task_id, project_id)
    params = fine._get_params(project)
    desc = str((params.get("materials") or {}).get("bg_desc") or "").strip()
    if not desc:
        return {"ran": False, "reason": "no_desc"}

    cached = params.get("ai_layout_result") or {}
    if isinstance(cached, dict) and str(cached.get("desc") or "") == desc:
        if cached.get("source") == "llm":
            return {"ran": False, "reason": "cached", "source": "llm",
                    "regions": len(cached.get("regions") or [])}
        ts = _parse_dt(cached.get("detected_at") or "")
        if ts is not None and time.time() - ts < _DETECT_RETRY_COOLDOWN_SEC:
            return {"ran": False, "reason": "cooldown", "source": "fallback"}

    snapshot = copy.deepcopy(params.get("layout") or {})
    try:
        out = ai_layout.run_ai_layout(root, task_id, project_id)
    except Exception as e:  # noqa: BLE001 — 检测失败不阻断渲染
        return {"ran": True, "source": "error", "reason": "stale",
                "error": str(e)[:200], "regions": 0}
    result = out.get("result") if isinstance(out, dict) else None
    source = str((result or {}).get("source") or "")
    regions = len((result or {}).get("regions") or [])
    if source == "llm":
        try:
            bg_detect.apply_detected_to_layout(root, task_id, project_id)
        except Exception as e:  # noqa: BLE001 — 填充失败仅记错误
            return {"ran": True, "source": source, "reason": "stale",
                    "regions": regions, "apply_error": str(e)[:200]}
        return {"ran": True, "source": source, "reason": "stale",
                "regions": regions}
    # fallback：恢复手动 layout（run_ai_layout 内部 apply_ai_layout 已写默认布局）
    project = svc.load_project(root, task_id, project_id)
    s = svc.get_stage_state(project, "stage6_fine")
    p = fine._get_params(project)
    p["layout"] = snapshot
    s["params"] = p
    svc.set_stage_state(project, "stage6_fine", s)
    svc.save_project(root, project)
    return {"ran": True, "source": source or "fallback", "reason": "stale",
            "regions": regions}
