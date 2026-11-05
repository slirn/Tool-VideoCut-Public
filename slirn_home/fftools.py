# -*- coding: utf-8 -*-
"""ffmpeg/ffprobe 二进制解析（REQ-20261105-001）。

工程里 ffmpeg 统一走 compose_service.ffmpeg_bin()（imageio_ffmpeg 捆绑版
优先）；ffprobe 此前 15 处硬编码裸 ``"ffprobe"``（只能查到 PATH 上的）。
Windows 实测坑（2026-11-05 部署实例）：目标机器装了 ffmpeg 却没装 ffprobe，
而 compose_service._probe_duration 等只捕 ValueError/TimeoutExpired，
FileNotFoundError 直接冒泡 → 长短视频「粗剪合成」全部失败
（``[WinError 2] 系统找不到指定的文件``）。

ffprobe_bin() 解析顺序：
1. PATH 上的 ffprobe（shutil.which）—— 与旧行为完全一致；
2. 已解析的 ffmpeg（imageio 捆绑版优先，回退 PATH ffmpeg）同目录下的
   ffprobe(.exe) —— 覆盖「ffmpeg 可用但 PATH 只暴露了 ffmpeg」的常见发行；
3. 都没有 → 返回 "ffprobe"，subprocess 抛 FileNotFoundError，由调用方的
   容错分支处理（各调用点均已按「探测失败降级」语义兜底）。

结果进程内缓存；改环境后如需重解析，置 ``fftools._CACHE = None``。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE: str | None = None


def _ffmpeg_candidates() -> list[str]:
    """可能存在的 ffmpeg 绝对路径（imageio 捆绑版优先，PATH 兜底）。"""
    out: list[str] = []
    try:
        import imageio_ffmpeg

        out.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 — 未装 imageio_ffmpeg / 解析失败
        pass
    p = shutil.which("ffmpeg")
    if p:
        out.append(p)
    return out


def resolve_ffprobe() -> str | None:
    """在已知 ffmpeg 同目录里找 ffprobe；找不到返回 None。"""
    for exe in _ffmpeg_candidates():
        if not exe:
            continue
        cand = Path(exe).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if cand.is_file():
            return str(cand)
    return None


def ffprobe_bin() -> str:
    """ffprobe 可执行文件路径（见模块 docstring 的解析顺序与兜底语义）。"""
    global _CACHE
    if _CACHE is None:
        found = shutil.which("ffprobe")
        if not found:
            found = resolve_ffprobe()
            if found:
                log.info("[fftools] PATH 上无 ffprobe，回退 ffmpeg 同目录: %s", found)
        _CACHE = found or "ffprobe"
    return _CACHE


def ffprobe_available() -> bool:
    """当前进程里能否解析到 ffprobe（供「无 ffprobe 直接走降级」的前置判断）。"""
    return shutil.which("ffprobe") is not None or resolve_ffprobe() is not None
