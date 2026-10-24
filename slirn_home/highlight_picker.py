"""调用上游 FunClip 的「AI 智能裁剪」管线，从源视频提取精彩片段。

流程（与上游 launch.py: AI_clip 一致）：
    1. ASR（funasr AutoModel）→ state（含 recog_res_raw + timestamp）
    2. LLM 推理（funclip/llm/）→ 文本输出，含 `[HH:MM:SS,mmm-HH:MM:SS,mmm]`
    3. extract_timestamps（funclip/utils/trans_utils.py:113）→ [[start_ms, end_ms], ...]
    4. VideoClipper.video_clip(... timestamp_list=...) → 多个 mp4 片段 + SRT

目的：让 slirn_home 的短视频混剪 UI 能直接复用上游已有的"精彩片段"分析过程，
源视频由用户在 UI 里选（这里只是把上游能力暴露成一个可调用的函数）。

注意：
- funasr 1.3.9 模型注册表只认短名（SeacoParaformer / Paraformer），
  launch.py 用的长路径在这里也会自动注册别名（仿 tools/_launch_upstream.py 的做法）。
- LLM API key 优先用入参 explicit，未传则从环境变量取（按 model 前缀选 key）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ---------- 上游模块装载 ----------

def _ensure_funclip_on_path() -> Path:
    """把 funclip/ 注入 sys.path，让上游平级 import 可解析。"""
    funclip_dir = Path(__file__).resolve().parent.parent / "funclip"
    if not funclip_dir.is_dir():
        raise RuntimeError(f"funclip/ 目录不存在: {funclip_dir}")
    if str(funclip_dir) not in sys.path:
        sys.path.insert(0, str(funclip_dir))
    return funclip_dir


def _patch_funasr_long_names() -> None:
    """funasr 1.3.9 注册表只认短名；给 launch.py / 上游 CLI 用到的长路径注入别名。

    与 tools/_launch_upstream.py 的 patch 等价；此处独立执行是因为 highlight_picker
    既可能从 slirn_home 进程调用、也可能独立 CLI 调用，两条路都需要 patch。
    """
    import funasr  # noqa: F401  触发 @tables.register
    from funasr.register import tables

    aliases = [
        # 上游 launch.py zh 默认
        ("iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
         "SeacoParaformer"),
        # 上游 launch.py en 分支
        ("iic/speech_paraformer_asr-en-16k-vocab4199-pytorch", "Paraformer"),
        # --model sensevoice
        ("iic/SenseVoiceSmall", "SenseVoiceSmall"),
    ]
    for long_name, short_name in aliases:
        cls = tables.model_classes.get(short_name)
        if cls is not None and long_name not in tables.model_classes:
            tables.model_classes[long_name] = cls


def _load_upstream_modules():
    """返回 (VideoClipper 类, extract_timestamps 函数, llm_client_map)。"""
    _ensure_funclip_on_path()
    _patch_funasr_long_names()

    # VideoClipper
    from videoclipper import VideoClipper  # noqa: E402

    # extract_timestamps（funclip/utils/trans_utils.py:113）
    from utils.trans_utils import extract_timestamps  # noqa: E402

    # LLM 客户端
    from llm.openai_api import openai_call  # noqa: E402
    from llm.qwen_api import call_qwen_model  # noqa: E402
    from llm.g4f_openai_api import g4f_openai_call  # noqa: E402

    llm_map = {
        "prefix:qwen": call_qwen_model,
        "prefix:gpt": openai_call,
        "prefix:moonshot": openai_call,
        "prefix:deepseek": openai_call,
        "prefix:g4f": g4f_openai_call,
    }
    return VideoClipper, extract_timestamps, llm_map


# ---------- LLM 派发 ----------

DEFAULT_SYSTEM_PROMPT = (
    "你是一个视频srt字幕分析剪辑器，输入视频的srt字幕，"
    "分析其中的精彩且尽可能连续的片段并裁剪出来，输出四条以内的片段，"
    "将片段中在时间上连续的多个句子及它们的时间戳合并为一条，"
    "注意确保文字与时间戳的正确匹配。输出需严格按照如下格式："
    "1. [开始时间-结束时间] 文本，注意其中的连接符是\"-\""
)

DEFAULT_USER_PROMPT_PREFIX = "这是待裁剪的视频srt字幕："


# ---------- 结果数据结构 ----------

@dataclass
class HighlightClip:
    index: int
    start_ms: int
    end_ms: int
    duration_ms: int
    text: str
    mp4_path: Path | None = None
    srt_path: Path | None = None


@dataclass
class HighlightResult:
    source_video: Path
    model: str
    srt_text: str
    llm_output: str
    timestamp_list: list = field(default_factory=list)        # [[start_ms, end_ms], ...]
    highlights: list = field(default_factory=list)             # List[HighlightClip]
    log_lines: list = field(default_factory=list)


# ---------- 工具函数 ----------

def _resolve_llm_client(model: str, llm_map: dict):
    for prefix, fn in llm_map.items():
        if model.startswith(prefix.split(":", 1)[1]):
            return fn
    raise ValueError(
        f"未支持的 LLM model 前缀: {model!r}（已知：qwen/gpt/moonshot/deepseek/g4f）"
    )


def _resolve_api_key(model: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if model.startswith("qwen"):
        return os.environ.get("DASHSCOPE_API_KEY")
    if model.startswith("deepseek"):
        return os.environ.get("DEEPSEEK_API_KEY")
    if model.startswith("gpt") or model.startswith("moonshot"):
        return os.environ.get("OPENAI_API_KEY") or os.environ.get("MOONSHOT_API_KEY")
    # g4f 无需 key
    return None


def _srt_text_from_state(state: dict) -> str:
    """从 recog 返回的 state 里抽出可读的 SRT 文本。

    优先用 state["sentences"]（list[dict]，每个含 text + timestamp）；
    若空再退到 state["timestamp"]（token 级 [[start_ms, end_ms], ...]，无 text）。
    """
    sentences = state.get("sentences") or []
    if sentences and isinstance(sentences[0], dict):
        lines = []
        idx = 1
        for sent in sentences:
            text = sent.get("text")
            if isinstance(text, list):
                text = "".join(text)
            text = (text or "").strip()
            if not text:
                continue
            ts = sent.get("timestamp") or []
            if not ts:
                continue
            start = ts[0][0]
            end = ts[-1][1]
            lines.append(f"{idx}")
            lines.append(f"{_ms_to_srt(start)} --> {_ms_to_srt(end)}")
            lines.append(text)
            lines.append("")
            idx += 1
        if lines:
            return "\n".join(lines)

    # 退化：state["timestamp"] 是 token 级 [[start_ms, end_ms], ...]，无文本
    timestamp = state.get("timestamp") or []
    return "\n".join(
        f"{i}\n{_ms_to_srt(ts[0])} --> {_ms_to_srt(ts[1])}"
        for i, ts in enumerate(timestamp, start=1)
        if isinstance(ts, (list, tuple)) and len(ts) >= 2
    )


def _ms_to_srt(ms: int) -> str:
    h, rem = divmod(ms, 3600 * 1000)
    m, rem = divmod(rem, 60 * 1000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _ffmpeg_to_wav16k_mono(video_path: Path) -> Path:
    """用 ffmpeg 直接抽 16kHz 单声道 wav，绕开 MoviePy 的 write_audiofile。

    MoviePy 的 audio.write_audiofile 在本机对某些短视频会触发
    numpy._ArrayMemoryError（即使系统空闲内存够），ffmpeg 直接抽干净稳定。
    """
    out = Path(tempfile.gettempdir()) / f"_hlp_{os.getpid()}_{video_path.stem}.wav"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
        str(out),
    ]
    log.info("[highlight] ffmpeg audio extract: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    if not out.is_file() or out.stat().st_size < 44:
        raise RuntimeError(f"ffmpeg 抽 wav 失败或空: {out}")
    return out


# ---------- 主流程 ----------

def pick_highlights(
    source_video: str | Path,
    model: str = "deepseek-chat",
    api_key: str | None = None,
    output_dir: str | Path | None = None,
    sd_switch: str = "No",
    hotwords: str = "",
    lang: str = "zh",
    max_highlights: int = 4,
    start_ost: int = 0,
    end_ost: int = 100,
    system_prompt: str | None = None,
    user_prompt_prefix: str | None = None,
    progress_cb=None,
) -> HighlightResult:
    """调用上游"AI 智能裁剪"管线，返回精彩片段。

    Args:
        source_video: 源视频路径（用户在 slirn_home 短视频混剪 UI 里选的）
        model: LLM 模型名（上游支持：qwen / gpt / moonshot / deepseek / g4f 前缀）
        api_key: 显式 key；未传则按 model 前缀从环境变量取
        output_dir: 产物输出目录；未传则在源视频旁建 `_highlights_<basename>/`
        sd_switch: 'Yes' / 'No'，是否带说话人区分
        hotwords: 上游 ASR 热词
        lang: 'zh' / 'en'
        max_highlights: 最多取几条（与 prompt 中的"四条以内"一致）
        start_ost / end_ost: video_clip 的起止偏移（ms），与上游 Gradio Slider 一致
        system_prompt / user_prompt_prefix: 自定义 prompt；未传用上游默认值
        progress_cb: 可选回调 fn(stage: str, info: dict)

    Returns:
        HighlightResult（含 highlights 列表，每条带 mp4_path + srt_path）
    """
    source = Path(source_video).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"源视频不存在: {source}")

    if output_dir is None:
        output_dir = source.parent / f"_highlights_{source.stem}"
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    sys_p = system_prompt or DEFAULT_SYSTEM_PROMPT
    user_p = user_prompt_prefix or DEFAULT_USER_PROMPT_PREFIX

    log_lines: list[str] = []
    result = HighlightResult(source_video=source, model=model, srt_text="", llm_output="")

    def _step(stage: str, info: dict | None = None):
        msg = f"[highlight] {stage}"
        if info:
            msg += f" {info}"
        log_lines.append(msg)
        log.info(msg)
        if progress_cb:
            try:
                progress_cb(stage, info or {})
            except Exception as cb_err:
                log.warning("progress_cb raised: %s", cb_err)

    # ---- 加载上游 ----
    _step("loading upstream modules")
    VideoClipper, extract_timestamps, llm_map = _load_upstream_modules()

    # ---- ASR（funasr AutoModel）----
    _step("loading funasr AutoModel (paraformer-zh)")
    from funasr import AutoModel
    if lang == "zh":
        asr_model_id = "paraformer-zh"
    elif lang == "en":
        asr_model_id = "paraformer-en"
    else:
        raise ValueError(f"unsupported lang: {lang}")

    funasr_model = AutoModel(
        model=asr_model_id,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
        punc_model="ct-punc-c",
        spk_model="cam++" if sd_switch == "Yes" else None,
    )
    audio_clipper = VideoClipper(funasr_model)
    audio_clipper.lang = lang

    _step("running ASR", {"file": str(source)})
    # 不走 video_recog（MoviePy write_audiofile 会爆内存），改走：
    #   ffmpeg 抽 16k mono wav → librosa 读 (sr,data) → recog 直接收 tuple
    # video_clip() 后续还要 state['video']，所以同时 load VideoFileClip 放进 state。
    wav_path = _ffmpeg_to_wav16k_mono(source)
    try:
        import librosa
        wav_data, wav_sr = librosa.load(str(wav_path), sr=16000)
    finally:
        try:
            wav_path.unlink()
        except OSError:
            pass

    import moviepy.editor as mpy
    state = {
        "video_filename": str(source),
        "clip_video_file": str(out / f"{source.stem}_clip.mp4"),
        "video": mpy.VideoFileClip(str(source)),
    }
    rec_result, rec_res_raw, state = audio_clipper.recog(
        (wav_sr, wav_data), sd_switch, state, hotwords, output_dir=str(out),
    )
    srt_text = _srt_text_from_state(state)
    result.srt_text = srt_text
    (out / "source.srt").write_text(srt_text, encoding="utf-8")
    _step("ASR done", {"srt_chars": len(srt_text), "state_keys": list(state.keys())})

    if not srt_text.strip():
        _step("ASR produced empty SRT — aborting")
        return result

    # ---- LLM 推理 ----
    llm_fn = _resolve_llm_client(model, llm_map)
    key = _resolve_api_key(model, api_key)
    if key is None and not model.startswith("g4f"):
        raise RuntimeError(
            f"未找到 LLM API key（model={model}）。请传 api_key 或设置环境变量"
        )

    _step("calling LLM", {"model": model})
    # 上游 llm 客户端签名：(apikey, model, user_content, system_content)
    # g4f 例外：(model, system_content, user_content) — 在 _call_llm 里适配
    user_content = f"{user_p}\n{srt_text}"
    system_content = sys_p

    if model.startswith("g4f"):
        # g4f_openai_call(model=..., system_content=..., user_content=...)
        llm_output = llm_fn(model=model, system_content=system_content, user_content=user_content)
    elif model.startswith("qwen"):
        llm_output = llm_fn(key=key, model=model, user_content=user_content, system_content=system_content)
    else:
        # openai_call(apikey, model, user_content, system_content)
        llm_output = llm_fn(apikey=key, model=model, user_content=user_content, system_content=system_content)

    result.llm_output = llm_output
    (out / "llm_output.txt").write_text(llm_output, encoding="utf-8")
    _step("LLM done", {"output_chars": len(llm_output)})

    # ---- extract_timestamps ----
    timestamp_list = extract_timestamps(llm_output)
    timestamp_list = timestamp_list[:max_highlights]
    result.timestamp_list = timestamp_list
    _step("timestamps extracted", {"count": len(timestamp_list)})

    if not timestamp_list:
        _step("no timestamps extracted — aborting")
        return result

    # ---- video_clip 按时间戳切 ----
    _step("clipping highlights via VideoClipper.video_clip")
    highlights: list[HighlightClip] = []
    for idx, (start_ms, end_ms) in enumerate(timestamp_list, start=1):
        # 每个片段一个 timestamp_list 单元（video_clip 内部循环 all_ts）
        single_ts = [[start_ms, end_ms]]
        clip_video_file, message, clip_srt_text = audio_clipper.video_clip(
            None, start_ost, end_ost, state,
            output_dir=str(out),
            timestamp_list=single_ts,
            add_sub=False,
        )
        mp4 = Path(clip_video_file) if clip_video_file else None
        # 上游 video_clip 返回的 clip_srt 是 SRT 文本内容（不是路径），
        # 按"序号_起止.srt"命名写到 out/ 方便后续引用。
        srt_path = None
        if clip_srt_text:
            srt_path = out / f"highlight_{idx:02d}_{start_ms}-{end_ms}ms.srt"
            srt_path.write_text(clip_srt_text, encoding="utf-8")
        # 抽 LLM 输出中此片段对应的文本行
        text_match = _extract_clip_text(llm_output, start_ms, end_ms)
        highlights.append(HighlightClip(
            index=idx,
            start_ms=start_ms,
            end_ms=end_ms,
            duration_ms=end_ms - start_ms,
            text=text_match,
            mp4_path=mp4,
            srt_path=srt_path,
        ))
        _step(f"clip {idx}/{len(timestamp_list)}", {
            "start_ms": start_ms, "end_ms": end_ms,
            "mp4": str(mp4) if mp4 else None,
            "srt": str(srt_path) if srt_path else None,
        })

    result.highlights = highlights
    _step("done", {"highlights": len(highlights), "output_dir": str(out)})
    return result


def _extract_clip_text(llm_output: str, start_ms: int, end_ms: int) -> str:
    """从 LLM 输出里找出与 [start-end] 同一行的文本。"""
    s = _ms_to_srt(start_ms)
    e = _ms_to_srt(end_ms)
    # LLM 格式：1. [HH:MM:SS,mmm-HH:MM:SS,mmm] 文本
    pat = re.compile(
        rf"\[\s*{re.escape(s)}\s*-\s*{re.escape(e)}\s*\]\s*(.+)"
    )
    m = pat.search(llm_output)
    if m:
        return m.group(1).strip()
    # 退化：仅按起止毫秒数近似匹配
    pat2 = re.compile(rf"\[\s*{start_ms}\s*-\s*{end_ms}\s*\]\s*(.+)")
    m = pat2.search(llm_output)
    return m.group(1).strip() if m else ""


# ---------- CLI ----------

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="调用上游 AI 智能裁剪管线")
    ap.add_argument("video", help="源视频路径")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--api-key", default=None,
                    help="显式 API key；未传按 model 前缀从环境变量取")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--sd", choices=["Yes", "No"], default="No")
    ap.add_argument("--hotwords", default="")
    ap.add_argument("--lang", choices=["zh", "en"], default="zh")
    ap.add_argument("--max", type=int, default=4)
    ap.add_argument("--start-ost", type=int, default=0)
    ap.add_argument("--end-ost", type=int, default=100)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    r = pick_highlights(
        source_video=args.video,
        model=args.model,
        api_key=args.api_key,
        output_dir=args.output_dir,
        sd_switch=args.sd,
        hotwords=args.hotwords,
        lang=args.lang,
        max_highlights=args.max,
        start_ost=args.start_ost,
        end_ost=args.end_ost,
    )
    print("\n========== 高光片段 ==========")
    for h in r.highlights:
        print(f"[{h.index}] {h.start_ms}-{h.end_ms}ms ({h.duration_ms}ms) "
              f"→ {h.mp4_path}")
        print(f"    文本：{h.text}")
    print(f"\nLLM 输出：\n{r.llm_output[:600]}")
    print(f"\n输出目录：{Path(args.output_dir or (Path(args.video).parent / f'_highlights_{Path(args.video).stem}'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
