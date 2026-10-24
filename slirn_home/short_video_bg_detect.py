"""REQ-20261013-105：短视频精剪 bg 白底检测（v1）→ REQ-20261018-166 对齐长视频。

长视频口径（app.py detect_bg_white_area 端点）搬运：
- 检测对象 = **背景图素材**（params.materials.bg；此前 v1 从粗剪视频首帧抽，
  与「检测背景图上放视频的区域」语义不符，且看不到透明抠图）
- 算法可选：
  - center_expand 中心扩展（默认）——中心 patch 平均色为基准，从中心行/列
    向 4 方向逐像素扩展，遇颜色变化即停；基准透明时匹配「无图」（抠图区）
  - pixel          像素扫描白色（RGB 三通道 ≥ threshold，已知是白底时用）
- 结果 payload：4 角点 + 宽高 + 中心 + 像素数 + 原图分辨率 + 算法 + 时间，
  落 params.detected_region（长视频同名字段）

实现约束（沿 REQ-105 决策）：不引入 PIL/numpy —— ffmpeg 先把背景图缩放到
设计空间 1/4（270×480）输出 PNG，stdlib 解码（RGB/RGBA，RGBA 保留 alpha），
扫描后坐标 ×4 折算回 1080×1920 设计空间（误差 ±4px，可手调）。
"""
from __future__ import annotations

import logging
import struct
import subprocess
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

from slirn_home import short_video_service as svc

log = logging.getLogger(__name__)

# 设计空间（短视频竖屏画布）与扫描网格（1/4）
_DESIGN_W, _DESIGN_H = 1080, 1920
_SCAN_W, _SCAN_H = 270, 480
_SCALE = _DESIGN_W // _SCAN_W  # 4

# 中心扩展：颜色容差（长视频同值）；中心 patch 半径
_COLOR_TOL = 10
_PATCH_R = 5
# 透明判定（alpha < 此值视为无图/抠图）
_ALPHA_OPAQUE = 128
# REQ-20261019-211：视频槽是几何事实（模板上的均质挖槽/白区），像素检出
# 覆盖 ≥25% 设计画布视为「置信」——此时 video 组像素优先于 LLM 估计
# （LLM 对槽位边界只有近似精度，曾把 y552/h824 的白槽估成 y648/h624，
# 并在导出前经 ensure_layout_detected 自动回写，视频填不满槽）
_PIXEL_VIDEO_MIN_AREA = 0.25 * (_DESIGN_W * _DESIGN_H)


def _shrink_to_png(src: Path, out_png: Path) -> bool:
    """ffmpeg 把任意图片格式缩放到 270×480 PNG（保留 alpha）。"""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"scale={_SCAN_W}:{_SCAN_H}",
        "-frames:v", "1", "-f", "image2", str(out_png),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return proc.returncode == 0 and out_png.is_file() and out_png.stat().st_size > 0


def _decode_png(png: Path) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """最小 PNG 解码器：8-bit RGB / RGBA（非隔行），返回 (w, h, [(r,g,b,a), ...])。"""
    data = png.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos = 8
    width = height = 0
    bit_depth = color_type = 0
    idat = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 8 + length + 4  # skip CRC
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    if bit_depth != 8 or color_type not in (2, 6):  # RGB / RGBA only
        raise ValueError(f"不支持的 PNG 格式 bit_depth={bit_depth} color_type={color_type}")
    raw = zlib.decompress(idat)
    bpp = 3 if color_type == 2 else 4
    stride = 1 + width * bpp  # filter byte + pixel bytes
    pixels: list[tuple[int, int, int, int]] = []
    prev = bytes(width * bpp)
    for y in range(height):
        row_start = y * stride
        filt = raw[row_start]
        line = bytearray(raw[row_start + 1:row_start + stride])
        # 应用 filter（None=0 / Sub=1 / Up=2 / Avg=3 / Paeth=4）
        if filt == 0:
            cur = bytes(line)
        elif filt == 1:  # Sub
            cur = bytearray(line)
            for i in range(bpp, len(line)):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
            cur = bytes(cur)
        elif filt == 2:  # Up
            cur = bytes((a + b) & 0xFF for a, b in zip(line, prev))
        elif filt == 3:  # Avg
            cur = bytearray(line)
            for i in range(len(line)):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + (left + prev[i]) // 2) & 0xFF
            cur = bytes(cur)
        elif filt == 4:  # Paeth
            cur = bytearray(line)
            for i in range(len(line)):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                cur[i] = (cur[i] + pr) & 0xFF
            cur = bytes(cur)
        else:
            raise ValueError(f"未知 PNG filter={filt}")
        for x in range(width):
            r = cur[x * bpp]
            g = cur[x * bpp + 1]
            b = cur[x * bpp + 2]
            a = cur[x * bpp + 3] if bpp == 4 else 255
            pixels.append((r, g, b, a))
        prev = cur
    return width, height, pixels


