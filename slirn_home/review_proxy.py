# -*- coding: utf-8 -*-
"""REQ-20261030-016：审校播放提速代理（外网部署「点字幕定位播放慢」的根治）。

三个审校面板（字幕生成 / 字幕修订 / 切分修剪）点行定位播放用的都是任务的
「解析视频」（_resolve_task_video：截取段优先，否则原视频）。外网带宽受限时
两件事让 seek 很慢：

1) **moov 索引在文件尾** —— tasklib.cut_video 产的截取段全部如此（stream copy
   不带 +faststart，moov 落尾）。浏览器必须先到文件尾取索引才能 seek：外网
   上一个 404MB 文件的尾部请求就是数秒起步，且每次重进面板（video 元素重建）
   都要再来一遍。
2) **高码率源**（相机/手机直出 2-8Mbps）seek 点每次要拉的缓冲字节数大。

分三档（plan() 纯函数判定，maybe_start 自动执行）：

- 码率 ≥ HIGH_BITRATE_BPS → **转码 480p ~600kbps** 代理（字节量骤降）
- 码率低但 moov 不在文件头 → **无损 remux +faststart**（几十秒、画质不变、
  体积几乎不变 —— 录屏类低码率源的正确档位）
- 低码率且已 faststart → **不生成**（原片已是最优，直接用原片）

代理产物 outputs/review_proxy.mp4：时间轴与源一致（remux 天然一致；转码保
时长），审校行的 start-ms 对代理同样成立。触发点：渲染三个审校面板时
maybe_start（幂等，有字幕产物才做 —— 审校的前提）；播放器侧 data-review
属性切换播放源（router.js wbResolveReviewSrc）。
"""
from __future__ import annotations

import json
import logging
import subprocess
import struct
import threading
import time
from pathlib import Path

from tasklib.video import get_video_duration

log = logging.getLogger(__name__)

PROXY_NAME = "review_proxy.mp4"
META_NAME = "review_proxy.json"

# 码率分档线（kbps）：≥1200 转码 480p 才划算（更低码率转码省不了几个字节还损画质）
HIGH_BITRATE_KBPS = 1200
# 转码档目标码率（480p 录屏/人像足够审校用；音频 96k 保语音清晰）
_PROXY_V_KBPS = 600
_PROXY_A_KBPS = 96
# 480p 上限宽（854=16:9 480p；小分辨率源不放大 —— 表达式里逗号须转义）
_SCALE_FILTER = "scale=w=min(854\\,iw):h=-2"

# 任务级 job 表（内存；服务器重启后按需重建 —— 产物在磁盘，幂等）
JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def proxy_path(outputs_dir: Path) -> Path:
    """代理产物路径（是否存在由调用方判断）。"""
    return Path(outputs_dir) / PROXY_NAME


def _meta_path(outputs_dir: Path) -> Path:
    return Path(outputs_dir) / META_NAME


def _source_fingerprint(src: Path | None) -> dict:
    """源指纹：路径 + mtime + 大小。源一变（重新截取/重导视频）→ 代理作废。"""
    if src is None or not src.exists():
        return {}
    try:
        st = src.stat()
    except OSError:
        return {}
    return {"src": str(src.resolve()), "mtime_ns": st.st_mtime_ns, "size": st.st_size}


