"""时间工具 — HH:MM:SS 解析 / 格式化 / 区间校验。"""

from __future__ import annotations

from datetime import timedelta
import re

from tasklib.exceptions import InvalidTimeRangeError

_TIME_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2}(?:\.\d{1,3})?)$")


def parse_time(s: str) -> timedelta:
    """把 'HH:MM:SS' 或 'HH:MM:SS.mmm' 解析为 timedelta。

    Raises:
        ValueError: 格式不合法或越界。
    """
    match = _TIME_RE.match(s)
    if not match:
        raise ValueError(f"时间格式不合法: {s!r}，期望 HH:MM:SS 或 HH:MM:SS.mmm")
    h = int(match.group("h"))
    m = int(match.group("m"))
    sec = float(match.group("s"))
    if m >= 60 or sec >= 60:
        raise ValueError(f"时间分量越界: {s!r}")
    return timedelta(hours=h, minutes=m, seconds=sec)


def format_time(td: timedelta) -> str:
    """把 timedelta 格式化为 HH:MM:SS。"""
    total = int(td.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def validate_segment(start: str, end: str) -> tuple[timedelta, timedelta]:
    """校验 start < end，并返回解析后的 (start_td, end_td)。

    Raises:
        InvalidTimeRangeError: start >= end。
        ValueError: 时间格式不合法。
    """
    s_td = parse_time(start)
    e_td = parse_time(end)
    if s_td >= e_td:
        raise InvalidTimeRangeError(f"start 必须小于 end: start={start!r}, end={end!r}")
    return s_td, e_td


def segment_filename(stem: str, start: str, end: str, ext: str) -> str:
    """生成截取段文件名：<stem>_segment_<HH-MM-SS>_<HH-MM-SS>.<ext>

    HH:MM:SS 中的 ':' 替换为 '-' 以兼容 Windows 文件名。
    """
    safe_start = start.replace(":", "-")
    safe_end = end.replace(":", "-")
    return f"{stem}_segment_{safe_start}_{safe_end}.{ext}"
