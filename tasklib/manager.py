"""TaskManager — REQ-A §3.6 主接口。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from tasklib.exceptions import (
    DuplicateTaskError,
    TaskNotFoundError,
)
from tasklib.models import Task, TaskStatus, TaskSummary, TimeSegment
from tasklib.schema import task_from_dict, task_to_dict
from tasklib.time_utils import segment_filename, validate_segment

log = logging.getLogger(__name__)


class TaskManager:
    """任务管理器 — REQ-A §3.6 接口契约。"""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.tasks_dir = self.repo_root / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 创建 ----------

    def create(
        self,
        name: str,
        original_video: Path,
        segment: TimeSegment | None = None,
        hotwords: list[str] | None = None,
        status: TaskStatus = TaskStatus.DRAFT,
        inherit_public: bool = False,
        hotword_sources: dict[str, str] | None = None,
        created_by: str | None = None,            # REQ-20260926-NNN：创建者用户名
    ) -> Task:
        """新建任务。

        Args:
            name: 任务名（用户可改）
            original_video: 原视频绝对路径（**不复制**，仅记录）
            segment: 可选截取段
            hotwords: 任务级热词列表
            inherit_public: 建任务时是否勾选「继承公共库所有热词」（REQ-20260915-002）
            hotword_sources: 热词来源 word → "inherit"/"pick"/"manual"（同上，可选）

        Returns:
            创建的 Task 对象

        Raises:
            DuplicateTaskError: task_id 已存在（极端情况下）
            InvalidTimeRangeError: segment 起止时间不合法
        """
        original_video = original_video.resolve()
        if not original_video.exists():
            raise FileNotFoundError(f"原视频不存在: {original_video}")

        task_id = self._next_task_id()
        task_dir = self.tasks_dir / task_id
        if task_dir.exists():
            raise DuplicateTaskError(f"task_id 已存在: {task_id}")
        task_dir.mkdir()

        # 子目录
        raw_input_dir = task_dir / "raw_input"
        raw_input_dir.mkdir()
        (task_dir / "outputs").mkdir()

        # 截取段文件名（如有）
        segment_obj: TimeSegment | None = None
        if segment is not None:
            validate_segment(segment.start, segment.end)
            ext = original_video.suffix.lstrip(".") or "mp4"
            seg_filename = segment_filename(original_video.stem, segment.start, segment.end, ext)
            seg_path = raw_input_dir / seg_filename
            segment_obj = TimeSegment(start=segment.start, end=segment.end, path=seg_path)

        # 符号链接（或回退到路径）
        symlink_path = raw_input_dir / f"original{original_video.suffix}"
        actual_link = self._link_or_record(original_video, symlink_path)

        # 热词文件
        hotwords_path = task_dir / "hotwords.txt"
        hotwords_path.write_text("\n".join(hotwords or []), encoding="utf-8")

        now = datetime.now(timezone.utc)
        task = Task(
            task_id=task_id,
            name=name,
            original_video_source=original_video,
            original_video_symlink=actual_link,
            segment=segment_obj,
            hotwords_path=hotwords_path,
            status=status,
            inherit_public=inherit_public,
            hotword_sources=dict(hotword_sources) if hotword_sources else None,
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )

        self._write_metadata(task)
        log.info("创建任务 %s: %s", task_id, name)
        return task

    # ---------- 查询 ----------

    def get(self, task_id: str) -> Task:
        task_dir = self.tasks_dir / task_id
        if not task_dir.is_dir():
            raise TaskNotFoundError(f"任务不存在: {task_id}")
        meta_path = task_dir / "metadata.json"
        if not meta_path.exists():
            raise TaskNotFoundError(f"任务 {task_id} 缺少 metadata.json")
        return task_from_dict(json.loads(meta_path.read_text(encoding="utf-8")), self.repo_root)

    def list(self) -> list[TaskSummary]:
        summaries: list[TaskSummary] = []
        if not self.tasks_dir.exists():
            return summaries
        for entry in sorted(self.tasks_dir.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            meta_path = entry / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                task = task_from_dict(
                    json.loads(meta_path.read_text(encoding="utf-8")), self.repo_root
                )
            except Exception as e:  # noqa: BLE001 — 跳过损坏任务，继续列其他
                log.warning("跳过损坏任务 %s: %s", entry.name, e)
                continue
            summaries.append(
                TaskSummary(
                    task_id=task.task_id,
                    name=task.name,
                    status=task.status,
                    original_video=task.original_video_source.name,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                    has_segment=task.segment is not None,
                )
            )
        # 按 updated_at 倒序
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    # ---------- REQ-20260926-NNN：按用户可见性过滤 ----------

    def list_for_user(
        self,
        username: str,
        is_admin: bool,
        member_of: list[str] | None = None,
    ) -> list[TaskSummary]:
        """按用户可见性过滤任务列表：admin 看全部；其他用户看「自己创建的 ∪ 自己被加为成员的任务」。
        member_of: 该用户被加入的所有 task_id（由 AuthStore.list_user_tasks 提供）。
        """
        all_summaries = self.list()
        if is_admin:
            return all_summaries
        visible: list[TaskSummary] = []
        member_set = set(member_of or [])
        for s in all_summaries:
            try:
                task = self.get(s.task_id)
            except Exception:                # noqa: BLE001
                continue                      # 跳过损坏任务
            creator = getattr(task, "created_by", None)
            if creator == username or s.task_id in member_set:
                visible.append(s)
        return visible

    def assert_access(
        self,
        user: dict,
        task_id: str,
        member_of: list[str] | None = None,
    ) -> Task:
        """权限校验：admin 豁免；创建者永远可见；成员可见；否则抛 PermissionError。
        任务不存在抛 TaskNotFoundError（沿用 mgr.get 行为）。"""
        task = self.get(task_id)
        if user.get("is_admin"):
            return task
        creator = getattr(task, "created_by", None)
        if creator == user.get("username"):
            return task
        if member_of and task_id in set(member_of):
            return task
        raise PermissionError(
            f"用户 {user.get('username')!r} 不是任务 {task_id!r} 的成员（也非创建者/admin）"
        )

    def exists(self, task_id: str) -> bool:
        return (self.tasks_dir / task_id).is_dir()

    # ---------- 更新 ----------

    def update_status(self, task_id: str, new_status: TaskStatus) -> None:
        task = self.get(task_id)  # 不存在时抛 TaskNotFoundError
        task.status = new_status
        task.updated_at = datetime.now(timezone.utc)
        self._write_metadata(task)
        log.info("更新任务 %s 状态为 %s", task_id, new_status.value)

    def update_hotwords(self, task_id: str, hotwords: list[str]) -> None:
        task = self.get(task_id)
        task.hotwords_path.write_text("\n".join(hotwords), encoding="utf-8")
        task.updated_at = datetime.now(timezone.utc)
        self._write_metadata(task)
        log.info("更新任务 %s 热词（%d 条）", task_id, len(hotwords))

    def update_task(
        self,
        task_id: str,
        name: str | None = None,
        segment: TimeSegment | None = None,
        segment_changed: bool = False,
        hotwords: list[str] | None = None,
        inherit_public: bool | None = None,
        hotword_sources: dict[str, str] | None = None,
    ) -> Task:
        """编辑任务（REQ-20260915-003）— 只更新传入的字段，updated_at 刷新。

        Args:
            name: 新任务名（None = 不改）
            segment: 新截取段；segment_changed=False 时仅当传入非 None 才整体替换，
                     传 None 且 segment_changed=True 表示清除截取段
            hotwords: 新热词列表（写 hotwords.txt；None = 不改）
            inherit_public / hotword_sources: 热词来源（None = 不改）

        Returns:
            更新后的 Task
        """
        task = self.get(task_id)  # 不存在时抛 TaskNotFoundError
        if name is not None and name.strip():
            task.name = name.strip()
        if segment_changed:
            task.segment = segment
        if hotwords is not None:
            task.hotwords_path.write_text("\n".join(hotwords), encoding="utf-8")
        if inherit_public is not None:
            task.inherit_public = inherit_public
        if hotword_sources is not None:
            task.hotword_sources = dict(hotword_sources) if hotword_sources else None
        task.updated_at = datetime.now(timezone.utc)
        self._write_metadata(task)
        log.info("更新任务 %s（编辑）", task_id)
        return task

    # ---------- 删除 ----------

    def delete(self, task_id: str) -> None:
        """删除任务 — 整个任务目录从磁盘移除（不可恢复）。

        Windows 加固（REQ-20260917-034）：
        - metadata.json **最后**删：中途失败时任务在列表里仍完整可见，
          不留「磁盘有目录但 UI 看不见」的幽灵残留；
        - 失败先清只读位再重试 3 次（视频预览/流式播放握着的句柄常是瞬态的）；
        - 仍失败则如实抛最后一次 OSError，由调用方反馈用户（不吞）。
        """
        import shutil
        import stat
        import time

        task_dir = self.tasks_dir / task_id
        if not task_dir.is_dir():
            raise TaskNotFoundError(f"任务不存在: {task_id}")

        last_err: OSError | None = None
        for attempt in range(3):
            try:
                for child in task_dir.iterdir():
                    if child.name == "metadata.json":
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                (task_dir / "metadata.json").unlink(missing_ok=True)
                task_dir.rmdir()
                log.info("删除任务 %s", task_id)
                return
            except FileNotFoundError:
                if not task_dir.exists():
                    return  # 并发删除/已消失 — 视为成功
            except OSError as e:
                last_err = e
                log.warning(
                    "删除任务 %s 第 %d 次失败: %s", task_id, attempt + 1, e
                )
                for p in task_dir.rglob("*"):  # 清只读位（Windows 常见）
                    try:
                        os.chmod(p, stat.S_IWRITE)
                    except OSError:
                        pass
                time.sleep(0.6)
        raise last_err  # type: ignore[misc]

    # ---------- 私有 ----------

    def _next_task_id(self) -> str:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = f"{today}-"
        max_seq = 0
        for entry in self.tasks_dir.iterdir():
            if entry.is_dir() and entry.name.startswith(prefix):
                try:
                    seq = int(entry.name[len(prefix):])
                    max_seq = max(max_seq, seq)
                except ValueError:
                    continue
        return f"{prefix}{max_seq + 1:03d}"

    def _link_or_record(self, src: Path, dst: Path) -> Path:
        """尝试创建符号链接 src -> dst。失败时返回 src（用于 metadata 记录）。"""
        try:
            dst.symlink_to(src)
            return dst
        except (OSError, NotImplementedError) as e:
            log.warning(
                "无法创建符号链接 %s -> %s: %s。原视频路径将记录在 metadata 中。",
                dst, src, e,
            )
            return src

    def _write_metadata(self, task: Task) -> None:
        """原子写 metadata.json：先写 .tmp 再 os.replace。"""
        meta_path = self.tasks_dir / task.task_id / "metadata.json"
        tmp_path = meta_path.with_suffix(".json.tmp")
        data = task_to_dict(task, self.repo_root)
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, meta_path)
