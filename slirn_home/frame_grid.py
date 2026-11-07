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

from slirn_home.fftools import ffprobe_bin as _ffprobe_bin

log = logging.getLogger(__name__)

# 边界回退量（秒）：trim 的 end 是开区间、start 是闭区间。snap 到帧边界后
# 必须让「起始帧」被选中、「结束帧」被排除 ⇒ 两端统一回退一点。
# 1ms ≪ 最短帧长（30fps = 33.3ms）⇒ 不会误纳前一帧。
_BOUND_EPS = 0.001

# 探测结果缓存：(路径, mtime, 大小) → FrameGrid | None
_GRID_CACHE: dict[tuple, "FrameGrid | None"] = {}

# ---- CFR 判定（2026-11-07 现场修正）----
#
# 原判据：r_frame_rate == avg_frame_rate 才算 CFR。**这个判据是错的**：
# avg_frame_rate 是 ffprobe 用「帧数 ÷ 时长」反算的，时长带编辑列表偏移时
# 会有 1e-7 量级的舍入误差 —— 现场 630MB 课程视频实测
#   r_frame_rate = 25/1
#   avg_frame_rate = 1860016359/74400656 = 24.99999945   ← 只差 5.5e-7！
# 精确比较把严格 CFR 判成 VFR ⇒ 帧对齐失效 ⇒ 分块合成 A/V 时长校验必然
# 失败（3580 段实测偏差 +15.3s ≫ 1.0s 阈值）⇒ 白跑 43 分钟再回落老路。
#
# 改为**实测帧 PTS**：采样头部若干帧的 pts，若相邻差恒定 ⇒ 等间隔（CFR），
# 并直接拟合出 (start_pts, step)。这既判了 CFR，又给出精确栅格
# （start_pts 不再依赖容器 start_time 的编辑列表偏移）。
#
# 采样帧数 / 时长：60 帧足够判定等间隔（等间隔序列的差众数占比会接近 100%，
# 而 VFR 序列不会）；读 4 秒覆盖到 30fps 以上的帧率。
_CFR_SAMPLE_FRAMES = 60
_CFR_SAMPLE_SECONDS = 4.0

# 相邻差众数占比下限：低于此值视为非等间隔（真 VFR）⇒ 不对齐。
_CFR_UNIFORM_RATIO = 0.9

# 全局帧数自洽容差：|帧数 - 时长×帧率| / 帧数 超过此值 ⇒ 判非等间隔。
# 头部采样只能覆盖开头，用「总帧数 vs 总时长」做全局交叉校验。
_CFR_COUNT_TOLERANCE = 0.02


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


def _probe_stream(src: Path) -> dict | None:
    """ffprobe 读视频流元数据（r_frame_rate / avg_frame_rate / time_base …）。"""
    try:
        r = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=r_frame_rate,avg_frame_rate,time_base,start_time,start_pts,"
             "nb_frames,duration",
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
        return streams[0] if streams else None
    except Exception:  # noqa: BLE001 — 解析失败按「无栅格」处理
        return None


def _sample_frame_pts(src: Path, max_frames: int = _CFR_SAMPLE_FRAMES) -> list[int]:
    """采样视频流前若干帧的 PTS（整数 tick，流 time_base）。

    用 ``frame=pts``（帧 PTS，显示顺序）而不是 packet —— B 帧视频的包序与
    帧序不同，会把等间隔序列打乱成假 VFR。

    实测开销：630MB / 9577s 的课程视频采样 50 帧约 0.35s（见 2026-11-07
    现场记录），可接受。
    """
    try:
        r = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-read_intervals", f"%+{_CFR_SAMPLE_SECONDS:.3f}",
             "-show_entries", "frame=pts", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
    except Exception as e:  # noqa: BLE001 — 采样失败按「无栅格」处理
        log.debug("[frame-grid] 采样帧 PTS 失败 %s: %s", src, e)
        return []
    out: list[int] = []
    for line in r.stdout.splitlines():
        line = line.strip().strip(",")
        if not line:
            continue
        try:
            out.append(int(line.split(",")[0]))
        except ValueError:
            continue
        if len(out) >= max_frames:
            break
    return out


