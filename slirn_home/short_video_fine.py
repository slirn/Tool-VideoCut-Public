"""REQ-20261013-105：短视频 Stage 7 精剪 v1 完整版（按长视频 fine 整套搬）。

包含 4 块：
1. ``run_fine_one / run_fine_batch``：单条 + 批量精剪（基于 short_video_finalize.run_stage6_one
   套 6 素材卡 + 字体 + BGM）
2. ``_JOBS`` 异步 job 表（仿 compose_service.start_compose 模式）
3. ``params`` 6 组子字段（materials / layout / font / output / audio / preview）落
   ``project.json → pipeline.stage6_fine.params``
4. ``short_video_fine_profiles`` 子模块：模板管理（6 端点）
5. ``short_video_bg_detect`` 子模块：bg 白底检测（端点 + 落 detected_region）
6. ``short_video_ai_layout`` 子模块：AI 智能布局（端点 + 落 layout.{video,subtitle,cover,bg}）

短/长隔离：execution_history 写短视频 stage6_dir；模板落
``tasks/_global_short_video_fine_profiles.json``，绝不写长视频
``tasks/_global_fine_profiles.json``。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from slirn_home import short_video_compose as _sv_compose
from slirn_home import short_video_service as svc

log = logging.getLogger(__name__)


# ---------- 异步 job 表（仿 compose_service.start_compose） ----------

_JOBS: dict[tuple[str, str], dict] = {}
_JOBS_LOCK = threading.Lock()
# 跨项目串行锁（仿 app.py _FINE_RENDER_GATE；防止 ffmpeg 撞）
_RENDER_GATE = threading.Lock()


def _job_key(task_id: str, project_id: str) -> tuple[str, str]:
    return (str(task_id or ""), str(project_id or ""))


def job_status(task_id: str, project_id: str) -> dict | None:
    with _JOBS_LOCK:
        j = _JOBS.get(_job_key(task_id, project_id))
        return dict(j) if j else None


def _set_job(task_id: str, project_id: str, **fields) -> None:
    with _JOBS_LOCK:
        key = _job_key(task_id, project_id)
        cur = _JOBS.get(key) or {"state": "idle", "progress": 0.0,
                                  "stage": "", "error": None,
                                  "started_at": 0.0, "finished_at": None,
                                  "count": 0, "total_count": 0,
                                  "result": None}
        cur.update(fields)
        _JOBS[key] = cur


def start_fine_async(repo_root: Path | str, task_id: str, project_id: str,
                     index: int | None = None) -> bool:
    """启动异步精剪线程。已在跑 → False。

    index=None 全量；传 index 只跑该条（REQ-20261017-157 单条导出，
    其余条 final_mp4 保留）。
    """
    key = _job_key(task_id, project_id)
    with _JOBS_LOCK:
        existing = _JOBS.get(key)
        if existing and existing.get("state") == "running":
            return False
        _JOBS[key] = {
            "state": "running", "progress": 0.0,
            "stage": f"准备（单条 #{index}）" if index else "准备",
            "error": None, "started_at": time.time(), "finished_at": None,
            "count": 0, "total_count": 1 if index else 0, "result": None,
        }

    def _run() -> None:
        try:
            # 跨项目串行（仿 _FINE_RENDER_GATE；短视频单 ffmpeg 一次）
            with _RENDER_GATE:
                from slirn_home import execution_history
                # REQ-20261022-144：落任务级 outputs（与粗剪一致，list_logs 可查）
                stage6_dir = svc.sv_logs_dir(repo_root, task_id)
                eid = execution_history.record_start(
                    stage6_dir,
                    execution_history.KIND_SHORT_VIDEO_FINE,
                )
                try:
                    if index:
                        res = run_fine_single(
                            repo_root, task_id, project_id, int(index),
                            progress_cb=lambda ev, p: _set_job(
                                task_id, project_id,
                                stage=p.get("stage") or "合成中",
                                progress=p.get("progress", 0.0),
                                count=p.get("count", 0),
                                total_count=p.get("total", 0),
                            ),
                        )
                    else:
                        res = run_fine_batch(
                            repo_root, task_id, project_id,
                            progress_cb=lambda ev, p: _set_job(
                                task_id, project_id,
                                stage=p.get("stage") or "合成中",
                                progress=p.get("progress", 0.0),
                                count=p.get("count", 0),
                                total_count=p.get("total", 0),
                            ),
                        )
                    _set_job(task_id, project_id,
                             state="done", progress=100.0, stage="完成",
                             finished_at=time.time(), result=res)
                    execution_history.record_finish(stage6_dir, eid, success=True)
                except Exception as e:  # noqa: BLE001
                    _set_job(task_id, project_id,
                             state="failed", stage="失败", error=str(e)[:200],
                             finished_at=time.time())
                    execution_history.record_finish(
                        stage6_dir, eid, success=False, error=str(e)[:200],
                    )
        except Exception as e:  # noqa: BLE001 — 线程兜底
            log.exception("[fine] 异步线程异常")
            _set_job(task_id, project_id,
                     state="failed", error=str(e)[:200], finished_at=time.time())

    t = threading.Thread(target=_run, daemon=True, name=f"sv-fine-{task_id}-{project_id}")
    t.start()
    return True


# ---------- 6 组 params 子字段（落 project.json → stage6_fine.params） ----------

DEFAULT_PARAMS: dict[str, Any] = {
    # REQ-20261017-157：video/subtitle 移出公共素材 —— 它们是每条短视频自己的
    # 素材（自动继承 Stage 5 / 手动上传覆盖，落 stage6_fine.clips）。
    "materials": {
        "cover": "",        # 封面图（可选）
        "bg": "",           # 背景图（可选，AI 布局输入）
        "bg_desc": "",      # 背景图说明（REQ-20261017-163，喂 AI 布局）
        "audio": "",        # 附加音频轨（可选）
        "avatar": "",       # 导师头像（可选，REQ-20261017-164）
    },
    "layout": {
        # REQ-20261018-174：视频源裁剪（对齐长视频口径）—— crop_* 描述
        # 「从源画面里截的矩形」，以源画面 1920×1080 设计空间为准（源是
        # 横屏课程视频；渲染时按源视频实际尺寸等比换算）；与画布 1080×1920
        # 的 video 区域正交。crop_aspect_lock = 宽高输入按 16:9 联动。
        # REQ-20261018-198：crop_region_lock = 宽高按视频区域 W/H 比例联动
        # （基准随区域手改实时变；与 16:9 锁互斥，两键同真按区域锁为准）。
        # REQ-20261018-184：每个区域带 enabled 开关（UI 勾选框）——
        # 未勾选 = 不使用该区域的内容（bg/avatar/mentor_text/subtitle
        # 不渲染；cover/title 同口径；video 恒用，它是内容本体）
        "video": {"x": 0, "y": 0, "w": 1080, "h": 1920, "enabled": True,
                  "crop_x": 0, "crop_y": 0, "crop_w": 1920, "crop_h": 1080,
                  "crop_aspect_lock": True, "crop_region_lock": False},
        "subtitle": {"x": 60, "y": 1620, "w": 960, "h": 240, "enabled": True},
        "cover": {"x": 0, "y": 0, "w": 1080, "h": 1080, "enabled": True},
        "bg": {"x": 0, "y": 0, "w": 1080, "h": 1920, "enabled": True},
        # REQ-20261018-173：导师文字 / 导师头像区域（「✅ 填充到参数区域」
        # 按识别类型自动填；头像形状 circle 圆形 / rect 矩形）
        "mentor_text": {"x": 60, "y": 100, "w": 740, "h": 240, "enabled": True},
        "avatar": {"x": 850, "y": 80, "w": 220, "h": 220, "enabled": True},
        "avatar_shape": "circle",
        # REQ-20261018-177：标题区域（视频标题文字的显示盒，渲染时文字
        # 按区域大小自动折行、按区域位置放置；enabled=False 不渲染）
        "title": {"x": 90, "y": 380, "w": 900, "h": 340, "enabled": True},
        # REQ-20261018-185：封面标题区域（封面片头段烧录标题的显示盒，
        # 时间轴 0..封面时长；enabled=False 封面不叠标题）
        "cover_title": {"x": 90, "y": 380, "w": 900, "h": 340, "enabled": True},
        # REQ-20261018-189：video_fit（适配方式）键移除 —— 视频区域固定
        # 语义「截取的视频自动适应区域宽高」，老项目的残留键保存时清出
    },
    # REQ-20261017-164：导师文字（昵称 / 标题）—— 字体及大小 + 文字本体
    # REQ-20261018-182：颜色 + 描边可选（颜色/宽度；enabled=False 不描边，
    # 默认值 = 渲染器此前的硬编码口径，老项目观感不变）
    "mentor": {
        "nickname": "",
        "nickname_font": "STHeitiMedium.ttc",
        "nickname_size": 42,
        "nickname_color": "#FFFFFF",
        "nickname_outline_enabled": True,
        "nickname_outline_color": "#000000",
        "nickname_outline_width": 2,
        "title": "",
        "title_font": "STHeitiMedium.ttc",
        "title_size": 36,
        "title_color": "#FFE9B8",
        "title_outline_enabled": True,
        "title_outline_color": "#000000",
        "title_outline_width": 2,
        # REQ-20261018-195：导师头衔行间距（px；头衔可多行折行，昵称单行无此参数）
        "title_line_spacing": 0,
    },
    # REQ-20261018-177：视频标题样式（字体/颜色/大小/阴影）。
    # REQ-20261018-179（修订二）：**无 text 键** —— 标题文字只来自精剪
    # 混编区该条卡片顶部的标题（每条各自），参数区不统一设置文字
    "title": {
        "family": "STHeitiMedium.ttc",
        "size": 64,
        "bold": True,
        "align": "center",           # 区域内水平对齐 left / center / right
        "color": "#FFFFFF",
        "shadow_color": "#000000",
        "shadow_opacity": 0.6,       # 0-1（0=全透明无阴影）
        "shadow_depth": 4,          # 阴影偏移（px，设计空间）
        # REQ-20261018-195：行间距（px，折行行与行之间；0=默认行距）
        "line_spacing": 0,
    },
    # REQ-20261018-185：封面标题样式（字体/颜色/大小/阴影）—— 与视频标题
    # 样式独立；**无 text 键**，文字同源该条卡片标题（封面段单独烧录）
    "cover_title": {
        "family": "STHeitiMedium.ttc",
        "size": 64,
        "bold": True,
        "align": "center",
        "color": "#FFFFFF",
        "shadow_color": "#000000",
        "shadow_opacity": 0.6,
        "shadow_depth": 4,
        # REQ-20261018-195：行间距（px；语义同视频标题）
        "line_spacing": 0,
    },
    "font": {
        "family": "STHeitiMedium.ttc",
        "size": 48,
        "bold": False,
        "align": "center",
        "offset_ms": 0,
        "stroke_width": 2,
        "color": "#FFFFFF",
        "stroke_color": "#000000",
        "bg_enabled": False,
        "bg_color": "#000000",
        "bg_opacity": 0.5,
        "bg_radius": 8,
    },
    "output": {
        # REQ-20261017-164：分辨率扩 960x1280（3:4 竖屏）与 bg_res
        # （按背景图片分辨率输出，成片渲染时读背景图尺寸，无背景图退回 720p）
        "resolution": "720p",  # 720p/1080p/960x1280/1086x1448（3:4 竖屏）/1280x720/1920x1080（16:9 横屏）/source/bg_res
        "fps": 30,
        "codec": "libx264",
    },
    "audio": {
        "enabled": False,
        "bgm_path": "",        # 系统默认或上传的 BGM 绝对路径
        "volume_db": -6,
        "fade_in_ms": 500,
        "fade_out_ms": 1000,
    },
    "preview": {
        "start_ms": 0,
        "duration_sec": 5,     # 2-30s
    },
    "detected_region": None,  # bg 检测结果（dict：{x, y, w, h}）
    # REQ-20261018-170：AI 识别布局结果（regions/layout/prompt/source/detected_at）
    "ai_layout_result": None,
}


def _get_params(project: dict) -> dict:
    """读 stage6_fine.params；缺省补齐 6 组子字段。"""
    s = svc.get_stage_state(project, "stage6_fine")
    p = s.get("params")
    if not isinstance(p, dict):
        p = {}
    # 浅合并各组（保用户已有值；缺字段用默认）
    for group_key, group_default in DEFAULT_PARAMS.items():
        if group_key not in p or not isinstance(p[group_key], dict):
            p[group_key] = dict(group_default) if isinstance(group_default, dict) else group_default
    # REQ-20261018-173：layout 组已有但缺新子键的老项目也要补默认
    # （mentor_text / avatar / avatar_shape），否则 UI 与填充逻辑读到 None
    for k, v in DEFAULT_PARAMS["layout"].items():
        if k not in p["layout"]:
            p["layout"][k] = dict(v) if isinstance(v, dict) else v
    # REQ-20261018-174：video 组内的 crop_* 子键同理（嵌套一层，上面只补顶层的）
    vid = p["layout"].get("video")
    if isinstance(vid, dict):
        for k, v in DEFAULT_PARAMS["layout"]["video"].items():
            if k not in vid:
                vid[k] = v
    return p


# REQ-20261017-164：分辨率白名单（select 之外来的值归一回默认）
# REQ-20261018-187：+ 16:9 横屏两档（1280x720 / 1920x1080）。注意 720p/
# 1080p 的实际输出一直是**竖屏**（720×1280 / 1080×1920），旧 UI 标签把
# 宽高写反了；本次按真实方向标注（9:16 族），16:9 族是新增值（渲染时
# 等比缩放 + 黑边补齐，不变形）
_RESOLUTIONS = {"720p", "1080p", "960x1280", "1086x1448", "1280x720",
                "1920x1080", "source", "bg_res"}


def _probe_video_size(abs_path) -> tuple[int, int] | None:
    """ffprobe 读视频宽高（best-effort：失败/无 ffprobe 返回 None）。"""
    p = Path(abs_path) if abs_path else Path()
    if not p.is_file() or not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(p)],
            capture_output=True, text=True, timeout=10,
        )
        parts = (r.stdout or "").strip().split(",")[0].split("x")
        w, h = int(parts[0]), int(parts[1])
        return (w, h) if w > 0 and h > 0 else None
    except Exception as e:  # noqa: BLE001 — 探测失败不阻断保存
        log.info("[video-fit] ffprobe 探测失败（%s）：%s", p.name, e)
        return None


def save_params(repo_root: Path | str, task_id: str, project_id: str,
                params: dict) -> dict:
    """保存 6 组子字段到 project.json（验证后浅合并）。"""
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    current = _get_params(project)
    # 浅合并每组
    for group_key, group_val in (params or {}).items():
        if group_key not in DEFAULT_PARAMS:
            continue
        if isinstance(group_val, dict) and isinstance(current.get(group_key), dict):
            current[group_key].update(group_val)
        else:
            current[group_key] = group_val
    # REQ-20261017-157：老项目的 video/subtitle 槽随保存清出公共素材
    # REQ-20261017-163：reference/reference_desc 同理（参考图槽移除）
    for legacy in ("video", "subtitle", "reference", "reference_desc"):
        (current.get("materials") or {}).pop(legacy, None)
    # REQ-20261018-179（修订二）：标题文字只在精剪卡片设置，随保存清出参数区
    # REQ-20261018-185：封面标题文字同口径（渲染时注入，不落盘）
    (current.get("title") or {}).pop("text", None)
    (current.get("cover_title") or {}).pop("text", None)
    # REQ-20261018-189：适配方式（video_fit）已移除，老项目残留键随保存清出
    (current.get("layout") or {}).pop("video_fit", None)
    # REQ-20261017-164：分辨率白名单归一
    out = current.get("output") or {}
    if str(out.get("resolution") or "") not in _RESOLUTIONS:
        out["resolution"] = "720p"
    # REQ-20261018-174：crop_* 钳到源画面设计空间（长视频同口径），
    # crop_aspect_lock 强制 bool（前端可能传 true/"on"/1）
    vid = (current.get("layout") or {}).get("video") or {}
    for ck, mx in (("crop_x", 1920), ("crop_y", 1080),
                   ("crop_w", 1920), ("crop_h", 1080)):
        if ck in vid:
            try:
                vid[ck] = max(0, min(mx, int(vid[ck] or 0)))
            except (TypeError, ValueError):
                vid[ck] = 0
    if "crop_aspect_lock" in vid:
        vid["crop_aspect_lock"] = bool(vid["crop_aspect_lock"])
    # REQ-20261018-198：区域比锁同口径规整 bool；两锁同真时按区域锁
    # 为准（导入 JSON 等旁路可能绕过前端互斥）
    if "crop_region_lock" in vid:
        vid["crop_region_lock"] = bool(vid["crop_region_lock"])
    if vid.get("crop_region_lock") and vid.get("crop_aspect_lock"):
        vid["crop_aspect_lock"] = False
    s = svc.get_stage_state(project, "stage6_fine")
    s["params"] = current
    svc.set_stage_state(project, "stage6_fine", s)
    svc.save_project(root, project)
    return {"params": current}


def load_default_params() -> dict:
    """返回默认 params（前端首次加载用）。"""
    return json.loads(json.dumps(DEFAULT_PARAMS))  # 深拷贝


# ---------- 参数导出 / 导入（REQ-20261018-167，对齐长视频 export/import_fine_params） ----------

# 导出/导入覆盖的组（materials 特殊：只带 bg_desc 文本，不带素材文件路径 ——
# 素材是项目本地上传的文件，跨项目/跨机器复用无意义，沿长视频 REQ-073 口径）
# REQ-20261018-170：ai_layout_result 与 detected_region 同为可迁移的检测结果
# REQ-20261018-177：title（视频标题文字+样式）随组导出导入
# REQ-20261018-185：cover_title（封面标题样式）同组口径
_PARAMS_IO_GROUPS = ("layout", "mentor", "title", "cover_title", "font",
                     "output", "audio",
                     "detected_region", "ai_layout_result")
_PARAMS_IO_SCHEMA = 1
_PARAMS_IO_MAX_BYTES = 64 * 1024


def export_params_json(repo_root: Path | str, task_id: str,
                       project_id: str) -> dict:
    """导出当前精剪参数为 JSON 字符串（返回 {filename, content, mime}）。

    含全部设置组（layout/mentor/font/output/audio/preview/detected_region）
    + materials.bg_desc（文本说明）；不含素材文件路径（导入端也不接受）。
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    params = _get_params(project)
    mats = params.get("materials") or {}
    payload: dict[str, Any] = {
        "_schema": _PARAMS_IO_SCHEMA,
        "_exported_at": datetime.now().isoformat(timespec="seconds"),
        "_source_task_id": task_id,
        "_source_project_id": project_id,
        # 只导文本说明；素材路径不进文件（见模块注释）
        "materials": {"bg_desc": str(mats.get("bg_desc") or "")},
        "preview": params.get("preview"),
    }
    for key in _PARAMS_IO_GROUPS:
        payload[key] = params.get(key)
    filename = (f"sv_fine_params_{project_id}_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"filename": filename, "content": content,
            "mime": "application/json"}


