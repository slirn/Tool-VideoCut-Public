"""粗剪合成服务 — 复用上游 VideoClipper 合成方法拼接保留区间（REQ-20260916-018）。

REQ-20260916-016 的自研 ffmpeg concat 重编码路线被弃用，本文件改为复刻
上游主项目首页合成页的调用过程（funclip/launch.py「AI 智能裁剪」按钮 →
funclip/videoclipper.py VideoClipper.video_clip 的 timestamp_list 分支）：

    clipper = VideoClipper(None)   # clip 阶段不加载 ASR 模型（上游 CLI 同款）
    state = {'sentences': 保留行, 'video': VideoFileClip(src),
             'clip_video_file': dst, 'video_filename': src, ...}
    out, message, clip_srt = clipper.video_clip(
        '', 0, 0, state, add_sub=False, timestamp_list=保留区间毫秒)

对接说明：
- timestamp_list 单位毫秒（上游 extract_timestamps 同口径）— 切分清单即毫秒，直传
- sentences = 整行单 token（{'text', 'timestamp': [[行首ms, 行尾ms]]}）；
  行与区间边界同源（区间=行区间的并集），generate_srt_clip 整行进/整行出
- 上游输出固定带 _no{GLOBAL_COUNT} 后缀 → 按返回值 rename 成标准产物名
- clip_srt（处理之后的字幕，时间轴随片平移）落 rough_compose.srt 副产物
- 进度：合成期间临时包一层 VideoClip.write_videofile 注入 proglog logger
  （视频帧 t/total）；全程持 _COMPOSE_LOCK（GLOBAL_COUNT 与 patch 均非并发安全）
- 实测（44min 源 / 997 区间）：编码约 80 fps，全程约 11-12 分钟
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import proglog  # moviepy 1.0.3 自带依赖（帧进度协议）

from slirn_home import execution_history

log = logging.getLogger(__name__)

ROUGH_COMPOSE_NAME = "rough_compose.mp4"

# 跨任务串行：VideoClipper.GLOBAL_COUNT（类属性自增）与 write_videofile 补丁
# 都不是并发安全的，两任务同时合成会互踩输出文件名
_COMPOSE_LOCK = threading.Lock()

_UPSTREAM_CLS = None  # VideoClipper 类缓存（sys.path 注入 + import 只做一次）


def rough_compose_path(outputs_dir: Path) -> Path:
    """粗剪成片固定产物路径 outputs/rough_compose.mp4。"""
    return Path(outputs_dir) / ROUGH_COMPOSE_NAME


def merge_intervals(intervals: list[tuple[float, float]]) -> list[list[float]]:
    """升序合并相邻区间（缝隙 <1ms 视为连续 — 保留单元接缝处不产生无谓切点）。"""
    merged: list[list[float]] = []
    for s, e in sorted(intervals):
        if merged and s - merged[-1][1] < 0.001:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def merge_intervals_ms(intervals_ms: list[tuple[int, int]]) -> list[list[int]]:
    """毫秒整数版本的 merge_intervals — 传给上游合成时无浮点精度损失。"""
    merged: list[list[int]] = []
    for s, e in sorted(intervals_ms):
        if merged and s - merged[-1][1] < 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def build_sentences(lines: list[dict]) -> list[dict]:
    """保留行 → 上游 sentences 格式（整行单 token，timestamp 毫秒）。

    generate_srt_clip 会自行 str2list(text)；行边界与区间边界同源，
    token 级裁剪分支（句子被区间从中切断）不会触发。
    """
    return [
        {"text": str(ln.get("text", "")),
         "timestamp": [[int(ln["start_ms"]), int(ln["end_ms"])]]}
        for ln in lines
    ]


def _ms_to_srt_time(ms: int) -> str:
    """整数毫秒 → SRT 时间戳 'HH:MM:SS,mmm'（HH 不补零前导截断）。"""
    h, ms = divmod(int(ms), 3600 * 1000)
    m, ms = divmod(ms, 60 * 1000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_srt(units: list[dict], text_overrides: dict | None = None) -> str:
    """保留行列表 → SRT 字符串（按 start_ms 升序，1-based 编号）。

    text_overrides[id] = new_text 优先于 unit.text（热词替换未撤销行的修正）。
    空 units → ""（前端按 lines=0 自行提示）。
    """
    text_overrides = text_overrides or {}
    parts: list[str] = []
    for i, u in enumerate(sorted(units, key=lambda x: int(x["start_ms"])), start=1):
        start = int(u["start_ms"])
        end = int(u["end_ms"])
        text = text_overrides.get(str(u["id"]), str(u.get("text", "")))
        parts.append(f"{i}\n{_ms_to_srt_time(start)} --> {_ms_to_srt_time(end)}\n{text}")
    return "\n\n".join(parts) + ("\n" if parts else "")


# ---- 按字幕文件时间段截取拼接（REQ-20260916-020） ----

# SRT 时间轴行：HH:MM:SS,mmm --> HH:MM:SS,mmm（毫秒分隔符兼容 ',' / '.'，可省略）
_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})(?:[,.](\d{1,3}))?"
    r"\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})(?:[,.](\d{1,3}))?"
)


def _srt_clock_to_ms(h: str, mi: str, s: str, ms: str | None) -> int:
    """SRT 时钟三元组（+可选毫秒）→ 整数毫秒。毫秒不足 3 位右补零（',5' = 500ms）。"""
    return (int(h) * 3600 + int(mi) * 60 + int(s)) * 1000 + int((ms or "0").ljust(3, "0"))


def parse_srt(srt_text: str) -> list[dict]:
    """SRT 文本 → [{"start_ms", "end_ms", "text"}]（按 start_ms 升序）。

    每个字幕条目即一个截取区间。容忍 BOM/CRLF/缺编号/乱序块/多行文本；
    时间轴非法（end <= start）或整块无时间轴的条目跳过；一个都解析不出 → []。
    """
    text = srt_text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    entries: list[dict] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        m = None
        ts_line = -1
        for i, ln in enumerate(lines):
            mm = _SRT_TIME_RE.search(ln)
            if mm:
                m, ts_line = mm, i
                break
        if not m:
            continue
        g = m.groups()
        start_ms = _srt_clock_to_ms(*g[:4])
        end_ms = _srt_clock_to_ms(*g[4:])
        if end_ms <= start_ms:
            continue
        entries.append({
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": "\n".join(lines[ts_line + 1:]).strip(),
        })
    entries.sort(key=lambda e: (e["start_ms"], e["end_ms"]))
    return entries


def compose_from_srt(src: Path, srt_path: Path, dst: Path, on_progress=None) -> dict:
    """按 SRT 字幕文件的时间段从源视频截取片段并拼接（REQ-20260916-020）。

    不经过大语言模型：SRT 每条目即一个截取区间（毫秒级），升序合并相邻后
    走 compose_rough_cut（上游 VideoClipper 切片拼接 + 随片字幕时间轴平移
    到成片时间轴，副产物落 dst 同名 .srt）。

    越界处理：end 超视频时长截到时长；整体在视频外（或截后不足 1ms）的
    条目丢弃并在返回值 dropped 里计数 —— 不会静默丢段。

    Returns:
        compose_rough_cut 返回值 + {"srt_entries", "dropped", "keep_sec_expected"}
    """
    src, srt_path, dst = Path(src), Path(srt_path), Path(dst)
    if not srt_path.exists():
        raise RuntimeError(f"字幕文件不存在: {srt_path}")
    if not src.exists():
        raise RuntimeError(f"源视频不存在: {src}")
    entries = parse_srt(srt_path.read_text(encoding="utf-8-sig"))
    if not entries:
        raise RuntimeError(
            "字幕文件未解析到有效时间段（需 SRT 格式：HH:MM:SS,mmm --> HH:MM:SS,mmm）")
    dur = _probe_duration(src)
    if not dur:
        raise RuntimeError(f"读不到源视频时长: {src}")
    dur_ms = int(dur * 1000)

    kept: list[dict] = []
    dropped = 0
    for e in entries:
        s = e["start_ms"]
        t = min(e["end_ms"], dur_ms)
        if s >= dur_ms or t - s < 1:  # 整体越界 / 截后为空
            dropped += 1
            continue
        kept.append({**e, "end_ms": t})
    if not kept:
        raise RuntimeError(f"所有时间段都在视频时长（{dur:.1f}s）之外")

    intervals = [(e["start_ms"], e["end_ms"]) for e in kept]
    result = compose_rough_cut(src, intervals, dst, kept, on_progress=on_progress)
    merged_total_ms = sum(e - s for s, e in merge_intervals_ms(intervals))
    result.update({
        "srt_entries": len(entries),
        "dropped": dropped,
        "keep_sec_expected": round(merged_total_ms / 1000.0, 2),
    })
    return result


def delete_rough_compose(outputs_dir: Path, *, auto: bool = False,
                       auto_session_id: str = "") -> dict:
    """删除粗剪成片（mp4 + srt 副产物）。同步、毫秒级，不进 job 表。

    REQ-20260919-075：删除操作本身也写执行历史（之前只有合成动作记录），
    便于"查看历史时看到删除粗剪成品"。

    REQ-20260920-081：auto_session_id 透传，同一次自动流的多次操作共用 session_id。

    Returns:
        {"deleted": bool, "removed": [...], "remaining": [...], "message": str}
    """
    out_dir = Path(outputs_dir)
    mp4 = rough_compose_path(out_dir)
    srt = mp4.with_suffix(".srt")
    removed: list[str] = []
    for p in (mp4, srt):
        if p.exists():
            try:
                p.unlink()
                removed.append(p.name)
            except OSError as e:
                log.warning("[compose] 删除失败 %s: %s", p, e)
    remaining = [n for n in (mp4.name, srt.name) if (out_dir / n).exists()]
    # REQ-20260919-075：删除动作也记执行历史
    try:
        eid = execution_history.record_start(
            out_dir, execution_history.KIND_ROUGH_COMPOSE_DELETE,
            extra={"removed": removed, "remaining": remaining},
            auto=auto,
            auto_session_id=auto_session_id,
        )
        if removed:
            execution_history.patch_fields(out_dir, eid, {
                "description": f"删除粗剪成品：{' + '.join(removed)}"
            })
            execution_history.record_finish(out_dir, eid, success=True, error="")
        else:
            execution_history.patch_fields(out_dir, eid, {
                "description": "粗剪成片不存在，无需删除（无操作）"
            })
            execution_history.record_finish(out_dir, eid, success=True, error="")
    except Exception as e:  # noqa: BLE001 — 历史写失败不影响主流程
        log.warning("[compose][history] delete_rough_compose 记录失败: %s", e)
    if removed:
        return {"deleted": True, "removed": removed,
                "remaining": remaining, "message": f"已删除 {' + '.join(removed)}"}
    return {"deleted": False, "removed": [],
            "remaining": remaining, "message": "粗剪成片不存在，无需删除"}


def _load_upstream_clipper():
    """加载上游 VideoClipper（funclip/ 内部是平级 import，需注入 sys.path）。"""
    global _UPSTREAM_CLS
    if _UPSTREAM_CLS is not None:
        return _UPSTREAM_CLS
    funclip_dir = Path(__file__).resolve().parent.parent / "funclip"
    if not funclip_dir.is_dir():
        raise RuntimeError(f"上游 funclip/ 目录不存在: {funclip_dir}")
    if str(funclip_dir) not in sys.path:
        sys.path.insert(0, str(funclip_dir))
    from videoclipper import VideoClipper  # noqa: E402 — 上游平级 import 风格

    _UPSTREAM_CLS = VideoClipper
    return VideoClipper


class _FrameProgressLogger(proglog.ProgressBarLogger):
    """moviepy 帧进度 → on_frame(pct 0-100)；不打印进度条（后台服务避免刷屏）。

    proglog 协议：'bar__total'/'bar__index' 经 __call__ 转入 bars 结构后回调
    bars_callback(bar, attr, value, old_value)（moviepy 的视频帧 bar 名是 t、
    音频 chunk bar 名是 chunk — 两者都算编码进度）。
    """

    def __init__(self, on_frame: Callable[[float], None]):
        super().__init__(min_time_interval=0.4)
        self._on_frame = on_frame

    def bars_callback(self, bar, attr, value, old_value) -> None:  # noqa: ARG002
        if attr != "index" or not self._on_frame:
            return
        total = (self.bars.get(bar) or {}).get("total")
        if isinstance(total, (int, float)) and total > 0:
            self._on_frame(min(100.0, float(value) / float(total) * 100.0))


def compose_rough_cut(
    src: Path,
    intervals_ms: list[tuple[int, int]],
    dst: Path,
    lines: list[dict],
    on_progress=None,
) -> dict:
    """用上游 VideoClipper.video_clip 把保留区间按序拼接合成 dst。

    Args:
        src: 源视频（任务工作视频 — 截取段或原视频，时间轴与切分清单一致）
        intervals_ms: [(start_ms, end_ms), …] 整数毫秒，升序（内部**合并相邻**——
            无缝拼接的行会被并成一段；上游 generate_srt_clip 在 CASE2 路径遇到
            浮点精度差导致空 token 时崩溃，整数毫秒直传可避免）
        dst: 输出路径（上游先写带 _no{N} 后缀的同名文件，成功后 rename）
        lines: 保留行 [{"id","start_ms","end_ms","text"}…] — 最新确认文本
            （含热词替换未撤销行的 new_text，由调用端合并）
        on_progress: 可选回调 (pct: 0-100)（视频编码帧进度）

    Returns:
        {"duration": 秒, "size_mb": MB, "segments": 合并后区间数, "raw_segments": 原数, "elapsed": 秒}
    """
    if not src.exists():
        raise RuntimeError(f"源视频不存在: {src}")
    if not intervals_ms:
        raise RuntimeError("没有保留区间 — 全部内容都被删除时无需合成")
    if not lines:
        raise RuntimeError("缺少保留行（字幕）— 上游合成方法按行生成随片字幕")

    VideoClipper = _load_upstream_clipper()
    import moviepy.editor as mpy
    from moviepy.video.VideoClip import VideoClip

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 合并相邻区间：相邻 <1ms 视为连续（毫秒整数比较）
    merged_ms = merge_intervals_ms(intervals_ms)
    # upstream timestamp_list 单位 = 毫秒（funclip/videoclipper.py: start = ts/16000 秒）
    # 给 end 加 1ms 容差规避 upstream int(end*1000) 浮点精度丢 1ms 触发的
    # CASE2 空 _ts 崩溃（funclip/utils/subtitle_utils.py:91-110 路径）
    ts_list = [[s, e + 1] for s, e in merged_ms]
    raw_n = len(intervals_ms)

    total_keep_ms = sum(e - s for s, e in merged_ms)
    last_tick = 0.0

    def _on_frame(pct: float) -> None:
        nonlocal last_tick
        if not on_progress:
            return
        now = time.time()
        if now - last_tick >= 0.5:  # 节流 ≤2Hz
            last_tick = now
            on_progress(5.0 + pct * 0.94)  # 预热 5% + 编码映射到 5-99

    t0 = time.time()
    if on_progress:
        on_progress(2.0)
    # ---- 临时给 VideoClip.write_videofile 注入进度 logger（锁内安全） ----
    _orig_wvf = VideoClip.write_videofile
    frame_logger = _FrameProgressLogger(_on_frame)

    def _wvf(self, filename, *a, **kw):
        kw.setdefault("logger", frame_logger)
        return _orig_wvf(self, filename, *a, **kw)

    video = None
    with _COMPOSE_LOCK:
        try:
            VideoClip.write_videofile = _wvf  # type: ignore[method-assign]
            if on_progress:
                on_progress(5.0)
            clipper = VideoClipper(None)  # clip 不需要 ASR 模型
            clipper.lang = "zh"
            video = mpy.VideoFileClip(str(src))
            state = {
                # timestamp_list 直传分支不读这两项，但函数头无条件解包 → 占位
                "recog_res_raw": "",
                "timestamp": [],
                "sentences": build_sentences(lines),
                "video": video,
                "clip_video_file": str(dst),
                "video_filename": str(src),
            }
            out_file, _message, clip_srt = clipper.video_clip(
                "", 0, 0, state, add_sub=False, timestamp_list=ts_list,
            )
        finally:
            VideoClip.write_videofile = _orig_wvf  # type: ignore[method-assign]
            if video is not None:  # 服务常驻 — 必须释放源视频句柄
                try:
                    video.close()
                except Exception:  # noqa: BLE001
                    pass
            # 清掉上游写的临时音轨残片（正常路径已由 moviepy 删，兜底）
            for p in dst.parent.glob(dst.stem + "_tempaudio_no*.mp4"):
                try:
                    p.unlink()
                except OSError:
                    pass

    out_path = Path(out_file)
    if not out_path.exists():
        raise RuntimeError(f"上游合成未产出文件: {out_file}")
    os.replace(out_path, dst)
    if clip_srt:  # 随片字幕副产物（正式场景即 rough_compose.srt）
        dst.with_suffix(".srt").write_text(clip_srt, encoding="utf-8")
    dur = _probe_duration(dst)
    return {
        "duration": dur,
        "size_mb": round(dst.stat().st_size / 1024 / 1024, 1),
        "segments": len(merged_ms),
        "raw_segments": raw_n,
        "keep_sec": round(total_keep_ms / 1000.0, 1),
        "elapsed": round(time.time() - t0, 1),
    }


def _probe_duration(p: Path) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip()) if r.returncode == 0 else None
    except (ValueError, subprocess.TimeoutExpired):
        return None


def _probe_av_durations(p: Path) -> tuple[float | None, float | None]:
    """逐流时长 (video, audio) — 优化成片编码后 A/V 一致性校验用。

    format=duration 是容器时长（取最长流），分不出音轨被截断；必须逐流看。
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=30,
        )
        v = a = None
        for row in r.stdout.splitlines():
            fields = (row or "").strip().split(",")
            if len(fields) >= 2 and fields[0] in ("video", "audio"):
                try:
                    d = float(fields[-1])
                except ValueError:
                    continue
                if fields[0] == "video":
                    v = d
                else:
                    a = d
        return v, a
    except subprocess.TimeoutExpired:
        return None, None


