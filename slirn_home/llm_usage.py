# -*- coding: utf-8 -*-
"""LLM Token 用量记账 — 全工程大模型调用统一在这里累计（输入/输出分开）。

数据源：API 响应体自带的 usage 字段（OpenAI: prompt_tokens/completion_tokens；
Anthropic: input_tokens/output_tokens），由 revision_service 的两个调用口
（_chat_completion / _call_llm_vision）在解析响应时接住并 record —
**精确值，非估算**；个别网关不回 usage 时按字符数粗估并打 estimated 标记。

维度：
- models  按模型累计（全局）
- tasks   按任务累计（ctx 带 task_id 时），任务下再分 stage（字幕修订/优化/短视频…）
- total   全局总计

持久化 data/llm_usage.json（原子写：.part → replace），服务重启不丢。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_CACHE: dict | None = None          # 进程内存缓存（与磁盘同源，读时懒加载）
_CACHE_AT: Path | None = None       # 缓存对应的账本路径（避免测试间串台）


def _ledger_path() -> Path:
    from slirn_home.paths import find_slirn_standalone_root
    return Path(find_slirn_standalone_root()) / "data" / "llm_usage.json"


def _empty() -> dict:
    return {"models": {}, "tasks": {}, "total": _zero(), "updated_at": ""}


def _zero() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "estimated_calls": 0}


def _add(bucket: dict, prompt: int, completion: int, estimated: bool) -> None:
    bucket["prompt_tokens"] += max(0, int(prompt))
    bucket["completion_tokens"] += max(0, int(completion))
    bucket["calls"] += 1
    if estimated:
        bucket["estimated_calls"] = bucket.get("estimated_calls", 0) + 1


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "total" in data:
            return data
    except Exception:  # noqa: BLE001 — 损坏账本从零重来，别影响主流程
        pass
    return _empty()


def estimate_tokens(text: str) -> int:
    """网关未回 usage 时的粗估：中文约 1.5 字符/token、英文约 4 字符/token，
    混合取 2 字符/token（偏差 ±30%，仅兜底用，会打 estimated 标记）。"""
    return (len(text or "") + 1) // 2


def extract_usage_openai(data) -> tuple[int, int]:
    """OpenAI 兼容响应 → (输入, 输出) tokens；无 usage 返回 (0, 0)。"""
    u = (data or {}).get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return 0, 0
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def extract_usage_anthropic(data) -> tuple[int, int]:
    """Anthropic 协议响应 → (输入, 输出) tokens；无 usage 返回 (0, 0)。"""
    u = (data or {}).get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        return 0, 0
    return int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)


def record(model: str, prompt_tokens: int, completion_tokens: int,
           ctx: dict | None = None, *, estimated: bool = False,
           elapsed_ms: int = 0) -> None:
    """记一笔调用。

    ctx：调用上下文 {"task_id": ..., "stage": ...} — 带 task_id 时该笔同时
    计入任务维度；不带只进全局。线程安全；磁盘写失败只记日志不抛
    （用量统计绝不能影响主流程）。

    除累计账本外，同笔追加一行到 data/llm_usage_log.jsonl（逐笔明细：
    时间/模型/任务/阶段/输入/输出/耗时），超限轮转保留最近 _MAX_LOG 条。
    """
    global _CACHE, _CACHE_AT
    try:
        with _LOCK:
            path = _ledger_path()
            if _CACHE is None or _CACHE_AT != path:
                _CACHE = _load(path)
                _CACHE_AT = path
            led = _CACHE
            _add(led["total"], prompt_tokens, completion_tokens, estimated)
            m = led["models"].setdefault(model or "未知模型", _zero())
            _add(m, prompt_tokens, completion_tokens, estimated)
            tid = (ctx or {}).get("task_id") or ""
            if tid:
                task = led["tasks"].setdefault(str(tid), _zero())
                task.setdefault("stages", {})
                _add(task, prompt_tokens, completion_tokens, estimated)
                stage = led["tasks"][tid]["stages"].setdefault(
                    str((ctx or {}).get("stage") or "其他"), _zero())
                _add(stage, prompt_tokens, completion_tokens, estimated)
            led["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            part = path.with_suffix(".json.part")
            path.parent.mkdir(parents=True, exist_ok=True)
            part.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
            part.replace(path)
            _append_log(path.parent / "llm_usage_log.jsonl", {
                "ts": led["updated_at"],
                "model": model or "未知模型",
                "task_id": (ctx or {}).get("task_id") or "",
                "stage": (ctx or {}).get("stage") or "其他",
                "prompt_tokens": max(0, int(prompt_tokens)),
                "completion_tokens": max(0, int(completion_tokens)),
                "estimated": bool(estimated),
                "elapsed_ms": int(elapsed_ms or 0),
            })
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("[llm_usage] 记账失败（不影响调用）: %s", e)


# ---------- 逐笔明细（llm_usage_log.jsonl） ----------

_MAX_LOG_LINES = 5000


def _append_log(path: Path, rec: dict) -> None:
    """追加一行 JSONL；超过 _MAX_LOG_LINES 行时裁掉最旧的一半。"""
    if path.exists() and path.stat().st_size > 64:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if sum(1 for _ in path.open("rb")) > _MAX_LOG_LINES:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            keep = lines[len(lines) // 2:]
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            tmp.replace(path)
    else:
        path.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")


def _norm_time(v: str, is_from: bool) -> str:
    """把用户输入的时间规整成与 rec.ts 同构的 'YYYY-MM-DDTHH:MM:SS'（可比较）。

    接受 YYYY-MM-DD（日起止自动补 00:00:00 / 23:59:59）、YYYY-MM-DD HH:MM(:SS)、
    或带 T 的完整形式；空返回空。
    """
    v = str(v or "").strip().replace(" ", "T")
    if not v:
        return ""
    if len(v) == 10:
        v += "T00:00:00" if is_from else "T23:59:59"
    elif len(v) == 16:
        v += ":00"
    return v


def list_records(task_id: str | None = None, model: str = "", stage: str = "",
                 time_from: str = "", time_to: str = "",
                 dur_min_ms: int | None = None, dur_max_ms: int | None = None,
                 limit: int = 200, offset: int = 0) -> dict:
    """读逐笔明细（倒序 = 最新在前），支持组合过滤 + 分页。

    过滤：task_id / model（子串）/ stage（精确）/ 时间段（含起止日）/ 耗时区间
    （elapsed_ms 上下限，闭区间）。另返回 facets（日志中出现过的模型与阶段
    全集）供前端下拉框用。
    """
    path = _ledger_path().parent / "llm_usage_log.jsonl"
    tf = _norm_time(time_from, True)
    tt = _norm_time(time_to, False)
    out: list[dict] = []
    models_seen: set[str] = set()
    stages_seen: set[str] = set()
    if path.exists():
        for line in reversed(path.read_text(encoding="utf-8", errors="replace")
                             .splitlines()):  # 新→旧
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001 — 脏行跳过
                continue
            models_seen.add(str(rec.get("model") or ""))
            stages_seen.add(str(rec.get("stage") or ""))
            if task_id and rec.get("task_id") != task_id:
                continue
            if model and model not in str(rec.get("model") or ""):
                continue
            if stage and rec.get("stage") != stage:
                continue
            ts = str(rec.get("ts") or "")
            if tf and ts < tf:
                continue
            if tt and ts > tt:
                continue
            ms = int(rec.get("elapsed_ms") or 0)
            if dur_min_ms is not None and ms < dur_min_ms:
                continue
            if dur_max_ms is not None and ms > dur_max_ms:
                continue
            out.append(rec)
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    total = len(out)
    return {"records": out[offset:offset + limit],
            "total": total, "offset": offset, "limit": limit,
            "page": offset // limit + 1,
            "pages": max(1, (total + limit - 1) // limit),
            "facets": {"models": sorted(m for m in models_seen if m),
                       "stages": sorted(s for s in stages_seen if s)}}


def summary(task_id: str | None = None) -> dict:
    """读取账本。task_id 传入 → 只返回该任务；None → 全局（含 models/tasks）。"""
    global _CACHE, _CACHE_AT
    with _LOCK:
        path = _ledger_path()
        if _CACHE is None or _CACHE_AT != path:
            _CACHE = _load(path)
            _CACHE_AT = path
        led = _CACHE
    if task_id:
        t = led["tasks"].get(task_id)
        return {"task_id": task_id, "task": t} if t else {"task_id": task_id, "task": None}
    return led
