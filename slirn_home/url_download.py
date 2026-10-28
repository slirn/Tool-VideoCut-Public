# -*- coding: utf-8 -*-
"""URL 视频下载 — 新建任务的源视频获取方式之二（与「本地文件上传」并列）。

用户粘贴 http(s) 链接，服务端流式下载到仓库根 ``downloads/``，完成后前端走
与上传完全相同的链路（file_selected → applyFileInfo → create_task），下游零改动。

设计要点：
- 纯标准库（urllib），不引入新依赖；
- 流式落盘（64KB 块）到 ``<name>.part``，完成后 ``os.replace`` 原子转正；
- 进度取自 Content-Length；响应头没给长度时只报已收字节数；
- 任务模型对齐 asr_service / fine_render：内存注册表 + 后台线程 + TTL 清理；
- 大小上限与上传一致（10 GB）；连接/读超时 60s（整体不限时 — 慢链路下载
  GB 级文件是合法场景）；
- SSRF 取舍：本工具定位**本机/局域网自用**，从 NAS 等内网地址拉视频是
  合法用例，故只校验 http/https 协议、不做内网地址拦截（若未来公网部署
  需重新评估）。
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = __import__("logging").getLogger(__name__)

# 与前端 accept 列表 / videoclipper.video_suffixs 对齐
VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".ts", ".mpeg", ".m4v", ".flv", ".wmv"}
DEFAULT_EXT = ".mp4"
MAX_BYTES = 10 * 1024 ** 3          # 10 GB — 与上传上限一致
_CHUNK = 64 * 1024
_CONNECT_TIMEOUT = 60
_JOB_TTL_SEC = 30 * 60              # 完成态任务保留 30 分钟供前端回查
_MAX_FINISHED_KEEP = 50

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.RLock()

_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# ---------- 纯函数（单测覆盖） ----------

def sanitize_filename(name: str) -> str:
    """去掉路径分隔与 Windows 非法字符；空结果回退 download_<ts>。"""
    clean = _SAFE_NAME_RE.sub("_", (name or "").strip()).strip("._ ")
    return clean or f"download_{int(time.time())}"


def resolve_filename(url: str, content_disposition: str | None) -> str:
    """URL + Content-Disposition → 安全的本地文件名（带视频扩展名）。

    优先级：Content-Disposition（filename*=UTF-8'' 优先于 filename=）>
    URL path 末段。扩展名不在视频白名单（含无扩展名）时补 ``.mp4`` —
    下游 videoclipper 按扩展名判定音/视频模式，没有扩展名会被拒。
    """
    name = ""
    if content_disposition:
        m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", content_disposition)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"'))
        if not name:
            m = re.search(r'filename="?([^";]+)"?', content_disposition)
            if m:
                name = m.group(1).strip()
    if not name:
        name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    name = sanitize_filename(name)
    stem, ext = Path(name).suffix and (Path(name).stem, Path(name).suffix) or (name, "")
    if ext.lower() not in VIDEO_EXTS:
        ext = DEFAULT_EXT
    return f"{stem}{ext}"


def validate_url(url: str) -> str:
    """校验并规范化 URL；不合法抛 ValueError（信息可直接给用户）。"""
    u = (url or "").strip()
    if not u:
        raise ValueError("请填写 URL")
    if any(ord(c) < 0x20 for c in u):
        raise ValueError("URL 含非法控制字符")
    parts = urllib.parse.urlsplit(u)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 链接（收到 {parts.scheme or '空'}）")
    if not parts.netloc:
        raise ValueError("URL 缺少主机名")
    return u


# ---------- 任务注册表 ----------

def _cleanup_stale_locked() -> None:
    now = time.time()
    for jid in [k for k, v in _JOBS.items()
                if v["state"] in ("done", "error", "cancelled")
                and now - (v.get("finished_at") or 0) > _JOB_TTL_SEC]:
        _JOBS.pop(jid, None)
    finished = [(v.get("finished_at") or 0, k) for k, v in _JOBS.items()
                if v["state"] in ("done", "error", "cancelled")]
    if len(finished) > _MAX_FINISHED_KEEP:
        for _, jid in sorted(finished)[:len(finished) - _MAX_FINISHED_KEEP]:
            _JOBS.pop(jid, None)


def _unique_path(dir_: Path, name: str) -> Path:
    """同名碰撞时追加 _1 / _2 …（下载目录可能长期累积同名文件）。"""
    cand = dir_ / name
    stem, ext = cand.stem, cand.suffix
    i = 1
    while cand.exists():
        cand = dir_ / f"{stem}_{i}{ext}"
        i += 1
    return cand


def start_download(repo_root: Path | str, url: str) -> str:
    """校验 URL 并启动后台下载线程。返回 job_id（同一 job 不会重复启动）。"""
    import uuid

    url = validate_url(url)
    dl_dir = Path(repo_root) / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)

    job_id = f"dl_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    job = {
        "job_id": job_id, "state": "running", "url": url,
        "received_bytes": 0, "total_bytes": None, "path": "",
        "name": "", "error": None,
        "started_at": time.time(), "finished_at": None,
        "_cancel": threading.Event(),
    }
    with _JOBS_LOCK:
        _cleanup_stale_locked()
        _JOBS[job_id] = job
    t = threading.Thread(target=_download_thread, args=(job, dl_dir),
                         daemon=True, name=f"url-dl-{job_id}")
    t.start()
    return job_id


def job_status(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        out = {k: v for k, v in job.items() if not k.startswith("_")}
        out["elapsed_s"] = int((job.get("finished_at") or time.time()) - job["started_at"])
        return out


def cancel_download(job_id: str) -> bool:
    """请求取消；已结束的任务返回 False。实际终止由下载线程在下一个块边界感知。"""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job or job["state"] != "running":
            return False
        job["_cancel"].set()
        return True


# ---------- 下载线程 ----------

def _download_thread(job: dict, dl_dir: Path) -> None:
    part_path: Path | None = None
    try:
        req = urllib.request.Request(
            job["url"],
            headers={"User-Agent": "Mozilla/5.0 (VideoCut url_download)"},  # 部分 CDN 拒空 UA
        )
        with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
            cl = resp.headers.get("Content-Length")
            try:
                job["total_bytes"] = int(cl) if cl else None
            except ValueError:
                job["total_bytes"] = None
            name = resolve_filename(job["url"], resp.headers.get("Content-Disposition"))
            final_path = _unique_path(dl_dir, name)
            part_path = final_path.with_suffix(final_path.suffix + ".part")
            job["name"] = final_path.name

            received = 0
            with open(part_path, "wb") as f:
                while True:
                    if job["_cancel"].is_set():
                        _finish(job, "cancelled", f"已取消（写入 {received} 字节后终止）")
                        return
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_BYTES:
                        raise ValueError(
                            f"文件超过大小上限 {MAX_BYTES // 1024 ** 3} GB，已终止"
                        )
                    f.write(chunk)
                    job["received_bytes"] = received
            if received == 0:
                raise ValueError("下载内容为空（URL 可能无效）")
            part_path.replace(final_path)  # 原子转正
            job["path"] = str(final_path).replace("\\", "/")
            job["received_bytes"] = received
            _finish(job, "done", None)
    except ValueError as e:                     # 校验类：信息直接给用户
        _finish(job, "error", str(e))
    except urllib.error.HTTPError as e:
        _finish(job, "error", f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        _finish(job, "error", f"连接失败: {e.reason}")
    except Exception as e:                      # noqa: BLE001 — 兜底，任何异常都要回传给前端
        _finish(job, "error", f"下载失败: {e}")
    finally:
        # 终止/失败时清掉半成品 .part（取消发生在两个块之间时 part 可能未创建）
        if job["state"] != "done" and part_path is not None:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass


def _finish(job: dict, state: str, error: str | None) -> None:
    job["state"] = state
    job["error"] = error
    job["finished_at"] = time.time()
    if error:
        log.warning("[url_download] %s %s → %s", job["job_id"], state, error)
    else:
        log.info("[url_download] %s 完成 → %s", job["job_id"], job.get("path") or "取消")