# =============== 后台 job 管理（对齐 asr_service 范式） ===============

# task_id → {"state": "running|done|error", "progress": 0-100, "error": str|None,
#            "started_at": float, "finished_at": float|None, "result": dict|None}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def job_status(task_id: str) -> dict | None:
    with _JOBS_LOCK:
        j = _JOBS.get(task_id)
        return dict(j) if j else None


def start_compose(task_id: str, video_path: Path, intervals_ms: list[tuple[int, int]],
                  dst: Path, lines: list[dict], *, auto: bool = False,
                  auto_session_id: str = "") -> bool:
    """启动合成线程。已在跑 → 返回 False（不重复起）。

    REQ-20260919-075：auto 透传到 execution_history。
    REQ-20260920-081：auto_session_id 透传，同一次自动流的多次操作共用 session_id。
    """
    with _JOBS_LOCK:
        existing = _JOBS.get(task_id)
        if existing and existing.get("state") == "running":
            return False
        _JOBS[task_id] = {
            "state": "running", "progress": 0.0, "error": None,
            "started_at": time.time(), "finished_at": None, "result": None,
        }

    # REQ-20260918-048：执行历史（落盘，单写锁）
    outputs_dir = dst.parent
    exec_id = execution_history.record_start(
        outputs_dir, execution_history.KIND_ROUGH_COMPOSE,
        extra={"intervals": len(intervals_ms), "lines": len(lines)},
        auto=auto,
        auto_session_id=auto_session_id)

    def _run():
        job = _JOBS[task_id]

        def _tick(pct: float) -> None:
            job["progress"] = round(pct, 1)

        try:
            result = compose_rough_cut(video_path, intervals_ms, dst, lines, on_progress=_tick)
            job["result"] = result
            job["progress"] = 100.0
            job["state"] = "done"
            job["finished_at"] = time.time()
            log.info("[compose][%s] 完成：%s 段 → %s", task_id, result["segments"], dst.name)
            execution_history.patch_extra(outputs_dir, exec_id,
                                          {"segments": result.get("segments"),
                                           "output": dst.name})
            # REQ-20260919-075：完成时回填具体执行情况（段数 + 输出文件 + 合成时长）
            n_seg = result.get("segments") or 0
            execution_history.patch_fields(outputs_dir, exec_id, {
                "description": (f"ffmpeg 拼接 {n_seg} 段片段，输出初剪视频 {dst.name}（{len(intervals_ms)} 个区间）")
            })
            execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
        except Exception as e:  # noqa: BLE001 — 后台线程必须全兜底
            job["state"] = "error"
            job["error"] = str(e)
            job["finished_at"] = time.time()
            log.exception("[compose][%s] 合成失败", task_id)
            execution_history.record_finish(outputs_dir, exec_id, success=False, error=str(e))

    threading.Thread(target=_run, name=f"rough-compose-{task_id}", daemon=True).start()
    return True


