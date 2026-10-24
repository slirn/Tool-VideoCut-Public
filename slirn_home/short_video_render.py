"""短视频确定性 FFmpeg 渲染器。

输入是 ``short_video_service`` 规范化后的项目与分镜。渲染器负责：

- 多个主视频片段拼接和 xfade 转场
- 图片/视频 B-roll overlay
- 标题、Hook、CTA、字幕 ASS 叠加
- BGM 与原声混合
- 1080x1920 竖屏输出

渲染任务使用进程内 job 表，成功输出先写临时文件再原子替换，避免页面拿到半成品。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from slirn_home import short_video_service as svc

log = logging.getLogger(__name__)

_JOBS: dict[str, dict] = {}
_JOB_LOCK = threading.Lock()
_ACTIVE_PROJECTS: set[tuple[str, str]] = set()
_JOB_TTL_SECONDS = 3600


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


def build_ass(project: dict, variant: dict, total_duration: float) -> str:
    """生成标题、Hook、CTA 和字幕的 ASS 文本。"""
    width = int((project.get("config") or {}).get("width") or 1080)
    height = int((project.get("config") or {}).get("height") or 1920)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
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
        (
            "Style: Hook,Microsoft YaHei,76,&H0000E8FF,&H00FFFFFF,&H00101010,"
            "&H80000000,-1,0,0,0,100,100,0,0,1,5,2,8,60,60,72,1"
        ),
        (
            "Style: Title,Microsoft YaHei,54,&H00FFFFFF,&H00FFFFFF,&H00101010,"
            "&H80000000,-1,0,0,0,100,100,0,0,1,4,1,8,80,80,170,1"
        ),
        (
            "Style: Subtitle,Microsoft YaHei,52,&H00FFFFFF,&H00FFFFFF,&H00101010,"
            "&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,105,1"
        ),
        (
            "Style: CTA,Microsoft YaHei,46,&H0000E8FF,&H00FFFFFF,&H00101010,"
            "&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,80,80,280,1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    hook = _ass_escape(variant.get("hook") or "")
    title = _ass_escape(variant.get("title") or "")
    cta = _ass_escape(variant.get("cta") or "")
    if hook:
        lines.append(
            f"Dialogue: 1,{_ass_time(0)},{_ass_time(min(4.0, total_duration))},Hook,,0,0,0,,{hook}"
        )
    if title:
        lines.append(
            f"Dialogue: 1,{_ass_time(0)},{_ass_time(total_duration)},Title,,0,0,0,,{title}"
        )
    for sub in variant.get("subtitles") or []:
        text = _ass_escape(sub.get("text") or "")
        if not text:
            continue
        start = max(0.0, float(sub.get("start") or 0.0))
        end = min(total_duration, float(sub.get("end") or start + 2.0))
        if end <= start:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Subtitle,,0,0,0,,{text}"
        )
    if cta:
        lines.append(
            f"Dialogue: 1,{_ass_time(max(0.0, total_duration - 3.0))},"
            f"{_ass_time(total_duration)},CTA,,0,0,0,,{cta}"
        )
    return "\n".join(lines) + "\n"


def _transition_name(value: str) -> str:
    value = str(value or "fade")
    if value in svc.ALLOWED_TRANSITIONS and value != "none":
        return value
    return "fade"


def _material_path(repo_root: Path, project: dict, material_id: str) -> Path:
    return svc.material_abs_path(repo_root, project, material_id)


def build_render_command(
    repo_root: Path | str,
    project: dict,
    variant: dict,
    output_path: Path,
) -> dict:
    """构造 FFmpeg 命令，供执行和单元测试共用。"""
    root = Path(repo_root)
    cfg = svc.sanitize_config(project.get("config"))
    width, height, fps = cfg["width"], cfg["height"], cfg["fps"]
    trans_default = cfg["transition_duration"]
    segments = list(variant.get("segments") or [])
    if not segments:
        raise svc.ShortVideoError("分镜没有视频片段")

    input_args: list[str] = []
    chain: list[str] = []
    seg_v_labels: list[str] = []
    seg_durations: list[float] = []
    seg_input_indices: list[int] = []
    input_index = 0
    materials = {m.get("id"): m for m in project.get("materials") or []}

    # REQ-20261001-095：变体指定了背景图（bg_image 素材）→ 用它做整幅画布背景，
    # 替代默认的「模糊放大视频」填充；图片按 cover 方式铺满画布。
    # 只开一个图片输入（-loop 1），filter 内 split 分发给各段 —— 静态图解码一份，
    # 每段一个输入会成倍增加解码缓冲（实测多段直接 OOM）。
    bg_path: Path | None = None
    bg_mat = materials.get(str(variant.get("bg_material_id") or ""))
    if bg_mat and bg_mat.get("kind") == "bg_image":
        candidate = _material_path(root, project, bg_mat["id"])
        if candidate.exists():
            bg_path = candidate
    if bg_path is not None:
        total_seg = sum(
            max(0.5, float(seg.get("duration") or 5.0)) for seg in segments
        )
        input_args += ["-loop", "1", "-t", f"{total_seg:.3f}", "-i", str(bg_path)]
        bg_input_idx = input_index
        input_index += 1
        chain.append(
            f"[{bg_input_idx}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,"
            f"split={len(segments)}" + "".join(f"[bgx{i}]" for i in range(len(segments)))
        )

    # Main segments.
    for i, seg in enumerate(segments):
        mat_path = _material_path(root, project, str(seg.get("material_id") or ""))
        if not mat_path.exists():
            raise svc.ShortVideoError(f"视频素材不存在: {mat_path}")
        start = max(0.0, float(seg.get("start") or 0.0))
        duration = max(0.5, float(seg.get("duration") or 5.0))
        input_args += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(mat_path)]
        seg_input_indices.append(input_index)
        if bg_path is not None:
            chain.append(
                f"[{input_index}:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease[fgx{i}];"
                f"[bgx{i}][fgx{i}]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1[v{i}]"
            )
            input_index += 1
        else:
            chain.append(
                f"[{input_index}:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
                f"split=2[fg{i}][bg{i}];"
                f"[bg{i}]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},gblur=sigma=24,eq=brightness=-0.16[bgx{i}];"
                f"[fg{i}]scale={width}:{height}:force_original_aspect_ratio=decrease[fgx{i}];"
                f"[bgx{i}][fgx{i}]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1[v{i}]"
            )
            input_index += 1
        seg_v_labels.append(f"[v{i}]")
        seg_durations.append(duration)

    # Missing audio segments use synthetic silence so one bad source does not break the mix.
    audio_input_indices: list[int] = []
    for i, seg in enumerate(segments):
        mat = materials.get(seg.get("material_id")) or {}
        if mat.get("has_audio", True):
            # 视频输入的真实索引（背景图分支会在视频输入后插入图片输入，不能按段号取流）
            audio_input_indices.append(seg_input_indices[i])
        else:
            duration = seg_durations[i]
            input_args += [
                "-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", "anullsrc=r=48000:cl=stereo",
            ]
            audio_input_indices.append(input_index)
            input_index += 1

    # Chain main video with xfade.
    cur_v = seg_v_labels[0]
    cur_v_duration = seg_durations[0]
    for i in range(1, len(seg_v_labels)):
        trans = _transition_name(segments[i - 1].get("transition") or cfg["transition"])
        d = min(trans_default, max(0.1, seg_durations[i - 1] * 0.4), max(0.1, seg_durations[i] * 0.4))
        offset = max(0.1, cur_v_duration - d)
        out = f"[vx{i}]"
        chain.append(
            f"{cur_v}{seg_v_labels[i]}xfade=transition={trans}:duration={d:.3f}:offset={offset:.3f}{out}"
        )
        cur_v = out
        cur_v_duration += seg_durations[i] - d
    main_video = cur_v
    total_duration = max(0.5, cur_v_duration)

    # Chain main audio with acrossfade. "none" becomes a short fade to keep A/V aligned.
    cur_a = None
    cur_a_duration = 0.0
    for i, aidx in enumerate(audio_input_indices):
        d = seg_durations[i]
        label = f"[a{i}]"
        chain.append(f"[{aidx}:a]atrim=duration={d:.3f},asetpts=PTS-STARTPTS{label}")
        if cur_a is None:
            cur_a = label
            cur_a_duration = d
            continue
        trans = _transition_name(segments[i - 1].get("transition") or cfg["transition"])
        fade_d = min(trans_default, max(0.1, cur_a_duration * 0.4), max(0.1, d * 0.4))
        out = f"[ax{i}]"
        chain.append(f"{cur_a}{label}acrossfade=d={fade_d:.3f}:c1=tri:c2=tri{out}")
        cur_a = out
        cur_a_duration += d - fade_d
    main_audio = cur_a or "[main_silence]"
    if cur_a is None:
        chain.append(
            f"anullsrc=r=48000:cl=stereo:d={total_duration:.3f},"
            f"asetpts=PTS-STARTPTS{main_audio}"
        )

    # B-roll overlays.
    cur_video = main_video
    overlay_count = 0
    for ov in variant.get("overlays") or []:
        mat_path = _material_path(root, project, str(ov.get("material_id") or ""))
        if not mat_path.exists():
            continue
        mat = materials.get(ov.get("material_id")) or {}
        start = max(0.0, float(ov.get("start") or 0.0))
        duration = max(0.5, float(ov.get("duration") or 3.0))
        if mat.get("kind") == "image":
            input_args += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(mat_path)]
        else:
            input_args += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(mat_path)]
        ov_idx = input_index
        input_index += 1
        scale = max(0.05, float(ov.get("scale") or 0.38))
        label = f"[ov{overlay_count}]"
        chain.append(
            f"[{ov_idx}:v]scale=iw*{scale:.4f}:ih*{scale:.4f},setsar=1{label}"
        )
        out = f"[vov{overlay_count}]"
        chain.append(
            f"{cur_video}{label}overlay=x={int(ov.get('x') or 0)}:y={int(ov.get('y') or 0)}:"
            f"enable='between(t,{start:.3f},{start + duration:.3f})'{out}"
        )
        cur_video = out
        overlay_count += 1

    # ASS text layer.
    ass_text = build_ass(project, variant, total_duration)
    fh = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ass", encoding="utf-8", delete=False, prefix="slirn_sv_"
    )
    fh.write(ass_text)
    fh.close()
    ass_path = Path(fh.name)
    ass_safe = str(ass_path).replace("\\", "/").replace(":", r"\:")
    chain.append(f"{cur_video}ass='{ass_safe}'[vfinal]")

    # BGM.
    bgm_id = str(variant.get("bgm_material_id") or "")
    if bgm_id:
        bgm_path = _material_path(root, project, bgm_id)
        if bgm_path.exists():
            input_args += [
                "-stream_loop", "-1", "-t", f"{total_duration:.3f}", "-i", str(bgm_path)
            ]
            bgm_idx = input_index
            input_index += 1
            vol = float(variant.get("bgm_volume_db") or cfg["bgm_volume_db"])
            fade_out_start = max(0.0, total_duration - 1.0)
            chain.append(
                f"[{bgm_idx}:a]volume={vol:.1f}dB,"
                f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out_start:.3f}:d=1[bgm]"
            )
            chain.append(f"{main_audio}[bgm]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            chain.append(f"{main_audio}anull[aout]")
    else:
        chain.append(f"{main_audio}anull[aout]")

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", ";\n".join(chain),
        "-map", "[vfinal]",
        "-map", "[aout]",
        "-threads", "1",
        "-filter_threads", "1",
        "-filter_complex_threads", "1",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "160k",
        "-t", f"{total_duration:.3f}",
        "-movflags", "+faststart",
        str(output_path),
    ]
    return {
        "cmd": cmd,
        "ass_path": ass_path,
        "total_duration": total_duration,
        "width": width,
        "height": height,
    }


def _cleanup_jobs() -> None:
    cutoff = time.time() - _JOB_TTL_SECONDS
    with _JOB_LOCK:
        for job_id, job in list(_JOBS.items()):
            if job.get("finished_at", 0) and job["finished_at"] < cutoff:
                _JOBS.pop(job_id, None)


def _set_job(job_id: str, **fields: Any) -> None:
    with _JOB_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(fields)


def job_status(job_id: str) -> dict | None:
    _cleanup_jobs()
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        # Popen 仅用于取消信号，不能进入 JSON 响应。
        return {
            key: value
            for key, value in job.items()
            if key not in {"proc", "cancel_requested"}
        }


def cancel_render(job_id: str) -> bool:
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        proc = job.get("proc") if job else None
    if not job or job.get("state") not in ("queued", "running"):
        return False
    _set_job(job_id, cancel_requested=True)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            return False
    return True


def _render_one(
    repo_root: Path,
    project: dict,
    variant: dict,
    output_path: Path,
    job_id: str,
) -> dict:
    built = build_render_command(repo_root, project, variant, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(".tmp.mp4")
    cmd = list(built["cmd"])
    cmd[-1] = str(tmp)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8"
    )
    _set_job(job_id, proc=proc, state="running", stage=f"渲染 {variant.get('name')}")
    try:
        stdout, stderr = proc.communicate(timeout=6 * 3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise svc.ShortVideoError("短视频渲染超时（>6 小时）")
    finally:
        try:
            Path(built["ass_path"]).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    if _JOBS.get(job_id, {}).get("cancel_requested"):
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise svc.ShortVideoError("用户取消渲染")
    if proc.returncode != 0:
        tail = (stderr or stdout or "")[-1200:]
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise svc.ShortVideoError(f"ffmpeg 渲染失败：{tail}")
    os.replace(tmp, output_path)
    return {
        "variant_id": variant.get("id"),
        "name": variant.get("name"),
        "path": str(output_path).replace("\\", "/"),
        "duration": built["total_duration"],
        "width": built["width"],
        "height": built["height"],
        "rendered_at": svc.now_iso(),
    }


def _run_job(repo_root: Path, task_id: str, project_id: str, job_id: str) -> None:
    outputs_dir = repo_root / "tasks" / task_id / "outputs"
    exec_id = ""
    try:
        from slirn_home import execution_history

        exec_id = execution_history.record_start(
            outputs_dir,
            execution_history.KIND_SHORT_VIDEO_RENDER,
            extra={"job_id": job_id, "project_id": project_id},
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        project = svc.load_project(repo_root, task_id, project_id)
        variant_ids = _JOBS[job_id].get("variant_ids") or []
        variants = [
            v for v in project.get("storyboard", {}).get("variants", [])
            if not variant_ids or v.get("id") in variant_ids
        ]
        if not variants:
            raise svc.ShortVideoError("没有可渲染的版本")
        out_dir = svc.output_dir(repo_root, task_id, project_id)
        results = []
        _set_job(job_id, state="running", progress=1.0, total=len(variants), done=0)
        for i, variant in enumerate(variants):
            _set_job(
                job_id,
                current_variant=variant.get("id"),
                stage=f"渲染 {variant.get('name')}",
                progress=round(i / len(variants) * 100, 1),
            )
            out = out_dir / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', str(variant.get('id'))) or f'v{i+1}'}.mp4"
            result = _render_one(repo_root, project, variant, out, job_id)
            results.append(result)
            _set_job(job_id, done=i + 1, progress=round((i + 1) / len(variants) * 100, 1))
        project = svc.load_project(repo_root, task_id, project_id)
        project["render_jobs"] = [
            r for r in project.get("render_jobs", []) if r.get("job_id") != job_id
        ]
        project["render_jobs"].append({
            "job_id": job_id,
            "state": "done",
            "outputs": results,
            "finished_at": svc.now_iso(),
        })
        svc.save_project(repo_root, project)
        _set_job(
            job_id,
            state="done",
            progress=100.0,
            outputs=results,
            finished_at=time.time(),
            wall_finished_at=svc.now_iso(),
        )
        if exec_id:
            try:
                from slirn_home import execution_history

                execution_history.patch_extra(
                    outputs_dir, exec_id,
                    {"job_id": job_id, "project_id": project_id,
                     "variants": len(results)},
                )
                execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        log.exception("[short_video] 渲染任务失败: %s", job_id)
        try:
            project = svc.load_project(repo_root, task_id, project_id)
            project["render_jobs"] = [
                r for r in project.get("render_jobs", []) if r.get("job_id") != job_id
            ]
            project["render_jobs"].append({
                "job_id": job_id,
                "state": "cancelled" if _JOBS.get(job_id, {}).get("cancel_requested") else "failed",
                "error": str(e),
                "finished_at": svc.now_iso(),
            })
            svc.save_project(repo_root, project)
        except Exception:  # noqa: BLE001
            pass
        state = "cancelled" if _JOBS.get(job_id, {}).get("cancel_requested") else "failed"
        _set_job(job_id, state=state, error=str(e), finished_at=time.time(),
                 wall_finished_at=svc.now_iso())
        if exec_id:
            try:
                from slirn_home import execution_history

                execution_history.record_finish(
                    outputs_dir, exec_id, success=False, error=str(e)
                )
            except Exception:  # noqa: BLE001
                pass
    finally:
        with _JOB_LOCK:
            _ACTIVE_PROJECTS.discard((task_id, project_id))


def start_render(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    variant_ids: list[str] | None = None,
) -> dict:
    root = Path(repo_root)
    key = (task_id, project_id)
    with _JOB_LOCK:
        if key in _ACTIVE_PROJECTS:
            raise svc.ShortVideoError("该项目已有渲染任务在运行")
        _ACTIVE_PROJECTS.add(key)
        job_id = svc.new_id("svjob")
        _JOBS[job_id] = {
            "job_id": job_id,
            "task_id": task_id,
            "project_id": project_id,
            "state": "queued",
            "progress": 0.0,
            "stage": "排队",
            "variant_ids": variant_ids or [],
            "created_at": time.time(),
            "finished_at": 0.0,
            "cancel_requested": False,
        }
    thread = threading.Thread(
        target=_run_job,
        args=(root, task_id, project_id, job_id),
        name=f"short-video-{job_id}",
        daemon=True,
    )
    thread.start()
    return job_status(job_id) or {"job_id": job_id, "state": "queued"}
