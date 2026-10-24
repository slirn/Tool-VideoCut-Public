"""tasklib — 任务数据模型与存储。"""

from tasklib.exceptions import (
    DuplicateTaskError,
    InvalidTimeRangeError,
    MetadataCorruptedError,
    TaskError,
    TaskNotFoundError,
    VideoProcessingError,
)
from tasklib.hotword_lib import HotwordLibError, HotwordLibrary
from tasklib.manager import TaskManager
from tasklib.models import (
    TASK_STATUS_LABEL,
    Task,
    TaskStatus,
    TaskSummary,
    TimeSegment,
)
from tasklib.schema import CURRENT_SCHEMA_VERSION
from tasklib.time_utils import (
    format_time,
    parse_time,
    segment_filename,
    validate_segment,
)
from tasklib.video import VideoInfo, cut_video, get_video_duration, probe_video

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DuplicateTaskError",
    "HotwordLibError",
    "HotwordLibrary",
    "InvalidTimeRangeError",
    "MetadataCorruptedError",
    "TASK_STATUS_LABEL",
    "Task",
    "TaskError",
    "TaskManager",
    "TaskNotFoundError",
    "TaskStatus",
    "TaskSummary",
    "TimeSegment",
    "VideoInfo",
    "VideoProcessingError",
    "cut_video",
    "format_time",
    "get_video_duration",
    "parse_time",
    "probe_video",
    "segment_filename",
    "validate_segment",
]