# =============== 优化成片剪辑（REQ-20260922-NNN 标记删除行） ===============
#
# 优化字幕阶段人工标记删除行 → 保存后把删除行的时间区间从 rough_compose.mp4
# 剪除，产出 optimize_compose.mp4（精剪合成阶段自动优先用它）。删除量通常
# 很少，直接 ffmpeg 单趟重编码远快于重跑粗剪合成（moviepy 全片重编码 +
# 重识别 + LLM）。字幕时间轴按剪除量前移（deleted_intervals_ms / shift_ms
# 在 optimize_service，纯函数可单测）。

OPTIMIZE_COMPOSE_NAME = "optimize_compose.mp4"
OPTIMIZE_COMPOSE_TMP_NAME = "optimize_compose.tmp.mp4"
OPTIMIZE_CUT_JSON = "optimize_cut.json"


def optimize_compose_path(outputs_dir: Path) -> Path:
    """优化成片固定产物路径 outputs/optimize_compose.mp4。"""
    return Path(outputs_dir) / OPTIMIZE_COMPOSE_NAME


def complement_intervals_ms(del_ms: list[list[int]], total_ms: int) -> list[list[int]]:
    """删除区间的补集 = 保留区间（[0, total_ms] 内，clamp + 丢 <1ms 碎片）。

    与 merge_intervals_ms 同口径：缝隙不足 1ms 的保留段视为不存在（ffmpeg
    trim 也不会产出有效帧）。
    """
    total = int(total_ms)
    keep: list[list[int]] = []
    cursor = 0
    for s, e in sorted(del_ms or []):
        s, e = max(0, min(total, int(s))), max(0, min(total, int(e)))
        if e <= s:
            continue
        if s - cursor >= 1:
            keep.append([cursor, s])
        cursor = max(cursor, e)
    if total - cursor >= 1:
        keep.append([cursor, total])
    return keep


