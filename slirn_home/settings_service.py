# -*- coding: utf-8 -*-
"""系统设置 — 持久化在 config/settings.json（机器本地，不入库）。

当前条目：
- llm_workers：大模型并发 worker 数（字幕修订等分批调用的并行度）。
  默认 4；范围 1..8。改动即时生效（下一次 start_job 读取），无需重启。
- context_cache：上下文窗口探测缓存 {模型id: {"tokens", "at"}}（独立命名空间，
  get_settings 不透出）。revision_service.pack_batches 按它决定每批装多少行。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from slirn_home.paths import find_slirn_standalone_root

DEFAULTS = {
    "llm_workers": 4,
}
MIN_WORKERS, MAX_WORKERS = 1, 8
CTX_MIN, CTX_MAX = 4096, 2_000_000  # 探测缓存合理区间（超出视为脏数据）

_LOCK = threading.Lock()


def _path(repo_root: Path | str | None = None) -> Path:
    root = Path(repo_root) if repo_root else Path(find_slirn_standalone_root())
    return root / "config" / "settings.json"


def get_settings(repo_root: Path | str | None = None) -> dict:
    """读设置（文件缺失/损坏 → 全默认）。"""
    out = dict(DEFAULTS)
    try:
        data = json.loads(_path(repo_root).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:  # noqa: BLE001
        pass
    return out


def get_workers(repo_root: Path | str | None = None) -> int:
    """生效的并发数（钳到合法区间，任何异常回退默认）。"""
    try:
        w = int(get_settings(repo_root).get("llm_workers", DEFAULTS["llm_workers"]))
    except (TypeError, ValueError):
        w = DEFAULTS["llm_workers"]
    return max(MIN_WORKERS, min(MAX_WORKERS, w))


def update_settings(repo_root: Path | str | None, patch: dict) -> dict:
    """合并写入并返回最新设置。非法值 → ValueError（端点转 _err）。

    基于磁盘原文合并（保留 context_cache 等非 DEFAULTS 键），返回值只含
    DEFAULTS 键（对外语义不变）。
    """
    with _LOCK:
        p = _path(repo_root)
        raw = _read_raw(repo_root)
        cur = {**DEFAULTS, **{k: raw.get(k) for k in DEFAULTS if k in raw}}
        for k, v in (patch or {}).items():
            if k not in DEFAULTS:
                continue
            if k == "llm_workers":
                if not isinstance(v, int) or not (MIN_WORKERS <= v <= MAX_WORKERS):
                    raise ValueError(f"并发 worker 数必须是 {MIN_WORKERS}-{MAX_WORKERS} 的整数")
                cur[k] = v
        raw.update(cur)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        return cur


# ---------- 上下文窗口探测缓存（context_cache 命名空间） ----------

def _read_raw(repo_root: Path | str | None = None) -> dict:
    try:
        data = json.loads(_path(repo_root).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001 — 文件缺失/损坏 → 空
        pass
    return {}


def _write_raw(repo_root: Path | str | None, data: dict) -> None:
    with _LOCK:
        p = _path(repo_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)


def get_context(repo_root: Path | str | None = None, entry_id: str = "") -> dict | None:
    """某模型的探测缓存 {"tokens", "at"}；没有/非法 → None。"""
    hit = (_read_raw(repo_root).get("context_cache") or {}).get(str(entry_id or ""))
    if isinstance(hit, dict):
        try:
            if CTX_MIN <= int(hit.get("tokens") or 0) <= CTX_MAX:
                return {"tokens": int(hit["tokens"]),
                        "at": str(hit.get("at") or "")}
        except (TypeError, ValueError):
            pass
    return None


def set_context(repo_root: Path | str | None, entry_id: str = "",
                tokens: int = 0) -> dict:
    """写入某模型的探测结果（钳到合理区间），返回写入的条目。"""
    t = max(CTX_MIN, min(CTX_MAX, int(tokens)))
    rec = {"tokens": t, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    data = _read_raw(repo_root)
    data.setdefault("context_cache", {})[str(entry_id or "")] = rec
    _write_raw(repo_root, data)
    return rec
