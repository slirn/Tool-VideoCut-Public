"""精剪合成渲染引擎 — 原 app.py 内嵌实现抽出（保持逐字搬移，行为不变）。

内容：ASS 字幕样式/生成、L0-L6 视频层与 A1-A3 音频层 filter_complex 组装、
渲染执行（进度 / 取消 / 断点恢复 / 宽限杀进程 / 时长校验）、渲染任务表
_JOB_REGISTRY、素材路径解析与 fine_compose 配置 IO（_get_fine_compose /
_save_fine_compose / _save_bg_detect_cache）。

app.py 通过模块级 import 引回全部名字（同一批对象，含可变的 _JOB_REGISTRY，
行为与搬移前完全一致）。本模块只依赖标准库，不 import app.py（无环）。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# REQ-20260919-061：精剪视频·四素材合成器 — 数据结构辅助函数
_FINE_MATERIAL_KINDS = ("video", "subtitle", "cover", "bg", "reference", "audio")
_FINE_MATERIAL_LABELS = {
    "video":     ("🎬", "粗剪视频", "mp4/mov"),
    "subtitle":  ("📝", "字幕文件", "srt"),
    "cover":     ("🖼", "封面图片", "png/jpg"),
    "bg":        ("🎨", "背景图片", "png/jpg"),
    "reference": ("🤖", "参考位置关系图", "png/jpg（AI 解析用）"),
    "audio":     ("🎵", "背景音乐", "mp3/wav/m4a"),
}
# 用户补充 · x/y 改为像素坐标（基于 1920×1080 设计空间）：
# - 0.35 * 1920 ≈ 672；0.9 * 1080 = 972；0.7 * 1920 ≈ 1344；0.85 * 1080 = 918
# - 设计空间保证：720p 输出时 ffmpeg 在末尾 scale，WYSIWYG 不变
_FINE_DESIGN_W = 1920
_FINE_DESIGN_H = 1080
_FINE_LAYOUT_SCHEMA = 2  # _schema=2：x/y 是像素；旧 0-1 数据自动迁移
# REQ-20260919-062 v19：颜色 picker 只接受完整 #RRGGBB（拒绝 #fff 简写和非 hex 字符串）
_HEX_COLOR_OK = re.compile(r"^#[0-9A-Fa-f]{6}$")
# REQ-20260919-061 扩展：cover 不再是「角标小图」，改为「片头全屏海报」；
# 旧字段 x/y/scale 在迁移时被保留（数据不丢），新增 duration 字段控制展示秒数。
_FINE_LAYOUT_DEFAULTS = {
    # REQ-20260919-061 用户补充：视频不铺满画布，只占左侧 70%（右侧让背景图人员区透过）
    # crop_* 是从源视频里再截一个矩形（归一化 0-1），与画布 x/y/scale 正交。
    # 默认全幅（不裁剪）；点击「16:9 居中」预设切换到 16:9 居中矩形。
    "video":    {"x": 0,    "y": 0,    "scale": 0.7,
                 # crop_* 是从源视频里截一个矩形（设计空间 1920×1080 像素），
                 # 渲染时按源视频实际尺寸等比换算到 iw/ih
                 "crop_x": 0, "crop_y": 0, "crop_w": 1920, "crop_h": 1080,
                 "enabled": True,
                 # （viewport 字段已删除 — 用户反馈：检测区域「应用」只负责填充，
                 # 填充后 X/Y 需要手工自由调整，不得再夹紧回检测区域；
                 # 检测结果的持久化走 fc.detected_region / bg_detect_cache）
                 # REQ-20260919-062 v18 用户反馈：「视频源裁剪里的锁定 16:9 比例」也要保存。
                 # True = crop_w/crop_h 拖动时按 16:9 联动（防变形）；
                 # False = 任意调整（1:1 等预设才能任意设 w=h）。
                 # 默认 True，与前端默认勾选保持一致。
                 "crop_aspect_lock": True},
    "subtitle": {"x": 672,  "y": 972,  "scale": 1.0, "enabled": True},
    "cover":    {"enabled": False, "duration": 2.0},
    "bg":       {"x": 0,    "y": 0,    "scale": 1.0, "enabled": False},
}
_FINE_FONT_DEFAULTS = {
    "size":         36,
    "color":        "#FFFFFF",  # REQ-20260919-062：字幕文字本身颜色（libass PrimaryColour）
    "stroke_width": 2,
    "stroke_color": "#000000",
    "bg_enabled":   False,
    "bg_color":     "#000000",
    "bg_opacity":   0.6,
    "bg_radius":    4,
    "bold":         True,
    "align":        "center",
    "offset":       0,  # REQ-20260921-NNN：align=center_offset 时相对屏幕中心的偏移像素（负数=左偏，正数=右偏；「居中」模式忽略）
    "family":       "STHeitiMedium",
}
# REQ-20260919-061 扩展：背景音乐 4 项（启用 + 音量 + 淡入/淡出）
# REQ-20260922-NNN：volume(0-1 线性) → volume_db（dB 衰减，0=原曲，-40≈静音）。
# 线性刻度不直观且与源文件响度无关（0.2 恒为 -14 dB，响碟轻碟结果差数 dB）；
# 旧默认 0.4 = 20·log10(0.4) ≈ -8 dB，行为等价迁移（读取端 _fine_volume_db 兜底旧数据）。
_FINE_AUDIO_DEFAULTS = {
    "enabled":    False,
    "volume_db":  -8.0,
    "fade_in":    0.0,
    "fade_out":   0.0,
}
_FINE_VOLUME_DB_MIN = -40.0
_FINE_VOLUME_DB_MAX = 0.0


def _fine_volume_db(audio_cfg: dict) -> float:
    """REQ-20260922-NNN：BGM 音量统一取 dB 衰减值（0=原曲，-40≈静音）。

    优先 volume_db；兼容旧数据/旧模板里的线性 volume(0-1)：20·log10(v) 换算
    （0.2≈-14 dB、0.4≈-8 dB；旧 0 静音 → -40 保持静音），越界 clamp。
    _get_fine_compose 加载时已迁移落盘，这里兜底直调
    _assemble_fine_filter / _predict_audio_path 的未迁移 fc。
    """
    v = audio_cfg.get("volume_db")
    if v is not None:
        try:
            return max(_FINE_VOLUME_DB_MIN, min(_FINE_VOLUME_DB_MAX, float(v)))
        except (TypeError, ValueError):
            pass
    old = audio_cfg.get("volume")
    if old is not None:
        try:
            old_f = min(max(float(old), 1e-4), 1.0)
            return max(_FINE_VOLUME_DB_MIN, min(_FINE_VOLUME_DB_MAX, 20.0 * math.log10(old_f)))
        except (TypeError, ValueError):
            pass
    return float(_FINE_AUDIO_DEFAULTS["volume_db"])
# REQ-20260921-NNN：精剪·生成预览参数（独立于 layout/font/output/audio —
# 仅控制「生成预览」临时截取的起止区间；不影响导出最终视频）。
# start_h/m/s 是时:分:秒三段独立输入（前端友好），duration 是秒数（2-30）。
_FINE_PREVIEW_DEFAULTS = {
    "start_h":  0,
    "start_m":  0,
    "start_s":  0,
    "duration": 10,
}
_FINE_OUTPUT_DEFAULTS = {
    "resolution":  "1080p",  # 720p / 1080p / source
    "codec":       "h264",
    "audio_codec": "aac",
}


# REQ-20260920-078：系统默认 BGM 备选列表（绝对路径硬编码，不动态扫描）。
# REQ-20260921-NNN-bgm-in-repo：5 个 mp3 已入库 slirn_home/assets/default_bgms/。
#   优先从仓库内读（clone 后即可用）；缺失则 fallback 到原外部路径 D:\tmp\tttttt\（用户本机缓存）。
#   启动时校验 available；缺失则 UI 灰显，不报错。
_ASSETS_BGMS_DIR = Path(__file__).resolve().parent / "assets" / "default_bgms"
_EXTERNAL_BGMS_DIR = Path(r"D:\tmp\tttttt")
_DEFAULT_BGMS_DIR = _ASSETS_BGMS_DIR if _ASSETS_BGMS_DIR.exists() else _EXTERNAL_BGMS_DIR
_DEFAULT_BGMS = [
    {"id": "lofi_beat_1",    "name": "Pretty John — Lo-Fi Beat",
     "filename": "prettyjohn1-lo-fi-beat-580021.mp3"},
    {"id": "lofi_love_loop", "name": "Sonican — Sentimental Jazzy Love",
     "filename": "sonican-lo-fi-music-loop-sentimental-jazzy-love-473154.mp3"},
    {"id": "the_mountain",   "name": "The Mountain — Lo-Fi Beat",
     "filename": "the_mountain-lo-fi-beat-567432.mp3"},
    {"id": "zephira_lofi",   "name": "Zephira Music — Lo-Fi",
     "filename": "zephiramusic-lo-fi-581502.mp3"},
    {"id": "zephira_relax",  "name": "Zephira Music — Relaxing Lo-Fi",
     "filename": "zephiramusic-relaxing-lo-fi-587547.mp3"},
]


def _get_default_bgms() -> list[dict]:
    """返回系统默认 BGM 列表（启动时校验存在性，available 字段标识）。"""
    out = []
    for bgm in _DEFAULT_BGMS:
        p = _DEFAULT_BGMS_DIR / bgm["filename"]
        size = 0
        if p.exists():
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
        out.append({**bgm, "available": p.exists(), "size_bytes": size})
    return out


# REQ-20260919-061 用户补充：video/subtitle 既可手动上传，也可默认从上游产物获取
_FINE_AUTO_KINDS = frozenset({"video", "subtitle"})


def _fine_upstream_path(task_id: str, kind: str, mgr) -> Path | None:
    """返回上游产物绝对路径（None = 无上游或不支持 auto）。

    - video → 优化成片优先（REQ-20260922-NNN：优化字幕阶段标记删除行生成的
      optimize_compose.mp4，须与当前剪辑计划匹配且不旧于粗剪 — 见
      compose_service.optimize_cut_ready），否则 rough_compose.mp4
    - subtitle → optimize_subtitle.json → 落盘到 tmp/optimized_subs.srt
      （时间基跟随上面的视频选择：优化成片 → 排除删除内容 + 时间轴前移）
    """
    from slirn_home import compose_service, optimize_service

    outputs_dir = mgr.tasks_dir / task_id / "outputs"
    data = optimize_service.load_optimize(outputs_dir)
    plan = optimize_service.cut_plan(data or {})  # 整行标记 + 删除子段
    marks = (data or {}).get("line_marks") or []
    ready = compose_service.optimize_cut_ready(outputs_dir, plan)
    if kind == "video":
        if ready is not None:
            return compose_service.optimize_compose_path(outputs_dir)
        p = compose_service.rough_compose_path(outputs_dir)
        return p if p.exists() else None
    if kind == "subtitle":
        if not data or not data.get("saved_at"):
            return None
        segments = data.get("segments") or []
        srt_text = optimize_service.build_srt(
            segments, marks, shift=(ready is not None),
            line_splits=data.get("line_splits"),
            split_marks=data.get("split_marks"),
            occurrences=data.get("occurrences"))
        tmp_dir = mgr.tasks_dir / task_id / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        out = tmp_dir / "optimized_subs.srt"
        out.write_text(srt_text, encoding="utf-8")
        return out
    return None


def _fine_upstream_label(task_id: str, kind: str, mgr) -> str:
    """上游产物的可读文件名（用于 UI 显示）。"""
    p = _fine_upstream_path(task_id, kind, mgr)
    return p.name if p else ""


def _effective_fine_video_path(mgr, task_id: str, materials: dict | None = None) -> Path | None:
    """精剪合成实际生效的视频路径（REQ-20260922-NNN 标记删除行）。

    - 传 materials 且 video 素材是 auto 来源 → 实时取上游（优化成片
      optimize_compose.mp4 优先于 rough_compose.mp4，新鲜度见
      compose_service.optimize_cut_ready）
    - 传 materials 且手动上传 → _resolve_mat_abs 原样解析（用户显式上传优先）
    - 不传 materials → 纯上游口径（_probe_video_duration_ms 等探测用）

    自动获取的素材路径是挑选那一刻的快照 — 优化字幕阶段重存后上游可能已
    从 rough 换成 optimize_compose（或反向），每次组装/探测都实时重解析。
    """
    if materials is not None:
        mat = materials.get("video") or {}
        if mat.get("source") == "auto":
            p = _fine_upstream_path(task_id, "video", mgr)
            return p if p and p.exists() else None
        return _resolve_mat_abs(mgr, task_id, materials, "video")
    return _fine_upstream_path(task_id, "video", mgr)


def _resolve_mat_abs(mgr, task_id: str, materials: dict, kind: str) -> Path | None:
    """把 materials[kind].path（相对路径）解析为绝对路径（用于 ffmpeg input）。

    路径约定有两种历史来源：
    - 自动获取（compose_service.rough_compose_path / optimize_service.build_srt）：
      写的是 `mgr.tasks_dir.relative_to(mgr.repo_root)` → 不含 `tasks/` 前缀
      例：`20260918-022\\outputs\\rough_compose.mp4`
    - 上传（upload_fine_material_form）：
      写的是 `save_path.relative_to(repo_root)` → 含 `tasks/` 前缀
      例：`tasks\\20260918-022\\upload\\cover.png`

    这里两种都试一下：先 tasks_dir + pp，回退 repo_root + pp。
    """
    mat = materials.get(kind) or {}
    p = mat.get("path")
    if not p:
        return None
    pp = Path(p)
    if pp.is_absolute():
        return pp
    # 候选 1：tasks_dir + pp（自动获取约定）
    cand1 = (mgr.tasks_dir / pp).resolve()
    if cand1.exists():
        return cand1
    # 候选 2：repo_root + pp（上传约定，路径含 `tasks/` 前缀）
    cand2 = (mgr.repo_root / pp).resolve()
    if cand2.exists():
        return cand2
    # REQ-20260920-083：候选 3 — tasks_dir + task_id + pp（兼容 select_default_bgm 旧数据，
    # 旧版写 "materials/audio/<id>.mp3" 不含前缀；文件实际在 tasks/<tid>/materials/audio/）。
    # 用 exists() 检查兜底 — 不会误命中其他 kind 的同名文件。
    cand3 = (mgr.tasks_dir / task_id / pp).resolve()
    if cand3.exists():
        return cand3
    # 都找不到：返回最近似的（让上层报错信息有真实路径）
    return cand1


def _ffmpeg_filter_path(p: Path) -> str:
    """REQ-20260919-061 Phase B：把 Path 转成 ffmpeg filter 安全的字符串。

    Windows 路径含 `:`（盘符）+ `\`（转义符），与 ffmpeg filter 选项语法冲突。
    解决：全部转 `/`，并把 `:` 转义为 `\:`。
    """
    s = str(p).replace("\\", "/")
    s = s.replace(":", r"\:")
    return s


def _ass_force_style(font: dict, layout: dict | None = None) -> str:
    """REQ-20260919-061 Phase B：把 font 设置转成 ASS force_style（libass 字幕滤镜用）。

    REQ-20260920-099：新增可选 ``layout`` 参数（来自 ``fc.layout`` dict）。
    传入时按 ``layout.subtitle.x`` / ``layout.subtitle.y`` 推导字幕在画布上的位置：
    - subtitle.y > 0 → ``MarginV = H - subtitle.y``
      （Alignment=2 bottom → baseline 到画布底的距离，字幕基线画在 y 处）
    - subtitle.x 按 Alignment 走独立分支（互不复用）：
        * left  (Alignment=1) → MarginL = subtitle.x   （文字左边缘绝对位置）
        * right (Alignment=3) → MarginR = W - subtitle.x  （文字右边缘绝对位置）
        * center (Alignment=2, align_val=="center") → sub_x 忽略，文字中心 = W/2
        * center_offset (Alignment=2, align_val=="center_offset") → sub_x 忽略，
          文字中心 = W/2 + font.offset（offset 可负：负=左偏，正=右偏）
    ``layout=None`` 或 subtitle.x/y == 0 → 退回到纯 font 旧逻辑。

    REQ-20260921-NNN：``font.left_offset`` 字段重命名为 ``font.offset``，
    语义改为「相对屏幕中心的偏移，负=左偏、正=右偏」。老 fc.json 的
    ``left_offset`` 由 ``_get_fine_compose`` 自动迁移（取负号）。

    REQ-20260920-099 Phase C：本函数保留 force_style 字符串生成，但
    ``_assemble_fine_filter`` 现在改用 ``ass=`` 滤镜（直接喂 ASS 文件），
    因此该字符串仅用于「快速参照 / 测试断言」，运行时不再下发到 ffmpeg。
    """
    family_map = {
        "STHeitiMedium": "STHeiti Medium",
        "Noto Sans CJK SC": "Noto Sans CJK SC",
    }
    family = family_map.get(font["family"], font["family"])
    parts = [f"FontName={family}", f"FontSize={int(font['size'])}"]
    # REQ-20260919-062：字幕文字本身颜色 = libass PrimaryColour
    # #RRGGBB → &H00BBGGRR（ASS 用 BGR，alpha 在前 &H00 = 不透明）
    # None/非字符串（如 0/False）走默认 #FFFFFF；非 #RRGGBB 格式（如 #fff/#GGGGGG）静默忽略。
    tc_raw = font.get("color", "#FFFFFF")
    if isinstance(tc_raw, str):
        tc = tc_raw.lstrip("#")
        if len(tc) == 6 and all(c in "0123456789abcdefABCDEF" for c in tc):
            tc_bgr = tc[4:6] + tc[2:4] + tc[0:2]
            parts.append(f"PrimaryColour=&H00{tc_bgr.upper()}")
    if font.get("bold"):
        parts.append("Bold=1")
    align_map = {"left": 1, "center": 2, "right": 3, "center_offset": 2}
    align_val = font.get("align", "center")
    align = align_map.get(align_val, 2)
    parts.append(f"Alignment={align}")

    # REQ-20260920-099：从 layout.subtitle 读水平 / 垂直坐标
    sub_x, sub_y = 0, 0
    if isinstance(layout, dict):
        _sub_d = layout.get("subtitle") or {}
        if isinstance(_sub_d, dict):
            try:
                sub_x = int(_sub_d.get("x", 0) or 0)
            except (TypeError, ValueError):
                sub_x = 0
            try:
                sub_y = int(_sub_d.get("y", 0) or 0)
            except (TypeError, ValueError):
                sub_y = 0

    # 垂直：subtitle.y > 0 → MarginV = H - subtitle.y（baseline 到画布底的距离）
    if sub_y > 0:
        margin_v = max(0, _FINE_DESIGN_H - sub_y)
        parts.append(f"MarginV={margin_v}")

    # 水平：每个 align 走独立分支
    # REQ-20260921-NNN：center/center_offset 不再复用 sub_x；offset（signed）决定文字相对屏幕中心的位置
    if align_val == "center_offset":
        try:
            offset_px = int(font.get("offset", 0) or 0)
        except (TypeError, ValueError):
            offset_px = 0
    else:
        offset_px = 0

    if align == 1:  # left：文字左边缘 = sub_x
        if sub_x > 0:
            margin_l = max(0, sub_x)
            parts.append(f"MarginL={margin_l}")
    elif align == 3:  # right：文字右边缘 = W - sub_x
        if sub_x > 0:
            margin_r = max(0, _FINE_DESIGN_W - sub_x)
            parts.append(f"MarginR={margin_r}")
    elif align_val == "center_offset":
        # 居中+偏移：文字中心 = W/2 + offset
        #   offset > 0 → MarginL = 2*offset, MarginR = 0   （右偏）
        #   offset < 0 → MarginL = 0,        MarginR = -2*offset  （左偏）
        if offset_px > 0:
            parts.append(f"MarginL={offset_px * 2}")
        elif offset_px < 0:
            parts.append(f"MarginR={-offset_px * 2}")
        # offset_px == 0 → 不插 margin，文字自然居中
    # align=center：什么都不插，文字自然居中（W/2）
    sw = int(font.get("stroke_width") or 0)
    if sw > 0:
        # ASS stroke_color #RRGGBB → &H00BBGGRR
        sc = font.get("stroke_color", "#000000").lstrip("#")
        if len(sc) == 6:
            sc = sc[4:6] + sc[2:4] + sc[0:2]
            parts.append(f"Outline={sw}")
            parts.append(f"OutlineColour=&H00{sc.upper()}")
    if font.get("bg_enabled"):
        bc = font.get("bg_color", "#000000").lstrip("#")
        if len(bc) == 6:
            bc = bc[4:6] + bc[2:4] + bc[0:2]
        op = float(font.get("bg_opacity", 0.6))
        alpha_hex = format(int((1.0 - op) * 255), "02X")
        parts.append("BorderStyle=4")  # 背景框
        parts.append(f"BackColour=&H{alpha_hex}{bc.upper()}")
    return ",".join(parts)


def _ass_style_line(font: dict, layout: dict | None, W: int, H: int) -> dict:
    """REQ-20260920-099 Phase C：把 ``_ass_force_style`` 的力扣串「解构」成 dict，
    供 ``_srt_to_ass`` 写 ASS Style 行。

    返回 dict（含 22 列，与 ASS V4+ Style Format 一一对应）：
        ``name, fontname, fontsize, primary_colour, secondary_colour,
        outline_colour, back_colour, bold, italic, underline, strike_out,
        scale_x, scale_y, spacing, angle, border_style, outline, shadow,
        alignment, margin_l, margin_r, margin_v, encoding``

    水平位置推导（REQ-20260921-NNN 简化版）：
    - left  (Alignment=1) → MarginL = sub_x    （文字左边缘绝对位置）
    - right (Alignment=3) → MarginR = W - sub_x （文字右边缘绝对位置）
    - center (Alignment=2, align_val=="center") → sub_x 与 font.offset 都忽略，
      文字中心 = W/2
    - center_offset (Alignment=2, align_val=="center_offset") → sub_x 忽略，
      文字中心 = W/2 + font.offset（offset 可负：负=左偏、正=右偏）

    注：ASS Style 写「绝对值」，不能复用 force_style 串（force_style 是
    libass 的 overlay 机制，只对单 Style 行覆盖；ASS 文件必须写到底层 Style）。
    """
    family_map = {
        "STHeitiMedium": "STHeiti Medium",
        "Noto Sans CJK SC": "Noto Sans CJK SC",
    }
    family = family_map.get(font["family"], font["family"])
    fs = int(font["size"])
    # PrimaryColour = &H00BBGGRR
    tc_raw = font.get("color", "#FFFFFF")
    primary = "&H00FFFFFF"
    if isinstance(tc_raw, str):
        tc = tc_raw.lstrip("#")
        if len(tc) == 6 and all(c in "0123456789abcdefABCDEF" for c in tc):
            tc_bgr = tc[4:6] + tc[2:4] + tc[0:2]
            primary = f"&H00{tc_bgr.upper()}"
    # OutlineColour
    sc_raw = font.get("stroke_color", "#000000")
    outline_colour = "&H00000000"
    sw = int(font.get("stroke_width") or 0)
    if isinstance(sc_raw, str):
        sc = sc_raw.lstrip("#")
        if len(sc) == 6:
            sc = sc[4:6] + sc[2:4] + sc[0:2]
            outline_colour = f"&H00{sc.upper()}"
    # BackColour（含背景框 alpha）
    back_colour = "&H00000000"
    border_style = 1  # 1=stroke+shadow；4=opaque box
    if font.get("bg_enabled"):
        border_style = 4
        bc_raw = font.get("bg_color", "#000000")
        if isinstance(bc_raw, str):
            bc = bc_raw.lstrip("#")
            if len(bc) == 6:
                bc = bc[4:6] + bc[2:4] + bc[0:2]
                op = float(font.get("bg_opacity", 0.6))
                alpha_hex = format(int((1.0 - op) * 255), "02X")
                back_colour = f"&H{alpha_hex}{bc.upper()}"

    bold = -1 if font.get("bold") else 0  # -1 = True (libass)；ASS 用 1=否 0
    # ASS Bold: 1=Bold on, 0=off；force_style 用 -1 表示粗体。统一为 1。
    bold = 1 if font.get("bold") else 0
    italic = 0
    underline = 0
    strike_out = 0
    scale_x = 100
    scale_y = 100
    spacing = 0
    angle = 0
    shadow = 0  # 0=无 shadow（stroke 已经够用）
    if sw == 0:
        outline = 0
    else:
        outline = sw

    align_map = {"left": 1, "center": 2, "right": 3, "center_offset": 2}
    align_val = font.get("align", "center")
    alignment = align_map.get(align_val, 2)

    # 水平/垂直 margin 推导（与 _ass_force_style 同步）
    sub_x, sub_y = 0, 0
    if isinstance(layout, dict):
        _sub_d = layout.get("subtitle") or {}
        if isinstance(_sub_d, dict):
            try:
                sub_x = int(_sub_d.get("x", 0) or 0)
            except (TypeError, ValueError):
                sub_x = 0
            try:
                sub_y = int(_sub_d.get("y", 0) or 0)
            except (TypeError, ValueError):
                sub_y = 0

    margin_v = 0
    if sub_y > 0:
        margin_v = max(0, H - sub_y)

    if align_val == "center_offset":
        try:
            offset_px = int(font.get("offset", 0) or 0)
        except (TypeError, ValueError):
            offset_px = 0
    else:
        offset_px = 0

    # REQ-20260921-NNN：每个 align 走独立分支；center/center_offset 不再复用 sub_x
    margin_l = 0
    margin_r = 0
    if alignment == 1:  # left：文字左边缘 = sub_x
        margin_l = max(0, sub_x)
    elif alignment == 3:  # right：文字右边缘 = W - sub_x
        margin_r = max(0, W - sub_x)
    elif align_val == "center_offset":
        # 居中+偏移：文字中心 = W/2 + offset（offset 可负）
        if offset_px > 0:
            margin_l = 2 * offset_px
        elif offset_px < 0:
            margin_r = -2 * offset_px
        # offset_px == 0 → margin_l=0, margin_r=0 → 文字自然居中
    # align=center：margin_l=0, margin_r=0 → 文字自然居中（W/2）

    return {
        "name": "Default",
        "fontname": family,
        "fontsize": fs,
        "primary_colour": primary,
        "secondary_colour": "&H000000FF",
        "outline_colour": outline_colour,
        "back_colour": back_colour,
        "bold": bold,
        "italic": italic,
        "underline": underline,
        "strike_out": strike_out,
        "scale_x": scale_x,
        "scale_y": scale_y,
        "spacing": spacing,
        "angle": angle,
        "border_style": border_style,
        "outline": outline,
        "shadow": shadow,
        "alignment": alignment,
        "margin_l": margin_l,
        "margin_r": margin_r,
        "margin_v": margin_v,
        "encoding": 1,  # 1 = Default (English/Western)；中文字体仍能 fallback
    }


def _srt_to_ass(srt_entries: list[dict], font: dict, layout: dict | None,
                W: int, H: int) -> str:
    """REQ-20260920-099 Phase C：把 SRT entries + font/layout 生成 ASS 字符串。

    关键差异（vs 用 ``subtitles=`` 滤镜让 ffmpeg 自动转 SRT→ASS）：
    - ``subtitles=`` 会用默认 PlayResX=384 / PlayResY=288 转 ASS，导致 font size 与
      MarginL/R/V 都在 384×288 坐标里计算，与视频 1920×1080 像素不一致 → 文字会
      被自动放大 5 倍 + MarginL/R 缩放不对，结果就是 19 字中文被 libass 强行 wrap
      成每行一字（看上去「字幕消失了」）。
    - 这里显式写 PlayResX=W / PlayResY=H，使 force_style 的字号、margin 都在
      实际视频像素坐标系计算，文字大小 / 位置与编辑器参数一致。

    返回完整 ASS v4.00+ 文本（含 [Script Info] / [V4+ Styles] / [Events]）。
    每个 Dialogue 的 MarginL/R/V 设为 0（事件级 override），让 Style 行的值生效。
    """
    style = _ass_style_line(font, layout, W, H)
    style_line = ",".join(str(style[k]) for k in [
        "name", "fontname", "fontsize", "primary_colour", "secondary_colour",
        "outline_colour", "back_colour", "bold", "italic", "underline",
        "strike_out", "scale_x", "scale_y", "spacing", "angle", "border_style",
        "outline", "shadow", "alignment", "margin_l", "margin_r", "margin_v",
        "encoding",
    ])

    def _ms_to_ass_time(ms: int) -> str:
        """整数毫秒 → ASS 时间 H:MM:SS.cc（厘秒 = 1/100 秒；ASS 默认单位）。"""
        if ms < 0:
            ms = 0
        h = ms // 3_600_000
        m = (ms % 3_600_000) // 60_000
        s = (ms % 60_000) // 1_000
        cs = (ms % 1_000) // 10  # 厘秒（centisecond）
        return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"

    # ASS \n 是硬换行；SRT 文本里的换行要转成 \N 才能在 ASS 里真换行
    events = []
    for ent in srt_entries:
        text = str(ent.get("text", "")).replace("\r\n", "\n").replace("\n", r"\N")
        start_ms = int(ent["start_ms"])
        end_ms = int(ent["end_ms"])
        if end_ms <= start_ms:
            continue
        start = _ms_to_ass_time(start_ms)
        end = _ms_to_ass_time(end_ms)
        # MarginL/R/V 在 Dialogue 设为 0 → 强制使用 Style 行的值
        events.append(
            f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}"
        )

    return (
        "[Script Info]\n"
        "; Generated by slirn (REQ-20260920-099 Phase C)\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\n"
        f"PlayResY: {H}\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: None\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: {style_line}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
        + "\n".join(events)
        + ("\n" if events else "")
    )


# REQ-20260920-079：bg/cover/reference 超大图自动缩放（防 ffmpeg OOM 卡死）
# 触发：源图长边 > _PRESCALE_THRESHOLD_PX 才缩；缩到 fit target_w×target_h 后 pad。
# 不动用户原图（写 .<label>_req079_<stem>_<W>x<H><.ext> 临时文件）。
_PIL_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
_PRESCALE_THRESHOLD_PX = 4096  # 4K 横向分辨率；超过即视为「可能 OOM」


def _maybe_prescale_image(path: Path, target_w: int, target_h: int, label: str,
                          threshold: int = _PRESCALE_THRESHOLD_PX) -> Path:
    """如果图片长边 > threshold，按 LANCZOS 缩放至 fit target_w × target_h，
    黑边/透明 pad 到精确尺寸，写到源同目录的 .<label>_req079_<stem>_<W>x<H><.ext>。

    返回 ffmpeg 应该使用的路径：
    - 不需要缩放 → 返回原 path（无任何操作）
    - 缩放成功   → 返回临时文件 path
    - 缩放失败   → log.warning + 返回原 path（兜底不阻塞）

    RGBA 透明 PNG 保留 mode=RGBA + 透明 canvas，让 ffmpeg 继续按 alpha 合成
    （与 REQ-20260919-063 `_build_bg_layer_chain` 的黑底 overlay 链行为一致）。
    """
    if path.suffix.lower() not in _PIL_IMAGE_EXTS:
        return path
    try:
        from PIL import Image as _PILImage
        with _PILImage.open(path) as im:
            iw, ih = im.size
            if max(iw, ih) <= threshold:
                return path
            tmp = path.with_name(
                f".{label}_req079_{path.stem}_{target_w}x{target_h}{path.suffix}"
            )
            # RGBA/LA 保留 alpha；其他转 RGB
            mode = "RGBA" if im.mode in ("RGBA", "LA") else "RGB"
            im2 = im.convert(mode)
            # thumbnail 保比例 fit（max(target_w, target_h) 上限）
            im2.thumbnail((target_w, target_h), _PILImage.LANCZOS)
            bg = (0, 0, 0, 0) if mode == "RGBA" else (0, 0, 0)
            canvas = _PILImage.new(mode, (target_w, target_h), bg)
            x = (target_w - im2.size[0]) // 2
            y = (target_h - im2.size[1]) // 2
            canvas.paste(im2, (x, y))
            canvas.save(tmp, optimize=True)
            log.warning(
                "[REQ-079] %s: %dx%d → %dx%d (%s) → tmp=%s",
                label, iw, ih, target_w, target_h, path.name, tmp.name,
            )
            return tmp
    except Exception as e:
        log.warning("[REQ-079] %s pre-scale failed, using original: %s", label, e)
        return path


def _build_bg_layer_chain(
    bg_idx: int, W: int, H: int, duration: float | None = None
) -> list[str]:
    """REQ-20260919-063：bg 图透明区黑底 alpha 合成链。

    - bg_idx >= 0：bg 图是 RGBA 且透明像素 RGB=白色 → 必须用「黑底 + bg 图 overlay」
      才能让透明像素显示黑色（否则 ffmpeg 直接把透明像素当白色渲染 → 用户看到「白框」）。
      注：ffmpeg 的 format filter 不支持 'auto' 值；PNG 解码默认保留 alpha，
      overlay 会自动按 alpha 合成。
    - bg_idx < 0：纯黑底。
    - duration（REQ-20260929-NNN）：画布有限化。color 源无限 + bg 图 -loop 1 无限
      时，视频分支永远等不到自然 EOF，长视频导出（20260924-001, 4278s）中有限
      的音频分支在源 ~1104s 处被调度器静默截断（exit 0、stderr 无警告）。
      给 color 源加显式 duration 后视频分支在预期时长附近自然收口，输出 -t
      只作钳制；duration=None（probe 失败）保持旧行为（无限 + -shortest 兜底）。
    返回：filter_complex chain 片段。
    """
    d = f":duration={duration:.3f}" if duration else ""
    if bg_idx >= 0:
        return [
            f"color=size={W}x{H}:color=black:rate=30{d}[bg_b]",
            f"[{bg_idx}:v]scale={W}:{H},setsar=1[bg_img]",
            "[bg_b][bg_img]overlay=eof_action=pass[bg]",
        ]
    return [f"color=size={W}x{H}:color=black:rate=30{d}[bg]"]


def _build_fine_filter(fc: dict, W: int, H: int) -> tuple[list[str], list[str], str]:
    """构建 ffmpeg inputs + filter_complex（REQ-20260919-061 Phase B）。

    返回：(input_args, chain, final_label)
    - input_args: ["-i", path, ...]
    - chain: filter_complex 的分号串行
    - final_label: 最终视频流的标签（用于 -map）
    """
    layout = fc["layout"]
    materials = fc["materials"]
    font = fc["font"]

    inputs: list[str] = []
    chain: list[str] = []

    # Input 0: 视频（必填；render_fine_preview 先校验存在）
    # Input 1 (opt): 背景
    # Input 2 (opt): 封面
    bg_input_idx = -1
    cover_input_idx = -1

    if layout["bg"]["enabled"] and (materials.get("bg") or {}).get("path"):
        bg_input_idx = 1  # 假设视频=0, bg=1
    if layout["cover"]["enabled"] and (materials.get("cover") or {}).get("path"):
        cover_input_idx = 2 if bg_input_idx >= 0 else 1

    # 1. 背景层 — REQ-20260919-063：详见 _build_bg_layer_chain
    chain.extend(_build_bg_layer_chain(bg_input_idx, W, H))
    cur = "[bg]"

    # 2. 视频层：crop + scale + overlay
    vc = layout["video"]
    if vc["enabled"]:
        # ASS 表达式中 crop_w/h 是 0-1，归一化 iw/ih
        crop_expr = (
            f"crop=iw*{vc['crop_w']}:ih*{vc['crop_h']}:"
            f"iw*{vc['crop_x']}:ih*{vc['crop_y']}"
        )
        # 缩放后尺寸（向上取整防止 0）
        sw = max(1, int(round(W * vc["scale"])))
        sh = max(1, int(round(H * vc["scale"])))
        chain.append(
            f"[0:v]{crop_expr},scale={sw}:{sh}:flags=lanczos,setsar=1[v]"
        )
        vx = int(round(vc["x"] * W))
        vy = int(round(vc["y"] * H))
        chain.append(f"{cur}[v]overlay=x={vx}:y={vy}[v1]")
        cur = "[v1]"

    # 3. 封面层
    if cover_input_idx >= 0:
        cc = layout["cover"]
        # 封面基础尺寸：画布宽度的 30% × scale
        cw = max(1, int(round(W * 0.3 * cc["scale"])))
        ch = max(1, int(round(H * 0.3 * cc["scale"])))
        chain.append(f"[{cover_input_idx}:v]scale={cw}:{ch}[cv]")
        cx = int(round(cc["x"] * W))
        cy = int(round(cc["y"] * H))
        chain.append(f"{cur}[cv]overlay=x={cx}:y={cy}[v2]")
        cur = "[v2]"

    # 4. 字幕 burn-in（force_style 用 font 设置）
    sub_mat = materials.get("subtitle") or {}
    if layout["subtitle"]["enabled"] and sub_mat.get("path"):
        fs = _ass_force_style(font, layout)
        # force_style 含逗号，filter graph 用逗号分隔参数，所以 force_style 内不能用逗号
        # 我们已经把逗号作为参数分隔，所以单字符串内不能含逗号；上面已用 , 作分隔
        # 但 force_style 子串里有逗号时会被解析错 — 用 \\, 转义不靠谱，改用半角 ;?
        # 实际 ASS 风格里我们没用逗号，用空格/; 都不行。规范做法：用 ',' 作分隔符
        # 时 force_style 内容不能含 ','
        # 简化：把 ',' 在 font family 里换掉（这里 family 已知不含逗号）；stroke_color/...
        # 转 16 进制无逗号
        # 所以最终 force_style 不含逗号，安全
        chain.append(
            f"{cur}subtitles='{sub_mat['path']}':force_style='{fs}':si=0[vout]"
        )
        cur = "[vout]"
    else:
        chain.append(f"{cur}copy[vout]")
        cur = "[vout]"

    return inputs, chain, cur


def _assemble_fine_filter(
    task_id: str,
    mgr,
    duration: float | None,
    preview_start: float = 0.0,
    *,
    combo: dict | None = None,
) -> dict:
    """REQ-20260919-074：从 fc 组装 ffmpeg 输入参数 + filter_complex（同步/异步共用）。

    REQ-20260920-098：新增 keyword-only 参数 ``combo``，用于按需覆盖 fc.json 里的
    5 个元素 enabled 状态（``{video, subtitle, cover, bg, audio}`` 各 True/False）。
    覆盖只作用在内存中的 ``fc`` 对象，**不会写回 fc.json**；调用方负责传入正确
    状态。``combo=None``（默认）→ 行为与旧版完全一致，仅依赖 fc.json。

    返回 dict（成功）：
        {
          "ok": True,
          "input_args": list[str],          # 喂给 ffmpeg 的 -ss/-t/-loop/-i 等
          "filter_complex": str,            # -filter_complex 完整字符串
          "final_map": str,                 # 末位输出标签（[vfinal] / [vout] / [vsub]）
          "sub_input_tmp": Path | None,     # 字幕偏移临时文件（调用方负责 finally 清理）
          "out_w": int, "out_h": int,        # 输出分辨率
        }
    返回 dict（失败）：
        {"ok": False, "error": "..."}
    """
    import tempfile as _tf
    from slirn_home.compose_service import parse_srt

    fc = _get_fine_compose(mgr, task_id)
    # REQ-20260920-098：combo 瞬时覆盖（不改写 fc.json，不落盘）
    if combo:
        layout_obj = fc.setdefault("layout", {})
        for _k in ("video", "subtitle", "cover", "bg"):
            if _k in combo:
                layout_obj.setdefault(_k, {})["enabled"] = bool(combo[_k])
        if "audio" in combo:
            fc.setdefault("audio", {})["enabled"] = bool(combo["audio"])
    layout = fc["layout"]
    materials = fc["materials"]
    output_cfg = fc["output"]

    # 1. 校验视频素材存在
    # REQ-20260922-NNN：auto 来源实时取上游（优化成片优先），手动上传不动
    video_path = _effective_fine_video_path(mgr, task_id, materials)
    if not video_path or not video_path.exists():
        return {"ok": False, "error": "缺少视频素材，请上传或自动获取粗剪视频"}

    # 2. 输出分辨率
    res = output_cfg.get("resolution", "1080p")
    if res == "720p":
        out_w, out_h = 1280, 720
    else:
        out_w, out_h = _FINE_DESIGN_W, _FINE_DESIGN_H
    W, H = _FINE_DESIGN_W, _FINE_DESIGN_H

    # 3a. REQ-20260929-NNN：先算预期输出时长（原在 filter 组装后算，现提前 —
    # bg 图输入与 color 画布需要有限化边界，见 _build_bg_layer_chain 注释）。
    # probe 一次，复用给 expected_output_sec / audio_total_duration（fade_out 起算）。
    cover_input_enabled = (
        layout["cover"]["enabled"]
        and float(layout["cover"].get("duration", 0)) > 0
        and (materials.get("cover") or {}).get("path")
    )
    _probe_sec: float | None = None
    try:
        _ms = _probe_video_duration_ms(mgr, task_id)
        if _ms > 0:
            _probe_sec = _ms / 1000.0
    except Exception as e:  # noqa: BLE001
        log.warning("REQ-095: probe 源视频时长失败，fade_out 起算退回 st=0: %s", e)
    # REQ-20260923-NNN：预期输出时长（确定性 -t 上界 + 完成后截断校验基准）。
    # 与实际滤镜图同构：片头 cover（若启用）+ 源贡献段：
    #   - duration 给定（预览/区间导出）：min(duration, probe - preview_start)
    #     — start+duration 超源尾时音频分支先结束，不夹紧会在校验时误报「被截断」；
    #   - duration=None（完整导出）：probe - preview_start。
    # probe 不可得 → expected_output_sec=None → 渲染退回 -shortest 旧行为并跳过校验。
    expected_output_sec: float | None = None
    if duration is not None:
        _seg_sec = float(duration)
        if _probe_sec is not None:
            _seg_sec = min(_seg_sec, max(0.0, _probe_sec - float(preview_start)))
    elif _probe_sec is not None:
        _seg_sec = max(0.0, _probe_sec - float(preview_start))
    else:
        _seg_sec = None
    if _seg_sec is not None:
        if cover_input_enabled:
            try:
                _cd = float(layout["cover"].get("duration", 0.0))
                if _cd > 0:
                    _seg_sec += _cd
            except Exception:
                pass
        expected_output_sec = round(_seg_sec, 3)
    # REQ-20260929-NNN：视频画布边界 = 预期时长 - 片头 cover + 0.5s 余量。
    # 画布只承载主画面段（cover 由 concat 前缀提供，不能重复计入）；余量保证
    # 画布自然 EOF 落在输出 -t 钳制点之后，视频流不被画布提前掐断。
    canvas_bound: float | None = None
    if expected_output_sec is not None:
        _cover_prefix = 0.0
        if cover_input_enabled:
            try:
                _cp = float(layout["cover"].get("duration", 0.0))
                if _cp > 0:
                    _cover_prefix = _cp
            except Exception:
                pass
        canvas_bound = expected_output_sec - _cover_prefix + 0.5

    # 3b. 收集 inputs
    input_args: list[str] = []
    input_args += ["-ss", str(max(0.0, float(preview_start)))]
    if duration is not None:
        input_args += ["-t", str(duration)]
    input_args += ["-i", str(video_path)]

    if layout["bg"]["enabled"] and (materials.get("bg") or {}).get("path"):
        bg_path = _resolve_mat_abs(mgr, task_id, materials, "bg")
        if bg_path and bg_path.exists():
            # REQ-20260929-NNN：bg 图输入有限化（-loop 1 无限产包的输入线程是
            # 长导出中音频被静默截断的触发方；边界与画布一致）。canvas_bound
            # 未知时保持旧行为（无限 loop）。
            if canvas_bound is not None:
                input_args += ["-loop", "1", "-t", f"{canvas_bound:.3f}", "-i", str(bg_path)]
            else:
                input_args += ["-loop", "1", "-i", str(bg_path)]
        else:
            layout = {**layout, "bg": {**layout["bg"], "enabled": False}}
            fc["layout"] = layout

    # REQ-20260920-096：用 lavfi anullsrc 注入 cover 时长对应的静音前缀，再 concat
    # 原始 [0:a]，绕开 adelay + aloop + amix 在 input-level -ss/-t 下触发的 PTS 错位
    # （"Queue input is backward in time" → audio 被截断到 ~22ms）。aevalsrc=0|0:...
    # 是「每通道表达式 | 每通道表达式」的语法；若写 0:0 会被解析成 exprs=0 & duration=0，
    # 产出 0 采样静音导致 concat 退化成只有后半段。先用 anullsrc 验证可行，再决定是否换
    # 成 aevalsrc（语义更明确：彻底无声 vs 极低底噪）。
    silence_idx = -1
    if cover_input_enabled:
        cover_path = _resolve_mat_abs(mgr, task_id, materials, "cover")
        if cover_path and cover_path.exists():
            cover_dur = float(layout["cover"].get("duration", 2.0))
            input_args += ["-loop", "1", "-framerate", "30", "-t", f"{cover_dur:.2f}", "-i", str(cover_path)]
            # 在 cover 之后追加一段等长静音输入；index = 当前 inputs_count
            input_args += ["-f", "lavfi", "-t", f"{cover_dur:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]
        else:
            layout = {**layout, "cover": {**layout["cover"], "enabled": False}}
            fc["layout"] = layout
            cover_input_enabled = False

    # REQ-20260920-096：silence_idx 一定在 cover 之后、bgm 之前（如果有 audio input）。
    # inputs_count 此刻尚未更新，silence_idx 等于当前的输入数量。
    inputs_count_now = sum(1 for i_, _ in enumerate(input_args) if input_args[i_] == "-i")
    if cover_input_enabled:
        # cover 占一个 slot，silence 紧随其后
        silence_idx = inputs_count_now - 1
    else:
        silence_idx = -1

    audio_cfg = fc.get("audio") or _FINE_AUDIO_DEFAULTS
    audio_input_enabled = (
        audio_cfg.get("enabled")
        and (materials.get("audio") or {}).get("path")
    )
    bgm_input_bounded = False
    # REQ-20260929-NNN（两进程导出）：BGM 输入在 input_args 里的切片范围
    # [lo, hi)（pass2 原位替换成 -i <音频中间WAV>，保持后续输入索引不变）。
    audio_input_span: tuple[int, int] | None = None
    if audio_input_enabled:
        audio_path = _resolve_mat_abs(mgr, task_id, materials, "audio")
        if audio_path and audio_path.exists():
            # REQ-20260929-NNN：BGM 输入有限化。滤镜级 aloop=loop=-1 是无限源，
            # ffmpeg 9 调度器在视频支路（x264 编码）慢速并发时会把音频支路提前判
            # EOF（实测 1101~1109s 处静默截断、exit 0、stderr 空 —— 20260924-001
            # 两次「导出被提前截断」失败的根因；截断点逐次抖动 = 时序竞态）。
            # 修复：demux 级循环 + 输入级 -t（有限化在 demux 层，滤镜图内无无限源；
            # 压缩包队列有界 ≈ 时长×码率，无 amovie 的解码帧队列 OOM 问题）。
            # expected 不可得（probe 失败）时回退旧 -i + aloop。
            _audio_input_lo = len(input_args)
            if expected_output_sec is not None:
                input_args += [
                    "-stream_loop", "-1",
                    "-t", f"{expected_output_sec + 0.5:.3f}",
                    "-i", str(audio_path),
                ]
                bgm_input_bounded = True
            else:
                input_args += ["-i", str(audio_path)]
                bgm_input_bounded = False
            audio_input_span = (_audio_input_lo, len(input_args))
        else:
            audio_cfg = dict(audio_cfg)
            audio_cfg["enabled"] = False
            fc["audio"] = audio_cfg
            audio_input_enabled = False

    # REQ-20260920-079：超大图片预缩放（跟随 fc.output.resolution；不动原图）
    # 720p→1280×720；1080p→1920×1080；source→1920×1080（最长边 ≤ 1920 防 OOM）
    image_tmp_paths: list[Path] = []
    if res == "720p":
        _ps_target_w, _ps_target_h = 1280, 720
    else:
        _ps_target_w, _ps_target_h = 1920, 1080

    # 当前 input_args 里有几个 -i（用于判断 bg_idx / cover_idx）
    _ps_inputs_count = sum(1 for i_, _ in enumerate(input_args) if input_args[i_] == "-i")

    def _replace_input_path(old: Path, new: Path) -> None:
        """把 input_args 中 old 路径替换成 new（按 -i 之后的下个 token 匹配）。"""
        for i_, tok in enumerate(input_args):
            if tok == "-i" and i_ + 1 < len(input_args) and input_args[i_ + 1] == str(old):
                input_args[i_ + 1] = str(new)
                return

    if layout["bg"]["enabled"] and _ps_inputs_count >= 2:
        bg_abs = _resolve_mat_abs(mgr, task_id, materials, "bg")
        if bg_abs and bg_abs.exists():
            new_bg = _maybe_prescale_image(bg_abs, _ps_target_w, _ps_target_h, "bg")
            if new_bg != bg_abs:
                _replace_input_path(bg_abs, new_bg)
                image_tmp_paths.append(new_bg)
    if cover_input_enabled:
        cover_abs = _resolve_mat_abs(mgr, task_id, materials, "cover")
        if cover_abs and cover_abs.exists():
            new_cover = _maybe_prescale_image(cover_abs, _ps_target_w, _ps_target_h, "cover")
            if new_cover != cover_abs:
                _replace_input_path(cover_abs, new_cover)
                image_tmp_paths.append(new_cover)
    ref_mat_pre = materials.get("reference") or {}
    if ref_mat_pre.get("path"):
        ref_abs = _resolve_mat_abs(mgr, task_id, materials, "reference")
        if ref_abs and ref_abs.exists():
            new_ref = _maybe_prescale_image(ref_abs, _ps_target_w, _ps_target_h, "ref")
            if new_ref != ref_abs:
                _replace_input_path(ref_abs, new_ref)
                image_tmp_paths.append(new_ref)

    # 4. filter_complex
    chain: list[str] = []
    inputs_count = sum(1 for i, _ in enumerate(input_args) if input_args[i] == "-i")

    bg_idx = -1
    if layout["bg"]["enabled"]:
        bg_idx = 1 if inputs_count >= 2 else -1
    chain.extend(_build_bg_layer_chain(bg_idx, W, H, canvas_bound))
    cur = "[bg]"

    vc = layout["video"]
    if vc["enabled"]:
        crop_expr = (
            f"crop=iw*{vc['crop_w']}/{_FINE_DESIGN_W}:ih*{vc['crop_h']}/{_FINE_DESIGN_H}:"
            f"iw*{vc['crop_x']}/{_FINE_DESIGN_W}:ih*{vc['crop_y']}/{_FINE_DESIGN_H}"
        )
        # REQ-20260923-NNN：视频层显示尺寸保持「视频源裁剪」的宽高比，不再强制 16:9。
        # 旧实现 scale={W*scale}:{H*scale} 恒为 16:9 —— 非 16:9 裁剪区（如 1580×990）
        # 在成片里被拉伸变形。新公式与「🎯 按裁剪宽度」按钮、v15 scale=crop_w/1920
        # 自动重算、「占背景图百分比」同一语义：
        #   显示宽 = W × scale（占输出画布宽的百分比），显示高 = 显示宽 × crop_h/crop_w
        # 16:9 裁剪（含全幅 1920×1080）时与旧行为逐像素一致（向后兼容）。
        _cw = max(1, int(vc["crop_w"]))
        _ch = max(1, int(vc["crop_h"]))
        sw = max(1, int(round(W * vc["scale"])))
        sh = max(1, int(round(sw * _ch / _cw)))
        chain.append(
            f"[0:v]{crop_expr},scale={sw}:{sh}:flags=lanczos,setsar=1[v]"
        )
        vx = max(0, min(W, int(vc["x"])))
        vy = max(0, min(H, int(vc["y"])))
        chain.append(f"{cur}[v]overlay=x={vx}:y={vy}[v1]")
        cur = "[v1]"

    sub_mat = materials.get("subtitle") or {}
    preview_offset_ms = int(round(max(0.0, float(preview_start)) * 1000))
    sub_input_tmp: Path | None = None
    if layout["subtitle"]["enabled"] and sub_mat.get("path"):
        sub_path = _resolve_mat_abs(mgr, task_id, materials, "subtitle")
        # REQ-20260922-NNN 标记删除行（R4）：auto 字幕素材内存直供 — 不读共享
        # tmp/optimized_subs.srt（那是「自动获取」那一刻的快照，可能与上面
        # 实时解析出来的视频不同基）。时间基跟随本次实际视频：
        # optimize_compose.mp4 → 排除标记行 + 时间轴前移；rough → 只排除标记行。
        if sub_mat.get("source") == "auto":
            from slirn_home import optimize_service

            _od = optimize_service.load_optimize(mgr.tasks_dir / task_id / "outputs")
            if _od and _od.get("saved_at"):
                _shift = video_path is not None and video_path.name == "optimize_compose.mp4"
                _srt = optimize_service.build_srt(
                    _od.get("segments") or [], _od.get("line_marks") or [],
                    shift=_shift,
                    line_splits=_od.get("line_splits"),
                    split_marks=_od.get("split_marks"),
                    occurrences=_od.get("occurrences"))
                if _srt:
                    _tfh_srt = _tf.NamedTemporaryFile(
                        mode="w", suffix=".srt", encoding="utf-8",
                        delete=False, prefix="slirn_fine_subs_")
                    _tfh_srt.write(_srt)
                    _tfh_srt.close()
                    sub_path = Path(_tfh_srt.name)
                    image_tmp_paths.append(sub_path)  # 随调用方 finally 统一清理
        if sub_path and sub_path.exists():
            # REQ-20260920-099 Phase C：直接生成 ASS 字符串 + 写临时文件，
            # 用 ``ass=`` 滤镜而非 ``subtitles=`` 滤镜。理由：
            # 1. ``subtitles=`` 会用默认 PlayResX=384 / PlayResY=288 转 SRT→ASS，
            #    导致 font size 与 margin 都在 384×288 坐标里算，与实际视频像素
            #    不一致 → 中文被放大 5 倍、margin 错位 → wrap 后字符消失。
            # 2. 改用 ``ass=`` + 显式 PlayResX=W / PlayResY=H 后，所有坐标都在
            #    视频像素系，与参数卡的 x/y/font_size 完全对应。
            try:
                src_text = sub_path.read_text(encoding="utf-8-sig")
                src_entries = parse_srt(src_text)
                if preview_offset_ms > 0:
                    shifted: list[dict] = []
                    for ent in src_entries:
                        s = max(0, int(ent["start_ms"]) - preview_offset_ms)
                        e = max(0, int(ent["end_ms"]) - preview_offset_ms)
                        if e <= s:
                            continue
                        shifted.append({
                            "start_ms": s, "end_ms": e,
                            "text": ent.get("text", ""),
                        })
                    src_entries = shifted
                if not src_entries:
                    raise ValueError("SRT 解析为空（offset 后无有效条目）")
                ass_text = _srt_to_ass(src_entries, fc["font"], fc["layout"], W, H)
                _tfh = _tf.NamedTemporaryFile(
                    mode="w", suffix=".ass", encoding="utf-8",
                    delete=False, prefix="slirn_fine_ass_")
                _tfh.write(ass_text)
                _tfh.close()
                sub_filter_path = Path(_tfh.name)
                sub_input_tmp = sub_filter_path
            except Exception as e:  # noqa: BLE001
                log.warning("生成 ASS 临时文件失败，回退 subtitles= 滤镜: %s", e)
                sub_filter_path = sub_path
                fs = _ass_force_style(fc["font"], fc["layout"])
                sub_safe = _ffmpeg_filter_path(sub_filter_path)
                chain.append(
                    f"{cur}subtitles='{sub_safe}':force_style='{fs}':si=0[vsub]"
                )
                cur = "[vsub]"
                # 跳过下面的 ass 分支
                sub_path = None  # 触发下面的 else 分支（仅做 copy）
            if sub_path is not None and sub_input_tmp is not None:
                sub_safe = _ffmpeg_filter_path(sub_input_tmp)
                # ``ass=`` 滤镜不支持 ``si=``（只有 ``subtitles=`` 支持）。
                # 字幕偏移已在生成 ASS 文本时通过 ``start_ms/end_ms`` 减 preview_offset 生效。
                chain.append(
                    f"{cur}ass='{sub_safe}'[vsub]"
                )
                cur = "[vsub]"
        else:
            chain.append(f"{cur}copy[vsub]")
            cur = "[vsub]"
    else:
        chain.append(f"{cur}copy[vsub]")
        cur = "[vsub]"

    cover_idx = -1
    if cover_input_enabled:
        if bg_idx >= 0:
            cover_idx = 2 if inputs_count >= 3 else -1
        else:
            cover_idx = 1 if inputs_count >= 2 else -1
    if cover_idx >= 0:
        chain.append(
            f"[{cover_idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setpts=PTS-STARTPTS,fps=30,setsar=1[intro]"
        )
        chain.append(f"[intro]{cur}concat=n=2:v=1:a=0[vout]")
        cur = "[vout]"

    if (out_w, out_h) != (W, H):
        # REQ-20261021-139 / D5：缩放分支的输入标签必须用「当前标签」cur ——
        # [vout] 只在启用封面（concat 分支）时存在；无封面时上游标签是 [vsub]，
        # 硬编码 [vout] 会让 ffmpeg 报 "output 0 (vsub) unconnected"（720p + 无封面必现）。
        # 有封面时 cur 恰为 [vout]，生成的滤镜串与旧版逐字相同。
        chain.append(f"{cur}scale={out_w}:{out_h}:flags=lanczos,setsar=1[vfinal]")
        cur = "[vfinal]"

    audio_idx = inputs_count - 1 if audio_input_enabled else -1
    if cover_input_enabled:
        # REQ-20260920-096：用「silence prefix + [0:a]」concat 替代 adelay，彻底避开
        # PTS 错位。silence_idx 紧跟在 cover 之后，与 cover 时长一致，保证 concat 后
        # voice 总长 = cover_dur + sliced_input_audio_duration。
        chain.append(
            f"[{silence_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[ss];"
            f"[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[v_raw];"
            f"[ss][v_raw]concat=n=2:v=0:a=1[voice]"
        )
    else:
        # 无 cover：保留原有的 asetpts 防 amix 误读
        chain.append("[0:a]asetpts=PTS-STARTPTS,volume=1.0[voice]")
    # REQ-20260920-095：BGM fade_out 起算时间必须指向「音频总时长 - fade_out」，
    # 不能再写 st=0。原 st=0 让两条 afade（in/out）都从 0 起，第二条覆盖第一条，
    # 导致 t≥1s 后 BGM 整段静音、amix 输出基本只剩 voice（实测 mean_volume -23.3 dB
    # ≈ voice 单跑；BGM 段 1-9s 实测 -91 dB ≈ 数字 0）。
    #
    # REQ-20260921-NNN：BGM 实际总时长 = 视频时长 + 片头封面时长。
    # - amix=inputs=2:duration=first → 输出时长 = voice 总长
    # - voice = silence(cover_dur) + video[0:a]（有 cover 时，见上面 concat 链）
    # - 完整导出 duration=None → probe 拿到的是源视频时长，不含 cover_dur
    # → 加 cover_dur（仅当 cover_input_enabled 时）让 fade_out 起算对齐
    #   「音频总时长 - fade_out」= cover_dur + video_dur - fade_out。
    # preview 时 duration 已经是「预览段」秒数（如 10s），与 cover_dur 无关 → 不加。
    audio_total_duration: float | None = duration if duration is not None else _probe_sec
    # expected_output_sec 与 _probe_sec 已在 3a 块提前算好（REQ-20260929-NNN —
    # bg 图输入 / color 画布有限化需要边界；此处不再重复 probe）。
    # 仅「完整导出 + cover 启用」场景加 cover_dur；预览/区间导出已自带完整长度
    if cover_input_enabled and duration is None:
        try:
            _cover_dur = float(layout["cover"].get("duration", 0.0))
            if _cover_dur > 0 and audio_total_duration is not None:
                audio_total_duration += _cover_dur
                log.info("REQ-20260921-NNN: BGM fade_out 时长含 cover_dur（%ss）→ 总时长 %ss",
                         _cover_dur, audio_total_duration)
        except Exception:
            pass

    def _calc_fade_out_st(fade_dur: float) -> float:
        """afade=t=out 的 st 必须靠近流末尾；总时长未知时降级 st=0 但打 warning。"""
        if audio_total_duration is None:
            log.warning(
                "REQ-095: 无法确定 audio 总时长，fade_out st=0（潜在静音段），"
                "建议传 duration 或 ffprobe 改异步预热"
            )
            return 0.0
        st = max(0.0, audio_total_duration - fade_dur)
        if st <= 0.01:
            log.warning(
                "REQ-095: fade_out (%ss) ≥ audio_total_duration (%ss)，st=%s，"
                "退化为 st=0 与 fade_in 重叠",
                fade_dur, audio_total_duration, st,
            )
        return st

    if audio_input_enabled and audio_idx >= 0:
        vol_db = _fine_volume_db(audio_cfg)  # REQ-20260922-NNN：dB 衰减（兼容旧线性 volume）
        fade_in = float(audio_cfg.get("fade_in", 0.0))
        fade_out = float(audio_cfg.get("fade_out", 0.0))
        # REQ-20260920-080 修复：label [bgm] 必须紧接过滤器链尾部，不能 ",[bgm]"
        # （之前用 list + ",".join 会把 label 当成 filter name → No such filter: ''）
        # REQ-20260929-NNN：stream_loop 输入已自带 demux 级循环 + 输入级 -t 有限化，
        # 不再需要（也不能要）滤镜级无限 aloop —— 见上方 BGM 输入块注释。
        if bgm_input_bounded:
            bgm_chain = f"[{audio_idx}:a]volume={vol_db:.1f}dB"
        else:
            bgm_chain = f"[{audio_idx}:a]aloop=loop=-1:size=2e9,volume={vol_db:.1f}dB"
        if fade_in > 0:
            bgm_chain += f",afade=t=in:st=0:d={fade_in:.2f}"
        if fade_out > 0:
            _fo_st = _calc_fade_out_st(fade_out)
            bgm_chain += f",afade=t=out:st={_fo_st:.2f}:d={fade_out:.2f}"
        bgm_chain += "[bgm]"
        chain.append(bgm_chain)
        chain.append(
            "[voice][bgm]amix=inputs=2:duration=first:normalize=0[aout]"
        )
    else:
        chain.append("[voice]anull[aout]")

    filter_complex = ";\n".join(chain)
    return {
        "ok": True,
        "input_args": input_args,
        "filter_complex": filter_complex,
        "final_map": cur,
        "sub_input_tmp": sub_input_tmp,
        "image_tmp_paths": image_tmp_paths,  # REQ-20260920-079：预缩临时文件
        "out_w": out_w,
        "out_h": out_h,
        # REQ-20260923-NNN：预期输出秒数（含 cover 前缀；None = 未知）
        "expected_output_sec": expected_output_sec,
        # REQ-20260929-NNN（两进程导出）：BGM 合并图元信息。
        # audio_merge=True（图里有 [voice][bgm]amix）且完整导出且 expected 可得时，
        # _run_fine_render_locked 拆「音频段→无损WAV + 视频段WAV直通」两段跑，
        # 根治音频支路被调度器静默截断的竞态（详见 _split_fine_filter_graphs 注释）。
        "audio_merge": bool(audio_input_enabled and audio_idx >= 0),
        "audio_input_idx": audio_idx if (audio_input_enabled and audio_idx >= 0) else -1,
        "audio_input_span": audio_input_span,
    }


def _predict_audio_path(fc: dict, audio_total_duration: float | None = None) -> dict:
    """REQ-20260920-090：根据 fc 状态预测合成路径里 BGM 的处理分支（不跑 ffmpeg、不创建临时文件）。

    复刻 _assemble_fine_filter 中 inputs_count + audio_idx + BGM filter 链的计算逻辑
    —— 但不调用 ffmpeg、不创建临时字幕/预缩文件。供 /slirn/api/diagnose_bgm 同步调用。

    audio_total_duration: 若调用方已探测到源视频时长（秒），传入则让 fade_out 起算时间
        与 _assemble_fine_filter 完全一致（= duration - fade_out）。None 时退回 st=0 并标注。
        REQ-20260920-095：诊断字符串必须与实跑路径同形，避免前端误导。
    """
    layout = fc.get("layout") or {}
    materials = fc.get("materials") or {}
    audio_cfg = fc.get("audio") or {}

    # 1. 算 inputs_count（video 始终 1；bg/cover/audio 各可选）
    inputs_count = 1  # video
    if layout.get("bg", {}).get("enabled") and (materials.get("bg") or {}).get("path"):
        inputs_count += 1
    cover_input_enabled = bool(
        layout.get("cover", {}).get("enabled")
        and float(layout.get("cover", {}).get("duration", 0)) > 0
        and (materials.get("cover") or {}).get("path")
    )
    if cover_input_enabled:
        inputs_count += 1
    audio_input_enabled = bool(
        audio_cfg.get("enabled") and (materials.get("audio") or {}).get("path")
    )
    if audio_input_enabled:
        inputs_count += 1

    # 2. audio_idx = inputs_count - 1（audio 始终最后）
    audio_idx = inputs_count - 1 if audio_input_enabled else -1

    # 3. 5 个元素的 will_render 状态
    elements = {
        "video": {
            "layout_enabled": bool(layout.get("video", {}).get("enabled", False)),
            "material_path": (materials.get("video") or {}).get("path", ""),
            "will_render": bool(
                layout.get("video", {}).get("enabled", False)
                and (materials.get("video") or {}).get("path")
            ),
        },
        "subtitle": {
            "layout_enabled": bool(layout.get("subtitle", {}).get("enabled", False)),
            "material_path": (materials.get("subtitle") or {}).get("path", ""),
            "will_render": bool(
                layout.get("subtitle", {}).get("enabled", False)
                and (materials.get("subtitle") or {}).get("path")
            ),
        },
        "cover": {
            "layout_enabled": bool(layout.get("cover", {}).get("enabled", False)),
            "material_path": (materials.get("cover") or {}).get("path", ""),
            "will_render": cover_input_enabled,
        },
        "bg": {
            "layout_enabled": bool(layout.get("bg", {}).get("enabled", False)),
            "material_path": (materials.get("bg") or {}).get("path", ""),
            "will_render": bool(
                layout.get("bg", {}).get("enabled", False)
                and (materials.get("bg") or {}).get("path")
            ),
        },
        "audio": {
            "layout_enabled": bool(audio_cfg.get("enabled", False)),
            "material_path": (materials.get("audio") or {}).get("path", ""),
            "will_render": audio_input_enabled,
            "volume_db": _fine_volume_db(audio_cfg),
        },
    }

    # 4. 预测 audio filter（与 _assemble_fine_filter 行 2122-2173 一致）
    #    REQ-20260920-095：fade_out 起算必须用「总时长 - fade_out」；
    #    调用方若传入 audio_total_duration，则与实跑路径完全一致；
    #    未传则按 st=0 兜底并在 why_no_bgm 中注明「diagnose-only fallback」。
    #
    #    REQ-20260921-NNN：cover 启用时，BGM 总时长 = video_dur + cover_dur。
    #    诊断器是「完整导出路径」（duration=None）的镜像 → 加 cover_dur；
    #    预览 / 区间导出是显式 duration，已含完整长度 → 不加（与 assemble 对齐）。
    effective_audio_total_duration = audio_total_duration
    if cover_input_enabled and effective_audio_total_duration is not None:
        try:
            _cover_dur = float(layout.get("cover", {}).get("duration", 0.0))
            if _cover_dur > 0:
                effective_audio_total_duration = float(effective_audio_total_duration) + _cover_dur
        except Exception:
            pass

    predicted_audio_filters = ""
    if audio_input_enabled and audio_idx >= 0:
        vol_db = _fine_volume_db(audio_cfg)  # REQ-20260922-NNN：与 assemble 同源 dB 衰减
        fade_in = float(audio_cfg.get("fade_in", 0.0))
        fade_out = float(audio_cfg.get("fade_out", 0.0))
        # REQ-20260920-080：label [bgm] 必须紧接过滤器链尾部，不能 ",[bgm]"
        # REQ-20260929-NNN：与 assemble 对称 —— stream_loop 输入（expected 可得）
        # 无 aloop，只有回退路径（probe 失败）才保留无限 aloop。
        _bgm_bounded_like = effective_audio_total_duration is not None
        if _bgm_bounded_like:
            bgm_chain = f"[{audio_idx}:a]volume={vol_db:.1f}dB"
        else:
            bgm_chain = f"[{audio_idx}:a]aloop=loop=-1:size=2e9,volume={vol_db:.1f}dB"
        if fade_in > 0:
            bgm_chain += f",afade=t=in:st=0:d={fade_in:.2f}"
        if fade_out > 0:
            # REQ-20260920-095：fade_out 起算 = total - fade_out；与 assemble 对齐
            if effective_audio_total_duration is not None:
                _fo_st = max(0.0, float(effective_audio_total_duration) - fade_out)
                bgm_chain += f",afade=t=out:st={_fo_st:.2f}:d={fade_out:.2f}"
            else:
                bgm_chain += f",afade=t=out:st=0:d={fade_out:.2f}"
        bgm_chain += "[bgm]; [voice][bgm]amix=inputs=2:duration=first:normalize=0[aout]"
        predicted_audio_filters = bgm_chain

    # 5. 诊断 why_no_bgm
    why_no_bgm = ""
    if not audio_cfg.get("enabled"):
        why_no_bgm = "fc.audio.enabled = False（未勾选 BGM）"
    elif not (materials.get("audio") or {}).get("path"):
        why_no_bgm = "materials.audio.path 缺失（未上传音频素材）"

    return {
        "ok": True,
        "elements": elements,
        "inputs_count": inputs_count,
        "audio_idx": audio_idx,
        "predicted_has_bgm": bool(audio_input_enabled and audio_idx >= 0),
        "predicted_audio_filters": predicted_audio_filters,
        "why_no_bgm": why_no_bgm,
    }


def _run_fine_render(
    task_id: str,
    mgr,
    output_path: Path,
    duration: float | None,
    preview_start: float = 0.0,
    *,
    combo: dict | None = None,
) -> dict:
    """REQ-20260919-061 Phase B：调 ffmpeg 渲染精剪视频（同步版本，预览/短任务用）。

    REQ-20260919-074：filter_complex 组装抽到 `_assemble_fine_filter`，本函数
    只负责 ffmpeg subprocess.run + 错误处理。timeout=120（预览 ≤30 秒足够）。
    1-3 小时的导出任务请走 `export_fine_video` → 后台线程 + 进度轮询。

    REQ-20260920-098：新增 keyword-only 参数 ``combo``（dict | None），透传到
    ``_assemble_fine_filter``。``None`` 时仅依赖 fc.json（向后兼容）。
    """
    import subprocess

    asm = _assemble_fine_filter(task_id, mgr, duration, preview_start, combo=combo)
    if not asm.get("ok"):
        return asm
    sub_input_tmp = asm["sub_input_tmp"]
    image_tmp_paths = asm.get("image_tmp_paths") or []  # REQ-20260920-079

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # REQ-20260923-NNN：-t 确定性上界替代 -shortest（根因详见
    # _run_fine_render_locked 注释：无限视频流 + -shortest 在 I/O 拥塞下被
    # 静默截断且 exit 0）。expected 未知时退回 -shortest 旧行为。
    _expected = asm.get("expected_output_sec")
    _tail_args = ["-t", f"{_expected + 0.05:.3f}"] if _expected else ["-shortest"]
    cmd = [
        "ffmpeg", "-y",
        *asm["input_args"],
        "-filter_complex", asm["filter_complex"],
        "-map", asm["final_map"],
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        *_tail_args,
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, encoding="utf-8"
        )
    except FileNotFoundError:
        return {"ok": False, "error": "系统未安装 ffmpeg，请先安装并加入 PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffmpeg 渲染超时（>120s），请缩短视频或简化滤镜"}
    finally:
        if sub_input_tmp is not None:
            try:
                sub_input_tmp.unlink(missing_ok=True)
            except Exception:
                pass
        # REQ-20260920-079：同步渲染也清理预缩临时文件
        for _p in image_tmp_paths:
            try:
                _p.unlink(missing_ok=True)
            except Exception:
                pass
    if result.returncode != 0:
        log.error("ffmpeg failed: %s", result.stderr[-2000:])
        return {"ok": False, "error": f"ffmpeg 渲染失败: {(result.stderr or '')[-300:]}"}

    # REQ-20260923-NNN：exit 0 也可能被静默截断（-shortest 陷阱），复核双流时长
    _trunc = _verify_render_duration(output_path, asm.get("expected_output_sec"))
    if _trunc:
        log.error("REQ-20260923: 预览渲染产物被截断: %s", _trunc)
        return {"ok": False, "error": _trunc}

    return {"ok": True, "path": str(output_path.relative_to(mgr.tasks_dir.parent))
            if output_path.is_absolute() else str(output_path)}


# =====================================================================
# REQ-20260919-074：精剪·导出最终视频 → 异步后台任务 + 进度展示
# =====================================================================

@dataclass
class _RenderJob:
    """单次精剪渲染任务的状态容器（in-process 内存表，不持久化）。

    字段：
        job_id: 全局唯一 ID（job_<ts_ms>_<pid>）
        task_id: 来源任务 ID
        state: queued / running / done / failed / cancelled
        started_at / finished_at: monotonic 时间戳（用于算 elapsed）
        wall_started_at / wall_finished_at: time.time() 时间戳（用于显示）
        elapsed_sec: 已用秒数（每 0.5 秒刷新）
        progress_pct: 0-100（来自 ffmpeg out_time_ms / ffprobe total）
        progress_time_ms: 当前已编码毫秒
        total_duration_ms: 源视频总毫秒
        speed_x: ffmpeg speed=2.5x
        eta_sec: 预计剩余秒数（-1 表示未知）
        error: 失败时 stderr 末尾 500 字符
        output_url: 成功时的下载链接
        stderr_tail: ffmpeg stderr 末尾（REQ-20260923-NNN 诊断截断/失败用）
        proc: subprocess.Popen，用于 cancel
    """
    job_id: str
    task_id: str
    state: str = "queued"
    started_at: float = 0.0
    finished_at: float = 0.0
    wall_started_at: float = 0.0
    wall_finished_at: float = 0.0
    elapsed_sec: float = 0.0
    progress_pct: float = 0.0
    progress_time_ms: int = 0
    total_duration_ms: int = 0
    speed_x: float = 0.0
    eta_sec: float = -1.0
    error: str = ""
    output_url: str = ""
    stderr_tail: str = ""
    proc: Any = None


_JOB_REGISTRY: dict[str, _RenderJob] = {}
_JOB_LOCK = threading.Lock()
_JOB_TTL_SEC = 300  # 完成后保留 5 分钟，便于前端最后一次查询拿到结果
# REQ-20260923-NNN：跨任务精剪导出串行门。
# 实测（20260916-004 / 20260916-001 双任务并发复现）：两个 ffmpeg x264 同机并发
# 把彼此拖到 ~1.2x，而「慢速编码 + 输入停摆」正是 -shortest 静默截断的必要条件
# （详见 _run_fine_render_locked 注释）。串行既防截断，总墙钟也反而更短
# （2 个串行 @~4x ≈ 33 分钟 vs 并发 @~1.2x ≈ 110 分钟）。排队期间 job.state
# 保持 "queued"，render_status 照常可查；同任务重复导出仍被端点直接拒绝。
_FINE_RENDER_GATE = threading.Lock()


def _cleanup_stale_jobs(mgr) -> int:
    """清理已完成且超过 TTL 的 job。返回清理数量。线程安全。

    REQ-20260920-084：清理内存时同步删 .export_job.json。
    """
    now = time.time()
    removed = 0
    with _JOB_LOCK:
        for jid in list(_JOB_REGISTRY.keys()):
            j = _JOB_REGISTRY[jid]
            if j.finished_at and (now - j.wall_finished_at) > _JOB_TTL_SEC:
                task_id_to_clean = j.task_id
                del _JOB_REGISTRY[jid]
                removed += 1
                # REQ-20260920-084：内存清理时同步删 .export_job.json
                try:
                    _delete_active_export_job(mgr, task_id_to_clean)
                except Exception:
                    pass
    return removed


# REQ-20260920-084：task 级 active export job 落盘文件（页面刷新后回显用）
def _active_export_job_path(mgr, tid: str) -> Path:
    """REQ-20260920-084：task 级 export job 状态文件路径。"""
    return mgr.tasks_dir / tid / "outputs" / ".export_job.json"


def _write_active_export_job(mgr, tid: str, job_id: str, state: str) -> None:
    """REQ-20260920-084：写 job_id 到 task 级文件，供页面刷新后回查。"""
    p = _active_export_job_path(mgr, tid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        p.write_text(
            _json.dumps(
                {"job_id": job_id, "state": state, "started_at": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("[REQ-084] 写 .export_job.json 失败: %s", e)


def _read_active_export_job(mgr, tid: str) -> dict | None:
    """REQ-20260920-084：读 task 级 .export_job.json；解析失败返回 None。"""
    p = _active_export_job_path(mgr, tid)
    if not p.exists():
        return None
    try:
        import json as _json
        d = _json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _delete_active_export_job(mgr, tid: str) -> None:
    """REQ-20260920-084：删 task 级文件（job 终态 5 分钟后 / 用户取消 / 失败时）。"""
    p = _active_export_job_path(mgr, tid)
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass


def _kill_proc_with_grace(proc, grace_sec: float = 5.0) -> None:
    """SIGTERM → 等 grace_sec → SIGKILL 兜底。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        return
    try:
        proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except Exception:
            pass


def _probe_video_duration_ms(mgr, tid: str) -> int:
    """ffprobe 拿源视频时长（毫秒）。失败返回 0。

    REQ-20260922-NNN：走 _effective_fine_video_path（auto 来源实时解析 —
    优化成片优先），与 _assemble_fine_filter 实际用的视频保持同一个文件。
    """
    try:
        fc = _get_fine_compose(mgr, tid)
        vp = _effective_fine_video_path(mgr, tid, fc["materials"])
        if not vp or not Path(vp).exists():
            return 0
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(vp)],
            capture_output=True, text=True, timeout=10, encoding="utf-8",
        )
        sec = float((pr.stdout or "0").strip() or "0")
        return int(sec * 1000)
    except Exception:
        return 0


def _verify_render_duration(out_path: Path, expected_sec: float | None) -> str:
    """REQ-20260923-NNN：ffprobe 复核产物双流时长 ≥ 预期 98%，防静默截断。

    ffmpeg 对「无限视频流（封面/BG -loop 1）+ 有限音频流」组合在 I/O 拥塞下
    可能让 -shortest 提前判音频 EOF：exit 0、moov 完整、双流可播，但时长只剩
    零头（20260916-004 实测 69 分钟源反复只导出 18 分钟，无任何报错）。
    returncode==0 不可信，必须以实际流时长为准。

    返回空串 = 通过（expected 未知 / 文件缺失 / probe 失败时跳过，只打日志）；
    非空 = 截断描述（可直接作为 job.error / {"ok": False} 的 error）。
    """
    if not expected_sec or expected_sec <= 0:
        return ""
    if not out_path.exists():
        # 文件缺失：真实场景 exit 0 却无产物属于异常，但无法区分（测试桩 /
        # 网络盘延迟），统一告警跳过，交给上层既有逻辑处理
        log.warning("REQ-20260923: 渲染产物不存在，跳过时长校验: %s", out_path)
        return ""
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
             "-of", "json", str(out_path)],
            capture_output=True, text=True, timeout=60, encoding="utf-8",
        )
        durs: dict[str, float] = {}
        for s in (json.loads(pr.stdout or "{}").get("streams") or []):
            try:
                if s.get("codec_type") and s.get("duration"):
                    durs[s["codec_type"]] = float(s["duration"])
            except (TypeError, ValueError):
                pass
        thr = float(expected_sec) * 0.98
        for _kind, _dur in durs.items():
            if _dur < thr:
                _kind_cn = {"video": "视频", "audio": "音频"}.get(_kind, _kind)
                return (f"导出被提前截断：{_kind_cn}流 {_dur:.1f}s < 预期 "
                        f"{expected_sec:.1f}s（可能因系统负载过高），请重试导出")
    except Exception as e:  # noqa: BLE001
        log.warning("REQ-20260923: 输出时长校验失败（跳过）: %s", e)
    return ""


# REQ-20260929-NNN（两进程导出）：音频子图判别标记 —— 这些 label 只出现在音频链
# （cover 静音 concat / BGM volume / amix）里；视频链（画布/裁剪/overlay/ass/
# 封面 concat）不会引用。
_FINE_AUDIO_GRAPH_MARKERS = ("[voice]", "[bgm]", "[aout]", "[ss]", "[v_raw]")
# 命名 label（[ss]/[v_raw]/[bg_b] 等；[0:v]/[3:a] 这类输入垫以数字开头，不匹配）
_FINE_LABEL_RE = re.compile(r"\[[A-Za-z_][A-Za-z0-9_]*\]")


def _split_fine_filter_graphs(filter_complex: str) -> tuple[str, str] | None:
    """REQ-20260929-NNN（两进程导出）：把 filter_complex 拆成音频/视频两个闭合子图。

    背景：ffmpeg 9 在「音频合并图（concat/amix）+ x264 慢速视频支路」同一
    filter_complex 并发时，调度器会把音频支路提前判 EOF —— 全片（4278s）在
    ~1050-1230s 静默截断、exit 0、stderr 空、截断点逐次抖动（时序竞态；
    20260924-001 两次「导出被提前截断」失败的根因，错误提示里的「系统负载过高」
    是错误归因 —— 空闲机器离线必现）。单进程内所有修法实测无效：线程/队列
    flags、采样率、atrim、amix duration=longest（假阳性静音续命）；amovie
    循环全片 OOM；stream_loop 输入 -t 1500 短测通过但全片仍在 1233.7s 截断。
    唯一经全片实测稳定的架构（本函数服务的目标）：
      pass1 音频子图单独跑（4278s 仅 13 秒）→ 无损 WAV（pcm_s16le）
      pass2 视频子图 + [wav:a]anull 直通（音频链单输入、无合并）→ 最终 mp4

    判别规则：含 _FINE_AUDIO_GRAPH_MARKERS 任一 label、或以 [N:a] 输入垫开头
    的 chain 元素归音频图，其余归视频图。chain 元素以 ";\n" join；音频 cover
    静音元素内部的裸 ";"（无换行）不会被拆开。

    返回 (fc_audio, fc_video)；任一侧为空、或两图共享 label（跨图依赖，拆开
    会产生 dangling pad）时返回 None —— 调用方回退单进程旧路径。
    """
    segs = [s.strip() for s in filter_complex.split(";\n") if s.strip()]
    audio: list[str] = []
    video: list[str] = []
    for s in segs:
        if (any(m in s for m in _FINE_AUDIO_GRAPH_MARKERS)
                or re.match(r"^\[\d+:a\]", s)):
            audio.append(s)
        else:
            video.append(s)
    if not audio or not video:
        return None
    # 闭合校验：一张子图定义/引用的命名 label 出现在另一张里 = 跨图依赖
    a_labels = set(_FINE_LABEL_RE.findall("".join(audio)))
    v_labels = set(_FINE_LABEL_RE.findall("".join(video)))
    if a_labels & v_labels:
        return None
    return ";\n".join(audio), ";\n".join(video)


def _run_fine_render_async(job: _RenderJob, tid: str, mgr, out_path: Path,
                          exec_id: str = "", outputs_dir: Path | None = None,
                          preview_start: float = 0.0, duration: float | None = None,
                          *, combo: dict | None = None) -> None:
    """REQ-20260923-NNN：跨任务串行门 — 同一时刻只允许一个精剪导出编码。

    实测（20260916-004 / -001 并发复现）：两个 ffmpeg x264 同机并发把彼此拖到
    ~1.2x，慢速编码 + 输入停摆正是 -shortest 静默截断的触发条件（根因详见
    _run_fine_render_locked 注释）；串行后每个任务回到 ~4x，总墙钟反而大幅缩短。
    排队期间 job.state 保持 "queued"（render_status 的 elapsed 走 elapsed_sec=0），
    拿到门后才置 running 并开始计时。预览（_run_fine_render，≤120s）不经过此门。
    """
    with _FINE_RENDER_GATE:
        _run_fine_render_locked(job, tid, mgr, out_path, exec_id=exec_id,
                                outputs_dir=outputs_dir, preview_start=preview_start,
                                duration=duration, combo=combo)


def _run_fine_render_locked(job: _RenderJob, tid: str, mgr, out_path: Path,
                            exec_id: str = "", outputs_dir: Path | None = None,
                            preview_start: float = 0.0, duration: float | None = None,
                            *, combo: dict | None = None) -> None:
    """REQ-20260919-074：后台 daemon 线程跑 ffmpeg（1-3 小时不再超时）。

    写入 job.state/progress_pct/elapsed_sec/speed_x/eta_sec/progress_time_ms/
    total_duration_ms/error/output_url 等字段；前端 GET /render_status 读取。

    REQ-20260920-081：exec_id 与 outputs_dir 由 export_fine_video endpoint 传入，
    用于在多出口（assemble 失败 / FileNotFound / cancelled / returncode 非 0 /
    returncode 0）都补 record_finish。失败也写（status="failed"）。

    REQ-20260920-091：开始时间 + 时长（不导完整视频）。
    缺省 preview_start=0.0 / duration=None → 等价于跑完整视频。

    REQ-20260920-098：新增 keyword-only 参数 ``combo``（dict | None），透传到
    ``_assemble_fine_filter``。``None`` 时仅依赖 fc.json（向后兼容）。
    """
    job.state = "running"
    job.started_at = time.monotonic()
    job.wall_started_at = time.time()

    # REQ-20260920-091：用调用方传入的 time 参数（无值则导出完整视频，保持向后兼容）
    # REQ-20260920-098：combo 也透传
    asm = _assemble_fine_filter(tid, mgr, duration=duration, preview_start=preview_start,
                                combo=combo)
    if not asm.get("ok"):
        job.state = "failed"
        job.error = asm.get("error", "filter 组装失败")
        job.finished_at = time.monotonic()
        job.wall_finished_at = time.time()
        # REQ-20260920-081：assemble 失败也写历史
        # REQ-20260920-089：本地 import（daemon 线程无模块级 execution_history）
        from slirn_home import execution_history as _eh_async_asm
        if exec_id and outputs_dir is not None:
            try:
                _eh_async_asm.record_finish(
                    outputs_dir, exec_id, success=False,
                    error=str(asm.get("error") or "filter 组装失败")[:500],
                )
            except Exception:
                pass
        # REQ-20260920-084：assemble 失败删 .export_job.json
        try: _delete_active_export_job(mgr, tid)
        except Exception: pass
        return

    sub_input_tmp = asm["sub_input_tmp"]
    image_tmp_paths = asm.get("image_tmp_paths") or []  # REQ-20260920-079

    # 拿总时长（百分比 + ETA 计算依赖）
    job.total_duration_ms = _probe_video_duration_ms(mgr, tid)

    # ffmpeg 命令（关键差异：-progress pipe:1 -nostats）
    # REQ-20260923-NNN：-t 确定性上界替代 -shortest。
    # 根因（20260916-004 反复只导出 18 分钟）：滤镜图视频分支无限（封面/背景
    # 图 -loop 1 + overlay eof_action=pass），音频分支有限（源音轨 → concat 静
    # 音），输出长度全靠 -shortest 以音频 EOF 收口。并发重编码把速度拖到 ~1.2x
    # 后输入读在源 ~1086-1091s 处停摆数分钟 → ffmpeg muxer 同步队列防死锁策略
    # 提前判定 EOF：写出合法 moov、exit 0、双流可播 — 服务据 returncode==0 标
    # 记成功，实际只剩零头。显式 -t（预期时长 + 50ms）后输出长度不再依赖 EOF
    # 判定；expected 未知（probe 失败）时退回 -shortest 旧行为。完成后另有
    # _verify_render_duration 复核双流时长兜底。
    _expected_sec = asm.get("expected_output_sec")
    _tail_args = (["-t", f"{_expected_sec + 0.05:.3f}"] if _expected_sec
                  else ["-shortest"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        *asm["input_args"],
        "-filter_complex", asm["filter_complex"],
        "-map", asm["final_map"],
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        *_tail_args,
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        str(out_path),
    ]

    # ===== REQ-20260929-NNN（两进程导出）：完整导出 + BGM 合并图拆两段 =====
    # 根因（20260924-001 两次「导出被提前截断」全片终验结论）：「音频合并图
    # （concat/amix）+ x264 慢速视频支路」在同一 filter_complex 里并发时，
    # ffmpeg 9 调度器会把音频支路提前判 EOF（全片 ~1050-1230s 静默截断、
    # exit 0、stderr 空、截断点逐次抖动 = 时序竞态）。单进程内所有修法实测
    # 无效：线程/队列 flags、采样率、atrim、amix duration=longest（假阳性
    # 静音续命）；amovie 循环全片 OOM；stream_loop 输入 -t 1500 短测通过但
    # 全片仍在 1233.7s 截断（短测通过只是竞态侥幸）。全片实测稳定的架构：
    #   pass1 音频子图单独跑（4278s 仅 13 秒）→ 无损 WAV（pcm_s16le）
    #   pass2 视频子图 + [wav:a]anull 直通（音频链单输入、无合并）→ 最终 mp4
    # 适用条件：完整导出（duration=None）+ BGM 合并 + expected 可得 + 子图闭合
    # （_split_fine_filter_graphs 判定）。预览/区间导出（时长有限，远低于竞态
    # 触发区间）、无 BGM、expected 未知或拆分失败 → 走下方单进程旧路径。
    import tempfile as _tempfile
    _split = None
    if (duration is None and preview_start == 0.0
            and _expected_sec is not None
            and asm.get("audio_merge")
            and asm.get("audio_input_span")):
        _split = _split_fine_filter_graphs(asm["filter_complex"])
    cmd_audio: list[str] | None = None
    cmd_video: list[str] | None = None
    _wav_path: Path | None = None
    if _split is not None:
        # 音频中间 WAV 与输出同目录（4278s 立体声 pcm_s16le ≈ 800MB，finally
        # 删除；进程被硬杀残留时由下次导出前的清扫兜底 —— 导出门 _FINE_RENDER_GATE
        # 全局串行，清扫时不可能有并行导出在写同前缀文件）
        try:
            for _old in out_path.parent.glob("slirn_fine_audio_*.wav"):
                try:
                    _old.unlink(missing_ok=True)
                except OSError:
                    pass
            _wav_fd, _wav_name = _tempfile.mkstemp(
                prefix="slirn_fine_audio_", suffix=".wav", dir=str(out_path.parent))
            os.close(_wav_fd)
            _wav_path = Path(_wav_name)
        except Exception:
            _wav_path = None
        if _wav_path is None:
            _split = None  # WAV 建不出来（磁盘满/权限）→ 回退单进程
        else:
            fc_audio, fc_video = _split
            _lo, _hi = asm["audio_input_span"]
            # pass2 输入：原位把 BGM 输入切片替换成 -i <wav>（后续输入索引不变，
            # fc_video 的 [N:v] 垫与 [audio_input_idx:a] 直通链都保持有效）
            _ia2 = list(asm["input_args"])
            _ia2[_lo:_hi] = ["-i", str(_wav_path)]
            cmd_audio = [
                "ffmpeg", "-y",
                *asm["input_args"],
                "-filter_complex", fc_audio,
                "-map", "[aout]",
                "-c:a", "pcm_s16le",
                "-progress", "pipe:1",
                "-nostats",
                *_tail_args,
                str(_wav_path),
            ]
            cmd_video = [
                "ffmpeg", "-y",
                *_ia2,
                "-filter_complex",
                fc_video + f";\n[{asm['audio_input_idx']}:a]anull[aout]",
                "-map", asm["final_map"],
                "-map", "[aout]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                *_tail_args,
                "-movflags", "+faststart",
                "-progress", "pipe:1",
                "-nostats",
                str(out_path),
            ]
            log.info("REQ-20260929-NNN: 两进程导出（BGM 合并图）：pass1 音频→WAV + "
                     "pass2 视频+WAV 直通，expected=%.3fs", _expected_sec)

    # 启动 ffmpeg
    # REQ-20260920-077：解 Windows TextIOWrapper 8KB 缓冲卡死。
    # 原写法 `text=True, bufsize=1` 在 Windows 上无效 — `text=True` 会用
    # `io.TextIOWrapper` 包装 stdout，默认 8KB 缓冲；ffmpeg 每 ~0.4s 写一行
    # `out_time_ms=...`（~20 字节），要攒够 8KB 才喂给 `readline()`，进度条
    # 看似卡住 5-15 秒。改用 `text=False, bufsize=0`（unbuffered 给底层
    # BufferedReader），再手动重包为 `TextIOWrapper(line_buffering=True)`
    # 保证每行立即 flush。
    # REQ-20260920-089：stderr 不能用 PIPE（主循环只读 stdout，ffmpeg stderr 写满
    # OS pipe buffer ~64KB 后阻塞 → 永久死锁），REQ-089 曾改为 DEVNULL 砍掉消费端。
    # REQ-20260923-NNN：DEVNULL 把诊断信息也丢了 — 本次截断事故 exit=0 + stderr
    # 全空，排查只能黑盒复现。改为写临时文件：既不阻塞（写盘无缓冲上限）也保留
    # 现场，结束时取末尾 4KB 进 job.stderr_tail，随后删文件。临时文件创建失败时
    # 退回 DEVNULL（不阻塞导出主流程）。
    _err_file = None
    _err_path: Path | None = None
    proc = None  # 当前/最后一段 ffmpeg 进程（cancel 端点 + finally 收尾用）

    def _run_stage(stage_cmd: list[str], pct_lo: float, pct_hi: float) -> int:
        """跑一段 ffmpeg（stdout = -progress pipe:1），返回 returncode。

        REQ-20260929-NNN（两进程导出）：把原「Popen + 重包 stdout + 进度主循环」
        封装成段运行器 —— 单进程模式跑一段（pct 0-100 原语义），两段模式跑两段
        （音频段映射 0-5%、视频段 5-100%）。cancel 端点 kill job.proc 后
        readline 得 EOF 自然返回非 0 码，state 判定交给调用方；异常时 kill 当段
        进程再 raise（外层 finally 统一收尾）。
        """
        nonlocal proc
        _p = subprocess.Popen(
            stage_cmd, stdout=subprocess.PIPE,
            stderr=_err_file if _err_file is not None else subprocess.DEVNULL,
            bufsize=0,
        )
        proc = _p
        job.proc = _p
        # REQ-20260920-077：重包 stdout 为 line-buffered TextIOWrapper（原
        # `text=True` 的 8KB 默认缓冲会让 out_time_ms 行被积压，进度看似卡死）
        import io as _io_stage
        _p.stdout = _io_stage.TextIOWrapper(
            _p.stdout, encoding="utf-8", newline="\n",
            line_buffering=True,
        )
        _last = 0.0
        try:
            while True:
                line = _p.stdout.readline()
                if not line:
                    if _p.poll() is not None:
                        break
                    # ffmpeg 已退出但 stdout 关闭
                    continue
                line = line.strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key in ("out_time_ms", "out_time_us"):
                    # 兼容部分 ffmpeg 版本用 out_time_us
                    try:
                        _ms = int(val) if key == "out_time_ms" else int(val) // 1000
                        job.progress_time_ms = _ms
                        if job.total_duration_ms > 0:
                            _frac = min(1.0, _ms / job.total_duration_ms)
                            job.progress_pct = min(
                                100.0, pct_lo + (pct_hi - pct_lo) * _frac)
                    except ValueError:
                        pass
                elif key == "speed":
                    try:
                        job.speed_x = float(val.rstrip("x"))
                    except ValueError:
                        pass
                # 每 0.5 秒刷一次 elapsed/ETA（避免 dict 写太频繁）
                _now = time.monotonic()
                if _now - _last > 0.5:
                    _last = _now
                    job.elapsed_sec = _now - job.started_at
                    if job.speed_x > 0 and job.total_duration_ms > 0:
                        _remaining = max(0, job.total_duration_ms - job.progress_time_ms)
                        # speed_x = 源时长 / 墙钟时长 → 剩余墙钟 = 剩余源时长 / speed
                        job.eta_sec = _remaining / 1000.0 / job.speed_x
        except BaseException:
            if _p.poll() is None:
                _kill_proc_with_grace(_p)
            raise
        try:
            _p.wait(timeout=10)
        except Exception:
            pass
        return _p.returncode

    try:
        try:
            _fd, _err_name = _tempfile.mkstemp(prefix="slirn_fine_err_", suffix=".log")
            os.close(_fd)
            _err_path = Path(_err_name)
            _err_file = open(_err_name, "wb")
        except Exception:
            _err_file = None
            _err_path = None
        try:
            if cmd_audio is not None:
                # pass1：音频子图 → 无损 WAV（快，~330x 实时）
                _rc_a = _run_stage(cmd_audio, 0.0, 5.0)
                if _rc_a != 0:
                    # cancel（state=cancelled）优先于 failed，不覆盖
                    if job.state == "running":
                        job.state = "failed"
                        job.error = f"音频合成段 ffmpeg 异常终止（exit code {_rc_a}）"
                    return
                if job.state != "running":
                    # pass1 与 pass2 之间被取消
                    return
                # pass1 产物复核（音频段单独跑从未复现截断，fail-fast 兜底：
                # 避免把 15-25 分钟的视频段烧在一个坏 WAV 上）
                _wav_trunc = _verify_render_duration(_wav_path, _expected_sec)
                if _wav_trunc:
                    job.state = "failed"
                    job.error = f"音频合成段失败：{_wav_trunc}"
                    return
                # pass2：视频子图 + WAV 直通 → 最终 mp4
                _rc_v = _run_stage(cmd_video, 5.0, 100.0)
                if _rc_v != 0 and job.state == "running":
                    job.state = "failed"
                    job.error = f"ffmpeg 进程异常终止（exit code {_rc_v}）"
            else:
                _rc = _run_stage(cmd, 0.0, 100.0)
                if _rc != 0 and job.state == "running":
                    job.state = "failed"
                    job.error = f"ffmpeg 进程异常终止（exit code {_rc}）"
        except FileNotFoundError:
            # ffmpeg 不存在：state/error 在此设置，清理由外层 finally 统一处理
            job.state = "failed"
            job.error = "系统未安装 ffmpeg，请先安装并加入 PATH"
            return
    finally:
        # REQ-20260920-089：finally 块统一处理所有终态（cancel / done / failed /
        # 外部 kill / 异常），保证 record_finish 一定被调用，不再漏写。
        # 1) 兜底 kill 未退出的 ffmpeg（cancel / 外部 kill）
        #    REQ-20260929-NNN：两段模式下 proc 可能还没启动（FileNotFoundError）
        if proc is not None and proc.poll() is None:
            _kill_proc_with_grace(proc)

        # 2) 等 ffmpeg 真正退出 + 算 elapsed（如果还没设）
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        if not job.finished_at:
            job.finished_at = time.monotonic()
            job.wall_finished_at = time.time()
        job.elapsed_sec = job.finished_at - job.started_at

        # 3) 清理临时文件（SRT + 预缩图片）
        if sub_input_tmp:
            try: sub_input_tmp.unlink(missing_ok=True)
            except Exception: pass
        for _p in image_tmp_paths:
            try: _p.unlink(missing_ok=True)
            except Exception: pass
        # 3.6) REQ-20260929-NNN：两段模式的音频中间 WAV（大文件，必删）
        if _wav_path is not None:
            try: _wav_path.unlink(missing_ok=True)
            except Exception: pass

        # 3.5) REQ-20260923-NNN：提取 stderr 末尾（诊断截断/失败用）后清理临时文件
        if _err_path is not None:
            try:
                if _err_file is not None and not _err_file.closed:
                    _err_file.close()
                with open(_err_path, "rb") as _ef:
                    _ef.seek(0, os.SEEK_END)
                    _sz = _ef.tell()
                    _ef.seek(max(0, _sz - 4096))
                    job.stderr_tail = _ef.read().decode("utf-8", errors="replace")[-2000:]
            except Exception:
                pass
            try:
                _err_path.unlink(missing_ok=True)
            except Exception:
                pass

        # 4) 推算最终 state（如果主循环因 readline 返回空自然退出但 state 还是 running）
        if job.state == "running":
            if proc is None:
                # REQ-20260929-NNN：防御 —— state 还是 running 但进程从未启动
                job.state = "failed"
                if not job.error:
                    job.error = "渲染进程未启动（未知错误）"
            elif proc.returncode == 0:
                # REQ-20260923-NNN：exit 0 ≠ 完整。无限视频流 + 有限音频流的组合
                # 在 I/O 拥塞下 -shortest 会提前判音频 EOF：moov 完整、双流可播、
                # exit 0，但 69 分钟只剩 18 分钟（20260916-004 实测）。必须 ffprobe
                # 复核实际流时长 ≥ 预期 98%，不足则按失败处理（见 cmd 处根因注释）。
                _trunc = _verify_render_duration(out_path, asm.get("expected_output_sec"))
                if _trunc:
                    job.state = "failed"
                    job.error = _trunc
                else:
                    job.state = "done"
                    job.progress_pct = 100.0
                    # REQ-20260921-NNN-outputs-browser：output_url 必须带实际文件名
                    # （区间导出是 fine_export_t*_d*.mp4，而非 fine_export.mp4）。
                    # 用 out_path.name 透传，前端点 status 直接打开正确文件。
                    job.output_url = (
                        f"/slirn/api/video/{tid}?src=fine_export&fname={out_path.name}"
                        f"&t={int(time.time())}"
                    )
            else:
                job.state = "failed"
                if not job.error:
                    job.error = f"ffmpeg 进程异常终止（exit code {proc.returncode}）"

        # 4.5) REQ-20260923-NNN：失败时把 stderr 末尾附到 error（截断/崩溃诊断线索）
        if job.state == "failed" and job.stderr_tail:
            _stail = job.stderr_tail[-500:]
            if _stail not in job.error:
                job.error = (job.error + f"\nffmpeg stderr 尾部: {_stail}")[:1000]

        # 5) 统一 record_finish（所有路径都走这里）
        # REQ-20260920-089：本地 import execution_history（修复 REQ-081 同根 NameError BUG：
        # daemon 线程里没有模块级 execution_history 变量，原 success 分支的
        # execution_history.record_finish 永远抛 NameError 被 except 静默吞掉 → execution_history 永远 running）
        from slirn_home import execution_history as _eh
        if exec_id and outputs_dir is not None:
            try:
                if job.state == "done":
                    _eh.patch_extra(
                        outputs_dir, exec_id,
                        {"output_path": str(out_path),
                         "duration_sec": round((job.finished_at - job.started_at), 1),
                         "resolution": (asm.get("output_resolution") or "1080p")},
                    )
                    _eh.record_finish(
                        outputs_dir, exec_id, success=True, error="",
                    )
                elif job.state == "cancelled":
                    _eh.record_finish(
                        outputs_dir, exec_id, success=False,
                        error="用户取消渲染",
                    )
                else:
                    # failed / 外部 kill / 异常
                    err_msg = job.error or "ffmpeg 进程异常终止（可能被外部 kill）"
                    _eh.record_finish(
                        outputs_dir, exec_id, success=False,
                        error=str(err_msg)[:500],
                    )
            except Exception:
                pass

        # 6) 清理落盘文件（无论哪种终态都删）
        try: _delete_active_export_job(mgr, tid)
        except Exception: pass


def _get_fine_compose(mgr, task_id: str) -> dict:
    """返回 task 的 fine_compose 数据（独立 JSON 文件，不污染 Task dataclass）。

    存储位置：tasks/{tid}/fine_compose.json
    返回：含默认值的 dict（materials/layout/font/output）。

    自动迁移：磁盘数据若未带 `_schema: 2`（旧 0-1 归一化 x/y），读时把每个 layout
    的 x/y 乘以 (设计空间宽, 设计空间高) 转成像素值。_schema 字段在内存里维护，
    落盘时由 _save_fine_compose 写入。

    REQ-20260921-NNN-fix-scale-keyerror：补全每个 layout 子 dict 里缺失的字段
    （如 {"x":42} 缺 scale/enabled 等）。原版只 setdefault 顶层 key，对"key 在
    但子字段不全"的情况（典型：save_fine_layout 部分提交）会漏默认，导致渲染
    抛 KeyError → HTTP 500。
    """
    fc_path = mgr.tasks_dir / task_id / "fine_compose.json"
    fc = {}
    if fc_path.exists():
        try:
            fc = json.loads(fc_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            fc = {}
    fc.setdefault("materials", {})
    fc.setdefault("layout", {})
    fc.setdefault("output", dict(_FINE_OUTPUT_DEFAULTS))
    fc.setdefault("font", dict(_FINE_FONT_DEFAULTS))
    # REQ-20260919-061 扩展：背景音乐默认设置（独立于 layout）
    fc.setdefault("audio", dict(_FINE_AUDIO_DEFAULTS))
    # REQ-20260921-NNN：生成预览参数（独立保存 start_h/m/s + duration）
    fc.setdefault("preview", dict(_FINE_PREVIEW_DEFAULTS))
    # REQ-20260919-065：检测区域正式参数（旧任务缺该字段也兼容）
    fc.setdefault("detected_region", None)
    # layout 默认值与已存值合并（保留用户已设置的）
    for k, defaults in _FINE_LAYOUT_DEFAULTS.items():
        existing_lc = fc["layout"].get(k)
        if not isinstance(existing_lc, dict):
            # 旧任务把 layout[k] 写成非 dict（如 None / 字符串）→ 用默认值覆盖
            fc["layout"][k] = dict(defaults)
            continue
        # REQ-20260921-NNN-fix-scale-keyerror：子 dict 也补缺失字段
        # （仅补默认里有的 key，避免覆盖用户在旧版里塞进的自定义字段）。
        for dk, dv in defaults.items():
            existing_lc.setdefault(dk, dv)
        fc["layout"][k] = existing_lc

    # REQ-20260919-061 扩展：cover 旧字段（x/y/scale）迁移 —
    # 旧版封面是「右侧角标小图」，新版是「片头全屏海报」（仅需 duration 字段）。
    # 旧字段保留（数据不丢），但补一个 duration 默认值。
    cover_layout = fc["layout"].get("cover") or {}
    if "duration" not in cover_layout:
        cover_layout["duration"] = 2.0
        fc["layout"]["cover"] = cover_layout

    # REQ-20260919-062 v18：旧任务没有 crop_aspect_lock 字段 → 补默认值 True
    # （与前端默认勾选一致；不引入隐性行为变更，老数据按原渲染）
    video_layout = fc["layout"].get("video") or {}
    if "crop_aspect_lock" not in video_layout:
        video_layout["crop_aspect_lock"] = True
    fc["layout"]["video"] = video_layout

    # REQ-20260919-062 v19：旧任务 fc.font 缺 color 字段 → 补 #FFFFFF
    # （与 libass 默认 PrimaryColour 一致；不改写用户已设的值）
    _f_color = fc.get("font") or {}
    if "color" not in _f_color:
        _f_color["color"] = "#FFFFFF"
        fc["font"] = _f_color

    # REQ-20260921-NNN：老 fc.json 的 font.left_offset（正数=左偏）→ font.offset（可负）
    # 语义：老 left_offset=N（左偏 N）→ 新 offset=-N（仍是左偏 N；位置不变）
    # 已存在新字段 offset 时跳过；老字段 left_offset 保留以便回溯，下次保存时清掉。
    _f_off = fc.get("font") or {}
    if "left_offset" in _f_off and "offset" not in _f_off:
        try:
            _f_off["offset"] = -int(_f_off["left_offset"] or 0)
        except (TypeError, ValueError):
            _f_off["offset"] = 0
        fc["font"] = _f_off

    # 数字字段类型规整：JSON 不区分 int/float/str，f-string 用 .2f 时必须是数字
    # 否则报 "Unknown format code 'f' for object of type 'str'"。
    # 先做类型规整（字符串 → 数字），再做迁移（旧 0-1 → 像素）。
    _f = fc["font"]
    for k in ("size", "stroke_width", "bg_opacity", "bg_radius"):
        v = _f.get(k)
        if isinstance(v, str):
            try:
                _f[k] = float(v)
            except (TypeError, ValueError):
                _f[k] = dict(_FINE_FONT_DEFAULTS)[k]
    # layout 数字字段规整：x/y/crop_* 字符串 → 数字（最终转 int 像素），scale/duration 保留 float
    for layout in fc["layout"].values():
        for axis in ("x", "y", "crop_x", "crop_y", "crop_w", "crop_h"):
            v = layout.get(axis)
            if isinstance(v, str):
                try:
                    layout[axis] = float(v)
                except (TypeError, ValueError):
                    layout[axis] = 0  # 字符串解析失败 → 安全默认
        for axis in ("scale", "duration"):
            v = layout.get(axis)
            if isinstance(v, str):
                try:
                    layout[axis] = float(v)
                except (TypeError, ValueError):
                    pass
    # REQ-20260919-061 扩展：audio 字段（volume_db/fade_in/fade_out）字符串 → 数字
    _a = fc.get("audio") or {}
    for ak in ("volume_db", "fade_in", "fade_out"):
        v = _a.get(ak)
        if isinstance(v, str):
            try:
                _a[ak] = float(v)
            except (TypeError, ValueError):
                _a[ak] = 0.0
    # REQ-20260922-NNN：旧线性 volume(0-1) → volume_db（dB）一次性迁移落盘
    # （旧任务 fine_compose.json / 旧参数模板 apply 写入的 legacy 字段都走这里）
    if "volume" in _a:
        legacy = _a.pop("volume")
        if "volume_db" not in _a:
            _a["volume_db"] = _fine_volume_db({"volume": legacy})

    # 数据迁移：旧 _schema (None/1) → 2（x/y 与 crop_* 都由 0-1 转像素）
    # 此时所有相关字段已是数字（float 或 int），可直接判定。
    schema = int(fc.get("_schema") or 0)
    if schema < _FINE_LAYOUT_SCHEMA:
        for layout in fc["layout"].values():
            for axis, design_size in (
                ("x", _FINE_DESIGN_W), ("y", _FINE_DESIGN_H),
                ("crop_x", _FINE_DESIGN_W), ("crop_y", _FINE_DESIGN_H),
                ("crop_w", _FINE_DESIGN_W), ("crop_h", _FINE_DESIGN_H),
            ):
                v = layout.get(axis)
                if isinstance(v, (int, float)) and 0.0 <= v <= 1.0:
                    # 旧归一化坐标 → 像素（整数）
                    layout[axis] = int(round(v * design_size))
        fc["_schema"] = _FINE_LAYOUT_SCHEMA
        # 落盘（atomic write，迁移一次即可）
        try:
            _save_fine_compose(mgr, task_id, fc)
        except Exception:  # noqa: BLE001 — 迁移失败不应阻断功能
            log.warning("fine_compose 迁移落盘失败: %s", task_id)

    # 迁移后类型再规整：x/y/crop_* 强制 int（像素，纯 float 也转 int）
    for layout in fc["layout"].values():
        for axis in ("x", "y", "crop_x", "crop_y", "crop_w", "crop_h"):
            v = layout.get(axis)
            if isinstance(v, float):
                v = int(round(v))
            elif not isinstance(v, int):
                v = 0  # 兜底
            layout[axis] = v
    return fc



def _save_bg_detect_cache(mgr, task_id: str, result: dict) -> None:
    """REQ-20260919-062 v8 用户反馈：检测后把结果存到 fine_compose.bg_detect_cache，
    下次打开精剪面板直接渲染缓存值，无需重新扫描。

    REQ-20260919-065 升级：双写到 bg_detect_cache（向后兼容） + detected_region（正式参数）。

    缓存字段（与 _result_payload 输出对齐 + 算法/时间戳）：
      - x, y, width, height, center_x, center_y, corners
      - pixel_count, image_native_w/h
      - algorithm, threshold, detected_color, color_tolerance
      - detected_at (ISO 时间戳)
    """
    try:
        fc = _get_fine_compose(mgr, task_id)
        cache = {
            "x": result.get("x"),
            "y": result.get("y"),
            "width": result.get("width"),
            "height": result.get("height"),
            "center_x": result.get("center_x"),
            "center_y": result.get("center_y"),
            "corners": result.get("corners"),
            "pixel_count": result.get("pixel_count"),
            "image_native_w": result.get("image_native_w"),
            "image_native_h": result.get("image_native_h"),
            "algorithm": result.get("algorithm"),
            "threshold": result.get("threshold"),
            "detected_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 颜色字段：仅在算法返回时存（pixel 算法没颜色）
        if "detected_color" in result:
            cache["detected_color"] = list(result["detected_color"])
        if "color_tolerance" in result:
            cache["color_tolerance"] = result["color_tolerance"]
        fc["bg_detect_cache"] = cache
        # REQ-20260919-065：同一份内容也写到 detected_region（正式参数位置）
        fc["detected_region"] = cache
        _save_fine_compose(mgr, task_id, fc)
    except Exception:
        # 缓存失败不影响主流程（接口已返回成功结果）
        pass


def _save_fine_compose(mgr, task_id: str, fc: dict) -> None:
    """写回 fine_compose.json。

    REQ-20261022-145：顺带把参数**自动**另存一份到全局快照
    （tasks/_global_fine_params.json）—— 换任务 / 换机器 / 新系统时
    可直接沿用最新值，微调即可。快照写失败不影响本函数的主流程。
    """
    fc_path = mgr.tasks_dir / task_id / "fine_compose.json"
    fc_path.parent.mkdir(parents=True, exist_ok=True)
    fc_path.write_text(
        json.dumps(fc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        from slirn_home import global_params as _gp
        _gp.save_snapshot(mgr.repo_root, _gp.KIND_LONG, fc)
    except Exception:  # noqa: BLE001 — 快照失败不影响保存
        pass