def ffmpeg_bin() -> str:
    """编码用 ffmpeg 二进制：优先 imageio_ffmpeg 自带的稳定版。

    PATH 上的 ffmpeg 9.0.1（gyan full build）在 trim/concat filter 图 +
    长片上有回归：68 分钟三段剪除实测三种死法 — rc=0 但音频 ~900s 起数字
    静默且末段音轨缺失 / 编码尾部 muxer ENOMEM 崩（rc=-12）/ 中途 ENOMEM
    崩；同一条命令换 imageio 自带的 7.1 一次通过（A/V 双流 4128.6s 完整、
    全程响亮）。moviepy 依赖 imageio_ffmpeg，粗剪成片正是它编的 — 与粗剪
    同源最稳。拿不到（未装）再回落 PATH。
    """
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — 未装/解析失败 → PATH 兜底
        return "ffmpeg"


def build_optimize_cut_cmd(src: Path, dst: Path, keep_ms: list[list[int]],
                           *, has_audio: bool = True) -> list[str]:
    """优化成片剪辑的 ffmpeg argv（单趟精确重编码，纯函数可单测）。

    每段 trim+setpts（有音轨再 atrim+asetpts）→ concat 拼接。
    不用 -c copy 流复制：关键帧对齐会把被删语音漏回成片且累积字幕失步 —
    本阶段的意义就是精确剪除。

    输入流 [0:v]/[0:a] 在一个 filter_complex 里只能被消费一次：多段时先
    split/asplit 扇出成 n 份再各自 trim/atrim（单次消费合法，单段直连）。
    """
    n = len(keep_ms)
    parts: list[str] = []
    labels: list[str] = []
    if n > 1:  # 扇出：每段消费自己的 [svK]/[saK] 副本
        parts.append("[0:v]split=%d%s" % (n, "".join(f"[sv{k}]" for k in range(n))))
        if has_audio:
            parts.append("[0:a]asplit=%d%s" % (n, "".join(f"[sa{k}]" for k in range(n))))
    for k, (s, e) in enumerate(keep_ms):
        vsrc = f"[sv{k}]" if n > 1 else "[0:v]"
        asrc = f"[sa{k}]" if n > 1 else "[0:a]"
        ss, se = f"{s / 1000:.3f}", f"{e / 1000:.3f}"
        parts.append(f"{vsrc}trim=start={ss}:end={se},setpts=PTS-STARTPTS[v{k}]")
        if has_audio:
            parts.append(f"{asrc}atrim=start={ss}:end={se},asetpts=PTS-STARTPTS[a{k}]")
            labels.extend((f"[v{k}]", f"[a{k}]"))
        else:
            labels.append(f"[v{k}]")
    if has_audio:
        parts.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[vout][aout]")
    else:
        parts.append(f"{''.join(labels)}concat=n={n}:v=1:a=0[vout]")
    cmd = [ffmpeg_bin(), "-y", "-i", str(src),
           "-filter_complex", ";".join(parts)]
    if has_audio:
        cmd += ["-map", "[vout]", "-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-map", "[vout]"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(dst)]
    return cmd


