"""窗口化字幕生成 — 计划 / 指纹 / 校验（纯函数，REQ-20261107-003）。

把长音频按 VAD 静默间隙切成若干窗口，逐窗口识别、逐窗口落盘：

- **断点续传**：窗口产物 win_XXXXXX.json 带输入指纹，重跑只补缺失/损坏的
  窗口（与粗剪合成的块级 checkpoint 同思路）；
- **质量等价**：窗口边界永远落在 VAD 静默段之间（不切进语音），单窗口内
  仍走与整段推理完全同源的 FunASR 链路（seaco-paraformer + VAD + 标点）；
- **内存可控**：worker 只持有单窗口音频（~600s ≈ 38MB float32），替代原来
  整段 float64（2.66h ≈ 1.2GB），8GB 机器可开多 worker 并行；
- **说话人分离解耦**：cam++ 嵌入/聚类改为识别完成后的**全局独立阶段**
  （同一套 funasr 内部实现：sv_chunk → cam++ → ClusterBackend →
  postprocess → distribute_spk），跨窗口说话人同一性天然保持，并行 ASR
  不再受 SD 约束。

工作目录（任务 outputs 下，成功后清理）::

    outputs/asr_work/
        request.json      # 输入与指纹（video/hotwords/sd/span）
        audio_16k.wav     # 持久化 16k 单声道 wav（断点续跑免重抽）
        vad.json          # 全局 VAD 语音段 [[start_ms, end_ms], ...]
        windows.json      # 窗口计划
        win_000000.json   # 逐窗口识别结果 {fingerprint, sentences}
        sd_embs.npz       # cam++ 逐块嵌入（SD 阶段断点）
        progress_N.json   # 各 worker 进度心跳（父进程轮询）
        state.json        # 最终 sentence_info（与 _run_recognition 同形状）
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# 工作目录名（任务 outputs 下）
WORK_DIR_NAME = "asr_work"
# 单窗口目标跨度（秒）：窗口 = 从首个语音段起点到最后一个语音段终点，
# 加入下一段会超过该跨度就封口。单个超长语音段独占一个窗口（宁超不切）。
WINDOW_SPAN_S = 600.0
# 低于该时长（秒）的音频不走窗口化（单发推理更省事，行为与旧版一致）
WINDOW_MIN_DURATION_S = 600.0

AUDIO_NAME = "audio_16k.wav"
VAD_JSON = "vad.json"
WINDOWS_JSON = "windows.json"
PROGRESS_FMT = "progress_{}.json"
STATE_NAME = "state.json"
REQ_NAME = "request.json"
SD_EMBS_NAME = "sd_embs.npz"


def input_fingerprint(video_path: str, video_size: int, video_mtime_ns: int,
                      sd: bool, hotword_str: str,
                      window_span_s: float = WINDOW_SPAN_S) -> str:
    """识别输入指纹：源视频身份 + 识别参数。任一变化 → 全部窗口作废重算。"""
    h = hashlib.sha1()
    h.update(f"{video_path}|{video_size}|{video_mtime_ns}".encode("utf-8", "replace"))
    h.update(f"|sd={int(bool(sd))}|hw={hotword_str}|w={window_span_s}".encode("utf-8"))
    return h.hexdigest()


def build_windows(vad_ms: list[list[int]],
                  max_span_ms: int = int(WINDOW_SPAN_S * 1000)) -> list[dict]:
    """VAD 语音段 → 窗口计划。窗口边界 = 语音段边界（切在静默里）。

    逐段累计：加入下一段后跨度（新段 end − 窗口首段 start）≤ max_span_ms
    就并入；否则当前窗口封口、新段开新窗口。单个语音段自身超过 max_span
    时独占一个窗口（超长但不切进语音）。无语音段 → 空列表（调用方兜底）。
    """
    windows: list[dict] = []
    cur_start: int | None = None
    cur_end = 0
    for s, e in sorted((int(a), int(b)) for a, b in vad_ms):
        if e <= s:
            continue
        if cur_start is None:
            cur_start, cur_end = s, e
            continue
        if e - cur_start <= max_span_ms:
            cur_end = e
        else:
            windows.append({"i": len(windows), "start_ms": cur_start, "end_ms": cur_end})
            cur_start, cur_end = s, e
    if cur_start is not None:
        windows.append({"i": len(windows), "start_ms": cur_start, "end_ms": cur_end})
    return windows


def write_json_atomic(path: Path, obj) -> None:
    """原子写 JSON（.tmp → replace），并行 worker / 轮询读者都安全。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict | None:
    """读 JSON；不存在/损坏 → None（断点校验按无效处理）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 不存在/半截写/编码坏 → 一律视为无
        return None


def win_path(work_dir: Path, i: int) -> Path:
    return Path(work_dir) / f"win_{i:06d}.json"


def win_is_valid(path: Path, fingerprint: str) -> bool:
    """窗口产物有效性：JSON 可解析 + 指纹匹配 + 有 sentences 键。"""
    d = read_json(path)
    if not isinstance(d, dict):
        return False
    if d.get("fingerprint") != fingerprint:
        return False
    return isinstance(d.get("sentences"), list)


def load_valid_windows(work_dir: Path, fingerprint: str, n_windows: int) -> dict[int, list]:
    """扫描工作目录，收集本指纹下已完成的窗口结果 {i: sentences}。"""
    done: dict[int, list] = {}
    for i in range(n_windows):
        p = win_path(Path(work_dir), i)
        if win_is_valid(p, fingerprint):
            done[i] = read_json(p).get("sentences") or []
    return done


def offset_sentences(sentences: list[dict], start_ms: int) -> list[dict]:
    """窗口本地时间 → 全局时间：timestamp(ms 对)/start/end(ms) 整体平移。

    sentence_info 条目由上游 timestamp_sentence 产出（start/end/timestamp
    均为毫秒，distribute_spk 也按毫秒比较）。缺键/畸形条目原样保留。
    """
    for sent in sentences:
        ts = sent.get("timestamp")
        if isinstance(ts, list):
            off = []
            ok = True
            for pair in ts:
                try:
                    off.append([int(pair[0]) + start_ms, int(pair[1]) + start_ms])
                except (TypeError, ValueError, IndexError):
                    ok = False
                    break
            if ok:
                sent["timestamp"] = off
        for k in ("start", "end"):
            v = sent.get(k)
            try:
                sent[k] = int(v) + start_ms
            except (TypeError, ValueError):
                pass
    return sentences