def import_params_json(repo_root: Path | str, task_id: str, project_id: str,
                       raw: str) -> dict:
    """从 JSON 字符串导入参数（覆盖当前组；不动素材文件路径）。

    长视频同口径：整组替换 layout/mentor/font/output/audio/detected_region；
    preview 按字段钳制合并；materials 只接受 bg_desc 文本。
    导入后走保存同款归一（分辨率白名单；REQ-189 起 video_fit 死键丢弃）。
    返回 {params, applied_fields}。
    """
    if not isinstance(raw, str):
        raise svc.ShortVideoError("导入内容必须为 JSON 字符串")
    if len(raw) > _PARAMS_IO_MAX_BYTES:
        raise svc.ShortVideoError("文件过大（>64KB），拒绝导入")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise svc.ShortVideoError(f"JSON 解析失败: {e}")
    if not isinstance(payload, dict):
        raise svc.ShortVideoError("JSON 顶层必须是对象")
    schema = payload.get("_schema")
    if schema != _PARAMS_IO_SCHEMA:
        # REQ-20261028-NNN：schema 2/3/4 且无 _source_project_id → 长视频
        # 「精剪合成」工作台导出的 fine_params_*.json。两套参数体系刻意保持
        # 独立（横/竖版画布坐标、参数组集都不同，不能互导），报错指出文件
        # 来源与去处，而不是甩一个版本号让用户猜。
        if schema in (2, 3, 4) and "_source_project_id" not in payload:
            raise svc.ShortVideoError(
                "这是长视频「精剪合成」导出的参数文件（fine_params_*.json），"
                "长视频与短视频的参数相互独立、不能混用；"
                "请在精剪合成工作台的「导入参数」使用")
        raise svc.ShortVideoError(
            f"参数文件 _schema 不兼容（当前仅支持 {_PARAMS_IO_SCHEMA}，文件为 {schema}）")
    for key in (*_PARAMS_IO_GROUPS, "preview"):
        v = payload.get(key)
        if v is not None and not isinstance(v, dict):
            raise svc.ShortVideoError(f"{key} 必须是 dict 或 null")

    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    current = _get_params(project)
    applied: list[str] = []
    for key in _PARAMS_IO_GROUPS:
        if key in payload:
            current[key] = payload[key]
            applied.append(key)
    # preview：字段级钳制（duration_sec 2-30；start_ms ≥ 0），未知字段丢弃，
    # 未提供的字段保留原值（与长视频 preview 导入口径一致）
    praw = payload.get("preview")
    if isinstance(praw, dict):
        clamped: dict = {}
        for pk, pv in praw.items():
            if pk not in ("start_ms", "duration_sec"):
                continue
            try:
                n = int(pv)
            except (TypeError, ValueError):
                continue
            n = max(0, n) if pk == "start_ms" else max(2, min(30, n))
            clamped[pk] = n
        if clamped:
            base = current.get("preview") or {}
            current["preview"] = {**base, **clamped}
            applied.append("preview")
    # materials：只接受 bg_desc 文本（素材文件路径保持当前项目的）
    minput = payload.get("materials")
    if isinstance(minput, dict) and "bg_desc" in minput:
        mats = current.get("materials") or {}
        mats["bg_desc"] = str(minput.get("bg_desc") or "")
        current["materials"] = mats
        applied.append("materials.bg_desc")
    if not applied:
        raise svc.ShortVideoError("文件里没有任何可应用的参数组")
    # 保存同款归一：分辨率白名单 + 旧导出里的 video_fit 死键清出（见 save_params 尾部）
    out = current.get("output") or {}
    if str(out.get("resolution") or "") not in _RESOLUTIONS:
        out["resolution"] = "720p"
    (current.get("layout") or {}).pop("video_fit", None)
    s = svc.get_stage_state(project, "stage6_fine")
    s["params"] = current
    svc.set_stage_state(project, "stage6_fine", s)
    svc.save_project(root, project)
    return {"params": current, "applied_fields": applied}


