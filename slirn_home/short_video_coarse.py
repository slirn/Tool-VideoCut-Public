"""短视频工作台 Stage 4（粗剪合成）执行模块。

REQ-20261003-098：第 4 阶段读 ``stage3/highlights.json`` + 源视频，
对每条 highlight 调 ``moviepy.editor.VideoFileClip.subclip(start, end)`` 导 mp4。
产物 ``stage4/coarse_NN.mp4`` + ``stage4/coarse_NN.srt``。

实现要点：

- MoviePy 的 ``video.audio.write_audiofile`` 在本机对部分短视频会触发 numpy 内存错误
  （参见 highlight_picker.py 的 bypass 路径）。本模块绕开它，直接用 ffmpeg 抽 wav + librosa
  不行（这里我们用 moviepy.subclip 即可，不需要 ffmpeg 抽 audio）。
- 每条 highlight 独立 try，一条失败不阻塞其他。
- MoviePy 加载视频到内存；源视频很大时内存占用高 —— 控制在源视频 ≤ 500 MB 时使用，
  否则建议后续扩展 ffmpeg 直接 subclip（避免全片 decode）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from slirn_home import short_video_service as svc

log = logging.getLogger(__name__)


@dataclass
class CoarseClipResult:
    index: int
    coarse_mp4: Path
    coarse_srt: Path
    status: str        # done / failed
    duration_sec: float
    elapsed_sec: int
    error: str = ""


def _clamp_segments_ms(pairs_ms, vid_dur_sec: float) -> list[tuple[float, float]]:
    """毫秒区间列表 → 秒区间列表，并夹在视频时长内（返回可能为空）。

    REQ-20261015-118 回归修复：修复前把 min(vid_dur, 毫秒值) 的结果再除
    1000 —— 秒（video.duration）和毫秒（字幕时间戳）直接比大小，任何起点
    晚于 vid_dur*1000 毫秒（≈6.1s）的区间都被夹到 ≤ vid_dur 秒、比起点
    还小，整段被丢 → 全部 clip 报「segments 为空或全部无效」粗剪失败。
    除法必须放在 min 之前（先统一单位再比较）。
    """
    out: list[tuple[float, float]] = []
    for s_ms, e_ms in pairs_ms:
        s_sec = max(0, s_ms) / 1000.0
        e_sec = min(vid_dur_sec, e_ms / 1000.0)
        if e_sec <= s_sec:
            continue
        out.append((s_sec, e_sec))
    return out


# ---------- REQ-20261013-103：异步粗剪（仿 compose_service.start_compose） ----------

# 内存 job 表：key = (task_id, project_id) 元组，区分不同项目
_JOBS: dict[tuple[str, str], dict] = {}
_JOBS_LOCK = threading.Lock()


def _job_key(task_id: str, project_id: str) -> tuple[str, str]:
    return (str(task_id or ""), str(project_id or ""))


def coarse_job_status(task_id: str, project_id: str) -> dict | None:
    """读内存 job 状态；服务重启后内存清空 → 返回 None（前端按 idle 处理）。"""
    with _JOBS_LOCK:
        j = _JOBS.get(_job_key(task_id, project_id))
        return dict(j) if j else None


def _set_job(task_id: str, project_id: str, **fields) -> None:
    """原子更新 _JOBS[(tid, pid)]；保留未传字段。"""
    with _JOBS_LOCK:
        key = _job_key(task_id, project_id)
        cur = _JOBS.get(key) or {"state": "idle", "progress": 0.0,
                                  "stage": "", "error": None,
                                  "started_at": 0.0, "finished_at": None,
                                  "count": 0, "total_count": 0}
        cur.update(fields)
        _JOBS[key] = cur


def start_coarse_async(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    *,
    start_ost_ms: int = 0,
    end_ost_ms: int = 100,
    hl_index: int | None = None,
) -> bool:
    """启动异步粗剪线程。已在跑 → False。

    仿 ``compose_service.start_compose`` 模式：daemon 线程跑 run_stage5，
    通过 progress_cb 回调更新 _JOBS[(tid, pid)] 进度。

    REQ-20261013-108：hl_index 可选 — 透传给 run_stage5 做单 highlight 粗剪。
    """
    key = _job_key(task_id, project_id)
    with _JOBS_LOCK:
        existing = _JOBS.get(key)
        if existing and existing.get("state") == "running":
            return False
        _JOBS[key] = {
            "state": "running", "progress": 0.0, "stage": "准备",
            "error": None, "started_at": time.time(), "finished_at": None,
            "count": 0, "total_count": 0,
        }

    def _run():
        # 进度回调：每个 clip done 时更新 progress / count
        def _cb(event: str, payload: dict) -> None:
            if event == "stage4_start":
                _set_job(task_id, project_id,
                         total_count=int(payload.get("count") or 0),
                         stage="合成中", progress=5.0)
            elif event == "stage4_clip_done":
                done = _JOBS.get(key, {}).get("count", 0) + 1
                total = max(1, _JOBS.get(key, {}).get("total_count", 1))
                _set_job(task_id, project_id,
                         count=done,
                         progress=round(5.0 + 90.0 * done / total, 1),
                         stage=f"合成 {done}/{total}")
            elif event == "stage4_clip_failed":
                done = _JOBS.get(key, {}).get("count", 0) + 1
                _set_job(task_id, project_id, count=done)
            elif event in ("stage4_done", "stage4_failed"):
                _set_job(task_id, project_id,
                         state="done" if event == "stage4_done" else "failed",
                         progress=100.0 if event == "stage4_done" else _JOBS.get(key, {}).get("progress", 0.0),
                         stage="完成" if event == "stage4_done" else "失败",
                         finished_at=time.time())

        # REQ-20261022-144：写执行历史到**任务级** outputs（与长视频同一文件，
        # list_logs 才查得到；此前写 stage5_dir 根本看不见）
        from slirn_home import execution_history
        stage5_dir = svc.sv_logs_dir(repo_root, task_id)
        exec_id = execution_history.record_start(
            stage5_dir,
            execution_history.KIND_SHORT_VIDEO_COARSE,
            extra={"start_ost_ms": start_ost_ms, "end_ost_ms": end_ost_ms},
        )
        try:
            run_stage5(repo_root, task_id, project_id,
                       start_ost_ms=start_ost_ms, end_ost_ms=end_ost_ms,
                       progress_cb=_cb,
                       hl_index=hl_index)
            # run_stage5 内部已通过 _cb 把 state 推到 done/failed；这里取最终
            final = _JOBS.get(key, {})
            ok = final.get("state") == "done"
            execution_history.record_finish(
                stage5_dir, exec_id, success=ok,
                error="" if ok else str(final.get("error") or "粗剪合成未全部成功"),
            )
        except Exception as e:  # noqa: BLE001 — 后台线程兜底
            log.exception("[stage5_async][%s/%s] 失败: %s", task_id, project_id, e)
            _set_job(task_id, project_id,
                     state="failed", error=str(e),
                     finished_at=time.time())
            try:
                execution_history.record_finish(
                    stage5_dir, exec_id, success=False, error=str(e)[:500],
                )
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_run, name=f"sv-stage5-{task_id}-{project_id}",
                     daemon=True).start()
    return True


def _write_coarse_srt(
    out_srt: Path,
    highlight: dict,
    *,
    segments: list[tuple[int, int]] | None = None,
    line_windows: list[list[tuple[int, int]]] | None = None,
) -> None:
    """把 highlight.subtitle_lines 写成 SRT 文本。

    - line_windows 不为 None（REQ-20261016-147）：与 subtitle_lines **严格
      平行**的每行窗口组；行时长 = sum(本行窗口)，跨行累加；空组行整行跳过
      （SRT 与视频内容一致，也修复了旧 positional 配对在 _resolve 跳行时
      整体错位的问题）。
    - segments 不为 None：按 segments 顺序累加真实时长写时间戳（多窗拼接
      模式，legacy）。subtitle_lines[i] 与 segments[i] 一一对应。
    - 两者皆 None：保留旧行为，时间戳全部 00:00:00,000 占位（Stage 5 重
      ASR 后覆盖）。
    """
    lines = highlight.get("subtitle_lines") or []
    if not lines:
        out_srt.write_text("", encoding="utf-8")
        return
    out_lines: list[str] = []
    cumulative_ms = 0
    for i, sl in enumerate(lines, start=1):
        text = str(sl.get("text") or "").strip()
        if not text:
            continue
        if line_windows is not None and (i - 1) < len(line_windows):
            windows = [w for w in (line_windows[i - 1] or []) if w[1] > w[0]]
            if not windows:
                continue  # 空组行整行跳过（该行不进视频）
            dur = sum(e - s for s, e in windows)
            sub_start = cumulative_ms
            sub_end = cumulative_ms + dur
            cumulative_ms += dur
            out_lines.append(str(i))
            out_lines.append(f"{_fmt_srt_time(sub_start)} --> {_fmt_srt_time(sub_end)}")
        elif segments is not None and (i - 1) < len(segments):
            seg_start, seg_end = segments[i - 1]
            seg_dur = max(0, seg_end - seg_start)
            sub_start = cumulative_ms
            sub_end = cumulative_ms + seg_dur
            cumulative_ms += seg_dur
            start_s = _fmt_srt_time(sub_start)
            end_s = _fmt_srt_time(sub_end)
            out_lines.append(str(i))
            out_lines.append(f"{start_s} --> {end_s}")
        else:
            out_lines.append(str(i))
            out_lines.append(f"00:00:00,000 --> 00:00:00,000")
        out_lines.append(text)
        out_lines.append("")
    out_srt.write_text("\n".join(out_lines), encoding="utf-8")


def _fmt_srt_time(ms: int) -> str:
    """毫秒 → ``HH:MM:SS,mmm`` 格式。"""
    h, rem = divmod(max(0, ms), 3600 * 1000)
    m, rem = divmod(rem, 60 * 1000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _coarse_one(
    source_video: Path,
    highlight: dict,
    out_mp4: Path,
    out_srt: Path,
    *,
    start_ost_ms: int = 0,
    end_ost_ms: int = 100,
    segments: list[tuple[int, int]] | None = None,
    line_windows: list[list[tuple[int, int]]] | None = None,
) -> CoarseClipResult:
    """粗剪一条 highlight。

    segments 不为 None：按多段（毫秒）顺序切片 + 拼接；用于按 subtitle_lines 时间戳切。
    segments 为 None：保留旧行为，按 highlight 整体 [start_ms, end_ms] 一刀切。
    line_windows（REQ-20261016-147）：与 subtitle_lines 平行的每行窗口组
    （剪词 keep 段），透传给 _write_coarse_srt 做逐行配对时长；视频切片
    仍按 segments（flat 展开后的窗口序列）。
    """
    index = max(1, int(highlight.get("index") or 0))
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    # 先写 srt（即使后面 mp4 失败也落地，方便排查）
    _write_coarse_srt(out_srt, highlight, segments=segments, line_windows=line_windows)

    # 决定 segments 实际用哪一组
    if segments is None:
        start_ms = max(0, int(highlight.get("start_ms") or 0)) + start_ost_ms
        end_ms = max(start_ms, int(highlight.get("end_ms") or 0)) + end_ost_ms
        segments = [(start_ms, end_ms)]

    t0 = time.time()
    # 快路径：ffmpeg 单趟 trim+concat（粗剪合成/优化成片同款机制 — moviepy
    # 的 Python 逐帧管道是该阶段耗时大头，preset 只是零头）。失败回落
    # moviepy 老路兜底（快路径只许快，不许拦路）。
    tmp: Path | None = None
    try:
        import os
        import subprocess

        from slirn_home import asr_service as _asr
        from slirn_home import compose_service as _cs

        vid_dur = _cs._probe_duration(source_video)
        seg_list = (_clamp_segments_ms(segments, vid_dur) if vid_dur
                    else [(max(0, s) / 1000.0, e / 1000.0) for s, e in segments])
        if not seg_list:
            raise ValueError("segments 为空或全部无效")
        tmp = out_mp4.with_name(out_mp4.stem + ".tmp.mp4")
        cmd = _cs.build_optimize_cut_cmd(
            source_video, tmp,
            [[int(s * 1000), int(e * 1000)] for s, e in seg_list],
            has_audio=_asr.has_audio_track(source_video))
        r = subprocess.run(cmd, capture_output=True, timeout=600,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
            raise RuntimeError(f"ffmpeg rc={r.returncode}: {(r.stderr or '')[-300:]}")
        os.replace(tmp, out_mp4)
    except Exception as e_fast:  # noqa: BLE001
        log.warning("[sv-coarse] ffmpeg 快路径失败，回落 moviepy: %s", e_fast)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            import moviepy.editor as mpy
            video = mpy.VideoFileClip(str(source_video))
            try:
                # 把所有 segments 限制在 video.duration 内
                vid_dur = video.duration
                seg_list = _clamp_segments_ms(segments, vid_dur)
                if not seg_list:
                    raise ValueError("segments 为空或全部无效")

                clips = [video.subclip(s, e) for s, e in seg_list]
                try:
                    if len(clips) == 1:
                        final = clips[0]
                    else:
                        final = mpy.concatenate_videoclips(clips)
                    try:
                        final.write_videofile(
                            str(out_mp4),
                            audio_codec="aac",
                            codec="libx264",
                            preset="medium",
                            verbose=False,
                            logger=None,
                        )
                    finally:
                        final.close()
                finally:
                    for c in clips:
                        try:
                            c.close()
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                video.close()
        except Exception as e:  # noqa: BLE001
            return CoarseClipResult(
                index=index, coarse_mp4=out_mp4, coarse_srt=out_srt,
                status="failed", duration_sec=0.0,
                elapsed_sec=int(time.time() - t0),
                error=f"moviepy subclip/concat 失败: {e}",
            )

    elapsed = int(time.time() - t0)
    if not out_mp4.is_file() or out_mp4.stat().st_size < 1024:
        return CoarseClipResult(
            index=index, coarse_mp4=out_mp4, coarse_srt=out_srt,
            status="failed", duration_sec=0.0,
            elapsed_sec=elapsed,
            error="write_videofile 未产出有效 mp4",
        )

    # 读时长做记录（实际产出可能与 segments 累加有 PTS 漂移）
    duration_sec = sum(max(0, e - s) for s, e in segments) / 1000.0
    try:
        import subprocess
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(out_mp4)],
            capture_output=True, text=True, timeout=30, encoding="utf-8",
        )
        if probe.returncode == 0:
            duration_sec = max(0.0, float((probe.stdout or "").strip()))
    except Exception:  # noqa: BLE001
        pass

    return CoarseClipResult(
        index=index, coarse_mp4=out_mp4, coarse_srt=out_srt,
        status="done", duration_sec=duration_sec, elapsed_sec=elapsed,
    )


def _resolve_line_windows(
    highlight: dict,
    trims: dict | None,
    srt_index_to_line: dict,
    restores: list | None = None,
    bases: dict | None = None,
) -> list[list[tuple[int, int]]]:
    """REQ-20261016-147：subtitle_lines → 每行的视频窗口组（严格平行输出）。

    与 short_video_optimize.effective_subtitle_lines 同口径（预览/粗剪一致）：
    - 唯一 sid 在 raw.srt → 整窗；该 sid 有剪词（trims）→ trim_keep_segments
      的 keep 窗口组（对不上回退整窗 — 宁可不切）；
    - REQ-20261019-219：优化表烘焙的 LLM 剪词行（keep_segments 在行上）
      → 直接用烘焙的 keep 窗口组；被 line_restores 还原的行脱掉烘焙按整窗；
      用户对 LLM 剪词行再剪词时以原文（trimmed_from）为对齐基准 + raw.srt
      整窗（与 effective 口径一致）；
    - REQ-20261019-223：``bases``（line_bases）优先作对齐基准 — 剪词时把
      改字（ASR 错词纠错）烘进去的基准，窗口按它算，改字不切音频；
    - sid 重复（LLM split 子行共用）→ 兄弟行按字数占比切分父窗（不重复），
      剪词跳过；
    - 解析不出 → 空组（该行不进视频；_write_coarse_srt 按空组跳过 SRT 行）。
    """
    from slirn_home import short_video_optimize as _sv_opt

    lines = [sl for sl in (highlight.get("subtitle_lines") or [])
             if isinstance(sl, dict)]
    sid_counts: dict[int, int] = {}
    for sl in lines:
        try:
            sid = int(sl.get("src_index") or 0)
        except (TypeError, ValueError):
            sid = 0
        sid_counts[sid] = sid_counts.get(sid, 0) + 1
    trims = trims or {}
    restores = {int(x) for x in (restores or [])}
    # 重复 sid（LLM split 子行）→ 字数占比切分父窗（父窗口不再重复 N 次）
    dup_windows: dict[int, list[tuple[int, int]]] = {}
    for sid, cnt in sid_counts.items():
        if cnt > 1:
            ln = srt_index_to_line.get(sid)
            if ln is None or ln.end_ms <= ln.start_ms:
                continue
            sib_texts = [str(x.get("text") or "") for x in lines
                         if int(x.get("src_index") or 0) == sid]
            dup_windows[sid] = _sv_opt.share_split_windows(
                sib_texts, int(ln.start_ms), int(ln.end_ms))
    out: list[list[tuple[int, int]]] = []
    dup_pos: dict[int, int] = {}
    for sl in lines:
        try:
            sid = int(sl.get("src_index") or 0)
        except (TypeError, ValueError):
            out.append([])
            continue
        if sid in dup_windows:
            j = dup_pos.get(sid, 0)
            dup_pos[sid] = j + 1
            windows = dup_windows[sid]
            out.append([windows[min(j, len(windows) - 1)]])
            continue
        ln = srt_index_to_line.get(sid)
        target = str(trims.get(str(sid)) or "").strip()
        doc_keep = sl.get("keep_segments")
        doc_trimmed = (doc_keep is not None and sl.get("trimmed_from")
                       and sid not in restores)
        if (target and sid_counts.get(sid, 0) == 1
                and ln is not None and ln.end_ms > ln.start_ms):
            try:
                # REQ-20261019-219：LLM 剪词行再剪词 → 对齐基准 = 原文
                # （trimmed_from），窗口 = raw.srt 整窗（与 effective 一致）
                # REQ-20261019-223：line_bases（烘进去的改字基准）优先
                base_text = str(
                    (bases or {}).get(str(sid))
                    or (str(sl.get("trimmed_from")) if doc_trimmed
                        else str(sl.get("text") or "")))
                segs = _sv_opt.trim_keep_segments(
                    base_text, target, int(ln.start_ms), int(ln.end_ms))
                windows = [(int(x["start_ms"]), int(x["end_ms"])) for x in segs]
                if windows:
                    out.append(windows)
                    continue
            except svc.ShortVideoError:
                pass  # 对不上 → 回退烘焙 keep / 整窗（宁可不切）
        if doc_trimmed:
            windows = [(int(x["start_ms"]), int(x["end_ms"]))
                       for x in (doc_keep or [])]
            if windows:
                out.append(windows)
                continue
        if ln is None or ln.end_ms <= ln.start_ms:
            # 行内自带有效时间兜底（同 effective_subtitle_lines 口径；LLM
            # 剪词行已收敛到保留跨度）
            ism = int(sl.get("start_ms") or 0)
            iem = int(sl.get("end_ms") or 0)
            out.append([(ism, iem)] if iem > ism else [])
        else:
            out.append([(int(ln.start_ms), int(ln.end_ms))])
    return out


def _apply_trim_texts(highlight: dict, trims: dict | None,
                      edits: dict | None = None) -> dict:
    """REQ-20261016-148：粗剪 SRT 文字 = 剪词后文字。

    视频侧剪词行只保留 trim_keep_segments 窗口（呃/就等已物理剪掉），SRT
    文字必须同步替换为 trim target，否则字幕与视频不一致 —— 字幕优化阶段
    移除后，粗剪 SRT 是精剪烧录字幕的默认来源，文字必须对得上画面。

    与 _resolve_line_windows 同口径：仅 unique sid 且有 trim target 的行替换。
    REQ-20261019-223：``edits``（line_edits 改字层）存在时优先于 trim
    target（最终文字），且对未剪词的行也生效 — run_stage5 传 ``apply_edits=
    False`` 的行表过来时，本函数是 SRT 文字的唯一替换点。
    返回浅拷贝 highlight（subtitle_lines 逐行浅拷贝），未命中时原样返回。
    """
    trims = trims or {}
    edits = edits or {}
    if not trims and not edits:
        return highlight
    lines = [sl for sl in (highlight.get("subtitle_lines") or [])
             if isinstance(sl, dict)]
    sid_counts: dict[int, int] = {}
    for sl in lines:
        try:
            sid = int(sl.get("src_index") or 0)
        except (TypeError, ValueError):
            sid = 0
        sid_counts[sid] = sid_counts.get(sid, 0) + 1
    changed = False
    new_lines = []
    for sl in lines:
        try:
            sid = int(sl.get("src_index") or 0)
        except (TypeError, ValueError):
            sid = 0
        target = str(trims.get(str(sid)) or "").strip()
        edit_text = str(edits.get(str(sid)) or "").strip()
        if (edit_text or (target and sid_counts.get(sid, 0) == 1)) \
                and (edit_text or target) != str(sl.get("text") or ""):
            row = dict(sl)
            row["text"] = edit_text or target
            new_lines.append(row)
            changed = True
        else:
            new_lines.append(sl)
    if not changed:
        return highlight
    out = dict(highlight)
    out["subtitle_lines"] = new_lines
    return out


def _apply_manual_state(highlight: dict, per: dict,
                        *, apply_edits: bool = True) -> dict:
    """REQ-20261019-210：粗剪消费 Stage 4 手工编辑状态（与
    effective_subtitle_lines 同口径 — 预览看到什么，粗剪就剪什么）：

    - ``line_marks``：手动删行剔除（此前删行仍被剪进视频 = 预览≠粗剪）；
    - ``line_edits``：✏️ 修改后文字（SRT 同步；trim 行与 edits 互斥不动）；
    - ``line_order``：手动重排 → 视频剪切拼接顺序 + SRT 顺序。
      （split 子行不参与重排，整体追加尾部 — apply_line_order 单一口径）
    - REQ-20261019-219：``line_restores``（还原 LLM 剪词）→ 行脱掉优化表
      烘焙的剪词（text 回原文、去 keep_segments）；被用户再剪词的行同样
      以原文为基准（target 由 _apply_trim_texts / _resolve_line_windows 消费）。
    - REQ-20261019-223：``apply_edits=False`` → 跳过改字层（改字不能进
      对齐基准 — 窗口解析用；SRT 文字由 _apply_trim_texts 消费 edits）。

    ``line_trims`` 仍由 _resolve_line_windows / _apply_trim_texts 消费。
    无任何手工状态时原样返回（零开销）。
    """
    from slirn_home import short_video_optimize as _sv_opt

    marks = {int(x) for x in per.get("line_marks") or []}
    edits = per.get("line_edits") or {}
    order = per.get("line_order") or []
    restores = {int(x) for x in per.get("line_restores") or []}
    trims = per.get("line_trims") or {}
    if not marks and not edits and not order and not restores and not trims:
        return highlight
    lines = [sl for sl in (highlight.get("subtitle_lines") or [])
             if isinstance(sl, dict)]
    # REQ-20261019-219：先脱 LLM 剪词烘焙（还原行 / 再剪词行都以原文为基准）
    if restores or trims:
        stripped: list[dict] = []
        for sl in lines:
            try:
                sid = int(sl.get("src_index") or 0)
            except (TypeError, ValueError):
                sid = 0
            if (sl.get("keep_segments") is not None
                    and (sid in restores or str(sid) in trims)):
                row = dict(sl)
                row["text"] = str(sl.get("trimmed_from") or sl.get("text") or "")
                row.pop("keep_segments", None)
                row.pop("trimmed_from", None)
                stripped.append(row)
            else:
                stripped.append(sl)
        lines = stripped
    if marks:
        lines = [sl for sl in lines
                 if int(sl.get("src_index") or 0) not in marks]
    if edits and apply_edits:
        new_lines: list[dict] = []
        for sl in lines:
            sid = str(sl.get("src_index") or 0)
            if sid in edits and str(edits[sid]).strip():
                row = dict(sl)
                row["text"] = str(edits[sid])
                new_lines.append(row)
            else:
                new_lines.append(sl)
        lines = new_lines
    if order:
        lines = _sv_opt.apply_line_order(lines, order)
    out = dict(highlight)
    out["subtitle_lines"] = lines
    return out


def run_stage5(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    *,
    start_ost_ms: int = 0,
    end_ost_ms: int = 100,
    progress_cb=None,
    hl_index: int | None = None,
) -> dict:
    """Stage 5: 读 highlights.json + 源视频 → ``stage5_coarse/coarse_NN.{mp4,srt}``。

    （历史命名 run_stage4 是早期阶段编号漂移的残留 — 产物目录与状态键
    一直是 stage5_coarse；现统一为 run_stage5，旧名保留为兼容别名。）

    REQ-20261013-103：增加 ``progress_cb(event, payload)`` 注入；默认 None
    保持向后兼容（不传回调走原行为）。回调事件：
    - ``stage4_start``: {"count": N}
    - ``stage4_clip_done``: {"index": idx, "count": N, "duration_sec": ...}
    - ``stage4_clip_failed``: {"index": idx, "error": ...}
    - ``stage4_done`` / ``stage4_failed``: {"count": N}

    REQ-20261013-108：增加 ``hl_index`` 可选参数 — 仅跑一条 highlight
    （预览跳转时使用）。``None`` / 0 = 全量（默认，保持向后兼容）；
    int = 只处理 index == hl_index 的那一条，其他 highlight 的 coarse_NN.mp4
    文件**不会被删**（与全量跑行为一致 — 不动既有产物）。

    返回最新 pipeline.stage5_coarse 状态。
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)

    src_id = project.get("base_material_id") or ""
    src = next(
        (m for m in project.get("materials") or []
         if m.get("id") == src_id and m.get("kind") == "video"),
        None,
    )
    if not src:
        raise svc.ShortVideoError("Stage 4 需要先在 Stage 1 选源视频")
    source_video = svc.material_abs_path(root, project, src["id"])

    # REQ-20261017-149：底稿 = Stage 4 分离存储（stage4 done →
    # optimized_highlights.json 优化表；否则回退 stage3/highlights.json 原表）
    from slirn_home import short_video_optimize as _sv_opt

    hl_doc = _sv_opt.load_stage4_base_doc(root, task_id, project_id,
                                          project=project)
    if not hl_doc:
        raise svc.ShortVideoError("Stage 4 需要先跑 Stage 3 生成 highlights.json")
    raw_highlights = hl_doc.get("highlights") or []
    if not raw_highlights:
        raise svc.ShortVideoError("highlights.json 没有有效片段")

    # REQ-20261013-108：hl_index 过滤 — None / 0 / 越界 = 全量；int = 只跑一条。
    # 注意：其他 highlight 的 coarse_NN.{mp4,srt} 文件**不会被删**（只覆盖 hl_index 那条）。
    if hl_index:
        try:
            want = int(hl_index)
        except (TypeError, ValueError):
            want = 0
        if want > 0:
            raw_highlights = [h for h in raw_highlights
                              if int(h.get("index") or 0) == want]
            if not raw_highlights:
                raise svc.ShortVideoError(f"hl#{want} 不存在")

    # 解析 raw.srt：subtitle_lines[].src_index → SRT 行 (start_ms, end_ms)
    # 用于按字幕行时间戳多窗拼接（REQ-20261004-stage3-restructure）。
    srt_path = svc.stage_raw_srt_path(root, task_id, project_id)
    srt_lines: list = []
    if srt_path.is_file():
        try:
            from slirn_home.short_video_analyze import _parse_srt_lines
            srt_lines = _parse_srt_lines(srt_path.read_text(encoding="utf-8"))
        except Exception as parse_err:  # noqa: BLE001
            log.warning("[stage4] raw.srt 解析失败: %s", parse_err)
    srt_index_to_line = {ln.index: ln for ln in srt_lines}

    state = svc.get_stage_state(project, "stage5_coarse")
    state["status"] = "running"
    state["started_at"] = svc.now_iso()
    state["count"] = len(raw_highlights)
    state["logs"] = list(state.get("logs") or []) + [
        f"[{svc.now_iso()}] stage4 start, {len(raw_highlights)} clips",
    ]
    svc.set_stage_state(project, "stage5_coarse", state)
    svc.save_project(root, project)

    if progress_cb:
        try:
            progress_cb("stage4_start", {"count": len(raw_highlights)})
        except Exception as cb_err:  # noqa: BLE001
            log.warning("progress_cb raised: %s", cb_err)

    results: list[CoarseClipResult] = []
    # REQ-20261019-227：粗剪成片字幕（subs_*）随（重）合成作废 — 新视频时间轴
    # 必变，旧 ASR 字幕必错位；下方 highlights 条目重建时 subs_srt 字段自然消失
    from slirn_home import short_video_subs as _sv_subs

    for hl in raw_highlights:
        idx = int(hl.get("index") or (len(results) + 1))
        out_mp4, out_srt = svc.stage4_paths(root, task_id, project_id, idx)
        _sv_subs.invalidate_subs(root, task_id, project_id, idx)
        # REQ-20261016-147：per-hl 剪词（line_trims）→ 每行窗口组（keep 段），
        # 视频按 flat 展开切 + SRT 按行配对时长
        from slirn_home import short_video_optimize as _sv_opt
        per = _sv_opt.load_per_hl(root, task_id, project_id, idx)
        # REQ-20261019-210：手动编辑状态（删行/重排）先并入行表 — 粗剪的视频
        # 剪切拼接顺序跟随 Stage 4 用户视角（同预览）。
        # REQ-20261019-223：改字层（line_edits）不进对齐基准 — 窗口解析用
        # apply_edits=False 的行表（基准 = 原文/line_bases），SRT 文字由
        # _apply_trim_texts 一并消费 edits（最终文字）。
        hl_manual = _apply_manual_state(hl, per, apply_edits=False)
        groups = _resolve_line_windows(hl_manual, per.get("line_trims") or {},
                                       srt_index_to_line,
                                       per.get("line_restores") or [],
                                       per.get("line_bases") or {})
        flat = [w for g in groups for w in g]
        # REQ-20261016-148：写 SRT 用剪词后文字（视频已剪掉呃/就等；精剪
        # 字幕默认来源 = 粗剪 SRT）。窗口组仍按原始 hl 解析（trim 匹配基准）。
        srt_hl = _apply_trim_texts(hl_manual, per.get("line_trims") or {},
                                   per.get("line_edits") or {})
        # 优先按字幕行多窗拼接；无有效 segments 时 fallback 到整体区间切
        if flat:
            res = _coarse_one(
                source_video, srt_hl, out_mp4, out_srt,
                start_ost_ms=start_ost_ms, end_ost_ms=end_ost_ms,
                segments=flat, line_windows=groups,
            )
        else:
            res = _coarse_one(
                source_video, srt_hl, out_mp4, out_srt,
                start_ost_ms=start_ost_ms, end_ost_ms=end_ost_ms,
            )
        results.append(res)
        if progress_cb:
            try:
                progress_cb(
                    f"stage4_clip_{res.status}",
                    {"index": idx, "duration_sec": res.duration_sec, "error": res.error},
                )
            except Exception as cb_err:  # noqa: BLE001
                log.warning("progress_cb raised: %s", cb_err)

    any_failed = any(r.status != "done" for r in results)

    state = svc.get_stage_state(project, "stage5_coarse")
    state["status"] = "done" if not any_failed else "failed"
    state["finished_at"] = svc.now_iso()
    # REQ-20261013-103 + REQ-20261013-108：增量字段 — 总时长按 highlights 累加
    # REQ-20261016-134：title 从 stage3 highlights 透传到 stage4（前端要显示
    # 在每张 clip 卡片顶部）。hl_by_idx 用 index 索引避免 N² 嵌套循环。
    hl_by_idx = {int(h.get("index") or 0): h for h in raw_highlights}
    new_highlights = [
        {
            "index": r.index,
            # REQ-20261016-134：标题透传（无 title 时 None → 前端用「片段 #N」占位）
            "title": (hl_by_idx.get(r.index) or {}).get("title"),
            "coarse_mp4": svc._rel(root, r.coarse_mp4) if r.coarse_mp4 else "",
            "coarse_srt": svc._rel(root, r.coarse_srt) if r.coarse_srt else "",
            "status": r.status,
            "duration_sec": round(r.duration_sec, 3),
            "elapsed_sec": r.elapsed_sec,
            "error": r.error,
        }
        for r in results
    ]
    # REQ-20261013-108：hl_index 单条模式 — 保留其他 highlight 的既有状态，
    # 只把新结果按 index 覆盖合并（避免单条粗剪把全表清空）。
    if hl_index:
        old_by_idx = {int(h.get("index") or 0): h
                      for h in (state.get("highlights") or [])}
        for h in new_highlights:
            old_by_idx[int(h.get("index") or 0)] = h
        state["highlights"] = sorted(
            old_by_idx.values(), key=lambda h: int(h.get("index") or 0),
        )
    else:
        state["highlights"] = new_highlights
    state["total_duration_sec"] = round(
        sum(float(h.get("duration_sec") or 0)
            for h in state["highlights"]
            if h.get("status") == "done"),
        3,
    )
    _log_lines = list(state.get("logs") or [])
    for r in results:
        _log_lines.append(
            f"[{svc.now_iso()}] clip #{r.index} {r.status} dur={r.duration_sec:.1f}s elapsed={r.elapsed_sec}s"
            + (f" err={r.error}" if r.error else "")
        )
    _log_lines.append(
        f"[{svc.now_iso()}] stage4 {'done' if not any_failed else 'failed'}, "
        f"{sum(1 for r in results if r.status == 'done')}/{len(results)} clips ok"
    )
    state["logs"] = _log_lines
    svc.set_stage_state(project, "stage5_coarse", state)
    svc.save_project(root, project)

    # 粗剪（重）合成 → 下游精剪作废：
    # REQ-20261019-221 单条模式（hl_index）只作废**该条**的精剪输出条目，
    # 其余条的精剪成品保留；全量合成仍整段作废（全部条的视频都变了）。
    if not any_failed:
        if hl_index:
            for r in results:
                if r.status == "done":
                    svc.invalidate_hl_downstream(
                        project, int(r.index), after_stage="stage5_coarse")
        else:
            svc.reset_downstream_stages(project, "stage5_coarse")
        svc.save_project(root, project)

    if progress_cb:
        try:
            progress_cb(
                "stage4_done" if not any_failed else "stage4_failed",
                {"count": len(results)},
            )
        except Exception as cb_err:  # noqa: BLE001
            log.warning("progress_cb raised: %s", cb_err)

    return state


