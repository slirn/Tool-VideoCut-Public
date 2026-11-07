"""窗口化字幕生成 — worker 端阶段机（REQ-20261107-003）。

由 ``asr_service._run_recognition_windowed`` 以
``python -m slirn_home.asr_worker --pipeline --work-dir ... --idx i 启动，
N 个 worker 进程协作完成一次识别：

    extract → 全局 VAD → 窗口划分 → 逐窗口 ASR（并行/断点）→ 全局 SD → 合并

- worker 0 独占 extract / VAD / 划分 / SD / 合并；其余 worker 只跑自己
  名下的窗口（``i % num_workers == idx``）后即退出。
- 所有阶段产物落在 asr_windows.WORK_DIR_NAME 工作目录，逐窗口落盘 +
  指纹校验 ⇒ 中断后重跑只补缺失窗口（断点续传）。
- 进度以 progress_{idx}.json 心跳文件外发，父进程轮询汇总（不碰 torch，
  8GB 部署机的 app 进程保持轻量）。
- 说话人分离是识别完成后的**全局独立阶段**：sv_chunk → cam++ 嵌入 →
  ClusterBackend（谱聚类/HDBSCAN）→ postprocess → distribute_spk，
  与 FunASR 内部实现同源（funasr.models.campplus.*），说话人同一性跨
  窗口天然保持。嵌入逐批落盘 sd_embs.npz，SD 阶段也能断点。

模型加载刻意延迟到首次需要（torch/funasr ~1-2GB 只出现在本进程）；
idx>0 的 worker 在等待共享产物期间后台预加载 ASR 模型，缩短整体墙钟。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from slirn_home import asr_windows as aw
from slirn_home.asr_windows import read_json, write_json_atomic

log = logging.getLogger(__name__)

_SD_EMB_BATCH = 64          # cam++ 嵌入逐批提交大小
_WAIT_INTERVAL_S = 3.0      # 轮询共享产物 / 心跳间隔


# =============== 音频读取 ===============


def _read_wav_slice(audio_path: Path, start_ms: int, end_ms: int) -> np.ndarray:
    """按毫秒区间随机读 wav（soundfile seek，不整读）→ float32 (-1,1)。"""
    import soundfile as sf

    with sf.SoundFile(str(audio_path)) as f:
        sr = f.samplerate
        start = max(0, int(start_ms * sr / 1000))
        count = max(0, int((end_ms - start_ms) * sr / 1000))
        if count == 0:
            return np.zeros(0, dtype="float32")
        f.seek(start)
        data = f.read(count, dtype="int16", always_2d=False)
    if data.ndim == 2:  # 保险：强制单声道
        data = data[:, 0]
    return data.astype("float32") / 32768.0


def _extract_audio(video_path: str, audio_path: Path) -> None:
    """ffmpeg 流式抽 16k 单声道 wav（复用 asr_service 实现，落盘到工作目录）。"""
    from slirn_home.asr_service import _extract_mono_wav_16k

    _extract_mono_wav_16k(str(video_path), audio_path)


def _audio_ok(audio_path: Path) -> bool:
    if not audio_path.is_file() or audio_path.stat().st_size <= 44:
        return False
    try:
        import soundfile as sf

        with sf.SoundFile(str(audio_path)) as f:
            return f.frames > 0 and f.samplerate == 16000
    except Exception:  # noqa: BLE001
        return False


# =============== 进度心跳 ===============


def _heartbeat(work_dir: Path, idx: int, stage: str,
               done: int = 0, total: int = 0) -> None:
    try:
        write_json_atomic(
            Path(work_dir) / aw.PROGRESS_FMT.format(idx),
            {"stage": stage, "done": int(done), "total": int(total),
             "updated_at": time.time()})
    except OSError as e:  # noqa: BLE001 — 心跳失败不影响识别
        log.warning("[asr_pipeline] worker %d 心跳写盘失败: %s", idx, e)


def _wait_until(pred, work_dir: Path, idx: int, stage: str,
                timeout_s: float = 7200) -> None:
    """轮询等待共享产物就绪；期间持续心跳（父进程可见 worker 未僵死）。"""
    t0 = time.time()
    while not pred():
        if time.time() - t0 > timeout_s:
            raise RuntimeError(f"worker {idx} 等待 {stage} 超时（>{timeout_s}s）")
        _heartbeat(work_dir, idx, f"等待 {stage}")
        time.sleep(_WAIT_INTERVAL_S)


# =============== 各阶段 ===============


def _stage_vad(work_dir: Path, idx: int, audio_path: Path) -> None:
    """全局 VAD：fsmn-vad 单趟过整段音频 → vad.json（毫秒段列表）。"""
    vad_path = Path(work_dir) / aw.VAD_JSON
    if _vad_ok(vad_path, _req_fp(work_dir)):
        return
    import soundfile as sf

    from slirn_home.asr_service import _local_model_dir

    from funasr import AutoModel

    t0 = time.time()
    vad = AutoModel(model=_local_model_dir("damo/speech_fsmn_vad_zh-cn-16k-common-pytorch"))
    with sf.SoundFile(str(audio_path)) as f:
        sr = f.samplerate
        data = f.read(-1, dtype="float32", always_2d=False)
    if data.ndim == 2:
        data = data[:, 0]
    res = vad.generate(input=data, fs=sr, cache={})
    segs = [[int(s), int(e)] for s, e in (res[0].get("value") or [])]
    write_json_atomic(vad_path, {"fingerprint": _req_fp(work_dir), "segments": segs})
    log.info("[asr_pipeline] VAD 完成：%d 段，用时 %.1fs", len(segs), time.time() - t0)


def _vad_ok(vad_path: Path, fingerprint: str | None = None) -> bool:
    d = read_json(vad_path)
    if not (d and isinstance(d.get("segments"), list)):
        return False
    return fingerprint is None or d.get("fingerprint") == fingerprint


def _req_fp(work_dir: Path) -> str:
    req = read_json(Path(work_dir) / aw.REQ_NAME) or {}
    return str(req.get("fingerprint") or "")


def _stage_windows(work_dir: Path, vad_path: Path) -> list[dict]:
    """窗口划分：VAD 段 → windows.json（无语音段 → 整段单窗口兜底）。"""
    win_path = Path(work_dir) / aw.WINDOWS_JSON
    d = read_json(win_path)
    if d and isinstance(d.get("windows"), list) and d.get("fingerprint") == _req_fp(work_dir):
        return d["windows"]
    vad = read_json(vad_path) or {}
    windows = aw.build_windows(vad.get("segments") or [])
    if not windows:
        # VAD 没识别出语音：单窗口兜底（识别结果自然为空，与旧路径等价）
        import soundfile as sf

        audio = Path(work_dir) / aw.AUDIO_NAME
        with sf.SoundFile(str(audio)) as f:
            dur_ms = int(f.frames * 1000 / f.samplerate)
        windows = [{"i": 0, "start_ms": 0, "end_ms": dur_ms}]
    write_json_atomic(win_path, {"fingerprint": _req_fp(work_dir), "windows": windows})
    return windows


def _stage_asr_windows(work_dir: Path, idx: int, windows: list[dict]) -> None:
    """逐窗口识别（跳过已完成）：VideoClipper.recog 单窗口版 + 时间平移。"""
    req = read_json(Path(work_dir) / aw.REQ_NAME) or {}
    fp = str(req.get("fingerprint") or "")
    hotwords = str(req.get("hotwords") or "")
    num_workers = int(req.get("num_workers") or 1)
    audio_path = Path(work_dir) / aw.AUDIO_NAME

    mine = [w for w in windows if w["i"] % num_workers == idx]
    todo = [w for w in mine if not aw.win_is_valid(aw.win_path(work_dir, w["i"]), fp)]
    log.info("[asr_pipeline] worker %d：认领 %d 窗口（缺 %d）",
             idx, len(mine), len(todo))

    clipper = None
    done = len(mine) - len(todo)
    for w in mine:
        wp = aw.win_path(work_dir, w["i"])
        if aw.win_is_valid(wp, fp):
            continue
        if clipper is None:
            clipper = _build_clipper()
        t0 = time.time()
        audio = _read_wav_slice(audio_path, w["start_ms"], w["end_ms"])
        state = {"video_filename": req.get("video") or ""}
        _res_text, _res_srt, state = clipper.recog(
            (16000, audio), "no", state, hotwords, None)
        sentences = aw.offset_sentences(
            state.get("sentences") or [], int(w["start_ms"]))
        write_json_atomic(wp, {"fingerprint": fp, "i": w["i"],
                               "sentences": sentences})
        done += 1
        _heartbeat(work_dir, idx, "识别中", done, len(mine))
        log.info("[asr_pipeline] worker %d 窗口 %d/%d 完成（%d 句，%.1fs）",
                 idx, done, len(mine), len(sentences), time.time() - t0)


def _build_clipper():
    """ASR(+VAD+标点，不带 spk) 单例 → VideoClipper（窗口识别用）。"""
    import funclip as _fc

    # funclip/videoclipper.py 以裸包名导入同目录的 utils/ —— 生产路径由
    # `python funclip/launch.py` 的脚本目录语义提供；`-m slirn_home.asr_worker`
    # 子进程没有这条路径，这里显式补上（幂等）
    d = str(Path(_fc.__file__).resolve().parent)
    if d not in sys.path:
        sys.path.insert(0, d)

    from funclip.videoclipper import VideoClipper

    from slirn_home import asr_service

    clipper = VideoClipper(asr_service.get_model(sd=False))
    clipper.lang = "zh"
    return clipper


def _stage_sd(work_dir: Path, windows: list[dict], sentences: list[dict]) -> None:
    """全局说话人分离：sv_chunk → cam++ 嵌入（断点）→ 聚类 → 贴标签。"""
    req = read_json(Path(work_dir) / aw.REQ_NAME) or {}
    fp = str(req.get("fingerprint") or "")
    vad = read_json(Path(work_dir) / aw.VAD_JSON) or {}
    vad_ms = [tuple(map(int, seg)) for seg in (vad.get("segments") or [])]
    if not vad_ms or not sentences:
        return

    from funasr.models.campplus.cluster_backend import ClusterBackend
    from funasr.models.campplus.utils import distribute_spk, postprocess, sv_chunk

    from slirn_home.asr_service import _local_model_dir

    audio_path = Path(work_dir) / aw.AUDIO_NAME
    t0 = time.time()
    log.info("[asr_pipeline] SD：VAD %d 段 → sv_chunk…", len(vad_ms))
    vad_segments = [[s / 1000.0, e / 1000.0, _read_wav_slice(audio_path, s, e)]
                    for s, e in vad_ms]
    segments = sv_chunk(vad_segments)  # [[start_sec, end_sec, chunk_audio], ...]
    n_chunks = len(segments)
    log.info("[asr_pipeline] SD：%d 个嵌入块，cam++ 提取中…", n_chunks)

    # 嵌入断点：npz 存已完成块数 N（块序确定性 ⇒ 前缀续跑安全）
    emb_path = Path(work_dir) / aw.SD_EMBS_NAME
    embs = None
    if emb_path.is_file():
        try:
            z = np.load(emb_path)
            if str(z.get("fingerprint")) == fp:
                embs = z["embs"]
        except Exception as e:  # noqa: BLE001 — 损坏断点按重来
            log.warning("[asr_pipeline] SD 嵌入断点损坏，重算: %s", e)
    have = int(embs.shape[0]) if embs is not None else 0
    if have > n_chunks:  # 指纹一致但块数缩了（理论不该发生）→ 重算
        embs, have = None, 0

    campplus = None
    if have < n_chunks:
        from funasr import AutoModel

        campplus = AutoModel(
            model=_local_model_dir("damo/speech_campplus_sv_zh-cn_16k-common"))
    if embs is None:
        embs = np.zeros((0, 192), dtype="float32")
    bi = have
    while bi < n_chunks:
        batch = segments[bi:bi + _SD_EMB_BATCH]
        inputs = [b[2] for b in batch]
        # AutoModel 逐条调 inference → res 是每输入一项的列表（各 (1,192)），
        # 必须全部拼接（2026-11-07 e2e 发现：取 res[0] 每批只收 1 个嵌入，
        # 行数≠块数 → postprocess assert 崩）
        parts = []
        for r in campplus.inference(
                inputs, input_len=None, key=["x"] * len(inputs),
                device="cpu", fs=16000):
            t = r["spk_embedding"].detach().cpu().numpy().astype("float32")
            parts.append(t if t.ndim == 2 else t.reshape(1, -1))
        emb = np.concatenate(parts, axis=0) if parts else np.zeros(
            (0, 192), dtype="float32")
        embs = np.concatenate([embs, emb], axis=0)
        bi += len(batch)
        if embs.shape[0] != bi:  # 嵌入数必须逐块对齐 — 断点前缀续跑的前提
            raise RuntimeError(
                f"SD 嵌入数错位：{embs.shape[0]} != 已处理块数 {bi}")
        np.savez(emb_path, fingerprint=fp, embs=embs)
        _heartbeat(work_dir, 0, "说话人分离", bi, n_chunks)

    if embs.shape[0] != n_chunks:
        raise RuntimeError(
            f"SD 嵌入数 {embs.shape[0]} != 块数 {n_chunks}（断点或分批异常）")

    labels = ClusterBackend()(embs, oracle_num=None)
    sd_time = postprocess(segments, None, labels, embs)
    distribute_spk(sentences, sd_time)
    log.info("[asr_pipeline] SD 完成：%d 块 → %d 位说话人，用时 %.1fs",
             n_chunks, int(max(labels) + 1 if len(labels) else 0), time.time() - t0)


def _merge_sentences(work_dir: Path, windows: list[dict]) -> list[dict]:
    """各窗口 sentences 按（窗口序=时间序）顺序合并。"""
    fp = _req_fp(work_dir)
    merged: list[dict] = []
    for w in windows:
        d = read_json(aw.win_path(work_dir, w["i"]))
        if d and d.get("fingerprint") == fp:
            merged.extend(d.get("sentences") or [])
    return merged


# =============== 主入口 ===============


def run_pipeline(work_dir: Path, idx: int) -> int:
    """worker idx 的完整阶段机。退出码 0 = 本 worker 职责完成。"""
    work_dir = Path(work_dir)
    req = read_json(work_dir / aw.REQ_NAME)
    if not req:
        raise RuntimeError(f"request.json 缺失或损坏（work_dir={work_dir}）")
    num_workers = int(req.get("num_workers") or 1)
    sd = bool(req.get("sd"))

    audio_path = work_dir / aw.AUDIO_NAME
    vad_path = work_dir / aw.VAD_JSON

    # idx>0 的 worker 后台预加载 ASR 模型（与 extract/VAD 重叠，省一次加载墙钟）
    preload: threading.Thread | None = None
    if idx > 0:
        preload = threading.Thread(target=_build_clipper, daemon=True)
        preload.start()

    _heartbeat(work_dir, idx, "等待提取音频")
    if idx == 0 and not _audio_ok(audio_path):
        _heartbeat(work_dir, 0, "提取音频")
        _extract_audio(str(req.get("video")), audio_path)
    _wait_until(lambda: _audio_ok(audio_path), work_dir, idx, "提取音频")

    _heartbeat(work_dir, idx, "全局 VAD")
    if idx == 0 and not _vad_ok(vad_path, str(req.get("fingerprint") or "")):
        _stage_vad(work_dir, 0, audio_path)
    _wait_until(lambda: _vad_ok(vad_path, str(req.get("fingerprint") or "")),
                work_dir, idx, "全局 VAD")

    if idx == 0:
        windows = _stage_windows(work_dir, vad_path)
    else:
        _wait_until(lambda: (read_json(work_dir / aw.WINDOWS_JSON) or {}).get("windows") is not None,
                    work_dir, idx, "窗口划分")
        windows = read_json(work_dir / aw.WINDOWS_JSON)["windows"]

    if preload is not None:
        preload.join()
    _stage_asr_windows(work_dir, idx, windows)

    if idx != 0:
        _heartbeat(work_dir, idx, "本 worker 完成")
        return 0

    # worker 0：等齐所有窗口 → 全局 SD → 合并 → state.json
    fp = str(req.get("fingerprint") or "")
    _wait_until(
        lambda: all(aw.win_is_valid(aw.win_path(work_dir, w["i"]), fp) for w in windows),
        work_dir, 0, "全部窗口完成")

    sentences = _merge_sentences(work_dir, windows)
    if sd and sentences:
        _stage_sd(work_dir, windows, sentences)
    write_json_atomic(work_dir / aw.STATE_NAME,
                      {"sentences": sentences})
    _heartbeat(work_dir, 0, "完成", len(windows), len(windows))
    log.info("[asr_pipeline] 完成：%d 窗口 / %d 句", len(windows), len(sentences))
    return 0