def _fit_uniform_lattice(pts: list[int]) -> tuple[int, int] | None:
    """从帧 PTS 序列拟合「等间隔栅格」→ (start_pts, step_ticks)。

    返回 None 表示不是等间隔（真 VFR）。

    算法：相邻差取**众数**作为 step（VFR 序列的差没有稳定众数；偶发的一两
    个离群差 —— 如 mp4 首帧的编辑列表偏移 —— 不会影响众数），众数占比低于
    ``_CFR_UNIFORM_RATIO`` ⇒ 判非等间隔。

    start_pts 由「PTS ≡ start_pts (mod step)」反推：取众数 step 后，
    各帧 ``pts - k*step`` 的众数即 start_pts（首帧离群不影响众数）。
    """
    if len(pts) < 8:
        return None
    diffs = [pts[i + 1] - pts[i] for i in range(len(pts) - 1)]
    diffs = [d for d in diffs if d > 0]
    if len(diffs) < 4:
        return None
    counts: dict[int, int] = {}
    for d in diffs:
        counts[d] = counts.get(d, 0) + 1
    step, hits = max(counts.items(), key=lambda kv: kv[1])
    if hits < len(diffs) * _CFR_UNIFORM_RATIO:
        return None  # 差值没有稳定众数 ⇒ 真 VFR
    # start_pts = (pts[k] - k*step) 的众数
    offsets: dict[int, int] = {}
    for k, p in enumerate(pts):
        off = p - k * step
        offsets[off] = offsets.get(off, 0) + 1
    start_pts = max(offsets.items(), key=lambda kv: kv[1])[0]
    return start_pts, step


def _probe_grid_uncached(src: Path) -> FrameGrid | None:
    """探测源视频帧栅格。

    判定 CFR 的依据是**实测帧 PTS 是否等间隔**（见模块常量区的说明），
    不再用 r_frame_rate == avg_frame_rate 的精确比较 —— 后者会把严格 CFR
    误判成 VFR（现场 5.5e-7 的舍入差就足以触发），直接导致帧对齐失效、
    分块合成白跑 43 分钟后回落老路。
    """
    st = _probe_stream(src)
    if not st:
        return None
    tb = _parse_rate(st.get("time_base"))
    if not tb:
        return None
    tb_den = tb[1]

    pts = _sample_frame_pts(src)
    fit = _fit_uniform_lattice(pts) if pts else None
    if fit is None:
        # 采样判定不是等间隔 ⇒ 真 VFR（或采样失败）⇒ 不对齐（保持原行为）
        log.info("[frame-grid] 非等间隔源，跳过对齐: %s（采样 %d 帧）",
                 src.name, len(pts))
        return None
    start_pts, step = fit

    # 全局交叉校验：总帧数应与「时长 × 帧率」一致。头部采样只能覆盖开头，
    # 这一步保证「开头等间隔」不代表中后段也是（真 VFR 常在中段出现）。
    try:
        nb_frames = int(st.get("nb_frames") or 0)
    except (TypeError, ValueError):
        nb_frames = 0
    try:
        duration = float(st.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if nb_frames > 0 and duration > 0:
        expect = duration * tb_den / step
        if abs(nb_frames - expect) / nb_frames > _CFR_COUNT_TOLERANCE:
            log.info("[frame-grid] 总帧数与时长不自洽，跳过对齐: %s"
                     "（帧数 %d vs 期望 %.1f）", src.name, nb_frames, expect)
            return None

    fd = step / tb_den
    if fd <= 0 or fd > 1.0:
        return None
    fps = tb_den / step
    grid = FrameGrid(
        fps=fps,
        fd=fd,
        t0=start_pts / tb_den,
        start_pts=start_pts,
        step=step,
        tb_den=tb_den,
    )
    log.info("[frame-grid] 等间隔栅格: %s → fps=%.4f step=%d tick start_pts=%d",
             src.name, fps, step, start_pts)
    return grid



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