def _delete_cut_artifacts(outputs_dir: Path) -> list[str]:
    """删优化成片三件产物 + tmp 半成品（逐个 try，返回实际删掉的文件名）。"""
    out_dir = Path(outputs_dir)
    removed: list[str] = []
    for name in (OPTIMIZE_COMPOSE_NAME, "optimize_compose.srt",
                 OPTIMIZE_CUT_JSON, OPTIMIZE_COMPOSE_TMP_NAME):
        p = out_dir / name
        if p.exists():
            try:
                p.unlink()
                removed.append(name)
            except OSError as e:
                log.warning("[opt-cut] 删除失败 %s: %s", p, e)
    return removed


def _norm_cut_plan(plan: dict | list | None) -> dict:
    """剪辑计划规范化（可比较形态）。

    接受 optimize_service.cut_plan 的 dict，或旧调用口径的纯 marks 列表；
    sidecar 里的 plan（JSON 解析回的）也过这里 — 三方比较前统一形态。
    """
    if isinstance(plan, list):  # 旧口径：只有整行标记
        plan = {"marks": plan, "splits": {}, "split_marks": {}}
    if not isinstance(plan, dict):
        plan = {}
    marks: set[int] = set()
    for m in plan.get("marks") or []:
        try:
            marks.add(int(m))
        except (TypeError, ValueError):
            continue
    splits: dict[str, list[list[int]]] = {}
    for key, spans in (plan.get("splits") or {}).items():
        norm: list[list[int]] = []
        for sp in spans or []:
            if isinstance(sp, (list, tuple)) and len(sp) == 2:
                try:
                    norm.append([int(sp[0]), int(sp[1])])
                except (TypeError, ValueError):
                    continue
        if norm:  # 空列表（全保留切分行）不参与比较 — 与「无该切分删除」等价
            splits[str(key)] = norm
    smarks: dict[str, list[int]] = {}
    for key, idx in (plan.get("split_marks") or {}).items():
        try:
            norm_idx = sorted({int(v) for v in idx or []})
        except (TypeError, ValueError):
            continue
        if norm_idx:
            smarks[str(int(key))] = norm_idx
    return {"marks": sorted(marks), "splits": splits, "split_marks": smarks}