# ---------- REQ-20261013-103：粗剪成片删除 / 连续 SRT 预览 ----------

def delete_coarse(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    *,
    auto: bool = False,
    auto_session_id: str = "",
) -> dict:
    """删除 Stage 5 粗剪成片（N 条 mp4 + N 条占位 srt）。仿 compose_rough_delete。

    同步、毫秒级；写执行历史 KIND_SHORT_VIDEO_COARSE_DELETE；
    作废下游 stage 6/7（reset_downstream_stages）。
    """
    from slirn_home import execution_history

    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    stage4 = svc.stage4_dir(root, task_id, project_id)
    stage5 = svc.stage5_dir(root, task_id, project_id)

    removed: list[str] = []
    # 删 stage4 目录下所有 coarse_NN.{mp4,srt}（不删 stage4 目录本身）
    if stage4.is_dir():
        for p in sorted(stage4.iterdir()):
            if p.is_file() and (p.suffix.lower() in (".mp4", ".srt")) and p.name.startswith("coarse_"):
                try:
                    p.unlink()
                    removed.append(svc._rel(root, p))
                except OSError as e:
                    log.warning("[stage5][delete] %s 失败: %s", p, e)
    # 删 stage5/preview_subs.srt 缓存
    preview = stage5 / "preview_subs.srt"
    if preview.is_file():
        try:
            preview.unlink()
            removed.append(svc._rel(root, preview))
        except OSError:
            pass
    # REQ-20261019-227：粗剪成片字幕三件套（subs_NN.srt / .asr.srt / .edits.json）
    # 随成品一并删除（成片没了字幕无从对齐；state.highlights 下面整表清空）
    if stage5.is_dir():
        for p in sorted(stage5.iterdir()):
            if (p.is_file() and p.name.startswith("subs_")
                    and p.suffix.lower() in (".srt", ".json")):
                try:
                    p.unlink()
                    removed.append(svc._rel(root, p))
                except OSError as e:
                    log.warning("[stage5][delete] %s 失败: %s", p, e)

    # 写执行历史（短视频自己的 stage5_dir）
    try:
        # REQ-20261022-144：日志落任务级 outputs（与长视频同文件，list_logs 可查）
        eid = execution_history.record_start(
            svc.sv_logs_dir(root, task_id),
            execution_history.KIND_SHORT_VIDEO_COARSE_DELETE,
            extra={"removed": removed},
            auto=auto,
            auto_session_id=auto_session_id,
        )
        if removed:
            execution_history.patch_fields(svc.sv_logs_dir(root, task_id), eid, {
                "description": f"删除粗剪成品：{' + '.join(removed)}",
            })
        else:
            execution_history.patch_fields(svc.sv_logs_dir(root, task_id), eid, {
                "description": "粗剪成片不存在，无需删除（无操作）",
            })
        execution_history.record_finish(stage5, eid, success=True, error="")
    except Exception as e:  # noqa: BLE001
        log.warning("[stage5][history] delete 记录失败: %s", e)

    # 重置 stage5_coarse 状态（status=pending）+ 作废下游
    state = svc.get_stage_state(project, "stage5_coarse")
    state["status"] = "pending"
    state["highlights"] = []
    state["count"] = 0
    state["total_duration_sec"] = 0.0
    state.pop("finished_at", None)
    state["logs"] = list(state.get("logs") or []) + [
        f"[{svc.now_iso()}] stage5 coarse deleted, {len(removed)} files removed",
    ]
    svc.set_stage_state(project, "stage5_coarse", state)
    svc.reset_downstream_stages(project, "stage5_coarse")
    svc.save_project(root, project)

    if removed:
        return {"deleted": True, "removed": removed,
                "remaining": [], "message": f"已删除 {' + '.join(removed)}"}
    return {"deleted": False, "removed": [], "remaining": [],
            "message": "粗剪成片不存在，无需删除"}


