# -*- coding: utf-8 -*-
"""字幕字体 STHeitiMedium.ttc 的按需获取。

公开版工程**不随仓库分发字体**（约 54MB）：首次启动 / 首次渲染时从上游
FunClip 官方仓库下载到 ``font/STHeitiMedium.ttc``，之后离线可用。

失败策略：所有下载源都失败时返回 ``None``，渲染路径降级为 libass 的
系统字体回退（与字体缺失机器上的既有行为一致），不阻断启动 / 渲染。
网络恢复后，渲染路径的惰性调用会再次尝试（每个进程内失败后至少隔
``_RETRY_INTERVAL`` 秒才重试，避免离线时反复拉 54MB）。

手动获取（下载慢 / 无外网时）：
    从 https://github.com/alibaba-damo-academy/FunClip/tree/main/font
    下载 STHeitiMedium.ttc，放到本工程 ``font/`` 目录下即可，程序检测到
    文件存在（>1MB）就不再下载。
"""
import logging
import shutil
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = REPO_ROOT / "font" / "STHeitiMedium.ttc"

# 下载源（按顺序尝试；官方上游仓库，镜像可按需追加）
FONT_URLS = (
    "https://github.com/alibaba-damo-academy/FunClip/raw/main/font/STHeitiMedium.ttc",
    "https://raw.githubusercontent.com/alibaba-damo-academy/FunClip/main/font/STHeitiMedium.ttc",
)

_MIN_SIZE = 1_000_000       # 正常字体 >50MB，这里只挡明显不完整的下载
_RETRY_INTERVAL = 300.0     # 同一进程内下载失败后的重试间隔（秒）
_chunk = 1024 * 1024
_last_fail = 0.0


def _is_valid(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > _MIN_SIZE
    except OSError:
        return False


def _download() -> bool:
    FONT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FONT_PATH.with_name(FONT_PATH.name + ".part")
    for url in FONT_URLS:
        try:
            print(f"[font] 正在下载字幕字体 STHeitiMedium.ttc（约 54MB，仅需一次）\n"
                  f"[font] 来源: {url}", flush=True)
            done = 0
            with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                while True:
                    block = r.read(_chunk)
                    if not block:
                        break
                    f.write(block)
                    done += len(block)
                    if done % (8 * _chunk) < _chunk:
                        print(f"[font] 已下载 {done // _chunk}MB ...", flush=True)
            if done < _MIN_SIZE:
                raise RuntimeError(f"下载数据异常：仅 {done} 字节")
            tmp.replace(FONT_PATH)
            print(f"[font] 字体就绪: {FONT_PATH}", flush=True)
            return True
        except Exception as e:  # noqa: BLE001 — 逐源尝试，全部失败才放弃
            log.warning("字体下载失败（%s）: %s", url, e)
            print(f"[font] 该来源下载失败: {e}", flush=True)
            tmp.unlink(missing_ok=True)
    return False


def ensure_font() -> Path | None:
    """确保 ``font/STHeitiMedium.ttc`` 存在，不存在则下载。

    返回字体绝对路径；彻底失败返回 ``None``（调用方应优雅降级）。
    """
    global _last_fail
    if _is_valid(FONT_PATH):
        return FONT_PATH
    if time.time() - _last_fail < _RETRY_INTERVAL:
        return None
    if _download():
        return FONT_PATH
    _last_fail = time.time()
    print("[font] 全部下载源失败。可手动下载 STHeitiMedium.ttc 放到 font/ 目录：\n"
          "[font]   https://github.com/alibaba-damo-academy/FunClip/tree/main/font",
          flush=True)
    return None


def ensure_font_dir() -> Path | None:
    """``ensure_font`` 的目录版：成功返回 ``font/`` 目录，失败返回 ``None``。

    供 ffmpeg ``fontsdir=`` 参数使用——返回 None 时调用方应**省略**该参数
    （libass 自动回退系统字体），与字体缺失时的既有行为一致。
    """
    p = ensure_font()
    return p.parent if p is not None else None
