# -*- coding: utf-8 -*-
"""源视频帧栅格探测 + 保留区间对齐（粗剪合成提速/正确性配套，2026-10-28）。

问题
----
ffmpeg 的 ``trim``/``atrim`` 保留的是**整帧**（视频）与**整采样**（音频），
边界按秒给出。于是毫秒区间 ``[s, e)`` 落到源视频上时：

- 视频：保留 ``PTS ∈ [s, e)`` 的帧 ⇒ 实际时长 = 帧数 × 帧长（25fps ⇒
  40ms/帧），与「毫秒差 e−s」最多差 **1 帧**；
- 音频：保留 ``PTS ∈ [s, e)`` 的采样 ⇒ 实际时长 = e−s（毫秒差，逐字精确）。

两者每段最多差 1 帧，而 ``concat`` 滤镜按最长流对齐 ⇒ 段间补空隙；总时长
= Σ帧数×帧长，与「毫秒求和」**随段数线性偏离**（每段 ±1 帧）。现场 2579 段
⇒ 偏离 +2.9s（模型 5310.24s vs 毫秒求和 5307.28s）⇒ 粗剪的 A/V 复核（固定
1.0s 阈值）必然失败 ⇒ 回落到 80 分钟的 moviepy 老路；随片字幕时间轴也累积
偏移（末尾偏 3s）。

修法
----
把每段边界 snap 到**源帧栅格**（帧 k 的 PTS = t0 + k×帧长）：

- 选中的帧集合**完全不变**（边界只是取整到帧边界 —— 实测 3559 帧 = 模型
  预测 3559 帧）；
- 音频用同一边界裁 ⇒ 段内音视频等长 ⇒ concat 不再补空隙；
- 总时长 = Σ帧数×帧长，可**精确预期** ⇒ A/V 复核口径从「毫秒求和」改成
  「帧栅格预期」，1.0s 阈值才重新有意义（实测偏差 ≤ 1 帧）；
- 段内音视频的内容起点也一致（原先音频最多比画面早 40ms）。

本模块只做纯计算（探测 + 对齐），不调用 ffmpeg，可单测。
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 边界回退量（秒）：trim 的 end 是开区间、start 是闭区间。snap 到帧边界后
# 必须让「起始帧」被选中、「结束帧」被排除 ⇒ 两端统一回退一点。
# 1ms ≪ 最短帧长（30fps = 33.3ms）⇒ 不会误纳前一帧。
_BOUND_EPS = 0.001

# 探测结果缓存：(路径, mtime, 大小) → FrameGrid | None
_GRID_CACHE: dict[tuple, "FrameGrid | None"] = {}


@dataclass(frozen=True)
class FrameGrid:
    """源视频的帧栅格（CFR 恒定帧率）。

    帧 k 的 PTS = (start_pts + k×step) / tb_den 秒 —— 全部用整数 tick 算，
    避免浮点误差在几千段上累积。
    """

    fps: float
    fd: float          # 帧长（秒）
    t0: float          # 第 0 帧的 PTS（秒）
    start_pts: int     # 第 0 帧的 tick
    step: int          # 每帧 tick 数
    tb_den: int        # time_base 分母（每秒 tick 数）

    def time_of(self, k: int) -> float:
        """第 k 帧的 PTS（秒）。"""
        return (self.start_pts + k * self.step) / self.tb_den

    def index_at(self, t_sec: float) -> int:
        """第一个 PTS ≥ t_sec 的帧下标（ceil 语义；可与 t_sec 同帧）。"""
        d = int(round(t_sec * self.tb_den)) - self.start_pts
        return -((-d) // self.step)  # ceil(d / step)

    def frame_at(self, t_sec: float) -> float:
        """t_sec 所在（或其后第一）帧的 PTS（秒）。"""
        return self.time_of(self.index_at(t_sec))


@dataclass(frozen=True)
class KeepPlan:
    """保留区间对齐到帧栅格后的计划。

    bounds_s[i]  —— 第 i 段 snap 后的源绝对边界（秒），起点/终点都在帧上
    frames[i]    —— 第 i 段保留的帧数
    seg_start_ms[i] —— 第 i 段 snap 后的源起点（毫秒，用于字幕时间轴）
    seg_dur_ms[i]   —— 第 i 段在成片里的实际时长（毫秒，= 帧数×帧长）
    expected_sec —— 合成产物的预期时长（秒）= Σ帧数×帧长
    """

    bounds_s: list[tuple[float, float]]
    frames: list[int]
    seg_start_ms: list[int]
    seg_dur_ms: list[int]
    expected_sec: float
    grid: FrameGrid


def fmt_bound(sec: float) -> str:
    """帧栅格边界 → ffmpeg 滤镜参数（秒，6 位小数 + 边界回退量）。

    回退是为了让「起始帧」被选中、「结束帧」被排除（trim 的 start 闭、
    end 开）。1ms ≪ 帧长，不会误纳前一帧。
    """
    return f"{sec - _BOUND_EPS:.6f}"


def _parse_rate(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    try:
        num, _, den = str(text).partition("/")
        n, d = int(num), int(den or 1)
    except (TypeError, ValueError):
        return None
    return (n, d) if n > 0 and d > 0 else None


def _probe_grid_uncached(src: Path) -> FrameGrid | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=r_frame_rate,avg_frame_rate,time_base,start_time",
             "-of", "json", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60)
    except Exception as e:  # noqa: BLE001 — 探测失败按「无栅格」处理
        log.debug("[frame-grid] ffprobe 失败 %s: %s", src, e)
        return None
    if r.returncode != 0:
        return None
    try:
        streams = json.loads(r.stdout).get("streams") or []
        st = streams[0]
    except Exception:  # noqa: BLE001 — 解析失败按「无栅格」处理
        return None

    r_rate = _parse_rate(st.get("r_frame_rate"))
    a_rate = _parse_rate(st.get("avg_frame_rate"))
    if not a_rate:
        return None
    if r_rate and r_rate != a_rate:
        # 可变帧率（VFR）：帧栅格不成立 ⇒ 不对齐（保持原行为）
        log.debug("[frame-grid] VFR 源，跳过对齐: %s", src)
        return None

    tb = _parse_rate(st.get("time_base"))
    if not tb:
        return None
    tb_den = tb[1]

    fps_num, fps_den = a_rate
    # 每帧 tick 数 = tb_den × 帧长(秒) = tb_den × fps_den / fps_num，
    # 必须是整数，否则不是严格 CFR
    num = tb_den * fps_den
    if num % fps_num:
        return None
    step = num // fps_num

    try:
        start_time = float(st.get("start_time") or 0.0)
    except (TypeError, ValueError):
        start_time = 0.0
    start_pts = int(round(start_time * tb_den))
    if start_pts < 0:
        start_pts = 0

    fd = step / tb_den
    if fd <= 0 or fd > 1.0:
        return None
    return FrameGrid(
        fps=fps_num / fps_den,
        fd=fd,
        t0=start_pts / tb_den,
        start_pts=start_pts,
        step=step,
        tb_den=tb_den,
    )


def probe_grid(src: Path | str) -> FrameGrid | None:
    """探测源视频帧栅格；不可用（VFR / 无视频流 / 探测失败）返回 None。

    结果按 (路径, mtime, 大小) 缓存 —— 一次合成里多次调用只探一次。
    """
    src = Path(src)
    try:
        st = src.stat()
    except OSError:
        return None
    key = (str(src.resolve()), st.st_mtime_ns, st.st_size)
    if key in _GRID_CACHE:
        return _GRID_CACHE[key]
    grid = _probe_grid_uncached(src)
    _GRID_CACHE[key] = grid
    return grid


def plan_keep(keep_ms: list[list[int]], grid: FrameGrid | None) -> KeepPlan | None:
    """保留区间（毫秒）→ 对齐到帧栅格的计划；无栅格返回 None（调用方走原路径）。"""
    if grid is None or not keep_ms:
        return None

    bounds: list[tuple[float, float]] = []
    frames: list[int] = []
    seg_start_ms: list[int] = []
    seg_dur_ms: list[int] = []
    total_frames = 0

    for s, e in keep_ms:
        k0 = grid.index_at(int(s) / 1000.0)
        k1 = grid.index_at(int(e) / 1000.0)
        if k1 <= k0:
            # 不足一帧（或零长）也保留一帧：否则该段字幕行的声音会整段丢失
            k1 = k0 + 1
        n = k1 - k0
        bounds.append((grid.time_of(k0), grid.time_of(k1)))
        frames.append(n)
        total_frames += n
        seg_start_ms.append(int(round(grid.time_of(k0) * 1000)))
        seg_dur_ms.append(int(round(n * grid.fd * 1000)))

    return KeepPlan(
        bounds_s=bounds,
        frames=frames,
        seg_start_ms=seg_start_ms,
        seg_dur_ms=seg_dur_ms,
        expected_sec=total_frames * grid.fd,
        grid=grid,
    )


def plan_keep_for(src: Path | str, keep_ms: list[list[int]]) -> KeepPlan | None:
    """一步到位：探测源栅格 + 对齐保留区间。"""
    return plan_keep(keep_ms, probe_grid(src))