def optimize_cut_ready(outputs_dir: Path, plan: dict | list | None) -> dict | None:
    """优化成片是否「就绪」（与当前剪辑计划匹配且不旧于粗剪成片）。不就绪 → None。

    REQ-20260923-NNN：判据从 line_marks 扩展为完整剪辑计划（整行标记 +
    切分行删除子段）。plan 接受 cut_plan 的 dict 或旧口径 marks 列表；
    sidecar 优先读 plan 字段，旧 sidecar（只有 marks）等价于无切分计划。
    单靠 mtime 判新鲜度会被「只翻转一行/一个子段」绕过；且 optimize_compose.mp4
    不旧于 rough_compose.mp4（rough 重合成后自动失效，精剪回退用 rough）。
    """
    out_dir = Path(outputs_dir)
    mp4 = optimize_compose_path(out_dir)
    sidecar = out_dir / OPTIMIZE_CUT_JSON
    rough = rough_compose_path(out_dir)
    if not (mp4.exists() and sidecar.exists() and rough.exists()):
        return None
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("[opt-cut] sidecar 损坏: %s", e)
        return None
    want = _norm_cut_plan(plan)
    have = _norm_cut_plan(meta.get("plan") if isinstance(meta.get("plan"), dict)
                          else meta.get("marks") or [])
    if want != have:
        return None
    if mp4.stat().st_mtime < rough.stat().st_mtime:
        return None
    return meta


# job 表结构：{"state": "running|done|error", "progress": 0-100, "stage": str,
#             "error": str|None, "started_at", "finished_at", "result": dict|None}
_OPT_CUT_JOBS: dict[str, dict] = {}
_OPT_CUT_JOBS_LOCK = threading.Lock()


def opt_cut_job_status(task_id: str) -> dict | None:
    with _OPT_CUT_JOBS_LOCK:
        j = _OPT_CUT_JOBS.get(task_id)
        return dict(j) if j else None


def opt_cut_status(task_id: str, outputs_dir: Path,
                   plan: dict | list | None = None) -> dict:
    """剪辑状态：内存 job 优先，服务重启后按磁盘兜底。

    兜底口径：sidecar 与当前剪辑计划（cut_plan 或旧口径 marks）匹配且不旧于
    rough → done；只剩 tmp 半成品 → error（提示重存触发）；否则 idle。"""
    job = opt_cut_job_status(task_id)
    if job:
        return job
    ready = optimize_cut_ready(outputs_dir, plan)
    if ready is not None:
        return {"state": "done", "progress": 100.0, "stage": "完成",
                "error": None, "result": ready}
    if (Path(outputs_dir) / OPTIMIZE_COMPOSE_TMP_NAME).exists():
        return {"state": "error", "progress": 0.0, "stage": "失败",
                "error": "上次剪辑未完成（服务重启？）— 重新保存优化字幕可再次触发",
                "result": None}
    return {"state": "idle", "progress": 0.0, "stage": "", "error": None, "result": None}