def _px(pixels: list[tuple[int, int, int, int]], w: int, x: int, y: int):
    return pixels[y * w + x]


def _center_expand_region(
        w: int, h: int, pixels: list[tuple[int, int, int, int]]) -> dict:
    """中心扩展（长视频口径十字线版）：

    1) 中心 patch（10×10）平均色 + 平均 alpha 为基准
    2) 基准透明（抠图）→ 匹配 = 像素透明；否则 = 不透明且 |RGB-基准| ≤ 10
    3) 中心行向左/右、中心列向上/下逐像素扩展，失配即停
    返回扫描网格坐标 {x_min, y_min, x_max, y_max, base, transparent}。
    """
    cx, cy = w // 2, h // 2
    x0 = max(0, cx - _PATCH_R); x1 = min(w, cx + _PATCH_R + 1)
    y0 = max(0, cy - _PATCH_R); y1 = min(h, cy + _PATCH_R + 1)
    n = 0
    sr = sg = sb = sa = 0.0
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b, a = _px(pixels, w, x, y)
            sr += r; sg += g; sb += b; sa += a
            n += 1
    base_rgb = (sr / n, sg / n, sb / n)
    base_alpha = sa / n
    transparent = base_alpha < _ALPHA_OPAQUE

    def _match(x: int, y: int) -> bool:
        r, g, b, a = _px(pixels, w, x, y)
        if transparent:
            return a < _ALPHA_OPAQUE
        if a < _ALPHA_OPAQUE:
            return False
        return (abs(r - base_rgb[0]) <= _COLOR_TOL
                and abs(g - base_rgb[1]) <= _COLOR_TOL
                and abs(b - base_rgb[2]) <= _COLOR_TOL)

    x_left = cx
    while x_left > 0 and _match(x_left - 1, cy):
        x_left -= 1
    x_right = cx
    while x_right < w - 1 and _match(x_right + 1, cy):
        x_right += 1
    y_top = cy
    while y_top > 0 and _match(cx, y_top - 1):
        y_top -= 1
    y_bottom = cy
    while y_bottom < h - 1 and _match(cx, y_bottom + 1):
        y_bottom += 1
    return {
        "x_min": x_left, "y_min": y_top,
        "x_max": x_right, "y_max": y_bottom,
        "base": [int(round(v)) for v in base_rgb] + [int(round(base_alpha))],
        "transparent": transparent,
    }


def _white_bbox(w: int, h: int, pixels: list[tuple[int, int, int, int]],
                threshold: int) -> dict | None:
    """像素扫描白色（长视频口径：RGB 三通道 ≥ threshold；透明不算白）。"""
    min_x, min_y, max_x, max_y = w, h, -1, -1
    count = 0
    for i, (r, g, b, a) in enumerate(pixels):
        if a >= _ALPHA_OPAQUE and r >= threshold and g >= threshold and b >= threshold:
            x = i % w
            y = i // w
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
            count += 1
    if max_x < 0:
        return None
    return {"x_min": min_x, "y_min": min_y, "x_max": max_x, "y_max": max_y,
            "count": count}


