"""短视频工作台 Stage 6（精剪混编）执行模块。

REQ-20261003-098：第 6 阶段把每个 highlight 的粗剪 mp4 + 优化字幕 srt 喂给
上游 ``skill/video-timestamp-cutter/scripts/cut_by_srt.py``，由它产出
final mp4 与连续时间轴 srt。

REQ-20261021-139 / D2：2026-10-21 合并两仓为单工程后，skill 目录从
``slirn/skill/``（子模块）平铺为 ``skill/`` —— 定位 cut_by_srt.py 的相对路径
同步改为「新布局优先 + 旧布局兜底」的候选列表（与
``slirn_home/short_video_extract.py`` 既有模式一致）。

隔离策略：

- 子进程工作目录 = ``tasks/<tid>/short_video/<pid>/stage6/_work``，是 Stage 6
  自己的临时区，不进入长视频的 ``tasks/<tid>/outputs/...``。
- 产物（final mp4 + continuous srt）落 ``tasks/<tid>/short_video/<pid>/stage6/``。
- 复用 ``cut_by_srt.py`` 的解析 + 剪切逻辑（脚本本身不改），隔离通过 ``cwd`` +
  入参 + 出参绝对路径保证。
- 失败时 stderr 全部回传，便于上层诊断。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slirn_home import short_video_service as svc

log = logging.getLogger(__name__)


# ---------- cut_by_srt.py 子进程调用 ----------

# REQ-20261021-139 / D2：候选相对路径（新布局优先，旧布局兜底）。
# 合并前 skill 在 slirn/ 子模块下 → slirn/skill/<name>/scripts/；
# 合并后平铺为 skill/<name>/scripts/。两种布局都可能出现（归档仓 / 本仓），
# 故按顺序逐个探测。顺序与 short_video_extract.py 的
# EXTRACT_SUBTITLE_RELPATH_CANDIDATES 严格一致（旧布局优先）——
# 同一检出里若两种布局并存，Stage1 / Stage6 仍命中同一份 skill 树
# （REQ-20261021-139 评审 R3）。
CUT_BY_SRT_RELPATH_CANDIDATES = (
    Path("slirn") / "skill" / "video-timestamp-cutter" / "scripts" / "cut_by_srt.py",
    Path("skill") / "video-timestamp-cutter" / "scripts" / "cut_by_srt.py",
)


def _resolve_cut_by_srt(repo_root: Path) -> Path:
    """找到上游 cut_by_srt.py。按候选相对路径依次探测仓库根目录。

    返回绝对路径：子进程 cwd 会切到 stage6/_work，必须用绝对路径定位 cut_by_srt.py。
    """
    tried = []
    for rel in CUT_BY_SRT_RELPATH_CANDIDATES:
        cand = (Path(repo_root) / rel).resolve()
        if cand.is_file():
            return cand
        tried.append(str(cand))
    raise FileNotFoundError(
        "找不到上游 cut_by_srt.py：已尝试 " + " / ".join(tried)
        + "（请确认 skill/ 目录已随仓库拉取）"
    )


def _ffprobe_duration(path: Path) -> float:
    """ffprobe 读时长（秒）；失败返回 0.0。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8")
    except Exception:  # noqa: BLE001
        return 0.0
    if proc.returncode != 0:
        return 0.0
    try:
        return max(0.0, float((proc.stdout or "").strip()))
    except (TypeError, ValueError):
        return 0.0


def _validate_stage6_isolation(
    task_dir: Path,
    new_files: list[Path],
) -> list[str]:
    """验证 Stage 6 产物的落点都在 stage6 目录内，没有污染长视频项目区。"""
    long_roots = svc.find_long_video_state_root(task_dir)
    bad: list[str] = []
    for f in new_files:
        try:
            resolved = f.resolve()
        except OSError:
            continue
        in_long = False
        for root in long_roots:
            try:
                resolved.relative_to(root.resolve())
                in_long = True
                break
            except ValueError:
                continue
        if in_long:
            bad.append(str(resolved))
    return bad


