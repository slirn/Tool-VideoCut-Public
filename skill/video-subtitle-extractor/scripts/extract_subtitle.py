#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
"""
从视频文件提取 SRT 字幕。
支持多个视频文件批量处理，输出独立 SRT 文件。

用法:
    python extract_subtitle.py --input video.mp4 --output video.srt
    python extract_subtitle.py --input video.mp4 --output video.srt --model sensevoice
    python extract_subtitle.py --input video1.mp4 video2.mp4 --output-dir ./subs
"""

import argparse
import os
import sys
import logging
import subprocess
from datetime import datetime

def _find_funiclip_root(start_dir: str) -> str:
    """向上查找「含 funclip/videoclipper.py 的祖先目录」= 仓库根。

    REQ-20261021-139 / D1：本仓 2026-10-21 由 FunClip-main + slirn-standalone
    合并为单工程后，skill 目录从 ``slirn/skill/<name>/scripts/``（子模块，
    仓库根在第 4 级）平铺为 ``skill/<name>/scripts/``（仓库根在第 3 级）——
    固定层级数推导只对其中一种布局成立。

    改为向上搜索「含 ``funclip/videoclipper.py`` 的目录」：这是「本仓包含
    上游 fork」这一不变式，比相对层级稳定，新旧两种布局同时成立。
    搜索失败直接抛 ImportError（旧的 4-up 回退是死代码：向上搜索已遍历
    全部祖先目录，回退目标必然也搜过且未命中——REQ-20261021-139 评审 R2）。
    """
    cur = os.path.abspath(start_dir)
    while True:
        if os.path.isfile(os.path.join(cur, 'funclip', 'videoclipper.py')):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:  # 到文件系统根还没找到
            break
        cur = parent
    raise ImportError(
        f"找不到 funclip 包：从 {start_dir} 向上搜索 funclip/videoclipper.py 失败。"
        "请确认在 VideoCut 仓库内运行本脚本。"
    )


# 确保 funclip 模块在路径中（支持从任意目录运行）
_script_dir = os.path.dirname(os.path.abspath(__file__))
_funiclip_root = _find_funiclip_root(_script_dir)
_funclip_pkg_dir = os.path.join(_funiclip_root, 'funclip')

for path in (_funiclip_root, _funclip_pkg_dir):
    if path not in sys.path:
        sys.path.insert(0, path)

from funclip.videoclipper import VideoClipper
from funasr import AutoModel


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def build_model(model_name='paraformer', lang='zh', use_spk=False):
    """初始化 FunASR AutoModel"""
    if model_name == 'fun-asr-nano':
        kwargs = dict(
            model="FunAudioLLM/Fun-ASR-Nano-2512",
            trust_remote_code=True,
            remote_code="./model.py",
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            hub="hf",
        )
        if use_spk:
            kwargs["spk_model"] = "cam++"
        return AutoModel(**kwargs)
    elif model_name == 'sensevoice':
        kwargs = dict(
            model="iic/SenseVoiceSmall",
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
        )
        if use_spk:
            kwargs["spk_model"] = "cam++"
        return AutoModel(**kwargs)
    else:  # paraformer 默认
        if lang == 'en':
            kwargs = dict(
                model="iic/speech_paraformer_asr-en-16k-vocab4199-pytorch",
                vad_model="damo/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                punc_model="damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
            )
            if use_spk:
                kwargs["spk_model"] = "damo/speech_campplus_sv_zh-cn-16k-common"
            return AutoModel(**kwargs)
        else:
            kwargs = dict(
                model="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
                vad_model="damo/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                punc_model="damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
            )
            if use_spk:
                kwargs["spk_model"] = "damo/speech_campplus_sv_zh-cn-16k-common"
            return AutoModel(**kwargs)


def recog_video(clipper, video_path, sd_switch='no', hotwords='', output_dir=None):
    """识别视频音频，跳过上游 ``VideoClipper.video_recog`` 的 moviepy 路径。

    REQ-20261016-125：``video_recog`` 用 moviepy 写 44.1kHz 立体声临时 wav，
    ``librosa.load`` 再一次性读入整段 float32——1.7h 立体声就是 2GiB 的
    ``np.empty``，系统提交内存不足时直接 ``_ArrayMemoryError``（退出码 1），
    内存临界时还会触发原生层 0xC0000005（退出码 3221225477）。
    这里改为 ffmpeg 直抽 16kHz 单声道 wav（体积/内存约 1/11）后直接调
    ``clipper.recog``，识别结果与上游路径一致。
    """
    import librosa

    work_dir = output_dir or os.path.dirname(os.path.abspath(video_path))
    os.makedirs(work_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    wav_path = os.path.join(work_dir, base + '_mono16k_tmp.wav')

    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-ac', '1', '-ar', '16000', '-f', 'wav', wav_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    wav_ok = os.path.exists(wav_path) and os.path.getsize(wav_path) > 44
    if proc.returncode != 0 or not wav_ok:
        if os.path.exists(wav_path):
            os.remove(wav_path)
        tail = (proc.stderr or '')[-500:]
        raise RuntimeError(f"No audio information found or ffmpeg failed: {tail}")

    try:
        wav = librosa.load(wav_path, sr=16000)[0]
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)

    state = {'video_filename': video_path}
    return clipper.recog(
        (16000, wav), sd_switch=sd_switch, state=state,
        hotwords=hotwords, output_dir=output_dir,
    )


