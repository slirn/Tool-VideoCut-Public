# -*- coding: utf-8 -*-
"""剪辑参数的全局自动快照（REQ-20261022-145）。

用户每次保存剪辑参数时，系统**自动**另存一份到全局文件 ——
与「命名模板」（用户手动保存、可多份）不同，这里是**单份最近值**，
目的：换项目 / 换机器 / 新系统时可直接沿用，微调即可。

存储：
  长视频 → ``tasks/_global_fine_params.json``
  短视频 → ``tasks/_global_short_video_params.json``

结构::

    {"_schema": 1, "updated_at": "2026-10-22T12:00:00", "params": {...}}

写入用「先写 .tmp 再 replace」的原子写；文件缺失或损坏 → 读取返回 None
（调用方降级为「没有快照」），**不影响主流程**。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 两类参数的全局快照文件
KIND_LONG = "long"
KIND_SHORT = "short"
_PATHS: dict[str, str] = {
    KIND_LONG: "tasks/_global_fine_params.json",
    KIND_SHORT: "tasks/_global_short_video_params.json",
}
# 内置默认参数文件（随仓库分发，可被用户改成「团队默认值」）
DEFAULTS_DIR = Path(__file__).resolve().parent / "assets" / "defaults"
_DEFAULTS_FILES: dict[str, str] = {
    KIND_LONG: "long_video_params.json",
    KIND_SHORT: "short_video_params.json",
}

_SCHEMA = 1


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def snapshot_path(repo_root: Path | str, kind: str) -> Path:
    rel = _PATHS.get(kind)
    if not rel:
        raise ValueError(f"未知的参数类型：{kind}")
    return Path(repo_root) / rel


def default_params_path(kind: str) -> Path:
    name = _DEFAULTS_FILES.get(kind)
    if not name:
        raise ValueError(f"未知的参数类型：{kind}")
    return DEFAULTS_DIR / name


def _load_json(path: Path) -> Any:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — 损坏文件按「没有」处理
        log.warning("[global_params][%s] 读取失败：%s", path, e)
    return None


def save_snapshot(repo_root: Path | str, kind: str, params: dict) -> dict | None:
    """把参数另存为全局快照。失败返回 None（**不影响主流程**）。"""
    try:
        path = snapshot_path(repo_root, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_schema": _SCHEMA,
            "updated_at": _now_iso(),
            "params": params or {},
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
        return payload
    except Exception as e:  # noqa: BLE001
        log.warning("[global_params] 快照写入失败（已忽略）：%s", e)
        return None


def load_snapshot(repo_root: Path | str, kind: str) -> dict | None:
    """读全局快照；不存在 / 损坏 → None。"""
    try:
        path = snapshot_path(repo_root, kind)
    except ValueError:
        return None
    data = _load_json(path)
    if not isinstance(data, dict):
        return None
    params = data.get("params")
    if not isinstance(params, dict):
        return None
    return {"updated_at": data.get("updated_at") or "", "params": params}


def load_default_params(kind: str) -> dict:
    """读内置默认参数文件；缺失 / 损坏 → {}。"""
    try:
        path = default_params_path(kind)
    except ValueError:
        return {}
    data = _load_json(path)
    return data if isinstance(data, dict) else {}


def has_default_params(kind: str) -> bool:
    try:
        return default_params_path(kind).is_file()
    except ValueError:
        return False