def _proxy_matches_source(outputs_dir: Path, src: Path | None) -> bool:
    """代理产物是否对应当前源（sidecar 元数据比对）。

    必须校验：用户重新截取段后，源的时间轴变了 —— 旧代理还在就会按旧坐标
    定位（播错位置）。无 sidecar（旧版本生成）→ 视为不匹配 → 重建。
    """
    if not proxy_path(outputs_dir).exists():
        return False
    try:
        meta = json.loads(_meta_path(outputs_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    want = _source_fingerprint(src)
    if not want:
        return False
    return all(meta.get(k) == v for k, v in want.items())


def probe_faststart(path: Path) -> bool:
    """mp4 顶层 atom 走一遍：moov 出现在 mdat 之前 = faststart。

    非 mp4 / 解析失败 → False（保守：当作不 faststart，走 remux 档无害）。
    只读文件头几个 atom，几次预读即出，对 GB 级文件也是毫秒级。
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            offset = 0
            seen: list[str] = []
            while offset < size and len(seen) < 64:
                f.seek(offset)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                atom_size, typ = struct.unpack(">I4s", hdr)
                if atom_size == 1:  # 64 位扩展长度
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    atom_size = struct.unpack(">Q", ext)[0]
                elif atom_size == 0:  # 到文件尾
                    atom_size = size - offset
                name = typ.decode("latin-1", "replace")
                if name in ("moov", "mdat"):
                    seen.append(name)
                    if len(seen) >= 2:
                        break
                if atom_size <= 0:
                    break
                offset += atom_size
        return seen[:2] == ["moov", "mdat"]
    except (OSError, struct.error):
        return False


def plan(src: Path | None, outputs_dir: Path) -> dict:
    """判定该任务需要哪档代理（纯函数，可单测）。

    Returns: {tier: transcode|remux|skip|none, reason, duration, kbps, faststart}
      none = 无源 / 无法判时长（起不了代理）；skip = 原片已最优。
    """
    if src is None or not src.exists():
        return {"tier": "none", "reason": "无源视频"}
    dur = get_video_duration(src)
    if not dur or dur < 30:
        return {"tier": "none", "reason": f"时长未知或过短({dur})"}
    size = src.stat().st_size
    kbps = int(size * 8 / dur / 1000)
    fast = probe_faststart(src)
    if kbps >= HIGH_BITRATE_KBPS:
        return {"tier": "transcode", "reason": f"{kbps}kbps 高码率 → 480p 转码",
                "duration": dur, "kbps": kbps, "faststart": fast}
    if not fast:
        return {"tier": "remux", "reason": f"{kbps}kbps 低码率但 moov 在尾 → 无损 remux faststart",
                "duration": dur, "kbps": kbps, "faststart": fast}
    return {"tier": "skip", "reason": f"{kbps}kbps 且已 faststart → 原片最优",
            "duration": dur, "kbps": kbps, "faststart": fast}


def _encoder_args() -> list[str]:
    """转码档视频编码参数：探测到的硬件编码器优先，统一码率控制（不用 CRF —
    代理要的是可预期的字节量）。

    preset 取「快」的一档：代理是后台一次性任务，2 小时源在无显卡的云机器上
    用 libx264 veryfast 要 40 分钟+（ultrafast 只要几分钟），审校等不起；
    固定码率下 ultrafast 的码率控制依然有效。
    """
    from slirn_home import hw_encode

    try:
        enc = hw_encode.hw_encoder_cached()
    except Exception:  # noqa: BLE001 — 探测失败 → 软编
        enc = "libx264"
    if enc == "h264_qsv":
        pre = ["-c:v", "h264_qsv", "-preset", "veryfast"]
    elif enc == "h264_nvenc":
        pre = ["-c:v", "h264_nvenc", "-preset", "p1"]
    else:
        pre = ["-c:v", "libx264", "-preset", "ultrafast"]
    return pre + ["-b:v", f"{_PROXY_V_KBPS}k", "-maxrate", f"{_PROXY_V_KBPS}k",
                  "-bufsize", f"{_PROXY_V_KBPS * 2}k"]


def build_proxy_cmd(src: Path, dst: Path, tier: str) -> list[str]:
    """代理 ffmpeg argv（纯函数，可单测）。tier: transcode | remux。"""
    from slirn_home.compose_service import ffmpeg_bin

    cmd = [ffmpeg_bin(), "-y", "-i", str(src), "-sn", "-dn",
           "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
           "-f", "mp4"]
    if tier == "transcode":
        cmd += ["-vf", _SCALE_FILTER] + _encoder_args()
        cmd += ["-c:a", "aac", "-b:a", f"{_PROXY_A_KBPS}k", "-ac", "2"]
    else:  # remux：无损换容器布局
        cmd += ["-c", "copy"]
    cmd.append(str(dst))
    return cmd


def maybe_start(mgr, task_id: str) -> str:
    """按需启动代理生成（幂等）。返回当前状态供渲染提示用。

    前置：字幕产物已存在（审校的前提 —— 没字幕谁也不会点行播放，不必白烧
    CPU）。已有产物且对应当前源 / 在跑 / 本进程已失败过 → 不再启动。
    源变了（重新截取）→ 旧代理作废，重建。
    """
    outputs_dir = mgr.tasks_dir / task_id / "outputs"
    if not (outputs_dir / "subtitle.json").exists():
        return "skip_no_subtitle"
    t = None
    try:
        t = mgr.get(task_id)
    except Exception as e:  # noqa: BLE001
        log.warning("review_proxy: 任务不存在 %s (%s)", task_id, e)
        return "none"
    src = None
    if t is not None:
        if t.segment and t.segment.path.exists():
            src = t.segment.path
        elif t.original_video_source.exists():
            src = t.original_video_source
    if _proxy_matches_source(outputs_dir, src):
        return "done"
    with _LOCK:
        job = JOBS.get(task_id)
        if job and job.get("state") == "running":
            return "running"
        if job and job.get("state") == "failed":
            # 失败不永久拉黑：5 分钟后允许再试（磁盘满/临时占用等会自愈），
            # 但同一进程最多试 3 次（防坏源把 CPU 烧穿）。
            attempts = int(job.get("attempts") or 0)
            if attempts >= 3:
                return "failed"
            if time.time() - float(job.get("finished_at") or 0) < 300:
                return "failed"
            JOBS[task_id] = {"state": "retry", "attempts": attempts}
    p = plan(src, outputs_dir)
    if p["tier"] in ("skip", "none"):
        with _LOCK:
            JOBS[task_id] = {"state": p["tier"], "reason": p["reason"],
                             "at": time.time()}
        return p["tier"]
    threading.Thread(
        target=_run_job, args=(task_id, src, outputs_dir, p), daemon=True,
        name=f"review-proxy-{task_id}",
    ).start()
    return "running"


def _run_job(task_id: str, src: Path, outputs_dir: Path, p: dict) -> None:
    import os

    dst = proxy_path(outputs_dir)
    # 原子落盘：先写临时文件，成功后改名 —— 进程被杀/失败绝不留半个文件
    # （「产物存在即视为 ready」，半截 mp4 会被播放器当真源用）
    # ⚠️ 临时名必须保留 .mp4 后缀：ffmpeg 靠后缀选封装格式，"x.mp4.part"
    # 会报「Unable to choose an output format」而直接失败。
    part = dst.with_name(dst.stem + ".part.mp4")
    with _LOCK:
        prev = JOBS.get(task_id) or {}
        JOBS[task_id] = {"state": "running", "tier": p["tier"], "progress": 0,
                         "attempts": int(prev.get("attempts") or 0),
                         "started_at": time.time()}
    try:
        part.unlink(missing_ok=True)
        cmd = build_proxy_cmd(src, part, p["tier"])
        log.info("[review-proxy] %s %s: %s", task_id, p["tier"], p["reason"])
        # stderr 落临时文件而非管道：ffmpeg 日志写满 64KB 管道缓冲会**堵死**
        # 进程（本实现同时在读 stdout，日志一多就互相等 → 卡住不动）
        err_log = part.with_name(part.name + ".err")
        with open(err_log, "wb") as ef:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef,
                                    text=True, encoding="utf-8", errors="replace")
            dur = p.get("duration") or 0
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_ms=") and dur > 0:
                    try:
                        us = int(line.split("=", 1)[1])
                        pct = max(0, min(99, int(us / 1000 / dur * 100)))
                        with _LOCK:
                            JOBS[task_id]["progress"] = pct
                    except ValueError:
                        pass
            rc = proc.wait()
        try:
            err = err_log.read_text(encoding="utf-8", errors="replace")[-800:]
        except OSError:
            err = ""
        finally:
            try:
                err_log.unlink(missing_ok=True)
            except OSError:
                pass
        if rc != 0 or not part.exists() or part.stat().st_size <= 0:
            raise RuntimeError(f"ffmpeg rc={rc} {err}")
        os.replace(part, dst)
        # sidecar：源指纹（源变了 → 代理作废重建）
        try:
            fp = _source_fingerprint(src)
            fp.update({"tier": p["tier"], "duration": p.get("duration"),
                       "created_at": time.time()})
            _meta_path(outputs_dir).write_text(
                json.dumps(fp, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("[review-proxy] %s 元数据写入失败: %s", task_id, e)
        with _LOCK:
            JOBS[task_id] = {"state": "done", "tier": p["tier"], "progress": 100,
                             "finished_at": time.time()}
        log.info("[review-proxy] %s done: %s (%.1f MB)", task_id, dst.name,
                 dst.stat().st_size / 1e6)
    except Exception as e:  # noqa: BLE001 — 后台线程绝不外抛
        with _LOCK:
            prev = JOBS.get(task_id) or {}
            JOBS[task_id] = {"state": "failed", "error": str(e)[:300],
                             "attempts": int(prev.get("attempts") or 0) + 1,
                             "finished_at": time.time()}
        for f in (part, dst):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        log.warning("[review-proxy] %s 失败: %s", task_id, e)


def job_status(task_id: str) -> dict | None:
    with _LOCK:
        j = JOBS.get(task_id)
        return dict(j) if j else None


def render_hint(mgr, task_id: str) -> str:
    """播放器 wrap 上的 data-review 属性（服务端渲染一次性判定）。

    - 代理对应当前源 → data-review="1"（JS 直接走 ?src=review）
    - 在跑 → data-review-pending="1"（JS 点行时查一次状态，好了即切换）
    - 其余（skip/none/无字幕/源已变） → 空
    """
    outputs_dir = mgr.tasks_dir / task_id / "outputs"
    src = _current_source(mgr, task_id)
    if _proxy_matches_source(outputs_dir, src):
        return ' data-review="1"'
    j = job_status(task_id)
    if j and j.get("state") == "running":
        pct = int(j.get("progress") or 0)
        return (f' data-review-pending="1" data-review-progress="{pct}"')
    return ""


def _current_source(mgr, task_id: str) -> Path | None:
    """任务当前的解析视频源（截取段优先，否则原视频）—— 与播放器同源。"""
    try:
        t = mgr.get(task_id)
    except Exception:  # noqa: BLE001
        return None
    if t is None:
        return None
    if t.segment and t.segment.path.exists():
        return t.segment.path
    if t.original_video_source.exists():
        return t.original_video_source
    return None
