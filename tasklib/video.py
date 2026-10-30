"""视频处理底层 — ffmpeg/ffprobe 调用（仅 stdlib subprocess）。"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import NamedTuple

from tasklib.exceptions import VideoProcessingError

log = logging.getLogger(__name__)

_FFMPEG_TIMEOUT = 300  # 5 分钟（stream copy 通常几秒完成）


class VideoInfo(NamedTuple):
    """视频元数据 — REQ-C.5.3。"""

    duration: float | None   # 秒
    codec: str | None
    size_bytes: int


def get_video_duration(path: Path) -> float | None:
    """调用 ffprobe 获取时长（秒）。

    REQ-20261030-015：mtime+size 键控缓存 —— 工作台一次渲染调 2 次、
    每次重渲/刷新又调，每次都是 ~100ms 的 ffprobe 子进程；产物视频在
    重新合成前路径不变，缓存命中后 0 开销（文件更新 → mtime 变 → 失效）。

    Returns:
        时长（秒），失败返回 None（D1 决策）。
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        _DUR_CACHE.pop(str(p), None)
        return None
    hit = _DUR_CACHE.get(str(p))
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    dur = _ffprobe_duration(p)
    # 失败结果也缓存（负缓存），同一坏文件不再反复起子进程
    _DUR_CACHE[str(p)] = (st.st_mtime_ns, st.st_size, dur)
    return dur


_DUR_CACHE: dict[str, tuple[int, int, float | None]] = {}


def _ffprobe_duration(path: Path) -> float | None:
    """真正起 ffprobe 子进程（get_video_duration 的缓存内层）。"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("ffprobe 失败 (%s): %s", path, result.stderr.strip())
            return None
        return float(result.stdout.strip())
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired) as e:
        log.warning("ffprobe 异常 (%s): %s", path, e)
        return None


def probe_video(path: Path) -> VideoInfo:
    """返回视频元数据（duration / codec / size）。失败时 duration=None。"""
    duration = get_video_duration(path)
    codec = _get_video_codec(path)
    size_bytes = path.stat().st_size if path.exists() else 0
    return VideoInfo(duration=duration, codec=codec, size_bytes=size_bytes)


def _get_video_codec(path: Path) -> str | None:
    """获取视频编码（best-effort，失败返回 None）。"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def cut_video(src: Path, dst: Path, start: str, end: str) -> None:
    """用 ffmpeg stream copy 模式截取视频片段。

    Args:
        src: 原视频路径
        dst: 输出路径
        start: HH:MM:SS 格式
        end: HH:MM:SS 格式

    Raises:
        VideoProcessingError: ffmpeg 调用失败 / 超时 / 输出文件未生成
    """
    if not src.exists():
        raise VideoProcessingError(f"原视频不存在: {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",                  # 覆盖输出
        "-ss", start,          # 在 input 前（快速 seek）
        "-to", end,            # 注意：-to 相对于输入
        "-i", str(src),
        "-c", "copy",          # stream copy，不重新编码
        "-avoid_negative_ts", "make_zero",
        # REQ-20261030-016：moov 索引前置 —— 否则审校播放器（截取段）每次起播
        # 都要先去文件尾取索引，外网受限带宽下点字幕定位播放要数秒。
        "-movflags", "+faststart",
        str(dst),
    ]

    log.info("ffmpeg 截取: %s [%s -> %s] -> %s", src.name, start, end, dst.name)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_FFMPEG_TIMEOUT,
        )
    except FileNotFoundError as e:
        raise VideoProcessingError(
            "ffmpeg 不在 PATH 中。请安装 ffmpeg 并确保可执行。"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise VideoProcessingError(
            f"ffmpeg 截取超时（>{_FFMPEG_TIMEOUT}s）: {src}"
        ) from e

    if result.returncode != 0:
        raise VideoProcessingError(
            f"ffmpeg 截取失败 (exit={result.returncode}):\n{result.stderr[-500:]}"
        )

    if not dst.exists():
        raise VideoProcessingError(f"ffmpeg 退出成功但输出文件未生成: {dst}")