# 连续 SRT 缓存文件名（落 stage5/preview_subs.srt，避免每次都现拼）
_PREVIEW_SUBS_FILENAME = "preview_subs.srt"


def preview_continuous_subs(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
) -> dict:
    """跨 N 条 coarse_NN.srt 拼成连续时间轴 SRT（仿 compose_rough_preview_subs）。

    返回 ``{"srt": "....", "clips": N, "cached": bool}``。
    缓存到 ``stage5/preview_subs.srt``，第二次取直接读（除非 highlights 变化）。
    """
    from slirn_home.compose_service import format_srt

    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    stage4 = svc.stage4_dir(root, task_id, project_id)
    stage5 = svc.stage5_dir(root, task_id, project_id)
    state = svc.get_stage_state(project, "stage5_coarse")
    highlights = state.get("highlights") or []
    if not highlights:
        return {"srt": "", "clips": 0, "cached": False}

    # 取缓存（与 state.highlights 数量一致即直接返回）
    preview = stage5 / _PREVIEW_SUBS_FILENAME
    expected_count = len(highlights)
    if preview.is_file():
        try:
            cached = preview.read_text(encoding="utf-8", errors="replace")
            # 简单启发：缓存内容含 expected_count 个 "coarse_"
            if cached.count("-->") == expected_count:
                return {"srt": cached, "clips": expected_count, "cached": True}
        except Exception:  # noqa: BLE001
            pass

    # 现拼：把每条 coarse_NN.srt 当作一个 unit（start_ms = 累加偏移；end_ms = 累加+该条时长）
    from slirn_home.short_video_finalize import _ffprobe_duration
    units: list[dict] = []
    offset_ms = 0
    for hl in highlights:
        rel_mp4 = hl.get("coarse_mp4") or ""
        if not rel_mp4:
            continue
        mp4 = (root / rel_mp4).resolve()
        if not mp4.is_file():
            continue
        dur = _ffprobe_duration(mp4)
        units.append({
            "id": str(hl.get("index")),
            "start_ms": offset_ms,
            "end_ms": offset_ms + int(round(dur * 1000)),
            "text": f"clip_{hl.get('index')}",
        })
        offset_ms += int(round(dur * 1000))

    if not units:
        return {"srt": "", "clips": 0, "cached": False}

    srt_text = format_srt(units, text_overrides=None)
    # 落缓存
    try:
        stage5.mkdir(parents=True, exist_ok=True)
        preview.write_text(srt_text, encoding="utf-8")
    except OSError as e:
        log.warning("[stage5][preview] 缓存失败: %s", e)

    return {"srt": srt_text, "clips": len(units), "cached": False}