def _payload_from_bbox(box: dict, *, algorithm: str, threshold: int | None,
                       pixel_count: int, native_wh: tuple[int, int] | None,
                       detected_color: list[int] | None = None) -> dict:
    """扫描网格 bbox → 设计空间 payload（×4 折算 + 4 角点）。"""
    sx = sy = _SCALE
    x_min, y_min = box["x_min"], box["y_min"]
    x_max, y_max = box["x_max"], box["y_max"]
    nw, nh = native_wh or (_DESIGN_W, _DESIGN_H)
    out: dict[str, Any] = {
        "x": int(round(x_min * sx)),
        "y": int(round(y_min * sy)),
        "width": int(round((x_max - x_min + 1) * sx)),
        "height": int(round((y_max - y_min + 1) * sy)),
        "center_x": int(round(((x_min + x_max) / 2) * sx)),
        "center_y": int(round(((y_min + y_max) / 2) * sy)),
        "pixel_count": int(pixel_count),
        "image_native_w": int(nw),
        "image_native_h": int(nh),
        "algorithm": algorithm,
        "threshold": threshold,
        "corners": {
            "topleft":     [int(round(x_min * sx)), int(round(y_min * sy))],
            "topright":    [int(round(x_max * sx)), int(round(y_min * sy))],
            "bottomleft":  [int(round(x_min * sx)), int(round(y_max * sy))],
            "bottomright": [int(round(x_max * sx)), int(round(y_max * sy))],
        },
        "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if detected_color is not None:
        out["detected_color"] = detected_color
        out["color_tolerance"] = _COLOR_TOL
    return out


def _save_detected(project: dict, region: dict) -> None:
    from slirn_home import short_video_fine as _fine
    current = _fine._get_params(project)
    current["detected_region"] = region
    s = svc.get_stage_state(project, "stage6_fine")
    s["params"] = current
    svc.set_stage_state(project, "stage6_fine", s)


def detect_bg_white_area(repo_root: Path | str, task_id: str, project_id: str,
                         *, algorithm: str = "center_expand",
                         threshold: int = 250,
                         bg_path: str = "") -> dict:
    """bg 检测（REQ-20261018-166 对齐长视频）：

    检测 params.materials.bg 背景图素材（可显式传 bg_path 覆盖）；
    algorithm: center_expand（默认）/ pixel；threshold 仅 pixel 用（200-255）。
    结果落 params.detected_region 并返回。
    """
    if algorithm not in ("center_expand", "pixel"):
        raise svc.ShortVideoError(f"不支持的算法: {algorithm}")
    threshold = max(200, min(255, int(threshold)))
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    from slirn_home import short_video_fine as _fine
    params = _fine._get_params(project)
    mats = params.get("materials") or {}
    rel = str(bg_path or mats.get("bg") or "")
    if not rel:
        raise svc.ShortVideoError("请先上传背景图片（素材区「🎨 背景」槽）")
    src = (root / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
    if not src.is_file():
        raise svc.ShortVideoError(f"背景图片文件不存在: {rel}")

    tmp = svc.stage6_dir(root, task_id, project_id) / "_bg_detect.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not _shrink_to_png(src, tmp):
            raise svc.ShortVideoError("背景图读取失败（ffmpeg 转换失败）")
        w, h, pixels = _decode_png(tmp)
        # 原图分辨率（ffprobe 对图片同样给出宽高；失败则用设计空间兜底）
        native = None
        try:
            native = _fine._probe_video_size(src)
        except Exception:  # noqa: BLE001 — 原图尺寸仅展示用
            native = None

        if algorithm == "center_expand":
            r = _center_expand_region(w, h, pixels)
            payload = _payload_from_bbox(
                r, algorithm=algorithm, threshold=None,
                pixel_count=(r["x_max"] - r["x_min"] + 1) * (r["y_max"] - r["y_min"] + 1),
                native_wh=native, detected_color=r["base"])
            payload["transparent_base"] = bool(r["transparent"])
        else:  # pixel
            r = _white_bbox(w, h, pixels, threshold)
            if r is None:
                raise svc.ShortVideoError(
                    f"未检测到任何白色像素（threshold={threshold}）；"
                    f"请调低阈值或改用中心扩展算法")
            payload = _payload_from_bbox(
                r, algorithm=algorithm, threshold=threshold,
                pixel_count=r["count"], native_wh=native)

        _save_detected(project, payload)
        svc.save_project(root, project)
        return payload
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def clear_detected(repo_root: Path | str, task_id: str, project_id: str) -> dict:
    """清空检测缓存（params.detected_region）。"""
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    from slirn_home import short_video_fine as _fine
    params = _fine._get_params(project)
    had = params.pop("detected_region", None)
    s = svc.get_stage_state(project, "stage6_fine")
    s["params"] = params
    svc.set_stage_state(project, "stage6_fine", s)
    svc.save_project(root, project)
    return {"cleared": had is not None}


def clear_region_caches(repo_root: Path | str, task_id: str,
                        project_id: str) -> bool:
    """REQ-20261019-211：换背景图素材后清空区域缓存。

    detected_region 与 ai_layout_result 都是针对**旧背景图**的槽位结论；
    换图后留着不但误导结果卡，「填充到参数区域」还会把视频区回写成
    旧槽位（AI 优先级高于像素检测）。返回是否清掉了东西。
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    from slirn_home import short_video_fine as _fine
    params = _fine._get_params(project)
    had = False
    for key in ("detected_region", "ai_layout_result"):
        if params.pop(key, None) is not None:
            had = True
    if had:
        s = svc.get_stage_state(project, "stage6_fine")
        s["params"] = params
        svc.set_stage_state(project, "stage6_fine", s)
        svc.save_project(root, project)
    return had


def _refresh_region_for_fill(root: Path, task_id: str, project_id: str,
                             params: dict, region: dict) -> dict:
    """REQ-20261019-211：填充前的过期守卫（不落盘，随填充一起保存）。

    缓存带 image_native_w/h 且与当前背景图实测尺寸不一致 → 缓存是换图
    前的旧槽位，按缓存算法对当前图重检一次返回新 payload；旧 v1 缓存
    （无尺寸键）/ 无背景图 / 探测或重检失败 → 原样返回（旧行为）。
    """
    cw = int(region.get("image_native_w") or 0)
    ch = int(region.get("image_native_h") or 0)
    if not cw or not ch:
        return region
    rel = str((params.get("materials") or {}).get("bg") or "")
    if not rel:
        return region
    src = Path(rel) if Path(rel).is_absolute() else (root / rel)
    if not src.is_file():
        return region
    from slirn_home import short_video_fine as _fine
    try:
        nw, nh = _fine._probe_video_size(src)
    except Exception:  # noqa: BLE001 — 探不出尺寸就信缓存
        return region
    if (nw, nh) == (cw, ch):
        return region
    algo = str(region.get("algorithm") or "center_expand")
    if algo not in ("center_expand", "pixel"):
        algo = "center_expand"
    tmp = svc.stage6_dir(root, task_id, project_id) / "_bg_fill_probe.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not _shrink_to_png(src, tmp):
            return region
        w, h, pixels = _decode_png(tmp)
        if algo == "center_expand":
            r = _center_expand_region(w, h, pixels)
            payload = _payload_from_bbox(
                r, algorithm=algo, threshold=None,
                pixel_count=(r["x_max"] - r["x_min"] + 1)
                * (r["y_max"] - r["y_min"] + 1),
                native_wh=(nw, nh), detected_color=r["base"])
            payload["transparent_base"] = bool(r["transparent"])
        else:
            threshold = max(200, min(255, int(region.get("threshold") or 250)))
            r = _white_bbox(w, h, pixels, threshold)
            if r is None:
                return region
            payload = _payload_from_bbox(
                r, algorithm=algo, threshold=threshold,
                pixel_count=r["count"], native_wh=(nw, nh))
        log.info("detected_region 过期（bg %sx%s ≠ 缓存 %sx%s）→ 填充前已重检",
                 nw, nh, cw, ch)
        return payload
    except Exception:  # noqa: BLE001 — 重检失败沿用缓存，不阻断填充
        return region
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# REQ-20261018-172：区域类型关键词 → 参数区对应组（匹配 name+purpose）
# REQ-20261018-173：新增 mentor_text（导师文字区域）/ avatar（导师头像区域）；
# avatar 放 mentor_text 前面 ——「导师头像区」要先撞到头像，不能落到导师文字
_REGION_TYPE_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("video", ("视频", "播放")),
    ("subtitle", ("字幕",)),
    ("bg", ("背景",)),
    ("cover", ("封面", "片头")),
    ("avatar", ("头像",)),
    ("mentor_text", ("导师", "昵称", "介绍", "标题")),
)


def _region_type_key(region: dict) -> str:
    """按区域名/用途里的关键词归类（视频/字幕/背景/封面/导师文字/头像；其余不映射）。"""
    text = f"{region.get('name') or ''} {region.get('purpose') or ''}"
    for key, words in _REGION_TYPE_KEYS:
        if any(w in text for w in words):
            return key
    return ""


def _avatar_shape_hint(region: dict) -> str:
    """REQ-20261018-173：从头像区域的文字里推断形状 —— 含「圆」→ circle、
    含「方/矩形」→ rect；推断不出返回 ""（不动用户已选的形状）。"""
    text = f"{region.get('name') or ''} {region.get('purpose') or ''}"
    if "圆" in text:
        return "circle"
    if "方" in text or "矩形" in text or "rect" in text.lower():
        return "rect"
    return ""


def apply_detected_to_layout(repo_root: Path | str, task_id: str, project_id: str) -> dict:
    """REQ-20261018-172：按识别结果的区域类型填充到参数区对应组。

    来源优先级：
    1. ai_layout_result.regions（🤖 分析参数）——按类型映射：
       视频→layout.video / 字幕→layout.subtitle / 背景→layout.bg /
       封面(片头)→layout.cover / 导师文字(标题/导师信息)→layout.mentor_text /
       导师头像→layout.avatar（REQ-20261018-173；头像区域文字含「圆/方」
       时顺带更新 layout.avatar_shape）；其余类型只展示在结果卡里。
    2. 视频组：像素检测（detected_region，填充前有过期守卫）——
       AI 没填视频组时兜底；AI 填了但像素检出 ≥25% 画布（置信）时
       **像素优先**（REQ-20261019-211：槽位是几何事实，LLM 边界只是近似）。
    返回 {layout: video 区域或 None, filled: {组: 区域}, source: ai|detect|mixed}。
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    from slirn_home import short_video_fine as _fine
    params = _fine._get_params(project)

    filled: dict[str, dict] = {}
    avatar_shape = ""
    ai = params.get("ai_layout_result")
    regions = ai.get("regions") if isinstance(ai, dict) else None
    if isinstance(regions, list):
        for r in regions:
            if not isinstance(r, dict):
                continue
            key = _region_type_key(r)
            if key and key not in filled:
                filled[key] = {
                    "x": int(r.get("x") or 0), "y": int(r.get("y") or 0),
                    "w": int(r.get("w") or 0), "h": int(r.get("h") or 0),
                }
                if key == "avatar":
                    avatar_shape = _avatar_shape_hint(r)

    source = "ai" if filled else ""
    region = params.get("detected_region")
    if isinstance(region, dict) and region:
        # REQ-20261019-211：缓存可能是换背景图前的旧槽位 → 先守卫再消费
        fresh = _refresh_region_for_fill(root, task_id, project_id,
                                         params, region)
        if fresh is not region:
            params["detected_region"] = fresh
            region = fresh
        pw = int(region.get("width") or region.get("w") or 0)
        ph = int(region.get("height") or region.get("h") or 0)
        if "video" not in filled:
            filled["video"] = {
                "x": int(region.get("x") or 0), "y": int(region.get("y") or 0),
                "w": pw, "h": ph,
            }
            source = "mixed" if source == "ai" else "detect"
        elif pw * ph >= _PIXEL_VIDEO_MIN_AREA:
            # REQ-20261019-211：像素检出足够大（置信）→ video 组像素优先
            # 于 LLM 估计；其余组仍走 AI
            filled["video"] = {
                "x": int(region.get("x") or 0), "y": int(region.get("y") or 0),
                "w": pw, "h": ph,
            }
            source = "mixed"
    if not filled:
        raise svc.ShortVideoError(
            "没有检测结果；先运行「🤖 分析参数」或「🔍 检测区域」")

    layout = params.get("layout") or {}
    # REQ-20261018-184：只覆盖几何四键，保留组内其它键（video 的 crop_* /
    # 各区域的 enabled）——此前整组替换会把它们在每次填充时清掉
    for _key, _rect in filled.items():
        _old = layout.get(_key) or {}
        _keep = {k: v for k, v in _old.items() if k not in ("x", "y", "w", "h")}
        layout[_key] = {**_keep, **_rect}
    # REQ-20261018-189：适配方式（video_fit）已移除，填充不再清该键
    # REQ-20261018-173：头像区域被识别到且文字里能看出形状 → 一并带上
    if avatar_shape:
        layout["avatar_shape"] = avatar_shape
    params["layout"] = layout
    s = svc.get_stage_state(project, "stage6_fine")
    s["params"] = params
    svc.set_stage_state(project, "stage6_fine", s)
    svc.save_project(root, project)
    return {"layout": filled.get("video"), "filled": filled,
            "source": source or "ai"}