def normalize_srt(srt_text):
    """
    规范化 SRT 格式：
    - 替换所有 \\r\\n、\\r、\\n 为统一换行符
    - 字幕块之间只留一个空行
    - 字幕块内部（序号、时间戳、文本）不空行
    - 不添加尾部多余空行
    """
    import re

    # 统一换行符
    text = srt_text.replace('\r\n', '\n').replace('\r', '\n')

    # 按行分割，收集字幕块
    lines = text.split('\n')
    blocks = []
    current_block_lines = []

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            # 空行：结束当前字幕块
            if current_block_lines:
                blocks.append('\n'.join(current_block_lines))
                current_block_lines = []
            continue
        if re.match(r'^\d+$', line_stripped):
            # 序号行：开始新字幕块
            if current_block_lines:
                blocks.append('\n'.join(current_block_lines))
                current_block_lines = []
            current_block_lines.append(line_stripped)
        else:
            current_block_lines.append(line_stripped)

    if current_block_lines:
        blocks.append('\n'.join(current_block_lines))

    # 字幕块之间单空行连接，无尾部空行
    return '\n\n'.join(blocks)


def extract_subtitle(video_path, output_path, model_name='paraformer', lang='zh', hotwords='', sd_switch='no'):
    """
    从单个视频提取字幕

    Args:
        video_path: 视频文件路径
        output_path: 输出 SRT 文件路径
        model_name: ASR 模型名 ('paraformer' | 'fun-asr-nano' | 'sensevoice')
        lang: 语言 ('zh' | 'en')
        hotwords: 热词字符串，空格分隔
        sd_switch: 是否区分说话人 ('yes' | 'no')
    """
    logger.info(f"Initializing {model_name} model...")
    funasr_model = build_model(model_name, lang, use_spk=(sd_switch == 'yes'))

    clipper = VideoClipper(funasr_model)
    clipper.lang = lang

    logger.info(f"Processing: {video_path}")
    res_text, res_srt, state = recog_video(
        clipper,
        video_path,
        sd_switch=sd_switch,
        hotwords=hotwords,
        output_dir=os.path.dirname(output_path) or None,
    )

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    # 格式化 SRT：统一换行符，单字幕块内不空行，块之间单空行
    formatted_srt = normalize_srt(res_srt)

    with open(output_path, 'w', encoding='utf-8-sig') as f:
        f.write(formatted_srt)

    logger.info(f"SRT saved to: {output_path}")
    return output_path


def batch_extract(video_paths, output_dir, model_name='paraformer', lang='zh', hotwords='', sd_switch='no'):
    """
    批量从多个视频提取字幕
    每个视频输出同名 .srt 文件到 output_dir
    """
    os.makedirs(output_dir, exist_ok=True)

    funasr_model = build_model(model_name, lang, use_spk=(sd_switch == 'yes'))
    clipper = VideoClipper(funasr_model)
    clipper.lang = lang

    for video_path in video_paths:
        basename = os.path.splitext(os.path.basename(video_path))[0]
        output_path = os.path.join(output_dir, f"{basename}.srt")

        logger.info(f"Processing: {video_path} -> {output_path}")
        try:
            res_text, res_srt, state = recog_video(
                clipper,
                video_path,
                sd_switch=sd_switch,
                hotwords=hotwords,
                output_dir=output_dir,
            )
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(res_srt)
            logger.info(f"Saved: {output_path}")
        except Exception as e:
            logger.error(f"Failed to process {video_path}: {e}")
            continue

    logger.info(f"Batch extraction complete. Output dir: {output_dir}")


def get_parser():
    parser = argparse.ArgumentParser(
        description='从视频文件提取 SRT 字幕',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--input', '-i',
        nargs='+',
        required=True,
        help='输入视频文件路径，支持单个或多个文件'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        default=None,
        help='单个视频时指定输出 SRT 路径（默认与视频同名）'
    )
    parser.add_argument(
        '--output-dir',
        type=str,
        default=None,
        help='批量处理时指定输出目录'
    )
    parser.add_argument(
        '--model', '-m',
        type=str,
        default='paraformer',
        choices=['paraformer', 'fun-asr-nano', 'sensevoice'],
        help='ASR 模型（默认 paraformer）'
    )
    parser.add_argument(
        '--lang', '-l',
        type=str,
        default='zh',
        choices=['zh', 'en'],
        help='语言（默认中文）'
    )
    parser.add_argument(
        '--hotwords',
        type=str,
        default='',
        help='热词，多个用空格分隔（仅支持中文）'
    )
    parser.add_argument(
        '--sd-switch',
        type=str,
        default='no',
        choices=['yes', 'no'],
        help='是否区分说话人（默认否）'
    )
    return parser


def main():
    args = get_parser().parse_args()

    # 单个视频，指定输出路径
    if len(args.input) == 1 and args.output:
        extract_subtitle(
            video_path=args.input[0],
            output_path=args.output,
            model_name=args.model,
            lang=args.lang,
            hotwords=args.hotwords,
            sd_switch=args.sd_switch,
        )
    # 单个视频，输出路径从输入推导
    elif len(args.input) == 1:
        video_path = args.input[0]
        if args.output_dir:
            basename = os.path.splitext(os.path.basename(video_path))[0]
            output_path = os.path.join(args.output_dir, f"{basename}.srt")
        else:
            output_path = os.path.splitext(video_path)[0] + '.srt'
        extract_subtitle(
            video_path=video_path,
            output_path=output_path,
            model_name=args.model,
            lang=args.lang,
            hotwords=args.hotwords,
            sd_switch=args.sd_switch,
        )
    # 多个视频
    else:
        if args.output_dir is None:
            raise ValueError("批量处理时必须指定 --output-dir")
        batch_extract(
            video_paths=args.input,
            output_dir=args.output_dir,
            model_name=args.model,
            lang=args.lang,
            hotwords=args.hotwords,
            sd_switch=args.sd_switch,
        )


if __name__ == '__main__':
    main()
