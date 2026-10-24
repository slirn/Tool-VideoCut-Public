# -*- coding: utf-8 -*-
"""短视频「流程配置」自动执行编排器（REQ-20261022-143）。

参照长视频工作台的流程配置范式（``slirn_home/pipeline_service.py`` +
``slirn_home/static/pipeline.js``，REQ-20260918-047）：把各阶段参数集中配置，
点「执行」由后台守护线程按阶段顺序自动跑到指定停点。

与长视频的差异：
- 阶段集合 = 短视频的 4 个可自动执行阶段（字幕提取 / 内容提取 / 内容优化 /
  粗剪合成）。Stage 1（选择源视频）与 Stage 6（精剪混编）**不参与**自动执行：
  前者需人工选素材，后者需人工逐条精修（见 REQ §4）。
- 编排**进程内直调模块函数**（不走 HTTP 自调）：与 UI 按钮调用同一份代码，
  无需会话透传 / 端口发现（长视频 pipeline 走 HTTP 是其历史设计）。

配置落盘：``tasks/<task_id>/short_video/<project_id>/flow_config.json``
执行历史：``tasks/<task_id>/short_video/<project_id>/flow_history.json``
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------- 阶段表（顺序即执行顺序）----------

STAGE_ORDER: list[tuple[str, str]] = [
    ("stage2_extract", "字幕提取"),
    ("stage3_analyze", "内容提取"),
    ("stage4_optimize", "内容优化"),
    ("stage5_coarse", "粗剪合成"),
]
STAGE_KEYS: list[str] = [k for k, _ in STAGE_ORDER]
STAGE_LABELS: dict[str, str] = dict(STAGE_ORDER)
STAGE_INDEX: dict[str, int] = {k: i for i, k in enumerate(STAGE_KEYS)}

# 配置里各阶段允许的键（白名单：防止前端传脏字段进配置文件）
_STAGE_FIELDS: dict[str, tuple[str, ...]] = {
    "stage2_extract": ("enabled", "model", "lang", "hotwords", "sd_switch"),
    "stage3_analyze": ("enabled", "template", "n_clips", "duration_min_sec",
                       "duration_max_sec", "use_image", "prompt"),
    "stage4_optimize": ("enabled",),
    "stage5_coarse": ("enabled", "start_ost", "end_ost"),
}
_TOP_FIELDS = ("run_mode", "stop_after", "since")

FLOW_CONFIG_NAME = "flow_config.json"
FLOW_HISTORY_NAME = "flow_history.json"
_HISTORY_MAX = 50
# 粗剪合成是异步任务，编排器轮询它的等待上限（秒）
_COARSE_POLL_TIMEOUT = 3600.0
_COARSE_POLL_INTERVAL = 2.0

_JOBS: dict[tuple[str, str], "FlowJob"] = {}
_JOBS_LOCK = threading.Lock()
_STOP_FLAGS: dict[tuple[str, str], bool] = {}
_STOP_LOCK = threading.Lock()


# ---------- 配置 ----------

def default_config() -> dict:
    """默认配置（REQ §2.2）：阶段全启用、数量 4、60-90 秒、hook_first、2→5。"""
    return {
        "stage2_extract": {
            "enabled": True, "model": "paraformer", "lang": "zh",
            "hotwords": "", "sd_switch": "no",
        },
        "stage3_analyze": {
            "enabled": True, "template": "hook_first", "n_clips": 4,
            "duration_min_sec": 60, "duration_max_sec": 90,
            "use_image": True, "prompt": "",
        },
        "stage4_optimize": {"enabled": True},
        "stage5_coarse": {"enabled": True, "start_ost": 0, "end_ost": 100},
        "run_mode": "stop_after",
        "stop_after": "stage5_coarse",
        "since": "stage2_extract",
    }


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def validate_config(cfg: Any) -> dict:
    """白名单 + 类型兜底 + 范围钳制。返回**新 dict**（不修改入参）。"""
    base = default_config()
    if not isinstance(cfg, dict):
        return base
    out: dict = {}
    for stage_key, fields in _STAGE_FIELDS.items():
        src = cfg.get(stage_key)
        src = src if isinstance(src, dict) else {}
        dst = dict(base[stage_key])
        for f in fields:
            if f in src:
                dst[f] = src[f]
        # 布尔 / 数值 / 字符串分别规范化
        dst["enabled"] = bool(dst.get("enabled", True))
        if stage_key == "stage2_extract":
            dst["model"] = str(dst.get("model") or "paraformer")
            dst["lang"] = str(dst.get("lang") or "zh")
            dst["hotwords"] = str(dst.get("hotwords") or "")
            dst["sd_switch"] = str(dst.get("sd_switch") or "no")
        elif stage_key == "stage3_analyze":
            dst["template"] = str(dst.get("template") or "hook_first")
            dst["n_clips"] = _clamp_int(dst.get("n_clips"), 4, 1, 10)
            dst["duration_min_sec"] = _clamp_int(dst.get("duration_min_sec"), 60, 10, 600)
            dst["duration_max_sec"] = _clamp_int(dst.get("duration_max_sec"), 90, 20, 900)
            if dst["duration_max_sec"] < dst["duration_min_sec"]:
                dst["duration_max_sec"] = dst["duration_min_sec"]
            dst["use_image"] = bool(dst.get("use_image", True))
            dst["prompt"] = str(dst.get("prompt") or "")
        elif stage_key == "stage5_coarse":
            # 毫秒偏移量（叠加到 highlight 时间戳上）：0 ~ 10 分钟
            dst["start_ost"] = _clamp_int(dst.get("start_ost"), 0, 0, 600000)
            dst["end_ost"] = _clamp_int(dst.get("end_ost"), 100, 0, 600000)
        out[stage_key] = dst
    # 顶层（并入 out —— 阶段级规范化结果必须一起返回）
    for f in _TOP_FIELDS:
        if f in cfg:
            out[f] = cfg[f]
    base = out
    rm = str(base.get("run_mode") or "stop_after")
    base["run_mode"] = rm if rm in ("to_end", "stop_after") else "stop_after"
    sa = base.get("stop_after")
    base["stop_after"] = sa if sa in STAGE_KEYS else None
    if base["run_mode"] == "to_end":
        base["stop_after"] = None
    since = base.get("since")
    base["since"] = since if since in STAGE_KEYS else STAGE_KEYS[0]
    return base


# ---------- 落盘 ----------

def project_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return Path(repo_root) / "tasks" / str(task_id) / "short_video" / str(project_id)


def config_path(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return project_dir(repo_root, task_id, project_id) / FLOW_CONFIG_NAME


def history_path(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return project_dir(repo_root, task_id, project_id) / FLOW_HISTORY_NAME


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if data is not None else default
    except Exception:  # noqa: BLE001 — 损坏文件按默认值处理，不阻断
        pass
    return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_config(repo_root: Path | str, task_id: str, project_id: str) -> dict:
    """读配置（不存在 → 默认配置）。"""
    raw = _load_json(config_path(repo_root, task_id, project_id), None)
    if not isinstance(raw, dict):
        return default_config()
    return validate_config(raw)


def save_config(repo_root: Path | str, task_id: str, project_id: str,
                 cfg: Any) -> dict:
    """保存配置（校验后落盘）。返回落盘的配置。"""
    valid = validate_config(cfg)
    _save_json(config_path(repo_root, task_id, project_id), valid)
    return valid


def load_history(repo_root: Path | str, task_id: str, project_id: str) -> list[dict]:
    data = _load_json(history_path(repo_root, task_id, project_id), [])
    return data if isinstance(data, list) else []


def append_history(repo_root: Path | str, task_id: str, project_id: str,
                   summary: dict) -> None:
    data = load_history(repo_root, task_id, project_id)
    data.append(summary)
    if len(data) > _HISTORY_MAX:
        data = data[-_HISTORY_MAX:]
    _save_json(history_path(repo_root, task_id, project_id), data)


# ---------- 运行态 ----------

@dataclass
class FlowJob:
    """一次自动执行的运行态。"""

    state: str = "idle"          # idle | running | done | error | stopped
    current_stage: str | None = None
    percent: float = 0.0
    logs: list[dict] = field(default_factory=list)
    stages_done: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    since: str | None = None
    summary: dict | None = None
    # REQ-20261022-144：本次自动流 ID（写进执行日志的 auto_session_id）
    auto_session_id: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "current_stage": self.current_stage,
            "current_stage_label": STAGE_LABELS.get(self.current_stage or "", ""),
            "percent": round(self.percent, 1),
            "logs": list(self.logs[-200:]),
            "stages_done": list(self.stages_done),
            "stages_skipped": list(self.stages_skipped),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "since": self.since,
            "summary": self.summary,
            "auto_session_id": self.auto_session_id,
        }


def _job_key(task_id: str, project_id: str) -> tuple[str, str]:
    return (str(task_id), str(project_id))


def _set_job(task_id: str, project_id: str, job: FlowJob) -> None:
    with _JOBS_LOCK:
        _JOBS[_job_key(task_id, project_id)] = job


def get_job(task_id: str, project_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(_job_key(task_id, project_id))
        return job.to_dict() if job else None


def is_running(task_id: str, project_id: str) -> bool:
    with _JOBS_LOCK:
        job = _JOBS.get(_job_key(task_id, project_id))
        return bool(job and job.state == "running")


def _log(job: FlowJob, stage: str, msg: str, level: str = "info") -> None:
    job.logs.append({
        "stage": stage, "msg": msg, "level": level,
        "time": time.strftime("%H:%M:%S"),
    })


def _request_stop(task_id: str, project_id: str) -> None:
    with _STOP_LOCK:
        _STOP_FLAGS[_job_key(task_id, project_id)] = True


def _consume_stop(task_id: str, project_id: str) -> bool:
    with _STOP_LOCK:
        return bool(_STOP_FLAGS.get(_job_key(task_id, project_id)))


def _clear_stop(task_id: str, project_id: str) -> None:
    with _STOP_LOCK:
        _STOP_FLAGS.pop(_job_key(task_id, project_id), None)


def stop_flow(task_id: str, project_id: str) -> bool:
    """请求停止。无运行中的 job → False。"""
    if not is_running(task_id, project_id):
        return False
    _request_stop(task_id, project_id)
    return True


# ---------- 阶段 handler（进程内直调模块）----------

def _stage2_handler(repo_root: Path, task_id: str, project_id: str,
                    cfg: dict, job: FlowJob) -> tuple[bool, str]:
    from slirn_home import short_video_extract as sv_extract

    state = sv_extract.run_stage2(
        repo_root, task_id, project_id,
        model=str(cfg.get("model") or "paraformer"),
        lang=str(cfg.get("lang") or "zh"),
        hotwords=str(cfg.get("hotwords") or ""),
        sd_switch=str(cfg.get("sd_switch") or "no"),
    )
    status = str((state or {}).get("status") or "")
    if status == "error":
        return False, str((state or {}).get("error") or "字幕提取失败")
    chars = (state or {}).get("srt_chars") or 0
    return True, f"字幕提取完成（{chars} 字）"


def _stage3_handler(repo_root: Path, task_id: str, project_id: str,
                    cfg: dict, job: FlowJob) -> tuple[bool, str]:
    from slirn_home import llm_config
    from slirn_home import short_video_analyze as sv_analyze

    entry = llm_config.get_current_entry(repo_root)
    # REQ-20261022-143 / G3：提示词块要真正生效 ——
    # 现状：_resolve_stage3_system_prompt 只把 template 当「custom_prompts 覆盖的
    # 键」，三套模板正文（hook_first/topic_cluster/story_arc）已退出默认链，
    # 单传 template 不改变 system prompt。此处把模板正文按占位符展开后作为
    # prompt 传入（用户自定义提示词仍优先）。
    template = str(cfg.get("template") or "hook_first")
    user_prompt = str(cfg.get("prompt") or "").strip()
    prompt = user_prompt
    if not prompt:
        tpl_text = (sv_analyze._PROMPT_TEMPLATES or {}).get(template, "")
        if tpl_text:
            n_clips = int(cfg.get("n_clips") or 4)
            dmin = float(cfg.get("duration_min_sec") or 60)
            dmax = float(cfg.get("duration_max_sec") or 90)
            has_dur_ph = "{dmin" in tpl_text or "{dmax" in tpl_text
            try:
                prompt = tpl_text.format(n_clips=n_clips, dmin=dmin, dmax=dmax)
            except (KeyError, IndexError, ValueError):
                prompt = tpl_text
            if not has_dur_ph:
                # 模板正文未带时长占位符（hook_first 的基础段写死「60-90 秒」）
                # → 追加本次实际参数，否则配置里的时长上下限到不了 LLM。
                prompt += (f"\n\n【本次参数】生成 {n_clips} 条短视频；"
                           f"每条总时长必须落在 {dmin:.0f}–{dmax:.0f} 秒（含端点）。")
    state = sv_analyze.run_stage3(
        repo_root, task_id, project_id,
        template=template,
        n_clips=int(cfg.get("n_clips") or 4),
        custom_prompt="",
        prompt=prompt,
        use_image=bool(cfg.get("use_image", True)),
        entry=entry,
        duration_min_sec=cfg.get("duration_min_sec"),
        duration_max_sec=cfg.get("duration_max_sec"),
    )
    status = str((state or {}).get("status") or "")
    if status == "error":
        return False, str((state or {}).get("error") or "内容提取失败")
    count = (state or {}).get("count") or len((state or {}).get("highlights") or [])
    return True, f"内容提取完成（{count} 条片段）"


def _stage4_handler(repo_root: Path, task_id: str, project_id: str,
                    cfg: dict, job: FlowJob) -> tuple[bool, str]:
    from slirn_home import short_video_optimize as sv_optimize

    state = sv_optimize.run_stage4_optimize(repo_root, task_id, project_id)
    status = str((state or {}).get("status") or "")
    if status == "error":
        return False, str((state or {}).get("error") or "内容优化失败")
    return True, "内容优化完成"


def _stage5_handler(repo_root: Path, task_id: str, project_id: str,
                    cfg: dict, job: FlowJob) -> tuple[bool, str]:
    """粗剪合成：异步任务 → 启动后轮询到结束。

    注意 start_ost / end_ost 的语义：**毫秒偏移量**（叠加到 highlight 的
    start_ms / end_ms 上），不是百分比 —— 与既有端点
    ``short_video_stage5_export`` 的 ``start_ost_ms`` / ``end_ost_ms`` 同义。
    """
    from slirn_home import short_video_coarse as sv_coarse

    started = sv_coarse.start_coarse_async(
        repo_root, task_id, project_id,
        start_ost_ms=int(cfg.get("start_ost") or 0),
        end_ost_ms=int(cfg.get("end_ost") or 100),
    )
    if not started:
        # 已在跑：等它跑完（不重复启动）
        _log(job, "stage5_coarse", "粗剪已在进行中，等待完成…")
    t0 = time.time()
    while True:
        if _consume_stop(task_id, project_id):
            return False, "用户请求停止"
        j = sv_coarse.coarse_job_status(task_id, project_id) or {}
        state = str(j.get("state") or "")
        prog = j.get("progress")
        if isinstance(prog, (int, float)) and job is not None:
            job.percent = max(job.percent, float(prog))
        if state == "done":
            return True, "粗剪合成完成"
        if state in ("failed", "error"):
            return False, str(j.get("error") or "粗剪合成失败")
        if time.time() - t0 > _COARSE_POLL_TIMEOUT:
            return False, "粗剪合成等待超时"
        time.sleep(_COARSE_POLL_INTERVAL)


HANDLERS = {
    "stage2_extract": _stage2_handler,
    "stage3_analyze": _stage3_handler,
    "stage4_optimize": _stage4_handler,
    "stage5_coarse": _stage5_handler,
}


# ---------- 编排 ----------

def run_flow(repo_root: Path | str, task_id: str, project_id: str, *,
             since: str | None = None) -> bool:
    """启动后台守护线程跑流程；已在 running → False。"""
    if is_running(task_id, project_id):
        return False
    _clear_stop(task_id, project_id)
    # REQ-20261022-144：本次自动流的会话 ID（与长视频一致）——
    # 各阶段写出的执行日志带 auto=true + 该 session_id，
    # 前端「⚙ 自动」筛选据此聚合同一轮自动流。
    auto_session_id = uuid.uuid4().hex[:12]
    job = FlowJob(state="running", started_at=time.time(), since=since,
                  auto_session_id=auto_session_id)
    _set_job(task_id, project_id, job)

    def _run() -> None:
        try:
            cfg = load_config(repo_root, task_id, project_id)
            since_key = since or cfg.get("since") or STAGE_KEYS[0]
            since_idx = STAGE_INDEX.get(since_key, -1)
            stop_after = cfg.get("stop_after")
            stop_label = STAGE_LABELS.get(stop_after or "", "不限")
            mode_desc = ("跑到最后" if cfg.get("run_mode") == "to_end"
                         else f"停在「{stop_label}」后")
            _log(job, "_start",
                 f"运行模式：{mode_desc}（共 {len(STAGE_ORDER)} 阶段）")
            for stage_key, stage_label in STAGE_ORDER:
                idx = STAGE_INDEX[stage_key]
                if idx < since_idx:
                    continue
                if _consume_stop(task_id, project_id):
                    _log(job, stage_key, "⏹ 用户请求停止", "warn")
                    job.state = "stopped"
                    break
                job.current_stage = stage_key
                job.percent = (idx + 1) / max(len(STAGE_ORDER), 1) * 95
                _log(job, stage_key, f"== 开始阶段：{stage_label} ==")
                stage_cfg = cfg.get(stage_key) or {}
                if not stage_cfg.get("enabled", True):
                    job.stages_skipped.append(stage_key)
                    _log(job, stage_key, f"⏭ {stage_label}：配置为不启用，跳过", "warn")
                    continue
                handler = HANDLERS.get(stage_key)
                if handler is None:
                    _log(job, stage_key, f"未知阶段 {stage_key}，跳过", "warn")
                    continue
                try:
                    # REQ-20261022-144：标记「自动执行」→ 该阶段写出的执行日志
                    # 带 auto=true + auto_session_id（contextvar，同线程生效）
                    from slirn_home import execution_history as _eh
                    with _eh.auto_context(auto_session_id):
                        ok, msg = handler(Path(repo_root), task_id, project_id,
                                          stage_cfg, job)
                except Exception as e:  # noqa: BLE001
                    log.exception("[sv-flow][%s/%s] %s 抛异常", task_id, project_id, stage_key)
                    job.state = "error"
                    job.error = f"{stage_label}：{e}"
                    _log(job, stage_key, f"❌ 异常：{e}", "error")
                    break
                if not ok:
                    job.state = "error"
                    job.error = msg
                    _log(job, stage_key, f"❌ {msg}", "error")
                    break
                job.stages_done.append(stage_key)
                _log(job, stage_key, f"✅ {msg}", "ok")
                if stop_after and stage_key == stop_after:
                    _log(job, stage_key,
                         f"⏸ 配置要求「{stage_label}」后停（等人工）", "info")
                    job.state = "stopped"
                    break
            else:
                job.state = "done"
                if job.stages_skipped:
                    _log(job, "_end",
                         f"⚠️ 全部阶段完成（含 {len(job.stages_skipped)} 个跳过）", "warn")
                else:
                    _log(job, "_end", "✅ 全部阶段完成", "ok")
            job.finished_at = time.time()
            job.percent = 100.0 if job.state == "done" else job.percent
            summary = {
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "duration_ms": int((job.finished_at - (job.started_at or job.finished_at)) * 1000),
                "status": job.state,
                "stages_done": list(job.stages_done),
                "stages_skipped": list(job.stages_skipped),
                "since": since_key,
                "error": job.error,
            }
            job.summary = summary
            append_history(repo_root, task_id, project_id, summary)
        except Exception as e:  # noqa: BLE001 — 线程兜底
            log.exception("[sv-flow][%s/%s] 调度器异常", task_id, project_id)
            job.state = "error"
            job.error = str(e)
            job.finished_at = time.time()

    threading.Thread(target=_run, name=f"sv-flow-{task_id}-{project_id}",
                     daemon=True).start()
    return True
