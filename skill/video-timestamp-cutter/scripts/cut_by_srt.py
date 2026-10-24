#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
"""
根据 SRT 字幕时间戳从视频中提取片段。

支持多种时间戳格式：SRT、JSON、带时间戳的纯文本。

用法:
    python cut_by_srt.py --input video.mp4 --srt video.srt --output video_cut.mp4
    python cut_by_srt.py --input video.mp4 --srt video.srt --output video_cut.mp4 --indices 1,3,5
    python cut_by_srt.py --input video.mp4 --srt video.srt --output video_cut.mp4 --start 10.5 --end 65.3
"""

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import moviepy.editor as mpy
from moviepy.video.io.ffmpeg_writer import FFMPEG_VideoWriter


# ============ 时间戳解析 ============

def parse_srt_timestamps(srt_path):
    """
    解析 SRT 文件，返回 [(start_sec, end_sec), ...] 列表
    """
    segments = []
    with open(srt_path, 'r', encoding='utf-8-sig') as f:
        content = f.read()

    # 处理 \r\n -> \n
    content = content.replace('\r\n', '\n')

    # 匹配时间戳行: 00:00:10,500 --> 00:00:15,200
    pattern = re.compile(
        r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})'
    )

    for match in pattern.finditer(content):
        h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
        start = int(h1)*3600 + int(m1)*60 + int(s1) + int(ms1)/1000
        end = int(h2)*3600 + int(m2)*60 + int(s2) + int(ms2)/1000
        segments.append({'start': start, 'end': end})

    return segments


def parse_cleaned_format(txt_path):
    """
    解析清洗技能输出的固定格式:
    1. [00:01:11,830 - 00:01:18,940] 然后呃我们回到 IP 这个东西

    格式: 序号. [开始时间 - 结束时间] 文本内容
    """
    segments = []
    with open(txt_path, 'r', encoding='utf-8-sig') as f:
        content = f.read()

    # 处理 \r\n
    content = content.replace('\r\n', '\n')

    # 匹配: 序号. [hh:mm:ss,mmm - hh:mm:ss,mmm] 文本
    # 格式: 1. [00:01:11,830 - 00:01:18,940] 文本
    pattern = re.compile(
        r'^\d+\.\s*\[(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\]'
    , re.MULTILINE)

    for match in pattern.finditer(content):
        h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
        start = int(h1)*3600 + int(m1)*60 + int(s1) + int(ms1)/1000
        end = int(h2)*3600 + int(m2)*60 + int(s2) + int(ms2)/1000
        segments.append({'start': start, 'end': end})

    return segments


