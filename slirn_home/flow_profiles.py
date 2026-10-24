"""流程配置·全局模板（REQ-20260930-092：红框内全部参数存为可复用模板）。

用户把流程配置面板的整套配置（六个阶段的每个复选框选中/不选中、单选卡片、
输入框 + 顶层运行模式/停止阶段）保存为命名模板，在任何任务一键套用。

存储：``<repo_root>/tasks/_global_flow_profiles.json`` —
与 fine_profiles（_global_fine_profiles.json）同级同模式：
独立 JSON + atomic write + 损坏回退空注册表。

数据形态：

    {
      "_schema": 1,
      "profiles": [
        {"id": "fp_20260930_xxx", "name": "课程全自动",
         "saved_at": "2026-09-30T15:30:00", "task_id_origin": "20260924-001",
         "config": {"subtitle_generation": {...}, ..., "run_mode": ..., "stop_after": ...}},
        ...
      ],
      "updated_at": "..."
    }

config 存 pipeline_service.validate_config 规范化后的形态（与任务
pipeline.json 的 config 完全同构 — 应用模板 = 直接喂给 renderPanel）。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Iterable

from slirn_home.pipeline_service import validate_config as _validate_config

log = logging.getLogger(__name__)

GLOBAL_FLOW_PROFILES_REL = "tasks/_global_flow_profiles.json"


def _path(repo_root: Path | str) -> Path:
    return Path(repo_root) / GLOBAL_FLOW_PROFILES_REL


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _new_id() -> str:
    # 时间前缀 + 随机后缀 — 易读且唯一；fp_ 前缀与 fine_profiles 的 p_ 区分
    return "fp_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)


def _load(repo_root: Path | str) -> dict:
    """读全局模板 JSON；文件缺失/损坏 → 回退空注册表（不抛，同 fine_profiles）。"""
    p = _path(repo_root)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("profiles"), list):
                return data
            log.warning("flow_profiles 全局文件格式异常，回退空注册表")
        except Exception as e:  # noqa: BLE001
            log.warning("flow_profiles 全局文件损坏（%s），回退空注册表", e)
    return {"_schema": 1, "profiles": [], "updated_at": ""}


def _save(repo_root: Path | str, data: dict) -> None:
    """atomic write：先写 .tmp 再 os.replace（同 fine_profiles）。"""
    p = _path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now_iso()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _unique_name(existing: Iterable[dict], base: str) -> str:
    """重名时追加 `(2)` / `(3)` / ... 直到唯一。空名兜底为「未命名」。"""
    name = (base or "").strip() or "未命名"
    taken = {p.get("name") for p in existing}
    if name not in taken:
        return name
    n = 2
    while True:
        cand = f"{name} ({n})"
        if cand not in taken:
            return cand
        n += 1


def _sanitize_config(cfg: dict) -> dict:
    """规范化为合法 v5 schema（丢死字段/兜底脏值/丢阶段内 stop_after）。

    与任务 pipeline.json 的 config 同构 — 应用端直接 renderPanel 即可。
    显式 False 的复选框值原样保留（validate_config 按用户值合并）。
    """
    return _validate_config(cfg if isinstance(cfg, dict) else {})


def list_profiles(repo_root: Path | str) -> list[dict]:
    """返回所有模板（按 saved_at 倒序），含完整 config。"""
    data = _load(repo_root)
    return sorted(
        list(data.get("profiles") or []),
        key=lambda p: p.get("saved_at") or "",
        reverse=True,
    )


def get_profile(repo_root: Path | str, profile_id: str) -> dict | None:
    """按 ID 查找单个模板。"""
    for p in list_profiles(repo_root):
        if p.get("id") == profile_id:
            return p
    return None


def save_profile(
    repo_root: Path | str,
    name: str,
    config: dict,
    task_id_origin: str = "",
) -> dict:
    """把流程配置保存为新模板（规范化 config）。重名时自动加 `(2)`。

    返回新建的 profile dict（含 id / name / saved_at / config）。
    """
    data = _load(repo_root)
    profiles = list(data.get("profiles") or [])
    final_name = _unique_name(profiles, name)
    profile = {
        "id": _new_id(),
        "name": final_name,
        "saved_at": _now_iso(),
        "task_id_origin": task_id_origin,
        "config": _sanitize_config(config),
    }
    profiles.append(profile)
    data["profiles"] = profiles
    _save(repo_root, data)
    return profile


def delete_profile(repo_root: Path | str, profile_id: str) -> bool:
    """按 ID 删除模板；返回是否真删了一条。"""
    data = _load(repo_root)
    profiles = list(data.get("profiles") or [])
    new_profiles = [p for p in profiles if p.get("id") != profile_id]
    if len(new_profiles) == len(profiles):
        return False
    data["profiles"] = new_profiles
    _save(repo_root, data)
    return True
