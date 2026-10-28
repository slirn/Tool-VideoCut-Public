"""短视频多素材混剪项目服务。

短视频项目与长视频六阶段流水线分离：

- 项目元数据：``tasks/<task_id>/short_video/<project_id>/project.json``
- 上传素材：``tasks/<task_id>/short_video/<project_id>/assets/``
- 渲染产物：``tasks/<task_id>/outputs/short_videos/<project_id>/``

本模块只负责项目、素材、分镜规范化和持久化；实际编码在
``short_video_render.py``。外部 LLM 只用于生成结构化分镜，任何失败都会回退
到本地规则，避免把渲染主链路绑定到外部服务。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

SHORT_VIDEO_DIR = "short_video"
ASSETS_DIR = "assets"
OUTPUTS_DIRNAME = "outputs"
OUTPUTS_REL = Path("outputs") / "short_videos"
PROJECT_FILENAME = "project.json"
SCHEMA_VERSION = 1

# REQ-20261003-098：单源 AI 拆条工作台
# - kind="mixcut"（默认）= REQ-094 多素材混剪旧路径
# - kind="split" = 6 阶段 AI 拆条新路径
# 旧项目不主动迁移；新项目默认 kind="split"（可由调用方覆盖）。
PROJECT_KIND_MIXCUT = "mixcut"
PROJECT_KIND_SPLIT = "split"
PROJECT_KINDS = {PROJECT_KIND_MIXCUT, PROJECT_KIND_SPLIT}

# Stage 6（精剪混编）目录与产物命名
STAGE6_DIR = "stage6"
STAGE5_DIR = "stage5"
STAGE4_DIR = "stage4"
STAGE3_DIR = "stage3"
STAGE4_OPTIMIZE_DIR = "stage4_optimize"
STAGE2_DIR = "stage2"
STAGE1_DIR = "stage1"
FINAL_MP4_PREFIX = "final"
FINAL_SRT_PREFIX = "final_continuous"
RAW_SRT_NAME = "raw.srt"
RAW_JSON_NAME = "raw.json"
HIGHLIGHTS_JSON_NAME = "highlights.json"
HIGHLIGHTS_LLM_RAW_NAME = "llm_raw.txt"
COARSE_MP4_PREFIX = "coarse"
COARSE_SRT_PREFIX = "coarse"
REFINED_SRT_PREFIX = "refined"
# REQ-20261019-227：粗剪成片字幕三文件制前缀（subs_NN.srt / .asr.srt / .edits.json）
SUBS_SRT_PREFIX = "subs"

# Stage 3 模板 ID（与模板 prompt 一一对应）
HIGHLIGHT_TEMPLATE_HOOK_FIRST = "hook_first"
HIGHLIGHT_TEMPLATE_TOPIC_CLUSTER = "topic_cluster"
HIGHLIGHT_TEMPLATE_STORY_ARC = "story_arc"
HIGHLIGHT_TEMPLATES = (
    HIGHLIGHT_TEMPLATE_HOOK_FIRST,
    HIGHLIGHT_TEMPLATE_TOPIC_CLUSTER,
    HIGHLIGHT_TEMPLATE_STORY_ARC,
)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".ts", ".mpeg", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
ALLOWED_TRANSITIONS = {
    "none", "fade", "wipeleft", "wiperight", "slideup", "slidedown", "circleopen",
}


class ShortVideoError(ValueError):
    """短视频项目输入或状态错误。"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"


def default_config() -> dict:
    return {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "duration_min": 30,
        "duration_max": 60,
        "variants": 3,
        "transition": "fade",
        "transition_duration": 0.5,
        "bgm_volume_db": -16.0,
        "allow_external_llm": True,
    }


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def sanitize_config(cfg: dict | None) -> dict:
    base = default_config()
    if not isinstance(cfg, dict):
        return base
    for key in ("width", "height", "fps", "duration_min", "duration_max", "variants"):
        if key in cfg:
            base[key] = _clamp_int(cfg.get(key), base[key], 1, 10000)
    base["width"] = 1080
    base["height"] = 1920
    base["fps"] = _clamp_int(base.get("fps"), 30, 15, 60)
    base["duration_min"] = _clamp_int(base.get("duration_min"), 30, 5, 600)
    base["duration_max"] = _clamp_int(base.get("duration_max"), 60, base["duration_min"], 900)
    base["variants"] = _clamp_int(base.get("variants"), 3, 1, 5)
    if cfg.get("transition") in ALLOWED_TRANSITIONS:
        base["transition"] = cfg["transition"]
    base["transition_duration"] = _clamp_float(
        cfg.get("transition_duration"), 0.5, 0.1, 2.0
    )
    base["bgm_volume_db"] = _clamp_float(cfg.get("bgm_volume_db"), -16.0, -40.0, 0.0)
    base["allow_external_llm"] = bool(cfg.get("allow_external_llm", True))
    return base