# ---------- 单条 clip 素材覆盖（REQ-20261017-157） ----------
#
# 每条短视频的 标题/视频/字幕 自动继承 Stage 5（coarse mp4 + 字幕回退链 +
# highlight 标题）；用户可在 Stage 6 处理区手动覆盖（改标题 / 上传视频 /
# 上传字幕），覆盖值落 ``stage6_fine.clips["<index>"]``，空值 = 回退自动继承。

_UNSET = object()

_UPLOAD_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
_UPLOAD_SUBTITLE_EXTS = {".srt"}
_UPLOAD_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_UPLOAD_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def get_clip_override(project: dict, index: int) -> dict:
    """读单条 clip 的覆盖值（title/video/subtitle）；无覆盖返回 {}。"""
    s = svc.get_stage_state(project, "stage6_fine")
    clips = s.get("clips")
    if not isinstance(clips, dict):
        return {}
    entry = clips.get(str(int(index)))
    return dict(entry) if isinstance(entry, dict) else {}


def save_clip(repo_root: Path | str, task_id: str, project_id: str, index: int,
              *, title: object = _UNSET, video: object = _UNSET,
              subtitle: object = _UNSET, bg: object = _UNSET,
              audio: object = _UNSET) -> dict:
    """写单条 clip 覆盖字段；传空串 = 清除该字段（回退自动继承）。

    REQ-20261017-161：bg/audio 也是 per-clip 覆盖字段 —— 设置了则该条
    不用公共素材的背景/音频，用自己上传的。
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    s = svc.get_stage_state(project, "stage6_fine")
    clips = s.get("clips") if isinstance(s.get("clips"), dict) else {}
    key = str(int(index))
    entry = dict(clips.get(key) or {})
    for field, value in (("title", title), ("video", video),
                         ("subtitle", subtitle), ("bg", bg), ("audio", audio)):
        if value is _UNSET:
            continue
        if value:
            entry[field] = str(value)
        else:
            entry.pop(field, None)
    if entry:
        clips[key] = entry
    else:
        clips.pop(key, None)
    s["clips"] = clips
    svc.set_stage_state(project, "stage6_fine", s)
    svc.save_project(root, project)
    return {"index": int(index), "clip": entry}


_UPLOAD_CLIP_SLOTS = {
    # slot: (中文名, 后缀白名单, 落盘文件模板)
    "video": ("视频", _UPLOAD_VIDEO_EXTS, "clip{idx:02d}_upload{ext}"),
    "subtitle": ("字幕（仅 .srt）", _UPLOAD_SUBTITLE_EXTS,
                 "clip{idx:02d}_upload{ext}"),
    # REQ-20261017-161：per-clip 背景/音频（设置后该条不用公共素材）
    "bg": ("图片", _UPLOAD_IMAGE_EXTS, "clip{idx:02d}_bg{ext}"),
    "audio": ("音频", _UPLOAD_AUDIO_EXTS, "clip{idx:02d}_audio{ext}"),
}


def save_clip_upload(repo_root: Path | str, task_id: str, project_id: str,
                     index: int, slot: str, src_path: Path) -> dict:
    """上传文件落 ``stage6/`` 并写入该 clip 覆盖。

    slot: video / subtitle / bg / audio（REQ-20261017-161 扩 bg、audio）。
    字幕只收 .srt（cut_by_srt 只吃 srt）。
    """
    if slot not in _UPLOAD_CLIP_SLOTS:
        raise svc.ShortVideoError(f"未知素材槽: {slot}")
    kind, allowed, tmpl = _UPLOAD_CLIP_SLOTS[slot]
    root = Path(repo_root)
    src = Path(src_path)
    suffix = src.suffix.lower()
    if suffix not in allowed:
        raise svc.ShortVideoError(
            f"不支持的文件类型 {suffix or '(无后缀)'}，{kind}"
            f"仅支持 {'/'.join(sorted(allowed))}")
    dest = svc.stage6_dir(root, task_id, project_id) / \
        tmpl.format(idx=int(index), ext=suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    rel = svc._rel(root, dest)
    save_clip(root, task_id, project_id, index, **{slot: rel})
    return {"index": int(index), "slot": slot, "path": rel, "name": dest.name}


_UPLOAD_MAT_SLOTS = {
    "cover": ("图片", _UPLOAD_IMAGE_EXTS),
    "bg": ("图片", _UPLOAD_IMAGE_EXTS),
    "audio": ("音频", _UPLOAD_AUDIO_EXTS),
    # REQ-20261017-164：导师头像（公共素材，全部短视频共用）
    "avatar": ("图片", _UPLOAD_IMAGE_EXTS),
}


def save_material_upload(repo_root: Path | str, task_id: str, project_id: str,
                         slot: str, src_path: Path) -> dict:
    """REQ-20261017-159：公共素材上传落 ``stage6/mat_<slot>.<ext>``。

    上传后写 ``params.materials[slot]``（相对路径），全部 clip 共用；
    同槽再传覆盖旧值。slot: cover / bg / audio / avatar
    （REQ-20261017-163：reference 槽已移除；REQ-20261017-164：avatar 加入）。
    """
    if slot not in _UPLOAD_MAT_SLOTS:
        raise svc.ShortVideoError(f"未知素材槽: {slot}")
    kind, allowed = _UPLOAD_MAT_SLOTS[slot]
    root = Path(repo_root)
    src = Path(src_path)
    suffix = src.suffix.lower()
    if suffix not in allowed:
        raise svc.ShortVideoError(
            f"不支持的文件类型 {suffix or '(无后缀)'}，"
            f"{kind}仅支持 {'/'.join(sorted(allowed))}")
    dest = svc.stage6_dir(root, task_id, project_id) / f"mat_{slot}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    rel = svc._rel(root, dest)
    save_params(root, task_id, project_id, {"materials": {slot: rel}})
    caches_cleared = False
    if slot == "bg":
        # REQ-20261019-211：换背景图 → 区域检测/AI 分析缓存全部过期
        # （旧图的槽位会经「填充到参数区域」回写 layout），直接清掉
        from slirn_home import short_video_bg_detect as _bg_detect
        caches_cleared = _bg_detect.clear_region_caches(
            root, task_id, project_id)
    return {"slot": slot, "path": rel, "name": dest.name,
            "caches_cleared": caches_cleared}


def resolved_clip_materials(root: Path, project: dict, index: int) -> dict:
    """单条 clip 的 背景/音频 有效素材解析（REQ-20261017-161）。

    优先级：per-clip 覆盖（clips["<index>"].bg/audio，本条独立上传）
    > 公共素材（params.materials.bg/audio）。返回
    {bg_rel, bg_abs, bg_manual, audio_rel, audio_abs, audio_manual}；
    manual=True 表示该条用自己上传的（不用公共素材）。
    """
    override = get_clip_override(project, int(index))
    params = _get_params(project)
    mats = params.get("materials") or {}
    out: dict[str, Any] = {}
    for slot in ("bg", "audio"):
        rel = str(override.get(slot) or "")
        if rel:
            out[f"{slot}_rel"] = rel
            out[f"{slot}_abs"] = svc._abs(root, rel)
            out[f"{slot}_manual"] = True
        else:
            rel = str(mats.get(slot) or "")
            out[f"{slot}_rel"] = rel
            out[f"{slot}_abs"] = (svc._abs(root, rel) if rel else Path())
            out[f"{slot}_manual"] = False
    return out


def resolved_clip_sources(root: Path, project: dict, index: int) -> dict:
    """单条 clip 的标题/视频/字幕解析（手动覆盖优先，否则自动继承 Stage 5）。

    返回 {title, video_rel, video_abs, video_manual,
          subtitle_rel, subtitle_abs, subtitle_manual}；abs 允许不存在
    （渲染层只显示文件名，导出时才强校验）。
    """
    tid = str(project.get("task_id") or "")
    pid = str(project.get("id") or "")
    override = get_clip_override(project, index)
    s5 = svc.get_stage_state(project, "stage5_coarse")
    ch = next((h for h in (s5.get("highlights") or [])
               if int(h.get("index") or 0) == int(index)), None) or {}

    video_rel = str(override.get("video") or "")
    if video_rel:
        video_abs = svc._abs(root, video_rel)
        video_manual = True
    else:
        video_abs = _resolve_video_path(root, ch, pid, tid)
        video_rel = svc._rel(root, video_abs)
        video_manual = False

    subtitle_rel = str(override.get("subtitle") or "")
    if subtitle_rel:
        subtitle_abs = svc._abs(root, subtitle_rel)
        subtitle_manual = True
    else:
        subtitle_abs = _resolve_subtitle_path(root, ch, int(index), pid, tid)
        subtitle_rel = (svc._rel(root, subtitle_abs)
                        if subtitle_abs.is_file() else "")
        subtitle_manual = False

    title = str(override.get("title") or ch.get("title")
                or f"片段 #{int(index)}")
    return {
        "title": title,
        "video_rel": video_rel, "video_abs": video_abs,
        "video_manual": video_manual,
        "subtitle_rel": subtitle_rel, "subtitle_abs": subtitle_abs,
        "subtitle_manual": subtitle_manual,
    }


# ---------- 精剪主流程：run_fine_one / run_fine_batch ----------

def _resolve_video_path(root: Path, hl: dict, project_id: str, task_id: str) -> Path:
    """粗剪 mp4 绝对路径（项目内相对路径 → 绝对）。"""
    rel = str(hl.get("coarse_mp4") or "")
    if not rel:
        return root / "tasks" / task_id / "short_video" / project_id / "stage4" / "coarse.mp4"
    cand = (root / rel).resolve()
    return cand


def _resolve_subtitle_path(root: Path, hl: dict, index: int, project_id: str, task_id: str) -> Path:
    """精剪字幕文件回退链（REQ-20261016-148 字幕优化阶段移除后）。

    老项目 refine hl 的 refined_srt/asr_srt 相对路径 → ``stage5/refined_NN.srt``
    （用户编辑版）→ ``stage5/refined_NN_asr.srt``（ASR 原始版）→
    ``stage4/coarse_NN.srt``（新默认：粗剪自产 SRT，时间戳与粗剪视频对齐）。

    REQ-20261019-217：refined/asr 三候选全是「字幕优化阶段（已移除）」的遗产，
    该阶段删掉后不再有新产者；粗剪一重跑它们就是过期化石（用户精剪 #1 字幕
    与 Stage 5 预览不一致即此因：refined_01 停在 10-16，coarse_01 是 10-19）。
    规则改为**新旧仲裁**：候选文件比当前 ``stage4/coarse_NN.srt`` 新（用户最后
    一次编辑晚于最后一次粗剪合成，时间戳仍对齐该版粗剪视频）才采用，否则落
    到粗剪 SRT。同时刻（重跑边界）归 coarse（新默认优先）。

    REQ-20261019-227：候选链**首位**插入 ``stage5/subs_NN.srt``（按合成后视频
    提取 + 用户改字的字幕）。提取必晚于合成（mtime 仲裁天然成立），且它是最新
    用户意图 → 优先于 legacy refined 化石；粗剪重合成时 subs 三件套已被作废
    删除（见 short_video_coarse.run_stage4），不会残留抢位。
    """
    coarse = svc.stage4_paths(root, task_id, project_id, index)[1]
    cands: list[Path] = [svc.stage5_subs_srt_path(root, task_id, project_id, index)]
    rel = str(hl.get("refined_srt") or hl.get("asr_srt") or "")
    if rel:
        cands.append((root / rel).resolve())
    cands.append(svc.stage5_refined_srt_path(root, task_id, project_id, index))
    cands.append(svc.stage5_default_srt_path(root, task_id, project_id, index))
    coarse_mtime = coarse.stat().st_mtime if coarse.is_file() else -1.0
    for p in cands:
        try:
            if p.is_file() and p.stat().st_mtime > coarse_mtime:
                return p
        except OSError:
            continue
    return coarse


def run_fine_one(repo_root: Path | str, task_id: str, project_id: str,
                 index: int, *, progress_cb=None) -> dict:
    """单 highlight 精剪：复用 short_video_finalize.run_stage6_one（cut_by_srt 子进程）。

    progress_cb(event, payload) 事件契约：
    - fine_clip_start: {index, total}
    - fine_clip_done: {index, total, mp4_url, srt_url}
    - fine_clip_failed: {index, total, error}
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    s5 = svc.get_stage_state(project, "stage5_coarse")
    coarse_hls = s5.get("highlights") or []
    # 按 index 找对应 highlight
    coarse_hl = next((h for h in coarse_hls if int(h.get("index") or 0) == int(index)), None)
    if not coarse_hl:
        raise svc.ShortVideoError(f"找不到 highlight #{index}")

    # REQ-20261017-157：标题/视频/字幕手动覆盖优先，否则自动继承 Stage 5
    src = resolved_clip_sources(root, project, int(index))
    coarse_mp4 = src["video_abs"]
    refined_srt = src["subtitle_abs"]
    if not coarse_mp4.is_file():
        raise svc.ShortVideoError(f"精剪源视频不存在: {coarse_mp4}")
    if not refined_srt.is_file():
        raise svc.ShortVideoError(f"精剪字幕不存在: {refined_srt}")

    if progress_cb:
        progress_cb("fine_clip_start", {"index": index, "total": 1})

    # REQ-20261018-176：混编渲染器 short_video_compose.compose_export_one
    # （背景图+区域参数+字幕+头像+导师文字+封面+BGM 单次 ffmpeg 合成；
    # 取代 cut_by_srt 子进程切割）。旧 short_video_finalize.run_stage6_one
    # 保留不再被渲染路径调用。
    res = _sv_compose.compose_export_one(
        root, task_id, project_id, index,
        coarse_mp4=coarse_mp4, refined_srt=refined_srt,
    )
    # compose_export_one 返回 ComposeResult dataclass（字段与
    # Stage6HighlightResult 同形：final_srt 不是 continuous_srt）
    err = str(getattr(res, "error", "") or "")
    if getattr(res, "status", "") != "done" or err:
        if progress_cb:
            progress_cb("fine_clip_failed", {"index": index, "total": 1,
                                             "error": err or "精剪失败"})
        raise svc.ShortVideoError(err or f"精剪 #{index} 失败")
    final_mp4 = getattr(res, "final_mp4", None)
    final_srt = getattr(res, "final_srt", None)
    if progress_cb:
        progress_cb("fine_clip_done", {"index": index, "total": 1,
                                        "mp4_path": str(final_mp4),
                                        "srt_path": str(final_srt)})
    return {
        "index": int(index),
        "final_mp4": str(final_mp4) if final_mp4 else "",
        "continuous_srt": str(final_srt) if final_srt else "",
        "duration_sec": float(getattr(res, "duration_sec", 0.0) or 0.0),
    }


