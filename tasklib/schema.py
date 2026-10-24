"""JSON 序列化 — metadata.json ↔ Task。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from tasklib.exceptions import MetadataCorruptedError
from tasklib.models import Task, TaskStatus, TimeSegment

CURRENT_SCHEMA_VERSION = "1"


def _to_repo_relative(p: Path, repo_root: Path) -> str:
    """优先转相对路径，跨盘符或逃出仓库时回退到绝对路径。统一 POSIX 风格。"""
    try:
        rel = p.resolve().relative_to(repo_root.resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return str(p.resolve()).replace("\\", "/")


def _normalize_path(s: str, repo_root: Path) -> Path:
    """JSON 路径字符串还原为 Path。绝对路径直接用，相对路径基于 repo_root。

    POSIX 风格的 '/' 会被 Path 自动转换为平台原生分隔符。
    """
    p = Path(s)
    if p.is_absolute():
        return p
    return (repo_root / p).resolve()


def task_to_dict(task: Task, repo_root: Path) -> dict:
    """显式构造 dict，字段顺序可控（REQ-A.7.2）。"""
    repo_root = repo_root.resolve()
    seg = task.segment
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "task_id": task.task_id,
        "name": task.name,
        "original_video_source": _to_repo_relative(task.original_video_source, repo_root),
        "original_video_symlink": _to_repo_relative(task.original_video_symlink, repo_root),
        "segment": None if seg is None else {
            "start": seg.start,
            "end": seg.end,
            "path": _to_repo_relative(seg.path, repo_root),
        },
        "hotwords_path": _to_repo_relative(task.hotwords_path, repo_root),
        # REQ-20260915-002：热词来源（可选字段；旧 metadata.json 无 → 读侧回退默认值）
        "inherit_public": task.inherit_public,
        "hotword_sources": task.hotword_sources,
        "status": task.status.value,
        "created_at": task.created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": task.updated_at.isoformat().replace("+00:00", "Z"),
        # REQ-20260926-NNN：创建者用户名（None 表示旧任务/系统任务，d.get 兼容）
        "created_by": task.created_by,
    }


def task_from_dict(d: dict, repo_root: Path) -> Task:
    """从 JSON dict 反序列化为 Task。

    Raises:
        MetadataCorruptedError: 字段缺失 / 类型错误 / schema_version 不识别。
    """
    repo_root = repo_root.resolve()
    try:
        if d.get("schema_version") != CURRENT_SCHEMA_VERSION:
            raise MetadataCorruptedError(
                f"schema_version 不识别: {d.get('schema_version')!r}（当前 {CURRENT_SCHEMA_VERSION!r}）"
            )

        required = ["task_id", "name", "original_video_source", "status", "created_at", "updated_at"]
        for key in required:
            if key not in d:
                raise MetadataCorruptedError(f"metadata.json 缺少必需字段: {key!r}")

        seg_d = d.get("segment")
        segment = None
        if seg_d is not None:
            segment = TimeSegment(
                start=seg_d["start"],
                end=seg_d["end"],
                path=_normalize_path(seg_d["path"], repo_root),
            )

        return Task(
            task_id=d["task_id"],
            name=d["name"],
            original_video_source=_normalize_path(d["original_video_source"], repo_root),
            original_video_symlink=_normalize_path(
                d.get("original_video_symlink", d["original_video_source"]), repo_root
            ),
            segment=segment,
            hotwords_path=_normalize_path(
                d.get("hotwords_path", str(Path("tasks") / d["task_id"] / "hotwords.txt")),
                repo_root,
            ),
            status=TaskStatus(d["status"]),
            # REQ-20260915-002：旧任务无来源数据 → False/None（详情页降级为无标签）
            inherit_public=bool(d.get("inherit_public", False)),
            hotword_sources=(
                {str(k): str(v) for k, v in d["hotword_sources"].items()}
                if isinstance(d.get("hotword_sources"), dict) else None
            ),
            created_at=datetime.fromisoformat(d["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(d["updated_at"].replace("Z", "+00:00")),
            # REQ-20260926-NNN：旧 metadata 无此字段 → None（系统/迁移前任务）
            created_by=d.get("created_by"),
        )
    except MetadataCorruptedError:
        raise
    except (KeyError, ValueError, TypeError) as e:
        raise MetadataCorruptedError(f"metadata.json 损坏: {e}") from e
