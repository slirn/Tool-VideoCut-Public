"""数据模型 — TaskStatus 枚举 + TimeSegment NamedTuple + Task dataclass。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from tasklib.exceptions import InvalidTimeRangeError


class TaskStatus(str, Enum):
    """任务状态枚举 — REQ-A §3.2。

    状态机迁移规则留 TODO（REQ-A.2.2），本枚举只暴露取值。
    继承 str 是为了 JSON 序列化时直接输出枚举值字符串（如 "DRAFT"）。
    """

    DRAFT = "DRAFT"
    ASSETS_READY = "ASSETS_READY"
    SUBTITLE_GENERATED = "SUBTITLE_GENERATED"
    SUBTITLE_REVIEWED = "SUBTITLE_REVIEWED"
    ROUGH_CUT_DONE = "ROUGH_CUT_DONE"
    FINE_SUBTITLE_DONE = "FINE_SUBTITLE_DONE"
    FINE_SUBTITLE_REVIEWED = "FINE_SUBTITLE_REVIEWED"
    FINE_CUT_DONE = "FINE_CUT_DONE"
    MUXED = "MUXED"


TASK_STATUS_LABEL: dict[TaskStatus, str] = {
    TaskStatus.DRAFT: "草稿",
    TaskStatus.ASSETS_READY: "已准备素材",
    TaskStatus.SUBTITLE_GENERATED: "已生成字幕",
    TaskStatus.SUBTITLE_REVIEWED: "已分析修改字幕",
    TaskStatus.ROUGH_CUT_DONE: "切分修剪完成",
    TaskStatus.FINE_SUBTITLE_DONE: "精剪字幕",
    TaskStatus.FINE_SUBTITLE_REVIEWED: "优化字幕完成",
    TaskStatus.FINE_CUT_DONE: "精剪完成",
    TaskStatus.MUXED: "视频字幕合成",
}


class TimeSegment(NamedTuple):
    """时间片段 — REQ-A §3.4（NamedTuple 不可变 + 可解构）。

    start / end 均为 'HH:MM:SS' 字符串。
    path 是截取段视频的物理路径（绝对路径，运行时填充）。
    """

    start: str
    end: str
    path: Path

    def validate(self) -> None:
        """校验 start < end，调用方负责。"""
        from tasklib.time_utils import parse_time  # 避免循环 import

        s = parse_time(self.start)
        e = parse_time(self.end)
        if s >= e:
            raise InvalidTimeRangeError(f"start 必须小于 end: {self.start!r} >= {self.end!r}")


@dataclass
class Task:
    """任务数据模型 — REQ-A §3.1。

    字段语义：
        task_id:               唯一 ID（如 "20260914-001"）
        name:                  用户可改的任务名
        original_video_source: 原视频绝对路径（用户提供的，不复制）
        original_video_symlink: 任务目录下的符号链接路径（可能不存在）
        segment:               截取段（如有）
        hotwords_path:         任务级热词文件路径
        status:                当前状态
        created_at:            创建时间（UTC）
        updated_at:            最后修改时间（UTC）
    """

    task_id: str
    name: str
    original_video_source: Path
    original_video_symlink: Path
    segment: TimeSegment | None
    hotwords_path: Path
    status: TaskStatus = TaskStatus.DRAFT
    # REQ-20260915-002：热词来源回显（可选，旧任务默认 None/False → 详情页降级）
    inherit_public: bool = False
    hotword_sources: dict[str, str] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # REQ-20260926-NNN：创建者用户名（None = 系统/旧任务，向后兼容）
    created_by: str | None = None


@dataclass
class TaskSummary:
    """任务摘要 — 用于 list() 返回，避免加载完整热词文件。"""

    task_id: str
    name: str
    status: TaskStatus
    original_video: str      # 文件名（basename）
    created_at: datetime
    updated_at: datetime
    has_segment: bool