def run_fine_batch(repo_root: Path | str, task_id: str, project_id: str,
                   *, progress_cb=None) -> dict:
    """批量精剪：按 stage5_coarse.highlights 全部完成。"""
    root = Path(repo_root)
    # REQ-20261018-176：渲染前确保区域检测新鲜（整批一次，不逐条跑）
    _sv_compose.ensure_layout_detected(root, task_id, project_id)
    project = svc.load_project(root, task_id, project_id)
    s5 = svc.get_stage_state(project, "stage5_coarse")
    hls = s5.get("highlights") or []
    total = len(hls)
    if not total:
        raise svc.ShortVideoError("没有粗剪 highlights，先完成 Stage 5")

    out = []
    for i, hl in enumerate(hls, 1):
        idx = int(hl.get("index") or i)
        if progress_cb:
            progress_cb("fine_clip_start", {"index": idx, "total": total,
                                             "stage": f"精剪 {i}/{total}",
                                             "progress": 5.0 + 90.0 * (i - 1) / total,
                                             "count": i - 1})
        try:
            res = run_fine_one(root, task_id, project_id, idx, progress_cb=None)
            out.append(res)
        except Exception as e:  # noqa: BLE001 — 单条失败不阻塞整批
            out.append({"index": idx, "error": str(e)[:200]})

    # 写 stage6_fine 状态
    s7 = svc.get_stage_state(project, "stage6_fine")
    s7["status"] = "done" if all("final_mp4" in r and r["final_mp4"] for r in out) else "partial"
    s7["highlights"] = out
    svc.set_stage_state(project, "stage6_fine", s7)
    svc.save_project(root, project)
    return {"results": out, "count": len(out)}


