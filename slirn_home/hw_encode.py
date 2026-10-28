# -*- coding: utf-8 -*-
"""硬件视频编码探测与参数选择（各渲染导出路径共用）。

逐级探测 h264_nvenc → h264_qsv，各跑一次 0.1s lavfi 黑帧试编码、结果缓存
1 小时；全不可用（无独显/核显、驱动过旧——云主机常态）自动回落 CPU
libx264，行为与无硬件时完全一致，无需部署侧配置。SLIRN_FINE_HW_ENC=0
强制关闭硬件编码。

画质口径：quality 参数对标 libx264 crf（默认 23）——nvenc p5 + VBR cq、
qsv fast + ICQ（同码率效率略逊于 x264，口播/图文类内容肉眼无差）。

接入方：
- 长视频精剪预览/导出（fine_render，预览 + 单进程导出 + BGM 两遍 pass2）
- 短视频 Stage6 精剪混编导出（short_video_compose.compose_export_one）
- 短视频多素材混剪（short_video_render）
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

log = logging.getLogger(__name__)

ENC_CACHE: dict = {"enc": None, "at": 0.0}


def hw_encode_enabled() -> bool:
    return os.environ.get("SLIRN_FINE_HW_ENC", "1").strip().lower() not in ("0", "false", "no")


def probe_hw_encoder() -> str:
    """试编码探测可用的硬件编码器名（h264_nvenc / h264_qsv / 空串）。"""
    for enc in ("h264_nvenc", "h264_qsv"):
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                 "-c:v", enc, "-f", "null", "-"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return enc
        except Exception:  # noqa: BLE001 — 单个探测异常继续试下一个
            continue
    return ""


def hw_encoder_cached() -> str:
    if not hw_encode_enabled():
        return ""
    now = time.time()
    if ENC_CACHE["enc"] is None or now - ENC_CACHE["at"] > 3600:
        ENC_CACHE.update(enc=probe_hw_encoder(), at=now)
        log.info("[hw-encode] 硬件编码探测：%s", ENC_CACHE["enc"] or "无，用 CPU libx264")
    return ENC_CACHE["enc"] or ""


def encoder_args(quality: int = 23) -> tuple[list[str], str]:
    """视频编码 argv 片段 + 编码器名；quality 对标 libx264 crf 口径。"""
    enc = hw_encoder_cached()
    if enc == "h264_nvenc":
        return (["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                 "-cq", str(quality), "-b:v", "0"], enc)
    if enc == "h264_qsv":
        return (["-c:v", "h264_qsv", "-preset", "fast",
                 "-global_quality", str(quality)], enc)
    return (["-c:v", "libx264", "-preset", "fast", "-crf", str(quality)], "libx264")
