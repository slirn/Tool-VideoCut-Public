"""异常体系 — REQ-A §3.8"""


class TaskError(Exception):
    """所有任务相关异常的基类。"""


class TaskNotFoundError(TaskError):
    """按 task_id 找不到任务时抛出。"""


class DuplicateTaskError(TaskError):
    """创建任务时 task_id 已存在时抛出。"""


class InvalidTimeRangeError(TaskError):
    """时间片段 start >= end 或 end 超出视频总时长时抛出。"""


class MetadataCorruptedError(TaskError):
    """metadata.json 缺失字段 / 类型错误 / schema_version 不识别时抛出。"""


class VideoProcessingError(TaskError):
    """ffmpeg/ffprobe 调用失败时抛出（REQ-C）。"""