def run_fine_single(repo_root: Path | str, task_id: str, project_id: str,
                    index: int, *, progress_cb=None) -> dict:
    """单条精剪（REQ-20261017-157）：只跑该条，结果按 index 合并进
    stage6_fine.highlights（其余条 final_mp4 保留）。

    status 口径与批量一致：stage5 全部条都有 final_mp4 才算 done。
    """
    root = Path(repo_root)
    # REQ-20261018-176：渲染前确保区域检测新鲜（单条入口也先检测）
    _sv_compose.ensure_layout_detected(root, task_id, project_id)
    res = run_fine_one(root, task_id, project_id, int(index),
                       progress_cb=progress_cb)
    project = svc.load_project(root, task_id, project_id)
    s6 = svc.get_stage_state(project, "stage6_fine")
    by_idx = {int(h.get("index") or 0): h
              for h in (s6.get("highlights") or [])}
    by_idx[int(index)] = res
    merged = sorted(by_idx.values(), key=lambda h: int(h.get("index") or 0))
    s5 = svc.get_stage_state(project, "stage5_coarse")
    want = {int(h.get("index") or 0) for h in (s5.get("highlights") or [])}
    have = {int(h.get("index") or 0) for h in merged
            if h.get("final_mp4")}
    s6["status"] = "done" if want and want <= have else "partial"
    s6["highlights"] = merged
    svc.set_stage_state(project, "stage6_fine", s6)
    svc.save_project(root, project)
    return {"results": [res], "count": 1}


