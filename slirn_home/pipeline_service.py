"""REQ-20260918-047 — 流程配置 + 自动执行。

Per-task 持久化 `outputs/pipeline.json`：
- `config`：6 阶段可选项（subtitle_generation/subtitle_review/rough_cut/
  rough_compose/optimize/fine_cut；v5 schema）
- `stop_after`：流程层总停点（覆盖每阶段的 stop_after，取更严的）
- `history`：最近 10 次完整运行的 summary

后台调度器（`_PIPELINE_JOBS[tid]`）守护线程顺序跑 6 个 handler，每个
handler 通过 in-process HTTP client（urllib）调现有 `/slirn/api/*` 端点，
复用现有 client polling 与 modal 状态机。

两层 stop_after：每阶段 config 内的 stop_after + 流程层 stop_after，取
阶段索引较小者（更靠前）为准。

任何阶段报错即停（不自动重试）；手动按钮始终可用，与自动跑共用同一组
service 的 _JOBS，并发时按各 service 的「running 拒绝新起」语义处理。

设计见 docs/design/DESIGN-20260918-047-pipeline-auto-run.md。

本批次更新（REQ-20260921-NNN — v2/v3/v4 多子任务一起收）：
- v2：default_config 移除 cover_image/bg_image/bgm 死字段（实际在 fc.json
  的 materials.{cover,bg,audio}.path，由工作台第 6 阶段详情页上传写入）；
  _check_fine_cut_materials 简化规则 —— video/subtitle 上游产物不预检，
  cover/bg 仅当 layout.enabled=True 时检查，reference/audio 缺失不阻塞。
- v3：_poll_status 改 POST 调 4 个 status 端点
  （/subtitle_status / /revise_status / /compose_rough_status /
  /optimize_subtitle_status）—— 早期版本误用 _http_get 导致
  state 永远不是 done，主循环卡第一阶段。/render_status 由独立
  _poll_export 处理（GET 端点）不要混用。handler_optimize accept_all
  改构造全量 decisions 数组 {occ_id, applied, after, reviewed} ——
  save_optimize_subtitle 端点硬约束；不接受 applied_all 标志。
- v4：fine_cut 加 range_enabled 门控「区间导出」（HH:MM:SS 输入）——
  不勾 = 全片（保留默认意图），勾上 = 按 preview_start/duration 导。
  validate_config 同步兜底 range_enabled bool（脏数据 → False）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# 落盘文件名（与 execution_history 同模式：单文件 + 原子写 + 单写锁）
PIPELINE_FILENAME = "pipeline.json"

# 阶段顺序：key, 中文名, 工作台面板 key
# REQ-20260921-NNN：6 阶段（最后 fine_cut = 精剪合成 / 最终导出）。
# assets 阶段（上传/时间截取）不进 pipeline — 手动阶段，无自动 API。
STAGE_ORDER: list[tuple[str, str, str]] = [
    ("subtitle_generation", "字幕生成", "subtitle"),
    ("subtitle_review", "字幕修订", "subtitle_review"),
    ("rough_cut", "切分修剪", "rough_cut"),
    ("rough_compose", "粗剪合成", "rough_compose"),
    ("optimize", "优化字幕", "fine_review"),
    ("fine_cut", "精剪合成", "fine_cut"),
]

# 阶段索引（用于 stop_after 比较）
STAGE_INDEX: dict[str, int] = {k: i for i, k in enumerate(s[0] for s in STAGE_ORDER)}
STAGE_INDEX[None] = 99  # 流程跑完 = 99（最大，永不因 stop_after 停）

# 单写锁（跨任务共享）
_WRITE_LOCK = threading.Lock()

# 模块级 job 表：tid → PipelineJob
_PIPELINE_JOBS: dict[str, "PipelineJob"] = {}
_JOBS_LOCK = threading.Lock()

# stop 请求位：tid → True 表示请求停止
_STOP_FLAGS: dict[str, bool] = {}
_STOP_LOCK = threading.Lock()


# =============== 数据类 ===============


@dataclass
class PipelineJob:
    """内存中的流程任务状态（每次 run_pipeline 启动时新建 / 复用）。"""

    state: str = "idle"  # idle / running / done / error / stopped
    current_stage: str | None = None
    percent: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    history: list[dict] = field(default_factory=list)  # 本次运行的日志（结构见 _append_log）
    summary: dict | None = None  # 完成后写回的 summary {stages_done, total_ms, status}
    # REQ-20260920-081：自动流会话 ID；run_pipeline 启动时生成；handler 调 _http_post
    # 时透传给后端 endpoint，写入 execution_history 的 auto_session_id 字段。
    auto_session_id: str = ""
    # REQ-20261021-139 / D4：发起者的 slirn_session cookie 值。内部 HTTP 调用
    # 必须带它 —— 鉴权中间件只认 cookie（X-Slirn-Auto 不是旁路），不带则 401。
    # 端点在启动后台线程前取到（contextvars 不跨线程），经 run_pipeline 落到这里。
    session_token: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "current_stage": self.current_stage,
            "percent": round(self.percent, 2),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "log": list(self.history),
            "summary": self.summary,
        }


# =============== 配置 schema ===============


def default_config() -> dict:
    """v5 默认配置：6 阶段都跑，顶层 run_mode='stop_after' / stop_after='subtitle_review'。

    REQ-20260921-NNN 新增字段：
    - subtitle_review / rough_cut：link_person_ids（自动关联人员ID checkbox）
    - fine_cut：精剪合成（封面/背景/背景音乐/设置参数/起点/时长/enabled）
    - 顶层 run_mode：to_end | stop_after（决定 stop_after 是否生效）

    2026-09-30 用户要求：流程配置的复选框默认全部选中 — speaker_diarization /
    accept_all_suggestions / link_person_ids ×2 / accept_all_replacements 一律
    True（此前默认 False，每项都得手动点开）。已保存过显式 False 的任务不受
    影响（validate_config 按用户保存值合并，不会被新默认覆盖）。
    fine_cut 无复选框（导出方式是二选一 radio），enabled 默认 True 见下方
    radio-mode 注释。
    """
    return {
        "subtitle_generation": {
            "speaker_diarization": True,
        },
        "subtitle_review": {
            "accept_all_suggestions": True,
            "skip_categories": [],
            # REQ-20260918-049：大模型分析严谨性级别（pipe 通道支持高/中/低；
            # custom 档需在 pipe-panel 外的工作台才有 textarea，前端降级为 medium）
            "rigor": "medium",
            # REQ-20260921-NNN：自动关联人员ID（调 /rev_speaker_link）
            "link_person_ids": True,
        },
        "rough_cut": {
            "delete_speakers": [],
            "default_decision": "keep",
            # REQ-20260921-NNN：自动关联人员ID（调 /cut_speaker_link）
            "link_person_ids": True,
        },
        "rough_compose": {},
        "optimize": {
            "accept_all_replacements": True,
        },
        # REQ-20260921-NNN：精剪合成（最终导出视频）配置
        # v2 用户反馈：cover_image / bg_image / bgm 是死字段 —— handler 不读，
        # 实际素材路径在 fc.json 的 materials.{cover,bg,audio}.path，由工作台第 6 阶段
        # 详情页上传写入。流程配置面板只保留「跑不跑」+「跑哪段」+「用哪份参数」3 类决策。
        "fine_cut": {
            # REQ-20260921-NNN-radio-mode：默认 enabled=true + range_enabled=true
            # （与 UI 默认 radio=range「按区间导出一段」一致）。设计要求：默认
            # 行为 = 跑到 fine_cut 就导 10 分钟看效果；想导全片用户在 UI 切到
            # 「出整个片」；想彻底跳过 fine_cut 走顶层 stop_after="optimize"。
            "enabled": True,
            "params_source": "current",   # "current" | "template:<profile_id>"
            "range_enabled": True,         # 默认按区间（10 分钟）
            "preview_start": 0.0,         # 导出起点（秒，0 = 全篇）
            "duration": 600.0,            # 导出时长（秒，默认 10 分钟）
        },
        # REQ-20260921-NNN：顶层运行模式
        #   "to_end"      = 一键跑到底（忽略 stop_after）
        #   "stop_after"  = 按下方 stop_after 字段决定停在哪
        "run_mode": "stop_after",
        # 兼容 v4 字段：在哪个阶段完成后停（None = 跑到底）
        "stop_after": "subtitle_review",
    }


def validate_config(cfg: dict) -> dict:
    """校验 + 补全配置（缺字段用 default_config 兜底）。

    v5 schema：
    - 阶段 dict 内不再含 stop_after（顶层 stop_after 才是权威源）
    - 顶层 stop_after 是字符串（STAGE_ORDER 的某个 key）或 None
    - 顶层 run_mode ∈ {"to_end", "stop_after"}（v4 无此字段，自动推算）
    - fine_cut.enabled 默认 True（radio 模式：导出方式二选一必跑，跳过走顶层 stop_after）
    - 兼容 v4 旧数据：缺 run_mode 但有 stop_after → "stop_after"；缺且 stop_after=None → "to_end"

    返回合法配置（不抛异常 — 任何坏字段都用默认值替换，便于 UI 编辑容错）。
    """
    base = default_config()
    if not isinstance(cfg, dict):
        return base
    # 兼容旧数据：v4 无 run_mode → 按 stop_after 推算
    if "run_mode" in cfg:
        rm = cfg["run_mode"]
        if rm in ("to_end", "stop_after"):
            base["run_mode"] = rm
    else:
        sa = cfg.get("stop_after")
        base["run_mode"] = "to_end" if sa is None else "stop_after"
    # 顶层 stop_after 校验
    if "stop_after" in cfg:
        v = cfg["stop_after"]
        if v is None:
            base["stop_after"] = None
        elif isinstance(v, str) and v in STAGE_INDEX and STAGE_INDEX[v] != 99:
            base["stop_after"] = v
        # else: 无效值（None 字符串、未知 key、空串）→ 保留默认
    # run_mode == "to_end" → 强制 stop_after = None（忽略 stop_after 配置）
    if base["run_mode"] == "to_end":
        base["stop_after"] = None
    for stage_key in (s[0] for s in STAGE_ORDER):
        user_stage = cfg.get(stage_key)
        if not isinstance(user_stage, dict):
            continue  # 用默认
        merged = dict(base[stage_key])
        for k, v in user_stage.items():
            if k == "stop_after":
                # v3/v2 阶段内 stop_after：丢弃（顶层 stop_after 才是权威）
                continue
            elif k in merged:
                merged[k] = v
        base[stage_key] = merged
    # REQ-20260918-049：subtitle_review.rigor 校验（脏数据容错 → 回落 medium）
    valid_rigor = ("high", "medium", "low", "custom")
    if base["subtitle_review"].get("rigor") not in valid_rigor:
        base["subtitle_review"]["rigor"] = "medium"
    # REQ-20260921-NNN-radio-mode：fine_cut.enabled / range_enabled 必须 bool。
    # 脏数据兜底回退到 default_config 的默认值（True — 与 UI 默认 radio=range 一致），
    # 而非硬编码 False（v5 旧逻辑已废）。
    if not isinstance(base["fine_cut"].get("enabled"), bool):
        base["fine_cut"]["enabled"] = True
    if not isinstance(base["fine_cut"].get("range_enabled"), bool):
        base["fine_cut"]["range_enabled"] = True
    # REQ-20260921-NNN-range-overrides-enabled：legacy 迁移 — 旧 cfg 可能是
    # enabled=False, range_enabled=True（联动修复前的脏状态）。规范化时强制
    # enabled=True。新 radio UI 已物理保证一致性，本块仅为兼容 legacy pipeline.json。
    if base["fine_cut"].get("range_enabled") is True:
        base["fine_cut"]["enabled"] = True
    # link_person_ids 兜底为 bool（脏数据 → 新默认 True，2026-09-30 起复选框默认全选）
    for sk in ("subtitle_review", "rough_cut"):
        if not isinstance(base[sk].get("link_person_ids"), bool):
            base[sk]["link_person_ids"] = True
    return base


# =============== 持久化 ===============


def _read(outputs_dir: Path) -> dict | None:
    """读取 pipeline.json；不存在或损坏 → None。"""
    p = outputs_dir / PIPELINE_FILENAME
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("[pipeline][%s] 读取失败: %s", outputs_dir, e)
        return None


def _write_atomic(outputs_dir: Path, data: dict) -> None:
    """原子写：先写 .tmp 再 rename。"""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    tmp = outputs_dir / (PIPELINE_FILENAME + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(outputs_dir / PIPELINE_FILENAME)


def load_pipeline(outputs_dir: Path) -> dict | None:
    """读取 + 校验 pipeline.json。"""
    with _WRITE_LOCK:
        data = _read(outputs_dir)
    if data is None:
        return None
    # 校验/补全（merge default）保证调用方拿到完整 schema
    merged = validate_config(data.get("config") or {})
    return {
        "version": int(data.get("version") or 1),
        "config": merged,
        "updated_at": data.get("updated_at"),
        "history": list(data.get("history") or [])[-10:],  # 最近 10 条
    }


def save_pipeline(outputs_dir: Path, cfg: dict) -> str:
    """保存配置 + 记录 updated_at；保留 history 最近 10 条。返回 ISO 时间戳。

    cfg 必须含 config 字段（5 阶段）。
    """
    cfg_in = cfg.get("config") if isinstance(cfg, dict) else None
    merged = validate_config(cfg_in or cfg)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 保留旧 history
    old = _read(outputs_dir) or {}
    new_data = {
        "version": 4,  # v5 schema：6 阶段 + 顶层 run_mode + fine_cut 配置
        "config": merged,
        "updated_at": ts,
        "history": list(old.get("history") or [])[-10:],
    }
    with _WRITE_LOCK:
        _write_atomic(outputs_dir, new_data)
    return ts


def append_history(outputs_dir: Path, summary: dict) -> None:
    """一次完整 run 完成后追加一条 summary（最多保留 10 条）。"""
    try:
        with _WRITE_LOCK:
            data = _read(outputs_dir) or {}
            history = list(data.get("history") or [])
            history.append(summary)
            data["history"] = history[-10:]
            _write_atomic(outputs_dir, data)
    except Exception as e:  # noqa: BLE001 — 历史写失败不影响主流程
        log.warning("[pipeline][%s] append_history 失败: %s", outputs_dir, e)


# =============== 内存 job 管理 ===============


def pipeline_status(tid: str) -> dict | None:
    """读内存 job（无 job → None 表示从未跑过）。"""
    with _JOBS_LOCK:
        j = _PIPELINE_JOBS.get(tid)
        return j.to_dict() if j else None


def _is_running(tid: str) -> bool:
    with _JOBS_LOCK:
        j = _PIPELINE_JOBS.get(tid)
        return bool(j and j.state == "running")


def _set_job(tid: str, job: PipelineJob) -> None:
    with _JOBS_LOCK:
        _PIPELINE_JOBS[tid] = job


def _request_stop(tid: str) -> None:
    with _STOP_LOCK:
        _STOP_FLAGS[tid] = True


def _consume_stop(tid: str) -> bool:
    with _STOP_LOCK:
        return bool(_STOP_FLAGS.pop(tid, False))


def _clear_stop(tid: str) -> None:
    with _STOP_LOCK:
        _STOP_FLAGS.pop(tid, None)


# =============== 调度器 ===============


def _http_post(api: str, path: str, payload: dict, *, timeout: float = 30.0,
               auto_session_id: str = "", session_token: str = "") -> dict:
    """in-process HTTP client：POST {api}{path} → JSON 响应。

    与前端 router.js 的 postJSON 同源思路；用 urllib 零依赖。
    不抛异常：网络/解析错误 → 返回 {ok: False, error: <msg>}。

    REQ-20260919-075：自动加 X-Slirn-Auto=1 header，让后端 endpoint 把当前调用
    识别为流程自动触发（写入 execution_history 的 auto=true）。

    REQ-20260920-081：auto_session_id 非空时附加 X-Slirn-Auto-Session header，
    让后端 endpoint 把同一次自动流的多次操作聚合到同一个 session。

    REQ-20261021-139 / D4：session_token 非空时附加 Cookie: slirn_session=… ——
    鉴权中间件只认 cookie，X-Slirn-Auto 不是旁路；不带 cookie 的内部调用一律 401。
    """
    url = api.rstrip("/") + path
    body = json.dumps(payload or {}).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Slirn-Auto": "1"}
    if auto_session_id:
        headers["X-Slirn-Auto-Session"] = str(auto_session_id)
    if session_token:
        headers["Cookie"] = f"slirn_session={session_token}"
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"ok": False, "error": "非 JSON 响应"}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "error": (
                "内部调用被拒绝(401)：会话已过期或缺少会话令牌，"
                "请重新登录后再跑流程")}
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"连接失败: {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _http_get(api: str, path: str, payload: dict | None = None, *, timeout: float = 30.0) -> dict:
    """GET 用 query 参数传 task_id（与 subtitle_status 端点签名一致）。

    注意：本函数目前无生产调用方（状态端点都是 POST，见 _poll_status docstring），
    故 REQ-20261021-139 / D4 未给它加会话透传；若将来启用，需一并补 Cookie 头。
    """
    from urllib.parse import urlencode

    q = urlencode(payload or {})
    url = api.rstrip("/") + path + ("?" + q if q else "")
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"ok": False, "error": "非 JSON 响应"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _poll_status(api: str, status_path: str, tid: str, *,
                 interval: float = 2.0, timeout: float = 24 * 3600.0,
                 session_token: str = "") -> tuple[str, str]:
    """轮询 status 端点直到 done / error / 超时。

    REQ-20260921-NNN-v3：必须用 POST 调 status 端点。早期版本误用 _http_get，
    但 /subtitle_status / /revise_status / /compose_rough_status / /optimize_subtitle_status
    4 个端点都是 @app.app.post(...)（GET 返回 405），导致 _poll_status 永远轮询不到
    state="done"，handler 永远不返回，主循环永远卡在第一个阶段。
    /render_status 是 GET，由单独的 _poll_export 处理，不要混用。

    返回 (state, error) — state ∈ {done, error, timeout}。
    """
    t0 = time.time()
    last_err = ""
    while True:
        if _consume_stop(tid + ":poller"):  # 用户中途 stop 也算
            return ("stopped", "用户请求停止")
        try:
            # 关键：POST（与 4 个 status 端点签名一致）；不要改成 _http_get
            r = _http_post(api, status_path, {"task_id": tid}, timeout=15.0,
                           session_token=session_token)
            if isinstance(r, dict) and r.get("ok") is False:
                # 会话过期（401）这类持久失败不会自愈：立刻失败，
                # 不要空转到超时才报一个空 error（REQ-20261021-139 评审 R1）。
                return ("error", str(r.get("error") or "内部调用失败"))
            job = (r or {}).get("job") or {}
            state = str(job.get("state") or "")
            if state == "done":
                return ("done", "")
            if state == "error":
                return ("error", str(job.get("error") or "服务报错"))
            last_err = str(job.get("error") or "")
        except Exception as e:  # noqa: BLE001
            last_err = f"轮询异常: {e}"
        if time.time() - t0 > timeout:
            return ("timeout", f"轮询超时（>{int(timeout)}s）: {last_err}")
        time.sleep(interval)


# =============== 5 个 stage handler ===============
# 每个 handler 签名：handler(tid, cfg, outputs_dir, api, log_fn) -> tuple[bool, str]
# bool = success（不含 stop_after），str = 详情。失败时调度器整体停。


def _log(job: PipelineJob, stage: str, msg: str, level: str = "info") -> None:
    """调度器内 log helper：写进内存 job.history（前端轮询可见）。"""
    entry = {
        "stage": stage,
        "msg": msg,
        "level": level,
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    job.history.append(entry)
    if len(job.history) > 200:  # 防止内存爆炸
        job.history = job.history[-200:]


def _check_prereq(outputs_dir: Path, required_files: list[str]) -> tuple[bool, str]:
    """检查 prereq 产物文件是否存在。"""
    missing = [f for f in required_files if not (outputs_dir / f).exists()]
    if missing:
        return (False, f"缺少依赖产物：{', '.join(missing)}")
    return (True, "")


# REQ-20260921-NNN：精剪合成（fine_cut）前置物 料预检 helper。
# 流程跑到 fine_cut 之前必须先确认本任务在精剪合成详情页已维护好所需素材。
# 规则（与 _render_fine_cut_zone / _FINE_AUTO_KINDS 对齐 + REQ-20260921-NNN v2 用户反馈）：
#   - video：上游产物（auto-fetch from rough_compose.mp4），handler 内 _check_prereq 已把
#     关，无须在预检里强求 fc.json 的 materials.video.path 存在（用户无法提前提供）。
#   - subtitle：上游产物（auto-fetch from optimized_subs.srt），同上不预检；
#     render 时若 path 缺失会自动跳过 burn-in（无字幕也能跑）。
#   - cover / bg：仅在 layout.{cover,bg}.enabled=True 时要求 materials.<kind>.path 存在；
#     enabled=False（默认）→ 不阻塞（用户可以跑出"无封面无背景"的精剪视频）。
#   - reference：仅作 AI 解析辅助用，缺失不阻塞（render 代码只在有 path 时才预缩图）。
#   - audio（BGM）：可选，缺失不阻塞（why_no_bgm 已有诊断）。
#
# 返回 dict：{ok: bool, missing: [{kind, label}], optional_missing: [{kind, label}], reason: str}
# 让前端可结构化渲染「去精剪合成页补 X」。
def _check_fine_cut_materials(tid: str, fc_root: Path, fine_cut_cfg: dict) -> dict:
    """精剪合成的素材预检（不读上游产物路径 —— 那是运行时 / export 时的事）。

    fc_root：mgr.tasks_dir / tid / fine_compose.json
    fine_cut_cfg：cfg.fine_cut dict（仅看 enabled，决定要不要预检；不强制读布局）。
    """
    # 默认无害：未启用 fine_cut → 不预检，让后续跳过
    if not bool((fine_cut_cfg or {}).get("enabled", False)):
        return {"ok": True, "missing": [], "optional_missing": [], "reason": ""}
    # 没 fc.json → 视同「无任何精剪参数」，预检逻辑本身没意义；
    # 但「缺参数」这件事另由参数模板预检负责；这里仅在 fc.json 存在时做素材预检。
    if not fc_root.exists():
        return {"ok": True, "missing": [], "optional_missing": [], "reason": ""}
    try:
        fc = json.loads(fc_root.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "missing": [], "optional_missing": [],
                "reason": f"fine_compose.json 解析失败：{e}"}
    if not isinstance(fc, dict):
        return {"ok": True, "missing": [], "optional_missing": [], "reason": ""}
    materials = fc.get("materials") or {}
    layout = fc.get("layout") or {}
    _KIND_LABELS = {
        "video":     "粗剪视频",
        "subtitle":  "字幕文件",
        "cover":     "封面图片",
        "bg":        "背景图片",
        "reference": "参考位置关系图",
        "audio":     "背景音乐",
    }
    missing: list[dict] = []
    optional_missing: list[dict] = []

    def _has_path(kind: str) -> bool:
        mat = materials.get(kind) or {}
        return bool((mat.get("path") or "").strip())

    # video / subtitle：上游产物自动获取，handler 内已做 prereq / render-time 容错，不预检
    # （用户根本没法提前提供 fc.json 里的 materials.video.path 路径去指向一个尚未产生的文件）
    # cover：仅当 layout.cover.enabled=True 时要求 path
    if (layout.get("cover") or {}).get("enabled") and not _has_path("cover"):
        missing.append({"kind": "cover", "label": _KIND_LABELS["cover"]})
    # bg：仅当 layout.bg.enabled=True 时要求 path
    if (layout.get("bg") or {}).get("enabled") and not _has_path("bg"):
        missing.append({"kind": "bg", "label": _KIND_LABELS["bg"]})
    # reference：AI 解析辅助，缺失不阻塞
    if not _has_path("reference"):
        optional_missing.append({"kind": "reference", "label": _KIND_LABELS["reference"]})
    # audio：可选
    if not _has_path("audio"):
        optional_missing.append({"kind": "audio", "label": _KIND_LABELS["audio"]})

    return {
        "ok": len(missing) == 0,
        "missing": missing,
        "optional_missing": optional_missing,
        "reason": "" if not missing else
            "缺少素材：" + "、".join(m["label"] for m in missing)
            + " — 请到「第 6 阶段 · 精剪合成」详情页上传"
            + "（或在精剪合成页关闭该素材的启用开关）",
    }


def _check_fine_cut_params(tid: str, fc_root: Path, fine_cut_cfg: dict) -> dict:
    """精剪参数预检（REQ-20260921-NNN）。

    规则：
    - fc.json 不存在 → 必须显式选模板（template:<id>）才能跑
      （用户无法在第 6 阶段页面导入「当前参数」，因为没 fc.json 可覆盖）
    - fc.json 存在 → 「当前参数」可用

    返回 {ok, reason, has_fc_json, params_source}。
    """
    has_fc = bool(fc_root.exists())
    params_source = str((fine_cut_cfg or {}).get("params_source") or "current")
    if has_fc:
        return {"ok": True, "reason": "", "has_fc_json": True,
                "params_source": params_source}
    # fc.json 不存在：
    if params_source.startswith("template:") and params_source != "template:":
        return {"ok": True, "reason": "", "has_fc_json": False,
                "params_source": params_source}
    if params_source == "import":
        return {"ok": True, "reason": "", "has_fc_json": False,
                "params_source": params_source}
    # current 或其他无效值 → 阻断
    return {"ok": False,
            "reason": "本任务尚未保存精剪参数（fine_compose.json）— "
                      "请在「第 6 阶段 · 精剪合成」详情页点「导入参数」"
                      "，或在此处选择模板。",
            "has_fc_json": False,
            "params_source": params_source}


def fine_cut_preflight(tid: str, cfg: dict, outputs_dir: Path) -> dict:
    """REQ-20260921-NNN：fine_cut 阶段启动前的素材 + 参数双重预检。

    用于 `/pipeline_run` 启动前快速校验，避免跑到 fine_cut 才报错（重编码几小时）。
    仅当 fine_cut.enabled=True 时严格检查；否则返回 ok=True（跳过整个精剪）。

    返回 dict：
      - ok: bool（必须 True 才能启动；任一子检查失败 → False）
      - reason: str（人类可读说明；前端直接 toast）
      - enabled: bool（是否启用 fine_cut，便于前端条件渲染）
      - has_fc_json: bool（fc.json 是否存在 —— 影响前端能否选「当前参数」）
      - materials: dict {missing[], optional_missing[], reason}
      - parameters: dict {ok, reason, has_fc_json, params_source}
    """
    fc_cfg = (cfg or {}).get("fine_cut") or {}
    # REQ-20260921-NNN-range-overrides-enabled：勾了 range_enabled 也视为启用
    # （与 handler_fine_cut 跳过条件保持一致 — 单点真相，避免「预检通过但
    # 跑到 handler 又被 skip」的语义错位）。
    enabled = bool(fc_cfg.get("enabled", False)) or bool(fc_cfg.get("range_enabled", False))
    if not enabled:
        return {"ok": True, "reason": "", "enabled": False,
                "has_fc_json": True,
                "materials": {"ok": True, "missing": [], "optional_missing": [], "reason": ""},
                "parameters": {"ok": True, "reason": "", "has_fc_json": True,
                               "params_source": "current"}}
    # fc_root = tasks_dir / tid / fine_compose.json（与 _save_fine_compose 路径约定一致）
    # outputs_dir 通常是 mgr.tasks_dir / tid / outputs；fc_root 与 outputs_dir 同级
    fc_root = outputs_dir.parent / "fine_compose.json"
    mat = _check_fine_cut_materials(tid, fc_root, fc_cfg)
    par = _check_fine_cut_params(tid, fc_root, fc_cfg)
    ok = bool(mat.get("ok")) and bool(par.get("ok"))
    if ok:
        reason = ""
    else:
        # 优先报参数（更紧迫：模板选择错了根本跑不起来；素材可中途补）
        reason = par.get("reason") or mat.get("reason") or ""
    return {"ok": ok, "reason": reason, "enabled": enabled,
            "has_fc_json": bool(par.get("has_fc_json")),
            "materials": mat, "parameters": par}


def handler_subtitle_generation(tid: str, cfg: dict, outputs_dir: Path, api: str,
                                job: PipelineJob) -> tuple[bool, str]:
    """字幕生成阶段 — 调 /gen_subtitle + 等 /subtitle_status。"""
    sd_on = bool(cfg.get("speaker_diarization", True))
    _log(job, "subtitle_generation", f"启动字幕生成（区分说话人={sd_on}）")
    r = _http_post(api, "/gen_subtitle", {"task_id": tid, "sd": sd_on},
                   auto_session_id=job.auto_session_id,
                   session_token=job.session_token)
    if not r.get("ok"):
        return (False, f"启动字幕生成失败：{r.get('error') or '未知错误'}")
    _log(job, "subtitle_generation", "等待字幕生成完成（后台运行中…）")
    state, err = _poll_status(api, "/subtitle_status", tid, session_token=job.session_token)
    if state == "done":
        _log(job, "subtitle_generation", "✅ 字幕生成完成", "ok")
        return (True, "")
    return (False, f"字幕生成{state}：{err}")


def handler_subtitle_review(tid: str, cfg: dict, outputs_dir: Path, api: str,
                            job: PipelineJob) -> tuple[bool, str]:
    """字幕修订阶段 — 启动大模型分析 + 等完成 + (可选) 全接受 save_revision。

    prereq：subtitle.json 存在（由 handler 顺序保证，但兜底再 check）。
    """
    # REQ-20260918-053：执行日志 — 字幕修订（LLM 调用，分钟级）
    from .execution_history import (
        KIND_SUBTITLE_REVIEW, record_start, record_finish, patch_extra,
    )
    rev_id = record_start(outputs_dir, KIND_SUBTITLE_REVIEW,
                          extra={"rigor": cfg.get("rigor"), "accept_all": bool(cfg.get("accept_all_suggestions", True))},
                          auto=True, auto_session_id=job.auto_session_id)
    # 用容器存结果（finally 里统一 record_finish — 7 个 return 点不会再漏）
    _result: list = [False, ""]
    try:
        ok, err = _check_prereq(outputs_dir, ["subtitle.json"])
        if not ok:
            _log(job, "subtitle_review", f"跳过：{err}", "warn")
            _result[0] = True
            _result[1] = "skip"
            return (True, "skip")  # 跳过不算失败（设计文档 AC-6）
        # REQ-20260918-049：从配置读 rigor（pipe 通道只支持 high/medium/low，
        # custom 档在工作台有独立 textarea；前端读 form 时已降级为 medium，
        # 这里再校验一次兜底防御）
        rigor = str(cfg.get("rigor") or "medium")
        if rigor not in ("high", "medium", "low"):
            rigor = "medium"
        accept_all = bool(cfg.get("accept_all_suggestions", True))
        skip_cats = list(cfg.get("skip_categories") or [])
        _log(job, "subtitle_review", f"启动大模型分析（严谨性 {rigor}）")
        r = _http_post(api, "/revise_subtitle", {"task_id": tid, "rigor": rigor, "force": True},
                       auto_session_id=job.auto_session_id,
                       session_token=job.session_token)
        if not r.get("ok"):
            _result[0] = False
            _result[1] = f"启动修订失败：{r.get('error')}"
            return (False, _result[1])
        _log(job, "subtitle_review", "等待大模型分析完成…")
        state, err = _poll_status(api, "/revise_status", tid, session_token=job.session_token)
        if state != "done":
            _result[0] = False
            _result[1] = f"字幕修订{state}：{err}"
            return (False, _result[1])
        # REQ-20260921-NNN：先做 link_person_ids（不依赖 save_revision 全接受 —
        # rev_speaker_link 只读 subtitle.json + revision.json 的入口即可）。
        # 但人工决策模式下 revision.json 已生成（/revise_subtitle 产物），所以两个分支都能跑。
        if bool(cfg.get("link_person_ids", True)):
            _log(job, "subtitle_review", "关联人员ID（rev_speaker_link）")
            r = _http_post(api, "/rev_speaker_link", {"task_id": tid},
                           auto_session_id=job.auto_session_id,
                           session_token=job.session_token)
            if not r.get("ok"):
                _log(job, "subtitle_review", f"rev_speaker_link 失败：{r.get('error')}", "warn")
            else:
                _log(job, "subtitle_review", "✅ 字幕修订关联人员ID完成", "ok")
        if not accept_all:
            _log(job, "subtitle_review", "⏸ 配置要求人工决策修订 — 不自动 save_revision，等人工去工作台处理", "info")
            # v3：accept_all_suggestions=False 时只跑 revise，不自动 save_revision。
            # 是否停在该阶段由 stage_cfg.stop_after 单独决定（调度器主循环判），
            # 这里不再 _request_stop。
            _result[0] = True
            _result[1] = "revision 待人工决策"
            return (True, _result[1])
        # 全接受：构造 save_revision payload 把所有 entry 决策为 accept
        rev_path = outputs_dir / "revision.json"
        if not rev_path.exists():
            _result[0] = False
            _result[1] = "修订产物 revision.json 不存在"
            return (False, _result[1])
        try:
            rev = json.loads(rev_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            _result[0] = False
            _result[1] = f"revision.json 解析失败：{e}"
            return (False, _result[1])
        entries = (rev or {}).get("entries") or []
        # skip_categories 处理：跳过该类别的建议 → 决策为 pending（留给人工）
        decisions = []
        for e in entries:
            cat = str(e.get("category") or "")
            if cat in skip_cats:
                decisions.append({"i": int(e.get("i", 0)), "decision": "pending", "user_note": ""})
            else:
                decisions.append({"i": int(e.get("i", 0)), "decision": "accept", "user_note": ""})
        _log(job, "subtitle_review", f"应用全部接受决策（{len(decisions)} 条）")
        r = _http_post(api, "/save_revision",
                       {"task_id": tid, "decisions": decisions},
                       auto_session_id=job.auto_session_id,
                       session_token=job.session_token)
        if not r.get("ok"):
            _result[0] = False
            _result[1] = f"save_revision 失败：{r.get('error')}"
            return (False, _result[1])
        # 补 extra 摘要
        try:
            patch_extra(outputs_dir, rev_id,
                        {"entries": len(entries), "decisions": len(decisions), "accept_all": accept_all})
        except Exception:
            pass
        _log(job, "subtitle_review", "✅ 字幕修订完成（全部接受）", "ok")
        _result[0] = True
        _result[1] = ""
        return (True, _result[1])
    except Exception as e:
        _result[0] = False
        _result[1] = f"异常：{e}"
        raise
    finally:
        record_finish(outputs_dir, rev_id, success=_result[0], error=_result[1])


def handler_rough_cut(tid: str, cfg: dict, outputs_dir: Path, api: str,
                      job: PipelineJob) -> tuple[bool, str]:
    """切分修剪阶段 — 生成切分清单 + (可选) 删说话人。

    prereq：revision.json 已保存（且 all_decided）。由 handler 顺序保证。
    """
    # REQ-20260918-053：执行日志 — 切分修剪（短任务，但逻辑步骤多，便于排查）
    from .execution_history import (
        KIND_ROUGH_CUT, record_start, record_finish, patch_extra,
    )
    rc_id = record_start(outputs_dir, KIND_ROUGH_CUT,
                         extra={"delete_speakers": list(cfg.get("delete_speakers") or [])},
                         auto=True, auto_session_id=job.auto_session_id)
    _result: list = [False, ""]
    try:
        ok, err = _check_prereq(outputs_dir, ["subtitle.json", "revision.json"])
        if not ok:
            _log(job, "rough_cut", f"跳过：{err}", "warn")
            _result[0] = True
            _result[1] = "skip"
            return (True, _result[1])
        # 默认决策：keep（与设计文档 §2.1 默认同源）
        default_decision = str(cfg.get("default_decision") or "keep")
        _log(job, "rough_cut", f"生成切分清单（默认决策={default_decision}）")
        r = _http_post(api, "/build_cutlist", {"task_id": tid},
                       auto_session_id=job.auto_session_id,
                       session_token=job.session_token)
        if not r.get("ok"):
            _result[0] = False
            _result[1] = f"build_cutlist 失败：{r.get('error')}"
            return (False, _result[1])
        # 若配置删除说话人 → 调 cut_speaker + 标记 spk 全部行 delete
        del_spks = list(cfg.get("delete_speakers") or [])
        if del_spks:
            _log(job, "rough_cut", f"删除说话人 {del_spks} 的全部记录")
            # link speakers（自动算 rows）
            r = _http_post(api, "/cut_speaker_link", {"task_id": tid},
                           auto_session_id=job.auto_session_id,
                           session_token=job.session_token)
            if not r.get("ok"):
                _log(job, "rough_cut", f"cut_speaker_link 失败：{r.get('error')}", "warn")
            # 标 actions：每个 spk → delete
            actions = {str(s): "delete" for s in del_spks if s}
            r = _http_post(api, "/save_cut_decisions",
                           {"task_id": tid, "actions": actions},
                           auto_session_id=job.auto_session_id,
                           session_token=job.session_token)
            if not r.get("ok"):
                _result[0] = False
                _result[1] = f"save_cut_decisions 失败：{r.get('error')}"
                return (False, _result[1])
            # save_cut_decisions 默认 actions 覆盖其它 spk → 用 manual_marks 补回：
            # 设计文档没要求补非目标 spk 的 keep，按 actions = del_spks→delete（其它维持 keep）
            # 该端点设计是 actions 仅覆盖所列 spk，未列出的保留 default
            try:
                patch_extra(outputs_dir, rc_id, {"del_speakers_count": len(del_spks)})
            except Exception:
                pass
        # REQ-20260921-NNN：自动关联人员ID（独立于 delete_speakers — 仅当 cfg.link_person_ids=True）
        elif bool(cfg.get("link_person_ids", True)):
            _log(job, "rough_cut", "关联人员ID（cut_speaker_link）")
            r = _http_post(api, "/cut_speaker_link", {"task_id": tid},
                           auto_session_id=job.auto_session_id,
                           session_token=job.session_token)
            if not r.get("ok"):
                _log(job, "rough_cut", f"cut_speaker_link 失败：{r.get('error')}", "warn")
            else:
                _log(job, "rough_cut", "✅ 切分修剪关联人员ID完成", "ok")
        _log(job, "rough_cut", "✅ 切分修剪完成", "ok")
        _result[0] = True
        _result[1] = ""
        return (True, _result[1])
    except Exception as e:
        _result[0] = False
        _result[1] = f"异常：{e}"
        raise
    finally:
        record_finish(outputs_dir, rc_id, success=_result[0], error=_result[1])


def handler_rough_compose(tid: str, cfg: dict, outputs_dir: Path, api: str,
                          job: PipelineJob) -> tuple[bool, str]:
    """粗剪合成阶段 — 调 /compose_rough + 等 /compose_rough_status（重编码，10+ 分钟）。

    注意：执行日志由 compose_service.start_compose 自行落盘（REQ-20260918-048 既有）；
    这里只做编排，避免双记录。
    """
    _result: list = [False, ""]
    try:
        ok, err = _check_prereq(outputs_dir, ["subtitle.json", "revision.json"])
        if not ok:
            _log(job, "rough_compose", f"跳过：{err}", "warn")
            _result[0] = True
            _result[1] = "skip"
            return (True, _result[1])
        _log(job, "rough_compose", "启动粗剪合成")
        r = _http_post(api, "/compose_rough", {"task_id": tid},
                       auto_session_id=job.auto_session_id,
                       session_token=job.session_token)
        if not r.get("ok"):
            _result[0] = False
            _result[1] = f"启动合成失败：{r.get('error')}"
            return (False, _result[1])
        _log(job, "rough_compose", "等待合成完成（重编码约 10+ 分钟）")
        state, err = _poll_status(api, "/compose_rough_status", tid, timeout=6 * 3600.0, session_token=job.session_token)
        if state == "done":
            _log(job, "rough_compose", "✅ 粗剪合成完成", "ok")
            _result[0] = True
            _result[1] = ""
            return (True, _result[1])
        _result[0] = False
        _result[1] = f"粗剪合成{state}：{err}"
        return (False, _result[1])
    except Exception as e:
        _result[0] = False
        _result[1] = f"异常：{e}"
        raise


def handler_optimize(tid: str, cfg: dict, outputs_dir: Path, api: str,
                     job: PipelineJob) -> tuple[bool, str]:
    """优化字幕阶段 — /optimize_subtitle + 等完成 + (可选) 全接受（LLM 调用，分钟级）。

    注意：执行日志由 optimize_service.start_job 自行落盘（REQ-20260918-053 接入）；
    这里只做编排，避免双记录。
    """
    _result: list = [False, ""]
    try:
        # prereq：rough_compose.mp4
        ok, err = _check_prereq(outputs_dir, ["rough_compose.mp4"])
        if not ok:
            _log(job, "optimize", f"跳过：{err}", "warn")
            _result[0] = True
            _result[1] = "skip"
            return (True, _result[1])
        accept_all = bool(cfg.get("accept_all_replacements", True))
        _log(job, "optimize", "启动优化字幕分析")
        r = _http_post(api, "/optimize_subtitle",
                       {"task_id": tid, "force": True},
                       auto_session_id=job.auto_session_id,
                       session_token=job.session_token)
        if not r.get("ok"):
            _result[0] = False
            _result[1] = f"启动优化失败：{r.get('error')}"
            return (False, _result[1])
        _log(job, "optimize", "等待分析完成…")
        state, err = _poll_status(api, "/optimize_subtitle_status", tid, timeout=6 * 3600.0, session_token=job.session_token)
        if state != "done":
            _result[0] = False
            _result[1] = f"优化字幕{state}：{err}"
            return (False, _result[1])
        if accept_all:
            # REQ-20260921-NNN-v3：构造全量 decisions（{occ_id, applied, after, reviewed}），
            # 而不是发 applied_all=True 标志——save_optimize_subtitle 端点不识别那个标志，
            # 硬约束是 decisions 必须是数组。
            from . import optimize_service
            opt_data = optimize_service.load_optimize(outputs_dir)
            if not opt_data:
                _result[0] = False
                _result[1] = "optimize_subtitle.json 不存在 — 无法构造 decisions"
                return (False, _result[1])
            decisions = []
            for occ in (opt_data.get("occurrences") or []):
                try:
                    oid = int(occ.get("occ_id"))
                except (TypeError, ValueError):
                    continue
                # 「全部接受」= applied=True + after 用大模型给的建议值 + reviewed=True
                decisions.append({
                    "occ_id": oid,
                    "applied": True,
                    "after": str(occ.get("after") or ""),
                    "reviewed": True,
                })
            _log(job, "optimize",
                 f"应用全部替换（{len(decisions)} 条 decisions，applied=True）")
            r = _http_post(api, "/save_optimize_subtitle",
                           {"task_id": tid, "decisions": decisions},
                           auto_session_id=job.auto_session_id,
                           session_token=job.session_token)
            if not r.get("ok"):
                _result[0] = False
                _result[1] = f"save_optimize_subtitle 失败：{r.get('error')}"
                return (False, _result[1])
            # REQ-20260922-NNN 标记删除行（手动触发语义）：保存不触发剪辑 — 仅当
            # 「🎬 重新优化粗剪视频」触发的剪辑恰在跑时等它完成再进精剪；失败只
            # warn（精剪自动回退粗剪成片，阶段不算失败）
            cut = (r.get("cut") or {})
            if cut.get("state") in ("started", "running"):
                _log(job, "optimize",
                     f"标记删除 {len(cut.get('marks') or [])} 行 → 优化成片剪辑中…")
                st, cerr = _poll_status(api, "/optimize_cut_status", tid, timeout=3600.0, session_token=job.session_token)
                if st != "done":
                    _log(job, "optimize",
                         f"优化成片剪辑{st}：{cerr}（精剪合成将回退使用粗剪成片）", "warn")
        _log(job, "optimize", "✅ 优化字幕完成", "ok")
        _result[0] = True
        _result[1] = ""
        return (True, _result[1])
    except Exception as e:
        _result[0] = False
        _result[1] = f"异常：{e}"
        raise


def _poll_export(api: str, job_id: str, tid: str, *,
                 timeout: float = 6 * 3600.0,
                 session_token: str = "") -> tuple[str, str]:
    """轮询 /render_status（GET 端点，按 job_id）。

    REQ-20260921-NNN：/render_status 是 GET 端点，按 job_id 查询；与 _poll_status
    按 task_id 不同。返回 (state, error) — state ∈ {done, failed, stopped, timeout}。
    """
    from urllib.parse import urlencode
    url = api.rstrip("/") + "/render_status?" + urlencode({"job_id": job_id})
    t0 = time.time()
    last_err = ""
    while True:
        if _consume_stop(tid + ":poller"):
            return ("stopped", "用户请求停止")
        try:
            headers = {}
            if session_token:
                headers["Cookie"] = f"slirn_session={session_token}"
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=15.0) as r:
                raw = r.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                # /render_status 端点 _ok 包一层；job 字段平铺在 data
                st = str((data or {}).get("state") or "")
                if st == "done":
                    return ("done", "")
                if st in ("failed", "error"):
                    return (st, str((data or {}).get("error") or "失败"))
                last_err = str((data or {}).get("error") or "")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return ("error", ("内部调用被拒绝(401)：会话已过期，"
                                  "请重新登录后再跑流程"))
            last_err = f"HTTP {e.code}: {e.reason}"
        except Exception as e:  # noqa: BLE001
            last_err = f"轮询异常: {e}"
        if time.time() - t0 > timeout:
            return ("timeout", f"轮询超时（>{int(timeout)}s）：{last_err}")
        time.sleep(5.0)


def handler_fine_cut(tid: str, cfg: dict, outputs_dir: Path, api: str,
                     job: PipelineJob) -> tuple[bool, str]:
    """精剪合成阶段 — 调 /export_fine_video + 等 /render_status。

    REQ-20260921-NNN：可选 enabled=False 时跳过（默认 False，避免误触发
    几小时重编码）。启用时按 cfg.preview_start / cfg.duration 决定
    「全片 vs 区间」导出。参数模板（params_source）应用逻辑：若
    template:<id> → 先调 /apply_fine_global_profile 应用模板到 fc.json，
    再 export_fine_video。

    prereq：rough_compose.mp4 存在（精剪是基于粗剪成片重编码）。若不存在 → skip。
    """
    _result: list = [False, ""]
    try:
        # REQ-20260921-NNN-range-overrides-enabled：用户表达「按区间导出」意图
        # 时（range_enabled=True），即便 enabled 默认关也要执行 — 用户主动配
        # 起点 / 时长 = 明确要导一区段，不能再让 enabled 漏勾卡住。「用户意图」
        # 优先于「防误跑」开关。设计要求：勾 range 就该能跑（用户只看不跑全片
        # 就要看一段的反馈）。
        fc_enabled = bool(cfg.get("enabled", False))
        fc_range = bool(cfg.get("range_enabled", False))
        if not fc_enabled and not fc_range:
            # REQ-20260921-NNN-skip-warn：升级 log 等级为 warn，让 UI 顶部 status
            # 能区分「真跑通」vs「跳过」（之前 info 级 + 最终 ✅ 已完成 误显示
            # 为绿色对勾，用户以为精剪合成跑完了，实际 cfg.enabled=False 没产出
            # fine_export.mp4）。
            _log(job, "fine_cut", "⚠️ 未启用自动最终导出（enabled=False, range_enabled=False），跳过 — 不会产出 fine_export.mp4", "warn")
            _result[0] = True
            _result[1] = "skip"
            return (True, _result[1])
        # prereq 检查：粗剪成片（精剪的输入）
        ok, err = _check_prereq(outputs_dir, ["rough_compose.mp4"])
        if not ok:
            _log(job, "fine_cut", f"跳过：{err}", "warn")
            _result[0] = True
            _result[1] = "skip"
            return (True, _result[1])
        # 应用参数模板（若选了某 profile）
        params_source = str(cfg.get("params_source") or "current")
        if params_source.startswith("template:"):
            profile_id = params_source.split(":", 1)[1].strip()
            if profile_id:
                _log(job, "fine_cut", f"应用精剪模板 {profile_id}")
                r = _http_post(api, "/apply_fine_global_profile",
                               {"task_id": tid, "profile_id": profile_id},
                               auto_session_id=job.auto_session_id,
                               session_token=job.session_token)
                if not r.get("ok"):
                    _result[0] = False
                    _result[1] = f"应用模板失败：{r.get('error')}"
                    return (False, _result[1])
        # 触发导出
        body: dict = {"task_id": tid}
        # REQ-20260921-NNN-v4：range_enabled 门控——
        # 不勾 = 全片（不传 preview_start/duration，让端点走默认全片导出）；
        # 勾上 = 按 cfg.preview_start / cfg.duration 传（前端 HH:MM:SS 已转秒）。
        range_enabled = bool(cfg.get("range_enabled", False))
        if range_enabled:
            ps = cfg.get("preview_start")
            dur = cfg.get("duration")
            try:
                if ps not in (None, "", 0):
                    body["preview_start"] = float(ps)
                if dur not in (None, ""):
                    body["duration"] = float(dur)
            except (TypeError, ValueError) as e:
                _result[0] = False
                _result[1] = f"preview_start/duration 数值非法: {e}"
                return (False, _result[1])
        range_hint = ""
        if body.get("preview_start") or body.get("duration") is not None:
            range_hint = f"（区间 {body.get('preview_start', 0)}s 起, {body.get('duration', '全篇')}）"
        _log(job, "fine_cut",
             f"启动最终导出{range_hint}" + ("" if range_enabled else "（全片）"))
        r = _http_post(api, "/export_fine_video", body,
                       auto_session_id=job.auto_session_id,
                       session_token=job.session_token)
        if not r.get("ok"):
            _result[0] = False
            _result[1] = f"启动导出失败：{r.get('error')}"
            return (False, _result[1])
        job_id = r.get("job_id")
        if not job_id:
            _result[0] = False
            _result[1] = "导出端点未返回 job_id"
            return (False, _result[1])
        _log(job, "fine_cut", "等待导出完成（1-3 小时重编码）")
        state, err = _poll_export(api, job_id, tid, timeout=6 * 3600.0, session_token=job.session_token)
        if state == "done":
            _log(job, "fine_cut", "✅ 精剪合成完成（fine_export.mp4）", "ok")
            _result[0] = True
            _result[1] = ""
            return (True, _result[1])
        _result[0] = False
        _result[1] = f"精剪合成{state}：{err}"
        return (False, _result[1])
    except Exception as e:
        _result[0] = False
        _result[1] = f"异常：{e}"
        raise


HANDLERS: dict[str, Callable] = {
    "subtitle_generation": handler_subtitle_generation,
    "subtitle_review": handler_subtitle_review,
    "rough_cut": handler_rough_cut,
    "rough_compose": handler_rough_compose,
    "optimize": handler_optimize,
    "fine_cut": handler_fine_cut,
}


# =============== 主入口 ===============


def run_pipeline(tid: str, api: str, outputs_dir: Path, *,
                 since: str | None = None, session_token: str = "") -> bool:
    """启动后台守护线程跑流程；已有 running job → False。

    api = Gradio FastAPI 应用的 base URL（通常是 `http://127.0.0.1:<port>`）。

    REQ-20260920-081：启动时生成 auto_session_id（uuid4.hex[:12]），写到 job 上，
    handler 调 _http_post 时透传给后端 endpoint → 写入 execution_history 的
    auto_session_id 字段 → 前端按 session_id 聚合显示「同一次自动流」。

    REQ-20261021-139 / D4：session_token = 发起者的 slirn_session cookie 值。
    调用方（端点）必须在启动线程**之前**取到（contextvars 不跨线程），
    内部 HTTP 调用会带 Cookie: slirn_session=… 过鉴权中间件。
    """
    if _is_running(tid):
        return False
    _clear_stop(tid)
    # REQ-20260920-081：本次自动流唯一 ID；12 字符 / 48bit 足够唯一
    auto_session_id = uuid.uuid4().hex[:12]
    job = PipelineJob(
        state="running",
        started_at=time.time(),
        current_stage=None,
        percent=0.0,
        auto_session_id=auto_session_id,
        # REQ-20261021-139 / D4：发起者会话 token —— 内部调用要靠它过鉴权中间件
        session_token=session_token or "",
    )
    _set_job(tid, job)

    def _run() -> None:
        try:
            cfg_path_data = load_pipeline(outputs_dir) or {}
            cfg_full = cfg_path_data.get("config") or default_config()
            # v5 顶层：run_mode + stop_after（validate_config 已保证 to_end → stop_after=None）
            run_mode = cfg_full.get("run_mode") or "stop_after"
            flow_stop = cfg_full.get("stop_after")
            # REQ-20260921-NNN：起始日志 — 显式标出运行模式 + 停点
            mode_desc = (
                "一键跑到底" if run_mode == "to_end"
                else f"停在「{flow_stop or '不限'}」后"
            )
            _log(job, "_start", f"运行模式：{mode_desc}（共 {len(STAGE_ORDER)} 阶段）")
            # since 解析：None 表示从头跑；否则「从 since 这一阶段开始」跳过更早的阶段
            since_idx = STAGE_INDEX.get(since, -1) if since else -1
            stages_done: list[str] = []
            # REQ-20260921-NNN-skip-warn：单独追踪跳过的 stage，让 UI 能区分
            # 「真跑通」vs「被跳过」（之前 stages_done 不含 skip，结果 UI 把
            # fine_cut.cfg.enabled=False 跳过误显示为 ✅ 完成）。skip 不算
            # 「done」，但要暴露出来给前端渲染徽章用。
            stages_skipped: list[str] = []
            for stage_key, stage_label, _pane in STAGE_ORDER:
                idx = STAGE_INDEX[stage_key]
                if idx < since_idx:
                    continue
                # 用户中途 stop → 立即退出
                if _consume_stop(tid):
                    _log(job, stage_key, "⏹ 用户请求停止", "warn")
                    job.state = "stopped"
                    break
                job.current_stage = stage_key
                # per-stage percent: 6 阶段均匀 → 100/6 ≈ 16.7 每阶段；留 20% 给收尾
                job.percent = (idx + 1) / max(len(STAGE_ORDER), 1) * 80
                _log(job, stage_key, f"== 开始阶段：{stage_label} ==")
                stage_cfg = cfg_full.get(stage_key) or {}
                handler = HANDLERS.get(stage_key)
                if handler is None:
                    _log(job, stage_key, f"未知阶段 {stage_key}，跳过", "warn")
                    continue
                try:
                    ok, msg = handler(tid, stage_cfg, outputs_dir, api, job)
                except Exception as e:  # noqa: BLE001
                    log.exception("[pipeline][%s] handler %s 抛异常", tid, stage_key)
                    job.state = "error"
                    job.error = f"{stage_label}：{e}"
                    _log(job, stage_key, f"❌ 异常：{e}", "error")
                    break
                if not ok:
                    job.state = "error"
                    job.error = msg
                    _log(job, stage_key, f"❌ {msg}", "error")
                    break
                if msg == "skip":
                    # REQ-20260921-NNN-skip-warn：单独记录跳过的 stage，让前端
                    # 能区分「真跑通（✓ 绿）」vs「跳过（⚠️ 黄）」徽章
                    stages_skipped.append(stage_key)
                else:
                    stages_done.append(stage_key)
                # v5 stop_after：run_mode=to_end 时 flow_stop 已被 validate_config 重置为 None；
                # run_mode=stop_after 时按用户选的 stage_key 决定停点
                if flow_stop and stage_key == flow_stop:
                    _log(job, stage_key,
                         f"⏸ 顶层配置要求「{stage_label}」后停（等人工）", "info")
                    job.state = "stopped"
                    break
            else:
                # 全部阶段正常 → done（但区分全跑通 vs 含跳过）
                job.state = "done"
                if stages_skipped:
                    # REQ-20260921-NNN-skip-warn：含跳过的完成用 warn 级 + 明确
                    # 计数，让用户在 status 顶部一眼看到「不是真跑通」
                    skip_list = "、".join(stages_skipped)
                    _log(job, "_end",
                         f"⚠️ 全部阶段完成（含 {len(stages_skipped)} 个跳过：{skip_list}）",
                         "warn")
                else:
                    _log(job, "_end", "✅ 全部阶段完成", "ok")
            job.finished_at = time.time()
            job.percent = 100.0 if job.state == "done" else job.percent
            # summary + append history
            summary = {
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "duration_ms": int((job.finished_at - (job.started_at or job.finished_at)) * 1000),
                "status": job.state,
                "stages_done": stages_done,
                # REQ-20260921-NNN-skip-warn：把跳过列表写进 summary，前端能读到
                "stages_skipped": stages_skipped,
                "since": since,
                "error": job.error,
            }
            job.summary = summary
            append_history(outputs_dir, summary)
        except Exception as e:  # noqa: BLE001 — 后台线程兜底
            log.exception("[pipeline][%s] 调度器异常", tid)
            job.state = "error"
            job.error = str(e)
            job.finished_at = time.time()

    threading.Thread(target=_run, name=f"pipeline-{tid}", daemon=True).start()
    return True


def stop_pipeline(tid: str) -> bool:
    """请求停止（设置标志位，下次循环检查时退出）。无 job → False。"""
    if not _is_running(tid):
        return False
    _request_stop(tid)
    return True


def clear_pipeline_state(tid: str, *, history_path: Path | None = None) -> dict:
    """REQ-20260921-NNN：清理任务的 in-memory pipeline 状态（不删磁盘产物）。

    - 清 _PIPELINE_JOBS[tid]（让前端 /status 返回 None）
    - 清 _STOP_FLAGS[tid]（防止 reset 后用户再 stop 时无效）
    - 删磁盘 history（execution_history.json），下次 load_pipeline_state 重读为 0

    返回 {cleared_jobs, cleared_stop, cleared_history}。
    实际磁盘产物删除由 app.py 的 /pipeline_reset_stages 端点做。

    参数 history_path：可选 — 指定 execution_history.json 路径（精确）；不传就尝试 2 个候选路径。
    """
    cleared_jobs = 0
    cleared_stop = False
    cleared_history = False
    with _JOBS_LOCK:
        if tid in _PIPELINE_JOBS:
            try:
                del _PIPELINE_JOBS[tid]
            except Exception:
                pass
            cleared_jobs = 1
    with _STOP_LOCK:
        if tid in _STOP_FLAGS:
            try:
                del _STOP_FLAGS[tid]
            except Exception:
                pass
            cleared_stop = True
    # 删磁盘 history
    try:
        from slirn_home.execution_history import HISTORY_FILENAME
        candidates: list[Path] = []
        if history_path is not None:
            candidates.append(history_path)
        # 兜底：2 个候选路径（mgr.tasks_dir 是 slirn_home/tasks 或 tasks）
        candidates += [
            Path("slirn_home/tasks") / tid / "outputs" / HISTORY_FILENAME,
            Path("tasks") / tid / "outputs" / HISTORY_FILENAME,
        ]
        for c in candidates:
            if c.exists():
                try:
                    c.unlink()
                    cleared_history = True
                except Exception:
                    pass
                break
    except Exception:
        pass
    return {"cleared_jobs": cleared_jobs,
            "cleared_stop": cleared_stop,
            "cleared_history": cleared_history}