def start_optimize_cut(task_id: str, outputs_dir: Path, src: Path, *,
                       auto: bool = False, auto_session_id: str = "") -> bool:
    """启动优化成片剪辑线程。已在跑 → False（不重复起）。

    R1（autosave 竞态）：编码耗时分钟级，期间用户可能继续翻转标记/切分子段
    → 每轮编码完成后重读 optimize_subtitle.json 复核剪辑计划（cut_plan：
    整行标记 + 删除子段）；不一致删 tmp 重跑（≤3 次）；计划变空 → 线程自清
    产物直接完成。保存端点只 kick。
    R3（crash-safe）：先写 optimize_compose.tmp.mp4，成功后 os.replace —
    optimize_compose.mp4 永无半截态。
    """
    with _OPT_CUT_JOBS_LOCK:
        existing = _OPT_CUT_JOBS.get(task_id)
        if existing and existing.get("state") == "running":
            return False
        _OPT_CUT_JOBS[task_id] = {
            "state": "running", "progress": 0.0, "stage": "准备", "error": None,
            "started_at": time.time(), "finished_at": None, "result": None,
        }

    from slirn_home import optimize_service

    outputs_dir = Path(outputs_dir)
    src = Path(src)
    exec_id = execution_history.record_start(
        outputs_dir, execution_history.KIND_OPTIMIZE_CUT,
        extra={"src": src.name}, auto=auto, auto_session_id=auto_session_id)

    def _run():
        import io

        from slirn_home import asr_service

        job = _OPT_CUT_JOBS[task_id]
        t0 = time.time()
        try:
            for attempt in range(3):
                data = optimize_service.load_optimize(outputs_dir)
                if data is None:
                    raise RuntimeError("optimize_subtitle.json 不存在")
                # REQ-20260923-NNN：剪辑计划 = 整行标记 + 切分行删除子段
                plan = optimize_service.cut_plan(data)
                if not optimize_service.plan_has_deletions(plan):
                    # 计划已空（保存时全部取消 / 切分子段全部保留）→ 自清产物，直接完成
                    removed = _delete_cut_artifacts(outputs_dir)
                    job.update({"state": "done", "progress": 100.0, "stage": "无需剪辑",
                                "finished_at": time.time(),
                                "result": {"cleared": True, "removed": removed}})
                    log.info("[opt-cut][%s] 剪辑计划已空，清理产物: %s", task_id, removed)
                    execution_history.patch_fields(outputs_dir, exec_id, {
                        "description": "剪辑计划已空（无删除行/子段），清理优化成片产物（无剪辑）"})
                    execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
                    return
                # 已就绪且与当前计划一致 → 幂等直接完成（防重复 kick 白编）
                ready = optimize_cut_ready(outputs_dir, plan)
                if ready is not None:
                    job.update({"state": "done", "progress": 100.0, "stage": "完成",
                                "finished_at": time.time(),
                                "result": {**ready, "already": True}})
                    execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
                    return

                segments = data.get("segments") or []
                del_ms = optimize_service.deleted_intervals_ms(
                    segments, data.get("line_marks") or [],
                    data.get("line_splits"), data.get("split_marks"))
                if not del_ms:
                    raise RuntimeError("剪辑计划没有对应的有效时间段")
                job["stage"] = "读取源视频"
                dur = _probe_duration(src)
                if not dur:
                    raise RuntimeError(f"读不到源视频时长: {src}")
                total_ms = int(dur * 1000)
                keep_ms = complement_intervals_ms(del_ms, total_ms)
                if not keep_ms:
                    raise RuntimeError("全部内容都在剪除计划里 — 请至少保留一部分字幕")
                total_keep_ms = sum(e - s for s, e in keep_ms)
                deleted_ms = total_ms - total_keep_ms
                n_marks = len(plan["marks"])
                n_subs = sum(len(v) for v in plan["split_marks"].values())

                job["stage"] = "剪辑中"
                has_audio = asr_service.has_audio_track(src)
                dst_tmp = outputs_dir / OPTIMIZE_COMPOSE_TMP_NAME
                cmd = build_optimize_cut_cmd(src, dst_tmp, keep_ms, has_audio=has_audio)
                log.info("[opt-cut][%s] 第 %d 次剪辑：删 %d 区间（%d 行 + %d 子段）"
                         "%.1fs → 保留 %.1fs",
                         task_id, attempt + 1, len(del_ms), n_marks, n_subs,
                         deleted_ms / 1000, total_keep_ms / 1000)
                # 进度解析照搬 app.py 精剪导出（Windows 8KB 缓冲 + stderr 死锁教训）
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                proc.stdout = io.TextIOWrapper(
                    proc.stdout, encoding="utf-8", newline="\n", line_buffering=True)
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        continue
                    line = line.strip()
                    if "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    if key in ("out_time_ms", "out_time_us"):
                        try:
                            t = int(val) if key == "out_time_ms" else int(val) // 1000
                        except ValueError:
                            continue
                        # 输出时间轴总长 = 保留时长（concat 后重置）
                        job["progress"] = round(
                            5.0 + min(1.0, t / max(1, total_keep_ms)) * 93.0, 1)
                rc = proc.wait()
                if rc != 0:
                    try:
                        dst_tmp.unlink()
                    except OSError:
                        pass
                    raise RuntimeError(f"ffmpeg 剪辑失败（退出码 {rc}）")

                # 编码后 A/V 时长复核：rc=0 也可能产出坏文件（历史上
                # filter_complex 多次消费 [0:a] 导致音频中途数字静默 + 末段
                # 缺失，就是 rc=0 混过所有检查的）。视频/音频流时长都应
                # ≈ 保留总时长；背离 >1s 判坏，自清产物报错重试。
                expect_sec = total_keep_ms / 1000.0
                if has_audio:
                    v_dur, a_dur = _probe_av_durations(dst_tmp)
                else:
                    v_dur, _ = _probe_av_durations(dst_tmp)
                    a_dur = None
                if v_dur is None or abs(v_dur - expect_sec) > 1.0 or (
                        has_audio and (a_dur is None or abs(a_dur - expect_sec) > 1.0)):
                    try:
                        dst_tmp.unlink()
                    except OSError:
                        pass
                    raise RuntimeError(
                        f"优化成片 A/V 时长校验失败（视频 {v_dur}s / 音频 {a_dur}s，"
                        f"应约 {expect_sec:.1f}s）— 已丢弃坏产物，请重试")

                # R1 复核：编码期间剪辑计划可能又变了（autosave/resplit 不经过保存端点）
                data2 = optimize_service.load_optimize(outputs_dir)
                plan2 = optimize_service.cut_plan(data2 or {})
                if plan2 != plan:
                    log.info("[opt-cut][%s] 剪辑期间计划已变化，重跑", task_id)
                    try:
                        dst_tmp.unlink()
                    except OSError:
                        pass
                    continue  # attempt 下一轮（≤3）

                # R3：原子落盘 + 随片字幕（时间轴前移）+ sidecar
                final = optimize_compose_path(outputs_dir)
                os.replace(dst_tmp, final)
                segs_now = (data2 or {}).get("segments") or segments
                final.with_suffix(".srt").write_text(
                    optimize_service.build_srt(
                        segs_now, plan["marks"], shift=True,
                        line_splits=(data2 or {}).get("line_splits"),
                        split_marks=(data2 or {}).get("split_marks"),
                        occurrences=(data2 or {}).get("occurrences")),
                    encoding="utf-8")
                meta = {
                    "marks": plan["marks"],
                    "plan": plan,  # REQ-20260923-NNN：完整剪辑计划（新鲜度比较基准）
                    "split_subs": n_subs,
                    "intervals": len(del_ms),
                    "deleted_ms": deleted_ms,
                    "deleted_sec": round(deleted_ms / 1000.0, 1),
                    "kept_sec": round(total_keep_ms / 1000.0, 1),
                    "src": src.name,
                    "src_mtime": src.stat().st_mtime,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "elapsed": round(time.time() - t0, 1),
                }
                (outputs_dir / OPTIMIZE_CUT_JSON).write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                dur2 = _probe_duration(final)
                result = {**meta, "duration": dur2,
                          "size_mb": round(final.stat().st_size / 1024 / 1024, 1)}
                job.update({"state": "done", "progress": 100.0, "stage": "完成",
                            "finished_at": time.time(), "result": result})
                log.info("[opt-cut][%s] 完成：删 %.1fs 保留 %.1fs → %s（%.1fs）",
                         task_id, deleted_ms / 1000, total_keep_ms / 1000,
                         final.name, result["elapsed"])
                execution_history.patch_extra(outputs_dir, exec_id, {
                    "lines": n_marks, "split_subs": n_subs,
                    "deleted_sec": meta["deleted_sec"], "output": final.name})
                _what = f"{n_marks} 行" + (f" + {n_subs} 子段" if n_subs else "")
                execution_history.patch_fields(outputs_dir, exec_id, {
                    "description": (f"剪除 {_what}（{meta['deleted_sec']}s），"
                                    f"输出优化成片 {final.name}（{meta['kept_sec']}s）")})
                execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
                return
            # 3 轮计划都在变（用户持续编辑中）→ 提示稳定后再保存
            raise RuntimeError("剪辑期间剪辑计划持续变化（已重试 3 次）— "
                               "内容稳定后请再次触发重新拼接")
        except Exception as e:  # noqa: BLE001 — 后台线程必须全兜底
            job["state"] = "error"
            job["error"] = str(e)
            job["stage"] = "失败"
            job["finished_at"] = time.time()
            log.exception("[opt-cut][%s] 剪辑失败", task_id)
            execution_history.record_finish(outputs_dir, exec_id, success=False, error=str(e))

    threading.Thread(target=_run, name=f"optimize-cut-{task_id}", daemon=True).start()
    return True