# ---------- 单条 highlight Stage 6 执行 ----------

@dataclass
class Stage6HighlightResult:
    index: int
    coarse_mp4: Path
    refined_srt: Path
    final_mp4: Path
    final_srt: Path
    status: str                # done / failed
    duration_sec: float
    stderr_tail: str = ""
    error: str = ""
    elapsed_sec: int = 0


def run_stage6_one(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    index: int,
    *,
    coarse_mp4: Path | str,
    refined_srt: Path | str,
) -> Stage6HighlightResult:
    """跑单条 highlight 的 Stage 6。

    Args:
        repo_root: 项目根（含 slirn/ 子模块）
        task_id / project_id: 项目坐标
        index: highlight 序号（从 1 开始）
        coarse_mp4: Stage 4 粗剪产物 mp4
        refined_srt: Stage 5 优化字幕 srt

    Returns:
        Stage6HighlightResult；status='done' / 'failed'
    """
    root = Path(repo_root)
    coarse = Path(coarse_mp4).resolve()
    refined = Path(refined_srt).resolve()
    if not coarse.is_file():
        return Stage6HighlightResult(
            index=index, coarse_mp4=coarse, refined_srt=refined,
            final_mp4=Path(), final_srt=Path(),
            status="failed", duration_sec=0.0,
            error=f"粗剪 mp4 不存在: {coarse}",
        )
    if not refined.is_file():
        return Stage6HighlightResult(
            index=index, coarse_mp4=coarse, refined_srt=refined,
            final_mp4=Path(), final_srt=Path(),
            status="failed", duration_sec=0.0,
            error=f"优化字幕 srt 不存在: {refined}",
        )

    final_mp4, final_srt = svc.stage6_paths(root, task_id, project_id, index)
    # 转绝对路径：子进程 cwd 切到 work_dir 后，--output 必须用绝对路径才能落到 stage6/
    final_mp4 = final_mp4.resolve()
    final_srt = final_srt.resolve()
    final_mp4.parent.mkdir(parents=True, exist_ok=True)
    work_dir = (final_mp4.parent / "_work").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    cut_script = _resolve_cut_by_srt(root)
    cmd = [
        sys.executable,
        str(cut_script),
        "--input", str(coarse),
        "--srt", str(refined),
        "--output", str(final_mp4),
        "--crf", "23",
    ]
    log.info("[stage6 #%d] cwd=%s cmd=%s", index, work_dir, " ".join(cmd))

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=600,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return Stage6HighlightResult(
            index=index, coarse_mp4=coarse, refined_srt=refined,
            final_mp4=final_mp4, final_srt=final_srt,
            status="failed", duration_sec=0.0,
            error="cut_by_srt.py 超时（>600s）",
        )
    except Exception as e:  # noqa: BLE001
        return Stage6HighlightResult(
            index=index, coarse_mp4=coarse, refined_srt=refined,
            final_mp4=final_mp4, final_srt=final_srt,
            status="failed", duration_sec=0.0,
            error=f"启动 cut_by_srt.py 失败: {e}",
        )

    elapsed = int(time.time() - t0)
    stderr_tail = (proc.stderr or "")[-1200:]
    if proc.returncode != 0 or not final_mp4.is_file() or final_mp4.stat().st_size < 1024:
        return Stage6HighlightResult(
            index=index, coarse_mp4=coarse, refined_srt=refined,
            final_mp4=final_mp4, final_srt=final_srt,
            status="failed", duration_sec=0.0,
            elapsed_sec=elapsed,
            stderr_tail=stderr_tail,
            error=f"cut_by_srt.py 退出码 {proc.returncode}",
        )

    # 生成 continuous srt：cut_by_srt.py 不输出连续时间轴字幕，需本地拼一个
    duration = _ffprobe_duration(final_mp4)
    _write_continuous_srt(refined, final_srt, duration_sec=duration)

    return Stage6HighlightResult(
        index=index, coarse_mp4=coarse, refined_srt=refined,
        final_mp4=final_mp4, final_srt=final_srt,
        status="done", duration_sec=duration,
        elapsed_sec=elapsed, stderr_tail=stderr_tail,
    )