# ---------- 预览：2-30s 快速切段 ----------

def _preview_source(root: Path, project: dict, index: int) -> Path:
    """预览源视频解析（REQ-20261017-157）：手动覆盖视频 → 粗剪 mp4。

    REQ-20261018-176：去掉 final_mp4 分支 —— 预览的职责是「渲染混编效果」
    （compose_preview_one 现场合成），不再是「看成片切片」；成片直接点下载。
    """
    override = get_clip_override(project, index)
    if override.get("video"):
        return svc._abs(root, str(override["video"]))
    s5 = svc.get_stage_state(project, "stage5_coarse")
    ch = next((h for h in (s5.get("highlights") or [])
               if int(h.get("index") or 0) == int(index)), None)
    if not ch or not ch.get("coarse_mp4"):
        raise svc.ShortVideoError(f"找不到 #{index} 的 final_mp4 或粗剪 mp4")
    return svc._abs(root, str(ch["coarse_mp4"]))


def preview_clip(repo_root: Path | str, task_id: str, project_id: str,
                 index: int, *, start_ms: int = 0, duration_sec: int = 5) -> dict:
    """混编预览（REQ-20261018-176）：先按 bg_desc 确保区域检测新鲜，
    再走 short_video_compose 现场合成一段 2-30s 预览，落 stage6/preview_NN.mp4。
    """
    if not (2 <= int(duration_sec) <= 30):
        raise svc.ShortVideoError("preview 时长需在 2-30s 之间")
    return _sv_compose.compose_preview_one(
        repo_root, task_id, project_id, int(index),
        start_ms=int(start_ms), duration_sec=int(duration_sec),
    )


