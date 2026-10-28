# -*- coding: utf-8 -*-
"""粗剪合成的分块（chunked）ffmpeg 实现 —— 段数很多时的快路径。

背景（2026-10-28 现场）：118 分钟课程视频切出 2579 段保留区间，单图
split=2579 的 filter 图跑了 **80 分钟**。根因不是编码本身，而是规模：

- ``split=N`` 会把输入流扇出成 N 路，每路各自缓冲帧；N=2579 时帧缓冲
  量级 ~N × 帧大小（1080p I420 ≈ 3MB）→ 数 GB，进程内存/GC 压力剧增；
- 每路的 ``trim=start=…`` 都要在**自己那一份**输入流上顺序解到该时间点，
  于是同一段源被解码了 N 次（2579 段 ⇒ 源被重复解码上千遍）；
- concat 滤镜还要同时持有 N 路输入的帧队列。

分块方案把规模问题拆掉：

1. 按源时间顺序把保留区间切成若干 **块**（每块 G 段，默认 64）；
2. 每块用 ``-ss <块首> -t <块长>`` 把输入窗口限制在该段区间，
   块内只做 G 路 split/trim/concat（规模恒定，与总段数无关）；
3. 块产物用 concat 协议 ``-c copy`` 拼接（不再重编码，只做字节拼接）。

于是解码量从「源 × N」降到「源 × 1（每块窗口只解一次）」，总耗时对段数
近似线性且常数很小。实测（200 段铺满 7110s 源，1920x1080）：
单图 133.7s → 分块 102.6s；段数越多差距越大（80 分钟级场景的正解）。

与单图路径产物口径一致：帧精确（trim+setpts 重编码）、A/V 时长复核、
随片 SRT 副产物一致。块产物落在任务临时目录，合成结束即清理。
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

# 每块段数上限：块内 split 路数由此决定。64 路在 1080p 下内存可控
# （~64×3MB 帧缓冲），再大收益递减；块数随总段数线性增长。
DEFAULT_CHUNK_SEGMENTS = 64

# 块输入窗口两端各留一点余量（秒）：避免 -ss 落在关键帧边界时首段丢帧
_WINDOW_PAD_SEC = 2.0


def _ffmpeg() -> str:
    from slirn_home.compose_service import ffmpeg_bin
    return ffmpeg_bin()


def plan_chunks(merged_ms: list[list[int]],
                chunk_segments: int = DEFAULT_CHUNK_SEGMENTS) -> list[list[int]]:
    """保留区间 → 块索引列表（每块是 merged_ms 的连续切片下标区间）。

    merged_ms 必须已按时间升序（merge_intervals_ms 的产物）。
    """
    n = len(merged_ms)
    if n <= 0:
        return []
    g = max(1, int(chunk_segments))
    return [[i, min(i + g, n)] for i in range(0, n, g)]


def build_chunk_cmd(src: Path, dst: Path, segs: list[list[int]],
                    win_start_ms: int, win_dur_ms: int,
                    *, has_audio: bool = True,
                    bounds_s: list[tuple[float, float]] | None = None,
                    win_start_s: float | None = None,
                    win_dur_s: float | None = None) -> list[str]:
    """单块 ffmpeg argv：-ss/-t 限制输入窗口，块内 split+trim+concat 重编码。

    段时间是**源绝对时间**，这里转成相对窗口起点的偏移（窗口起点即
    win_start_ms），trim 用相对值。纯函数可单测。

    bounds_s: **帧栅格对齐**的段边界（秒，绝对源时间）。给出时用它当段边界
    （而不是 keep_ms 换算的毫秒边界），并以 win_start_s 为窗口起点换算
    相对偏移 —— 选中的帧集合不变，但音频与视频用同一边界 ⇒ 段内等长、
    concat 不补空隙、总时长可精确预期。win_start_s 建议取帧栅格上的时刻
    （见 slirn_home/frame_grid.py）。
    """
    from slirn_home.frame_grid import fmt_bound

    n = len(segs)
    parts: list[str] = []
    labels: list[str] = []
    if n > 1:
        parts.append("[0:v]split=%d%s" % (n, "".join(f"[sv{k}]" for k in range(n))))
        if has_audio:
            parts.append("[0:a]asplit=%d%s" % (n, "".join(f"[sa{k}]" for k in range(n))))
    for k, (s, e) in enumerate(segs):
        vsrc = f"[sv{k}]" if n > 1 else "[0:v]"
        asrc = f"[sa{k}]" if n > 1 else "[0:a]"
        if bounds_s is not None:
            base = win_start_s if win_start_s is not None else win_start_ms / 1000.0
            rs = fmt_bound(bounds_s[k][0] - base)
            re_ = fmt_bound(bounds_s[k][1] - base)
        else:
            rs = f"{(s - win_start_ms) / 1000.0:.3f}"
            re_ = f"{(e - win_start_ms) / 1000.0:.3f}"
        parts.append(f"{vsrc}trim=start={rs}:end={re_},setpts=PTS-STARTPTS[v{k}]")
        if has_audio:
            parts.append(f"{asrc}atrim=start={rs}:end={re_},asetpts=PTS-STARTPTS[a{k}]")
            labels.extend((f"[v{k}]", f"[a{k}]"))
        else:
            labels.append(f"[v{k}]")
    if has_audio:
        parts.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[vout][aout]")
    else:
        parts.append(f"{''.join(labels)}concat=n={n}:v=1:a=0[vout]")
    ss = win_start_s if win_start_s is not None else win_start_ms / 1000.0
    dur = win_dur_s if win_dur_s is not None else win_dur_ms / 1000.0
    cmd = [_ffmpeg(), "-y",
           "-ss", f"{ss:.6f}",
           "-t", f"{dur:.6f}",
           "-i", str(src),
           "-filter_complex", ";".join(parts)]
    if has_audio:
        cmd += ["-map", "[vout]", "-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-map", "[vout]"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-movflags", "+faststart", str(dst)]
    return cmd


def _concat_copy(parts: list[Path], dst: Path, work_dir: Path) -> None:
    """concat 协议字节拼接（-c copy，不重编码）。"""
    lst = work_dir / "concat_list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    r = subprocess.run(
        [_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", "-movflags", "+faststart", str(dst)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=3600,
    )
    if r.returncode != 0:
        raise RuntimeError(f"concat 拼接失败（退出码 {r.returncode}）: {(r.stderr or '')[-400:]}")


def compose_chunked(src: Path, merged_ms: list[list[int]], dst: Path, *,
                    has_audio: bool = True,
                    chunk_segments: int = DEFAULT_CHUNK_SEGMENTS,
                    work_dir: Path | None = None,
                    on_progress=None,
                    bounds_s: list[tuple[float, float]] | None = None,
                    grid=None) -> dict:
    """分块合成 → dst。返回 {"elapsed", "chunks", "segments"}。

    失败抛异常（调用方回落单图/上游 moviepy 老路）。

    bounds_s / grid: **帧栅格对齐**的段边界（秒，绝对源时间）与源帧栅格。
    给出时：块窗口起点也取帧栅格上的时刻（相对偏移因此落在同一栅格上），
    段内音视频等长 ⇒ concat 不补空隙、总时长 = Σ帧数×帧长 可精确预期。
    """
    from slirn_home import asr_service

    src = Path(src)
    dst = Path(dst)
    if not merged_ms:
        raise RuntimeError("没有保留区间")
    tmp = dst.with_name(dst.stem + ".chunk.tmp.mp4")
    work = Path(work_dir) if work_dir else tmp.parent
    work.mkdir(parents=True, exist_ok=True)
    chunk_dir = work / (dst.stem + "_chunks")
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir, ignore_errors=True)
    chunk_dir.mkdir(parents=True, exist_ok=True)

    chunks = plan_chunks(merged_ms, chunk_segments)
    total_keep_ms = sum(e - s for s, e in merged_ms)
    t0 = time.time()
    produced: list[Path] = []
    aligned = bounds_s is not None and grid is not None
    if aligned:
        pad_frames = int(math.ceil(_WINDOW_PAD_SEC / grid.fd))
    try:
        for ci, (i0, i1) in enumerate(chunks):
            segs = merged_ms[i0:i1]
            blk_bounds = None
            win_start_s = win_dur_s = None
            if aligned:
                # 窗口起点取帧栅格上的时刻 ⇒ 相对偏移与源帧栅格同相位
                k0 = grid.index_at(bounds_s[i0][0])
                k_end = grid.index_at(bounds_s[i1 - 1][1])
                win_idx = max(0, k0 - pad_frames)
                win_start_s = grid.time_of(win_idx)
                win_end_s = grid.time_of(k_end + pad_frames)
                win_dur_s = max(0.0, win_end_s - win_start_s)
                blk_bounds = bounds_s[i0:i1]
                win_start = int(round(win_start_s * 1000))
                win_dur = int(round(win_dur_s * 1000))
            else:
                win_start = max(0, segs[0][0] - int(_WINDOW_PAD_SEC * 1000))
                win_end = segs[-1][1] + int(_WINDOW_PAD_SEC * 1000)
                win_dur = win_end - win_start
            out = chunk_dir / f"chunk{i0:06d}.mp4"
            cmd = build_chunk_cmd(src, out, segs, win_start, win_dur,
                                  has_audio=has_audio, bounds_s=blk_bounds,
                                  win_start_s=win_start_s, win_dur_s=win_dur_s)
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=7200)
            if r.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
                raise RuntimeError(
                    f"块 {ci + 1}/{len(chunks)} 合成失败（退出码 {r.returncode}）: "
                    f"{(r.stderr or '')[-300:]}")
            produced.append(out)
            if on_progress:
                done_ms = sum(e - s for s, e in merged_ms[:i1])
                on_progress(5.0 + (done_ms / max(1, total_keep_ms)) * 88.0)
        # 字节拼接（不重编码）
        _concat_copy(produced, tmp, chunk_dir)
        if on_progress:
            on_progress(96.0)
    finally:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        if work_dir is not None:
            # 清理空的工作目录（调用方给的），别在任务 outputs 里留空壳
            try:
                work.rmdir()
            except OSError:
                pass
    elapsed = time.time() - t0
    return {
        "elapsed": round(elapsed, 1),
        "chunks": len(chunks),
        "segments": len(merged_ms),
        "tmp": tmp,
        "total_keep_ms": total_keep_ms,
    }