_SRT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _srt_time_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _write_continuous_srt(refined_srt: Path, out_srt: Path, *, duration_sec: float) -> None:
    """把 refined srt 写成「连续时间轴」的 SRT。

    Stage 6 输出的 final mp4 是单条 highlight（直接拿 SRT 切，不拼接多段），
    所以「连续」等于「按原始相对顺序、从 0 开始重新编号」。若最终时长小于
    refined srt 最末时间戳，截断处理；反之补齐到最终时长。

    容错：refined srt 可能没有空行分隔（funasr 直出常这样），按行解析——
    任意行的内容是纯数字（块序号）即视为新块的开始；含 "-->" 的行为时间戳行。
    """
    text = refined_srt.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    blocks: list[tuple[float, float, str]] = []
    cur_index = 0
    cur_time: tuple[float, float] | None = None
    cur_caption: list[str] = []

    def _flush():
        nonlocal cur_time, cur_caption
        if cur_time is not None:
            cap = "\n".join(cur_caption).strip()
            if cap:
                blocks.append((cur_time[0], cur_time[1], cap))
        cur_time = None
        cur_caption = []

    for line in lines:
        s = line.strip()
        if not s:
            _flush()
            continue
        m = _SRT_TIME_RE.search(s)
        if m:
            h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
            start = _srt_time_to_sec(h1, m1, s1, ms1)
            end = _srt_time_to_sec(h2, m2, s2, ms2)
            # 若已有时间戳未 flush，先收尾再开新块（避免无序号直接连时间戳）
            if cur_time is not None:
                _flush()
            cur_time = (start, end)
            continue
        # 块序号行：纯数字（1-4 位）
        if s.isdigit() and len(s) <= 4:
            _flush()
            cur_index = int(s)
            continue
        # 字幕行：追加到当前块
        cur_caption.append(s)
    _flush()

    if not blocks:
        out_srt.write_text("", encoding="utf-8")
        return

    # 重新映射到 [0, duration_sec]：线性缩放
    src_total = blocks[-1][1]
    if src_total <= 0 or duration_sec <= 0:
        scale = 1.0
    else:
        scale = duration_sec / src_total
    out_lines: list[str] = []
    for i, (start, end, cap) in enumerate(blocks, start=1):
        new_start = round(start * scale, 3)
        new_end = round(end * scale, 3)
        # 截断超出 final mp4 时长的字幕行；start>=end 则丢弃
        if duration_sec > 0 and new_end > duration_sec:
            new_end = round(duration_sec, 3)
        if new_end <= new_start:
            continue
        out_lines.append(str(i))
        out_lines.append(_fmt_srt_time(new_start) + " --> " + _fmt_srt_time(new_end))
        out_lines.append(cap)
        out_lines.append("")
    out_srt.write_text("\n".join(out_lines), encoding="utf-8")


def _fmt_srt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------- 多条 highlight 批量执行 ----------

