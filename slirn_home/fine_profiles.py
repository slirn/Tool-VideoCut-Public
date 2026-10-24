"""精剪视频·全局参数模板（REQ-20260919-061 扩展）。

用户把当前任务的「layout / font / output / audio / detected_region / preview」
六组参数保存为命名模板，可在其他任务一键应用（弹窗确认后覆盖，六组全量回填）。
素材（materials）不进模板。

存储：``<repo_root>/tasks/_global_fine_profiles.json`` —
与 llm_config 同级（独立 JSON 文件 + atomic write + 损坏回退默认），
与任务目录同级便于用户在文件管理器里直接看到。

数据形态：

    {
      "_schema": 1,
      "profiles": [
        {"id": "p_20260919_xxx", "name": "教学片头",
         "saved_at": "2026-09-19T15:30:00", "task_id_origin": "task_xxx",
         "params": {"layout": {...}, "font": {...}, "output": {...}, "audio": {...},
                    "detected_region": {...}|null, "preview": {...}}},
        ...
      ],
      "updated_at": "..."
    }

ID 内部稳定（用于 delete/rename），name 用户命名（允许重复，重名自动加 (2)）。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

GLOBAL_PROFILES_REL = "tasks/_global_fine_profiles.json"

# 模板载荷包含的 6 组子字段（其余 — 如 materials — 不进模板）。
# REQ-20260930-093：套用模板时六组全量回填（此前 detected_region/preview 只存不回填，
# 推翻 DESIGN-20260920-076 的「apply 不动 detected_region」决策）。
# 旧模板（REQ-076 时代保存）params 里没有 detected_region/preview 键 →
# apply 端 `if key in params` 天然跳过，不动目标任务的这两组，无需数据迁移。
PROFILE_PARAM_KEYS = ("layout", "font", "output", "audio", "detected_region", "preview")

# 保存与套用同口径（六组对称）：save 存多少，apply 就回填多少。
# 历史：REQ-20260920-076 曾让 SAVE 比 APPLY 多存 detected_region（仅为导出 JSON 与
# 任务级同口径）；REQ-20260921-NNN-preview-export 再加 preview；2026-09-30 用户
# 要求六组全部回填，两个白名单合并。
SAVE_PARAM_KEYS = PROFILE_PARAM_KEYS


def _path(repo_root: Path | str) -> Path:
    return Path(repo_root) / GLOBAL_PROFILES_REL


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _new_id() -> str:
    # 时间前缀 + 随机后缀 — 易读且唯一
    return "p_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)


def _load(repo_root: Path | str) -> dict:
    """读全局模板 JSON；文件缺失/损坏 → 回退空注册表。

    不抛异常（与 llm_config._load 同策略）：损坏的全局文件不应阻断用户使用。
    """
    p = _path(repo_root)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("profiles"), list):
                return data
            log.warning("fine_profiles 全局文件格式异常，回退空注册表")
        except Exception as e:  # noqa: BLE001
            log.warning("fine_profiles 全局文件损坏（%s），回退空注册表", e)
    return {"_schema": 1, "profiles": [], "updated_at": ""}


def _save(repo_root: Path | str, data: dict) -> None:
    """atomic write：先写 .tmp 再 os.replace，避免写一半断电导致文件损坏。"""
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


def _sanitize_params(params: dict) -> dict:
    """只保留 SAVE_PARAM_KEYS（= PROFILE_PARAM_KEYS，六组）中的子字段，
    避免塞入 materials 等意外数据。

    detected_region / preview 缺键的旧模板保持缺键（apply 端 `if key in params` 跳过）。
    """
    return {k: params.get(k) for k in SAVE_PARAM_KEYS if k in params}


def list_profiles(repo_root: Path | str) -> list[dict]:
    """返回所有模板（按 saved_at 倒序）。"""
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
    params: dict,
    task_id_origin: str = "",
) -> dict:
    """把 params (6 组子字段) 保存为新模板。重名时自动加 `(2)`。

    返回新建的 profile dict（含 id / name / saved_at / params）。
    """
    data = _load(repo_root)
    profiles = list(data.get("profiles") or [])
    final_name = _unique_name(profiles, name)
    profile = {
        "id": _new_id(),
        "name": final_name,
        "saved_at": _now_iso(),
        "task_id_origin": task_id_origin,
        "params": _sanitize_params(params),
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


def rename_profile(repo_root: Path | str, profile_id: str, new_name: str) -> bool:
    """按 ID 改名。重名时自动加 `(2)` 后缀。"""
    data = _load(repo_root)
    profiles = list(data.get("profiles") or [])
    target = next((p for p in profiles if p.get("id") == profile_id), None)
    if not target:
        return False
    others = [p for p in profiles if p.get("id") != profile_id]
    target["name"] = _unique_name(others, new_name)
    data["profiles"] = profiles
    _save(repo_root, data)
    return True