def parse_json_timestamps(json_path):
    """
    解析 JSON 格式:
    [{"start": 10.5, "end": 15.2, "text": "..."}, ...]
    或
    {"segments": [{"start": 10.5, "end": 15.2}, ...]}
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 可能是 {"segments": [...]} 或直接是 [...]
    if isinstance(data, dict):
        data = data.get('segments', data.get('timestamps', []))

    segments = []
    for item in data:
        if isinstance(item, dict):
            segments.append({'start': float(item['start']), 'end': float(item['end'])})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            segments.append({'start': float(item[0]), 'end': float(item[1])})
    return segments


def parse_time_range(text):
    """
    解析 "start-end" 或 "start,end" 格式
    """
    text = text.replace(',', '-')
    if '-' in text:
        parts = text.split('-')
        if len(parts) == 2:
            return float(parts[0].strip()), float(parts[1].strip())
    return None, None


def auto_detect_and_parse(timestamp_path):
    """
    自动检测时间戳文件格式并解析
    """
    ext = Path(timestamp_path).suffix.lower()

    if ext == '.srt':
        return parse_srt_timestamps(timestamp_path)
    elif ext == '.json':
        return parse_json_timestamps(timestamp_path)
    elif ext in ['.txt', '.md']:
        # 尝试 SRT 格式
        try:
            result = parse_srt_timestamps(timestamp_path)
            if result:
                return result
        except:
            pass
        # 尝试清洗格式
        try:
            result = parse_cleaned_format(timestamp_path)
            if result:
                return result
        except:
            pass
        # 尝试 JSON
        try:
            return parse_json_timestamps(timestamp_path)
        except:
            pass
        raise ValueError(f"无法解析时间戳文件: {timestamp_path}")
    else:
        # 尝试各种格式
        for parser in [parse_srt_timestamps, parse_cleaned_format, parse_json_timestamps]:
            try:
                result = parser(timestamp_path)
                if result:
                    return result
            except:
                continue
        raise ValueError(f"无法解析时间戳文件: {timestamp_path}")


# ============ 视频剪切核心逻辑 ============

def cut_video_segments(video_path, segments, output_path,
                       bitrate=None, crf=23, audio_codec='aac',
                       progress_callback=None):
    """
    根据时间戳片段剪切视频

    Args:
        video_path: 输入视频路径
        segments: 时间戳列表 [{'start': float, 'end': float}, ...]
        output_path: 输出视频路径
        bitrate: 视频码率（如 '2000k'），None 则用 CRF
        crf: 质量参数，0=无损，越大约模糊，默认 23
        audio_codec: 音频编码器
        progress_callback: 进度回调函数 (current, total) -> None
    """
    if not segments:
        raise ValueError("没有要处理的片段，时间戳列表为空")

    print(f"Loading video: {video_path}")
    video = mpy.VideoFileClip(video_path)

    # 检查音频
    if video.audio is None:
        print("Warning: Video has no audio track, proceeding without audio")

    total = len(segments)
    clips = []

    for i, seg in enumerate(segments):
        start = max(0, seg['start'])
        end = min(video.duration, seg['end'])

        if start >= end:
            print(f"Skip invalid segment {i+1}: start={start} >= end={end}")
            continue

        print(f"  [{i+1}/{total}] Cutting {start:.2f}s -> {end:.2f}s (duration: {end-start:.2f}s)")

        subclip = video.subclip(start, end)
        clips.append(subclip)

        if progress_callback:
            progress_callback(i + 1, total)

    if not clips:
        raise ValueError("没有有效的视频片段")

    # 拼接
    print("Concatenating clips...")
    if len(clips) == 1:
        final_clip = clips[0]
    else:
        final_clip = mpy.concatenate_videoclips(clips)

    # 输出
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    print(f"Writing output: {output_path}")

    # 构造编码参数
    write_kwargs = {
        'audio_codec': audio_codec,
    }

    if bitrate:
        write_kwargs['bitrate'] = bitrate
    else:
        # moviepy 的 CRF 通过 preset 控制
        write_kwargs['preset'] = 'medium'

    final_clip.write_videofile(output_path, **write_kwargs)

    # 释放内存
    video.close()
    final_clip.close()
    for clip in clips:
        clip.close()

    print(f"Done! Output: {output_path}")
    return output_path


def cut_video_by_srt(video_path, srt_path, output_path,
                     indices=None, start=None, end=None,
                     bitrate=None, crf=23):
    """
    便捷入口：按 SRT 剪切视频

    Args:
        video_path: 输入视频
        srt_path: SRT 或其他时间戳文件
        output_path: 输出路径
        indices: 要提取的字幕序号（从1开始），None 表示全部
        start/end: 直接指定时间范围（秒），优先于 indices
        bitrate: 视频码率
        crf: 质量参数
    """
    # 解析时间戳
    all_segments = auto_detect_and_parse(srt_path)

    # 按时间范围筛选
    if start is not None or end is not None:
        s = start if start is not None else 0
        e = end if end is not None else float('inf')
        segments = [seg for seg in all_segments
                    if seg['end'] >= s and seg['start'] <= e]
        print(f"Filtered to {len(segments)} segments in range [{s}, {e}]")
    elif indices is not None:
        # 序号从1开始
        idx_set = set(int(i) - 1 for i in indices.split(','))
        segments = [all_segments[i] for i in sorted(idx_set)
                    if 0 <= i < len(all_segments)]
        print(f"Selected {len(segments)} segments by index: {indices}")
    else:
        segments = all_segments
        print(f"Using all {len(segments)} segments")

    return cut_video_segments(
        video_path, segments, output_path,
        bitrate=bitrate, crf=crf
    )


# ============ CLI 入口 ============

def main():
    parser = argparse.ArgumentParser(
        description='根据时间戳从视频提取片段',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--input', '-i', required=True, help='输入视频文件')
    parser.add_argument('--srt', '-s', required=True,
                        help='SRT 字幕文件或其他时间戳文件（支持 .srt/.json/.txt/.md）')
    parser.add_argument('--output', '-o', required=True, help='输出视频文件')
    parser.add_argument('--indices',
                        help='要提取的字幕序号，从1开始，逗号分隔，如 "1,3,5"（默认全部）')
    parser.add_argument('--start', type=float,
                        help='开始时间（秒），优先于 --indices')
    parser.add_argument('--end', type=float,
                        help='结束时间（秒），优先于 --indices')
    parser.add_argument('--bitrate', '-b',
                        help='视频码率，如 "2000k"（默认自动）')
    parser.add_argument('--crf', type=int, default=23,
                        help='CRF 质量参数，0=无损，23=默认，51=最低（默认23）')

    args = parser.parse_args()

    cut_video_by_srt(
        video_path=args.input,
        srt_path=args.srt,
        output_path=args.output,
        indices=args.indices,
        start=args.start,
        end=args.end,
        bitrate=args.bitrate,
        crf=args.crf,
    )


if __name__ == '__main__':
    main()