# ---------- 模板管理（短/长隔离：tasks/_global_short_video_fine_profiles.json） ----------

_SHORT_VIDEO_PROFILES_FILE = "_global_short_video_fine_profiles.json"

_DEFAULT_TEMPLATE: dict = {
    "params": DEFAULT_PARAMS,
}


def _profiles_path(repo_root: Path) -> Path:
    return Path(repo_root) / "tasks" / _SHORT_VIDEO_PROFILES_FILE


def _load_profiles(repo_root: Path) -> dict:
    p = _profiles_path(repo_root)
    if not p.is_file():
        return {"profiles": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "profiles" not in data:
            return {"profiles": []}
        return data
    except (OSError, json.JSONDecodeError):
        return {"profiles": []}


def _save_profiles(repo_root: Path, data: dict) -> None:
    p = _profiles_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _new_profile_id() -> str:
    return "sv-fine-prof-" + str(int(time.time() * 1000))[-8:]


def _unique_name(existing: list[dict], base: str) -> str:
    names = {p.get("name") for p in existing}
    if base not in names:
        return base
    i = 2
    while f"{base} ({i})" in names:
        i += 1
    return f"{base} ({i})"


def list_profiles(repo_root: Path | str) -> list[dict]:
    """返回所有短视频模板（不含 params，节省 IO）。"""
    return [
        {"id": p.get("id"), "name": p.get("name"), "updated_at": p.get("updated_at")}
        for p in _load_profiles(repo_root).get("profiles", [])
    ]


def get_profile(repo_root: Path | str, profile_id: str) -> dict | None:
    for p in _load_profiles(repo_root).get("profiles", []):
        if p.get("id") == profile_id:
            return p
    return None


def save_profile(repo_root: Path | str, *, name: str, params: dict,
                 profile_id: str | None = None) -> dict:
    """新增或更新模板。name 重复自动加 (2)/(3)…"""
    data = _load_profiles(repo_root)
    profiles = data.get("profiles", [])
    if profile_id:
        for p in profiles:
            if p.get("id") == profile_id:
                p["name"] = name
                p["params"] = params
                p["updated_at"] = _now_iso()
                _save_profiles(repo_root, data)
                return p
    # 新增
    new = {
        "id": profile_id or _new_profile_id(),
        "name": _unique_name(profiles, name),
        "params": params,
        "updated_at": _now_iso(),
    }
    profiles.append(new)
    data["profiles"] = profiles
    _save_profiles(repo_root, data)
    return new


def delete_profile(repo_root: Path | str, profile_id: str) -> bool:
    data = _load_profiles(repo_root)
    profiles = data.get("profiles", [])
    new_profiles = [p for p in profiles if p.get("id") != profile_id]
    if len(new_profiles) == len(profiles):
        return False
    data["profiles"] = new_profiles
    _save_profiles(repo_root, data)
    return True


def rename_profile(repo_root: Path | str, profile_id: str, new_name: str) -> dict | None:
    data = _load_profiles(repo_root)
    for p in data.get("profiles", []):
        if p.get("id") == profile_id:
            p["name"] = new_name
            p["updated_at"] = _now_iso()
            _save_profiles(repo_root, data)
            return p
    return None


def export_profile(repo_root: Path | str, profile_id: str) -> dict | None:
    p = get_profile(repo_root, profile_id)
    if not p:
        return None
    return {"name": p.get("name"), "params": p.get("params"),
            "exported_at": _now_iso()}


def apply_profile(repo_root: Path | str, task_id: str, project_id: str,
                  profile_id: str) -> dict:
    """套模板到项目（覆盖 params）。"""
    p = get_profile(repo_root, profile_id)
    if not p:
        raise svc.ShortVideoError(f"模板不存在: {profile_id}")
    return save_params(repo_root, task_id, project_id, p.get("params") or {})


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(time.time()).astimezone().isoformat(timespec="seconds")