def run_stage6_batch(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    items: list[dict],
    *,
    progress_cb=None,
) -> dict:
    """批量执行 Stage 6；每条独立 try，整体结果汇总写回 project.pipeline.stage6_fine。

    每条 item 形如：
        {"index": 1, "coarse_mp4": "stage4/coarse_01.mp4", "refined_srt": "stage5/refined_01.srt"}

    返回 dict（含 status/highlights/logs）。
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)

    stage_state = svc.get_stage6_state(project)
    stage_state["status"] = "running"
    stage_state["started_at"] = svc.now_iso()
    stage_state["highlights"] = []   # 重新覆盖本次的（每次都是全量重跑）
    stage_state["logs"].append(f"[{svc.now_iso()}] stage6 start, {len(items)} clips")
    svc.set_stage6_state(project, stage_state)
    svc.save_project(root, project)

    if progress_cb:
        try:
            progress_cb("stage6_start", {"count": len(items)})
        except Exception as cb_err:  # noqa: BLE001
            log.warning("progress_cb raised: %s", cb_err)

    all_results: list[Stage6HighlightResult] = []
    for item in items:
        idx = int(item.get("index") or 0)
        coarse = svc._abs(root, item.get("coarse_mp4") or "")
        refined = svc._abs(root, item.get("refined_srt") or "")
        res = run_stage6_one(root, task_id, project_id, idx, coarse_mp4=coarse, refined_srt=refined)
        all_results.append(res)
        if progress_cb:
            try:
                progress_cb(
                    f"stage6_clip_{res.status}",
                    {
                        "index": idx,
                        "duration_sec": res.duration_sec,
                        "error": res.error,
                    },
                )
            except Exception as cb_err:  # noqa: BLE001
                log.warning("progress_cb raised: %s", cb_err)

    # 隔离校验：final mp4 / srt 都不应落到长视频项目区
    task_dir = root / "tasks" / task_id
    new_files: list[Path] = []
    for res in all_results:
        if res.status == "done":
            new_files.append(res.final_mp4)
            new_files.append(res.final_srt)
    bad_paths = _validate_stage6_isolation(task_dir, new_files)

    # 汇总
    highlights_dict: list[dict] = []
    any_failed = bool(bad_paths)
    for res in all_results:
        rec = {
            "index": res.index,
            "coarse_mp4": svc._rel(root, res.coarse_mp4) if res.coarse_mp4 else "",
            "refined_srt": svc._rel(root, res.refined_srt) if res.refined_srt else "",
            "final_mp4": svc._rel(root, res.final_mp4) if res.final_mp4 else "",
            "final_srt": svc._rel(root, res.final_srt) if res.final_srt else "",
            "status": res.status,
            "duration_sec": round(res.duration_sec, 3),
            "elapsed_sec": res.elapsed_sec,
            "error": res.error,
        }
        highlights_dict.append(rec)
        if res.status != "done":
            any_failed = True

    stage_state["status"] = "done" if not any_failed and not bad_paths else "failed"
    stage_state["finished_at"] = svc.now_iso()
    stage_state["highlights"] = highlights_dict
    if bad_paths:
        stage_state["logs"].append(
            f"[{svc.now_iso()}] isolation check failed: {bad_paths}"
        )
        stage_state["isolation_violations"] = [str(p) for p in bad_paths]
    else:
        stage_state["logs"].append(
            f"[{svc.now_iso()}] isolation check passed: {len(new_files)} files all in stage6/"
        )

    project = svc.load_project(root, task_id, project_id)
    svc.set_stage6_state(project, stage_state)
    svc.save_project(root, project)

    if progress_cb:
        try:
            progress_cb(
                "stage6_done" if not any_failed else "stage6_failed",
                {
                    "highlights": len(highlights_dict),
                    "isolation_violations": len(bad_paths),
                },
            )
        except Exception as cb_err:  # noqa: BLE001
            log.warning("progress_cb raised: %s", cb_err)

    return stage_state


# ---------- CLI 入口（便于测试与 CI） ----------

def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Stage 6 精剪混编（per highlight）")
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--index", type=int, required=True, help="highlight 序号（1-based）")
    ap.add_argument("--coarse-mp4", required=True)
    ap.add_argument("--refined-srt", required=True)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    res = run_stage6_one(
        args.repo_root, args.task_id, args.project_id, args.index,
        coarse_mp4=args.coarse_mp4, refined_srt=args.refined_srt,
    )
    print(json.dumps({
        "index": res.index,
        "status": res.status,
        "duration_sec": res.duration_sec,
        "final_mp4": str(res.final_mp4),
        "final_srt": str(res.final_srt),
        "elapsed_sec": res.elapsed_sec,
        "error": res.error,
    }, ensure_ascii=False, indent=2))
    return 0 if res.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(_cli())