def _rel(repo_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def _abs(repo_root: Path, value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (repo_root / p).resolve()


def project_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return Path(repo_root) / "tasks" / task_id / SHORT_VIDEO_DIR / project_id


def assets_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return project_dir(repo_root, task_id, project_id) / ASSETS_DIR


def output_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return Path(repo_root) / "tasks" / task_id / OUTPUTS_REL / project_id


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_project(repo_root: Path | str, task_id: str, project_id: str) -> dict:
    path = project_dir(repo_root, task_id, project_id) / PROJECT_FILENAME
    if not path.exists():
        raise ShortVideoError(f"短视频项目不存在: {project_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ShortVideoError(f"短视频项目损坏: {project_id}") from e
    if not isinstance(data, dict):
        raise ShortVideoError(f"短视频项目格式错误: {project_id}")
    return _normalize_project(data, task_id, project_id)


def save_project(repo_root: Path | str, project: dict) -> dict:
    task_id = str(project.get("task_id") or "").strip()
    project_id = str(project.get("id") or "").strip()
    if not task_id or not project_id:
        raise ShortVideoError("项目缺少 task_id 或 id")
    project["updated_at"] = now_iso()
    _write_json_atomic(project_dir(repo_root, task_id, project_id) / PROJECT_FILENAME, project)
    return project


def _normalize_project(data: dict, task_id: str, project_id: str) -> dict:
    data["_schema"] = SCHEMA_VERSION
    data["id"] = project_id
    data["task_id"] = task_id
    data.setdefault("name", "未命名短视频")
    data.setdefault("brief", "")
    data.setdefault("base_material_id", "")
    data.setdefault("created_at", now_iso())
    data.setdefault("updated_at", data["created_at"])
    # REQ-20261003-098：旧项目（缺 kind）默认 mixcut，新建走 split
    data.setdefault("kind", PROJECT_KIND_MIXCUT)
    if data["kind"] not in PROJECT_KINDS:
        data["kind"] = PROJECT_KIND_MIXCUT
    data["config"] = sanitize_config(data.get("config"))
    data.setdefault("materials", [])
    data.setdefault("storyboard", {"status": "empty", "variants": []})
    data.setdefault("render_jobs", [])
    # REQ-20261003-098：6 阶段流水线状态机（按 kind 决定是否初始化）
    pipeline = data.get("pipeline") or {}
    if not isinstance(pipeline, dict):
        pipeline = {}
    if data["kind"] == PROJECT_KIND_SPLIT:
        # 键迁移必须在下方 setdefault 初始化**之前**执行，否则新 key 已被填上
        # pending 默认值、迁移条件永远不成立。幂等：新 key 已有状态则保留新 key
        # （旧 key 仍清除）。
        # REQ-20261013-100：阶段重编号（3.5→4 内容优化、4→5 粗剪）。
        # REQ-20261016-148：Stage 6 字幕优化整阶段移除、精剪 7→6
        # （stage6_fine）。新键在前：同时存在旧两代精剪键时，较新的
        # stage7_finalize 先占位获胜。字幕优化的 refined srt 磁盘文件保留，
        # 精剪经文件回退链（stage5/refined*.srt）继续读取 → 仅丢键不删文件。
        for _old, _new in (
            ("stage7_finalize", "stage6_fine"),
            ("stage6_finalize", "stage6_fine"),
            ("stage4_coarse", "stage5_coarse"),
        ):
            if _old in pipeline:
                pipeline.setdefault(_new, pipeline.pop(_old))
        for _drop in ("stage6_refine", "stage5_refine", "stage3_verify"):
            pipeline.pop(_drop, None)
        for stage_key, defaults in (
            ("stage1_source", {"status": "pending"}),
            ("stage2_extract", {"status": "pending"}),
            ("stage3_analyze", {"status": "pending"}),
            ("stage4_optimize", {"status": "pending"}),
            ("stage5_coarse", {"status": "pending"}),
            ("stage6_fine", {"status": "pending"}),
        ):
            pipeline.setdefault(stage_key, dict(defaults))
        data["pipeline"] = pipeline
    return data


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_asset_name(filename: str) -> str:
    raw = Path(filename or "asset.bin").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(raw).stem).strip("._") or "asset"
    ext = Path(raw).suffix.lower()
    return stem[:80] + ext


def kind_from_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return "file"


def probe_media(path: Path) -> dict:
    """使用 ffprobe 读取媒体元数据；失败返回最小信息。"""
    result = {"duration": 0.0, "width": 0, "height": 0, "has_audio": False}
    if not path.exists():
        return result
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height,duration",
        "-of", "json", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8")
        data = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {}
    except Exception:  # noqa: BLE001
        return result
    fmt_duration = (data.get("format") or {}).get("duration")
    try:
        result["duration"] = max(0.0, float(fmt_duration or 0.0))
    except (TypeError, ValueError):
        pass
    for stream in data.get("streams") or []:
        ctype = stream.get("codec_type")
        if ctype == "audio":
            result["has_audio"] = True
        elif ctype == "video":
            try:
                result["width"] = int(stream.get("width") or 0)
                result["height"] = int(stream.get("height") or 0)
                if not result["duration"]:
                    result["duration"] = max(0.0, float(stream.get("duration") or 0.0))
            except (TypeError, ValueError):
                pass
    return result


def _auto_source_paths(repo_root: Path, task_id: str) -> list[Path]:
    task_dir = repo_root / "tasks" / task_id
    preferred_outputs = [
        task_dir / "outputs" / "optimize_compose.mp4",
        task_dir / "outputs" / "rough_compose.mp4",
        task_dir / "outputs" / "fine_export.mp4",
    ]
    # 同一成片可能同时存在优化版、粗剪版和精剪版，默认只导入最成熟的一版，
    # 避免 8 个 1080p 解码器同时打开导致解码缓冲竞争和内存峰值。
    paths = [next((p for p in preferred_outputs if p.exists()), None)]
    meta_path = task_dir / "metadata.json"
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            seg = data.get("segment") or {}
            if seg.get("path"):
                paths.append(_abs(repo_root, seg["path"]))
            if data.get("original_video_source"):
                paths.append(_abs(repo_root, data["original_video_source"]))
        except Exception:  # noqa: BLE001
            pass
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if path is None:
            continue
        key = str(path.resolve()).lower()
        if key not in seen and path.exists():
            out.append(path)
            seen.add(key)
    return [p for p in out if p is not None]


def _material_from_path(repo_root: Path, path: Path, kind: str, source: str) -> dict:
    info = probe_media(path)
    return {
        "id": new_id("mat"),
        "kind": kind,
        "name": path.name,
        "path": _rel(repo_root, path),
        "source": source,
        "duration": round(float(info.get("duration") or 0.0), 3),
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "has_audio": bool(info.get("has_audio")),
        "created_at": now_iso(),
    }


def create_project(
    repo_root: Path | str,
    task_id: str,
    name: str,
    brief: str = "",
    config: dict | None = None,
    *,
    kind: str = PROJECT_KIND_SPLIT,
) -> dict:
    root = Path(repo_root)
    tid = str(task_id or "").strip()
    if not tid:
        raise ShortVideoError("缺少 task_id")
    if not (root / "tasks" / tid).exists():
        raise ShortVideoError(f"任务不存在: {tid}")
    project_kind = str(kind or "").strip() or PROJECT_KIND_SPLIT
    if project_kind not in PROJECT_KINDS:
        raise ShortVideoError(f"未知项目类型: {project_kind}")
    project_id = new_id("sv")
    project = {
        "_schema": SCHEMA_VERSION,
        "id": project_id,
        "task_id": tid,
        "name": (name or "短视频混剪").strip()[:80],
        "brief": str(brief or "").strip()[:4000],
        "kind": project_kind,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "config": sanitize_config(config),
        "base_material_id": "",
        "materials": [],
        "storyboard": {"status": "empty", "variants": [], "model": "", "generated_at": ""},
        "render_jobs": [],
    }
    # REQ-20261003-098：仅 split 流程自动导入任务源视频；
    # mixcut 旧路径继续自动导入多个视频素材供 B-roll。
    if project_kind == PROJECT_KIND_SPLIT:
        for path in _auto_source_paths(root, tid):
            project["materials"].append(_material_from_path(root, path, "video", "auto"))
    save_project(root, project)
    return project


def list_projects_for_tasks(repo_root: Path | str, task_ids: Iterable[str]) -> list[dict]:
    root = Path(repo_root)
    out: list[dict] = []
    for tid in task_ids:
        base = root / "tasks" / tid / SHORT_VIDEO_DIR
        if not base.exists():
            continue
        for p in base.glob(f"*/{PROJECT_FILENAME}"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    out.append(_normalize_project(data, tid, p.parent.name))
            except Exception:  # noqa: BLE001
                log.warning("跳过损坏的短视频项目: %s", p)
    out.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return out


def delete_project(repo_root: Path | str, task_id: str, project_id: str) -> bool:
    root = Path(repo_root)
    pdir = project_dir(root, task_id, project_id)
    odir = output_dir(root, task_id, project_id)
    allowed_root = (root / "tasks").resolve()
    if not _is_relative_to(pdir, allowed_root) or not _is_relative_to(odir, allowed_root):
        raise ShortVideoError("拒绝删除任务目录之外的路径")
    existed = pdir.exists() or odir.exists()
    if pdir.exists():
        shutil.rmtree(pdir)
    if odir.exists():
        shutil.rmtree(odir)
    return existed


def add_material_path(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    source_path: Path | str,
    kind: str = "auto",
    display_name: str = "",
) -> dict:
    """把一个文件收进项目素材库。

    ``display_name``：用户选文件时的原始文件名（上传链路的临时文件名是一串
    随机字符，直接用会显示无意义字符串）。素材的 ``name`` 字段展示原始名，
    磁盘文件名仍做安全化 + 唯一前缀（防穿越 / 防重名覆盖）。
    """
    root = Path(repo_root)
    project = load_project(root, task_id, project_id)
    src = Path(source_path)
    if not src.exists() or not src.is_file():
        raise ShortVideoError(f"素材文件不存在: {src}")
    original_name = Path(str(display_name or "")).name or src.name
    resolved_kind = kind_from_extension(original_name) if kind in ("", "auto") else kind
    if resolved_kind == "bg_image" and Path(original_name).suffix.lower() not in IMAGE_EXTS:
        raise ShortVideoError("背景图片仅支持图片文件")
    if resolved_kind not in ("video", "image", "audio", "bg_image"):
        raise ShortVideoError("素材类型仅支持 video / image / audio / bg_image")
    dst_dir = assets_dir(root, task_id, project_id)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{new_id('asset')}_{_safe_asset_name(original_name)}"
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    material = _material_from_path(root, dst, resolved_kind, "upload")
    material["name"] = original_name
    project.setdefault("materials", []).append(material)
    save_project(root, project)
    return material


def remove_material(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    material_id: str,
) -> dict:
    """从项目素材库删除一个素材（REQ-20261001-096）。

    - 上传素材（source=upload）：连磁盘文件一起删（仅限本项目 assets 目录内）；
      任务自动导入的素材（source=auto）源文件属于任务本身，只移除引用不删文件。
    - 同步清理所有引用：基础视频、分镜 segments / overlays、BGM、背景图。
      基础视频被删后 ``base_material_id`` 置空，重新生成分镜时要求再选。
    """
    root = Path(repo_root)
    project = load_project(root, task_id, project_id)
    mid = str(material_id or "").strip()
    mat = next(
        (m for m in project.get("materials") or [] if m.get("id") == mid),
        None,
    )
    if not mat:
        raise ShortVideoError(f"素材不存在: {mid}")
    project["materials"] = [m for m in project["materials"] if m.get("id") != mid]
    if str(project.get("base_material_id") or "") == mid:
        project["base_material_id"] = ""
    for variant in (project.get("storyboard") or {}).get("variants") or []:
        if not isinstance(variant, dict):
            continue
        variant["segments"] = [
            s for s in variant.get("segments") or [] if s.get("material_id") != mid
        ]
        variant["overlays"] = [
            o for o in variant.get("overlays") or [] if o.get("material_id") != mid
        ]
        if variant.get("bgm_material_id") == mid:
            variant["bgm_material_id"] = ""
        if variant.get("bg_material_id") == mid:
            variant["bg_material_id"] = ""
    if str(mat.get("source") or "") == "upload":
        target = _abs(root, mat.get("path") or "")
        assets_root = assets_dir(root, task_id, project_id).resolve()
        if _is_relative_to(target, assets_root) and target.is_file():
            try:
                target.unlink(missing_ok=True)
            except OSError as e:  # noqa: PERF203
                log.warning("[short_video] 素材文件删除失败: %s (%s)", target, e)
    save_project(root, project)
    return project


def _fallback_variant(project: dict, index: int, base_id: str = "") -> dict:
    materials = project.get("materials") or []
    images = [m for m in materials if m.get("kind") == "image"]
    audios = [m for m in materials if m.get("kind") == "audio"]
    # REQ-20261001-095：主片段只用用户选定的基础视频，不再轮询全部视频素材
    base = next((m for m in materials if m.get("id") == base_id
                 and m.get("kind") == "video"), None)
    if not base:
        raise ShortVideoError("请先选择基础视频素材")
    cfg = sanitize_config(project.get("config"))
    target = min(cfg["duration_max"], max(cfg["duration_min"], 42 + index * 6))
    n_segments = 4
    segment_duration = max(3.0, min(8.0, target / n_segments))
    mat_duration = max(0.0, float(base.get("duration") or 0.0))
    if mat_duration > 0:
        segment_duration = min(segment_duration, mat_duration)
        n_segments = max(1, min(n_segments, int(mat_duration / max(1.0, segment_duration)) or 1))
    segments = []
    for i in range(n_segments):
        start = 0.0
        if mat_duration > 0 and n_segments > 1:
            span = max(0.0, mat_duration - segment_duration)
            start = round(span * i / (n_segments - 1), 3)
        segments.append({
            "id": f"seg_{index + 1}_{i + 1}",
            "material_id": base["id"],
            "start": start,
            "duration": round(max(1.0, segment_duration), 3),
            "transition": cfg["transition"],
        })
    overlays = []
    if images:
        overlays.append({
            "id": f"ov_{index + 1}_1",
            "material_id": images[index % len(images)]["id"],
            "start": 2.0,
            "duration": 3.0,
            "x": 690,
            "y": 150,
            "scale": 0.38,
        })
    brief = str(project.get("brief") or project.get("name") or "精彩内容").strip()
    chunks = [x.strip() for x in re.split(r"[。！？!?；;\n]+", brief) if x.strip()]
    if not chunks:
        chunks = ["精彩内容", "马上来看", "关注获取更多"]
    subtitles = []
    total = sum(float(s["duration"]) for s in segments)
    slot = max(1.5, min(4.0, total / max(1, len(chunks))))
    for i, text in enumerate(chunks[:12]):
        start = round(i * slot, 3)
        subtitles.append({
            "start": start,
            "end": round(min(total, start + slot * 0.92), 3),
            "text": text[:40],
        })
    return {
        "id": f"v{index + 1}",
        "name": f"版本 {index + 1}",
        "title": str(project.get("name") or "短视频")[:30],
        "hook": brief[:24],
        "cta": "关注获取更多内容",
        "segments": segments,
        "overlays": overlays,
        "subtitles": subtitles,
        "bgm_material_id": audios[index % len(audios)]["id"] if audios else "",
        "bgm_volume_db": cfg["bgm_volume_db"],
    }


def _extract_json(raw: str) -> dict:
    text = re.sub(r"```(?:json)?\s*|\s*```", "", raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ShortVideoError("LLM 未返回 JSON 对象")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ShortVideoError("LLM 返回值不是 JSON 对象")
    return data


def _llm_storyboard(repo_root: Path, project: dict, base_id: str = "") -> tuple[dict, str]:
    from slirn_home import llm_config, revision_service

    entry = llm_config.get_current_entry(repo_root)
    if not entry:
        raise ShortVideoError("未配置 LLM 模型")
    if not os.environ.get(str(entry.get("api_key_env") or ""), ""):
        raise ShortVideoError("当前模型 API Key 环境变量未配置")
    mats = [
        {
            "id": m.get("id"),
            "kind": m.get("kind"),
            "name": m.get("name"),
            "duration": m.get("duration"),
        }
        for m in (project.get("materials") or [])
    ]
    system = (
        "你是短视频混剪导演和分镜编辑。根据用户 Brief 与素材清单，输出可执行的"
        "结构化分镜 JSON。不要输出解释或 Markdown。所有素材引用必须使用给定 id。"
        "用户已指定基础视频（base_material_id）：segments 的 material_id 只能使用"
        "该基础视频（可在不同起点截取多段）；其余素材只能用于 overlays / BGM。"
    )
    user = json.dumps({
        "brief": project.get("brief") or "",
        "project_name": project.get("name") or "",
        "config": project.get("config") or default_config(),
        "base_material_id": base_id,
        "materials": mats,
        "required_shape": {
            "variants": [{
                "id": "v1",
                "name": "版本 1",
                "title": "标题",
                "hook": "开头 Hook",
                "cta": "结尾行动号召",
                "segments": [{
                    "material_id": "mat_id",
                    "start": 0,
                    "duration": 5,
                    "transition": "fade",
                }],
                "overlays": [{
                    "material_id": "mat_id",
                    "start": 2,
                    "duration": 3,
                    "x": 690,
                    "y": 150,
                    "scale": 0.38,
                }],
                "subtitles": [{"start": 0, "end": 2.5, "text": "字幕"}],
                "bgm_material_id": "mat_id",
                "bgm_volume_db": -16,
            }]
        },
    }, ensure_ascii=False)
    raw = revision_service._call_llm(system, user, entry=entry, retries=1,
                                     usage_ctx={"stage": "短视频-AI编排"})
    return _extract_json(raw), str(entry.get("id") or "")


def _normalize_variant(
    variant: dict,
    project: dict,
    index: int,
) -> dict:
    materials = {m.get("id"): m for m in project.get("materials") or []}
    cfg = sanitize_config(project.get("config"))
    v = variant if isinstance(variant, dict) else {}
    result = {
        "id": str(v.get("id") or f"v{index + 1}")[:40],
        "name": str(v.get("name") or f"版本 {index + 1}")[:60],
        "title": str(v.get("title") or project.get("name") or "短视频")[:60],
        "hook": str(v.get("hook") or project.get("brief") or "精彩内容")[:80],
        "cta": str(v.get("cta") or "关注获取更多内容")[:60],
        "segments": [],
        "overlays": [],
        "subtitles": [],
        "bgm_material_id": str(v.get("bgm_material_id") or ""),
        "bgm_volume_db": _clamp_float(v.get("bgm_volume_db"), cfg["bgm_volume_db"], -40.0, 0.0),
        # REQ-20261001-095：背景图只接受 bg_image 素材（普通 image 是 B-roll）
        "bg_material_id": "",
    }
    bg_mat = materials.get(str(v.get("bg_material_id") or ""))
    if bg_mat and bg_mat.get("kind") == "bg_image":
        result["bg_material_id"] = bg_mat["id"]
    for seg in (v.get("segments") or [])[:6]:
        if not isinstance(seg, dict):
            continue
        mat = materials.get(seg.get("material_id"))
        if not mat or mat.get("kind") != "video":
            continue
        duration = _clamp_float(seg.get("duration"), 5.0, 0.5, 30.0)
        if float(mat.get("duration") or 0) > 0:
            duration = min(duration, float(mat["duration"]))
        transition = str(seg.get("transition") or cfg["transition"])
        if transition not in ALLOWED_TRANSITIONS:
            transition = cfg["transition"]
        result["segments"].append({
            "id": str(seg.get("id") or new_id("seg"))[:50],
            "material_id": mat["id"],
            "start": round(max(0.0, _clamp_float(seg.get("start"), 0.0, 0.0, 86400.0)), 3),
            "duration": round(max(0.5, duration), 3),
            "transition": transition,
        })
    for ov in (v.get("overlays") or [])[:8]:
        if not isinstance(ov, dict):
            continue
        mat = materials.get(ov.get("material_id"))
        if not mat or mat.get("kind") not in ("video", "image"):
            continue
        result["overlays"].append({
            "id": str(ov.get("id") or new_id("ov"))[:50],
            "material_id": mat["id"],
            "start": round(max(0.0, _clamp_float(ov.get("start"), 0.0, 0.0, 86400.0)), 3),
            "duration": round(_clamp_float(ov.get("duration"), 3.0, 0.5, 30.0), 3),
            "x": _clamp_int(ov.get("x"), 690, -2000, 3000),
            "y": _clamp_int(ov.get("y"), 150, -3000, 4000),
            "scale": round(_clamp_float(ov.get("scale"), 0.38, 0.05, 3.0), 4),
        })
    for sub in (v.get("subtitles") or [])[:40]:
        if not isinstance(sub, dict):
            continue
        text = str(sub.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, _clamp_float(sub.get("start"), 0.0, 0.0, 86400.0))
        end = max(start + 0.2, _clamp_float(sub.get("end"), start + 2.0, 0.0, 86400.0))
        result["subtitles"].append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text[:120],
        })
    if not result["segments"]:
        raise ShortVideoError("分镜没有有效视频片段")
    total = sum(float(s["duration"]) for s in result["segments"])
    if result["subtitles"]:
        result["subtitles"] = [
            {
                **s,
                "start": round(min(float(s["start"]), max(0.0, total - 0.2)), 3),
                "end": round(min(float(s["end"]), total), 3),
            }
            for s in result["subtitles"]
            if float(s["end"]) <= total + 0.001 or float(s["start"]) < total
        ]
    return result


def generate_storyboard(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    *,
    use_external_llm: bool | None = None,
    base_material_id: str | None = None,
) -> dict:
    """生成分镜。REQ-20261001-095：必须由用户指定基础视频（不再自动挑选）。

    ``base_material_id`` 显式传参优先，否则读项目里保存的选择；
    缺失 / 非法 → 抛错（前端提示用户先在下拉里选择）。
    """
    root = Path(repo_root)
    project = load_project(root, task_id, project_id)
    base_id = str(base_material_id or "").strip() or str(project.get("base_material_id") or "")
    base_mat = next(
        (m for m in project.get("materials") or []
         if m.get("id") == base_id and m.get("kind") == "video"),
        None,
    )
    if not base_mat:
        raise ShortVideoError("请先选择基础视频（生成分镜的主素材）")
    project["base_material_id"] = base_mat["id"]
    outputs_dir = root / "tasks" / task_id / "outputs"
    exec_id = ""
    try:
        from slirn_home import execution_history

        exec_id = execution_history.record_start(
            outputs_dir,
            execution_history.KIND_SHORT_VIDEO_AI,
            extra={"project_id": project_id},
        )
    except Exception:  # noqa: BLE001
        pass
    cfg = sanitize_config(project.get("config"))
    allow = cfg["allow_external_llm"] if use_external_llm is None else bool(use_external_llm)
    model = ""
    error = ""
    variants_raw: list[dict] = []
    if allow:
        try:
            payload, model = _llm_storyboard(root, project, base_mat["id"])
            variants_raw = list(payload.get("variants") or [])
        except Exception as e:  # noqa: BLE001
            error = str(e)
            log.warning("[short_video] LLM 分镜失败，转本地兜底: %s", e)
    if not variants_raw:
        model = "local-rules"
        variants_raw = [_fallback_variant(project, i, base_mat["id"])
                        for i in range(cfg["variants"])]
    variants = []
    for i, raw in enumerate(variants_raw[: cfg["variants"]]):
        try:
            # LLM 违规引用了非基础视频 → 统一替换为基础视频再规范化
            if isinstance(raw, dict):
                for seg in raw.get("segments") or []:
                    if isinstance(seg, dict) and seg.get("material_id") != base_mat["id"]:
                        seg["material_id"] = base_mat["id"]
            variants.append(_normalize_variant(raw, project, i))
        except ShortVideoError:
            variants.append(
                _normalize_variant(_fallback_variant(project, i, base_mat["id"]), project, i)
            )
    if len(variants) < cfg["variants"]:
        for i in range(len(variants), cfg["variants"]):
            variants.append(
                _normalize_variant(_fallback_variant(project, i, base_mat["id"]), project, i)
            )
    project["storyboard"] = {
        "status": "ready",
        "model": model,
        "generated_at": now_iso(),
        "error": error,
        "base_material_id": base_mat["id"],
        "variants": variants,
    }
    save_project(root, project)
    if exec_id:
        try:
            from slirn_home import execution_history

            execution_history.patch_extra(
                outputs_dir, exec_id, {"project_id": project_id, "model": model}
            )
            execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
        except Exception:  # noqa: BLE001
            pass
    return project


def update_project(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    payload: dict,
) -> dict:
    root = Path(repo_root)
    project = load_project(root, task_id, project_id)
    if "name" in payload:
        project["name"] = str(payload.get("name") or project["name"]).strip()[:80]
    if "brief" in payload:
        project["brief"] = str(payload.get("brief") or "").strip()[:4000]
    if "base_material_id" in payload:
        value = str(payload.get("base_material_id") or "").strip()
        if value:
            mat = next(
                (m for m in project.get("materials") or []
                 if m.get("id") == value and m.get("kind") == "video"),
                None,
            )
            if not mat:
                raise ShortVideoError("基础视频必须是项目里的视频素材")
            value = mat["id"]
        project["base_material_id"] = value
    if isinstance(payload.get("config"), dict):
        project["config"] = sanitize_config({**project.get("config", {}), **payload["config"]})
    if isinstance(payload.get("storyboard"), dict):
        raw_variants = payload["storyboard"].get("variants")
        if isinstance(raw_variants, list):
            normalized = []
            for i, variant in enumerate(raw_variants[: project["config"]["variants"]]):
                normalized.append(_normalize_variant(variant, project, i))
            project["storyboard"] = {
                **project.get("storyboard", {}),
                "status": "ready",
                "variants": normalized,
                "updated_at": now_iso(),
            }
    save_project(root, project)
    return project


def material_abs_path(repo_root: Path | str, project: dict, material_id: str) -> Path:
    for material in project.get("materials") or []:
        if material.get("id") == material_id:
            return _abs(Path(repo_root), material.get("path") or "")
    raise ShortVideoError(f"素材不存在: {material_id}")


def select_source_material(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    material_id: str,
) -> dict:
    """Stage 1：把指定素材标记为「源视频」（split 流程的主素材）。

    与旧 mixcut 的 ``base_material_id`` 共享同一字段（语义上一致），
    校验素材存在 + kind=video。保存后会自动重置下游阶段。
    """
    root = Path(repo_root)
    project = load_project(root, task_id, project_id)
    mat = next(
        (m for m in project.get("materials") or []
         if m.get("id") == material_id and m.get("kind") == "video"),
        None,
    )
    if not mat:
        raise ShortVideoError("源视频必须是项目里的视频素材")
    # REQ-20261022-144：写执行日志（选源是主要功能之一）
    _exec_id = ""
    try:
        from slirn_home import execution_history
        _exec_id = execution_history.record_start(
            sv_logs_dir(root, task_id),
            execution_history.KIND_SHORT_VIDEO_SOURCE,
            extra={"project_id": project_id, "material_id": material_id,
                   "material_name": mat.get("name") or ""},
        )
    except Exception:  # noqa: BLE001 — 日志失败不影响主流程
        pass
    project["base_material_id"] = mat["id"]
    pipeline = project.setdefault("pipeline", {})
    pipeline["stage1_source"] = {
        "status": "done",
        "source_material_id": mat["id"],
        "source_name": mat.get("name") or "",
        "duration": float(mat.get("duration") or 0.0),
        "width": int(mat.get("width") or 0),
        "height": int(mat.get("height") or 0),
        "set_at": now_iso(),
    }
    # 源变了 → 下游 stage 2-6 全部作废
    reset_downstream_stages(project, "stage1_source")
    save_project(root, project)
    return project


# ---------- REQ-20261003-098：Stage 6（精剪混编）辅助 ----------

def sv_logs_dir(repo_root: Path | str, task_id: str) -> Path:
    """短视频执行日志落盘目录：``tasks/<tid>/outputs/``。

    REQ-20261022-144：与长视频**同一文件**（execution_history.json），
    这样 ``/slirn/api/list_logs``（只读任务级 outputs）就能查到短视频日志 ——
    此前短视频写在自己的 stage 目录里，写了也查不到。
    """
    return Path(repo_root) / "tasks" / str(task_id) / "outputs"


def stage6_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    """Stage 6 产物目录：``tasks/<tid>/short_video/<pid>/stage6/``。

    与长视频项目目录（``tasks/<tid>/outputs/...``）完全隔离 —— 长视频的
    cut_by_srt.py 产物不会写到本目录，反之亦然。
    """
    return project_dir(repo_root, task_id, project_id) / STAGE6_DIR


# REQ-20261016-142 的 stage6_preview_dir 已随字幕优化阶段移除（REQ-20261016-148）


def stage5_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return project_dir(repo_root, task_id, project_id) / STAGE5_DIR


def stage4_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return project_dir(repo_root, task_id, project_id) / STAGE4_DIR


def stage3_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return project_dir(repo_root, task_id, project_id) / STAGE3_DIR


def stage4_optimize_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return project_dir(repo_root, task_id, project_id) / STAGE4_OPTIMIZE_DIR


def stage2_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return project_dir(repo_root, task_id, project_id) / STAGE2_DIR


def stage1_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return project_dir(repo_root, task_id, project_id) / STAGE1_DIR


def stage_raw_srt_path(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return stage2_dir(repo_root, task_id, project_id) / RAW_SRT_NAME


def stage_raw_json_path(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return stage2_dir(repo_root, task_id, project_id) / RAW_JSON_NAME


def stage_highlights_path(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return stage3_dir(repo_root, task_id, project_id) / HIGHLIGHTS_JSON_NAME


def stage_llm_raw_path(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    return stage3_dir(repo_root, task_id, project_id) / HIGHLIGHTS_LLM_RAW_NAME


CUSTOM_PROMPTS_NAME = "custom_prompts.json"


def custom_prompts_path(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    """REQ-20261004-stage3-prompt-edit：用户修改后的 3 个模板提示词。"""
    return stage3_dir(repo_root, task_id, project_id) / CUSTOM_PROMPTS_NAME


def load_custom_prompts(repo_root: Path | str, task_id: str, project_id: str) -> dict:
    """读 custom_prompts.json。文件不存在/损坏 → 返回空 dict。

    返回 {template_name: {"prompt": "...", "updated_at": "..."}}。
    """
    p = custom_prompts_path(repo_root, task_id, project_id)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_custom_prompt(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    template: str,
    prompt: str,
) -> dict:
    """保存用户对某模板的修改提示词。覆盖同名 key。"""
    root = Path(repo_root)
    data = load_custom_prompts(root, task_id, project_id)
    data[template] = {
        "prompt": str(prompt or "").strip(),
        "updated_at": now_iso(),
    }
    p = custom_prompts_path(root, task_id, project_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(p, data)
    return data[template]


def reset_custom_prompt(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    template: str,
) -> bool:
    """删除某模板的用户修改（恢复系统默认）。"""
    root = Path(repo_root)
    data = load_custom_prompts(root, task_id, project_id)
    if template not in data:
        return False
    del data[template]
    p = custom_prompts_path(root, task_id, project_id)
    if data:
        _write_json_atomic(p, data)
    else:
        # 空 dict → 删文件
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    return True


# ---------- REQ-20261012-099：全局提示词两级存储（默认 + 最新 10 条历史） ----------

PROMPT_SETTINGS_REL = Path("config") / "short_video_prompts.json"
PROMPT_HISTORY_MAX = 10
STAGE3_IMAGE_BASENAME = "input_image"
ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# REQ-20261013-108：多 stage 提示词存储（全局默认 + 历史）
# 数据形态：{stage3: {default_prompt, history}, stage4: {default_prompt, history}}
# 旧形态（顶层 default_prompt / history）自动视为 stage3。
_PROMPT_DEFAULT_KEY = "default_prompt"
_PROMPT_HISTORY_KEY = "history"
_PROMPT_VALID_STAGES = ("stage3", "stage4")

# REQ-20261012-099 修订十：Stage 3 系统级提示词 —— 内置在系统里、独立于
# 用户「默认 / 历史」两级存储（不占历史名额、不被用户保存覆盖）。
# 「🧭 引用系统提示词」按钮把它一键填入文本域，之后照常编辑 / 解析 / 设为默认。
# 输出格式约束仍由 run_stage3 拼在 user payload 尾部（含「忽略上文的其他
# 输出格式要求」），因此本提示词只管选题与选行标准，不复述输出格式。
#
# REQ-20261015-121：条数 + 时长上下限参数化——模板带 {n}/{dmin}/{dmax}/
# {dmin2}/{dmax2}/{ex_sec}/{ex_lines} 占位符（文本无字面花括号，.format 安全），
# stage3_system_prompt() 按用户设置渲染；UI「引用」按钮带 data-prompt-tmpl，
# JS 在点击时按当前三个输入框的值做同口径替换（Python/JS 双端同公式）。
_STAGE3_SYSTEM_PROMPT_TMPL = """你是一个短视频内容导演，输入一门课程的完整 srt 字幕，从中挑选并组织出 {n} 条独立的短视频。

# 选题类型（每条四选一）
- 【痛点】解决创作者具体障碍
- 【痒点】展示令人向往的效果或案例
- 【干货】具体步骤、框架、方法、工具
- 【反差】违反直觉的观点或强结论
每条的标题以对应类型开头，例如：# 1 | 【干货】三步搭建AI脚本工作流

# 多条短视频相互独立（硬性要求）
- 最终生成的 {n} 条短视频，相互之间不存在任何前后关系或依赖关系：每条短视频说的都是自己独立的一件事；观众随机单独看到任何一条都能完整理解，不需要看过其他条，也不允许一条承接、引用或延续另一条的内容。
- 因此所有标准（尤其是时长）都必须逐条独立判断：禁止用各条的平均时长来代替判断——必须单独核对每一条是否满足时长上下限，任何一条不达标都算不达标，「平均达标」无效。

# 选行规则
1. 每条短视频由若干条字幕行组成；每行必须逐字取自 srt 原文，时间戳用该行在 srt 中的真实时间。
2. 一行输出只对应 srt 的一行字幕，禁止把多句合并成一条。
3. 优先选时间上相邻的连续句子（剪辑观感更顺）；仅当叙事确有必要时才跨位置挑行，行序按叙事逻辑排列（可以与 srt 原序不同）。

# 切片筛选标准（每条必须全部满足，缺一不可）
1. 信息密度高：含具体观点、步骤、工具名或数字。
2. 独立完整：不看完整课程、也不依赖其他条短视频，单独一条即可获得完整价值。
3. **时长硬约束（{dmin}-{dmax} 秒）**：每条全部字幕的「朗读时长合计」必须落在 {dmin}-{dmax} 秒区间（含端点），这是输出底线，不允许任何一条短视频的总字幕时长低于 {dmin} 秒或高于 {dmax} 秒。时长必须逐条单独判断，不允许用各条平均时长代替——平均落在区间而某一条不达标，仍然是不达标。
4. 开头有引子：第一行必须自带钩子（问句 / 反差 / 数字 / 强结论），且能自然引入本条话题——禁止第一句是莫名其妙、与话题无关或悬空的一句。
5. 主题契合：与 IP、AI影视、故事叙事、商业化强相关。
6. 主题纯净：只选与本条主题直接相关的行；无关行即使时间相邻也不选。
7. 结尾关闭话题：最后一句必须能收束、关闭本条话题（结论 / 总结 / 点题 / 行动号召），不允许话题开着就结束、戛然而止。

# 时长计算口径（以每行末尾标注的真实秒数为准，禁止用字符数估算）
- 解析输入的每行字幕末尾都带有真实时长标注（如「（2.1s）」），这个秒数 = 该行在 srt 中时间戳区间的真实长度（end_ms - start_ms）；每条短视频的总时长 = 该条所有选中行标注秒数之和。
- 绝对不要用「行数 × 平均 2 秒」之类的捷径估算——课程语速差异大（每行 1.0s~4.5s 都常见），平均估算几乎一定会偏离 1-3 秒。把每行末尾的标注当成可加数据直接相加，才是唯一正确口径。
- 例：一条短视频选了 3 行，标注分别是「（2.5s）」「（2.2s）」「（2.8s）」→ 总时长 = 2.5 + 2.2 + 2.8 = 7.5 秒，落在 {dmin}-{dmax} 区间 ✓。
- 阈值边界：低于 {dmin} 秒必须补行（哪怕加一句过渡），超过 {dmax} 秒必须砍行；不允许"差不多就行"。

# 凑时长/压时长的具体手段（按需使用）
- 时长 < {dmin} 秒（短了）：
  ① 在尾部追加一句与主题强相关的「收束 / 引申 / 案例补充」字幕行（时间相邻最优）；
  ② 仍不够就把开头延展一两句更早的铺垫行（必须保持逻辑连贯）；
  ③ 不要为了凑时长而塞入口水话或无关行。
- 时长 > {dmax} 秒（长了）：
  ① 优先砍重复 / 冗余 / 与核心论点弱相关的行；
  ② 把铺陈过长的一段压缩到只剩「钩子 + 关键论据 + 收束」三段式；
  ③ 不要为了压时长而砍掉结论或关键论据。
- **时长不达标的处理原则**：时长不达标 = 不可输出，必须重选；宁可在叙事完整度上让一点点步，也要把总时长拉到 {dmin}-{dmax} 区间内。

# 过滤（以下内容一律不选）
纯开场、纯过渡、口水话、运营通知、进群报名加微信等无传播价值内容。

# 内容质量门槛（与上述标准并行生效）
1. 主题传达完整：选出的字幕合起来必须完整传达标题的核心信息——有引入、有展开、有收束，不遗漏关键论据，不做零散堆砌。
2. 文字忠实原文：禁止改写、缩写、拼接或杜撰字幕文字。
3. 逻辑连贯：相邻两句语义衔接自然，无话题跳跃、无自相矛盾、无因缺句造成的语义断裂。

# 输出前自检（内部完成，不要把自检过程写进输出）
**1) 时长硬约束自检（最先做）**：
- 逐条把每条全部选中行末尾的（X.Xs）标注相加（禁止用各条平均时长代替），落在 [{dmin}, {dmax}] 区间？否 → 回到字幕里增/删行再加一遍，达标才进入下一项。
**2) 其它标准自检**：
- 信息密度 / 独立完整 / 开头引子 / 主题契合 / 主题纯净 / 结尾关闭话题 / 内容质量门槛逐条核对（同样逐条独立判断，不看平均），任何一项不满足，必须放弃当前选行、回到字幕中重新选取，直到全部满足才允许输出。
- 宁缺毋滥：宁可这条短视频少几行字幕，也不选入表达不完整或逻辑不通的行。
**3) 开头 / 结尾 / 中段专项自检**：
- 第一句单独拿出来，观众不看上下文能否明白在讲什么？它是否是能自然引入话题的引子，而不是莫名其妙的一句？
- 最后一句是否对主题形成收束、把话题关闭，而不是戛然而止或话说到一半？
- 中间是否缺少让逻辑成立的关键过渡句？"""


def stage3_system_prompt_template() -> str:
    """系统提示词模板原文（带占位符；UI data-prompt-tmpl / JS 替换用）。"""
    return _STAGE3_SYSTEM_PROMPT_TMPL


def stage3_system_prompt(
    n_clips: int = 4, duration_min_sec: float = 60.0, duration_max_sec: float = 90.0,
) -> str:
    """REQ-20261015-121：按用户设置渲染系统提示词。

    - 条数 / 时长上下限直接取调用方参数（与解析端 _norm_duration_range
      的兜底口径一致：这里只做文案渲染，倒挂时互换、非正值回默认）；
    - REQ-20261016-127：去掉「行数 × 平均 2s」折算类占位符
      （{dmin2}/{dmax2}/{ex_sec}/{ex_lines}），统一用真实时间戳加总口径。
    """
    dmin = int(round(float(duration_min_sec)))
    dmax = int(round(float(duration_max_sec)))
    if dmin <= 0 or dmax <= 0:
        dmin, dmax = 60, 90
    if dmax < dmin:
        dmin, dmax = dmax, dmin
    n = int(n_clips) if int(n_clips) > 0 else 4
    return _STAGE3_SYSTEM_PROMPT_TMPL.format(
        n=n, dmin=dmin, dmax=dmax,
    )


# 默认渲染版（60-90 / 4 条）：向后兼容既有引用方与测试。
STAGE3_SYSTEM_PROMPT = stage3_system_prompt()

# REQ-20261013-108：Stage 4「内容优化」系统级提示词
# 「🧭 引用系统提示词」按钮把本字符串一键填入文本域；用户编辑后照常
# 「💾 设为默认 / 📜 存入列表」覆盖全局默认 / 历史。
# REQ-20261019-219 重写要点（删除式优化）：
# 1) 唯一优化手段 = 删除（删整行 / 行内删词），严禁新增音频文字之外的任何文字
# 2) op 只剩 keep / trim（行内删词 = 重新切分，token 对齐出保留段时间戳），
#    fix（改写）/ split（拆行）/ 段内重排全部下线（重排保留为用户手动功能）
# 3) 解析侧逐 token 校验「删除式」：trim text 必须是原文的有序子序列，
#    对不上 = 保留原行（宁可不改不可改错）— 不信任 LLM 自律
STAGE4_SYSTEM_PROMPT = """你是短视频字幕优化师。输入一条短视频的标题和带编号的字幕行（每行 1 个 src_index 引用了原 SRT 的真实时间），按规则决策并只输出一个 JSON 对象。

# 任务目标
让这条短视频的字幕【信息完整 + 逻辑通顺 + 阅读顺滑】。**唯一的优化手段是删除**：删掉无信息量的整行，或删掉行内无信息量的字词。**严禁增加任何音频文字之外的文字**——不造词、不改写、不同义替换、不扩写、不合并出原文没有的句子。

# 优化维度（全部是「删」）
1. 删冗行：与标题主题无关、纯过渡、纯重复、无独立信息量的整行 → 在 lines 中省略该编号。
2. 去口水词：行内的"嗯""啊""呃""那个""就是说""对吧""然后呢"等无信息量语气词 → 用 trim 删掉，输出删减后的整行文字。
3. 去重复：相邻两行意思一致 → 保留信息更全的一条，另一条省略。
4. trim 后的文字必须仍是原行文字**去掉若干字词**后的结果；删完与原文一样 → 用 keep。

# 决策输出（每行 3 选 1）
- keep：不动 → {"no": 行号, "op": "keep"}
- trim：行内删词（= 重新切分，系统按剩余文字对齐原时间戳切视频）→ {"no": 行号, "op": "trim", "text": "删掉无用词后的整行文字"}
- 删整行：**不要输出 `{"op": "delete"}`**！在 `lines` 数组中**省略**对应 `no` 即可。输出 `op: "delete"` 一律视为保留（系统会忽略）。

# 强约束
1. trim 的 text 必须是原行文字去掉若干字词后的结果：保留字词的相对顺序不变、不新增任何字符（包括标点）。系统会逐字校验，对不上 = 该行保持原样。
2. 不新增行、不把两行合并成一行、不改标题、不调整行的先后顺序。
3. 输出的 no 必须是输入中真实存在的编号；未列出的 no 视为删除。

只输出严格 JSON：{"lines": [{"no": 1, "op": "keep"}, {"no": 2, "op": "trim", "text": "..."}]}，不要任何解释、代码块标记或额外文字。"""


def prompt_settings_path(repo_root: Path | str) -> Path:
    """全局提示词设置文件（config/short_video_prompts.json，机器本地运行时状态）。"""
    return Path(repo_root) / PROMPT_SETTINGS_REL


def _normalize_stage(stage: str) -> str:
    """Stage 名规范化（默认 stage3）。非法 → stage3。"""
    s = str(stage or "stage3").strip().lower()
    if s not in _PROMPT_VALID_STAGES:
        return "stage3"
    return s


def _empty_stage_node() -> dict:
    return {_PROMPT_DEFAULT_KEY: None, _PROMPT_HISTORY_KEY: []}


def _migrate_legacy_shape(data: dict) -> dict:
    """旧形态（顶层 default_prompt / history）→ 新形态 {stage3: {...}}。

    旧文件只有一个 stage（Stage 3）的内容；迁移到 stage3 节点后，
    stage4 节点为空（首次写入时再创建）。
    """
    if not isinstance(data, dict):
        return {"stage3": _empty_stage_node(), "stage4": _empty_stage_node()}
    if "stage3" in data or "stage4" in data:
        # 已经是新形态
        out = {"stage3": _empty_stage_node(), "stage4": _empty_stage_node()}
        for s in _PROMPT_VALID_STAGES:
            node = data.get(s)
            if isinstance(node, dict):
                dp = node.get(_PROMPT_DEFAULT_KEY)
                if isinstance(dp, dict) and str(dp.get("prompt") or "").strip():
                    out[s][_PROMPT_DEFAULT_KEY] = {
                        "prompt": str(dp.get("prompt")),
                        "updated_at": str(dp.get("updated_at") or ""),
                    }
                hist = node.get(_PROMPT_HISTORY_KEY)
                if isinstance(hist, list):
                    for item in hist[:PROMPT_HISTORY_MAX]:
                        if isinstance(item, dict) and str(item.get("prompt") or "").strip():
                            out[s][_PROMPT_HISTORY_KEY].append({
                                "prompt": str(item.get("prompt")),
                                "updated_at": str(item.get("updated_at") or ""),
                            })
        return out
    # 旧形态：顶层 default_prompt / history
    s3 = _empty_stage_node()
    dp = data.get(_PROMPT_DEFAULT_KEY)
    if isinstance(dp, dict) and str(dp.get("prompt") or "").strip():
        s3[_PROMPT_DEFAULT_KEY] = {
            "prompt": str(dp.get("prompt")),
            "updated_at": str(dp.get("updated_at") or ""),
        }
    hist = data.get(_PROMPT_HISTORY_KEY)
    if isinstance(hist, list):
        for item in hist[:PROMPT_HISTORY_MAX]:
            if isinstance(item, dict) and str(item.get("prompt") or "").strip():
                s3[_PROMPT_HISTORY_KEY].append({
                    "prompt": str(item.get("prompt")),
                    "updated_at": str(item.get("updated_at") or ""),
                })
    return {"stage3": s3, "stage4": _empty_stage_node()}


def _read_full_settings(repo_root: Path | str) -> dict:
    """读全文件（多 stage 形态）。损坏/不存在 → 空多 stage 节点。"""
    p = prompt_settings_path(repo_root)
    empty = {"stage3": _empty_stage_node(), "stage4": _empty_stage_node()}
    if not p.is_file():
        return empty
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return empty
    return _migrate_legacy_shape(data)


def load_prompt_settings(repo_root: Path | str, *, stage: str = "stage3") -> dict:
    """读全局提示词设置。损坏/不存在 → 空结构。

    ``stage`` ∈ ``{"stage3", "stage4"}``，默认 stage3（保持 Stage 3 老调用兼容）。
    返回 ``{"default_prompt": None | {prompt, updated_at},
    "history": [{prompt, updated_at}]}``（history 最新在前，≤ 10 条）。
    """
    full = _read_full_settings(repo_root)
    s = _normalize_stage(stage)
    return dict(full.get(s) or _empty_stage_node())


def save_default_prompt(repo_root: Path | str, prompt: str, *, stage: str = "stage3") -> dict:
    """把当前提示词保存为指定 stage 的全局默认（覆盖旧值）。

    同时把该提示词同步进该 stage 的历史列表（已存在 → 置顶去重；未满
    → 插入），这样历史列表里能看到哪一条是默认（前端按文本相等标 ⭐）。
    列表已满且该提示词不在其中 → **强制覆盖最旧**（confirm=True），
    保证默认提示词一定出现在历史列表中——否则前端 ⭐ 永不显示
    （REQ-20261016-128）。

    注意：旧形态（顶层 default_prompt）调用此函数时 stage 默认为 stage3，
    第一次保存即触发旧形态 → 新形态迁移。
    """
    text = str(prompt or "").strip()
    if not text:
        raise ShortVideoError("提示词不能为空")
    root = Path(repo_root)
    full = _read_full_settings(root)
    s = _normalize_stage(stage)
    full.setdefault(s, _empty_stage_node())
    entry = {"prompt": text, "updated_at": now_iso()}
    full[s][_PROMPT_DEFAULT_KEY] = entry
    _write_json_atomic(prompt_settings_path(root), full)
    # REQ-20261016-128：默认必须能进历史（否则前端 ⭐ 永不显示），
    # 列表满时强制覆盖最旧（confirm=True），不让默认丢失。
    save_prompt_history(root, text, confirm=True, stage=s)
    return entry


def delete_prompt_history(repo_root: Path | str, prompt: str, *, stage: str = "stage3") -> dict:
    """从指定 stage 的历史列表移除指定 prompt。

    若被删项的 prompt 与全局默认相等 → 同步将 default_prompt 置 None
    （"列表里没有默认"——REQ-20261016-128 用户确认语义）。

    Returns
    -------
    ``{"removed_default": bool, "history": [...]}``
        - ``removed_default``: 是否同时清空了默认提示词
        - ``history``: 移除后的最新历史列表（最新在前）
    """
    text = str(prompt or "").strip()
    if not text:
        raise ShortVideoError("提示词不能为空")
    root = Path(repo_root)
    full = _read_full_settings(root)
    s = _normalize_stage(stage)
    full.setdefault(s, _empty_stage_node())
    hist = full[s][_PROMPT_HISTORY_KEY] or []
    new_hist = [it for it in hist if str(it.get("prompt") or "").strip() != text]
    full[s][_PROMPT_HISTORY_KEY] = new_hist
    removed_default = False
    cur_default = full[s].get(_PROMPT_DEFAULT_KEY) or {}
    if str(cur_default.get("prompt") or "").strip() == text:
        full[s][_PROMPT_DEFAULT_KEY] = None
        removed_default = True
    _write_json_atomic(prompt_settings_path(root), full)
    return {"removed_default": removed_default, "history": new_hist}


def save_prompt_history(repo_root: Path | str, prompt: str, *, confirm: bool = False,
                        stage: str = "stage3") -> dict:
    """提示词存入指定 stage 的历史列表（最新在前，上限 10 条）。

    - 已存在相同提示词（strip 后相等）→ 更新时间戳并置顶，直接 saved；
    - 未满 10 条 → 插入最前，saved；
    - 已满 10 条的新提示词 → confirm=False 返回 ``{"status": "need_confirm"}``（前端
      弹窗由用户决定）；confirm=True → 丢弃最旧一条后插入，saved。
    """
    text = str(prompt or "").strip()
    if not text:
        raise ShortVideoError("提示词不能为空")
    root = Path(repo_root)
    full = _read_full_settings(root)
    s = _normalize_stage(stage)
    full.setdefault(s, _empty_stage_node())
    hist = full[s][_PROMPT_HISTORY_KEY]
    # 去重置顶（不算新增，无需确认）
    for i, item in enumerate(hist):
        if str(item.get("prompt") or "").strip() == text:
            hist.pop(i)
            break
    else:
        if len(hist) >= PROMPT_HISTORY_MAX and not confirm:
            return {"status": "need_confirm", "history": hist,
                    "max": PROMPT_HISTORY_MAX}
        if len(hist) >= PROMPT_HISTORY_MAX:
            hist.pop()  # 丢弃最旧（末尾）
    hist.insert(0, {"prompt": text, "updated_at": now_iso()})
    _write_json_atomic(prompt_settings_path(root), full)
    return {"status": "saved", "history": hist, "max": PROMPT_HISTORY_MAX}


def load_all_prompt_settings(repo_root: Path | str) -> dict:
    """读全部 stage 的设置（前端一次性拉取，避免 2 次 roundtrip）。

    返回 ``{"stage3": {default_prompt, history}, "stage4": {default_prompt, history}}``。
    """
    full = _read_full_settings(repo_root)
    return {s: dict(full.get(s) or _empty_stage_node()) for s in _PROMPT_VALID_STAGES}


# ---------- REQ-20261012-099：Stage 3 截图（解析短视频内容的输入图） ----------
# 文件落在 stage3/ 下（统一名 input_image.*）→ 浏览器经 short_video_file?kind=stage3
# 访问，复用既有安全校验。

def save_stage3_image(repo_root: Path | str, task_id: str, project_id: str,
                      src_path: Path | str) -> dict:
    """保存/覆盖 Stage 3 截图（统一名 input_image，保留原扩展名）+ 更新 state。"""
    suffix = Path(src_path).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise ShortVideoError(
            f"不支持的图片格式 {suffix or '(无扩展名)'}，允许: {sorted(ALLOWED_IMAGE_SUFFIXES)}"
        )
    root = Path(repo_root)
    name = STAGE3_IMAGE_BASENAME + suffix
    target = stage3_dir(root, task_id, project_id) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    # 覆盖旧扩展名不同的历史截图，避免残留多张
    for old in stage3_dir(root, task_id, project_id).glob(STAGE3_IMAGE_BASENAME + ".*"):
        if old.name != name:
            try:
                old.unlink()
            except FileNotFoundError:
                pass
    shutil.copyfile(Path(src_path), target)
    entry = {"name": name, "updated_at": now_iso()}
    project = load_project(root, task_id, project_id)
    s3 = get_stage_state(project, "stage3_analyze")
    s3["image"] = entry
    set_stage_state(project, "stage3_analyze", s3)
    save_project(root, project)
    return entry


def remove_stage3_image(repo_root: Path | str, task_id: str, project_id: str) -> bool:
    """移除 Stage 3 截图（删文件 + 清 state 引用）。无图返回 False。"""
    root = Path(repo_root)
    project = load_project(root, task_id, project_id)
    s3 = get_stage_state(project, "stage3_analyze")
    cur = s3.get("image") or {}
    name = str(cur.get("name") or "")
    removed = False
    for old in stage3_dir(root, task_id, project_id).glob(STAGE3_IMAGE_BASENAME + ".*"):
        try:
            old.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError:
            continue
    if name or removed:
        s3.pop("image", None)
        set_stage_state(project, "stage3_analyze", s3)
        save_project(root, project)
        return True
    return False


def stage3_current_image(repo_root: Path | str, task_id: str, project_id: str,
                         project: dict | None = None) -> dict | None:
    """返回 state 中记录且磁盘上确实存在的截图信息；否则 None（防悬空引用）。

    文件名净化：state 被手改出 ``../`` 之类路径时直接视为无图——本函数的
    返回值会拼进 stage3_dir 发给 LLM（base64），必须与 short_video_file
    端点的 ``Path(name).name == name`` 校验同级防护。
    """
    project = project or load_project(repo_root, task_id, project_id)
    cur = (get_stage_state(project, "stage3_analyze") or {}).get("image") or {}
    name = str(cur.get("name") or "")
    if not name or Path(name).name != name:
        return None
    if not (stage3_dir(repo_root, task_id, project_id) / name).is_file():
        return None
    return {"name": name, "updated_at": str(cur.get("updated_at") or "")}


def stage4_paths(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    index: int,
) -> tuple[Path, Path]:
    """Stage 4 单条 highlight 的 (coarse mp4, coarse srt) 路径。"""
    base = stage4_dir(repo_root, task_id, project_id)
    return (
        base / f"{COARSE_MP4_PREFIX}_{index:02d}.mp4",
        base / f"{COARSE_SRT_PREFIX}_{index:02d}.srt",
    )


def stage5_refined_srt_path(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    index: int,
) -> Path:
    return stage5_dir(repo_root, task_id, project_id) / f"{REFINED_SRT_PREFIX}_{index:02d}.srt"


def stage5_default_srt_path(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    index: int,
) -> Path:
    """Stage 5 默认（ASR 原始）字幕；用户未手动修正时使用此文件。"""
    return stage5_dir(repo_root, task_id, project_id) / f"{REFINED_SRT_PREFIX}_{index:02d}_asr.srt"


def stage5_subs_srt_path(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    index: int,
) -> Path:
    """REQ-20261019-227：粗剪成片字幕（ASR + 用户改字后的最终消费文件）。

    下载 / 预览字幕条 / 精剪烧录统一读这一个；由 asr 版 + edits 覆盖层
    重新物化，时间戳逐字保留。
    """
    return stage5_dir(repo_root, task_id, project_id) / f"{SUBS_SRT_PREFIX}_{index:02d}.srt"


def stage5_subs_asr_srt_path(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    index: int,
) -> Path:
    """REQ-20261019-227：粗剪成片 ASR 原始字幕（冻结不改写，原文保留基准）。"""
    return stage5_dir(repo_root, task_id, project_id) / f"{SUBS_SRT_PREFIX}_{index:02d}.asr.srt"


def stage5_subs_base_srt_path(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    index: int,
) -> Path:
    """REQ-20261019-228：断句+标点整理后的基准字幕（冻结；编辑器原文基线）。

    LLM 整理成功 = 整理版；失败/未配 LLM = ASR 原样拷贝（编辑链路无感）。
    """
    return stage5_dir(repo_root, task_id, project_id) / f"{SUBS_SRT_PREFIX}_{index:02d}.base.srt"


def stage5_subs_edits_path(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    index: int,
) -> Path:
    """REQ-20261019-227：改字覆盖层 ``{cue_index: 新文字}``（还原 = 删 key）。"""
    return stage5_dir(repo_root, task_id, project_id) / f"{SUBS_SRT_PREFIX}_{index:02d}.edits.json"


def stage6_paths(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    index: int,
) -> tuple[Path, Path]:
    """Stage 6 单条 highlight 的 (final mp4, continuous srt) 路径。

    ``index`` 从 1 开始（与 Stage 4/5 的命名一致）。
    """
    base = stage6_dir(repo_root, task_id, project_id)
    return (
        base / f"{FINAL_MP4_PREFIX}_{index:02d}.mp4",
        base / f"{FINAL_SRT_PREFIX}_{index:02d}.srt",
    )


def get_stage6_state(project: dict) -> dict:
    """读精剪混编状态字段（缺则视为空）。REQ-20261016-148：显示编号 6，
    key 迁移为 stage6_fine（函数/目录名保留稳定 API）。"""
    pipeline = project.get("pipeline") or {}
    state = pipeline.get("stage6_fine") or {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("status", "pending")
    state.setdefault("highlights", [])
    state.setdefault("logs", [])
    return state


def set_stage6_state(project: dict, state: dict) -> None:
    pipeline = project.setdefault("pipeline", {})
    pipeline["stage6_fine"] = state


# ---------- 通用阶段状态读写 ----------

_STAGE_STATE_KEYS = {
    "stage1_source": "stage1_source",
    "stage2_extract": "stage2_extract",
    "stage3_analyze": "stage3_analyze",
    "stage4_optimize": "stage4_optimize",
    "stage5_coarse": "stage5_coarse",
    # REQ-20261016-148：精剪 7→6；stage7_finalize 保留为旧名别名（兼容）
    "stage6_fine": "stage6_fine",
    "stage7_finalize": "stage6_fine",
}


def get_stage_state(project: dict, stage: str) -> dict:
    """读某阶段状态字段。"""
    key = _STAGE_STATE_KEYS.get(stage)
    if not key:
        raise ValueError(f"未知阶段: {stage}")
    pipeline = project.get("pipeline") or {}
    state = pipeline.get(key) or {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("status", "pending")
    return state


def set_stage_state(project: dict, stage: str, state: dict) -> None:
    key = _STAGE_STATE_KEYS.get(stage)
    if not key:
        raise ValueError(f"未知阶段: {stage}")
    pipeline = project.setdefault("pipeline", {})
    pipeline[key] = state


def invalidate_hl_downstream(project: dict, hl_index: int, *,
                             after_stage: str = "stage4_optimize") -> None:
    """REQ-20261019-221：单条 highlight 的上游改动 → **只作废该条**的下游。

    与 reset_downstream_stages（整阶段打回 pending）相对：内容优化阶段对某条
    短视频的字幕做处理（LLM 优化 / 手动剪词·改字·删行·重排）时，其他条已
    完成的粗剪/精剪不受影响，只有本条后续的粗剪合成与精剪混编需要重跑。

    - ``after_stage="stage4_optimize"``：作废该条的粗剪条目（coarse_mp4/srt
      清空、status=pending、stale_reason 说明）+ 该条的精剪输出条目；
    - ``after_stage="stage5_coarse"``：只作废该条的精剪输出条目
      （单条粗剪重合成用 — 其余条精剪成品保留）。

    精剪 ``params`` / ``clips`` 用户资产按 REQ-20261019-216 口径保留；
    磁盘文件一律不删。阶段整体 status 重算：
    - stage5_coarse：条目全 done → done；部分 done → partial；无 done → pending；
    - stage6_fine：stage5 现有条目都有 final_mp4 → done；有产出 → partial；
      否则 pending（与 run_fine_single 同口径）；
    - 空表 done（无条目可保留）→ 视为无产出，整阶段打回 pending。
    """
    hl_index = int(hl_index)
    pipeline = project.setdefault("pipeline", {})
    invalidate_coarse = after_stage == "stage4_optimize"
    # ---- stage5_coarse：该条条目 → pending + 清产物引用（保留标题/位置） ----
    if invalidate_coarse:
        s5 = pipeline.get("stage5_coarse")
        if isinstance(s5, dict):
            entries = [h for h in (s5.get("highlights") or [])
                       if isinstance(h, dict)]
            hit = next((h for h in entries
                        if int(h.get("index") or 0) == hl_index), None)
            if hit is not None and hit.get("coarse_mp4"):
                hit["coarse_mp4"] = ""
                hit["coarse_srt"] = ""
                hit["status"] = "pending"
                hit["stale_reason"] = "字幕已修改，待重新合成"
                hit.pop("error", None)
                hit["duration_sec"] = 0
                hit["elapsed_sec"] = 0
                s5["highlights"] = entries
                done_n = sum(1 for h in entries if h.get("status") == "done")
                s5["status"] = ("done" if done_n == len(entries) and entries
                                else ("partial" if done_n else "pending"))
                s5["total_duration_sec"] = round(sum(
                    float(h.get("duration_sec") or 0)
                    for h in entries if h.get("status") == "done"), 3)
            elif not entries and s5.get("status") == "done":
                # 空表 done（无任何条目可保留）→ 无产出可保，整阶段打回
                s5["status"] = "pending"
    # ---- stage6_fine：移除该条输出条目（params/clips 保留） ----
    s6 = pipeline.get("stage6_fine")
    if isinstance(s6, dict):
        before = s6.get("highlights") or []
        kept = [h for h in before
                if not (isinstance(h, dict)
                        and int(h.get("index") or 0) == hl_index)]
        if len(kept) != len(before):
            s6["highlights"] = kept
            s5 = pipeline.get("stage5_coarse") or {}
            want = {int(h.get("index") or 0)
                    for h in (s5.get("highlights") or [])
                    if isinstance(h, dict)}
            have = {int(h.get("index") or 0) for h in kept if h.get("final_mp4")}
            s6["status"] = ("done" if want and want <= have
                            else ("partial" if have else "pending"))
        elif not before and s6.get("status") == "done":
            # 空表 done → 无产出可保，整阶段打回
            s6["status"] = "pending"


def reset_downstream_stages(project: dict, after_stage: str) -> None:
    """把指定阶段之后的所有阶段重置为 pending（用于上游阶段重跑时清空下游）。

    ``after_stage`` 应是 "stage1_source" / "stage2_extract" / ... / "stage5_coarse"。
    "stage6_fine"（精剪，REQ-20261016-148 重编号后为末位）之后没有下游。

    REQ-20261019-216：重置 stage6_fine 时**保留用户资产**——
    ``params``（参数组 + 公共素材 mat_cover/bg/audio/avatar + 检测结果）与
    ``clips``（per-clip 上传/覆盖）是用户上传与调校出来的，上游重跑不应销毁；
    只清运行态（status / highlights 输出 / logs）。磁盘文件本就不删。
    """
    order = [
        "stage1_source",
        "stage2_extract",
        "stage3_analyze",
        "stage4_optimize",
        "stage5_coarse",
        "stage6_fine",
    ]
    if after_stage not in order:
        return
    idx = order.index(after_stage)
    pipeline = project.setdefault("pipeline", {})
    for stage_key in order[idx + 1:]:
        if stage_key in pipeline:
            fresh: dict = {"status": "pending"}
            if stage_key == "stage6_fine":
                old = pipeline.get("stage6_fine")
                if isinstance(old, dict):
                    for keep_key in ("params", "clips"):
                        old_val = old.get(keep_key)
                        if isinstance(old_val, dict) and old_val:
                            fresh[keep_key] = old_val
            pipeline[stage_key] = fresh


def reorder_subtitle_line(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    hl_index: int,
    src_index: int,
    before_src_index: int | None,
) -> dict:
    """REQ-20261004-stage3-restructure：拖拽重排某条字幕行。

    - 读 ``stage3/highlights.json``，找到 ``index == hl_index`` 的 highlight
    - 把 ``src_index == src_index`` 的 subtitle_line 从原位置移除
    - 把这条插入到 ``src_index == before_src_index`` 这条**之前**（None = 末尾）
    - 写回 highlights.json + 同步更新 pipeline.stage3_analyze.highlights 镜像
    - 触发 ``reset_downstream_stages("stage3_analyze")``（下游 Stage 4/5/6 作废）

    返回更新后的 highlight dict（含新 subtitle_lines 顺序）。
    """
    root = Path(repo_root)
    hp = stage_highlights_path(root, task_id, project_id)
    if not hp.is_file():
        raise ShortVideoError("highlights.json 不存在，请先跑 Stage 3")
    try:
        doc = json.loads(hp.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ShortVideoError(f"highlights.json 解析失败: {e}") from e
    hls = doc.get("highlights") or []
    target = None
    target_pos = -1
    for i, h in enumerate(hls):
        if int(h.get("index") or 0) == int(hl_index):
            target = h
            target_pos = i
            break
    if target is None:
        raise ShortVideoError(f"highlight #{hl_index} 不存在")

    lines = target.get("subtitle_lines") or []
    moving = None
    moving_pos = -1
    for i, sl in enumerate(lines):
        try:
            if int(sl.get("src_index") or 0) == int(src_index):
                moving = sl
                moving_pos = i
                break
        except (TypeError, ValueError):
            continue
    if moving is None:
        raise ShortVideoError(f"highlight #{hl_index} 没有 src_index={src_index} 的字幕行")
    # 移除
    lines.pop(moving_pos)
    # 找 before 位置（移除后的位置）
    insert_at = len(lines)
    if before_src_index is not None:
        for i, sl in enumerate(lines):
            try:
                if int(sl.get("src_index") or 0) == int(before_src_index):
                    insert_at = i
                    break
            except (TypeError, ValueError):
                continue
    lines.insert(insert_at, moving)
    target["subtitle_lines"] = lines
    hls[target_pos] = target

    # 写 highlights.json（保留其他字段）
    doc["highlights"] = hls
    _write_json_atomic(hp, doc)

    # 同步 project state
    project = load_project(root, task_id, project_id)
    pl = project.setdefault("pipeline", {})
    s3 = pl.setdefault("stage3_analyze", {})
    state_hls = s3.get("highlights") or []
    for sh in state_hls:
        if int(sh.get("index") or 0) == int(hl_index):
            sh["subtitle_lines"] = list(lines)
            break
    else:
        state_hls.append({
            "index": int(hl_index),
            "title": target.get("title"),
            "start_ms": target.get("start_ms"),
            "end_ms": target.get("end_ms"),
            "subtitle_lines": list(lines),
        })
        s3["highlights"] = state_hls
    s3["warnings"] = list(s3.get("warnings") or []) + [
        f"[{now_iso()}] subtitle_lines 重排：hl #{hl_index} src_index={src_index} → before src_index={before_src_index}",
    ]
    # 字幕顺序变了 → 下游全部作废
    reset_downstream_stages(project, "stage3_analyze")
    save_project(root, project)
    return target


def find_long_video_state_root(task_dir: Path) -> list[Path]:
    """返回 ``task_dir`` 下所有「长视频 cut_by_srt.py 可能写入」的位置。

    Stage 6 必须验证：本次调用的产物**没有**写到这些路径之下。
    仅用于运行时校验（产物隔离测试），不参与业务逻辑。
    """
    roots: list[Path] = []
    outputs = task_dir / "outputs"
    if outputs.exists():
        for child in outputs.iterdir():
            if child.is_dir():
                roots.append(child)
            elif child.is_file() and child.suffix.lower() in {".mp4", ".srt", ".txt"}:
                roots.append(child)
    return roots