def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Stage 4: coarse cut")
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--start-ost", type=int, default=0)
    ap.add_argument("--end-ost", type=int, default=100)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = run_stage5(
        args.repo_root, args.task_id, args.project_id,
        start_ost_ms=args.start_ost, end_ost_ms=args.end_ost,
    )
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0 if state.get("status") == "done" else 1


# ---------- REQ-20261019-214：Stage 4 预览播放列表（原视频逐段 seek 播放） ----------
# 旧实现（REQ-20261013-108，moviepy 拼一段 preview mp4 + 前端内嵌 video）已按
# 用户要求整体移除；预览改为不落盘 —— 返回源视频 URL + 按行分组的播放窗，
# 前端浮窗在**原视频**上逐段跳转播放（一行播完跳下一行起点），当前行高亮，
# 点击任意行从该行起播（见 short_video.js stage4Preview）。

def preview_playlist(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    hl_index: int,
) -> dict:
    """Stage 4 预览播放列表（REQ-20261019-214）。

    行序/窗口与粗剪消费完全一致：``effective_subtitle_lines`` 合并读
    （标删行剔除、剪词行按 keep_segments 展开多窗、line_order 重排后顺序）。

    Returns:
        {status, video_url, lines, duration_sec, segments_count, error}；
        lines: [{row_idx, src, no, text, segs: [[start_ms, end_ms], ...]}]，
        row_idx = 该行在 include_deleted 渲染表（Stage 4 行列表）里的下标
        —— 与行的 ``data-eff-idx`` 一一对应，前端据此高亮/定位。
    """
    from urllib.parse import quote

    from slirn_home import short_video_optimize as _sv_opt

    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    src_id = project.get("base_material_id") or ""
    src = next(
        (m for m in project.get("materials") or []
         if m.get("id") == src_id and m.get("kind") == "video"),
        None,
    )
    if not src:
        return {"status": "failed", "error": "Stage 4 需要先在 Stage 1 选源视频"}
    source_video = svc.material_abs_path(root, project, src["id"])
    if not source_video.is_file():
        return {"status": "failed", "error": f"源视频不存在: {source_video}"}

    # 渲染口径（include_deleted=True）取行 —— row_idx 与 Stage 4 行列表
    # 的 data-eff-idx 对齐；marked_deleted 行不进播放列表
    eff = _sv_opt.effective_subtitle_lines(
        root, task_id, project_id, hl_index, include_deleted=True)
    if not eff:
        return {"status": "failed", "error": f"hl#{hl_index} 没有有效字幕行"}

    lines: list[dict] = []
    total_ms = 0
    seg_count = 0
    for row_idx, s in enumerate(eff):
        if s.get("marked_deleted"):
            continue
        keep = s.get("keep_segments")
        windows = (
            [(int(x.get("start_ms") or 0), int(x.get("end_ms") or 0)) for x in keep]
            if keep else
            [(int(s.get("start_ms") or 0), int(s.get("end_ms") or 0))]
        )
        windows = [(sm, em) for sm, em in windows if em > sm]
        if not windows:
            continue
        lines.append({
            "row_idx": row_idx,
            "src": int(s.get("src_index") or 0),
            "no": int(s.get("no") or 0),
            "text": str(s.get("text") or ""),
            "segs": windows,
        })
        total_ms += sum(em - sm for sm, em in windows)
        seg_count += len(windows)
    if not lines:
        return {"status": "failed", "error": "所有字幕行时间戳无效"}

    video_url = (
        "/slirn/api/short_video_file?"
        f"task_id={quote(str(task_id))}&project_id={quote(str(project_id))}"
        f"&kind=material&name={quote(str(src.get('id') or ''))}"
    )
    return {
        "status": "done",
        "video_url": video_url,
        "lines": lines,
        "duration_sec": round(total_ms / 1000.0, 2),
        "segments_count": seg_count,
    }


if __name__ == "__main__":
    raise SystemExit(_cli())

# 兼容别名：旧调用方（外部脚本 / 未同步的调用点）继续可用
run_stage4 = run_stage5
