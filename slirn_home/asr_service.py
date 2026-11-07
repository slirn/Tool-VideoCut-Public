"""字幕生成服务 — REQ-20260915-001。

复用上游 funclip.videoclipper.VideoClipper 的识别链路（seaco-paraformer + 热词），
后台线程执行 + 内存 job 表 + 阶段级进度。结果双写 tasks/<id>/outputs/：
subtitle.srt（utf-8-sig）+ subtitle.json（结构化段列表）。

REQ-20260917-029：可选说话人分离（cam++，sd 参数默认开）——逐段 1 起始
人员编号（首次出现顺序）+ 每人句数统计（meta.speakers）。

设计见 docs/design/DESIGN-20260915-001-subtitle-stage.md。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from slirn_home import execution_history
from slirn_home import asr_windows as _awin
from slirn_home.fftools import ffprobe_bin as _ffprobe_bin
from slirn_home.paths import find_repo_root as _find_repo_root

log = logging.getLogger(__name__)

# funclip/videoclipper.py 顶层用 `from utils.xxx import`（相对包名导入），
# 需要把仓库根 + funclip/ 目录都挂进 sys.path（与 skill 脚本 extract_subtitle.py 同法）
_funclip_root = _find_repo_root()
_FUNCLIP_PKG = _funclip_root / "funclip"
for _p in (str(_funclip_root), str(_FUNCLIP_PKG)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# =============== 模型（懒加载单例） ===============

# 无 spk（SD 关闭用）/ 带 cam++ spk（SD 开启用）两个单例：cam++ 未缓存的
# 离线环境关掉 SD 仍能生成字幕（模型加载不因缺 cam++ 失败）
_MODEL = None
_MODEL_SD = None
_MODEL_LOCK = threading.Lock()


def _local_model_dir(model_id: str) -> str:
    """model id → 本地 modelscope 缓存目录（缓存优先，离线可加载）。

    funasr 传 model id 时 snapshot_download 必联网核对版本（get_or_download_model_dir），
    网络不通直接抛 model not registered——但四个模型通常早已完整缓存在
    ~/.cache/modelscope/hub/models/<org>/<name>。命中缓存就传目录路径加载
    （funasr 对本地路径只做可忽略的版本检查），未命中回退 id 走正常下载。
    """
    try:
        from modelscope.utils.file_utils import get_modelscope_cache_dir

        base = Path(get_modelscope_cache_dir())
    except Exception:  # noqa: BLE001 — modelscope 未装/版本差异时用默认缓存路径
        base = Path.home() / ".cache" / "modelscope" / "hub"
    # get_modelscope_cache_dir() 返回值可能已含 hub 后缀（实测 v1.x），两种形态都兼容
    root = base if base.name == "hub" else base / "hub"
    p = root / "models" / model_id
    return str(p) if (p / "config.yaml").exists() else model_id


def _build_model(with_spk: bool):
    """seaco-paraformer（热词优化版）+ VAD + 标点；with_spk 再挂 cam++ 说话人。

    与 skill extract_subtitle.py 的 paraformer 分支一致（模型离线缓存于
    ~/.cache/modelscope，cam++ 首次运行自动下载）。
    """
    from funasr import AutoModel

    kwargs = dict(
        model=_local_model_dir("iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"),
        vad_model=_local_model_dir("damo/speech_fsmn_vad_zh-cn-16k-common-pytorch"),
        punc_model=_local_model_dir("damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"),
    )
    if with_spk:
        kwargs["spk_model"] = _local_model_dir("damo/speech_campplus_sv_zh-cn_16k-common")
    return AutoModel(**kwargs)


def get_model(sd: bool = False):
    """按需取模型单例。首次调用加载（约 10-30s），之后复用。线程安全。

    sd=True → 带 cam++ 的模型；sd=False → 若带 spk 的单例已加载则直接复用
    （sd_switch='no' 时不返回说话人结果，识别不受影响，省一份 paraformer
    内存），否则建无 spk 的模型。
    """
    global _MODEL, _MODEL_SD
    if sd:
        if _MODEL_SD is None:
            with _MODEL_LOCK:
                if _MODEL_SD is None:
                    log.info("[asr] 加载 FunASR 模型（含 cam++ 说话人）…")
                    _MODEL_SD = _build_model(with_spk=True)
                    log.info("[asr] 模型就绪")
        return _MODEL_SD
    if _MODEL_SD is not None:
        return _MODEL_SD
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                log.info("[asr] 加载 FunASR 模型…")
                _MODEL = _build_model(with_spk=False)
                log.info("[asr] 模型就绪")
    return _MODEL


# =============== 纯函数：sentence_info → 段列表 ===============


def segments_from_sentences(sentence_info: list[dict]) -> list[dict]:
    """funclip state['sentences'] → [{i, start_ms, end_ms, text, start, end}]。

    text 可能是 str（未拆分句）或 token 列表（_split_long_sentence 拆分产物），
    Text2SRT.text() 两者都处理（中文直接拼接、英文词空格连接）。
    无有效 timestamp 的条目跳过（与 generate_srt 行为一致）。

    字级时间戳（tokens + token_ts，REQ-20260916-008）：FunASR 的 timestamp
    与 tokenize(text) 一一对应（上游 generate_srt_clip 同口径），切分修剪
    阶段靠它把「切分后文字」对齐到原段音频内的具体时间段；两列表等长
    才保存（不等长视为不可信，该段切分时降级整段）。
    """
    from utils.subtitle_utils import Text2SRT, time_convert

    from slirn_home.cutlist_service import tokenize

    segs = []
    for sent in sentence_info or []:
        ts = sent.get("timestamp")
        if not ts:
            continue
        try:
            t2s = Text2SRT(sent.get("text", ""), ts)
        except Exception:  # noqa: BLE001 — 单条畸形不拖垮整批
            log.warning("跳过畸形句子条目: %r", sent)
            continue
        seg = {
            "i": len(segs) + 1,
            "start_ms": int(t2s.start_sec),
            "end_ms": int(t2s.end_sec),
            "start": time_convert(t2s.start_sec),
            "end": time_convert(t2s.end_sec),
            "text": t2s.text(),
        }
        spk_raw = sent.get("spk")
        if spk_raw is not None:
            try:
                # FunASR 可能给 numpy 整型（json 不可序列化）→ int() 强转
                seg["spk_raw"] = int(spk_raw)
            except (TypeError, ValueError):
                pass
        raw_text = sent.get("text", "")
        tokens = [str(w) for w in raw_text] if isinstance(raw_text, list) else tokenize(str(raw_text))
        if tokens and len(tokens) == len(ts):
            try:
                seg["tokens"] = tokens
                seg["token_ts"] = [[int(t[0]), int(t[1])] for t in ts]
            except (TypeError, ValueError, IndexError):
                pass  # 时间戳非数值对 → 不保存（切分阶段该段降级整段）
        segs.append(seg)
    return segs


# =============== 说话人编号 + 统计（REQ-20260917-029） ===============


def assign_speaker_numbers(segments: list[dict]) -> list[dict]:
    """把 FunASR 原始簇标签（spk_raw）归一为 1 起始、按首次出现顺序的人员编号。

    cam++ 簇标签序号无语义（不保证按出现顺序、不保证连续），
    「先说话的人 = 人员1」对课程视频最直观。归一后每段只留 spk（int ≥ 1），
    spk_raw 剔除；无标签的段不加 spk（与 SD 关闭时的数据形状一致）。
    """
    mapping: dict[int, int] = {}
    for seg in segments:
        raw = seg.pop("spk_raw", None)
        if raw is None:
            continue
        if raw not in mapping:
            mapping[raw] = len(mapping) + 1
        seg["spk"] = mapping[raw]
    return segments


def speaker_stats(segments: list[dict]) -> list[dict]:
    """每个人员编号说了多少句（口径=字幕段数），按编号升序。

    句数总和 = 带 spk 的段数；与 UI 列表行数、subtitle.srt 块数同口径。
    """
    counts: dict[int, int] = {}
    for seg in segments:
        spk = seg.get("spk")
        if spk is None:
            continue
        counts[spk] = counts.get(spk, 0) + 1
    return [{"spk": k, "sentences": counts[k]} for k in sorted(counts)]


def segments_to_srt(segments: list[dict]) -> str:
    """segments → 标准 SRT 文本（块间空行分隔，与 subtitle.json/UI 同源）。

    说话人标签放序号行（`3  spk1`，与上游 FunClip SD 输出同构，编号用归一后
    人员编号）；无 spk 的段序号行为纯数字。文本行不携带标签，后续字幕清洗/
    热词替换按文本处理不受影响。
    """
    blocks = []
    for seg in segments:
        idx = seg["i"]
        if seg.get("spk") is not None:
            idx = f"{idx}  spk{seg['spk']}"
        blocks.append(f"{idx}\n{seg['start']} --> {seg['end']}\n{seg['text']}\n")
    return "\n".join(blocks)


# =============== 预检 ===============


def has_audio_track(video_path: Path) -> bool:
    """ffprobe 检查有无音频轨（上游 video_recog 对无音频视频直接 sys.exit）。"""
    try:
        r = subprocess.run(
            [
                _ffprobe_bin(),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        return "audio" in (r.stdout or "")
    except Exception as e:  # noqa: BLE001
        log.warning("ffprobe 预检失败（按有音频继续）: %s", e)
        return True


# =============== 识别入口（拆出便于测试 monkeypatch） ===============


def _extract_mono_wav_16k(video_path: str, wav_path: Path) -> None:
    """ffmpeg 流式抽 16k 单声道 wav（REQ-20260917-023）。

    上游 video_recog 用 moviepy 抽音轨（保持源采样率 + 立体声），librosa.load
    内部会整读 float32 (N, 2) 立体声大块——多小时视频是 GiB 级单块连续分配，
    直接内存不足（实测 2.26h@44.1k 立体声 = 2.68GiB）。ffmpeg -ac 1 -ar 16000
    流式重采样，内存几十 MB，长视频内存需求降到约 0.7GB/小时。
    """
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                video_path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ],
            capture_output=True,
            timeout=7200,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("音频提取超时（>2 小时），请检查视频文件") from None
    if r.returncode != 0 or not wav_path.exists() or wav_path.stat().st_size <= 44:
        raise RuntimeError(f"音频提取失败: {(r.stderr or '').strip()[-400:]}")


def _run_recognition(video_path: str, hotword_str: str, sd: bool = False) -> dict:
    """调上游 funclip VideoClipper.recog → state（含 sentences）。

    与上游 video_recog 的差别仅在「音频从哪来」：16k 单声道 wav 流式抽取
    （REQ-20260917-023，长视频内存修复）替代 moviepy 立体声抽轨 + librosa
    重采样；识别链路（seaco-paraformer / 热词 / 断句）与上游完全同源。

    SLIRN_ASR_SUBPROCESS=1 时改在独立 python 进程里跑同一条链路
    （_run_recognition_subprocess，REQ-20261105-001）。

    sd=True 走上游 SD 分支：cam++ 逐句 spk 标签进 state['sentences']。
    注意上游 recog 判 sd_switch == 'Yes'（大写 Y），传小写 'yes' 会静默
    走普通分支（skill 脚本 extract_subtitle.py 即中招，见 REQ-20260917-029 非目标）。
    """
    if _env_flag(_SUBPROCESS_ENV):
        return _run_recognition_subprocess(video_path, hotword_str, sd)
    import librosa  # 上游 videoclipper 同款依赖；懒加载保持服务启动轻量

    from funclip.videoclipper import VideoClipper

    clipper = VideoClipper(get_model(sd))
    clipper.lang = "zh"
    with tempfile.TemporaryDirectory(prefix="slirn_asr_") as td:
        wav_path = Path(td) / "audio_16k_mono.wav"
        _extract_mono_wav_16k(video_path, wav_path)
        wav = librosa.load(str(wav_path), sr=16000)[0]  # (N,) float32 单声道
    state = {"video_filename": video_path}
    _res_text, _res_srt, state = clipper.recog(
        (16000, wav),
        "Yes" if sd else "no",
        state,
        hotword_str,
        None,
    )
    return state


# =============== 子进程识别（REQ-20261105-001，默认关闭） ===============

# SLIRN_ASR_SUBPROCESS=1 时识别在独立 python 进程里跑：主进程不再 import
# torch/funasr/librosa（省 ~1-2GB 常驻），模型加载 + 推理的内存峰值被限制在
# 子进程内、跑完进程退出即全部归还。2026-11-05 部署实测（8GB 机器）：进程内
# 识别把系统内存推到 7.4/7.96GB，HTTP 连接被系统掐断（客户端
# RemoteDisconnected 而服务端其实已成功落盘），且事后 ~2.9GB 常驻不归还。
# 代价：每次识别多付一次模型加载（约 20-40s，模型已在本地缓存）。
# 长视频字幕生成与短视频提取快路径（short_video_extract._run_funasr_extract）
# 都走本函数，开关对两者同时生效。
_SUBPROCESS_ENV = "SLIRN_ASR_SUBPROCESS"
_ASR_TIMEOUT_SEC = 6 * 3600


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _run_recognition_subprocess(video_path: str, hotword_str: str, sd: bool) -> dict:
    """独立进程跑 _run_recognition（同一条实现，仅换进程边界）→ 回读 state JSON。"""
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.pop(_SUBPROCESS_ENV, None)  # worker 内部恒走进程内实现，防递归
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(repo_root), env.get("PYTHONPATH", "")) if p)
    with tempfile.TemporaryDirectory(prefix="slirn_asr_sub_") as td:
        out_path = Path(td) / "state.json"
        cmd = [
            sys.executable, "-m", "slirn_home.asr_worker",
            "--video", video_path,
            "--hotwords", hotword_str,
            "--sd", "1" if sd else "0",
            "--out", str(out_path),
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_ASR_TIMEOUT_SEC,
                env=env, cwd=str(repo_root))
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"识别超时（>{_ASR_TIMEOUT_SEC // 3600} 小时），请检查视频后重试") from None
        if r.returncode != 0:
            raise RuntimeError(
                f"ASR 子进程失败（exit={r.returncode}）: {(r.stderr or '').strip()[-400:]}")
        if not out_path.is_file():
            raise RuntimeError("ASR 子进程未产出结果文件")
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise RuntimeError(f"ASR 子进程结果解析失败: {e}") from None


# =============== 窗口化识别（REQ-20261107-003：断点续传 + 段数进度） ===============

# 并行 worker 数（进程）：每个 worker 独立加载 ASR 模型（~1.5-2GB），
# 8GB 部署机建议 ≤2；1 = 串行（仅断点续传，不并行）。可被环境变量覆盖。
_ASR_WINDOW_WORKERS = max(1, int(os.environ.get("SLIRN_ASR_WORKERS") or "1"))
_POLL_INTERVAL_S = 3.0


def _video_identity(video_path: Path) -> tuple[int, int]:
    st = Path(video_path).stat()
    return int(st.st_size), int(st.st_mtime_ns)


def _count_valid_windows(work_dir: Path, fingerprint: str, windows: list[dict]) -> int:
    return sum(1 for w in windows
               if _awin.win_is_valid(_awin.win_path(work_dir, w["i"]), fingerprint))


def _run_recognition_windowed(video_path: str, hotword_str: str, sd: bool,
                              outputs_dir: Path,
                              on_progress=None) -> tuple[dict, int]:
    """窗口化识别（长音频）：阶段机在子进程里跑，本函数只做编排与轮询。

    - 工作目录 ``outputs/asr_work/`` 持久化 ⇒ 中断重跑只补缺失窗口；
    - 指纹（源视频身份 + 热词/SD/窗口参数）不匹配的旧工作目录整目录作废；
    - 返回 (state, resumed_windows)：state 与 ``_run_recognition`` 同形状
      （含 sentences），resumed_windows = 本次启动时已可直接复用的窗口数。
    on_progress(stage: str, done: int, total: int) 在轮询线程上回调。
    """
    work_dir = Path(outputs_dir) / _awin.WORK_DIR_NAME
    video_size, video_mtime_ns = _video_identity(Path(video_path))
    fp = _awin.input_fingerprint(video_path, video_size, video_mtime_ns,
                                 sd, hotword_str, _awin.WINDOW_SPAN_S)

    # 指纹不匹配的旧工作目录（换了视频/热词/参数）整目录作废，防陈旧产物混用
    old_req = _awin.read_json(work_dir / _awin.REQ_NAME)
    if old_req and old_req.get("fingerprint") != fp:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)
        old_req = None
    resumed = 0
    if old_req:
        old_win = _awin.read_json(work_dir / _awin.WINDOWS_JSON) or {}
        windows = old_win.get("windows") or []
        resumed = _count_valid_windows(work_dir, fp, windows)

    work_dir.mkdir(parents=True, exist_ok=True)
    workers = _ASR_WINDOW_WORKERS
    _awin.write_json_atomic(work_dir / _awin.REQ_NAME, {
        "video": video_path, "video_size": video_size,
        "video_mtime_ns": video_mtime_ns, "hotwords": hotword_str,
        "sd": bool(sd), "fingerprint": fp,
        "window_span_s": _awin.WINDOW_SPAN_S, "num_workers": workers,
    })

    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.pop(_SUBPROCESS_ENV, None)  # pipeline worker 内部不走单发递归路径
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(repo_root), env.get("PYTHONPATH", "")) if p)

    procs = []
    for idx in range(workers):
        out_log = (work_dir / f"worker_{idx}.log").open("wb")
        procs.append({
            "idx": idx, "log": out_log,
            "popen": subprocess.Popen(
                [sys.executable, "-m", "slirn_home.asr_worker", "--pipeline",
                 "--work-dir", str(work_dir), "--idx", str(idx)],
                stdout=out_log, stderr=subprocess.STDOUT,
                env=env, cwd=str(repo_root)),
        })
    try:
        failed = None
        while True:
            alive = 0
            for p in procs:
                rc = p["popen"].poll()
                if rc is None:
                    alive += 1
                elif rc != 0 and failed is None:
                    failed = p
            if alive == 0:
                break
            if failed is not None:
                # 一个 worker 失败立即终止其余（否则 worker 0 会干等缺失的窗口）
                for p in procs:
                    try:
                        if p["popen"].poll() is None:
                            p["popen"].kill()
                    except Exception:  # noqa: BLE001
                        pass
                break
            _report_window_progress(work_dir, on_progress)
            time.sleep(_POLL_INTERVAL_S)
        _report_window_progress(work_dir, on_progress)
    finally:
        for p in procs:
            try:
                if p["popen"].poll() is None:  # 兜底：父线程异常时不留孤儿
                    p["popen"].kill()
                p["log"].close()
            except Exception:  # noqa: BLE001
                pass

    bad = [p for p in procs if p["popen"].returncode not in (0,)]
    if bad:
        tail = ""
        try:
            tail = (work_dir / f"worker_{bad[0]['idx']}.log").read_text(
                encoding="utf-8", errors="replace").strip()[-400:]
        except OSError:
            pass
        raise RuntimeError(
            f"窗口化识别 worker {bad[0]['idx']} 失败（exit={bad[0]['popen'].returncode}）：{tail}")

    state_path = work_dir / _awin.STATE_NAME
    if not state_path.is_file():
        raise RuntimeError("窗口化识别未产出 state.json")
    try:
        return json.loads(state_path.read_text(encoding="utf-8")), resumed
    except ValueError as e:
        raise RuntimeError(f"窗口化识别结果解析失败: {e}") from None


def _report_window_progress(work_dir: Path, on_progress) -> None:
    """汇总各 worker 心跳 → on_progress(stage, done, total)（结构化，文本由 job 侧组装）。

    - 窗口阶段：stage="识别中"，done = 各心跳 done 求和，total = 总窗口数；
    - SD 阶段：stage="说话人分离"，done/total 用 worker 0 心跳（嵌入块）；
    - 其余阶段：原样透传 worker 0 的 stage 文本（提取音频/全局 VAD/完成）。
    """
    if on_progress is None:
        return
    heads = []
    for idx in range(_ASR_WINDOW_WORKERS):
        d = _awin.read_json(work_dir / _awin.PROGRESS_FMT.format(idx))
        if d:
            heads.append((idx, d))
    if not heads:
        return
    by0 = dict(heads).get(0) or {}
    stage = str(by0.get("stage") or "处理中")
    done = total = 0
    win = _awin.read_json(work_dir / _awin.WINDOWS_JSON) or {}
    windows = win.get("windows") or []
    if any("识别中" in str(h.get("stage")) for _, h in heads):
        stage = "识别中"
        done = sum(int(h.get("done") or 0) for _, h in heads)
        total = len(windows)
        done = min(done, total)
    elif "说话人分离" in stage:
        done, total = int(by0.get("done") or 0), int(by0.get("total") or 0)
    try:
        on_progress(stage, done, total)
    except Exception as e:  # noqa: BLE001 — 进度回调失败不影响识别
        log.warning("[asr] 进度回调失败: %s", e)


# =============== 后台 job 管理 ===============

# task_id → {"state": "running|done|error", "stage": str, "error": str|None,
#            "started_at": float, "finished_at": float|None, "segments_count": int}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

SUBTITLE_JSON = "subtitle.json"
SUBTITLE_SRT = "subtitle.srt"


def job_status(task_id: str) -> dict | None:
    with _JOBS_LOCK:
        j = _JOBS.get(task_id)
        return dict(j) if j else None


def start_job(
    task_id: str,
    video_path: Path,
    hotwords: list[str],
    outputs_dir: Path,
    source: str = "original",
    base_offset_ms: int = 0,
    sd: bool = True,
    on_success: Callable[[list[dict]], None] | None = None,
    *,
    auto: bool = False,
    auto_session_id: str = "",
) -> bool:
    """启动字幕生成线程。已在跑 → 返回 False（不重复起）。

    sd=True 时同时做说话人分离（cam++），结果带 1 起始人员编号 + 每人句数
    统计（REQ-20260917-029）。
    on_success(segments) 在 worker 线程内、结果落盘之后调用（app 层用它迁任务状态）。

    REQ-20260919-075：auto=True 表示流程自动触发（pipeline_service 调用），
    写入 execution_history 的 auto 字段用于前端区分手动/自动。

    REQ-20260920-081：auto_session_id 透传给 execution_history，便于前端聚合显示
    「同一次自动流」。手动调用时为空字符串。
    """
    with _JOBS_LOCK:
        existing = _JOBS.get(task_id)
        if existing and existing.get("state") == "running":
            return False
        _JOBS[task_id] = {
            "state": "running",
            "stage": "加载模型",
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
            "segments_count": 0,
        }

    # REQ-20260918-048：执行历史（落盘，单写锁）。开始即记一条 running；完成/失败回填。
    # REQ-20260919-075：传 auto 让 history 标记自动/手动。
    exec_id = execution_history.record_start(
        outputs_dir, execution_history.KIND_SUBTITLE_GENERATION,
        extra={"sd": sd, "source": source}, auto=auto,
        auto_session_id=auto_session_id)

    def _run():
        job = _JOBS[task_id]

        def _stage(name: str) -> None:
            job["stage"] = name
            log.info("[asr][%s] %s", task_id, name)

        try:
            hotword_str = " ".join(w.strip() for w in hotwords if w and w.strip())
            # 长音频走窗口化（REQ-20261107-003：断点续传 + 已识别 N/M 窗口进度）；
            # 短视频保持旧单发路径（行为零变化）。探测失败按旧路径兜底。
            _duration = None
            try:
                from tasklib.video import get_video_duration

                _duration = get_video_duration(Path(video_path))
            except Exception:  # noqa: BLE001
                _duration = None
            use_windowed = bool(_duration and _duration >= _awin.WINDOW_MIN_DURATION_S)

            _sync = [0.0, -1]

            def _on_prog(stage: str, done: int, total: int) -> None:
                if stage == "识别中":
                    text = f"识别中 已识别 {done}/{total} 窗口"
                    job["windows_done"], job["windows_total"] = done, total
                elif stage == "说话人分离":
                    text = f"说话人分离 {done}/{total} 块"
                else:
                    text = stage
                _stage(text)
                now = time.time()
                if stage == "识别中" and (
                        now - _sync[0] >= 5.0 or done != _sync[1]):
                    _sync[0], _sync[1] = now, done
                    try:
                        execution_history.patch_extra(
                            outputs_dir, exec_id,
                            {"windows_done": done, "windows_total": total})
                    except Exception:  # noqa: BLE001 — 日志失败不影响识别
                        pass

            resumed_windows = 0
            if use_windowed:
                state, resumed_windows = _run_recognition_windowed(
                    str(video_path), hotword_str, sd, outputs_dir, _on_prog)
            else:
                _stage("加载模型")
                _stage("提取音频 → 识别中")
                state = _run_recognition(str(video_path), hotword_str, sd)

            _stage("保存结果")
            segments = segments_from_sentences(state.get("sentences") or [])
            if sd:
                assign_speaker_numbers(segments)
            stats = speaker_stats(segments)
            srt_text = segments_to_srt(segments)
            outputs_dir.mkdir(parents=True, exist_ok=True)
            srt_path = outputs_dir / SUBTITLE_SRT
            srt_path.write_text(srt_text, encoding="utf-8-sig")
            meta = {
                "version": 2,
                "model": "seaco-paraformer",
                "sd": sd,
                "source": source,
                "video_path": str(video_path).replace("\\", "/"),
                "video_url": f"/slirn/api/video/{task_id}",
                "base_offset_ms": base_offset_ms,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "segments": segments,
            }
            if stats:
                meta["speakers"] = {"count": len(stats), "stats": stats}
            (outputs_dir / SUBTITLE_JSON).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            job["segments_count"] = len(segments)
            job["state"] = "done"
            job["stage"] = "完成"
            job["finished_at"] = time.time()
            if stats:
                summary = "、".join(f"人员{s['spk']} {s['sentences']} 句" for s in stats)
                log.info("[asr][%s] 完成：%d 段 · %d 位说话人（%s）", task_id, len(segments), len(stats), summary)
            else:
                log.info("[asr][%s] 完成：%d 段", task_id, len(segments))
            if on_success:
                try:
                    on_success(segments)
                except Exception as e:  # noqa: BLE001 — 回调失败不影响结果
                    log.warning("[asr][%s] on_success 回调失败: %s", task_id, e)
            # REQ-20260918-048：执行历史成功回填（extra 补 segments / speakers 统计；
            # 窗口化路径补 engine / 复用窗口数，与粗剪合成的 reused_chunks 同思路）
            execution_history.patch_extra(outputs_dir, exec_id,
                                          {"segments": len(segments),
                                           "speakers": len(stats) if stats else 0,
                                           "engine": ("asr-windowed" if use_windowed
                                                      else "asr-direct"),
                                           "resumed_windows": resumed_windows,
                                           "windows_total": job.get("windows_total") or 0})
            # REQ-20260919-075：完成时回填具体执行情况（带段数 / 说话人数）
            execution_history.patch_fields(outputs_dir, exec_id, {
                "description": (f"FunASR 识别原始视频，共识别 {len(segments)} 段"
                                + (f"，{len(stats)} 位说话人" if stats else ""))
            })
            # 成功后清理窗口工作目录（断点只在失败/中断时保留供续跑）
            if use_windowed:
                import shutil

                shutil.rmtree(outputs_dir / _awin.WORK_DIR_NAME, ignore_errors=True)
            execution_history.record_finish(outputs_dir, exec_id, success=True,
                                            error="")
        except SystemExit as e:  # 上游 video_recog 对无音频视频 sys.exit(1)
            job["state"] = "error"
            job["error"] = f"视频没有音频轨（无法识别）: exit={e.code}"
            job["finished_at"] = time.time()
            execution_history.record_finish(outputs_dir, exec_id, success=False,
                                            error=job["error"])
        except Exception as e:  # noqa: BLE001 — 后台线程必须全兜底
            log.exception("[asr][%s] 生成失败", task_id)
            msg = str(e)
            if isinstance(e, MemoryError) or "Unable to allocate" in msg:
                # REQ-20260917-023：长视频识别需约 0.7GB/小时连续内存；
                # 不足时给出可操作建议而不是裸 numpy 报错
                msg += "（内存不足：请关闭其他应用（如上游 FunClip 服务）后重试）"
            job["state"] = "error"
            job["error"] = msg
            job["finished_at"] = time.time()
            execution_history.record_finish(outputs_dir, exec_id, success=False,
                                            error=msg)

    threading.Thread(target=_run, name=f"asr-{task_id}", daemon=True).start()
    return True


# =============== 读取既有结果 ===============


def load_subtitle(outputs_dir: Path) -> dict | None:
    """读取 subtitle.json（无/损坏 → None）。

    REQ-20261030-015：mtime+size 键控的解析缓存 —— subtitle.json 数 MB、
    一次工作台渲染里 5 个区域各自 load（重复 json.loads ~0.3s）。
    ⚠️ 返回的是**共享缓存对象**：调用方只读，不得原地修改（全仓调用方
    均只读，已扫描确认；写盘走 save_subtitle → mtime 变 → 缓存自动失效）。
    """
    p = Path(outputs_dir) / SUBTITLE_JSON
    try:
        st = p.stat()
    except OSError:
        _SUBTITLE_CACHE.pop(str(p), None)
        return None
    hit = _SUBTITLE_CACHE.get(str(p))
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("subtitle.json 损坏: %s", e)
        return None
    _SUBTITLE_CACHE[str(p)] = (st.st_mtime_ns, st.st_size, obj)
    return obj


_SUBTITLE_CACHE: dict[str, tuple[int, int, dict]] = {}
