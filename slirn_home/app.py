"""Gradio Blocks 构造 — 全 HTML 自定义渲染，玻璃拟态统一风格。

本批次更新（REQ-20260921-NNN — pipeline 端点 + 预检）：
- /pipeline_run 端点：精剪合成预检失败时改成 ok=True 携带 preflight 详情返回，
  而不是 ok=False 被前端走「未知错误」分支吞掉。started=False 不启动守护线程，
  前端按 r.preflight 结构化渲染「缺哪些素材/参数原因 + 解决路径」。
"""

from __future__ import annotations

import html
import json
import os
import re
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import gradio as gr
from fastapi import Body, File, Form as _Form, Request, UploadFile  # REQ-061：精剪视频上传用；REQ-075：Request 读 X-Slirn-Auto

log = logging.getLogger(__name__)

# 确保 tasklib 可导入（slirn-standalone 在 sibling 或子模块挂载点）
# 必须在 from slirn_home.task_list import 之前调用，因为 task_list.py 顶层 import tasklib
from slirn_home.paths import ensure_tasklib_importable

ensure_tasklib_importable()

# REQ-20260926-NNN：用户管理 + 会话
from slirn_home.auth import AuthStore as _AuthStore  # noqa: E402

from tasklib import TaskManager, TaskNotFoundError, TaskStatus  # noqa: E402
from tasklib.hotword_lib import HotwordLibrary  # noqa: E402

from slirn_home.task_list import (
    confirm_delete,
    show_detail,
)
from slirn_home.fine_render import (  # noqa: F401 — 引回搬移的名字
    _DEFAULT_BGMS,
    _DEFAULT_BGMS_DIR,
    _FINE_AUDIO_DEFAULTS,
    _FINE_AUTO_KINDS,
    _FINE_DESIGN_H,
    _FINE_DESIGN_W,
    _FINE_FONT_DEFAULTS,
    _FINE_LAYOUT_DEFAULTS,
    _FINE_MATERIAL_KINDS,
    _FINE_MATERIAL_LABELS,
    _FINE_PREVIEW_DEFAULTS,
    _FINE_VOLUME_DB_MAX,
    _FINE_VOLUME_DB_MIN,
    _HEX_COLOR_OK,
    _JOB_LOCK,
    _JOB_REGISTRY,
    _JOB_TTL_SEC,
    _PIL_IMAGE_EXTS,
    _PRESCALE_THRESHOLD_PX,
    _RenderJob,
    _cleanup_stale_jobs,
    _delete_active_export_job,
    _fine_upstream_label,
    _fine_upstream_path,
    _fine_volume_db,
    _get_default_bgms,
    _get_fine_compose,
    _predict_audio_path,
    _probe_video_duration_ms,
    _read_active_export_job,
    _resolve_mat_abs,
    _run_fine_render,
    _run_fine_render_async,
    _save_bg_detect_cache,
    _save_fine_compose,
    _write_active_export_job,
    log,
)

_DELETE_ARMED = "⚠️ 确认删除？"
_DELETE_IDLE = "🗑️ 删除"


# ============================================================
# 数据层：所有任务列表 / 热词库 / 创建任务的渲染 HTML
# ============================================================

def _esc(s: Any) -> str:
    return html.escape(str(s)) if s is not None else ""


def _to_local_naive(dt):
    """datetime / ISO 字符串 → 本地时区的 naive datetime。

    tasklib 的 created_at/updated_at 是带时区的 UTC（datetime.now(timezone.utc)）；
    显示/比较前必须 astimezone() 转本地。直接剥 tzinfo 会把 UTC 当本地用，
    UTC+8 下新建任务即显示「8 小时前」（2026-09-17 修复）。naive 输入视为已是本地。
    """
    from datetime import datetime
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _fmt_local(dt) -> str:
    """datetime / ISO 字符串 → 本地时间 YYYY-MM-DD HH:MM:SS。"""
    d = _to_local_naive(dt)
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else "未知"


def _time_ago(dt) -> str:
    """datetime / ISO 字符串 → 相对时间描述（先转本地时区，见 _to_local_naive）。"""
    from datetime import datetime
    dt = _to_local_naive(dt)
    if dt is None:
        return "未知"
    delta = datetime.now() - dt
    if delta.days > 0:
        return f"{delta.days} 天前"
    if delta.seconds >= 3600:
        return f"{delta.seconds // 3600} 小时前"
    if delta.seconds >= 60:
        return f"{delta.seconds // 60} 分钟前"
    return "刚刚"


def _stats_from_summaries(summaries) -> dict:
    """REQ-20260926-NNN 修复：stats 从（已按用户过滤的）summaries 算 —
    仪表盘不再对普通用户展示全局统计。"""
    from datetime import datetime, timedelta
    total = len(summaries)
    week_ago = datetime.now() - timedelta(days=7)
    week_new = 0
    draft = 0
    done = 0
    for s in summaries:
        created = _to_local_naive(s.created_at)
        if created and created >= week_ago:
            week_new += 1
        if s.status == TaskStatus.DRAFT:
            draft += 1
        # REQ-20260919-075：终态判断同时认 MUXED（旧任务）+ FINE_CUT_DONE（新任务）
        elif s.status in (TaskStatus.MUXED, TaskStatus.FINE_CUT_DONE):
            done += 1
    return {"total": total, "week": week_new, "draft": draft, "done": done}


def _stats(mgr: TaskManager) -> dict:
    return _stats_from_summaries(mgr.list())


# =============== Dashboard ===============

def _render_dashboard(
    mgr: TaskManager,
    repo_root: Path,
    current_user: dict | None = None,
    memberships: list[str] | None = None,
) -> str:
    """仪表盘 HTML。REQ-20260926-NNN 修复：传 current_user 时按用户可见性过滤
    （普通用户只看自己的统计 + 最近任务；admin / None 看全部 — None 仅供
    服务启动期占位与测试，页面初始 HTML 一律用 _render_dashboard_skeleton）。
    """
    if current_user and not current_user.get("is_admin"):
        summaries = mgr.list_for_user(
            current_user.get("username", ""),
            is_admin=False,
            member_of=memberships or [],
        )
    else:
        summaries = mgr.list()
    stats = _stats_from_summaries(summaries)
    recent = summaries[:5]
    cards = "".join([
        f'''<div class="slirn-stat-card" data-action="goto-tasks">
            <div class="slirn-stat-icon">{icon}</div>
            <div class="slirn-stat-label">{label}</div>
            <div class="slirn-stat-value">{value}</div>
            <div class="slirn-stat-trend">{trend}</div>
        </div>'''
        for icon, label, value, trend in [
            ("📋", "总任务数", stats["total"], "所有状态汇总"),
            ("✨", "本周新建", stats["week"], "最近 7 天 ↑"),
            ("📝", "草稿数", stats["draft"], "待处理"),
            ("✅", "已完成", stats["done"], "精剪视频导出完成"),
        ]
    ])

    recent_html = ""
    for t in recent:
        status_label = t.status.value if hasattr(t.status, "value") else str(t.status)
        recent_html += f'''<div class="slirn-recent-item" data-action="view-task" data-task-id="{_esc(t.task_id)}">
            <div class="slirn-recent-avatar">🎬</div>
            <div class="slirn-recent-info">
                <div class="slirn-recent-name">{_esc(t.name)}</div>
                <div class="slirn-recent-meta">{_esc(status_label)} · {_esc(_time_ago(t.updated_at))}</div>
            </div>
        </div>'''
    if not recent_html:
        recent_html = '<div class="slirn-empty"><div class="slirn-empty-icon">📭</div><div class="slirn-empty-text">暂无任务 — 在「➕ 新建任务」中创建第一个</div></div>'

    return f'''<div id="slirn-tab-dashboard-inner" class="slirn-tab-inner">
    <div class="slirn-stats">{cards}</div>
    <div class="slirn-main-grid">
        <div class="slirn-card">
            <div class="slirn-panel-header">
                <div class="slirn-panel-title">📋 最近任务</div>
                <span class="slirn-panel-subtitle">最多显示 5 条</span>
            </div>
            <div class="slirn-recent-list">{recent_html}</div>
        </div>
        <div class="slirn-card">
            <div class="slirn-panel-header">
                <div class="slirn-panel-title">🕐 活动时间线</div>
            </div>
            <div class="slirn-recent-list">{recent_html}</div>
        </div>
    </div>
    </div>'''


def _render_dashboard_skeleton() -> str:
    """启动期仪表盘占位 — 初始 HTML 不嵌任何任务数据（REQ-20260926-NNN 修复）。

    真正的内容由前端确认登录态后经 /slirn/api/dashboard 按用户拉取；
    未登录访客（含 view-source）只能看到这个骨架。
    """
    return '''<div id="slirn-tab-dashboard-inner" class="slirn-tab-inner">
    <div class="slirn-stats"><div class="slirn-empty" style="grid-column:1/-1;">
        <div class="slirn-empty-icon">⏳</div>
        <div class="slirn-empty-text">仪表盘加载中…（登录后按你的权限显示）</div>
    </div></div>
    </div>'''


# =============== 任务列表 ===============

def _status_dot_class(status) -> str:
    if status == TaskStatus.DRAFT:
        return "draft"
    # REQ-20260919-075：终态同时认 MUXED + FINE_CUT_DONE
    if status in (TaskStatus.MUXED, TaskStatus.FINE_CUT_DONE):
        return "done"
    return "progress"


def _render_task_list(
    mgr: TaskManager,
    scope: str = "mine",
    current_user: dict | None = None,
    memberships: list[str] | None = None,
) -> str:
    """任务列表 HTML。REQ-20260926-NNN 修复：非 admin 一律按「我的」过滤 —
    scope="all" 仅 admin 有效（之前普通用户点「全部」直接看到所有人的任务）。
    current_user=None（启动期占位 / 隐藏触发器 / 测试）时不做成员过滤。
    """
    is_admin = bool(current_user and current_user.get("is_admin"))
    if not is_admin:
        scope = "mine"
    if current_user and scope == "mine":
        summaries = mgr.list_for_user(
            current_user.get("username", ""),
            is_admin=False,
            member_of=memberships or [],
        )
    else:
        summaries = mgr.list()
    # scope radio（admin 才显示「我的/全部」切换；普通用户固定「我的」）
    scope_html = (
        '<div class="slirn-scope-toggle">'
        '<label><input type="radio" name="slirn-task-scope" value="mine"' +
        (' checked' if scope != "all" else '') + '> 我的</label>'
        '<label><input type="radio" name="slirn-task-scope" value="all"' +
        (' checked' if scope == "all" else '') + '> 全部</label>'
        '</div>'
    ) if is_admin else ''

    cards = ""
    for s in summaries:
        # status 可能是 enum 或 str — 兼容
        if hasattr(s.status, "value"):
            status_label = s.status.value
            dot_class = _status_dot_class(s.status)
        else:
            status_label = str(s.status)
            dot_class = "draft"
        cards += f'''<div class="slirn-task-card" data-action="view-task" data-task-id="{_esc(s.task_id)}">
            <div class="slirn-task-card-header">
                <span class="slirn-task-id">{_esc(s.task_id)}</span>
                <span class="slirn-status-badge"><span class="slirn-status-dot {dot_class}"></span>{_esc(status_label)}</span>
            </div>
            <div class="slirn-task-name">{_esc(s.name)}</div>
            <div class="slirn-task-video">🎬 {_esc(s.original_video or "—")}</div>
            <div class="slirn-task-meta">
                <span>创建 {_esc(_time_ago(s.created_at))}</span>
                <span>修改 {_esc(_time_ago(s.updated_at))}</span>
            </div>
            <div class="slirn-task-actions">
                <button class="slirn-btn slirn-btn-sm" data-action="view-task" data-task-id="{_esc(s.task_id)}">📄 详情</button>
                <button class="slirn-btn slirn-btn-sm" data-action="edit-task" data-task-id="{_esc(s.task_id)}">✏️ 编辑</button>
                <button class="slirn-btn slirn-btn-sm slirn-btn-primary" data-action="open-workbench" data-task-id="{_esc(s.task_id)}" data-task-label="{_esc(s.name)}">✂️ 剪辑</button>
                <button class="slirn-btn slirn-btn-sm slirn-btn-danger" data-action="delete-task" data-task-id="{_esc(s.task_id)}" data-task-name="{_esc(s.name)}">🗑️ 删除</button>
            </div>
        </div>'''

    if not cards:
        cards = '''<div class="slirn-empty" style="grid-column:1/-1;">
            <div class="slirn-empty-icon">📭</div>
            <div class="slirn-empty-text">暂无任务 — 点击「➕ 新建任务」开始</div>
        </div>'''

    return f'''<div id="slirn-tab-tasks-inner" class="slirn-tab-inner">
    <div class="slirn-toolbar">
        {scope_html}
        <div class="slirn-search-wrap">
            <input class="slirn-search-box" id="slirn-task-search" placeholder="搜索任务名 / ID / 视频..." />
        </div>
        <button class="slirn-btn" data-action="refresh-tasks">🔄 刷新</button>
        <button class="slirn-btn slirn-btn-primary" data-action="goto-create">➕ 新建任务</button>
    </div>
    <div class="slirn-task-grid">{cards}</div>
    <div class="slirn-status-msg" id="slirn-task-status" style="display:none;"></div>
    </div>'''


def _render_task_list_skeleton() -> str:
    """启动期任务列表占位 — 初始 HTML 不嵌任何任务数据（REQ-20260926-NNN 修复）。

    真正的列表由前端确认登录态后经 /slirn/api/refresh_tasks 按用户拉取；
    未登录访客（含 view-source）只能看到这个骨架。
    """
    return '''<div id="slirn-tab-tasks-inner" class="slirn-tab-inner">
    <div class="slirn-toolbar">
        <div class="slirn-search-wrap">
            <input class="slirn-search-box" id="slirn-task-search" placeholder="搜索任务名 / ID / 视频..." />
        </div>
        <button class="slirn-btn" data-action="refresh-tasks">🔄 刷新</button>
        <button class="slirn-btn slirn-btn-primary" data-action="goto-create">➕ 新建任务</button>
    </div>
    <div class="slirn-task-grid"><div class="slirn-empty" style="grid-column:1/-1;">
        <div class="slirn-empty-icon">⏳</div>
        <div class="slirn-empty-text">长视频列表加载中…（登录后按你的权限显示）</div>
    </div></div>
    </div>'''


# =============== 详情 ===============

def _resolve_task_video(t) -> tuple[Path | None, str]:
    """任务要识别/预览的视频：截取段优先，否则原视频。返回 (path, label)。"""
    if t.segment and t.segment.path.exists():
        return t.segment.path, f"截取段（{t.segment.start} → {t.segment.end}）"
    if t.original_video_source.exists():
        return t.original_video_source, "完整原视频"
    return None, ""


def _ensure_segment_file(t) -> bool:
    """自愈：segment 文件缺失但原视频在 → 重新截取到 segment.path。

    历史原因 cut_preview 曾把截取文件移进 Gradio 缓存目录而 create_task 没找到，
    造成部分任务 metadata 有 segment 记录但 raw_input 无文件。幂等，成功返回 True。
    """
    if not t.segment:
        return False
    if t.segment.path.exists():
        return True
    src = t.original_video_source
    if not src.exists():
        return False
    try:
        from tasklib.video import cut_video
        t.segment.path.parent.mkdir(parents=True, exist_ok=True)
        cut_video(src, t.segment.path, t.segment.start, t.segment.end)
        return t.segment.path.exists()
    except Exception:  # noqa: BLE001 — 失败则回退原视频路径
        return False


def _render_subtitle_zone(task_id: str, t, mgr: TaskManager) -> str:
    """处理剪辑·第 1 步：字幕生成区（按钮 + 进度 + 播放器 + 字幕列表）— REQ-20260915-001。"""
    from slirn_home import asr_service

    outputs_dir = mgr.tasks_dir / task_id / "outputs"
    meta = asr_service.load_subtitle(outputs_dir)
    job = asr_service.job_status(task_id)

    video, video_label = _resolve_task_video(t)
    if video is not None:
        from tasklib.video import get_video_duration
        dur = get_video_duration(video)
        if dur:
            mm, ss = divmod(int(dur), 60)
            hh, mm = divmod(mm, 60)
            dur_str = f"{hh:02d}:{mm:02d}:{ss:02d}"
        else:
            dur_str = "时长未知"
        hint = f"识别对象：{video_label}（{dur_str}）"
        # 项目定位就是长视频剪辑：≤5 小时不提示（REQ-20260917-022），仅超支持上限时提醒
        if dur and dur > 18000:
            hint += ' · <span style="color:#c2410c;">⚠️ 视频超过 5 小时（支持上限），识别耗时很长，建议先截取重点片段</span>'
    else:
        hint = "⚠️ 任务视频文件缺失（截取段和原视频都不在磁盘上）"

    hw_count = 0
    try:
        if t.hotwords_path.exists():
            hw_count = len([w for w in t.hotwords_path.read_text(encoding="utf-8").split() if w])
    except Exception:  # noqa: BLE001
        pass
    hint += f" · 任务热词 {hw_count} 个 · 模型 seaco-paraformer（热词优化）"

    job_state = job.get("state") if job else ("done" if meta and meta.get("segments") else "idle")

    # 字幕列表（已生成时）
    list_html = ""
    if meta and meta.get("segments"):
        # 行 HTML 用 list.append + join 累积：rows += 大字符串是 O(n²) 整串拷贝
        # （长任务进工作台慢的根因之一，实测千行级 100ms → 10ms）
        row_parts: list[str] = []
        for s in meta["segments"]:
            spk = s.get("spk")
            badge = (
                f'<span class="slirn-sub-spk spk-c{(int(spk) - 1) % 6 + 1}">人员{int(spk)}</span>'
                if spk else ""
            )
            row_parts.append(
                f'<div class="slirn-sub-row{" has-spk" if spk else ""}"'
                f' data-task-id="{_esc(task_id)}"'
                f' data-start-ms="{int(s["start_ms"])}" data-end-ms="{int(s["end_ms"])}">'
                f'<span class="slirn-sub-idx">{int(s["i"])}</span>'
                f'{badge}'
                f'<span class="slirn-sub-time">{_esc(s["start"])} → {_esc(s["end"])}</span>'
                f'<span class="slirn-sub-text">{_esc(s["text"])}</span>'
                f"</div>"
            )
        rows = "".join(row_parts)
        n = len(meta["segments"])
        created = _esc(meta.get("created_at", ""))
        src_label = "截取段时间轴" if meta.get("source") == "segment" else "原视频时间轴"
        # 说话人统计行（REQ-20260917-029）：仅 SD 生成的字幕有 speakers
        speakers = meta.get("speakers") or {}
        stats_html = ""
        if speakers.get("stats"):
            parts = "、".join(f"人员{st['spk']} {st['sentences']} 句" for st in speakers["stats"])
            stats_html = (f' · 🎙 {speakers.get("count", len(speakers["stats"]))}'
                          f" 位说话人：{parts}")
        list_html = f'''<div class="slirn-sub-meta">📝 {n} 段 · 识别于 {created} · {src_label}{stats_html} · 点击任一行定位播放，播放时当前行高亮</div>
        <div class="slirn-sub-list" id="slirn-sub-list">{rows}</div>'''

    gen_btn_label = "🔄 重新生成字幕" if (meta and meta.get("segments")) else "🎙 生成字幕"

    return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">🎬 处理剪辑 · 第 1 步：生成字幕</div>
        </div>
        <div class="slirn-form-hint">{hint}</div>
        <div id="slirn-sub-player-wrap" class="slirn-video-wrap slirn-sub-player-wrap" style="display:none;">
            <video id="slirn-sub-player" controls preload="metadata"></video>
        </div>
        <div id="slirn-asr-status" class="slirn-status-msg" style="display:none;"
             data-task-id="{_esc(task_id)}" data-state="{_esc(job_state or "idle")}"></div>
        {list_html}
        <div class="slirn-task-actions" style="margin-top:14px;">
            <button class="slirn-btn slirn-btn-primary" data-action="gen-subtitle" data-task-id="{_esc(task_id)}">{gen_btn_label}</button>
            <button class="slirn-btn" data-action="play-segment" data-task-id="{_esc(task_id)}">▶️ 播放视频</button>
            <label class="slirn-sd-toggle" title="开启后用 FunASR cam++ 分辨每句话的说话人：字幕带人员编号并统计每人句数；单人视频误分成多人时可关闭重生成">
                <input type="checkbox" id="slirn-sd-switch" /> 区分说话人
            </label>
        </div>
    </div>'''


def _render_rigor_picker() -> str:
    """分析严谨性级别单选卡（REQ-20260916-003，用户必选）— 高/中/低 + 自定义 四档。

    每档带定位标题、说明、例子（revision_service.RIGOR_LEVELS），
    让操作者不看文档也能体感三档差别；不预选（必须主动选择），
    上次选择由前端 localStorage 预填（applyRevRigorState）。
    自定义档（REQ-20260916-007）：默认底稿（高档完整提示词）可编辑；
    REQ-20260916-009：高/中/低三档完整提示词随 data-prompt-* 下发，
    编辑区提供三个底稿按钮 — 点击即载入/恢复该档（默认底稿仍为高档）；
    textarea 服务端置空，由 JS 预填 localStorage 草稿（slirnRevCustomPrompt）或高档底稿。
    """
    from slirn_home import revision_service
    from slirn_home.revision_service import RIGOR_LEVELS

    cards = ""
    for key in ("high", "medium", "low"):
        cfg = RIGOR_LEVELS[key]
        cards += (
            f'<label class="slirn-rigor-card" title="{_esc(cfg["desc"])}">'
            f'<input type="radio" name="slirn-rev-rigor" value="{key}" />'
            f'<span class="slirn-rigor-card-title">{_esc(cfg["badge"])} · {_esc(cfg["title"])}</span>'
            f'<span class="slirn-rigor-card-desc">{_esc(cfg["desc"])}</span>'
            f'<span class="slirn-rigor-card-example">例：{_esc(cfg["example"])}</span>'
            f'</label>'
        )
    cards += (
        '<label class="slirn-rigor-card" title="可在高/中/低任一底稿基础上修改修订标准，'
        '适合有自己一套规则的老手">'
        '<input type="radio" name="slirn-rev-rigor" value="custom" />'
        '<span class="slirn-rigor-card-title">自 · 自定义严谨性</span>'
        '<span class="slirn-rigor-card-desc">可在高/中/低任一底稿基础上修改修订标准，'
        '可附加自有规则</span>'
        '<span class="slirn-rigor-card-example">例：附加「专业术语保留英文原文」等自有规则</span>'
        '</label>'
    )
    # 自定义提示词编辑区：选「自定义」时由 JS 展开；高/中/低三档完整提示词随
    # data-prompt-* 下发（REQ-20260916-009）— 点底稿按钮即载入/恢复该档提示词
    # （与服务端 default_custom_prompt 回退逻辑同源，默认底稿 = 高档）
    preset_btns = "".join(
        f'<button type="button" class="slirn-btn slirn-rigor-preset"'
        f' data-action="rigor-prompt-preset" data-preset="{key}"'
        f' title="载入「{_esc(cfg["badge"])} · {_esc(cfg["title"])}」完整提示词，可在此基础上修改">'
        f'{_esc(cfg["badge"])} · {_esc(cfg["title"])}</button>'
        for key, cfg in RIGOR_LEVELS.items()
    )
    custom = (
        f'<div class="slirn-rigor-custom" id="slirn-rigor-custom" style="display:none;"'
        f' data-prompt-high="{_esc(revision_service.build_system_prompt("high"))}"'
        f' data-prompt-medium="{_esc(revision_service.build_system_prompt("medium"))}"'
        f' data-prompt-low="{_esc(revision_service.build_system_prompt("low"))}">'
        '<div class="slirn-rigor-custom-head">'
        '<span>📝 自定义提示词（可载入高/中/低底稿修改）</span>'
        '<span class="slirn-rigor-preset-group">'
        '<span class="slirn-rigor-preset-label">底稿：</span>'
        f'{preset_btns}'
        '</span>'
        '</div>'
        '<textarea class="slirn-textarea slirn-rigor-custom-text" rows="10"'
        ' placeholder="点右侧底稿载入参考后直接修改…"></textarea>'
        '<div class="slirn-rigor-custom-tip">点「底稿：高/中/低」载入对应级别的完整提示词'
        '（会覆盖当前编辑内容），给修改提供参考、也可一键恢复任一档；'
        '请保留「输出要求」中的 JSON 格式部分，否则模型输出无法解析；'
        '内容更正（fix）判定建议保留。草稿自动保存，仅本机浏览器。</div>'
        '</div>'
    )
    return f'<div class="slirn-rigor-cards">{cards}</div>{custom}'


def _render_revision_zone(task_id: str, t, mgr: TaskManager) -> str:
    """处理剪辑·第 2 步：字幕修订（大模型建议 + 手动决策）— REQ-20260915-005。"""
    from slirn_home import asr_service, revision_service

    outputs_dir = mgr.tasks_dir / task_id / "outputs"
    sub_meta = asr_service.load_subtitle(outputs_dir)
    rev = revision_service.load_revision(outputs_dir)
    job = revision_service.job_status(task_id)

    # ---- 状态 1：上一阶段未完成 → 引导 ----
    if not (sub_meta and sub_meta.get("segments")):
        return '''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header"><div class="slirn-panel-title">🎬 处理剪辑 · 第 2 步：字幕修订</div></div>
        <div class="slirn-empty"><div class="slirn-empty-icon">🚧</div>
            <div class="slirn-empty-text">请先完成上一阶段「字幕生成」— 修订以生成的字幕列表为输入</div></div>
        <div class="slirn-task-actions" style="margin-top:14px;">
            <button class="slirn-btn slirn-btn-primary" data-action="wb-stage" data-pane="subtitle">🎙 去生成字幕</button>
        </div></div>'''

    entries = (rev or {}).get("entries") or []
    job_state = job.get("state") if job else ("done" if entries else "idle")
    model = _esc((rev or {}).get("model") or "")
    status_display = "" if job_state == "running" else "display:none;"
    running_html = ""
    if job_state == "running":
        import time as _t
        elapsed = int((job.get("finished_at") or _t.time()) - job.get("started_at", _t.time()))
        running_html = f"⏳ {_esc(job.get('stage') or '处理中')} · 已耗时 {elapsed}s"

    # REQ-20260919-068：字幕修订阶段关联人员 ID（与切分修剪同口径）。
    # 关联状态落盘后，重进面板时现算（按当前 revision entries + subtitle spk），
    # 保证统计口径永远最新；快照仅供人工检查。
    rev_spk_link = None
    rev_spk_rows: dict[str, int] = {}
    if entries and sub_meta:
        from slirn_home import rev_speaker
        saved_link = rev_speaker.load_link(outputs_dir)
        if saved_link is not None:
            # 现算（不读快照的 rows/stats，但若字幕无 spk → 用 saved_link 提示）
            fresh = rev_speaker.link_speakers(sub_meta, rev)
            if fresh.get("available"):
                rev_spk_link = fresh
                rev_spk_rows = fresh.get("rows") or {}
            else:
                rev_spk_link = None  # 当前 subtitle 无 spk，不渲染统计条
        # saved_link is None → 不渲染 spk_bar（按钮显示「👤 关联人员ID」）

    # ---- 状态 2：有字幕、无建议 → 说明 + 分析按钮 ----
    if not entries:
        from slirn_home import llm_config

        cur_model = _esc(llm_config.get_current(mgr.tasks_dir.parent))
        return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header"><div class="slirn-panel-title">🎬 处理剪辑 · 第 2 步：字幕修订</div></div>
        <div class="slirn-form-hint">把上一阶段生成的 {len(sub_meta['segments'])} 段字幕交给大模型（<b>{cur_model}</b>，可在顶栏「大模型设置」 修改）逐段分析，识别：</div>
        <div class="slirn-rev-intro">
            <div>🟥 <strong>整行删除</strong> — 口癖、口头禅、语气词、无意义内容</div>
            <div>🟩 <strong>完整保留</strong> — 正常有效内容</div>
            <div>🟪 <strong>切分修剪</strong> — 行内重复只保留一次、剔除夹杂语气词（附建议保留文本）</div>
            <div>🟦 <strong>内容更正</strong> — 错字/别字等文字错误，直接给出更正后字幕（说明附原文对照）</div>
            <div>🟧 <strong>人工复核</strong> — 模型拿不准，交给你判断</div>
        </div>
        <div class="slirn-form-hint">每段建议都带具体分析说明；你在建议之上逐条决策（采纳/改判；切分行填写切分修剪后内容，更正行填写更正后内容）。</div>
        <div class="slirn-form-hint" style="margin-top:14px;"><b>分析严谨性级别</b>（必选）— 决定大模型按多严格的标准处理字幕：</div>
        {_render_rigor_picker()}
        <div id="slirn-rev-status" class="slirn-status-msg" style="{status_display};"
             data-task-id="{_esc(task_id)}" data-state="{_esc(job_state)}">{running_html}</div>
        <div class="slirn-task-actions" style="margin-top:14px;">
            <button class="slirn-btn slirn-btn-primary" data-action="revise-subtitle" data-task-id="{_esc(task_id)}">🤖 大模型分析字幕</button>
        </div></div>'''

    # ---- 状态 3：建议列表（行式与字幕生成列表一致：序号|时间|文本+徽章|决策；
    #      模型分析/建议保留/手动说明收进每行可展开的详情块 — REQ-20260916-002）----
    # 行 HTML 用 list.append + join 累积：rows += 大字符串是 O(n²) 整串拷贝
    # （长任务进工作台慢的根因之一，实测 924 行 357ms → 7.5ms）
    row_parts: list[str] = []
    for e in entries:
        cat = e.get("category", "review")
        cat_label = dict(revision_service.LLM_CATEGORIES).get(cat, ("人工复核",))[0]
        decision = e.get("decision", "pending")
        # 需要人工关注或已有手动说明的行默认展开，其余收起保持列表紧凑
        open_detail = cat == "review" or bool(e.get("user_note"))
        # 未决策的行默认选「采纳建议」（REQ-20260916-003）：扫一遍改掉不同意的，
        # 直接保存即全量采纳；「未决策」选项保留，可手动改回
        sel_val = decision if decision != "pending" else "accept"
        opts = "".join(
            f'<option value="{k}"{" selected" if sel_val == k else ""}>{v}</option>'
            for k, v in revision_service.USER_DECISIONS.items()
        )
        # 建议保留（split）/ 更正后（fix）：模型给出的目标文本，收进详情块（REQ-20260916-006）
        extra_html = ""
        if e.get("keep_text"):
            if cat == "split":
                extra_html = (
                    f'<div class="slirn-rev-keeptext">✂️ 建议保留：「{_esc(e["keep_text"])}」</div>'
                )
            elif cat == "fix":
                extra_html = (
                    f'<div class="slirn-rev-keeptext fix">✏️ 更正后：「{_esc(e["keep_text"])}」</div>'
                )
        # split/fix 行输入框自动预填建议文本（REQ-20260916-010 / 20260917-026）：
        # 手动填写优先；未填 → keep_text 作起点（与服务端分析时自动填写、切分清单
        # 回退同口径，存量任务未重分析也能看到/微调将要生效的修剪后/更正后内容）
        note_val = str(e.get("user_note") or "").strip()
        if not note_val and cat in ("split", "fix") and e.get("keep_text"):
            note_val = str(e["keep_text"])
        # 输入框语义按建议类别区分（决策下拉改为 fix/split 时 JS 动态跟随，REQ-20260917-026）
        note_ph = "更正后内容（可微调）" if cat == "fix" else "切分修剪后内容（可空）"
        # 决策的实质类别（REQ-20260916-010 过滤口径，与 cutlist_service._final_kind
        # 同源）：手动改判优先；accept = 模型建议类别；pending = 空（未决策）
        if decision in ("keep", "delete", "split", "fix"):
            final_kind = decision
        elif decision == "accept" and cat in ("keep", "delete", "split", "fix"):
            final_kind = cat
        else:
            final_kind = ""
        # REQ-20260919-068：行级人员编号（仅在已关联时显示）
        _i_str = str(int(e["i"]))
        _spk = rev_spk_rows.get(_i_str)
        _spk_attr = f' data-spk="{int(_spk)}"' if _spk else ""
        _spk_badge_html = f'👤{int(_spk)}' if _spk else ""
        row_parts.append(
            f'<div class="slirn-rev-row{" open" if open_detail else ""}" data-task-id="{_esc(task_id)}"'
            f' data-start-ms="{int(e.get("start_ms", 0))}" data-end-ms="{int(e.get("end_ms", 0))}"'
            f' data-sugg="{_esc(cat)}" data-decision="{_esc(decision)}" data-final="{_esc(final_kind)}"'
            f' data-i="{int(e["i"])}"{_spk_attr}'
            f' data-text="{_esc(e.get("text", ""))}">'
            f'<div class="slirn-rev-line">'
            f'<span class="slirn-sub-idx">{int(e["i"])}</span>'
            f'<span class="slirn-rev-spk">{_spk_badge_html}</span>'
            f'<span class="slirn-sub-time">{_esc(e.get("start", ""))} → {_esc(e.get("end", ""))}</span>'
            f'<span class="slirn-sub-text"><span class="slirn-rev-badge {cat}">{cat_label}</span>'
            f'{_esc(e.get("text", ""))}</span>'
            f'<select class="slirn-rev-select" data-i="{int(e["i"])}" title="处理决策">{opts}</select>'
            f'<button class="slirn-rev-toggle" data-action="rev-detail" data-i="{int(e["i"])}"'
            f' title="展开/收起模型分析与修剪/更正内容">{"▴" if open_detail else "▾"}</button>'
            f'</div>'
            f'<div class="slirn-rev-detail">'
            f'<div class="slirn-rev-note">🤖 {_esc(e.get("note", ""))}</div>{extra_html}'
            f'<input class="slirn-rev-note-input" data-i="{int(e["i"])}"'
            f' placeholder="{_esc(note_ph)}" value="{_esc(note_val)}" />'
            f'</div></div>'
        )
    rows = "".join(row_parts)

    n = len(entries)
    cat_counts = {k: 0 for k in revision_service.LLM_CATEGORIES}
    for e in entries:
        cat_counts[e.get("category", "review")] = cat_counts.get(e.get("category", "review"), 0) + 1
    decided = sum(1 for e in entries if e.get("decision") != "pending")
    created = _esc((rev or {}).get("created_at", ""))
    rigor_key = (rev or {}).get("rigor") or ""
    rigor_cfg = revision_service.RIGOR_LEVELS.get(rigor_key)  # 旧数据无 rigor → 不显示
    if rigor_cfg:
        rigor_stats = f" · 严谨性 {rigor_cfg['badge']}（{rigor_cfg['title']}）"
    elif rigor_key == revision_service.CUSTOM_RIGOR_KEY:
        rigor_stats = " · 严谨性 自定义"  # 实际提示词见 meta.custom_prompt（REQ-20260916-007）
    else:
        rigor_stats = ""
    stats = (
        f"📝 {n} 段 · 分析于 {created} · 模型 {model}{rigor_stats} · "
        f"保留 {cat_counts.get('keep', 0)} / 删除 {cat_counts.get('delete', 0)} / "
        f"切分 {cat_counts.get('split', 0)} / 更正 {cat_counts.get('fix', 0)} / 复核 {cat_counts.get('review', 0)}"
        f" · ✅ 已决策 <b>{decided}/{n}</b> · 点击行连续跳播（本条播完跳下一条，段间空白不播） · 决策列默认「采纳建议」"
        f" · ▾ 展开模型分析与修剪/更正内容"
    )
    # REQ-20260918-039：批量改判目标 = 决策下拉全部状态（与单条决策同口径）
    rev_batch_opts = "".join(
        f'<option value="{k}"{" selected" if k == "keep" else ""}>{v}</option>'
        for k, v in revision_service.USER_DECISIONS.items()
    )

    # REQ-20260919-068：字幕修订阶段关联人员 ID 后的统计条 + 按钮文案。
    # 模式与切分修剪 cut_spk_bar 完全对齐（含 chips、查找、上/下一条、删除、重算）。
    if rev_spk_link and rev_spk_link.get("available"):
        chips = "".join(
            f'<span class="slirn-rev-spk-chip" data-action="rev-spk-chip" data-spk="{s["spk"]}"'
            f' title="点击填入查找框（统计仅计未删除决策行）">👤{s["spk"]} · <b>{s["count"]}</b> 条</span>'
            for s in rev_spk_link["stats"]
        )
        rev_spk_bar = (
            '<div id="slirn-rev-spk-bar" class="slirn-rev-spk-bar" data-linked="1">'
            '<div class="slirn-rev-spk-title">👥 人员统计（仅计未删除决策行 · 关联已保存，重进任务自动显示）</div>'
            f'<div class="slirn-rev-spk-chips">{chips}</div>'
            '<div class="slirn-rev-spk-find">按人员ID查找：'
            '<input id="slirn-rev-spk-q" type="number" min="1" step="1" placeholder="如 2">'
            '<label class="slirn-rev-spk-skiplbl" title="勾选后「上一条/下一条」只在未删除决策行间跳转">'
            '<input id="slirn-rev-spk-skipdel" type="checkbox" checked>跳过已删除</label>'
            '<button class="slirn-btn slirn-btn-xs" data-action="rev-spk-prev">⬆️ 上一条</button>'
            '<button class="slirn-btn slirn-btn-xs" data-action="rev-spk-next">⬇️ 下一条</button>'
            '<button class="slirn-btn slirn-btn-xs" data-action="rev-spk-delete">❌ 删除该人员全部记录</button>'
            '<button class="slirn-btn slirn-btn-xs" data-action="rev-spk-recount">🧮 重新统计</button>'
            '<span class="slirn-rev-spk-hint">统计只计未删除决策行 — 删除非主讲人员后重算即可确认清零；'
            '删除改判需「💾 保存修订决策」落盘</span></div></div>'
        )
        rev_spk_btn_label = "🔄 重新关联人员ID"
    else:
        rev_spk_bar = '<div id="slirn-rev-spk-bar" class="slirn-rev-spk-bar" style="display:none;"></div>'
        rev_spk_btn_label = "👤 关联人员ID"

    return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header"><div class="slirn-panel-title">🎬 处理剪辑 · 第 2 步：字幕修订</div></div>
        <div class="slirn-sub-meta">{stats}</div>
        <div class="slirn-rev-kbhint">⌨ 快捷键：<kbd>↑</kbd><kbd>↓</kbd> 上一条 / 下一条 · <kbd>空格</kbd> 播放 / 暂停
 · <kbd>R</kbd> 重播本行 · <kbd>K</kbd> 保留 · <kbd>D</kbd> 删除（标记后自动下一条）
 · <kbd>S</kbd> 切分（展开详情聚焦内容） · <kbd>Esc</kbd> 退出输入框
 · <span class="slirn-revkeys-open" data-action="revkeys-open" role="button" tabindex="0">⚙ 自定义</span></div>
        <div class="slirn-rev-filter" id="slirn-rev-filter">
          <span class="slirn-rev-filter-label">🔎 筛选</span>
          <span class="slirn-rev-filter-dims">
            <button type="button" class="slirn-rev-filter-dim active" data-rev-filter-dim="sugg">建议状态</button>
            <button type="button" class="slirn-rev-filter-dim" data-rev-filter-dim="dec">决策状态</button>
          </span>
          <span class="slirn-rev-filter-chips" id="slirn-rev-filter-chips"></span>
          <select class="slirn-rev-jump-sel" id="slirn-rev-jump-sel"
                  title="跳转目标状态：跳转时整个列表保持显示，只定位到该状态的上/下一条记录（上下文可见）"></select>
          <span class="slirn-rev-jump">
            <button type="button" class="slirn-rev-jump-btn" data-rev-jump="prev" title="跳到上一条所选状态的记录">⬆ 上一条</button>
            <button type="button" class="slirn-rev-jump-btn" data-rev-jump="next" title="跳到下一条所选状态的记录">下一条 ⬇</button>
          </span>
          <span class="slirn-rev-filter-count" id="slirn-rev-filter-count"></span>
        </div>
        <div class="slirn-batch-bar" id="slirn-rev-batch">
          <span class="slirn-batch-label">🧮 批量</span>
          序号 <input class="slirn-batch-num" id="slirn-rev-batch-start" type="number" min="1" step="1">
          ～ <input class="slirn-batch-num" id="slirn-rev-batch-end" type="number" min="1" step="1">
          改为 <select class="slirn-batch-sel" id="slirn-rev-batch-sel">{rev_batch_opts}</select>
          <button type="button" class="slirn-btn slirn-btn-xs" data-action="rev-batch-apply"
                  title="把区间内字幕的决策批量改为所选状态（确认后应用，保存前可继续调整）">🧮 批量应用</button>
          <span class="slirn-batch-hint">区间内所有行一次改判 — 应用后记得「💾 保存修订决策」</span>
        </div>
        <div class="slirn-batch-bar" id="slirn-rev-search">
          <span class="slirn-batch-label">🔎 搜索</span>
          <input class="slirn-search-q" id="slirn-rev-search-q" type="text"
                 placeholder="输入字幕内容片段定位（回车下一条，Shift+回车上一条）">
          <span class="slirn-search-count" id="slirn-rev-search-count"></span>
          <button type="button" class="slirn-btn slirn-btn-xs" data-action="rev-search-prev">⬆ 上一条</button>
          <button type="button" class="slirn-btn slirn-btn-xs" data-action="rev-search-next">下一条 ⬇</button>
          <span class="slirn-batch-hint">按内容定位字幕（不区分大小写）；筛选生效时只在可见行中搜</span>
        </div>
        {rev_spk_bar}
        <div id="slirn-rev-player-wrap" class="slirn-video-wrap slirn-sub-player-wrap" style="display:none;">
            <video id="slirn-rev-player" controls preload="metadata"></video>
        </div>
        <div id="slirn-rev-status" class="slirn-status-msg" style="{status_display};"
             data-task-id="{_esc(task_id)}" data-state="{_esc(job_state)}">{running_html}</div>
        <div class="slirn-rev-list" id="slirn-rev-list">{rows}</div>
        <details class="slirn-rigor-box" id="slirn-rev-rigor-box">
            <summary>🔄 重新分析：点选分析严谨性级别（必选）</summary>
            {_render_rigor_picker()}
        </details>
        <div class="slirn-task-actions" style="margin-top:14px;">
            <button class="slirn-btn slirn-btn-primary" data-action="save-revision" data-task-id="{_esc(task_id)}">💾 保存修订决策</button>
            <button class="slirn-btn" data-action="play-rev-video" data-task-id="{_esc(task_id)}">▶️ 播放视频</button>
            <button class="slirn-btn" data-action="revise-subtitle" data-task-id="{_esc(task_id)}" data-has-revision="1">🔄 重新分析</button>
            <button class="slirn-btn" data-action="rev-spk-link" data-task-id="{_esc(task_id)}" title="按时间段重叠把 subtitle 段级 spk 对齐到修订每行">{rev_spk_btn_label}</button>
        </div></div>'''


def _render_cutlist_zone(task_id: str, t, mgr: TaskManager) -> str:
    """处理剪辑·第 3 步：切分修剪（REQ-20260916-008）。

    把字幕修订的最终决策翻译成切分修剪清单：保留/更正整段带入（编号继承
    不变）、删除剔除、切分对齐字级时间戳切子段（父编号.子序号 10.1/10.2/10.3）。
    面板打开即服务端现算预览（不落盘）；「生成切分清单」才落盘 +
    推进 ROUGH_CUT_DONE。
    """
    from slirn_home import asr_service, cut_speaker, cutlist_service, revision_service

    outputs_dir = mgr.tasks_dir / task_id / "outputs"

    # ---- 状态 1：上一阶段未完成 → 引导 ----
    def _guide(msg: str, btn: str) -> str:
        return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header"><div class="slirn-panel-title">✂️ 处理剪辑 · 第 3 步：切分修剪</div></div>
        <div class="slirn-empty"><div class="slirn-empty-icon">🚧</div>
            <div class="slirn-empty-text">{_esc(msg)}</div></div>
        <div class="slirn-task-actions" style="margin-top:14px;">{btn}</div></div>'''

    rev = revision_service.load_revision(outputs_dir)
    entries = (rev or {}).get("entries") or []
    if not entries:
        return _guide(
            "请先完成上一阶段「字幕修订」— 切分修剪以修订决策（保留/删除/切分/更正）为输入",
            '<button class="slirn-btn slirn-btn-primary" data-action="wb-stage" '
            'data-pane="subtitle_review">📝 去字幕修订</button>',
        )
    if not revision_service.all_decided(rev):
        pending = sum(1 for e in entries if e.get("decision") == "pending")
        return _guide(
            f"字幕修订还有 {pending}/{len(entries)} 条未决策 — 全部决策并保存后，本阶段才可生成切分清单",
            '<button class="slirn-btn slirn-btn-primary" data-action="wb-stage" '
            'data-pane="subtitle_review">📝 去完成决策</button>',
        )
    sub_meta = asr_service.load_subtitle(outputs_dir)
    if not (sub_meta and sub_meta.get("segments")):
        return _guide(
            "缺少字幕生成产物（subtitle.json）— 请先在「字幕生成」阶段生成字幕",
            '<button class="slirn-btn slirn-btn-primary" data-action="wb-stage" '
            'data-pane="subtitle">🎙 去生成字幕</button>',
        )

    # ---- 状态 2：决策齐备 → 预览清单（服务端现算；已保存的手工决策并入显示）----
    saved = cutlist_service.load_cutlist(outputs_dir)
    cutlist = cutlist_service.build_cutlist(
        sub_meta, rev,
        manual_marks=(saved or {}).get("manual_marks") if saved else None,
        actions=(saved or {}).get("actions") if saved else None,
    )
    stats = cutlist["stats"]
    dur_ms = stats["keep_duration_ms"]
    mm_, ss_ = divmod(dur_ms // 1000, 60)
    hh_, mm_ = divmod(mm_, 60)
    dur_str = f"{hh_}:{mm_:02d}:{ss_:02d}" if hh_ else f"{mm_:02d}:{ss_:02d}"
    saved_note = ""
    if saved and saved.get("items"):
        saved_at = _esc(saved.get("saved_at") or "")
        saved_note = f" · ✅ 清单已生成{f'（{saved_at}）' if saved_at else ''}"

    # 按父段分组渲染（REQ-20260916-011 第二步）：keep/fix 整段 = 单行组；
    # split = 组头 + 完整子段表（keep 块 + delete 洞，时间序编号 父.N）
    actions_map: dict[int, str] = {}
    for k, v in (cutlist.get("actions") or {}).items():
        try:
            actions_map[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    # 组级决策下拉（REQ-20260917-027）：点徽章位置即弹出可选状态——鼠标改判
    # 与快捷键 K/D/S 同效（原来仅键盘可达；删除后跳播会抢走选中，鼠标无从恢复）
    ACT_OPTS = [("", "维持原状"), ("keep", "✅ 改判保留"),
                ("delete", "❌ 改判删除"), ("split", "✂️ 改判切分")]

    def _act_sel(act: str | None) -> str:
        a = act or ""
        opts = "".join(
            f'<option value="{v}"{" selected" if a == v else ""}>{label}</option>'
            for v, label in ACT_OPTS
        )
        return ('<select class="slirn-cut-abadge slirn-cut-actsel" data-actsel'
                ' title="选择本条字幕的决策（与快捷键 K/D/S 同效）">' + opts + "</select>")
    groups: list[tuple[int, list[dict]]] = []
    for it in cutlist["items"]:
        si = int(it["source_i"])
        if not groups or groups[-1][0] != si:
            groups.append((si, []))
        groups[-1][1].append(it)
    # 改判切分的整段组预填用：修订条目的切分后内容（user_note 优先 → 模型建议）
    note_by_i: dict[int, str] = {}
    for e in rev["entries"]:
        try:
            note_by_i[int(e["i"])] = cutlist_service._split_target(e)
        except (KeyError, TypeError, ValueError):
            continue

    # 人员关联（REQ-033）：已落盘 enabled → 现算对齐（与端点同口径；快照备查），
    # 行上直接渲染 👤 徽章 + data-spk；subtitle 已无 spk（重新生成过）→ 静默回普通态
    spk_link = None
    if cut_speaker.load_link(outputs_dir):
        spk_link = cut_speaker.link_speakers(sub_meta, cutlist)
        if not spk_link.get("available"):
            spk_link = None
    spk_rows: dict[str, int] = (spk_link or {}).get("rows") or {}

    def _spk_attr(rid) -> str:
        spk = spk_rows.get(str(rid))
        return f' data-spk="{int(spk)}"' if spk else ""

    def _spk_badge(rid) -> str:
        spk = spk_rows.get(str(rid))
        if not spk:
            return ""
        # REQ-20260917-036：紧跟序号渲染（第2轨），与其他信息同行不折行
        return (f'<span class="slirn-cut-spk"'
                f' title="人员 {int(spk)}（时间段重叠最大的字幕段说话人）">👤{int(spk)}</span>')

    # 行 HTML 用 list.append + join 累积：rows += 大字符串是 O(n²) 整串拷贝
    # （长任务进工作台慢的根因之一，实测千行级 450ms → 数十 ms）
    row_parts: list[str] = []
    for si, its in groups:
        first = its[0]
        is_split_group = any(x.get("sub") is not None for x in its)
        act = actions_map.get(si)
        act_attr = f' data-act="{_esc(act)}"' if act else ""
        if is_split_group:
            # 切分组：组头（父编号+原段→切分后文字+决策下拉+重切/试听钮）+ 完整子段表
            row_parts.append(
                f'<div class="slirn-cut-group split" data-source-i="{si}"'
                f' data-task-id="{_esc(task_id)}"{act_attr}'
                f' data-target="{_esc(first.get("target_text") or "")}">'
                f'<div class="slirn-cut-ghead">'
                f'<span class="slirn-sub-idx">{si}</span>'
                f'<span class="slirn-rev-badge split" data-kind="split">切分修剪</span>'
                f'<span class="slirn-cut-gtext">原段：「{_esc(first.get("orig_text", ""))}」'
                f'<span class="slirn-cut-ptarget">→ 切分后：「{_esc(first.get("target_text") or "")}」</span></span>'
                f'{_act_sel(act)}'
                f'<button class="slirn-btn slirn-btn-xs" data-cut-act="resplit"'
                f' title="修改切分后内容并按新内容重新划分本段">✂️ 重新切分</button>'
                f'<button class="slirn-btn slirn-btn-xs" data-cut-act="play-keep"'
                f' title="连续跳播本段全部保留子段（成片效果）">▶ 试听</button></div>'
            )
            for it in its:
                mk = str(it.get("mark") or "keep")
                sub_orig_text = str(first.get("orig_text") or "")  # 子段共用父段原文（搜原文命中用）
                mk_manual = " ✏️" if it.get("mark_manual") else ""
                mk_label = ("✅ 保留" if mk == "keep" else "❌ 删除") + mk_manual
                fb_title = ' title="无字级时间戳（旧字幕数据）或切分后文字无法对齐 — 已整段带入，可重新生成字幕后重试"' if it.get("fallback") else ""
                fb_mark = " ⚠️" if it.get("fallback") else ""
                row_parts.append(
                    f'<div class="slirn-cut-row sub mark-{mk}" data-task-id="{_esc(task_id)}"'
                    f' data-id="{_esc(it["id"])}" data-mark="{mk}" data-mark-init="{mk}" data-source-i="{si}"'
                    f' data-start-ms="{int(it["start_ms"])}" data-end-ms="{int(it["end_ms"])}"{_spk_attr(it["id"])}{fb_title}'
                    f' data-text="{_esc(it["text"])}" data-orig="{_esc(sub_orig_text)}">'
                    f'<span class="slirn-sub-idx">{_esc(it["id"])}</span>'
                    f'{_spk_badge(it["id"])}'
                    f'<span class="slirn-sub-time">{_esc(it["start"])} → {_esc(it["end"])}</span>'
                    f'<span class="slirn-cut-mark" title="点击翻转 保留/删除">{mk_label}</span>'
                    f'<span class="slirn-sub-text">{_esc(it["text"])}{fb_mark}</span>'
                    f'</div>'
                )
            row_parts.append("</div>")
        else:
            # 整段组（keep/fix）：单行即组（可预播/键盘选中/改判，无需组头）。
            # 改判切分（act=split）时附 ✂️ 入口 + data-target（修订 user_note 预填）
            kind = str(first.get("kind", "keep"))
            kind_label = dict(cutlist_service.CUT_KINDS).get(kind, ("完整保留",))[0]
            if kind == "fix":
                text_html = (f'{_esc(first.get("text", ""))}'
                             f'<span class="slirn-cut-ptarget">（原文「{_esc(first.get("orig_text", ""))}」）</span>')
            else:
                text_html = _esc(first.get("text", ""))
            orig_text = str(first.get("text") or first.get("orig_text") or "")
            resplit_btn = ('<button class="slirn-btn slirn-btn-xs" data-cut-act="resplit"'
                           ' title="填写切分后内容，把本段按新内容重新切分">✂️ 重新切分</button>'
                           ) if act == "split" else ""
            row_parts.append(
                f'<div class="slirn-cut-group" data-source-i="{si}"'
                f' data-task-id="{_esc(task_id)}"{act_attr}'
                f' data-orig-text="{_esc(orig_text)}"'
                f' data-target="{_esc(note_by_i.get(si, ""))}">'
                f'<div class="slirn-cut-row whole" data-task-id="{_esc(task_id)}"'
                f' data-id="{_esc(first["id"])}" data-source-i="{si}"'
                f' data-start-ms="{int(first["start_ms"])}" data-end-ms="{int(first["end_ms"])}"{_spk_attr(first["id"])}'
                f' data-text="{_esc(first.get("text", ""))}" data-orig="{_esc(orig_text)}">'
                f'<span class="slirn-sub-idx">{si}</span>'
                f'{_spk_badge(first["id"])}'
                f'<span class="slirn-sub-time">{_esc(first["start"])} → {_esc(first["end"])}</span>'
                f'<span class="slirn-rev-badge {kind}" data-kind="{kind}">{kind_label}</span>'
                f'<span class="slirn-sub-text">{text_html}</span>'
                f'{_act_sel(act)}'
                f'{resplit_btn}'
                f'</div></div>'
            )
    rows = "".join(row_parts)
    # fallback 提示（有降级子段时在统计行下提醒）
    n_fb = stats["fallback"]
    fb_hint = (
        f'<div class="slirn-form-hint">⚠️ {n_fb} 条切分段缺少字级时间戳（旧格式字幕），已整段带入；'
        f"在「字幕生成」阶段重新生成字幕后重算即可精确切分。</div>" if n_fb else ""
    )

    # 修订比已保存清单新 → 过期黄条（REQ-20260916-011）
    stale_note = ""
    if saved and saved.get("items"):
        rev_at = str((rev or {}).get("saved_at") or "")
        cut_at = str(saved.get("saved_at") or "")
        if rev_at and cut_at and rev_at > cut_at:
            stale_note = ('<div class="slirn-form-hint slirn-cut-stale">'
                          "📝 修订决策已更新，已保存的切分清单可能过期 — 可「🔄 重新执行切分修剪」按最新决策重算</div>")

    # 人员统计条（REQ-033）：已关联 → 服务端直接渲染（结构与 router.cutSpkBarRender
    # 一致），chips 走事件委托（data-action="cut-spk-chip"），容器 data-linked=1
    # 让「重新统计」守卫直接放行。统计口径：仅计未删除记录。
    if spk_link:
        chips = "".join(
            f'<span class="slirn-cut-spk-chip" data-action="cut-spk-chip" data-spk="{s["spk"]}"'
            f' title="点击填入查找框（统计仅计未删除记录）">👤{s["spk"]} · <b>{s["count"]}</b> 条</span>'
            for s in spk_link["stats"]
        )
        spk_bar = (
            '<div id="slirn-cut-spk-bar" class="slirn-cut-spk-bar" data-linked="1">'
            '<div class="slirn-cut-spk-title">👥 人员统计（仅计未删除记录 · 关联已保存，重进任务自动显示）</div>'
            f'<div class="slirn-cut-spk-chips">{chips}</div>'
            '<div class="slirn-cut-spk-find">按人员ID查找：'
            '<input id="slirn-cut-spk-q" type="number" min="1" step="1" placeholder="如 2">'
            '<label class="slirn-cut-spk-skiplbl" title="勾选后「上一条/下一条」只在未删除的记录间跳转">'
            '<input id="slirn-cut-spk-skipdel" type="checkbox" checked>跳过已删除</label>'
            '<button class="slirn-btn slirn-btn-xs" data-action="cut-spk-prev">⬆️ 上一条</button>'
            '<button class="slirn-btn slirn-btn-xs" data-action="cut-spk-next">⬇️ 下一条</button>'
            '<button class="slirn-btn slirn-btn-xs" data-action="cut-spk-delete">❌ 删除该人员全部记录</button>'
            '<button class="slirn-btn slirn-btn-xs" data-action="cut-spk-recount">🧮 重新统计</button>'
            '<span class="slirn-cut-spk-hint">统计只计未删除记录 — 删除非主讲人员后重算即可确认清零；'
            '删除改判需「💾 保存切分决策」落盘</span></div></div>'
        )
        spk_btn_label = "🔄 重新关联人员ID"
    else:
        spk_bar = '<div id="slirn-cut-spk-bar" class="slirn-cut-spk-bar" style="display:none;"></div>'
        spk_btn_label = "👤 关联人员ID"

    total_in = len(entries)
    stats_line = (
        f"✂️ 切分修剪预览 · 修订输入 {total_in} 条 · 带入 <b>{stats['brought']}</b> 段"
        f"（保留 {stats['kept']} · 更正 {stats['fixed']} · 切分子段 {stats['split_subs']}"
        f"（含删除洞 {stats['split_subs_delete']}））"
        f" · 剔除 {stats['dropped']} 条 · 预计保留时长 {dur_str}{saved_note}"
        f" · 点击子段预播（播到段尾自动停）· 编号继承修订阶段（父编号.子序号）"
    )
    # 已生成过清单 → 主按钮变「重新执行」（按最新决策重算并清除全部手工决策）
    if saved and saved.get("items"):
        main_btn = (f'<button class="slirn-btn slirn-btn-primary" data-action="rebuild-cutlist"'
                    f' data-task-id="{_esc(task_id)}">🔄 重新执行切分修剪</button>')
    else:
        main_btn = (f'<button class="slirn-btn slirn-btn-primary" data-action="build-cutlist"'
                    f' data-task-id="{_esc(task_id)}">✅ 生成切分清单并完成本阶段</button>')

    return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header"><div class="slirn-panel-title">✂️ 处理剪辑 · 第 3 步：切分修剪</div></div>
        <div class="slirn-sub-meta">{stats_line}</div>
        <div class="slirn-form-hint">本阶段把上一阶段确定的字幕决策落到时间段：完整保留与内容更正的整段带入（编号不变）；
        删除的不带入；切分修剪的按「原段内容 vs 切分后文字」对齐，把整段时间轴**完整**切成交替子段
        （保留块 ✅ / 删除洞 ❌，父编号保留，子段依次编号 10.1、10.2、10.3）——点行从该处连续播放
        （播完一条接下一条，播放行高亮跟随，**只播保留内容：删除洞与改判删除的整条自动跳过 = 成片效果**），
        点标记徽章翻转去留；组头「▶ 试听」同样只播本组保留部分；快捷键与字幕修订阶段一致：
        ↑↓ 切换 · 空格 播/停 · R 重播 · K/D/S 整条改判（保留/删除/切分）。</div>
        {fb_hint}{stale_note}
        <div id="slirn-cut-player-wrap" class="slirn-video-wrap slirn-sub-player-wrap" style="display:none;">
            <video id="slirn-cut-player" controls preload="metadata"></video>
        </div>
        {spk_bar}
        <div class="slirn-batch-bar" id="slirn-cut-batch">
          <span class="slirn-batch-label">🧮 批量</span>
          行号 <input class="slirn-batch-num" id="slirn-cut-batch-start" type="number" min="1" step="any"
                 title="支持子段行号，如 10.1">
          ～ <input class="slirn-batch-num" id="slirn-cut-batch-end" type="number" min="1" step="any"
                 title="支持子段行号，如 10.2；区间覆盖父段号时含其全部子段">
          改为 <select class="slirn-batch-sel" id="slirn-cut-batch-sel">
            <option value="keep">✅ 保留</option>
            <option value="delete">❌ 删除</option>
            <option value="">↩️ 维持原状</option>
          </select>
          <button type="button" class="slirn-btn slirn-btn-xs" data-action="cut-batch-apply"
                  title="把区间内行的状态批量改为所选目标（整段行=组级决策，子段行=去留标记；确认后应用，保存前可继续调整）">🧮 批量应用</button>
          <span class="slirn-batch-hint">整段行改组级决策、子段行改去留 — 应用后记得「💾 保存切分决策」</span>
        </div>
        <div class="slirn-batch-bar" id="slirn-cut-search">
          <span class="slirn-batch-label">🔎 搜索</span>
          <input class="slirn-search-q" id="slirn-cut-search-q" type="text"
                 placeholder="输入字幕内容片段定位（回车下一条，Shift+回车上一条）">
          <span class="slirn-search-count" id="slirn-cut-search-count"></span>
          <button type="button" class="slirn-btn slirn-btn-xs" data-action="cut-search-prev">⬆ 上一条</button>
          <button type="button" class="slirn-btn slirn-btn-xs" data-action="cut-search-next">下一条 ⬇</button>
          <span class="slirn-batch-hint">整段行按显示文本与原文匹配，子段行按子段文本匹配（不区分大小写）</span>
        </div>
        <div class="slirn-cut-list" id="slirn-cut-list">{rows}</div>
        <div class="slirn-task-actions" style="margin-top:14px;">
            {main_btn}
            <button class="slirn-btn" id="slirn-cut-save" data-action="save-cut-decisions"
                    data-task-id="{_esc(task_id)}">💾 保存切分决策</button>
            <button class="slirn-btn" data-action="cut-spk-link" data-task-id="{_esc(task_id)}">{spk_btn_label}</button>
            <button class="slirn-btn" data-action="play-cut-video" data-task-id="{_esc(task_id)}">▶️ 播放视频</button>
        </div></div>'''


def _render_rough_compose_zone(task_id: str, t, mgr: TaskManager) -> str:
    """必做阶段 · 粗剪合成（REQ-20260916-016 → REQ-20260918-045 由可选改为必做）。

    根据切分修剪的执行口径（保留区间）用上游 VideoClipper 方法合成一版粗剪
    视频，快速预览切分后的整体效果。不推进任务状态，产物 outputs/rough_compose.mp4
    存在即视为本阶段完成（附随片字幕 rough_compose.srt）。口径与切分修剪面板
    完全一致（修订决策实时 + 已保存的手工翻转/改判），行文本并入热词替换已确认的修正。
    """
    from slirn_home import asr_service, compose_service, cutlist_service, fine_service, revision_service

    outputs_dir = mgr.tasks_dir / task_id / "outputs"

    def _fmt_dur(ms: int) -> str:
        s = ms // 1000
        mm_, ss_ = divmod(s, 60)
        hh_, mm_ = divmod(mm_, 60)
        return f"{hh_}:{mm_:02d}:{ss_:02d}" if hh_ else f"{mm_}:{ss_:02d}"

    def _guide(msg: str, btn: str) -> str:
        return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header"><div class="slirn-panel-title">🎥 粗剪合成</div></div>
        <div class="slirn-empty"><div class="slirn-empty-icon">🚧</div>
            <div class="slirn-empty-text">{_esc(msg)}</div></div>
        <div class="slirn-task-actions" style="margin-top:14px;">{btn}</div></div>'''

    rev = revision_service.load_revision(outputs_dir)
    entries = (rev or {}).get("entries") or []
    if not entries:
        return _guide(
            "需要先完成「字幕修订」— 粗剪合成按切分修剪的保留区间拼接视频",
            '<button class="slirn-btn slirn-btn-primary" data-action="wb-stage" '
            'data-pane="subtitle_review">📝 去字幕修订</button>',
        )
    if not revision_service.all_decided(rev):
        return _guide(
            "字幕修订还有未决策条目 — 切分修剪完成后才能确定保留区间",
            '<button class="slirn-btn slirn-btn-primary" data-action="wb-stage" '
            'data-pane="subtitle_review">📝 去完成决策</button>',
        )
    sub_meta = asr_service.load_subtitle(outputs_dir)
    if not (sub_meta and sub_meta.get("segments")):
        return _guide(
            "缺少字幕生成产物 — 请先在「字幕生成」阶段生成字幕",
            '<button class="slirn-btn slirn-btn-primary" data-action="wb-stage" '
            'data-pane="subtitle">🎙 去生成字幕</button>',
        )

    # 与切分修剪面板同口径：修订实时重建 + 已保存手工翻转/改判 → 执行口径保留区间
    saved = cutlist_service.load_cutlist(outputs_dir)
    cutlist = cutlist_service.build_cutlist(
        sub_meta, rev,
        manual_marks=(saved or {}).get("manual_marks") if saved else None,
        actions=(saved or {}).get("actions") if saved else None,
    )
    units = cutlist_service.effective_keep_units(cutlist)
    intervals_ms = [(int(u["start_ms"]), int(u["end_ms"])) for u in units]
    n_merged = len(compose_service.merge_intervals_ms(intervals_ms))
    keep_ms = sum(int(u["end_ms"]) - int(u["start_ms"]) for u in units)

    artifact = compose_service.rough_compose_path(outputs_dir)
    preview_html = ""
    stale_note = ""
    if artifact.exists():
        stat = artifact.stat()
        dur = compose_service._probe_duration(artifact)  # noqa: SLF001 — 同模块族内使用
        size_mb = round(stat.st_size / 1024 / 1024, 1)
        dur_str = _fmt_dur(int((dur or 0) * 1000)) if dur else "时长未知"
        import time as _time

        mtime = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(stat.st_mtime))
        saved_at = str((saved or {}).get("saved_at") or "")
        rev_at = str((rev or {}).get("saved_at") or "")
        fine_at = str((fine_service.load_fine(outputs_dir) or {}).get("saved_at") or "")
        newer = max(saved_at, rev_at, fine_at)
        if newer and newer > _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(stat.st_mtime)):
            stale_note = ('<div class="slirn-cut-stale">⚠️ 切分决策在合成之后有更新 — '
                          "建议重新合成以预览最新效果</div>")
        preview_html = f'''{stale_note}
        <div class="slirn-video-wrap" style="margin-top:12px;">
            <video id="slirn-rc-player" controls preload="metadata"
                   src="/slirn/api/video/{_esc(task_id)}?src=rough_compose"></video>
        </div>
        <div class="slirn-sub-meta" style="margin-top:8px;">🎞️ 粗剪成片 · {size_mb} MB · {dur_str} · 合成于 {mtime}</div>'''

    stats_line = (
        f"保留单元 {len(units)} 个（合并连续段后 {n_merged} 个切点） · "
        f"预计成片时长 {_fmt_dur(keep_ms)}"
    )
    # ---- REQ-20260916-019：有效字幕 SRT 内嵌（与精剪修订/字幕合成同源） ----
    fine_for_subs = fine_service.load_fine(outputs_dir)
    fmap = {str(e["id"]): e["new_text"]
            for e in (fine_for_subs or {}).get("entries") or []
            if not e.get("reverted")}
    subs_srt = compose_service.format_srt(units, fmap)
    subs_block = ""
    if subs_srt:
        # <pre> 内 textContent 由前端 escapeHtml 处理；这里直接拼字符串
        # （format_srt 输出安全字符：换行 + HH:MM:SS,mmm + 行文本 — 字幕文本
        # 是字幕修订/热词替换产物，前端落入 <pre> 时已用 escapeHtml 转义）
        subs_escaped = subs_srt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        subs_block = f'''<details class="slirn-rc-subs" open style="margin-top:14px;">
        <summary>📝 切分之后的有效字幕（{len(units)} 行 · {_fmt_dur(keep_ms)}）</summary>
        <pre class="slirn-rc-subs-body">{subs_escaped}</pre>
        <div class="slirn-task-actions" style="margin-top:10px;">
            <button class="slirn-btn slirn-btn-sm" data-action="compose-rough-subs-copy"
                    data-task-id="{_esc(task_id)}">📋 复制 SRT</button>
            <button class="slirn-btn slirn-btn-sm" data-action="compose-rough-subs-download"
                    data-task-id="{_esc(task_id)}">⬇️ 下载 SRT</button>
        </div>
    </details>'''
    else:
        subs_block = '<div class="slirn-sub-meta" style="margin-top:14px;">📝 没有保留内容 — 字幕清单为空</div>'

    # ---- REQ-20260916-019：删除按钮（仅产物存在时） ----
    delete_btn = ""
    if artifact.exists():
        delete_btn = (
            f'<button class="slirn-btn slirn-btn-danger" id="slirn-rc-delete" '
            f'data-action="compose-rough-delete" data-task-id="{_esc(task_id)}" '
            f'style="margin-left:8px;">🗑️ 删除粗剪成片</button>'
        )

    return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header"><div class="slirn-panel-title">🎥 粗剪合成</div></div>
        <div class="slirn-sub-meta">{stats_line}</div>
        <div class="slirn-form-hint">用「处理之后的字幕 + 原视频」走上游 FunClip 的合成方法（VideoClipper）
        把保留区间拼接成一版粗剪成片，快速预览切分后的整体效果，并附随片字幕 rough_compose.srt。
        本步骤为<b>必做</b>阶段 — 成片交付前需在此完成合成；口径与切分修剪面板一致（修订决策实时并入，
        手工翻转/整条改判以「💾 保存切分决策」之后的为准），行文本含热词替换已确认的修正。
        合成需重新编码：44 分钟源实测约 11 分钟，请在后台合成期间继续其它操作。</div>
        <div class="slirn-task-actions" style="margin-top:14px;">
            <button class="slirn-btn slirn-btn-primary" id="slirn-rc-compose" data-action="compose-rough"
                    data-task-id="{_esc(task_id)}">🎬 {'重新合成粗剪视频' if artifact.exists() else '合成粗剪视频'}</button>
            {delete_btn}
        </div>
        <div id="slirn-rc-status" class="slirn-status-msg" style="display:none;"></div>
        {subs_block}
        {preview_html}
    </div>'''


def _extract_opt_rows(zone_html: str, seg_id: int) -> str:
    """从 _render_optimize_zone 的 HTML 里抽出指定 seg 的行组（父行 + 子段行）。

    2026-09-29 用户反馈：行内切分「✂️ 按内容切分」后整页刷新会把用户踢出当前
    优化字幕工作环境（滚动/词筛选/焦点全丢）。/optimize_resplit 与
    /optimize_unsplit 成功后重渲 zone、用注释标记抽出该 seg 的新行组返回，
    前端只换本行组（见 router.js optSwapRows）。抽不到返回空串（前端兜底整刷）。
    """
    open_m = f"<!--opt-rows:{int(seg_id)}-->"
    close_m = f"<!--/opt-rows:{int(seg_id)}-->"
    i = zone_html.find(open_m)
    if i < 0:
        return ""
    i += len(open_m)
    j = zone_html.find(close_m, i)
    return zone_html[i:j] if j >= 0 else ""


def _opt_words_panel(data: dict) -> tuple[str, str]:
    """REQ-038 不明确字词频次面板（词列表 + 状态过滤条）— (words_html,
    word_filter_html)。REQ-20260929-NNN 抽成模块级：批量替换注入出现项后
    端点要重渲词频面板（新词 chip 可点选筛选），不能只在 zone 渲染里拼。
    词身份取分析时的建议替换值（occ.after 落盘值）；每词带「处理完成」标识
    （全部出现处都已明确处理 = 采纳/不采纳/编辑过）。
    """
    word_agg: dict[str, dict] = {}
    for o in data.get("occurrences") or []:
        w = str(o.get("after") or "")
        if not w:
            continue
        r = word_agg.setdefault(w, {"total": 0, "done": 0})
        r["total"] += 1
        if o.get("reviewed"):
            r["done"] += 1
    words_html = "".join(
        f'<div class="slirn-opt-word" data-word="{_esc(w)}" data-done="{1 if r["done"] >= r["total"] else 0}">'
        f'<button class="slirn-opt-chip" data-action="opt-word" data-word="{_esc(w)}" '
        f'title="点按筛选该词的出现行，再点取消">{_esc(w)}<b>×{r["total"]}</b></button>'
        f'<span class="slirn-opt-word-prog">{r["done"]}/{r["total"]}</span>'
        f'<span class="slirn-opt-word-badge{" done" if r["done"] >= r["total"] else ""}"'
        f' title="{"该词全部出现处都已明确处理（采纳或不采纳）" if r["done"] >= r["total"] else "还有 " + str(r["total"] - r["done"]) + " 处未处理 — 逐处切换 ✓/✕ 或编辑替换值即计为已处理"}">'
        f'{"✅ 已完成" if r["done"] >= r["total"] else "⬜ 未完成"}</span></div>'
        for w, r in sorted(word_agg.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    ) or '<span class="slirn-sub-meta">没有生效的替换（可重新优化或直接保存）</span>'
    word_filter_html = (
        '<div class="slirn-opt-word-filters" id="slirn-opt-word-filters">'
        '<button class="slirn-btn slirn-btn-xs active" data-action="opt-word-filter" data-mode="all">全部</button>'
        '<button class="slirn-btn slirn-btn-xs" data-action="opt-word-filter" data-mode="todo">⬜ 未完成</button>'
        '<button class="slirn-btn slirn-btn-xs" data-action="opt-word-filter" data-mode="done">✅ 已完成</button>'
        # REQ-20260918-057B：文字输入过滤（与状态过滤 AND 组合）
        '<span class="slirn-opt-word-text-filter">'
        '<input type="text" id="slirn-opt-word-text" placeholder="🔍 输入词文本过滤" autocomplete="off">'
        '<button type="button" class="slirn-opt-word-text-clear" data-action="opt-word-text-clear" '
        'title="清空过滤" style="display:none;">✕</button>'
        '</span>'
        '</div>'
    ) if word_agg else ""
    return words_html, word_filter_html


def _render_optimize_zone(task_id: str, t, mgr: TaskManager) -> str:
    """处理剪辑 · 优化字幕（REQ-20260917-030）。

    粗剪成片 + 任务热词 → ASR 重识别（只识别字幕，不区分说话人）→ 大模型
    提取不明确字/词（附替换建议与理由）→ 人工逐处分辨（采纳/不采纳/改替换值）
    → 保存替换对应关系 + 优化后字幕（SRT 可导出）。
    """
    from slirn_home import compose_service, optimize_service

    outputs_dir = mgr.tasks_dir / task_id / "outputs"
    artifact = compose_service.rough_compose_path(outputs_dir)

    def _guide(msg: str, btn: str) -> str:
        return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slir-panel-header"><div class="slirn-panel-title">✨ 优化字幕 · 不明确字词</div></div>
        <div class="slirn-empty"><div class="slirn-empty-icon">🚧</div>
            <div class="slirn-empty-text">{_esc(msg)}</div></div>
        <div class="slirn-task-actions" style="margin-top:14px;">{btn}</div></div>'''

    if not artifact.exists():
        return _guide(
            "需要先合成粗剪成片 — 优化字幕以成片为准重新识别字幕",
            '<button class="slirn-btn slirn-btn-primary" data-action="wb-stage" '
            'data-pane="rough_compose">🎥 去粗剪合成</button>',
        )

    hotwords: list[str] = []
    try:
        if t.hotwords_path.exists():
            hotwords = [w for w in t.hotwords_path.read_text(encoding="utf-8").split() if w]
    except Exception:  # noqa: BLE001
        pass

    data = optimize_service.load_optimize(outputs_dir)
    job = optimize_service.job_status(task_id)
    job_state = job.get("state") if job else ("done" if data else "idle")
    hw_chips = "".join(f'<span class="slirn-hw-chip">{_esc(w)}</span>' for w in hotwords)

    # ---- 状态 A：未优化 → 说明 + 热词 + 开始按钮 ----
    if not data:
        from slirn_home import llm_config
        if llm_config.get_current_entry(mgr.tasks_dir.parent) is None:
            return _guide("未注册任何大模型 — 请先点顶栏「大模型设置」 添加模型", "")
        try:
            size_mb = round(artifact.stat().st_size / 1024 / 1024, 1)
            art_disp = f"（{size_mb} MB）"
        except OSError:
            art_disp = ""
        hw_block = (f'<div class="slirn-form-hint" style="margin-top:10px;">'
                    f"<b>任务热词</b>（{len(hotwords)} 个，识别偏置 + 分析参照）：</div>"
                    f'<div class="slirn-hw-chips">{hw_chips}</div>' if hotwords
                    else '<div class="slirn-form-hint" style="margin-top:10px;">'
                         "⚠️ 任务没有热词 — 仍可分析（建议先在「素材准备」补充，"
                         '专有名词/术语的识别和判定会更准）</div>')
        return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header"><div class="slirn-panel-title">✨ 优化字幕 · 不明确字词</div></div>
        <div class="slirn-form-hint">对粗剪成片 <b>{_esc(artifact.name)}</b>{_esc(art_disp)} 的字幕做语义分析：
        行集<b>优先直读合成时随片生成的字幕</b>（与成片同源，秒级就绪），随片字幕缺失或对不上时
        回落 ASR 重新识别（只识别文字，不区分说话人）；先做<b>断句整理</b>（合并过碎的行，
        一行 10~20 字、一句完整的话或逗号连接的两小句、行尾无标点），再用大模型提取<b>不明确的字/词</b>
        （疑似误识别、语义不通、与热词矛盾），统计出现次数并定位位置。替换值可逐处编辑，
        人工分辨确认后保存替换对应关系，并可导出优化后的成片字幕 SRT。</div>
        {hw_block}
        <div class="slirn-task-actions" style="margin-top:14px;">
            <button class="slirn-btn slirn-btn-primary" data-action="optimize-start"
                    data-task-id="{_esc(task_id)}">✨ 开始优化字幕</button>
        </div>
        <div id="slirn-opt-status" class="slirn-status-msg" style="display:none;"
             data-task-id="{_esc(task_id)}" data-state="{_esc(job_state)}"></div>
    </div>'''

    # ---- 状态 B：优化结果 → 词频 + 行内出现项（可编辑/采纳）+ 确认保存 ----
    import time as _time
    est = optimize_service.effective_stats(data)
    occs_by_seg: dict[str, list[dict]] = {}
    for o in data.get("occurrences") or []:
        occs_by_seg.setdefault(str(o.get("seg")), []).append(o)
    # REQ-20260922-NNN 整句替换：行 → 整句文本（跳过该行局部替换）
    edits_by_seg: dict[str, str] = {}
    for e in data.get("line_edits") or []:
        if isinstance(e, dict) and e.get("text"):
            edits_by_seg[str(e.get("seg"))] = str(e["text"])
    # REQ-20260923-NNN 行内切分：切分行 → 子段列表 + 生效删除索引 + 重切预填
    splits_by_seg: dict[str, list[dict]] = {}
    for k, subs in (data.get("line_splits") or {}).items():
        try:
            key = str(int(k))
        except (TypeError, ValueError):
            continue
        if isinstance(subs, list) and subs:
            splits_by_seg[key] = subs
    smarks_by_seg = {rid: optimize_service.deleted_sub_indexes(
        rid, subs, data.get("split_marks")) for rid, subs in splits_by_seg.items()}
    targets_by_seg = {str(k): str(v) for k, v in (data.get("split_targets") or {}).items()}
    # REQ-20260922-NNN 标记删除行 + REQ-20260923-NNN 删除子段：剪辑计划 + 状态条
    marks_set = {str(m) for m in data.get("line_marks") or []}
    cut_job = compose_service.opt_cut_job_status(task_id)
    cut_plan_now = optimize_service.cut_plan(data)
    cut_ready = compose_service.optimize_cut_ready(outputs_dir, cut_plan_now)
    cut_has_del = optimize_service.plan_has_deletions(cut_plan_now)
    cut_state, cut_text = "", ""
    if cut_job and cut_job.get("state") == "running":
        cut_state = "running"
        cut_text = (f'⏳ 视频重新拼接中 · {cut_job.get("progress") or 0:.0f}% · '
                    f'{cut_job.get("stage") or ""} — 完成后精剪合成自动使用新视频')
    elif cut_job and cut_job.get("state") == "error":
        cut_state = "error"
        cut_text = (f'⚠️ 视频重新拼接失败：{cut_job.get("error") or "未知错误"}'
                    ' — 点「🎬 重新拼接视频」可再次触发')
    elif cut_has_del and cut_ready is not None:
        cut_state = "done"
        _n_subs = int(cut_ready.get("split_subs") or 0)
        _what = (f'{len(cut_ready.get("marks") or [])} 行'
                 + (f' + {_n_subs} 子段' if _n_subs else ''))
        cut_text = (f'🎬 优化成片已生成：剪除 {_what} · '
                    f'删 {cut_ready.get("deleted_sec", "?")}s · 保留 {cut_ready.get("kept_sec", "?")}s'
                    ' — 精剪合成将自动使用 optimize_compose.mp4')
    elif cut_has_del:
        cut_state = "pending"
        cut_text = ("⚠️ 有要剪除的内容（标记删除行/删除子段）但优化成片未生成 — "
                    "点下方「🎬 重新拼接视频」按钮触发剪辑（保存不会自动剪辑）")
    cut_bar = (f'<div id="slirn-opt-cut-status" class="slirn-status-msg slirn-opt-cut-status" '
               f'data-task-id="{_esc(task_id)}" data-state="{_esc(cut_state)}">{_esc(cut_text)}</div>'
               ) if cut_state else ""
    confirmed = bool(data.get("saved_at"))
    running_html = ""
    if job_state == "running":
        elapsed = int((job.get("finished_at") or _time.time()) - job.get("started_at", _time.time()))
        running_html = (f'⏳ {_esc(job.get("stage") or "处理中")} · '
                        f"{job.get('progress') or 0:.0f}% · 已耗时 {elapsed}s")

    stale_note = ""
    created = str(data.get("created_at") or "")
    try:
        mtime = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(artifact.stat().st_mtime))
    except OSError:
        mtime = ""
    if mtime and created and mtime > created:
        stale_note = ('<div class="slirn-cut-stale">⚠️ 粗剪成片在优化之后重新合成 — '
                      "建议重新优化以覆盖最新成片</div>")

    # REQ-038：不明确字词频次列表 — 按替换目标词分组（全部出现项，含未采纳），
    # 每词带「处理完成」标识（该词全部出现处都已明确处理 = 采纳/不采纳/编辑过），
    # 可按标识过滤。REQ-20260929-NNN 抽成 _opt_words_panel（批量替换后端点复用）。
    words_html, word_filter_html = _opt_words_panel(data)

    def _line_html(seg: dict) -> str:
        rid = str(seg.get("i"))
        occs = sorted(occs_by_seg.get(rid, []), key=lambda o: int(o.get("pos", 0)))
        # REQ-032 修订：替换栏放右侧独立列后，正文流不再插入 chip（避免重复显示），
        # 整段原文连续渲染，删除线 + 箭头 + 输入框全部在替换栏。
        raw = str(seg.get("text", ""))
        # REQ-20260922-NNN 整句替换：full_edit 行整行改写（覆盖本行局部替换）
        full_edit = edits_by_seg.get(rid)
        eff_text = str(seg.get("new_text") or raw)  # 保存后回显的最终文本（预填用）
        # REQ-20260922-NNN 标记删除行：🗑️ 标记后保存 → 本行时间段从粗剪成片剪除
        deleted = rid in marks_set
        parts = [_esc(raw)]
        if deleted:
            parts.insert(0, '<span class="slirn-opt-line-badge del">🗑️ 已标记删除</span>')
        if full_edit is not None:
            parts = [
                '<span class="slirn-opt-line-badge">✏️ 整句替换</span>',
                f'<span class="slirn-opt-line-new">{_esc(full_edit)}</span>',
                f'<span class="slirn-opt-line-orig" title="原文">{_esc(raw)}</span>',
            ]
            if deleted:
                parts.insert(0, '<span class="slirn-opt-line-badge del">🗑️ 已标记删除</span>')
        occ_chips = []
        for o in occs:
            applied = 1 if o.get("applied", True) else 0
            occ_id = int(o["occ_id"])
            before_txt = _esc(str(o["before"]))
            after_txt = _esc(str(o["after"]))
            reason_txt = _esc(str(o.get("reason") or "不明确片段"))
            toggle_title = _esc("已采纳（保存时替换）" if applied else "已不采纳（保留原文）")
            toggle_mark = "✓" if applied else "✕"
            overridden = " overridden" if full_edit is not None else ""
            occ_chips.append(
                f'<span class="slirn-opt-occ{overridden}" data-occ="{occ_id}" data-applied="{applied}"'
                f' data-word="{after_txt}" data-reviewed="{1 if o.get("reviewed") else 0}">'
                f'<s class="slirn-opt-before" title="{reason_txt}">{before_txt}</s>'
                f'<span class="slirn-opt-arrow">→</span>'
                f'<input class="slirn-opt-after" value="{after_txt}">'
                f'<button class="slirn-btn slirn-btn-xs slirn-opt-toggle" data-action="opt-occ-toggle" '
                f'data-occ="{occ_id}" title="{toggle_title}">{toggle_mark}</button></span>'
            )
        if occ_chips and full_edit is not None:
            occ_chips.append('<span class="slirn-opt-occ-override-hint">整句替换已覆盖本行局部替换</span>')
        row_words = sorted({str(o["after"]) for o in occs if o.get("applied", True)})
        has_occ = " has-occ" if occs else ""
        # 2026-09-29 用户反馈：处理完的行保持绿色区分（防重复处理）。口径与词行
        # ✅ 已完成一致：本行全部出现处都已 reviewed（采纳或不采纳都算）。
        # 前端 optOccMarkReviewed 用同一口径实时传播（回车/✓✕ 后即时变绿）。
        done_cls = " occ-done" if occs and all(bool(o.get("reviewed")) for o in occs) else ""
        full_cls = " full-edit" if full_edit is not None else ""
        occ_col = f'<div class="slirn-opt-col">{"".join(occ_chips)}</div>' if occ_chips else ''
        edit_btn = (
            f'<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" data-action="opt-line-edit" '
            f'title="整句替换：直接改写整行文本，覆盖本行局部替换">✏️</button>'
            if full_edit is None else
            f'<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" data-action="opt-line-edit" '
            f'title="取消整句替换，恢复局部替换">↩️</button>'
        )
        del_btn = (
            f'<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn slirn-opt-del-btn" '
            f'data-action="opt-line-delete" data-deleted="1" '
            f'title="取消删除标记：本行恢复保留">✚</button>'
            if deleted else
            f'<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn slirn-opt-del-btn" '
            f'data-action="opt-line-delete" data-deleted="0" '
            f'title="标记删除：点「🎬 重新拼接视频」后本行时间段将从粗剪成片剪除，字幕时间轴自动前移">🗑️</button>'
        )
        del_row_cls = " line-deleted" if deleted else ""
        # REQ-20260923-NNN 行内切分：未切分行也给 ✂️（首切口）
        split_btn = (
            f'<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" '
            f'data-action="opt-line-resplit" '
            f'title="行内切分：填「切分后内容」（保留想留的文字，对不上的字自动落进删除洞；'
            f'子段可单独删除）">✂️</button>'
        )
        # REQ-20260923-NNN 行内切分：切分行 → ✂️ 重切 / ↩️ 取消切分（🗑️/✏️ 让位 —
        # 切分定义了该行的命运，与整行标记/整句替换互斥），子段平铺在父行下方
        subs = splits_by_seg.get(rid)
        if subs is not None:
            del_idx = set(smarks_by_seg.get(rid) or [])
            n_del = len(del_idx)
            split_btns = (
                f'<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" '
                f'data-action="opt-line-resplit" '
                f'title="重新切分：修改「切分后内容」（多写少写都行，对不上的字自动落进删除洞）">✂️</button>'
                f'<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn" '
                f'data-action="opt-line-unsplit" '
                f'title="取消切分：本行恢复整行（子段标记一并清除）">↩️</button>'
            )
            parts = [f'<span class="slirn-opt-line-badge split">✂️ 已切分 · '
                     f'{len(subs)} 段（删 {n_del}）</span>', _esc(raw)]  # 整句替换在切分行上忽略
            parent = (f'<div class="slirn-opt-row{has_occ}{done_cls}{del_row_cls}" data-id="{_esc(rid)}" '
                      f'data-split="1" data-split-target="{_esc(targets_by_seg.get(rid) or "")}" '
                      f'data-start-ms="{int(seg.get("start_ms", 0))}" '
                      f'data-end-ms="{int(seg.get("end_ms", 0))}"'
                      f' data-words="{_esc(chr(10).join(row_words))}"'
                      f' data-orig-text="{_esc(raw)}"'
                      f' data-full-edit="0" data-full-text=""'
                      f' data-deleted="0">'
                      f'<span class="slirn-sub-idx">{_esc(rid)}</span>'
                      f'<span class="slirn-fw-time">{_esc(str(seg.get("start") or ""))}</span>'
                      f'<div class="slirn-fw-text">{split_btns}{"".join(parts)}</div>'
                      f'{occ_col}</div>')
            sub_rows = []
            for i, sub in enumerate(subs):
                smark = "delete" if i in del_idx else "keep"
                fb = ('<span class="slirn-opt-subfb" '
                      'title="无字级时间戳 — 该子段时间段按文本占比估算（⚠️ 仅供参考）">⚠️</span>'
                      if sub.get("fallback") else "")
                toggle = (
                    f'<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn slirn-opt-submark" '
                    f'data-action="opt-sub-toggle" data-parent="{_esc(rid)}" data-sub-idx="{i}" '
                    f'data-mark="delete" title="恢复保留该子段">✚</button>'
                    if smark == "delete" else
                    f'<button class="slirn-btn slirn-btn-xs slirn-opt-line-btn slirn-opt-submark" '
                    f'data-action="opt-sub-toggle" data-parent="{_esc(rid)}" data-sub-idx="{i}" '
                    f'data-mark="keep" title="标记删除该子段：重新拼接时从成片剪除该时间段">🗑️</button>'
                )
                sub_del_cls = " line-deleted" if smark == "delete" else ""
                sub_rows.append(
                    f'<div class="slirn-opt-row slirn-opt-subrow{has_occ}{sub_del_cls}"'
                    f' data-parent="{_esc(rid)}" data-sub-idx="{i}" data-mark="{smark}"'
                    f' data-start-ms="{int(sub.get("start_ms", 0))}"'
                    f' data-end-ms="{int(sub.get("end_ms", 0))}">'
                    f'<span class="slirn-sub-idx">{_esc(rid)}.{i + 1}</span>'
                    f'<span class="slirn-fw-time">{_esc(str(sub.get("start") or ""))}</span>'
                    f'<div class="slirn-fw-text">{toggle}{fb}'
                    f'<span class="slirn-opt-subtext">{_esc(str(sub.get("text") or ""))}</span></div>'
                    f'</div>')
            return parent + "".join(sub_rows)
        # REQ-20260918-054：补 data-end-ms，前端 timeupdate 按 [start,end) 命中行
        return (f'<div class="slirn-opt-row{has_occ}{full_cls}{done_cls}{del_row_cls}" data-id="{_esc(rid)}" '
                f'data-start-ms="{int(seg.get("start_ms", 0))}" '
                f'data-end-ms="{int(seg.get("end_ms", 0))}"'
                f' data-words="{_esc(chr(10).join(row_words))}"'
                f' data-orig-text="{_esc(raw)}" data-eff-text="{_esc(eff_text)}"'
                f' data-full-edit="{1 if full_edit is not None else 0}"'
                f' data-full-text="{_esc(full_edit or "")}"'
                f' data-deleted="{1 if deleted else 0}">'
                f'<span class="slirn-sub-idx">{_esc(rid)}</span>'
                f'<span class="slirn-fw-time">{_esc(str(seg.get("start") or ""))}</span>'
                f'<div class="slirn-fw-text">{del_btn}{split_btn}{edit_btn}{"".join(parts)}</div>'
                f'{occ_col}</div>')

    # 2026-09-29 用户反馈：切分/取消切分只做行组局部刷新（不再 openWorkbench 整刷）。
    # 每个 seg 的行组（父行 + 子段行）用 HTML 注释标记包起来 — 注释节点不参与
    # :last-child / previousElementSibling 等元素语义，_extract_opt_rows 靠它精确抽取。
    rows = "".join(
        f"<!--opt-rows:{_esc(str(s.get('i')))}-->" + _line_html(s)
        + f"<!--/opt-rows:{_esc(str(s.get('i')))}-->"
        for s in (data.get("segments") or [])
    )
    save_label = "💾 确认替换并保存" if not confirmed else "✅ 已确认 · 再次保存"
    n_occ_rows = len(occs_by_seg)
    n_lines = est["lines"]
    n_edits = int(est.get("line_edits") or 0)
    edit_stat = f" · 整句替换 <b>{n_edits}</b> 行" if n_edits else ""
    _mg = data.get("merge") or {}
    merge_stat = (f" · 断句整理 {_mg.get('before')}→{_mg.get('after')} 行"
                  if _mg.get("before") and _mg.get("after") else "")
    n_marks = int(est.get("line_marks") or 0)
    # 删除统计：行数 + 总时长（REQ-20260922-NNN：统计被删除的记录数和总时长）
    if n_marks:
        del_ms = sum(max(0, int(s.get("end_ms") or 0) - int(s.get("start_ms") or 0))
                     for s in (data.get("segments") or [])
                     if str(s.get("i")) in marks_set)
        mark_stat = f" · 标记删除 <b>{n_marks}</b> 行 · 共 {del_ms / 1000:.1f}s"
    else:
        mark_stat = ""
    # REQ-20260923-NNN 行内切分统计：切分行数 / 子段数（含删除子段数与总时长）
    n_split_lines = int(est.get("line_splits") or 0)
    n_del_subs = int(est.get("split_subs_delete") or 0)
    if n_split_lines:
        sub_del_ms = sum(
            max(0, int(sub.get("end_ms") or 0) - int(sub.get("start_ms") or 0))
            for rid, idx in smarks_by_seg.items() for i in idx
            if i < len(splits_by_seg.get(rid) or [])
            for sub in [splits_by_seg[rid][i]])
        split_stat = (f" · 切分 <b>{n_split_lines}</b> 行 / <b>{est.get('split_subs')}</b> 段"
                      f"（删 <b>{n_del_subs}</b> 段 · {sub_del_ms / 1000:.1f}s）")
    else:
        split_stat = ""
    model_disp = _esc(data.get("model") or "")
    # REQ-20260923-NNN 查看最终视频：优化成片就绪才渲染 — 同时是前端判断
    # 「成片时间基可用」的 DOM 信号（cut_ready 已含「与当前剪辑计划一致」判据）。
    # SRT 下载/预览统一走「📄 查看最终字幕」弹窗（内含粗剪/成片时间基切换 +
    # 下载）— 原两枚 ⬇️ 下载按钮与查看同打 /optimized_srt，操作行去重移除。
    cut_video_btn = (
        f'<button class="slirn-btn" data-action="opt-final-video" data-task-id="{_esc(task_id)}" '
        f'title="播放重新拼接后的优化成片（已剪除标记删除行/子段，时间轴已前移；'
        f'此模式下行高亮/跳播暂停 — 点任意字幕行恢复粗剪时间基定位播放）">▶️ 查看最终视频</button>'
        if cut_ready is not None else ""
    )
    # REQ-20260922-NNN 查找被删除的字幕：只看已删除行（有删除内容才渲染 —
    # REQ-20260923-NNN 计数并入删除子段）
    n_del_total = n_marks + n_del_subs
    del_filter_btn = (
        f'<button class="slirn-btn" data-action="opt-filter-deleted" data-shown="0" '
        f'data-all-text="🗑️ 只看已删除的（{n_del_total}）">🗑️ 只看已删除的（{n_del_total}）</button>'
        if n_del_total else ""
    )
    # REQ-20260923-NNN 行搜索：字幕行文本筛选（纯前端 — 原文/生效文本/整句替换/
    # 替换值/子段文本全量匹配，与上面两个筛选按钮 CSS 类叠加 = 交集）
    search_box = (
        '<span class="slirn-opt-row-search">'
        '<input type="text" id="slirn-opt-search" placeholder="🔍 搜索字幕文本…" '
        'autocomplete="off" title="匹配范围：原文 + 生效文本 + 整句替换 + 替换值 + 切分子段'
        '（不区分大小写）；Enter 跳到第一条命中行，Esc 清空">'
        '<button type="button" class="slirn-opt-search-clear" data-action="opt-search-clear" '
        'style="display:none" title="清空搜索">✕</button>'
        '<span id="slirn-opt-search-count" class="slirn-opt-search-count"></span>'
        '</span>'
    )
    # REQ-20260929-NNN 批量替换：搜指定词 → 每处命中展示前两行 + 本行 + 后两行
    # 共 5 行上下文 → 输入替换文字 → 确认一次替换全部（不可替换的行标注跳过原因）。
    batch_btn = (
        '<button class="slirn-btn" data-action="opt-batch-open" '
        'title="搜指定词，预览每处命中的前后各两行上下文，确认后一次替换全部">'
        '🔁 批量替换</button>'
    )
    batch_panel = (
        '<div class="slirn-opt-batchrep" id="slirn-opt-batchrep" style="display:none;">'
        '<div class="slirn-opt-batchrep-bar">'
        '<input type="text" id="slirn-opt-batchrep-word" '
        'placeholder="要查找的词（不区分大小写）" autocomplete="off">'
        '<button type="button" class="slirn-btn slirn-btn-xs" id="slirn-opt-batchrep-search-btn" '
        'data-action="opt-batch-search">🔍 查找</button>'
        '<button type="button" class="slirn-opt-batchrep-close" data-action="opt-batch-close" '
        'title="收起批量替换面板">✕</button>'
        '</div>'
        '<div class="slirn-opt-batchrep-result" id="slirn-opt-batchrep-result"></div>'
        '<div class="slirn-opt-batchrep-apply" id="slirn-opt-batchrep-apply" style="display:none;">'
        '<input type="text" id="slirn-opt-batchrep-to" placeholder="替换为…（输入后点确认）" '
        'autocomplete="off">'
        '<button type="button" class="slirn-btn slirn-btn-primary slirn-btn-xs" '
        'id="slirn-opt-batchrep-go" data-action="opt-batch-replace">✅ 确认批量替换</button>'
        '</div></div>'
    )
    return f'''<div class="slirn-card" style="margin-top:16px;">
        <div class="slirn-panel-header"><div class="slirn-panel-title">✨ 优化字幕 · 不明确字词</div></div>
        <div class="slirn-sub-meta">识别 {n_lines} 行{merge_stat} · 不明确 {est["occurrences"]} 处（{est["words"]} 个词）
        · 生效替换 <b>{est["applied"]}</b> 处 · 未采纳 {est["skipped"]} 处{edit_stat}{mark_stat}{split_stat} · 模型 {model_disp}
        {" · ✅ 已确认" if confirmed else ""}</div>
        {stale_note}
        {cut_bar}
        <div class="slirn-form-hint" style="margin-top:10px;"><b>不明确字词频次</b>（按替换目标词分组；
        出现处切换过 ✓/✕ 或编辑过替换值即计「已处理」，全部处理完 → ✅）：</div>
        {word_filter_html}
        <div class="slirn-fw-stats" id="slirn-opt-words">{words_html}</div>
        <div class="slirn-form-hint">行内 <s class="slirn-opt-before">删除线</s> = 疑似误识别原文，旁边输入框 = 替换值（可直接编辑）；
        <b>✓</b> 采纳 / <b>✕</b> 不采纳（逐处切换）。点词筛选出现行，点行按成片时间跳播核对。
        行首 <b>✏️</b> = 整句替换（直接改写整行文本，覆盖本行局部替换）；
        <b>🗑️</b> = 标记删除整行（点「🎬 重新拼接视频」后从成片剪除该行时间段并前移后续字幕；
        播放时也会自动跳过已删除的片段 = 成片效果预览）；
        <b>✂️</b> = 行内切分（填「切分后内容」，对不上的字自动落进删除洞；子段可单独 🗑️/✚，
        重新拼接时按子段剪除并前移）。</div>
        <div class="slirn-opt-kbhint" id="slirn-opt-kbhint"></div>
        <div class="slirn-task-actions" style="margin-top:10px;">
            <button class="slirn-btn" data-action="opt-filter" data-shown="1"
                    data-all-text="🔍 只看有不明确字词的行（{n_occ_rows}/{n_lines}）">🔍 只看有不明确字词的行（{n_occ_rows}/{n_lines}）</button>
            {del_filter_btn}
            {batch_btn}
            {search_box}
        </div>
        {batch_panel}
        <div id="slirn-opt-player-wrap" class="slirn-video-wrap slirn-sub-player-wrap" style="display:none;">
            <video id="slirn-opt-player" controls preload="metadata"></video>
        </div>
        <div class="slirn-fw-list slirn-opt-list" id="slirn-opt-list">{rows}</div>
        <div class="slirn-task-actions" style="margin-top:14px;">
            <button class="slirn-btn slirn-btn-primary" data-action="save-optimize"
                    data-task-id="{_esc(task_id)}">{save_label}</button>
            <button class="slirn-btn" data-action="opt-final-view" data-task-id="{_esc(task_id)}"
                    title="预览/下载最终字幕（应用替换 + 剔除删除内容；弹窗内可切换粗剪/优化成片时间基并下载）"
                    {"" if confirmed else 'disabled title="确认保存后可查看"'}>📄 查看最终字幕</button>
            <button class="slirn-btn" data-action="opt-cut-rekick" data-task-id="{_esc(task_id)}"
                    title="按当前剪辑计划（🗑️ 标记删除行 + 切分删除子段），对粗剪成片做剪除+拼接，生成 optimize_compose.mp4（精剪合成自动优先使用）">🎬 重新拼接视频</button>
            {cut_video_btn}
            <button class="slirn-btn" data-action="opt-resplice" data-task-id="{_esc(task_id)}"
                    title="按当前剪辑计划即时重算拼接字幕（optimize_compose.srt）：删除内容剔除 + 时间轴前移，不编码视频">🔄 重新拼接字幕</button>
            <button class="slirn-btn" data-action="optimize-start" data-task-id="{_esc(task_id)}"
                    data-has="1"
                    title="放弃当前结果重新分析（优先直读随片字幕，缺失时识别成片）：覆盖现有结果、重置全部采纳/删除/切分决定，并清除旧优化成片 — 需二次确认">♻️ 重新优化字幕</button>
        </div>
        <div id="slirn-opt-status" class="slirn-status-msg" style="{'display:none;' if job_state != 'running' else ''};"
             data-task-id="{_esc(task_id)}" data-state="{_esc(job_state)}">{running_html}</div>
    </div>'''


def _task_echo_fragments(t) -> tuple[str, str, str]:
    """任务回显片段（REQ-20260915-002）— 详情页 / 工作台共用。

    Returns:
        (video_disp, seg_disp, hotwords_disp) 三个 HTML 片段。
    """
    # ---- 原视频：格式对齐新建步骤1 的文件信息条「📁 name（size MB · duration）」 ----
    src = t.original_video_source
    if src.exists():
        from tasklib.video import get_video_duration
        dur = get_video_duration(src)
        if dur:
            mm_, ss_ = divmod(int(dur), 60)
            hh_, mm_ = divmod(mm_, 60)
            dur_str = f"{hh_:02d}:{mm_:02d}:{ss_:02d}"
        else:
            dur_str = "时长未知"
        size_mb = round(src.stat().st_size / 1024 / 1024, 1)
        video_disp = f"📁 {_esc(src.name)}（{size_mb} MB · {dur_str}）"
    else:
        video_disp = f"📁 {_esc(src.name)}（⚠️ 文件缺失）"

    # ---- 截取段：原样起止时间 + 时长 + 大小（对齐新建「✅ 待剪辑视频已生成 N MB」）----
    if t.segment:
        seg_dur = "时长未知"
        try:
            from tasklib.time_utils import parse_time
            _ds = (parse_time(t.segment.end) - parse_time(t.segment.start)).total_seconds()
            if _ds > 0:
                seg_dur = f"{int(_ds // 3600):02d}:{int(_ds % 3600 // 60):02d}:{int(_ds % 60):02d}"
        except ValueError:
            pass
        if t.segment.path.exists():
            seg_mb = round(t.segment.path.stat().st_size / 1024 / 1024, 1)
            seg_disp = f"⏱ {_esc(t.segment.start)} → {_esc(t.segment.end)}（{seg_dur} · {seg_mb} MB）"
        else:
            seg_disp = f"⏱ {_esc(t.segment.start)} → {_esc(t.segment.end)}（{seg_dur} · ⚠️ 截取文件缺失）"
    else:
        seg_disp = "未截取 · 使用完整原视频"

    # ---- 热词：chips 全量回显（带来源标签，与新建步骤3 显示方式一致；只读无 ✕）----
    hw_words: list[str] = []
    try:
        if t.hotwords_path.exists():
            hw_words = [w for w in t.hotwords_path.read_text(encoding="utf-8").split() if w]
    except Exception:  # noqa: BLE001
        pass
    sources = t.hotword_sources or {}
    src_labels = {"inherit": "继承", "pick": "已选", "manual": "手动"}
    if hw_words:
        chips = ""
        for w in hw_words:
            src_tag = sources.get(w)
            label = f'<span class="slirn-hw-chip-label">{src_labels.get(src_tag, src_tag)}</span>' if src_tag else ""
            cls = f"slirn-hw-chip src-{src_tag}" if src_tag else "slirn-hw-chip"
            chips += f'<span class="{cls}" data-word="{_esc(w)}">{label}<span class="slirn-hw-chip-text">{_esc(w)}</span></span>'
        summary = f"共 {len(hw_words)} 个词" + (" · 来自继承全部公共库" if t.inherit_public else "")
        hotwords_disp = f'<div class="slirn-hw-chips">{chips}</div><div class="slirn-hw-summary">{_esc(summary)}</div>'
    else:
        hotwords_disp = "（无）"

    return video_disp, seg_disp, hotwords_disp


def _render_task_detail(task_id: str, mgr: TaskManager) -> str:
    try:
        t = mgr.get(task_id)
    except Exception as e:
        return f'<div class="slirn-empty"><div class="slirn-empty-icon">⚠️</div><div class="slirn-empty-text">{_esc(e)}</div></div>'

    from tasklib.models import TASK_STATUS_LABEL
    status_label = TASK_STATUS_LABEL.get(t.status, str(getattr(t.status, "value", t.status)))
    video_disp, seg_disp, hotwords_disp = _task_echo_fragments(t)
    src = t.original_video_source

    rows = [
        ("任务 ID", _esc(t.task_id)),
        ("任务名", _esc(t.name)),
        ("状态", _esc(status_label)),
        ("原始视频", f"{video_disp}<div class='slirn-detail-sub'>{_esc(str(src))}</div>"),
        ("截取段", seg_disp),
        ("热词", hotwords_disp),
        ("创建时间", _esc(_fmt_local(t.created_at))),
        ("修改时间", _esc(t.updated_at.isoformat() if hasattr(t.updated_at, "isoformat") else str(t.updated_at))),
    ]
    if t.segment:
        rows.append(("截取文件", f"<div class='slirn-detail-sub'>{_esc(str(t.segment.path))}</div>"))

    detail_rows = "".join([
        f'<div class="slirn-detail-row"><div class="slirn-detail-key">{k}</div><div class="slirn-detail-val">{v}</div></div>'
        for k, v in rows
    ])

    return f'''<div class="slirn-card">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">📄 任务详情</div>
            <button class="slirn-btn slirn-btn-sm" data-action="close-detail">✕ 关闭</button>
        </div>
        {detail_rows}
        <div class="slirn-task-actions" style="margin-top:20px;">
            <button class="slirn-btn slirn-btn-danger" data-action="delete-task" data-task-id="{_esc(task_id)}" data-task-name="{_esc(t.name)}">🗑️ 删除任务</button>
        </div>
    </div>
    {_render_subtitle_zone(task_id, t, mgr)}'''


# =============== 剪辑工作台（REQ-20260915-003） ===============

# 阶段定义：(key, 对应 TaskStatus, 标题, 图标, 说明) — 与 TaskStatus 管线一一对应
# REQ-20260919-075：去掉第八阶段「字幕合成/MUXED」（已被精剪视频的最终导出覆盖）；
# 任务终态改为 FINE_CUT_DONE。TaskStatus.MUXED 枚举保留（向后兼容），但不再有 UI 阶段引导到它。
_WB_STAGES = [
    ("assets",          "ASSETS_READY",         "素材准备", "📦", "上传视频 · 时间截取 · 任务热词"),
    ("subtitle",        "SUBTITLE_GENERATED",   "字幕生成", "🎙", "FunASR seaco-paraformer + 热词识别"),
    ("subtitle_review", "SUBTITLE_REVIEWED",    "字幕修订", "📝", "对照视频逐段校对、修改字幕文本与时间"),
    ("rough_cut",       "ROUGH_CUT_DONE",       "切分修剪", "✂️", "按修订决策带入保留/更正段，切分段父编号+子编号"),
    ("rough_compose",  "FINE_SUBTITLE_DONE",   "粗剪合成", "🎥", "按切分保留内容用上游 VideoClipper 合成粗剪视频（含随片字幕）"),
    ("fine_review",     "FINE_SUBTITLE_REVIEWED", "优化字幕", "✨", "重识别粗剪成片字幕，大模型提取不明确字词，人工替换并保存对应关系"),
    ("fine_cut",        "FINE_CUT_DONE",        "精剪合成", "🎬", "按精剪段生成成品视频"),
]


def _wb_stage_states(t) -> list[str]:
    """各阶段状态：done / current / pending。

    素材准备看磁盘资产（原视频在即完成，DRAFT 状态也算）；
    字幕生成看产物 subtitle.json、字幕修订看 revision.json、切分修剪看
    cutlist.json（服务器重启后内存 job 不在，以磁盘为准）；
    其余按 TaskStatus 管线序比较。
    """
    from tasklib.models import TaskStatus

    from slirn_home import asr_service as _asr_mod
    from slirn_home import cutlist_service as _cut_mod
    from slirn_home import revision_service as _rev_mod

    rank = {s.name: i for i, s in enumerate(TaskStatus)}
    cur_rank = rank.get(getattr(t.status, "name", str(t.status)), 0)
    states: list[str] = []
    assets_done = t.original_video_source.exists() or t.original_video_symlink.exists()
    outputs_dir = Path(str(t.hotwords_path)).parent / "outputs"
    sub_meta = None
    rev_meta = None
    cut_meta = None
    try:
        sub_meta = _asr_mod.load_subtitle(outputs_dir)
    except Exception:  # noqa: BLE001
        pass
    try:
        rev_meta = _rev_mod.load_revision(outputs_dir)
    except Exception:  # noqa: BLE001
        pass
    try:
        cut_meta = _cut_mod.load_cutlist(outputs_dir)
    except Exception:  # noqa: BLE001
        pass
    subtitle_done = bool(sub_meta and sub_meta.get("segments"))
    review_done = bool(rev_meta and rev_meta.get("entries"))
    cut_done = bool(cut_meta and cut_meta.get("items"))
    for key, status_name, *_rest in _WB_STAGES:
        if key == "assets":
            states.append("done" if assets_done else "pending")
        elif key == "subtitle":
            states.append("done" if (subtitle_done or cur_rank >= rank["SUBTITLE_GENERATED"]) else "pending")
        elif key == "subtitle_review":
            states.append(
                "done" if (review_done or cur_rank >= rank["SUBTITLE_REVIEWED"]) else "pending"
            )
        elif key == "rough_cut":
            states.append("done" if (cut_done or cur_rank >= rank[status_name]) else "pending")
        elif key == "rough_compose":
            # 必做阶段（REQ-20260916-016 可选 → REQ-20260918-045 改必做）：
            # 不推进任务状态，产物存在即完成；current 停留于此提示尽快合成
            from slirn_home import compose_service as _comp_mod

            composed = _comp_mod.rough_compose_path(outputs_dir).exists()
            states.append("done" if (composed or cur_rank >= rank[status_name]) else "pending")
        elif key == "fine_review":
            # 优化字幕（REQ-20260917-030）：替换确认保存（saved_at）即完成；
            # 兼容旧任务：fine_revision.json 已确认（旧精剪修订流程）同样视为完成
            from slirn_home import fine_service as _fine_mod
            from slirn_home import optimize_service as _opt_mod

            opt = _opt_mod.load_optimize(outputs_dir)
            fine = _fine_mod.load_fine(outputs_dir)
            done = bool((opt and opt.get("saved_at")) or (fine and fine.get("saved_at")))
            states.append("done" if (done or cur_rank >= rank[status_name]) else "pending")
        else:
            states.append("done" if cur_rank >= rank[status_name] else "pending")
    # current = 第一个 pending
    for i, s in enumerate(states):
        if s == "pending":
            states[i] = "current"
            break
    return states


def _fine_param(
    label: str,
    slider_id: str,
    data_key: str,
    value: float,
    min_v: float,
    max_v: float,
    step: float,
    value_format: str = "{:.2f}",
    data_attr: str = "data-key",
    raw_format: str | None = None,
) -> str:
    """REQ-20260919-061a v7 用户反馈：单个参数设置块 = label + slider + num。

    结构：
    ```
    ┌─ slirn-fine-param ────────────────────────────────┐
    │  [label]  [────── slider ──────]  [┌ num ┐▴▾]     │
    └──────────────────────────────────────────────────┘
    ```

    - range slider：拖动快速调（同步写 num 框）
    - number input：精调（直接输入数字，浏览器自带原生 stepper 满足 +1/-1）
    - `data-for=slider_id` 让 bindFineSteppers() 双向同步 slider ↔ num
    - v7：移除之前在输入框外面额外加的自定义 ▲▼ 按钮 — 浏览器原生 stepper 已提供
      同样功能，自定义按钮是重复的。

    `data_attr`：默认 `data-key`，给 audio 滑块传 `data-audio-key` 以保持它们走独立的
    `save_fine_audio` 端点而不是 `save_fine_layout`。
    `raw_format`：slider/number input 的 `value=` 字面值格式，默认跟随 `value_format`
    —— 这样 `0.4` 会渲染成 `"0.40"` 兼容旧测试断言。
    """
    if raw_format is None:
        raw_format = value_format
    raw = raw_format.format(value)
    return (
        f'<div class="slirn-fine-param">'
        f'<span class="slirn-fine-param-label">{label}</span>'
        f'<input type="range" class="slirn-fine-slider" id="{slider_id}" '
        f'{data_attr}="{data_key}" min="{min_v}" max="{max_v}" step="{step}" value="{raw}">'
        f'<input type="number" class="slirn-fine-num" id="{slider_id}_num" '
        f'aria-label="数值输入" min="{min_v}" max="{max_v}" step="{step}" value="{raw}" '
        f'data-for="{slider_id}">'
        f'</div>'
    )


def _render_fine_upload_card(task_id: str, kind: str, mgr: TaskManager, materials: dict) -> str:
    """精剪视频·单个素材上传卡 HTML（REQ-20260919-061）。

    独立成函数供两处复用：_render_fine_cut_zone 全量渲染 +
    auto_pick_upstream_material 局部刷新（自动获取成功后前端只换对应
    素材卡，不整页/整个工作台刷新 — 点击事件全部委托 data-action，
    换卡后无需重绑）。
    """
    icon, label, accept = _FINE_MATERIAL_LABELS[kind]
    mat = materials.get(kind) or {}
    path = mat.get("path") or ""
    filename = Path(path).name if path else ""
    source = mat.get("source") or ("upload" if path else "")
    # REQ-20260919-061 用户补充：video/subtitle 可从上游 auto 获取
    upstream_name = _fine_upstream_label(task_id, kind, mgr) if kind in _FINE_AUTO_KINDS else ""
    # source=auto 但上游产物已不存在 → 降级回空态（清掉 path）
    if source == "auto" and not upstream_name:
        source = ""
        path = ""
        filename = ""
    has = " has-file" if path else ""
    # REQ-20260919-061 用户反馈：状态显示当前来源 + 文件名；上传/自动获取两个按钮都常驻可用，
    # 点哪个就用哪个（最后一次操作决定 source 字段），不再禁用对方按钮。
    if source == "auto":
        source_badge = '<span class="slirn-fine-source-badge auto">📥 自动获取</span>'
        status_text = f"✅ 已从上游获取：{_esc(upstream_name)}"
    elif source == "upload" and filename:
        source_badge = '<span class="slirn-fine-source-badge upload">📤 手动上传</span>'
        status_text = f"✅ {_esc(filename)}"
    else:
        source_badge = ""
        status_text = "未上传"
    # 自动获取按钮：仅当上游存在时显示，且只在未自动获取时高亮（避免反复点）
    auto_btn_html = ""
    if kind in _FINE_AUTO_KINDS and upstream_name:
        auto_btn_html = (
            f'<button class="slirn-btn slirn-btn-xs" data-action="fine-source-auto" '
            f'data-kind="{kind}" title="从上游阶段产物自动获取：{_esc(upstream_name)}">'
            f'📥 自动获取（{_esc(upstream_name)}）</button>'
        )
    # REQ-20260920-082：把「系统提供的 BGM」下拉嵌进 audio 上传卡（从参数区迁移）。
    # REQ-20260921-NNN：改名「系统提供的 BGM」+ 仅显示可用（available=true）项
    # （missing 的不让选，避免运行时崩溃）。
    # 只在 audio 卡片里追加；其他 kind 不显示。
    # class 名复用 REQ-20260920-078 的 .slirn-fine-default-bgm-row，
    # 避免 router.js / CSS 改动。data-task-id 用于 router.js 的 change 委托拿 tid。
    default_bgm_html = ""
    if kind == "audio":
        default_bgm_html = (
            f'<div class="slirn-fine-default-bgm-row" data-task-id="{_esc(task_id)}">'
            f'<span class="slirn-fine-actions-label">📦 系统提供的 BGM</span>'
            f'<select id="slirn-fine-default-bgm" class="slirn-fine-default-bgm-select">'
            f'<option value="">— 不选（清空选择）—</option>'
            f'</select>'
            f'</div>'
        )
    return (
        f'<div class="slirn-fine-upload-card{has}" data-kind="{kind}" data-source="{source or "none"}">'
        f'<div class="slirn-fine-upload-label">{icon} {label}{source_badge}</div>'
        f'<div class="slirn-fine-upload-hint">{accept}</div>'
        f'<input type="file" class="slirn-fine-file" id="slirn-fine-file-{kind}" '
        f'accept=".{",".join(accept.split("/"))}" data-kind="{kind}">'
        f'<button class="slirn-btn slirn-btn-xs" data-action="fine-upload" data-kind="{kind}">📤 上传文件</button>'
        # REQ-20260919-063 用户反馈：每个素材都要提供预览功能；预览窗口可缩放。
        # 已有素材（has=has-file）才显示 👁️ 按钮，缺文件时禁用。
        f'<button class="slirn-btn slirn-btn-xs" data-action="fine-mat-preview" data-kind="{kind}" '
        f'data-task-id="{_esc(task_id)}" '
        f'{"disabled" if not has else ""} '
        f'title="{_esc("请先上传或自动获取素材") if not has else _esc("打开预览窗口（可缩放）")}">'
        f'👁️ 预览</button>'
        # REQ-20260920-088：素材路径详情按钮（弹窗显示完整路径 + 来源色块 + 文件元数据）
        f'<button class="slirn-btn slirn-btn-xs" data-action="fine-mat-detail" '
        f'data-kind="{kind}" data-task-id="{_esc(task_id)}" '
        f'{"disabled" if not path else ""} '
        f'title="{_esc("请先上传或自动获取素材") if not path else _esc("查看完整路径 + 文件元数据")}">'
        f'🔍 详情</button>'
        f'{auto_btn_html}'
        f'<div class="slirn-fine-upload-status" data-status-kind="{kind}">{status_text}</div>'
        f'{default_bgm_html}'
        f'</div>'
    )


def _render_fine_cut_zone(task_id: str, t, mgr: TaskManager) -> str:
    """REQ-20260919-061：精剪视频 pane — 5 素材上传 + 位置/缩放 + 字体 5 项 + 输出。"""
    fc = _get_fine_compose(mgr, task_id)
    materials = fc.get("materials") or {}
    layout = fc["layout"]
    font = fc["font"]
    output = fc["output"]
    # REQ-20260921-NNN：居中+偏移 默认值（保留「左偏」语义）
    # 老公式：left_offset = 1920 - bg_width（左偏 N 像素）
    # 新公式：offset = -(1920 - bg_width) = bg_width - 1920，仍是左偏 N 像素
    # 旧任务（无 offset）第一次渲染时按 bg_detect_cache 算出，不写回 fc（保留手动值）。
    _bg_cache = (fc.get("detected_region") or fc.get("bg_detect_cache") or {}) if isinstance(fc, dict) else {}
    _cache_w = _bg_cache.get("width")
    if "offset" not in font:
        try:
            _w = int(_cache_w) if _cache_w else 0
            font["offset"] = (_w - _FINE_DESIGN_W) if _w else 0
        except (TypeError, ValueError):
            font["offset"] = 0

    # 1. 5 个素材上传卡（渲染逻辑在 _render_fine_upload_card，供局部刷新复用）
    upload_cards = [
        _render_fine_upload_card(task_id, kind, mgr, materials)
        for kind in _FINE_MATERIAL_KINDS
    ]
    upload_html = '<div class="slirn-fine-uploads">' + "".join(upload_cards) + '</div>'

    # 2. 3 个素材的位置/缩放控件（video/subtitle/bg）
    # REQ-20260919-061 扩展：cover 不再是角标小图 → 改为独立的「片头全屏」控制块，
    # 不在 layout 循环里（详见下面 cover_html）。
    # REQ-20260919-061a 用户反馈 v3：每个 block 单独渲染（不再合并成 .slirn-fine-layouts），
    # block 内部参数 = 单列堆叠；外层由 _render_fine_cut_zone 末尾组装为左右两列。
    position_blocks = {}
    for mat_key in ("video", "subtitle", "bg"):
        lc = layout[mat_key]
        label_icon = {"video": "🎬 视频", "subtitle": "📝 字幕", "cover": "🖼 封面", "bg": "🎨 背景"}[mat_key]
        # REQ-20260919-062 v13 用户反馈：画布 X/Y 允许负数（视频可半截出画布，做"露半边"效果）。
        # 允许范围 = [-画布宽, +画布宽] / [-画布高, +画布高]（-1920–1920 / -1080–1080）。
        # 渲染层（_render_fine_cut_zone）按绝对坐标直接定位素材；负值 = 素材左侧/上侧出画布。
        # v14 用户反馈：缩放 = crop_w / bg_w（视频原剪辑宽度 / 背景图片宽度）。
        # 默认 crop_w=1920 → scale=1.0；自定义 crop 后点按钮「🎯 按裁剪宽度」自动应用公式。
        scale_auto_btn = (
            f'<button class="slirn-btn slirn-btn-xs slirn-fine-scale-auto-btn" '
            f'data-action="fine-scale-auto" '
            f'title="把视频缩放自动设为 crop_w / 1920（即视频原裁剪宽度占背景图片宽度的百分比）">'
            f'🎯 按裁剪宽度</button>'
        ) if mat_key == "video" else ''
        params_html = (
            f'<div class="slirn-fine-params">'
            + _fine_param(
                f"X（-{_FINE_DESIGN_W}–{_FINE_DESIGN_W}）", f"slirn-fine-{mat_key}-x",
                f"{mat_key}.x", int(lc["x"]), -_FINE_DESIGN_W, _FINE_DESIGN_W, 1, "{:d}",
            )
            + _fine_param(
                f"Y（-{_FINE_DESIGN_H}–{_FINE_DESIGN_H}）", f"slirn-fine-{mat_key}-y",
                f"{mat_key}.y", int(lc["y"]), -_FINE_DESIGN_H, _FINE_DESIGN_H, 1, "{:d}",
            )
            + _fine_param(
                # REQ-20260919-062 v6 用户反馈：视频缩放精度 5% → 1%。
                # v16 用户反馈：视频缩放值要显示 4 位小数（与 v15 自动重算保留 4 位一致）。
                # v17 用户反馈：bug — 自动重算到 4 位后立刻被浏览器截到 2 位。
                #   根因：<input type="range" step="0.01"> 会把 value 吸附到 0.01 网格，
                #   导致 s.value = "0.6667" 变成 "0.67"。step 改成 0.0001 后浏览器保留 4 位精度。
                # 其它素材（subtitle/cover/bg）仍保持 5% step + 2 位小数显示。
                "缩放", f"slirn-fine-{mat_key}-scale",
                f"{mat_key}.scale", float(lc["scale"]), 0.1, 2.0,
                0.0001 if mat_key == "video" else 0.05,
                "{:.4f}" if mat_key == "video" else "{:.2f}",
            )
            + scale_auto_btn
            + '</div>'
        )
        # REQ-20260919-062 v7 用户反馈：给视频添加宽高信息。
        # 仅 video 块附一个只读"显示尺寸"行，由前端 JS 实时计算。
        # v10 用户反馈：「视频播放时的宽度百分比为视频原裁剪的宽度除以背景图片整个区域的宽度」。
        # 背景图整个区域 = 设计空间 1920×1080；视频裁剪宽度 = crop_w；所以
        # 宽度百分比 = crop_w / 1920 × 100%。同时显示高同理（crop_h / 1080）。
        # REQ-20260923-NNN：显示尺寸与渲染 filter 同一公式 —
        #   显示宽 = 1920 × scale，显示高 = 显示宽 × crop_h / crop_w（保持裁剪比例）。
        # 旧读数 crop_w×scale / crop_h×scale 与实际渲染不一致（非 16:9 裁剪时偏差明显）。
        info_html = ""
        if mat_key == "video":
            try:
                _crop_w = int(lc["crop_w"])
                _crop_h = int(lc["crop_h"])
                _disp_w = int(round(_FINE_DESIGN_W * float(lc["scale"])))
                _disp_h = int(round(_disp_w * _crop_h / _crop_w)) if _crop_w > 0 else 0
            except (TypeError, ValueError):
                _disp_w, _disp_h, _crop_w, _crop_h = 0, 0, 0, 0
            _scale_pct = round(float(lc["scale"]) * 100, 4)
            # 背景图整个区域 = 设计空间 1920×1080；百分比按此计算
            _bg_w, _bg_h = 1920, 1080
            _crop_w_pct = round(_crop_w / _bg_w * 100, 2) if _bg_w else 0
            _crop_h_pct = round(_crop_h / _bg_h * 100, 2) if _bg_h else 0
            info_html = (
                f'<div class="slirn-fine-video-info" id="slirn-fine-video-info">'
                f'📐 显示尺寸: '
                f'<strong id="slirn-fine-video-disp-w">{_disp_w}</strong> × '
                f'<strong id="slirn-fine-video-disp-h">{_disp_h}</strong> px'
                # REQ-20260919-062 v16：缩放百分比也显示 4 位小数（与滑块/auto-recompute 一致）。
                f'　|　🎞 缩放 <span id="slirn-fine-video-scale-pct">{_scale_pct:.4f}</span>%'
                f'　|　📊 宽高比 '
                f'<span id="slirn-fine-video-aspect">'
                f'{(_disp_h / _disp_w) if _disp_w > 0 else 0:.3f}'
                f'</span>'
                f'　|　📏 占背景图 '
                f'<span id="slirn-fine-video-crop-w-pct">{_crop_w_pct:.2f}</span>'
                f'×<span id="slirn-fine-video-crop-h-pct">{_crop_h_pct:.2f}</span>%'
                f'（裁剪 {_crop_w}×{_crop_h} / 背景 1920×1080）'
                f'</div>'
            )
        position_blocks[mat_key] = (
            f'<div class="slirn-fine-layout-block">'
            f'<div class="slirn-fine-layout-title">'
            f'<label><input type="checkbox" class="slirn-fine-enabled" data-key="{mat_key}" '
            f'{"checked" if lc["enabled"] else ""}> {label_icon}</label>'
            f'</div>'
            f'{params_html}'
            f'{info_html}'
            f'</div>'
        )

    # 2.5 视频源裁剪（REQ-20260919-061 用户补充：crop_* 也用像素，基于 1920×1080 设计空间，
    # 渲染时按源视频实际尺寸等比换算）
    vc = layout["video"]
    crop_params = []
    for ck, ck_max in (
        ("crop_x", _FINE_DESIGN_W), ("crop_y", _FINE_DESIGN_H),
        ("crop_w", _FINE_DESIGN_W), ("crop_h", _FINE_DESIGN_H),
    ):
        ck_label = {
            "crop_x": f"X 起点（0–{_FINE_DESIGN_W}）",
            "crop_y": f"Y 起点（0–{_FINE_DESIGN_H}）",
            "crop_w": f"宽度（0–{_FINE_DESIGN_W}）",
            "crop_h": f"高度（0–{_FINE_DESIGN_H}）",
        }[ck]
        crop_params.append(
            _fine_param(
                ck_label, f"slirn-fine-video-{ck}",
                f"video.{ck}", int(vc[ck]), 0, ck_max, 1, "{:d}",
            )
        )
    # 实时算 crop 矩形比例（设计空间下）
    if vc["crop_w"] > 0:
        aspect = vc["crop_h"] / vc["crop_w"]
    else:
        aspect = 0
    crop_html = (
        f'<div class="slirn-fine-crop-block">'
        f'<div class="slirn-fine-layout-title">🎥 视频源裁剪（从源画面里截一个矩形范围）</div>'
        f'<div class="slirn-fine-crop-presets">'
        f'<button class="slirn-btn slirn-btn-xs" data-action="fine-crop-preset" data-preset="full">📐 全幅（不裁剪）</button>'
        f'<button class="slirn-btn slirn-btn-xs" data-action="fine-crop-preset" data-preset="16x9">🎯 16:9 居中</button>'
        f'<button class="slirn-btn slirn-btn-xs" data-action="fine-crop-preset" data-preset="1x1">⬛ 1:1 居中</button>'
        f'</div>'
        f'<label class="slirn-fine-crop-link-toggle">'
        # REQ-20260919-062 v18：把勾选状态读自 layout.video.crop_aspect_lock，
        # 默认 True（与旧版 HTML 默认勾选一致）。
        f'<input type="checkbox" id="slirn-fine-crop-aspect-link" '
        f'data-key="video.crop_aspect_lock" '
        f'{"checked" if bool(vc.get("crop_aspect_lock", True)) else ""}> '
        f'🔗 锁定 16:9 比例（防变形；关闭后可任意调整）</label>'
        f'<div class="slirn-fine-crop-aspect">当前矩形比例 ≈ <span id="slirn-fine-crop-aspect-val">{aspect:.3f}</span>'
        f'（目标 16:9 = {16/9:.3f}，1:1 = 1.000）</div>'
        f'<div class="slirn-fine-params">'
        + "".join(crop_params) +
        f'</div>'
        f'</div>'
    )

    # 2.6 片头封面（REQ-20260919-061 扩展）：不再画角标小图，封面作为片头全屏海报展示 N 秒
    cover_layout = layout["cover"]
    cover_duration = float(cover_layout.get("duration", 2.0))
    cover_html = (
        f'<div class="slirn-fine-cover-block">'
        f'<div class="slirn-fine-layout-title">🎞 片头封面（全屏展示 N 秒后切视频）</div>'
        f'<label><input type="checkbox" class="slirn-fine-enabled" data-key="cover" '
        f'{"checked" if cover_layout["enabled"] else ""}> 启用片头封面</label>'
        f'<div class="slirn-fine-params">'
        f'{_fine_param("展示时长（0–10 秒）", "slirn-fine-cover-duration", "cover.duration", cover_duration, 0, 10, 0.5, "{:.1f}")}'
        f'</div>'
        f'<div class="slirn-form-hint">设为 0 秒 = 不显示片头；建议 1–3 秒。'
        f'封面图会自动按比例铺满 1920×1080 画布（黑边填充）。'
        f'<b>封面播放期间视频静音（不输出原声），封面结束后才开始播放</b>。</div>'
        f'</div>'
    )

    # 2.7 背景音乐控制（REQ-20260919-061 扩展）：与原声混合（amix），保留说话人语音
    audio_cfg = fc.get("audio") or _FINE_AUDIO_DEFAULTS
    audio_html = (
        f'<div class="slirn-fine-audio-block">'
        f'<div class="slirn-fine-layout-title">🎵 背景音乐（与原声混合播放，保留说话人语音）</div>'
        f'<label><input type="checkbox" class="slirn-fine-enabled" data-key="audio" '
        f'{"checked" if audio_cfg["enabled"] else ""}> 启用背景音乐</label>'
        f'<div class="slirn-fine-params">'
        f'{_fine_param("音量（dB 衰减，0 = 原曲）", "slirn-fine-audio-volume", "volume_db", _fine_volume_db(audio_cfg), -40, 0, 1, "{:.0f}", data_attr="data-audio-key")}'
        f'{_fine_param("淡入（0–5 秒）", "slirn-fine-audio-fade_in", "fade_in", float(audio_cfg["fade_in"]), 0, 5, 0.5, "{:.1f}", data_attr="data-audio-key")}'
        f'{_fine_param("淡出（0–5 秒）", "slirn-fine-audio-fade_out", "fade_out", float(audio_cfg["fade_out"]), 0, 5, 0.5, "{:.1f}", data_attr="data-audio-key")}'
        f'</div>'
        f'<div class="slirn-form-hint">上传 mp3/wav/m4a 文件 → 原说话人语音 + BGM 同时播放；'
        f'短 BGM 自动循环填充。音量为相对 BGM 原曲的 dB 衰减'
        f'（-14 ≈ 旧版 0.2，-8 ≈ 旧版 0.4；曲子偏响建议 -20 左右）。'
        # REQ-20260920-082：系统提供的 BGM 下拉已从参数区迁移到「🎵 背景音乐」素材上传卡内
        # （贴在 audio upload card status 行下方）。下方留 hint 引导用户去上传区选 BGM。
        f'或在上方「🎵 背景音乐」上传卡内点「📦 系统提供的 BGM」选内置 lo-fi mp3。</div>'
        f'</div>'
    )

    # 3. AI 智能识别布局按钮（REQ-20260921-NNN：移到背景图区域检测块内）
    has_reference = bool(materials.get("reference", {}).get("path"))
    # tooltip 显示「将调用的当前模型」，让用户清楚按钮背后是哪个模型
    from slirn_home import llm_config as _llm_cfg
    _cur_model_id = _llm_cfg.get_current(mgr.tasks_dir.parent) or "未选"
    # 「必须先有参考图」单独标识 + 当前模型提示（一行展示，不挤按钮文字）
    if has_reference:
        _ai_badge = (
            f'<span class="slirn-fine-bg-detect-ai-badge slirn-fine-bg-detect-ai-badge-ok" '
            f'title="参考图已上传，可调用当前模型识别">✅ 参考图已就绪 · 模型：{_esc(_cur_model_id)}</span>'
        )
    else:
        _ai_badge = (
            f'<span class="slirn-fine-bg-detect-ai-badge slirn-fine-bg-detect-ai-badge-warn" '
            f'title="需先在「上传素材」里添加「参考位置关系图」">⚠️ 必须先上传参考图，才能识别</span>'
        )
    ai_btn = (
        f'<button class="slirn-btn slirn-btn-primary" data-action="fine-ai-parse" '
        f'data-task-id="{_esc(task_id)}" '
        f'{"disabled" if not has_reference else ""} '
        f'title="{_esc("需先上传参考位置关系图") if not has_reference else _esc(f"调当前模型「{_cur_model_id}」自动识别参考图布局")}">'
        f'🤖 AI 智能识别布局</button>'
        f'{_ai_badge}'
    )

    # 4. 字体 5 项
    font_family_options = (
        f'<option value="STHeitiMedium" {"selected" if font["family"] == "STHeitiMedium" else ""}>STHeitiMedium（系统）</option>'
        f'<option value="Noto Sans CJK SC" {"selected" if font["family"] == "Noto Sans CJK SC" else ""}>思源黑体 Noto Sans CJK SC</option>'
    )
    font_html = (
        f'<div class="slirn-fine-font-block">'
        f'<div class="slirn-fine-font-title">🔤 字幕字体设置</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">字体本身</span>'
        f'<select class="slirn-fine-font-sel" data-font-key="family">{font_family_options}</select>'
        f'</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">字体大小（px）</span>'
        f'<input type="number" class="slirn-fine-font-num" data-font-key="size" '
        f'min="12" max="96" value="{font["size"]}">'
        f'</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">粗体</span>'
        f'<label><input type="checkbox" class="slirn-fine-font-chk" data-font-key="bold" '
        f'{"checked" if font["bold"] else ""}> 启用</label>'
        f'</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">屏幕对齐</span>'
        f'<select class="slirn-fine-font-sel" data-font-key="align">'
        f'<option value="left" {"selected" if font["align"] == "left" else ""}>左对齐</option>'
        f'<option value="center" {"selected" if font["align"] == "center" else ""}>居中</option>'
        f'<option value="right" {"selected" if font["align"] == "right" else ""}>右对齐</option>'
        # REQ-20260921-NNN：居中+偏移（offset 可负：负数=左偏，正数=右偏）
        f'<option value="center_offset" {"selected" if font["align"] == "center_offset" else ""}>居中+偏移</option>'
        f'</select>'
        f'</div>'
        # REQ-20260921-NNN：偏移量（px）— signed；老 left_offset=434 已迁移为 offset=-434
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">偏移量（px）</span>'
        f'<input type="number" class="slirn-fine-font-num" data-font-key="offset" '
        f'min="-1920" max="1920" step="1" value="{int(font.get("offset", 0) or 0)}" '
        f'title="相对屏幕中心的偏移像素：负数=左偏，正数=右偏（仅「居中+偏移」模式生效）">'
        f'</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">描边宽度（px）</span>'
        f'<input type="number" class="slirn-fine-font-num" data-font-key="stroke_width" '
        f'min="0" max="10" value="{font["stroke_width"]}">'
        f'</div>'
        # REQ-20260919-062 v19：字幕文字本身颜色 picker（之前只有描边/背景，
        # 在白色 PPT 背景上默认白字=看不见 → 用户以为是「白框」）
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">文字颜色</span>'
        f'<input type="color" class="slirn-fine-font-color" data-font-key="color" '
        f'value="{font["color"]}">'
        f'</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">描边颜色</span>'
        f'<input type="color" class="slirn-fine-font-color" data-font-key="stroke_color" '
        f'value="{font["stroke_color"]}">'
        f'</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">背景框</span>'
        f'<label><input type="checkbox" class="slirn-fine-font-chk" data-font-key="bg_enabled" '
        f'{"checked" if font["bg_enabled"] else ""}> 启用</label>'
        f'</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">背景颜色</span>'
        f'<input type="color" class="slirn-fine-font-color" data-font-key="bg_color" '
        f'value="{font["bg_color"]}">'
        f'</div>'
        # REQ-20260919-061a v7：背景透明度 = label + slider + num（用浏览器原生 stepper，
        # 移除自定义 ▲▼ 按钮 — 与外层其他 num 框一致）。
        f'<div class="slirn-fine-params">'
        f'<div class="slirn-fine-param">'
        f'<span class="slirn-fine-param-label">背景透明度</span>'
        f'<input type="range" class="slirn-fine-font-slider" id="slirn-fine-font-bg_opacity" '
        f'data-font-key="bg_opacity" min="0" max="1" step="0.05" value="{font["bg_opacity"]}">'
        f'<input type="number" class="slirn-fine-num" id="slirn-fine-font-bg_opacity_num" '
        f'aria-label="数值输入" min="0" max="1" step="0.05" value="{font["bg_opacity"]}" '
        f'data-for="slirn-fine-font-bg_opacity">'
        f'</div>'
        f'</div>'
        f'</div>'
    )

    # 5. 输出 3 项
    output_html = (
        f'<div class="slirn-fine-output-block">'
        f'<div class="slirn-fine-font-title">📺 输出设置</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">分辨率</span>'
        f'<select class="slirn-fine-output-sel" data-output-key="resolution">'
        f'<option value="1080p" {"selected" if output["resolution"] == "1080p" else ""}>1080p（1920×1080）</option>'
        f'<option value="720p" {"selected" if output["resolution"] == "720p" else ""}>720p（1280×720）</option>'
        f'<option value="source" {"selected" if output["resolution"] == "source" else ""}>原始视频分辨率</option>'
        f'</select>'
        f'</div>'
        f'</div>'
    )

    # 6. 预览/导出按钮（Phase B：ffmpeg 渲染已就绪）
    #   启用条件：至少视频素材已就绪（auto-pick 上游产物或 手动上传）
    has_video = bool(materials.get("video", {}).get("path"))
    preview_btn_disabled = "" if has_video else "disabled"
    preview_btn_title = "渲染预览（ffmpeg overlay，时长 2–30 秒可调）" if has_video else "请先上传或自动获取视频素材"
    export_btn_disabled = "" if has_video else "disabled"
    export_btn_title = "导出完整视频到 outputs/fine_export.mp4" if has_video else "请先上传或自动获取视频素材"
    preview_btn = (
        f'<button class="slirn-btn slirn-btn-primary" data-action="fine-preview" data-task-id="{_esc(task_id)}" '
        f'{preview_btn_disabled} title="{_esc(preview_btn_title)}">🎬 生成预览</button>'
    )
    # REQ-20260921-NNN：预览参数（start_h/m/s + duration）作为「设置参数」保存到 fc.json；
    # 渲染时优先读 fc.preview（不再硬编码 0/10），刷新页面也能恢复。
    _preview_cfg = fc.get("preview") or _FINE_PREVIEW_DEFAULTS
    _ph = int(_preview_cfg.get("start_h", 0) or 0)
    _pm = int(_preview_cfg.get("start_m", 0) or 0)
    _ps = int(_preview_cfg.get("start_s", 0) or 0)
    _pd = int(_preview_cfg.get("duration", _FINE_PREVIEW_DEFAULTS["duration"]) or _FINE_PREVIEW_DEFAULTS["duration"])
    # REQ-20260919-061a v6 用户反馈：删除预览时长旁的 ▲▼ 按钮 — 直接修改 number input 即可。
    # 仍保留 number input + min/max 限制 + Enter 提交语义。
    # REQ-20260919-064：新增「预览开始时间」input 在「预览时长」前 — 让用户能跳到视频
    # 不同时间点预览。start + duration 由后端钳到不超出源视频时长。
    # REQ-20260919-066 用户反馈：开始时间改为 时:分:秒 三段输入（比纯秒更直观）；
    # 前端在发送前转成总秒数发给后端。默认 00:00:00。
    # REQ-20260921-NNN：input 加 data-preview-key，让 fineSaveAll 收集并持久化到 fc.preview。
    preview_start = (
        f'<span class="slirn-fine-preview-start">'
        f'<span class="slirn-fine-actions-label">预览开始时间</span>'
        f'<input type="number" id="slirn-fine-preview-start-h" class="slirn-fine-num slirn-fine-preview-time" '
        f'data-preview-key="start_h" aria-label="预览开始时间（小时）" '
        f'min="0" step="1" value="{_ph}" maxlength="2">'
        f'<span class="slirn-fine-time-sep">:</span>'
        f'<input type="number" id="slirn-fine-preview-start-m" class="slirn-fine-num slirn-fine-preview-time" '
        f'data-preview-key="start_m" aria-label="预览开始时间（分钟）" '
        f'min="0" max="59" step="1" value="{_pm}" maxlength="2">'
        f'<span class="slirn-fine-time-sep">:</span>'
        f'<input type="number" id="slirn-fine-preview-start-s" class="slirn-fine-num slirn-fine-preview-time" '
        f'data-preview-key="start_s" aria-label="预览开始时间（秒）" '
        f'min="0" max="59" step="1" value="{_ps}" maxlength="2">'
        f'<span class="slirn-fine-actions-label">（时:分:秒）</span>'
        f'</span>'
    )
    preview_duration = (
        f'<span class="slirn-fine-preview-duration">'
        f'<span class="slirn-fine-actions-label">预览时长</span>'
        f'<input type="number" id="slirn-fine-preview-duration" class="slirn-fine-num" '
        f'data-preview-key="duration" aria-label="预览时长（秒，2-30）" '
        f'min="2" max="30" step="1" value="{_pd}">'
        f'<span class="slirn-fine-actions-label">秒（2–30）</span>'
        f'</span>'
    )
    # REQ-20260920-077：导出按钮 + 右侧 inline 状态元素（替代 REQ-074 的进度模态框）。
    # 包成 cell：按钮 + 状态元素同行，flex 容器由 .slirn-fine-actions-bar 提供。
    export_btn = (
        f'<span class="slirn-fine-export-cell">'
        f'<button class="slirn-btn slirn-btn-primary" id="slirn-fine-export-btn" '
        f'data-action="fine-export" data-task-id="{_esc(task_id)}" '
        f'{export_btn_disabled} title="{_esc(export_btn_title)}">'
        f'💾 导出最终视频</button>'
        f'<span class="slirn-fine-export-status" id="slirn-fine-export-status" '
        f'data-state="idle" hidden></span>'
        # REQ-20260920-089：独立可见的取消按钮（不依赖 status 元素的 hidden 态）
        f'<button class="slirn-btn slirn-btn-xs slirn-fine-export-cancel" '
        f'id="slirn-fine-export-cancel-btn" data-action="fine-export-cancel" '
        f'data-state="idle" style="display:none;" '
        f'title="取消当前渲染">⏹ 取消</button>'
        f'</span>'
    )
    preview_box = (
        # REQ-20260919-062 v10 用户反馈：去掉页面内的预览框（设计空间画布），
        # 弹出窗口预览（.slirn-mat-preview-float）就够了；预览框占页面大块空间，
        # 不再需要。生成预览按钮仍保留，让用户能渲染后再用弹窗预览查看。
        '<div class="slirn-fine-preview-empty" id="slirn-fine-preview-empty" hidden>'
        '本页已去掉内嵌预览；点「🎬 生成预览」后用「👁️ 预览」弹窗查看效果。</div>'
    )

    # REQ-20260919-061 用户反馈：把「保存参数为模板」+ 模板管理搬到顶部操作栏。
    # 设计：
    #   - 模板名输入框（空着也行，但保存时会弹窗要求填名）
    #   - 💾 保存参数为模板：保存当前参数到 fine_compose；如有模板名 → 同步另存为全局模板
    #   - 📥 引用参数：弹出模态框列出所有已保存模板，点「应用」覆盖当前任务参数
    #   - 状态指示器：显示「未保存 / 保存中 / 上次保存 HH:MM:SS / 保存失败」
    #   - 自动保存（滑块拖动 300ms 防抖）仍然只写 fine_compose，不写模板（避免一堆「未命名」）
    save_status = '<span class="slirn-fine-save-status" id="slirn-fine-save-status" data-state="idle">未保存</span>'

    # 引用参数（本地）modal（默认 hidden）。列表内容由前端 fineImportShow() 动态填充。
    import_modal = (
        f'<div class="slirn-modal-overlay" id="slirn-fine-import-overlay" hidden>'
        f'<div class="slirn-modal-card slirn-fine-import-card">'
        f'<div class="slirn-modal-title">📥 引用参数模板（应用到当前任务）</div>'
        f'<div class="slirn-fine-import-list" id="slirn-fine-import-list">'
        f'<div class="slirn-fine-profile-empty">加载中…</div>'
        f'</div>'
        f'<div style="display:flex; gap:10px; justify-content:center; margin-top:12px;">'
        f'<button class="slirn-btn" data-action="fine-import-close">关闭</button>'
        f'</div>'
        f'<div class="slirn-form-hint" style="margin-top:10px;">'
        f'「应用」会覆盖当前任务的对应参数（位置/字体/输出/音频），弹窗确认后生效。'
        f'「导出」会把该模板的参数下载为 JSON 文件（不依赖任务，可在外部备份/分享）。'
        f'</div>'
        f'</div>'
        f'</div>'
    )

    # REQ-20260920-088：素材路径详情模态框（弹窗显示完整路径 + 来源色块 + 文件元数据）
    mat_detail_modal = (
        f'<div class="slirn-modal-overlay" id="slirn-mat-detail-modal" hidden>'
        f'<div class="slirn-modal-card slirn-mat-detail-modal">'
        f'<div class="slirn-modal-title">🔍 素材路径详情</div>'
        f'<div class="slirn-mat-detail-body" id="slirn-mat-detail-body">'
        f'<div class="slirn-fine-profile-empty">加载中…</div>'
        f'</div>'
        f'<div style="display:flex; gap:10px; justify-content:center; margin-top:12px;">'
        f'<button class="slirn-btn" data-action="mat-detail-close">关闭</button>'
        f'</div>'
        f'</div>'
        f'</div>'
    )

    # REQ-20260919-061a 用户反馈 v4：左右两列的前 2 个块固定为视频/视频源裁剪（左）、
    # 字幕/字幕字体设置（右）。剩余 4 块按主题续列：
    #   左列（视频主层相关）：视频位置 → 视频源裁剪 → 背景位置 → 背景音乐
    #   右列（叠加 + 收尾）：  字幕位置 → 字幕字体设置 → 片头封面 → 输出设置
    # 每个 block 内部参数仍是单列堆叠（_fine_param 3 列 mini-grid = label/slider/num）。
    left_col_blocks = (
        position_blocks["video"]
        + crop_html
        + position_blocks["bg"]
        + audio_html
    )
    right_col_blocks = (
        position_blocks["subtitle"]
        + font_html
        + cover_html
        + output_html
    )
    fine_cols_html = (
        f'<div class="slirn-fine-cols">'
        f'<div class="slirn-fine-col">{left_col_blocks}</div>'
        f'<div class="slirn-fine-col">{right_col_blocks}</div>'
        f'</div>'
    )

    # REQ-20260919-064：把预览/导出行 + 模板/保存行 拆成两行（每个一行 .slirn-fine-actions-bar）。
    # 模板名 input 用 flex:1 自动填充剩余宽度；不再需要 sep 分隔符。
    combined_actions_bar = (
        # 行 1：导出最终视频 + 参数文件导入导出（REQ-20260921-NNN：
        #   - 预览相关参数挪到下方独立一行
        #   - 导出参数 / 导入参数紧贴「导出最终视频」按钮）
        f'<div class="slirn-fine-actions-bar">'
        f'{export_btn}'
        f'<button class="slirn-btn" data-action="fine-export-params" '
        f'data-task-id="{_esc(task_id)}" '
        f'title="下载所有参数（布局/字体/输出/音频/检测区域）为 JSON 文件">'
        f'📤 导出参数</button>'
        f'<button class="slirn-btn" data-action="fine-import-params" '
        f'data-task-id="{_esc(task_id)}" '
        f'title="从 JSON 文件导入参数（覆盖当前参数；不动素材文件）">'
        f'📥 导入参数</button>'
        f'</div>'
        # 行 2：模板管理 + 引用参数（REQ-20260921-NNN：「💾 保存参数为模板」挪到行首）
        f'<div class="slirn-fine-actions-bar">'
        f'<button class="slirn-btn slirn-btn-primary" data-action="fine-save-all" '
        f'data-task-id="{_esc(task_id)}" title="保存当前参数为模板；未填名会弹窗要求填">'
        f'💾 保存参数为模板</button>'
        f'<span class="slirn-fine-actions-label">模板名</span>'
        f'<input type="text" class="slirn-fine-profile-name" id="slirn-fine-profile-name" '
        f'placeholder="（可选）填了名另存为模板" maxlength="30" autocomplete="off">'
        f'<button class="slirn-btn" data-action="fine-import-show" '
        f'data-task-id="{_esc(task_id)}" title="从本机已保存的全局参数模板中选择应用（不会发到外部）">'
        f'📥 引用参数（本地）</button>'
        f'{save_status}'
        f'</div>'
        # 行 3：生成预览相关参数（按钮 + 预览开始秒数 + 预览时长）独立一行
        # 紧贴模板行下方，让用户集中调节预览相关选项（与「导出最终视频」区分开）
        f'<div class="slirn-fine-actions-bar">'
        f'{preview_btn} {preview_start} {preview_duration}'
        f'</div>'
    )

    # REQ-20260919-062：背景图区域检测面板（独立块）。
    # v8 用户反馈：
    #   - 「把背景图区域颜色剪下」→ 检测主色用大色块 + RGB + HEX 醒目标签
    #   - 「信息区挪到 AI 智能布局区域的上方」→ 整个检测块上移到 combined_actions_bar 之前
    #   - 「检测后存储检测信息」→ 命中检测后写 fc.bg_detect_cache；渲染时优先读缓存
    # 算法：
    #   - center_expand 从中心向 4 方向扩展，遇到颜色变化即停（默认；最稳）
    #   - pixel         像素扫描（白色 RGB≥threshold，已知是白色时用）
    #   - ai_color      LLM 识别左下角主色 → 像素扫描（颜色未知但需要彩色 bbox 时）
    #   - ai            LLM 直接给出 bbox（已废弃）
    has_bg = bool(materials.get("bg", {}).get("path"))
    # REQ-20260919-065：优先读 detected_region（新正式位置），fallback 到 bg_detect_cache（向后兼容）
    bg_cache = (fc.get("detected_region") or fc.get("bg_detect_cache") or {}) if isinstance(fc, dict) else {}
    _has_cache = bool(bg_cache)
    _hidden_attr = '' if _has_cache else ' hidden'
    # v12 用户反馈：「背景图主色」大色块已去掉；下方的几何信息区仍保留（角点+宽高+中心+像素数+原图尺寸）。
    # 缓存里的几何信息
    _cache_x = bg_cache.get("x")
    _cache_y = bg_cache.get("y")
    _cache_w = bg_cache.get("width")
    _cache_h = bg_cache.get("height")
    _cache_cx = bg_cache.get("center_x")
    _cache_cy = bg_cache.get("center_y")
    _cache_pixels = bg_cache.get("pixel_count")
    _cache_native_w = bg_cache.get("image_native_w")
    _cache_native_h = bg_cache.get("image_native_h")
    _cache_corners = bg_cache.get("corners") or {}
    _cache_algo = bg_cache.get("algorithm") or "—"
    _cache_at = bg_cache.get("detected_at") or ""
    def _cache_val(v):
        return "—" if v is None else str(v)
    def _cache_corner(name):
        c = _cache_corners.get(name) if isinstance(_cache_corners, dict) else None
        if not c or not isinstance(c, list) or len(c) != 2:
            return "—"
        return f"({c[0]}, {c[1]})"
    # REQ-20260919-072：背景图检测状态徽章（summary 右侧，折叠时一眼看到进度）
    if not has_bg:
        _bg_status_badge = '<span class="slirn-fine-section-status">⏳ 未上传背景图</span>'
    elif _has_cache and _cache_w and _cache_h:
        _bg_status_badge = (
            f'<span class="slirn-fine-section-status">✅ 已检测 '
            f'{_cache_val(_cache_w)} × {_cache_val(_cache_h)}</span>'
        )
    else:
        _bg_status_badge = '<span class="slirn-fine-section-status">⚠️ 未检测</span>'

    bg_detect_block = (
        f'<div class="slirn-fine-bg-detect-block">'
        # v12 用户反馈：「背景图主色」那个大色块看不出有什么用（结果区已含坐标/宽高，
        # 再展示 RGB/HEX 没意义；主色只是中间量，不是终态交付物）。已去掉主色显示区。
        f'<div class="slirn-fine-layout-title">🎨 背景图区域检测（4 角点 + 宽高）</div>'
        # 算法 + 阈值/色容差 + 触发按钮
        f'<div class="slirn-fine-bg-detect-controls">'
        f'<span class="slirn-fine-actions-label">算法</span>'
        f'<select class="slirn-fine-bg-detect-algo" id="slirn-fine-bg-detect-algo">'
        f'<option value="center_expand" selected>🎯 中心扩展（默认；颜色未知时推荐）</option>'
        f'<option value="pixel">🔍 像素扫描白色（已知是白色）</option>'
        f'<option value="ai_color">🤖 AI 识别主色（颜色未知，需彩色 bbox）</option>'
        f'<option value="ai">🤖 AI 直接给 bbox（已废弃）</option>'
        f'</select>'
        # 阈值/容差控件（仅 pixel 算法显示；ai_color 用 ±10 容差不可改，ai 无需）
        f'<span class="slirn-fine-bg-detect-threshold-row" id="slirn-fine-bg-detect-threshold-row">'
        f'<span class="slirn-fine-actions-label">阈值</span>'
        f'<select class="slirn-fine-bg-detect-threshold-sel" id="slirn-fine-bg-detect-threshold-sel">'
        f'<option value="255">纯白 (255)</option>'
        f'<option value="250" selected>近白 (250)</option>'
        f'<option value="240">宽松 (240)</option>'
        f'<option value="230">很宽松 (230)</option>'
        f'</select>'
        f'<input type="number" class="slirn-fine-num" id="slirn-fine-bg-detect-threshold-num" '
        f'min="200" max="255" step="1" value="250" '
        f'aria-label="手动输入阈值（200-255）">'
        f'</span>'
        f'<button class="slirn-btn slirn-btn-primary" data-action="fine-bg-detect" '
        f'data-task-id="{_esc(task_id)}" '
        f'{"disabled" if not has_bg else ""} '
        f'title="{_esc("请先上传背景图片") if not has_bg else _esc("运行区域检测")}">'
        f'🔍 检测区域</button>'
        # v8：清空缓存按钮（用「重新检测」也可，但显式清空能让用户区分「清缓存」与「再跑一次」）
        f'<button class="slirn-btn slirn-btn-xs" data-action="fine-bg-detect-clear" '
        f'data-task-id="{_esc(task_id)}" '
        f'{"disabled" if not _has_cache else ""} '
        f'title="清空检测缓存（之后可重新检测）">'
        f'🗑 清空缓存</button>'
        f'</div>'
        # 只读结果区
        f'<div class="slirn-fine-bg-detect-result" id="slirn-fine-bg-detect-result"{_hidden_attr}>'
        f'<div class="slirn-fine-bg-detect-grid">'
        f'<span class="slirn-fine-bg-detect-key">左上角 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-tl">{_cache_corner("topleft")}</span>'
        f'<span class="slirn-fine-bg-detect-key">右上角 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-tr">{_cache_corner("topright")}</span>'
        f'<span class="slirn-fine-bg-detect-key">左下角 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-bl">{_cache_corner("bottomleft")}</span>'
        f'<span class="slirn-fine-bg-detect-key">右下角 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-br">{_cache_corner("bottomright")}</span>'
        f'<span class="slirn-fine-bg-detect-key">宽 × 高 (px)</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-wh">'
        f'{_cache_val(_cache_w)} × {_cache_val(_cache_h)}'
        f'</span>'
        f'<span class="slirn-fine-bg-detect-key">中心 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-center">'
        f'{_cache_val(_cache_cx)}, {_cache_val(_cache_cy)}'
        f'</span>'
        f'<span class="slirn-fine-bg-detect-key">白色像素数</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-pixels">{_cache_val(_cache_pixels)}</span>'
        f'<span class="slirn-fine-bg-detect-key">原图分辨率</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-native">'
        f'{_cache_val(_cache_native_w)} × {_cache_val(_cache_native_h)}'
        f'</span>'
        f'<span class="slirn-fine-bg-detect-key">检测算法</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-algo-used">{_esc(_cache_algo)}</span>'
        f'<span class="slirn-fine-bg-detect-key">检测时间</span>'
        f'<span class="slirn-fine-bg-detect-val" id="slirn-fine-bg-detect-time">{_esc(_cache_at)}</span>'
        f'</div>'
        f'<div class="slirn-fine-bg-detect-actions">'
        f'<button class="slirn-btn" data-action="fine-bg-detect-apply" '
        f'data-task-id="{_esc(task_id)}" title="把白色区域应用到 video：X/Y=区域左上角，crop=从原视频(0,0)取区域宽高，scale=1.0，并把视频限定在该区域内">'
        f'✅ 填充到视频位置和裁剪</button>'
        f'</div>'
        f'</div>'
        # REQ-20260921-NNN：AI 智能识别布局按钮从顶部操作栏移到此处；
        # 必须有参考图（reference 素材），AI 对参考图做视觉识别，自动给出布局建议。
        f'<div class="slirn-fine-bg-detect-ai-row">'
        f'{ai_btn}'
        f'<span class="slirn-fine-bg-detect-ai-desc">'
        f'需先在「上传素材 → 参考位置关系图」里上传一张参考图，'
        f'AI 会自动识别图中素材位置/缩放关系，并写回下方 4 个素材的滑块。'
        f'</span>'
        f'</div>'
        f'<div class="slirn-form-hint">白色 = R/G/B 三通道均 ≥ 阈值；阈值越小（200）越宽松，越大（255）越严格。'
        f'检测到的坐标都是 1920×1080 设计空间像素，结果自动保存到任务里（避免反复扫描）。</div>'
        f'</div>'
    )

    # REQ-20260919-072 v1：上传区域状态徽章（折叠时一眼看到进度）
    _uploaded_count = sum(
        1 for k in ("video", "subtitle", "cover", "bg", "reference", "audio")
        if materials.get(k, {}).get("path")
    )
    if _uploaded_count == 0:
        _upload_status_badge = '<span class="slirn-fine-section-status">⏳ 0/6 已上传</span>'
    elif _uploaded_count == 6:
        _upload_status_badge = f'<span class="slirn-fine-section-status">✅ {_uploaded_count}/6 已上传</span>'
    else:
        _upload_status_badge = f'<span class="slirn-fine-section-status">⚠️ {_uploaded_count}/6 已上传</span>'

    return (
        f'<div class="slirn-wb-pane-card">'
        f'<div class="slirn-wb-pane-title">🎬 精剪合成 · 素材合成器</div>'
        # REQ-20260919-072 v1：说明挪到最顶端（标题之后立刻显示）
        f'<div class="slirn-form-hint">📐 位置坐标（X/Y）和缩放（归一化 0-1 / 0.1-2.0）</div>'
        # REQ-20260919-072 v1：素材上传区可折叠（默认折叠；不写 open 属性）
        f'<details class="slirn-fine-section" id="slirn-fine-uploads-details">'
        f'<summary class="slirn-fine-section-summary">📁 上传素材（6 项）{_upload_status_badge}</summary>'
        f'{upload_html}'
        f'</details>'
        # REQ-20260919-072 v1：背景图区域检测可折叠（默认折叠；不写 open 属性）
        f'<details class="slirn-fine-section" id="slirn-fine-bg-detect-details">'
        f'<summary class="slirn-fine-section-summary">🎨 背景图区域检测（4 角点 + 宽高）{_bg_status_badge}</summary>'
        f'{bg_detect_block}'
        f'</details>'
        f'{combined_actions_bar}'
        # REQ-20260920-098：「一键合成」两步流程已废弃 —— 删除组合测试面板
        # 现在「生成预览」「导出最终视频」直接读参数卡的 .slirn-fine-enabled checkbox
        f'<div class="slirn-form-hint">💡 上传 5 个素材后调滑块调位置/缩放/字体；改动后 300ms 自动保存到当前任务。'
        f'点「💾 保存参数为模板」会同时把当前参数另存为全局模板（未填模板名则弹窗要求填）。</div>'
        f'{preview_box}'
        f'{fine_cols_html}'
        f'{import_modal}'
        f'{mat_detail_modal}'
        f'</div>'
    )


def _render_workbench(task_id: str, mgr: TaskManager) -> str:
    """剪辑工作台：顶部任务信息 + 左侧阶段步骤条 + 右侧各阶段执行面板。"""
    try:
        t = mgr.get(task_id)
    except Exception as e:
        return f'<div class="slirn-empty"><div class="slirn-empty-icon">⚠️</div><div class="slirn-empty-text">{_esc(e)}</div></div>'

    from tasklib.models import TASK_STATUS_LABEL
    status_label = TASK_STATUS_LABEL.get(t.status, str(getattr(t.status, "value", t.status)))
    video_disp, seg_disp, hotwords_disp = _task_echo_fragments(t)

    # ---- 顶部：任务信息 ----
    top_rows = "".join([
        f'<div class="slirn-detail-row"><div class="slirn-detail-key">{k}</div><div class="slirn-detail-val">{v}</div></div>'
        for k, v in [
            ("任务", f"{_esc(t.name)}<span class='slirn-wb-tid'>（{_esc(task_id)}）</span>"),
            ("状态", _esc(status_label)),
            ("原始视频", video_disp),
            ("截取段", seg_disp),
            ("热词", hotwords_disp),
        ]
    ])

    # ---- 左：阶段步骤条 ----
    states = _wb_stage_states(t)
    # 默认聚焦：最后一个 done 的下一阶段（= current）；全完成 → 最后一阶段
    focus = next((i for i, s in enumerate(states) if s == "current"), len(_WB_STAGES) - 1)
    stage_items = ""
    for i, (key, _st, title, icon, desc) in enumerate(_WB_STAGES):
        state = states[i]
        # REQ-20260921-NNN：阶段序号从 0 开始（assets=0、精剪合成=6）。
        # mark 规则：done → ✓ 覆盖序号；current/pending → 显示序号（不要让
        # ▶ 进度图标把序号盖掉 —— 用户希望序号一直可见，只在完成后才被 ✓ 替换）
        mark = "✓" if state == "done" else str(i)
        opt_chip = ""  # REQ-20260918-045：粗剪合成由可选改为必做，不再显示「可选」徽章
        stage_items += (
            f'<div class="slirn-wb-stage {state}{" active" if i == focus else ""}" '
            f'data-action="wb-stage" data-pane="{key}">'
            f'<span class="slirn-wb-stage-mark">{mark}</span>'
            f'<div class="slirn-wb-stage-body"><div class="slirn-wb-stage-title">{icon} {title}{opt_chip}</div>'
            f'<div class="slirn-wb-stage-desc">{desc}</div></div></div>'
        )
    # REQ-20260918-053：执行日志 — 非流水线阶段，单独追加在 rail 末尾（不计入
    # _WB_STAGES，避免 wbAutoNextMaybe 自动跳到此页）
    # REQ-20260920-086：在「精剪视频」与「📜 执行日志」之间加明显分隔条，明确
    # 声明「执行日志不属于流水线阶段」；logs stage 加 slirn-wb-stage-extra 类
    # （与 router.js wbAutoNextMaybe 的 :not(.slirn-wb-stage-extra) 选择器对齐）。
    stage_items += (
        f'<div class="slirn-wb-rail-divider" '
        f'title="执行日志是历史视图，不属于流水线阶段（不参与自动跳转）">'
        f'📜 执行日志（不属于流水线阶段）'
        f'</div>'
    )
    stage_items += (
        f'<div class="slirn-wb-stage slirn-wb-stage-logs slirn-wb-stage-extra pending" '
        f'data-action="wb-stage" data-pane="logs" '
        f'title="查看该任务所有阶段的执行历史（含耗时、错误信息，支持分页 + 过滤）">'
        f'<span class="slirn-wb-stage-mark">📜</span>'
        f'<div class="slirn-wb-stage-body"><div class="slirn-wb-stage-title">📜 执行日志</div>'
        f'<div class="slirn-wb-stage-desc">查看所有阶段执行历史（生成/修订/切分/合成/优化）</div></div></div>'
    )

    # ---- 右：各阶段面板 ----
    def _pane_planned(i: int, desc: str) -> str:
        return (
            '<div class="slirn-empty"><div class="slirn-empty-icon">🚧</div>'
            '<div class="slirn-empty-text">规划中 — 该阶段将在后续版本提供</div>'
            f'<div class="slirn-form-hint">{_esc(desc)}</div></div>'
        )

    assets_pane = "".join([
        f'<div class="slirn-detail-row"><div class="slirn-detail-key">{k}</div><div class="slirn-detail-val">{v}</div></div>'
        for k, v in [
            ("原始视频", f"{video_disp}<div class='slirn-detail-sub'>{_esc(str(t.original_video_source))}</div>"),
            ("截取段", seg_disp),
            ("热词", hotwords_disp),
        ]
    ])

    panes = {
        "assets": f'<div class="slirn-wb-pane-card"><div class="slirn-wb-pane-title">📦 资产清单</div>{assets_pane}</div>',
        "subtitle": _render_subtitle_zone(task_id, t, mgr),
        "subtitle_review": _render_revision_zone(task_id, t, mgr),
        "rough_cut": _render_cutlist_zone(task_id, t, mgr),
        "rough_compose": _render_rough_compose_zone(task_id, t, mgr),
        "fine_review": _render_optimize_zone(task_id, t, mgr),
        # REQ-20260919-061：精剪视频·四素材合成器（Phase A 骨架）
        "fine_cut": _render_fine_cut_zone(task_id, t, mgr),
        "logs": _render_exec_logs_pane(task_id),  # REQ-20260918-053
    }
    for i, (key, _st, _t2, _ic, desc) in enumerate(_WB_STAGES):
        if key in panes:
            continue
        panes[key] = _pane_planned(i, desc)

    hidden_attr = ' style="display:none;"'
    # REQ-20260920-086：pane 渲染迭代所有 panes key（含 logs 等非流水线视图）。
    # 旧实现只迭代 _WB_STAGES → logs 等非流水线 pane 从未渲染到 HTML —— 是历史 BUG。
    # 现在 panes dict 里所有 key 都参与渲染（含 logs）；focus 那一项默认显示，其他隐藏。
    focus_key = _WB_STAGES[focus][0]
    pane_html = "".join(
        f'<div class="slirn-wb-pane" id="slirn-wb-pane-{key}"{hidden_attr if key != focus_key else ""}>{panes[key]}</div>'
        for key in panes.keys()
    )

    return f'''<div id="slirn-tab-workbench-inner" class="slirn-tab-inner" data-task-id="{_esc(task_id)}">
    <div class="slirn-card slirn-wb-top">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">✂️ 剪辑工作台 · {_esc(t.name)}</div>
            <div>
                <button class="slirn-btn slirn-btn-sm" data-action="refresh-wb" data-task-id="{_esc(task_id)}"
                        title="刷新工作台数据（也可按 F5 / Cmd+R，URL #wb= 自动回到本页）">🔄 刷新</button>
                <button class="slirn-btn slirn-btn-sm slirn-wb-stages-expand" data-action="wb-toggle-stages"
                        title="展开左侧阶段列表">🧭 展开阶段</button>
                <button class="slirn-btn slirn-btn-sm" data-action="edit-task" data-task-id="{_esc(task_id)}">✏️ 编辑任务</button>
                <button class="slirn-btn slirn-btn-sm" data-action="goto-tasks">📋 返回列表</button>
            </div>
        </div>
        {top_rows}
        <!-- REQ-20260920-086：移除顶部「📜 执行历史」折叠卡（日志查看统一走 rail tab + 分页） -->
    </div>
    <!-- 流程配置面板 + 状态条挂载点（REQ-20260918-047，v2：去掉抽屉壳，工作台内常驻纵向面板）-->
    <div id="slirn-pipe-status" class="slirn-pipe-status" data-task-id="{_esc(task_id)}" hidden></div>
    <section id="slirn-pipe-panel" class="slirn-pipe-panel" data-task-id="{_esc(task_id)}"></section>
    <div class="slirn-wb-main">
        <div class="slirn-wb-stages-rail" data-action="wb-toggle-stages"
             title="展开左侧阶段列表"><span>🧭</span><span>阶</span><span>段</span><span>»</span></div>
        <div class="slirn-card slirn-wb-stages">
            <div class="slirn-wb-stages-head"><span class="slirn-wb-stages-title">🧭 阶段</span>
                <button class="slirn-btn-mini" data-action="wb-toggle-stages"
                        title="收起阶段列表，加宽右侧工作区">« 收起</button></div>
            <label class="slirn-wb-autonext" title="勾选后：当前阶段的工作完成（识别/保存/合成完成）时，自动打开下一阶段的页面。不勾选则保持在原页面，由用户手动切到下一阶段。">
                <input type="checkbox" id="slirn-wb-autonext"> 完成后，自动打开下一阶段界面
            </label>
            {stage_items}
        </div>
        <div class="slirn-wb-panes">{pane_html}</div>
    </div>
    </div>'''


# =============== 热词库 ===============

def _render_exec_logs_pane(task_id: str) -> str:
    """REQ-20260918-053 — 工作台「📜 执行日志」面板。

    不在服务端渲染记录（量大 + 需过滤），仅渲染过滤区 + 列表占位；
    由 router.js 在面板首次显示时拉 /slirn/api/list_logs（REQ-20260920-084 切换）。

    REQ-20260920-084：新增 模式 chip（手动/自动）+ 时间段 chip（今天/近 7 天/近 30 天/全部）。
    """
    from slirn_home import execution_history

    kind_chips = "".join(
        f'<button type="button" class="slirn-chip" data-log-kind="{_esc(k)}" '
        f'title="{_esc(v)}">{_esc(v)}</button>'
        for k, v in execution_history.KIND_LABELS.items()
        # REQ-20261028-NNN：长/短视频日志分流 — 长视频工作台只出长视频阶段
        # chips（查询端 flow=long 也只回长视频记录；短视频日志在短视频页看）。
        if not execution_history.is_short_video_kind(k)
    )
    return (
        f'<div class="slirn-wb-pane-card slirn-logs-pane" data-task-id="{_esc(task_id)}">'
        f'  <div class="slirn-wb-pane-title">📜 执行日志 <span class="slirn-logs-count" data-bind="logs-count">--</span></div>'
        f'  <div class="slirn-form-hint slirn-logs-hint">'
        f'    长视频各阶段的执行历史（含字幕生成/字幕修订/切分修剪/粗剪合成/优化字幕/精剪 AI/检测/预览/导出）。短视频的执行日志请到短视频工作台查看。阶段多选；状态/模式/时间段单选；输入关键词搜错误信息。'
        f'  </div>'
        f'  <div class="slirn-logs-filter">'
        f'    <div class="slirn-logs-filter-row">'
        f'      <span class="slirn-logs-filter-label">阶段：</span>{kind_chips}'
        f'      <button type="button" class="slirn-chip slirn-chip-clear" data-action="logs-clear-kinds" title="清除阶段过滤">清除</button>'
        f'    </div>'
        f'    <div class="slirn-logs-filter-row">'
        f'      <span class="slirn-logs-filter-label">状态：</span>'
        f'      <button type="button" class="slirn-chip" data-log-status="success" title="仅看成功的">✅ 成功</button>'
        f'      <button type="button" class="slirn-chip" data-log-status="failed" title="仅看失败的">❌ 失败</button>'
        f'      <button type="button" class="slirn-chip" data-log-status="running" title="仅看运行中的">⏳ 运行中</button>'
        f'      <button type="button" class="slirn-chip slirn-chip-clear" data-action="logs-clear-statuses" title="清除状态过滤">清除</button>'
        f'    </div>'
        f'    <div class="slirn-logs-filter-row">'
        f'      <span class="slirn-logs-filter-label">模式：</span>'
        f'      <button type="button" class="slirn-chip" data-log-auto="any" title="不限（手动 + 自动）">全部</button>'
        f'      <button type="button" class="slirn-chip" data-log-auto="manual" title="仅看手动触发的">👆 手动</button>'
        f'      <button type="button" class="slirn-chip" data-log-auto="auto" title="仅看流程配置自动执行的">⚙ 自动</button>'
        f'    </div>'
        f'    <div class="slirn-logs-filter-row">'
        f'      <span class="slirn-logs-filter-label">时间：</span>'
        f'      <button type="button" class="slirn-chip" data-log-time="today" title="今天 00:00 至今">📅 今天</button>'
        f'      <button type="button" class="slirn-chip" data-log-time="7d" title="近 7 天">🗓 近 7 天</button>'
        f'      <button type="button" class="slirn-chip" data-log-time="30d" title="近 30 天">📆 近 30 天</button>'
        f'      <button type="button" class="slirn-chip" data-log-time="all" title="不限时间">∞ 全部</button>'
        f'    </div>'
        f'    <div class="slirn-logs-filter-row">'
        f'      <input type="text" class="slirn-input slirn-logs-keyword" id="slirn-logs-keyword" '
        f'             placeholder="🔍 搜错误信息或阶段（不区分大小写）" />'
        f'      <button type="button" class="slirn-btn slirn-btn-sm" data-action="logs-refresh" title="按当前过滤条件重新查询">🔄 刷新</button>'
        f'    </div>'
        f'  </div>'
        f'  <div class="slirn-logs-list" id="slirn-logs-list" data-task-id="{_esc(task_id)}">'
        f'    <div class="slirn-form-hint slirn-logs-empty">尚未查询。点击「🔄 刷新」或切换过滤条件自动加载。</div>'
        f'  </div>'
        # REQ-20260920-086：分页导航（每页 20 条，含首页/上一页/下一页/末页 + 当前页/总页数/总条数 + 每页大小下拉）
        f'  <div class="slirn-logs-pager" id="slirn-logs-pager" data-page="1" data-page-size="20" data-total="0">'
        f'    <button type="button" class="slirn-pager-btn" data-pager="first" disabled title="首页">«</button>'
        f'    <button type="button" class="slirn-pager-btn" data-pager="prev" disabled title="上一页">‹</button>'
        f'    <span class="slirn-pager-info">第 <span data-bind="page">1</span> / <span data-bind="total-pages">1</span> 页 · 共 <span data-bind="total">0</span> 条</span>'
        f'    <button type="button" class="slirn-pager-btn" data-pager="next" disabled title="下一页">›</button>'
        f'    <button type="button" class="slirn-pager-btn" data-pager="last" disabled title="末页">»</button>'
        f'    <select class="slirn-pager-size" data-pager="size" title="每页条数">'
        f'      <option value="10">10 条/页</option>'
        f'      <option value="20" selected>20 条/页</option>'
        f'      <option value="50">50 条/页</option>'
        f'      <option value="100">100 条/页</option>'
        f'    </select>'
        f'  </div>'
        f'</div>'
    )

def _render_exec_history_card(task_id: str, mgr) -> str:
    """REQ-20260918-048 — 工作台顶部的执行历史折叠卡片（字幕生成 + 粗剪合成）。

    读 outputs/execution_history.json 倒序最多 10 条；落盘数据，无需实时刷新。
    复用 REQ-20260918-044 的 .slirn-col 折叠壳：JS 自动 wrap，零新逻辑。
    """
    from slirn_home import execution_history

    outputs_dir = mgr.tasks_dir / task_id / "outputs"
    items = execution_history.load_history(outputs_dir)
    # REQ-20261028-NNN：长/短视频日志分流 — 长视频工作台的卡片只列长视频记录
    items = [it for it in items
             if not execution_history.is_short_video_kind(it.get("kind"))]
    # 倒序：最新在前；空文件 → 友好空态
    items.sort(key=lambda x: (float(x.get("started_at") or 0), str(x.get("id") or "")), reverse=True)
    total = len(items)
    shown = items[:10]

    if not shown:
        body = ('<div class="slirn-form-hint slirn-exec-empty">'
                '尚无执行记录。点击「字幕生成 / 粗剪合成」开始第一次执行后，这里会出现历史。'
                '</div>')
    else:
        KIND_LABEL = {
            execution_history.KIND_SUBTITLE_GENERATION: ("🎙 字幕生成", "字幕生成"),
            execution_history.KIND_ROUGH_COMPOSE: ("🎥 粗剪合成", "粗剪合成"),
        }
        rows = []
        # 倒序：shown[0] 是最近一次；真实序号 = total - i（最大 = 最新）
        for i_row, it in enumerate(shown):
            real_n = total - i_row
            kind_icon, _kind_text = KIND_LABEL.get(it.get("kind") or "", ("⚙ 操作", "操作"))
            status = it.get("status") or "running"
            if status == "success":
                badge = '<span class="slirn-exec-ok">✅ 成功</span>'
            elif status == "failed":
                badge = '<span class="slirn-exec-fail">❌ 失败</span>'
            else:
                badge = '<span class="slirn-exec-running">⏳ 运行中</span>'
            start_iso = (it.get("started_at_iso") or "").replace("T", " ")[:19]
            end_iso = (it.get("finished_at_iso") or "").replace("T", " ")[:19] or "—"
            duration = execution_history.format_duration(it.get("duration_ms"))
            # extra 摘要：subtitle → segments/speakers；compose → segments/output
            extra = it.get("extra") or {}
            extra_bits = []
            if "segments" in extra:
                extra_bits.append(f"{extra['segments']} 段")
            if extra.get("speakers"):
                extra_bits.append(f"{extra['speakers']} 位说话人")
            if extra.get("intervals"):
                extra_bits.append(f"{extra['intervals']} 区间")
            extra_str = (" · " + " / ".join(extra_bits)) if extra_bits else ""
            err_html = ""
            if it.get("error"):
                err_html = (f'<div class="slirn-exec-err">{_esc(str(it["error"])[:300])}</div>')
            rows.append(
                f'<div class="slirn-exec-row" data-status="{_esc(status)}">'
                f'  <span class="slirn-exec-idx">#{real_n}</span>'
                f'  <span class="slirn-exec-kind">{_esc(kind_icon)} · {_esc(start_iso)} → {_esc(end_iso)}</span>'
                f'  <span class="slirn-exec-dur">{_esc(duration)}</span>'
                f'  {badge}{extra_str}'
                f'  {err_html}'
                f'</div>'
            )

        more = (f'<div class="slirn-form-hint">共 {total} 次记录，仅展示最近 {len(shown)} 次</div>'
                if total > len(shown) else '')
        body = "".join(rows) + more

    # 折叠壳：与 REQ-20260918-044 的 .slirn-col 自动 wrap 兼容；key 用 exec:history
    return (
        f'<div class="slirn-col slirn-exec-card" data-col-key="exec:history">'
        f'  <div class="slirn-col-head"><button type="button" class="slirn-col-btn" data-action="col-toggle">'
        f'    <span class="slirn-col-chev">▾</span>📜 执行历史'
        f'    <span class="slirn-exec-count">{total} 次</span>'
        f'  </button></div>'
        f'  <div class="slirn-exec-body">{body}</div>'
        f'</div>'
    )


def _render_hotword_picker(repo_root: Path) -> str:
    """渲染公共库的「选择器」网格（用于「新建任务」页：点击切换选中状态，无 X / 无批量按钮）。"""
    hwlib = HotwordLibrary(repo_root)
    grouped = hwlib.list_grouped()

    sections = ""
    for cat, words in grouped.items():
        cells = ""
        for w in words:
            cells += (
                f'<div class="slirn-hotword-cell slirn-pick-cell" data-word="{_esc(w)}">'
                f'  <span class="slirn-cell-text">{_esc(w)}</span>'
                f'</div>'
            )
        for _ in range(len(words) % 5):
            cells += '<div class="slirn-hotword-cell empty"></div>'
        cat_id = _esc(cat)
        sections += f'''<div class="slirn-category-section slirn-pick-section" data-category="{cat_id}">
            <div class="slirn-category-title">
                <span class="slirn-cat-name">{cat_id} <span class="slirn-category-count">{len(words)}</span></span>
                <button class="slirn-btn-mini" data-action="hw-cat-select-all" title="全选该分类所有热词">☑ 全选</button>
                <button class="slirn-btn-mini" data-action="hw-cat-invert" title="反选该分类所有热词">⇄ 反选</button>
            </div>
            <div class="slirn-hotword-grid">{cells}</div>
        </div>'''
    if not sections:
        sections = '<div class="slirn-empty"><div class="slirn-empty-text">公共热词库为空 — 先去「📚 热词库」添加</div></div>'
    return sections

# REQ-20260926-NNN：AI 热词分析面板（输入框 + URL + 结果候选 + 加入任务/公共库）
#   REQ-20260926-NNN-b：默认折叠，需要时点 summary 展开
def _render_hotword_analysis_section() -> str:
    return (
        '<details class="slirn-hw-analysis-collapsible" style="margin-top:14px;">'
        '  <summary>🤖 AI 热词分析 <small style="opacity:.6;font-weight:400;">— 点此展开（默认折叠以保持页面紧凑）</small></summary>'
        '  <div class="slirn-form-row" style="padding-top:10px;">'
        '    <textarea class="slirn-textarea" id="slirn-hw-analysis-requirement" rows="2"'
        '              placeholder="描述需求 / 主题，例如：这是一档讲 RAG 与向量数据库的播客，主持人叫张老师"></textarea>'
        '    <input class="slirn-input" id="slirn-hw-analysis-url" style="margin-top:8px;"'
        '           placeholder="（可选）粘贴文章 / 字幕 URL，AI 会读取内容一起分析" />'
        '    <input class="slirn-input" id="slirn-hw-analysis-hint" style="margin-top:8px;"'
        '           placeholder="（可选）页面区域 / 选择器 / 文字说明 — 例如 #article, .post-content, 或「找正文部分」" />'
        '    <div style="display:flex; gap:8px; margin-top:8px; align-items:center;">'
        '      <button class="slirn-btn slirn-btn-primary" data-action="hw-analyze">🔍 分析</button>'
        '      <span class="slirn-status-msg" id="slirn-hw-analysis-status" style="flex:1;"></span>'
        '    </div>'
        '    <div class="slirn-form-hint" style="margin-top:10px;">✅ 候选（点击选中要采纳的词）</div>'
        '    <div class="slirn-hotword-grid" id="slirn-hw-analysis-grid"></div>'
        '    <div style="display:flex; gap:8px; margin-top:10px;">'
        '      <button class="slirn-btn slirn-btn-primary" data-action="hw-analysis-add-task">➕ 选中的加入任务</button>'
        '      <button class="slirn-btn" data-action="hw-analysis-add-public">📚 选中的加入公共库</button>'
        '    </div>'
        '  </div>'
        '</details>'
    )

def _render_hotword_lib(repo_root: Path) -> str:
    hwlib = HotwordLibrary(repo_root)
    grouped = hwlib.list_grouped()

    categories_html = ""
    for cat, words in grouped.items():
        cells = ""
        for w in words:
            cells += (
                f'<div class="slirn-hotword-cell" data-word="{_esc(w)}">'
                f'  <span class="slirn-cell-text">{_esc(w)}</span>'
                f'  <button class="slirn-cell-delete" data-action="hw-delete-one" data-word="{_esc(w)}" title="删除此词">✕</button>'
                f'</div>'
            )
        # 补齐空白（让网格保持 5 列对齐）
        for _ in range(len(words) % 5):
            cells += '<div class="slirn-hotword-cell empty"></div>'
        cat_id = _esc(cat)
        categories_html += f'''<div class="slirn-category-section" data-category="{cat_id}">
            <div class="slirn-category-title">
                <span class="slirn-cat-name">{cat_id} <span class="slirn-category-count">{len(words)}</span></span>
                <div class="slirn-category-actions">
                    <button class="slirn-btn-mini" data-action="hw-cat-select-all">☑ 全选</button>
                    <button class="slirn-btn-mini" data-action="hw-cat-invert">⇄ 反选</button>
                    <button class="slirn-btn-mini slirn-btn-mini-danger" data-action="hw-cat-delete">🗑 删除选中</button>
                </div>
            </div>
            <div class="slirn-hotword-grid">{cells}</div>
        </div>'''

    if not categories_html:
        categories_html = '<div class="slirn-empty"><div class="slirn-empty-icon">📚</div><div class="slirn-empty-text">公共热词库为空 — 在下方添加第一个词</div></div>'

    return f'''<div class="slirn-card">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">📚 现有词（按分类）</div>
            <span class="slirn-panel-subtitle">{_esc(sum(len(v) for v in grouped.values()))} 个词 · {_esc(len(grouped))} 个分类 · 鼠标移上去显示 ✕ 可单删</span>
        </div>
        {categories_html}
    </div>
    <div class="slirn-card">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">➕ 添加新词</div>
        </div>
        <div class="slirn-form-grid">
            <div class="slirn-form-row">
                <label class="slirn-form-label">词</label>
                <textarea class="slirn-input" id="slirn-hw-word" rows="3" placeholder="例如：张老师（多个用半角逗号或换行分隔）"></textarea>
            </div>
            <div class="slirn-form-row">
                <label class="slirn-form-label">分类</label>
                <input class="slirn-input" id="slirn-hw-category" placeholder="例如：讲师（留空 = 默认）" />
            </div>
        </div>
        <div class="slirn-form-hint">💡 支持一次添加多个词（半角逗号或换行分隔），已存在的词会自动跳过</div>
        <button class="slirn-btn slirn-btn-primary" data-action="hw-add" style="margin-top:12px;">➕ 添加</button>
        <div class="slirn-status-msg" id="slirn-hw-status" style="display:none;"></div>
    </div>'''


# =============== 新建任务 ===============

def _render_create_task(repo_root: Path, edit=None) -> str:
    """新建任务页；edit 传入 tasklib.models.Task 时渲染为「编辑任务」模式（REQ-20260915-003）：
    预填原视频信息（不可换视频）、截取时间、任务名、热词三来源状态。"""
    picker_html = _render_hotword_picker(repo_root)

    if edit is not None:
        # ---- 编辑模式预填 ----
        from tasklib.video import get_video_duration
        src = edit.original_video_source
        dur = get_video_duration(src) if src.exists() else None
        dur_str = ""
        if dur:
            _mm, _ss = divmod(int(dur), 60)
            _hh, _mm = divmod(_mm, 60)
            dur_str = f"{_hh:02d}:{_mm:02d}:{_ss:02d}"
        size_mb = round(src.stat().st_size / 1024 / 1024, 1) if src.exists() else 0
        # 公共库选中态：来源为 pick 的词在网格里预亮（直接加 .selected，JS 端照常收集）
        sources = edit.hotword_sources or {}
        if sources:
            missing_picked: list[str] = []
            for w in sorted(sources):
                if sources[w] != "pick":
                    continue
                marker = f'data-word="{_esc(w)}"'
                fresh = (
                    f'<div class="slirn-hotword-cell slirn-pick-cell" {marker}>',
                    f'<div class="slirn-hotword-cell slirn-pick-cell selected" {marker}>',
                )
                if fresh[0] in picker_html:
                    picker_html = picker_html.replace(fresh[0], fresh[1])
                else:
                    missing_picked.append(w)  # 词已不在公共库（被删）→ 不能丢，追加专属分区
            if missing_picked:
                cells = "".join(
                    f'<div class="slirn-hotword-cell slirn-pick-cell selected" data-word="{_esc(w)}">'
                    f'  <span class="slirn-cell-text">{_esc(w)}</span></div>'
                    for w in missing_picked
                )
                cat = "任务已选（不在公共库）"
                picker_html += (
                    f'<div class="slirn-category-section slirn-pick-section" data-category="{cat}">'
                    f'<div class="slirn-category-title"><span class="slirn-cat-name">{cat} '
                    f'<span class="slirn-category-count">{len(missing_picked)}</span></span></div>'
                    f'<div class="slirn-hotword-grid">{cells}</div></div>'
                )
        manual_words = [w for w in (edit.hotwords_path.read_text(encoding="utf-8").split()
                                    if edit.hotwords_path.exists() else []) if sources.get(w) == "manual"]
        # REQ-20260926-NNN：回填 manual textarea 用「半角逗号+换行」拼接
        # （不能用空格，否则 separator 改后多词热词被塌缩成一个）
        manual_text_value = ("," + chr(10)).join(manual_words)
        return f'''<div id="slirn-tab-create-inner" class="slirn-tab-inner">
    <div id="slirn-edit-state"
         data-task-id="{_esc(edit.task_id)}"
         data-video-path="{_esc(str(src).replace(chr(92), '/'))}"
         data-video-url="/slirn/api/video/{_esc(edit.task_id)}?src=original"
         data-duration-seconds="{float(dur) if dur else 0}"></div>
    <div class="slirn-card" style="margin-bottom:14px;">
        <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;">
            <div class="slirn-form-hint" style="margin:0;">
                正在编辑任务 <strong>{_esc(edit.name)}</strong>（{_esc(edit.task_id)}）— 可直接跳到剪辑工作台查看/操作各阶段
            </div>
            <button class="slirn-btn slirn-btn-primary"
                    data-action="open-workbench"
                    data-task-id="{_esc(edit.task_id)}"
                    data-task-label="{_esc(edit.name)}">✂️ 进入剪辑工作台 →</button>
        </div>
    </div>
    <div class="slirn-card">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">📁 步骤 1 · 原视频（编辑模式，不可更换）</div>
        </div>
        <div class="slirn-status-msg">📁 {_esc(src.name)}（{size_mb} MB · {dur_str or "时长未知"}）</div>
        <div class="slirn-form-hint" style="margin-top:6px;">如需更换视频请新建任务</div>
    </div>

    <div class="slirn-card" id="slirn-player-card" style="display:none;">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">🎥 步骤 2 · 视频预览 + 时间设置</div>
            <span class="slirn-panel-subtitle">拖动滑块定位 · 点击按钮设置开始/结束</span>
        </div>
        <div class="slirn-video-wrap">
            <video id="slirn-player" controls preload="metadata"></video>
        </div>
        <div class="slirn-slider-wrap">
            <input type="range" class="slirn-slider" id="slirn-seek" min="0" max="600" step="0.1" value="0" />
            <div class="slirn-time-display" id="slirn-time-display">00:00:00.000 / 00:00:00.000</div>
        </div>
        <div style="display:flex; gap:8px; margin-top:12px;">
            <button class="slirn-btn slirn-btn-primary" data-action="set-start">⏱ 设为开始</button>
            <button class="slirn-btn slirn-btn-primary" data-action="set-end">⏱ 设为结束</button>
        </div>
        <div class="slirn-form-grid" style="margin-top:16px;">
            <div class="slirn-form-row">
                <label class="slirn-form-label">开始时间 (HH:MM:SS.mmm)</label>
                <input class="slirn-input" id="slirn-start-box" placeholder="00:00:00.000" value="{_esc(edit.segment.start if edit.segment else '')}" />
            </div>
            <div class="slirn-form-row">
                <label class="slirn-form-label">结束时间 (HH:MM:SS.mmm)</label>
                <input class="slirn-input" id="slirn-end-box" placeholder="00:00:00.000" value="{_esc(edit.segment.end if edit.segment else '')}" />
            </div>
        </div>
        <button class="slirn-btn slirn-btn-primary" data-action="cut-preview" style="margin-top:8px;">🎬 待剪辑视频预览</button>
        <div id="slirn-cut-msg" style="margin-top:12px;"></div>
        <div id="slirn-cut-preview-wrap" style="display:none; margin-top:12px;">
            <div class="slirn-video-wrap">
                <video id="slirn-cut-preview" controls preload="metadata"></video>
            </div>
        </div>
    </div>

    <div class="slirn-card">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">📝 步骤 3 · 任务信息</div>
        </div>
        <div class="slirn-form-row">
            <label class="slirn-form-label">任务名（留空 = 文件名）</label>
            <input class="slirn-input" id="slirn-task-name" placeholder="例如：讲师介绍视频" value="{_esc(edit.name)}" />
        </div>
        <div class="slirn-form-row">
            <label class="slirn-form-label">🔥 任务级热词（3 种来源可叠加）</label>

            <label class="slirn-hw-inherit-row" for="slirn-hw-inherit-all">
                <input type="checkbox" id="slirn-hw-inherit-all" {'checked' if edit.inherit_public else ''} />
                <span>① <strong>继承公共库所有热词</strong>（无需逐个勾选 · 公共库新增自动生效）</span>
            </label>

            <div class="slirn-form-hint" style="margin-top:10px;">
                ② <strong>从公共库选择</strong>（仅在未勾选「继承」时显示 · 点击切换）
            </div>
            <div id="slirn-hw-picker-wrap">
                <div id="slirn-hw-picker">
                    {picker_html}
                </div>
            </div>

            <div class="slirn-form-hint" style="margin-top:14px;">③ <strong>手动输入</strong>（半角逗号或换行分隔 · 与上面两个叠加）</div>
            <textarea class="slirn-textarea" id="slirn-hotwords-manual" rows="3" placeholder="手动输入热词（多个用半角逗号或换行分隔）">{_esc(manual_text_value)}</textarea>
            {_render_hotword_analysis_section()}

            <div class="slirn-form-hint" style="margin-top:14px;">✅ <strong>最终生效的热词</strong>（点击 ✕ 可移除）</div>
            <div class="slirn-hw-chips" id="slirn-hw-chips"></div>
            <div class="slirn-hw-summary" id="slirn-hw-summary"></div>
        </div>
    </div>

    <!-- REQ-20260926-NNN：成员管理（多用户协作）— 成员列表由 JS 拉成员端点动态渲染 -->
    <details class="slirn-members-section" id="slirn-members-section"
             data-task-id="{_esc(edit.task_id)}"
             data-created-by="{_esc(edit.created_by or '')}">
        <summary>👥 成员管理（多用户协作）</summary>
        <div class="slirn-form-hint" style="margin:8px 0 4px;">
            创建者与 admin 始终可访问；成员可查看 / 编辑该任务并出现在其「我的任务」里。
        </div>
        <div id="slirn-member-list"></div>
        <div style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap;">
            <input class="slirn-input" id="slirn-member-add-name" placeholder="要添加的用户名"
                   style="flex:1; min-width:160px;" />
            <button class="slirn-btn" data-action="member-add"
                    data-task-id="{_esc(edit.task_id)}">➕ 添加成员</button>
        </div>
        <div id="slirn-member-msg" class="slirn-form-hint" style="margin-top:6px;"></div>
    </details>

    <div style="display:flex; gap:12px;">
        <button class="slirn-btn slirn-btn-primary" data-action="update-task" data-task-id="{_esc(edit.task_id)}" style="flex:1;">💾 保存修改</button>
        <button class="slirn-btn slirn-btn-danger" data-action="cancel-create">❌ 取消</button>
    </div>
    <div id="slirn-create-msg" style="margin-top:12px;"></div>
    </div>'''

    return f'''<div id="slirn-tab-create-inner" class="slirn-tab-inner">
    <div class="slirn-card">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">📁 步骤 1 · 选择视频</div>
        </div>
        <div class="slirn-dropzone" data-action="trigger-file">
            <div class="slirn-dropzone-icon">📂</div>
            <div>点击选择视频文件 · 或把视频拖到这里</div>
            <div class="slirn-form-hint">支持 mp4 / avi / mkv / mov / webm / ts / mpeg（最大 10 GB）</div>
        </div>
        <div id="slirn-file-info-display" style="margin-top:12px;"></div>
    </div>

    <div class="slirn-card" id="slirn-player-card" style="display:none;">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">🎥 步骤 2 · 视频预览 + 时间设置</div>
            <span class="slirn-panel-subtitle">拖动滑块定位 · 点击按钮设置开始/结束</span>
        </div>
        <div class="slirn-video-wrap">
            <video id="slirn-player" controls preload="metadata"></video>
        </div>
        <div class="slirn-slider-wrap">
            <input type="range" class="slirn-slider" id="slirn-seek" min="0" max="600" step="0.1" value="0" />
            <div class="slirn-time-display" id="slirn-time-display">00:00:00.000 / 00:00:00.000</div>
        </div>
        <div style="display:flex; gap:8px; margin-top:12px;">
            <button class="slirn-btn slirn-btn-primary" data-action="set-start">⏱ 设为开始</button>
            <button class="slirn-btn slirn-btn-primary" data-action="set-end">⏱ 设为结束</button>
        </div>
        <div class="slirn-form-grid" style="margin-top:16px;">
            <div class="slirn-form-row">
                <label class="slirn-form-label">开始时间 (HH:MM:SS.mmm)</label>
                <input class="slirn-input" id="slirn-start-box" placeholder="00:00:00.000" />
            </div>
            <div class="slirn-form-row">
                <label class="slirn-form-label">结束时间 (HH:MM:SS.mmm)</label>
                <input class="slirn-input" id="slirn-end-box" placeholder="00:00:00.000" />
            </div>
        </div>
        <button class="slirn-btn slirn-btn-primary" data-action="cut-preview" style="margin-top:8px;">🎬 待剪辑视频预览</button>
        <div id="slirn-cut-msg" style="margin-top:12px;"></div>
        <div id="slirn-cut-preview-wrap" style="display:none; margin-top:12px;">
            <div class="slirn-video-wrap">
                <video id="slirn-cut-preview" controls preload="metadata"></video>
            </div>
        </div>
    </div>

    <div class="slirn-card">
        <div class="slirn-panel-header">
            <div class="slirn-panel-title">📝 步骤 3 · 任务信息</div>
        </div>
        <div class="slirn-form-row">
            <label class="slirn-form-label">任务名（留空 = 文件名）</label>
            <input class="slirn-input" id="slirn-task-name" placeholder="例如：讲师介绍视频" />
        </div>
        <div class="slirn-form-row">
            <label class="slirn-form-label">🔥 任务级热词（3 种来源可叠加）</label>

            <label class="slirn-hw-inherit-row" for="slirn-hw-inherit-all">
                <input type="checkbox" id="slirn-hw-inherit-all" />
                <span>① <strong>继承公共库所有热词</strong>（无需逐个勾选 · 公共库新增自动生效）</span>
            </label>

            <div class="slirn-form-hint" style="margin-top:10px;">
                ② <strong>从公共库选择</strong>（仅在未勾选「继承」时显示 · 点击切换）
            </div>
            <div id="slirn-hw-picker-wrap">
                <div id="slirn-hw-picker">
                    {picker_html}
                </div>
            </div>

            <div class="slirn-form-hint" style="margin-top:14px;">③ <strong>手动输入</strong>（半角逗号或换行分隔 · 与上面两个叠加）</div>
            <textarea class="slirn-textarea" id="slirn-hotwords-manual" rows="3" placeholder="手动输入热词（多个用半角逗号或换行分隔）"></textarea>
            {_render_hotword_analysis_section()}

            <div class="slirn-form-hint" style="margin-top:14px;">✅ <strong>最终生效的热词</strong>（点击 ✕ 可移除）</div>
            <div class="slirn-hw-chips" id="slirn-hw-chips"></div>
            <div class="slirn-hw-summary" id="slirn-hw-summary"></div>
        </div>
    </div>

    <div style="display:flex; gap:12px;">
        <button class="slirn-btn slirn-btn-primary" data-action="create-task" style="flex:1;">✅ 创建任务</button>
        <button class="slirn-btn slirn-btn-danger" data-action="cancel-create">❌ 取消</button>
    </div>
    <div id="slirn-create-msg" style="margin-top:12px;"></div>
    </div>'''


# ============================================================
# JS 路由：捕获自定义 HTML 元素的点击 → 转发到 Gradio hidden button
# ============================================================

ROUTER_JS = '<script defer src="/slirn/static/router.js"></script>'
# REQ-20260918-047：流程配置 + 自动执行 JS（与 router.js 同模式走静态文件）
PIPELINE_JS = '<script defer src="/slirn/static/pipeline.js"></script>'
SHORT_VIDEO_JS = '<script defer src="/slirn/static/short_video.js"></script>'
# REQ-20261022-143：短视频流程配置面板 + 自动执行
SHORT_VIDEO_FLOW_JS = '<script defer src="/slirn/static/short_video_flow.js"></script>'
# REQ-20261022-146：硬件按键（Web MIDI → 合成键盘事件，桥接既有快捷键）
HARDWARE_KEYS_JS = '<script defer src="/slirn/static/hardware_keys.js"></script>'
# 说明：ROUTER_JS 原本是 ~115KB 的内联 JS 字符串，通过 gr.HTML(head=...) 注入。
# Gradio 6.17.3 的 head 传输链路会把脚本文本里的反斜杠转义解码
# （\n -> 换行、\x20 -> 空格、\\ -> \、孤立的 \ 被删除），内联 JS 因此产生
# SyntaxError 而整体失效（页面所有 data-action 按钮无响应，2026-09-17 排查）。
# 现在改为：JS 源码放 static/router.js，由 _register_slirn_api 注册的
# GET /slirn/static/router.js 提供，这里只注入 <script src>（无反斜杠，安全）。


# ============================================================
# CSS 注入
# ============================================================

def _read_css() -> str:
    """读取 home.css — 用 launch(css=...) 注入，因为 Gradio 6.x 会用 _deprecated_css 覆盖 app.css。"""
    css_path = Path(__file__).parent / "static" / "home.css"
    return css_path.read_text(encoding="utf-8")


def _build_topbar(repo_root: str) -> str:
    """REQ-20261013-101：顶栏按钮组。⌨ 快捷键在热词库前面（统一设置入口）。

    全部按钮都是 data-action 驱动的客户端事件，pure HTML，测试可直接拼装。
    """
    return (
        '<div class="slirn-topbar">'
        '<div class="slirn-logo">'
        '<div class="slirn-logo-icon">🎬</div>'
        '<span>Video Studio</span>'
        '</div>'
        '<div class="slirn-topbar-actions">'
        # REQ-20261022-145：设置项收进「⚙ 设置」下拉（与「帮助」同构）
        '<div class="slirn-topbar-menu">'
        '<button class="slirn-btn" data-action="settings-menu-toggle" aria-haspopup="menu" '
        'aria-expanded="false" title="快捷键 / 热词库 / 大模型设置 / 登录日志">⚙ 设置 ▾</button>'
        '<div class="slirn-topbar-dropdown" id="slirn-settings-dropdown" role="menu" hidden>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="revkeys-open" role="menuitem">⌨ 快捷键</button>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="goto-hotwords" role="menuitem">📚 热词库</button>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="goto-usage" role="menuitem">📊 Token 用量</button>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="open-llm-settings" role="menuitem">大模型设置</button>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="open-sys-settings" role="menuitem">系统设置</button>'
        '<div class="slirn-topbar-menu-divider"></div>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="loginlog-open" role="menuitem">📜 登录日志</button>'
        '</div></div>'
        # 主题切换保持最外层（与「设置」「帮助」同级）
        '<button class="slirn-btn-icon" onclick="window.slirnToggleTheme && window.slirnToggleTheme()" aria-label="切换主题" title="切换主题（亮/暗）">🌓</button>'
        # 帮助下拉（使用说明 + 导入默认参数）
        '<div class="slirn-topbar-menu">'
        '<button class="slirn-btn" data-action="help-menu-toggle" aria-haspopup="menu" '
        'aria-expanded="false" title="帮助与默认参数">❓ 帮助 ▾</button>'
        '<div class="slirn-topbar-dropdown" id="slirn-help-dropdown" role="menu" hidden>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="help-long" role="menuitem">📖 长视频使用帮助</button>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="help-short" role="menuitem">📖 短视频使用帮助</button>'
        '<div class="slirn-topbar-menu-divider"></div>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="import-long-params" role="menuitem">📥 导入长视频参数</button>'
        '<button type="button" class="slirn-topbar-menu-item" data-action="import-short-params" role="menuitem">📥 导入短视频参数</button>'
        '</div></div>'
        '<span class="slirn-current-user" id="slirn-current-user" style="display:none;">'
        '👤 <span id="slirn-current-user-name"></span>'
        '<button class="slirn-btn-mini" data-action="users-open" id="slirn-users-open-btn" style="display:none;" title="用户管理">用户管理</button>'
        '<button class="slirn-btn-mini" data-action="logout">登出</button>'
        '</span>'
        '<button class="slirn-btn-icon" data-action="show-login" id="slirn-login-btn" title="登录">🔑</button>'
        '</div>'
        '</div>'
    )


def _build_head() -> str:
    """构造 head 注入：主题切换 JS（内联，无反斜杠，安全）+ 事件路由 JS（静态文件引用）。

    注意：head= 传输链路会解码反斜杠转义，任何含 \\ 的内联 JS 都会被破坏 —— 见 ROUTER_JS 注释。
    """
    theme_js = """
    <script>
    (function() {
      function applyTheme(t) {
        document.documentElement.setAttribute('data-theme', t);
        try { localStorage.setItem('slirn-theme', t); } catch(e) {}
      }
      window.slirnToggleTheme = function() {
        var cur = document.documentElement.getAttribute('data-theme');
        applyTheme(cur === 'dark' ? 'light' : 'dark');
      };
      var saved = null;
      try { saved = localStorage.getItem('slirn-theme'); } catch(e) {}
      if (saved) applyTheme(saved);
    })();
    </script>
    """
    return (theme_js + ROUTER_JS + PIPELINE_JS + SHORT_VIDEO_JS + SHORT_VIDEO_FLOW_JS
            + HARDWARE_KEYS_JS)


def _inject_css_and_js(app: gr.Blocks) -> None:
    """老接口兼容 — 仍设置 app.css 但 launch() 会用 _deprecated_css 覆盖，外部需读 slirn_home_static_css。

    不再设置 app.head — Gradio 6.17.3 的 Blocks.head 不会渲染到页面 <head>。
    JS 注入走 gr.HTML 组件的 head= 参数（见 build_app）：theme_js 内联（无反斜杠），
    事件路由 JS 用 <script src="/slirn/static/router.js"> 静态文件（head= 传输会破坏反斜杠）。
    """
    css_path = Path(__file__).parent / "static" / "home.css"
    app.css = css_path.read_text(encoding="utf-8")


# 暴露给 launcher 直接读
slirn_home_static_css = lambda: _read_css()  # noqa: E731
slirn_home_static_head = lambda: _build_head()  # noqa: E731


# ============================================================
# 主构造
# ============================================================

def build_app(repo_root: Path | None = None) -> gr.Blocks:
    if repo_root is None:
        from slirn_home.paths import find_slirn_standalone_root
        repo_root = find_slirn_standalone_root()

    mgr = TaskManager(repo_root)
    auth = _AuthStore(repo_root)                      # REQ-20260926-NNN：用户/会话/成员扁平文件存储

    app = gr.Blocks(title="Video Studio — 自定义首页")
    _inject_css_and_js(app)
    # 放宽 Gradio 默认上传大小限制到 10 GB（原默认很小，会截断大视频）
    app.max_file_size = 10 * 1024 * 1024 * 1024  # bytes

    # REQ-20260926-NNN 修复：启动期不再渲染任务/仪表盘数据 — 初始 HTML 只放
    # 骨架占位（所有访客拿到同一份 HTML，真实数据由前端确认登录态后按用户拉取），
    # 否则全部任务会随页面源码泄漏给未登录访客和普通用户。
    # 单个 gr.HTML 包裹全部 UI — JS 通过 ID 控制各 tab 的可见性
    full_html = f'''
    {_build_topbar(str(repo_root))}

    <div class="slirn-hero">
        <div class="slirn-hero-text">
            <h1>欢迎回来 👋</h1>
            <p>登录后按你的权限管理任务 · 仓库 <code>{_esc(repo_root)}</code></p>
        </div>
        <div class="slirn-hero-actions">
            <button class="slirn-btn" data-action="goto-short-video">🎞 短视频列表</button>
            <button class="slirn-btn" data-action="goto-tasks">📋 长视频列表</button>
        </div>
    </div>

    <div id="slirn-tab-dashboard" class="slirn-tab-content">{_render_dashboard_skeleton()}</div>
    <div id="slirn-tab-tasks" class="slirn-tab-content" style="display:none;">{_render_task_list_skeleton()}</div>
    <div id="slirn-tab-create" class="slirn-tab-content" style="display:none;">{_render_create_task(repo_root)}</div>
    <div id="slirn-tab-hotwords" class="slirn-tab-content" style="display:none;">{_render_hotword_lib(repo_root)}</div>
    <div id="slirn-tab-short-video" class="slirn-tab-content" style="display:none;">
        <div id="slirn-short-video-inner" class="slirn-tab-inner"><div class="slirn-empty">短视频列表加载中…</div></div>
    </div>
    <div id="slirn-tab-usage" class="slirn-tab-content" style="display:none;">
        <div class="slirn-tab-inner">
            <div class="slirn-card">
                <div class="slirn-panel-header">
                    <div class="slirn-panel-title">📊 Token 用量统计</div>
                    <div class="slirn-panel-subtitle">全工程大模型调用的输入 / 输出 token 累计与逐笔明细</div>
                </div>
                <div id="slirn-usage-summary">汇总加载中…</div>
            </div>
            <div class="slirn-card" style="margin-top:14px;">
                <div class="slirn-panel-header">
                    <div class="slirn-panel-title">📋 调用明细</div>
                    <div style="display:flex; gap:8px;">
                        <button class="slirn-btn" data-action="usage-page-filter">🔍 筛选</button>
                        <button class="slirn-btn" data-action="usage-page-reset">↺ 重置</button>
                    </div>
                </div>
                <div class="slirn-usage-fltbar">
                    <input class="slirn-input slirn-usage-flt" id="slirn-usage-filter" placeholder="任务 ID（留空=全部）" style="width:220px;" />
                    <select class="slirn-input slirn-usage-flt" id="slirn-usage-model"><option value="">模型（全部）</option></select>
                    <select class="slirn-input slirn-usage-flt" id="slirn-usage-stage"><option value="">阶段（全部）</option></select>
                    <input type="date" class="slirn-input slirn-usage-flt" id="slirn-usage-from" title="起始日期（含当天）" />
                    <span class="slirn-form-hint">→</span>
                    <input type="date" class="slirn-input slirn-usage-flt" id="slirn-usage-to" title="截止日期（含当天）" />
                    <input type="number" min="0" class="slirn-input slirn-usage-flt" id="slirn-usage-dmin" placeholder="耗时≥(秒)" style="width:100px;" />
                    <span class="slirn-form-hint">~</span>
                    <input type="number" min="0" class="slirn-input slirn-usage-flt" id="slirn-usage-dmax" placeholder="≤(秒)" style="width:90px;" />
                </div>
                <div id="slirn-usage-detail">明细加载中…</div>
            </div>
        </div>
    </div>
    <div id="slirn-tab-detail" class="slirn-tab-content" style="display:none;"></div>
    <div id="slirn-tab-workbench" class="slirn-tab-content" style="display:none;"></div>

    <!-- REQ-20260926-NNN：登录 modal（默认隐藏） -->
    <div class="slirn-modal" id="slirn-login-modal" style="display:none;">
        <div class="slirn-modal-card">
            <h3>🔑 登录</h3>
            <div class="slirn-form-row">
                <label class="slirn-form-label">用户名</label>
                <input class="slirn-input" id="slirn-login-username" placeholder="用户名" autocomplete="username">
            </div>
            <div class="slirn-form-row">
                <label class="slirn-form-label">密码</label>
                <input class="slirn-input" id="slirn-login-password" type="password" placeholder="密码" autocomplete="current-password">
            </div>
            <div class="slirn-form-hint" id="slirn-login-hint" style="display:none;"></div>
            <div style="display:flex; gap:8px; margin-top:14px;">
                <button class="slirn-btn slirn-btn-primary" data-action="do-login" style="flex:1;">登录</button>
                <button class="slirn-btn" data-action="hide-login" style="flex:1;">取消</button>
            </div>
        </div>
    </div>

    <!-- REQ-20261021-142：登录日志 modal（本人可见，默认隐藏） -->
    <div class="slirn-modal" id="slirn-loginlog-modal" style="display:none;">
        <div class="slirn-modal-card" style="width:560px;">
            <h3>📜 登录日志</h3>
            <div class="slirn-form-hint">仅显示当前账号的登录活动（最近 50 条）。</div>
            <div id="slirn-loginlog-list" style="margin-top:10px; max-height:360px; overflow-y:auto;"></div>
            <div style="display:flex; justify-content:flex-end; margin-top:12px;">
                <button class="slirn-btn" data-action="loginlog-close">关闭</button>
            </div>
        </div>
    </div>

    <!-- REQ-20260926-NNN：用户管理 modal（仅 admin 可用，默认隐藏） -->
    <div class="slirn-modal" id="slirn-users-modal" style="display:none;">
        <div class="slirn-modal-card" style="width:460px;">
            <h3>👤 用户管理</h3>
            <div id="slirn-users-list"></div>
            <div class="slirn-form-hint" style="margin:12px 0 4px;">➕ 创建新用户</div>
            <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <input class="slirn-input" id="slirn-users-new-name" placeholder="用户名" style="flex:1; min-width:110px;">
                <input class="slirn-input" id="slirn-users-new-pw" type="password" placeholder="密码 ≥4 位" style="flex:1; min-width:110px;">
                <label class="slirn-sd-toggle"><input type="checkbox" id="slirn-users-new-admin"> admin</label>
                <button class="slirn-btn slirn-btn-primary" data-action="users-create">创建</button>
            </div>
            <div class="slirn-form-hint" id="slirn-users-msg" style="margin-top:8px;"></div>
            <div style="display:flex; justify-content:flex-end; margin-top:12px;">
                <button class="slirn-btn" data-action="users-close">关闭</button>
            </div>
        </div>
    </div>
    '''

    with app:
        # Gradio 6.17.3 的 Blocks.head / app.head 不会渲染到页面 <head>（template 静态），
        # 所以 JS 必须挂在 gr.HTML 组件的 head= 参数上 — 前端运行时会用 DOMParser
        # 解析后把 <script> 重新 createElement/appendChild 到 document.head（src 脚本按 src 去重）。
        # 但注意：head= 传输链路会解码反斜杠转义，内联 JS 含 \ 就会被破坏 —
        # 所以这里只放 theme_js（无反斜杠）+ <script src> 引用，路由 JS 走静态文件。
        gr.HTML(full_html, head=_build_head())

        # 必要的隐藏 inputs（Gradio 6 不会把这些渲染成组件，需要用作 API 参数传递）
        hidden_start = gr.Textbox(value="", elem_id="hidden-start-input", elem_classes=["slirn-hidden-trigger"])
        hidden_end = gr.Textbox(value="", elem_id="hidden-end-input", elem_classes=["slirn-hidden-trigger"])
        hidden_name = gr.Textbox(value="", elem_id="hidden-name-input", elem_classes=["slirn-hidden-trigger"])
        hidden_hotwords = gr.Textbox(value="", elem_id="hidden-hotwords-input", elem_classes=["slirn-hidden-trigger"])
        hidden_hw_word = gr.Textbox(value="", elem_id="hidden-hw-word", elem_classes=["slirn-hidden-trigger"])
        hidden_hw_cat = gr.Textbox(value="", elem_id="hidden-hw-category", elem_classes=["slirn-hidden-trigger"])
        hidden_file = gr.File(elem_id="slirn-file-input-real", elem_classes=["slirn-hidden-trigger"])
        hidden_task_id = gr.Textbox(value="", elem_id="hidden-task-id-input", elem_classes=["slirn-hidden-trigger"])

        # ====================================================
        # 每个按钮只返回一个字符串 — JS fetch 拿到的 data[0] = HTML
        # 闭包保留 mgr / repo_root / hidden_file.value 等状态
        # ====================================================

        def go_to_dashboard() -> str: return _render_dashboard(mgr, repo_root)
        def go_to_tasks() -> str: return _render_task_list(mgr)
        def go_to_create() -> str: return _render_create_task(repo_root)
        def go_to_hotwords() -> str: return _render_hotword_lib(repo_root)

        btn_goto_dashboard = gr.Button("dashboard", elem_classes=["slirn-hidden-trigger"])
        btn_goto_tasks = gr.Button("tasks", elem_classes=["slirn-hidden-trigger"])
        btn_goto_create = gr.Button("create", elem_classes=["slirn-hidden-trigger"])
        btn_goto_hotwords = gr.Button("hotwords", elem_classes=["slirn-hidden-trigger"])
        btn_goto_dashboard.click(fn=go_to_dashboard, inputs=[], outputs=[])
        btn_goto_tasks.click(fn=go_to_tasks, inputs=[], outputs=[])
        btn_goto_create.click(fn=go_to_create, inputs=[], outputs=[])
        btn_goto_hotwords.click(fn=go_to_hotwords, inputs=[])

        def on_refresh_tasks() -> str: return _render_task_list(mgr)
        btn_refresh_tasks = gr.Button("refresh-tasks", elem_classes=["slirn-hidden-trigger"])
        btn_refresh_tasks.click(fn=on_refresh_tasks, inputs=[], outputs=[])

        def on_view_task(task_id: str) -> str:
            if not task_id.strip():
                return '<div class="slirn-empty"><div class="slirn-empty-icon">🔍</div><div class="slirn-empty-text">请指定任务 ID</div></div>'
            return _render_task_detail(task_id.strip(), mgr)
        btn_view_task = gr.Button("view", elem_classes=["slirn-hidden-trigger"])
        btn_view_task.click(fn=on_view_task, inputs=[hidden_task_id], outputs=[])

        # REQ-20260926-NNN 修复：移除死的 delete 触发器 — router.js 早已走
        # /slirn/api/delete_task（有 auth 中间件 + task_access 校验），这个遗留
        # Gradio 队列回调不经过任何鉴权，可被未登录请求经 queue/join 伪造调用删任务。
        # 其余 hidden-trigger 回调 outputs=[] 纯渲染、无副作用，留作兼容。

        def on_file_selected() -> str:
            fp = hidden_file.value
            if not fp:
                return _render_create_task(repo_root)
            from tasklib.video import get_video_duration
            p = Path(fp)
            if not p.exists():
                return _render_create_task(repo_root)
            dur = get_video_duration(p)
            if dur:
                mm, ss = divmod(int(dur), 60)
                hh, mm = divmod(mm, 60)
                dur_str = f"{hh:02d}:{mm:02d}:{ss:02d}"
            else:
                dur_str = "未知"
            size_mb = p.stat().st_size / 1024 / 1024
            js_callback = (
                f'<script>window.slirnFileLoaded && window.slirnFileLoaded('
                f'"{_esc(p.name)}", "{dur_str}", {size_mb:.1f}, '
                f'"{_esc(str(p).replace(chr(92), "/"))}");</script>'
            )
            return js_callback + _render_create_task(repo_root)
        btn_on_file_selected = gr.Button("file-selected", elem_classes=["slirn-hidden-trigger"])
        btn_on_file_selected.click(fn=on_file_selected, inputs=[], outputs=[])

        def on_cut_preview(start: str, end: str) -> str:
            fp = hidden_file.value
            if not fp or not start.strip() or not end.strip():
                return _render_create_task(repo_root)
            import uuid

            from tasklib.exceptions import VideoProcessingError
            from tasklib.video import cut_video
            src = Path(fp)
            temp_dir = repo_root / ".temp"
            temp_dir.mkdir(exist_ok=True)
            dst = temp_dir / f"cut_{uuid.uuid4().hex[:8]}_{src.stem}{src.suffix}"
            try:
                cut_video(src, dst, start.strip(), end.strip())
            except VideoProcessingError:
                return _render_create_task(repo_root)
            size_mb = dst.stat().st_size / 1024 / 1024 if dst.exists() else 0
            js_callback = (
                f'<script>window.slirnCutDone && window.slirnCutDone('
                f'"{_esc(str(dst).replace(chr(92), "/"))}", {size_mb:.1f});</script>'
            )
            return js_callback + _render_create_task(repo_root)
        btn_cut_preview = gr.Button("cut", elem_classes=["slirn-hidden-trigger"])
        btn_cut_preview.click(fn=on_cut_preview, inputs=[hidden_start, hidden_end], outputs=[])

        def on_create_task(start: str, end: str, name: str, hotwords_text: str) -> str:
            fp = hidden_file.value
            if not fp:
                return _render_create_task(repo_root)
            src = Path(fp)
            final_name = name.strip() or src.stem
            hotwords = [w.strip() for w in _re.split(r"[,\n，;；、]+", hotwords_text) if w.strip()]
            segment = None
            segment_temp = None
            seg_filename = None
            if start.strip() and end.strip():
                temp_dir = repo_root / ".temp"
                if temp_dir.exists():
                    cuts = sorted(
                        temp_dir.glob(f"cut_*_{src.stem}{src.suffix}"),
                        key=lambda p: p.stat().st_mtime, reverse=True,
                    )
                    if cuts:
                        segment_temp = cuts[0]
                        from tasklib.time_utils import segment_filename
                        ext = src.suffix.lstrip(".") or "mp4"
                        seg_filename = segment_filename(src.stem, start.strip(), end.strip(), ext)
                        from tasklib.models import TimeSegment
                        segment = TimeSegment(start=start.strip(), end=end.strip(), path=Path("placeholder"))
            try:
                task = mgr.create(name=final_name, original_video=src, segment=segment, hotwords=hotwords)
            except Exception:
                return _render_create_task(repo_root)
            if segment_temp and segment_temp.exists():
                import shutil
                final_path = repo_root / "tasks" / task.task_id / "raw_input" / seg_filename
                try:
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(segment_temp), str(final_path))
                except Exception:
                    pass
            return _render_task_list(mgr)
        btn_create_task = gr.Button("create-task", elem_classes=["slirn-hidden-trigger"])
        btn_create_task.click(fn=on_create_task, inputs=[hidden_start, hidden_end, hidden_name, hidden_hotwords], outputs=[])

        def on_cancel_create() -> str: return _render_task_list(mgr)
        btn_cancel_create = gr.Button("cancel-create", elem_classes=["slirn-hidden-trigger"])
        btn_cancel_create.click(fn=on_cancel_create, inputs=[], outputs=[])

        def on_hw_add(word: str, category: str) -> str:
            hwlib = HotwordLibrary(repo_root)
            try:
                hwlib.add(word.strip(), category=category.strip() or "默认")
            except Exception:
                pass
            return _render_hotword_lib(repo_root)
        btn_hw_add = gr.Button("hw-add", elem_classes=["slirn-hidden-trigger"])
        btn_hw_add.click(fn=on_hw_add, inputs=[hidden_hw_word, hidden_hw_cat], outputs=[])

    # 注册自定义 FastAPI 路由 — 绕过 Gradio 队列（POST 同步返回 JSON）
    _register_slirn_api(app, mgr, repo_root, auth)

    return app


def slirn_task_access(mgr: "TaskManager", auth: "_AuthStore"):
    """REQ-20260926-NNN：构造 task_access(user, tid) -> bool 给 auth 中间件用。

    admin → True；创建者 → True；成员 → True；其他 → False。
    任务不存在 → True（放行，由端点自己报「任务不存在」，保住 404 语义）。
    """
    def _check(user: dict, tid: str) -> bool:
        try:
            t = mgr.get(tid)
        except Exception:                       # noqa: BLE001 — 不存在/坏数据 → 端点兜底
            return True
        if user.get("is_admin"):
            return True
        username = user.get("username")
        if getattr(t, "created_by", None) == username:
            return True
        return username in auth.list_task_members(tid)
    return _check


def _register_slirn_api(
    app: gr.Blocks, mgr: TaskManager, repo_root: Path, auth: _AuthStore,
) -> None:
    # REQ-20260926-NNN：从 contextvar 读当前 user（中间件已 set，端点直接 get）
    # 绕开 Gradio 6 把任意额外参数当 query 的 bug — 不在端点参数位声明 Request/Depends
    from slirn_home.auth import get_current_user_from_context as _get_cur_user
    # REQ-20261021-139 / D4：会话 token 也从 contextvar 取（同一套绕开方式）。
    # 必须在启动后台线程**之前**取 —— contextvars 不跨线程传播。
    from slirn_home.auth import get_session_token_from_context as _get_ctx_token
    """为 Gradio app 注册自定义 /slirn/api/* 路由 — 同步返回 JSON，绕过 Gradio 队列。

    路由返回格式：{"ok": true, "html": "...", "toast": "...", "file_info": {...}, "cut_done": {...}}
    JS 端只需 fetch + .then(r => r.json()) 即可拿到结果。

    注意：不能直接用 `req: fastapi.Request` 作为参数 — Gradio 6 的 App.create_app 重建后的
    FastAPI 实例似乎对 Request 类型识别有 bug，会把 Request 当作 query 参数。
    改用 Pydantic 模型 body 解析，避开 Request 类型注入。
    """
    import hashlib as _hashlib
    import os as _os
    import re as _re
    import tempfile as _tempfile
    import uuid as _uuid
    from typing import Optional

    # from fastapi import Body  # 已在文件顶部 import
    from fastapi.responses import JSONResponse

    # 直接从 starlette.requests 导入 Request，避免 Gradio 6 rebuild 后
    # FastAPI 把 fastapi.Request 类型误识别为 query 参数的 bug
    from starlette.requests import Request as _StarletteRequest

    def _ok(html: str = "", **extra) -> JSONResponse:
        return JSONResponse({"ok": True, "html": html, **extra})

    def _err(msg: str) -> JSONResponse:
        return JSONResponse({"ok": False, "error": msg})

    # REQ-20260926-NNN 修复：按当前登录用户渲染任务列表 / 仪表盘。
    # 之前 create/delete/update/cancel 端点直接回 _render_task_list(mgr)（无用户
    # 过滤）→ 普通用户一操作就看到全部任务；仪表盘则完全没有按用户的入口。
    # 中间件保证非公开端点必有 user（401 拦截 / bypass 挂 test-admin）。
    # scope=None（增删改端点）：admin → 全部（老行为），普通用户 → 我的（钳制）。
    def _task_list_for_cur_user(scope: str | None = None) -> str:
        user = _get_cur_user()
        members = auth.list_user_tasks(user.get("username", "")) if user else []
        if scope is None:
            scope = "all" if user and user.get("is_admin") else "mine"
        return _render_task_list(mgr, scope=scope, current_user=user, memberships=members)

    def _dashboard_for_cur_user() -> str:
        user = _get_cur_user()
        members = auth.list_user_tasks(user.get("username", "")) if user else []
        return _render_dashboard(mgr, repo_root, current_user=user, memberships=members)

    # 事件路由 JS — 静态文件提供。head= 内联注入会被反斜杠转义解码破坏（见 ROUTER_JS 注释），
    # 所以 _build_head 只注入 <script src>，实际内容从这里出。no-cache 方便改版后刷新即生效。
    from fastapi.responses import FileResponse, HTMLResponse

    router_js_path = Path(__file__).parent / "static" / "router.js"

    @app.app.get("/slirn/static/router.js")
    async def serve_router_js():
        return FileResponse(
            router_js_path,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    # REQ-20260918-047：流程配置 + 自动执行 JS
    pipeline_js_path = Path(__file__).parent / "static" / "pipeline.js"

    @app.app.get("/slirn/static/pipeline.js")
    async def serve_pipeline_js():
        return FileResponse(
            pipeline_js_path,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    # REQ-20261022-143：短视频流程配置 + 自动执行 JS
    # REQ-20261022-146：硬件按键（Web MIDI → 合成键盘事件）
    hardware_keys_js_path = Path(__file__).parent / "static" / "hardware_keys.js"

    @app.app.get("/slirn/static/hardware_keys.js")
    async def serve_hardware_keys_js():
        return FileResponse(
            hardware_keys_js_path,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    short_video_flow_js_path = Path(__file__).parent / "static" / "short_video_flow.js"

    @app.app.get("/slirn/static/short_video_flow.js")
    async def serve_short_video_flow_js():
        return FileResponse(
            short_video_flow_js_path,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    short_video_js_path = Path(__file__).parent / "static" / "short_video.js"

    @app.app.get("/slirn/static/short_video.js")
    async def serve_short_video_js():
        return FileResponse(
            short_video_js_path,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    # 视频上传端点 — 自己解析 multipart + 写盘
    # 不用 Gradio /gradio_api/upload：那个端点用 python_multipart.MultipartParser，
    # 并发场景下会截断大文件（实测 5 个并发 415MB 上传有 3 个被截断到 0–52%）。
    # 不用 FastAPI UploadFile + python_multipart：同一个库的同一个 bug。
    # 也不用 Request 参数：Gradio 6 rebuild 后 FastAPI 把 Request 误判为 query param。
    # 解法：直接挂载一个原始 ASGI handler（不经过 FastAPI 路由的依赖注入），
    # 自己读 body、解析 multipart、写盘、返回 JSON。这样无论并发与否都稳定。
    import json as _json

    from starlette.responses import Response as _StarletteResponse

    async def _upload_video_asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        # 提取 boundary
        ctype_full = ""
        for k, v in scope.get("headers", []):
            if k == b"content-type":
                ctype_full = v.decode("latin-1", errors="replace")
                break
        m = _re.search(r'boundary="?([^";]+)"?', ctype_full)
        if not m:
            resp = _err("缺少 boundary")
            await resp(scope, receive, send)
            return
        # 收集所有 body chunks 到一个 bytes — 简单且可靠（415MB 在现代机器上无压力）
        # 不依赖流式 chunk 边界，避免复杂的 tail 窗口管理 bug
        body_chunks = []
        bytes_received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"") or b""
            if chunk:
                body_chunks.append(chunk)
                bytes_received += len(chunk)
            if not message.get("more_body", False):
                break
        if bytes_received == 0:
            resp = _err("收到空 body")
            await resp(scope, receive, send)
            return
        body_bytes = b"".join(body_chunks)

        # 解析 multipart — 找 opening boundary 行 + headers + data + closing boundary
        # 格式: --boundary\r\nheaders\r\n\r\ndata\r\n--boundary--\r\n
        boundary = b"--" + m.group(1).encode("ascii")
        # 找第一个 \r\n\r\n（headers 结束标记）
        hdr_end = body_bytes.find(b"\r\n\r\n")
        if hdr_end < 0:
            resp = _err("multipart 头不完整")
            await resp(scope, receive, send)
            return
        # 解析 filename
        header_block = body_bytes[:hdr_end]
        cdm = _re.search(
            rb'Content-Disposition:[^\r\n]*filename\*?=(?:UTF-8\'\'|")?([^\r\n";]+)',
            header_block,
            _re.IGNORECASE,
        )
        raw_name = ""
        if cdm:
            fn_raw = cdm.group(1).strip().strip(b'"').decode("utf-8", errors="replace")
            if "%" in fn_raw:
                from urllib.parse import unquote
                fn_raw = unquote(fn_raw)
            raw_name = Path(fn_raw).name
        if not raw_name:
            raw_name = f"upload_{_uuid.uuid4().hex[:8]}.bin"
        # data 起点
        data_start = hdr_end + 4
        # 找结束 boundary —— 必须从 data_start 之后开始找
        # 优先 \r\n--boundary--（最终终止）；兜底 \r\n--boundary（中间 part 终止）
        end_marker_full = b"\r\n" + boundary + b"--"
        end_marker_part = b"\r\n" + boundary
        end_idx = body_bytes.find(end_marker_full, data_start)
        if end_idx < 0:
            end_idx = body_bytes.find(end_marker_part, data_start)
            if end_idx < 0:
                resp = _err("找不到结束 boundary")
                await resp(scope, receive, send)
                return
        data_bytes = body_bytes[data_start:end_idx]
        if not data_bytes:
            resp = _err("data 为空")
            await resp(scope, receive, send)
            return

        # 计算 SHA + 写到 Gradio 缓存目录
        sha = _hashlib.sha256()
        sha.update(data_bytes)
        sha_hex = sha.hexdigest()
        safe_name = _re.sub(r'[\\/:\0]', "_", raw_name)
        gradio_cache = Path(_tempfile.gettempdir()) / "gradio" / sha_hex
        gradio_cache.mkdir(parents=True, exist_ok=True)
        final_path = gradio_cache / safe_name
        try:
            with open(final_path, "wb") as f:
                f.write(data_bytes)
        except Exception as e:
            resp = _err(f"写文件失败: {e}")
            await resp(scope, receive, send)
            return
        body_json = _json.dumps(
            {"ok": True, "path": str(final_path).replace("\\", "/"), "size_mb": round(len(data_bytes) / 1024 / 1024, 2)},
            ensure_ascii=False,
        )
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body_json.encode("utf-8")})

    # 把 ASGI handler 直接挂到 FastAPI 路由（不依赖 Request 参数）
    # 注意：starlette.routing.Route 把函数视为 request_response 风格（只接受 request 一个参数），
    # 把类视为 ASGI 风格（接受 scope/receive/send）。我们的 _upload_video_asgi 是 raw ASGI，
    # 所以包成一个类来强制 Starlette 用 ASGI 模式调用。
    class _UploadVideoASGIApp:
        async def __call__(self, scope, receive, send):
            await _upload_video_asgi(scope, receive, send)

    # 把 ASGI 类挂上去，Starlette 看到是类就当 ASGI app 处理（传 scope, receive, send）
    app.app.router.add_route(
        "/slirn/api/upload_video",
        _UploadVideoASGIApp(),
        methods=["POST"],
    )

    @app.app.post("/slirn/api/hw_add")
    async def hw_add(body: dict = Body(default_factory=dict)):
        # 支持三种入参形式：
        #   1) {"word": "人工智能", "category": "科技"}            单个
        #   2) {"word": "人工智能 机器学习\nNLP", "category": ...} 一个字段里多个，分隔符任意
        #   3) {"words": ["人工智能", "机器学习"], "category": ...} 显式数组
        cat = (body.get("category") or "").strip() or "默认"
        words_raw = body.get("words")
        if isinstance(words_raw, list):
            tokens = [str(w).strip() for w in words_raw]
        else:
            raw = body.get("word") or ""
            # 切分：半角逗号 / 换行 / 全角逗号 / 分号 / 顿号（空格不算分隔符，
            # 保留多词热词如 "machine learning"）
            tokens = _re.split(r"[,\n，;；、]+", str(raw))
            tokens = [t.strip() for t in tokens]
        tokens = [t for t in tokens if t]
        if not tokens:
            return _err("词不能为空")
        try:
            hwlib = HotwordLibrary(repo_root)
            added, dup = [], []
            for w in tokens:
                try:
                    ok = hwlib.add(w, category=cat)
                    if ok:
                        added.append(w)
                    else:
                        dup.append(w)  # 已存在（add 返回 False）
                except Exception:
                    dup.append(w)  # 抛异常（如空字符串）
        except Exception as e:
            return _err(f"添加失败: {e}")
        if added and not dup:
            msg = f"✅ 已添加 {len(added)} 个词"
        elif added and dup:
            msg = f"✅ 新增 {len(added)} 个，跳过重复 {len(dup)} 个"
        else:
            return _err(f"全部为重复词：{', '.join(dup[:5])}")
        return _ok(_render_hotword_lib(repo_root), toast=msg)

    def _fetch_url_text(url: str) -> str:
        """抓取 URL 内容并提纯为文本（URL → HTML → 文本）。
        失败抛 RuntimeError；空内容抛 RuntimeError("empty")。"""
        import re as _re2
        try:
            import httpx
            resp = httpx.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (slirn hotword analyzer)"},
                timeout=20, follow_redirects=True,
            )
            html = resp.text or ""
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"GET 失败: {e}") from e
        # HTML → 纯文本（stdlib；不引 bs4 依赖）
        # 1) 先剥 <script>/<style>/<noscript> 内容（保留标签不删，避免漏嵌套）
        html = _re2.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=_re2.IGNORECASE | _re2.DOTALL)
        html = _re2.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=_re2.IGNORECASE | _re2.DOTALL)
        html = _re2.sub(r"<noscript\b[^>]*>.*?</noscript>", " ", html, flags=_re2.IGNORECASE | _re2.DOTALL)
        # 2) 其余标签全剥
        text = _re2.sub(r"<[^>]+>", " ", html)
        # 3) 解码 HTML entities + 合并空白
        import html as _html_mod
        text = _html_mod.unescape(text)
        text = _re2.sub(r"\s+", " ", text).strip()
        if not text:
            raise RuntimeError("empty")
        return text

    @app.app.post("/slirn/api/hotword_extract")
    async def hotword_extract(body: dict = Body(default_factory=dict)):
        """REQ-20260926-NNN：分析输入框/URL → LLM 提取热词（同步）。

        入参：{"requirement": "...", "url": "..."}，二选一必填。
        出参：{"ok": true, "candidates": [{"word","category"}, ...]}。
        """
        from slirn_home import llm_config, hotword_service

        requirement = str(body.get("requirement") or "").strip()
        url = str(body.get("url") or "").strip()
        page_hint = str(body.get("page_hint") or "").strip()
        parts: list[str] = []
        if requirement:
            parts.append(requirement)
        if url:
            try:
                url_text = _fetch_url_text(url)
            except RuntimeError as e:
                msg = str(e)
                if msg == "empty":
                    return _err(f"抓取 URL 失败或为空：{url}")
                return _err(f"抓取 URL 失败：{msg}（{url}）")
            parts.append(f"\n\n[URL 内容]\n{url_text[:8000]}")
        if not parts:
            return _err("requirement 或 url 必填其一")
        source = "\n\n".join(parts)

        entry = llm_config.get_current_entry(repo_root)
        if entry is None:
            return _err("未注册任何大模型 — 请先点顶栏「大模型设置」 添加模型")
        try:
            candidates = hotword_service.extract_hotwords(
                source, entry=entry, page_hint=page_hint,
            )
        except Exception as e:  # noqa: BLE001
            return _err(str(e))
        return _ok("", candidates=candidates)

    @app.app.post("/slirn/api/hw_remove")
    async def hw_remove(body: dict = Body(default_factory=dict)):
        # 支持单删 ({word}) 与批量删 ({words:[...]})
        words_raw = body.get("words")
        if isinstance(words_raw, list):
            targets = [str(w).strip() for w in words_raw if str(w or "").strip()]
        else:
            word = (body.get("word") or "").strip()
            targets = [word] if word else []
        if not targets:
            return _err("请提供要删除的词")
        try:
            hwlib = HotwordLibrary(repo_root)
            removed = []
            missing = []
            for w in targets:
                try:
                    if hwlib.remove(w):
                        removed.append(w)
                    else:
                        missing.append(w)
                except Exception:
                    missing.append(w)
        except Exception as e:
            return _err(f"删除失败: {e}")
        n = len(removed)
        if n == 0:
            return _err("没有词被删除（全部不存在）")
        if n == 1:
            toast = f"✅ 已删除：{removed[0]}"
        else:
            toast = f"✅ 已删除 {n} 个词"
        if missing:
            toast += f" · 跳过 {len(missing)} 个不存在的词"
        return _ok(_render_hotword_lib(repo_root), toast=toast)

    @app.app.post("/slirn/api/refresh_tasks")
    async def refresh_tasks(body: dict = Body(default_factory=dict)):
        # REQ-20260926-NNN：按用户可见性过滤（_task_list_for_cur_user 读 contextvar user；
        # 非 admin 的 scope="all" 会在 _render_task_list 里钳回 "mine"）
        scope = str(body.get("scope") or "mine").strip() or "mine"
        return _ok(_task_list_for_cur_user(scope=scope))

    # REQ-20260926-NNN 修复：仪表盘按用户渲染 — 初始 HTML 已骨架化，
    # 登录态确认后前端经此端点拉取（普通用户只看自己的统计 + 最近任务）
    @app.app.post("/slirn/api/dashboard")
    async def dashboard_page():
        return _ok(_dashboard_for_cur_user())

    @app.app.post("/slirn/api/view_task")
    async def view_task(body: dict = Body(default_factory=dict)):
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _ok('<div class="slirn-empty"><div class="slirn-empty-icon">🔍</div><div class="slirn-empty-text">请指定任务 ID</div></div>')
        return _ok(_render_task_detail(tid, mgr))

    @app.app.post("/slirn/api/create_page")
    async def create_page():
        """全新建任务页 HTML（编辑模式退出后还原用）。"""
        return _ok(_render_create_task(repo_root))

    @app.app.post("/slirn/api/edit_task")
    async def edit_task(body: dict = Body(default_factory=dict)):
        """编辑任务 — 返回新建页（编辑模式预填）HTML（REQ-20260915-003）。"""
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        return _ok(_render_create_task(repo_root, edit=t))

    @app.app.post("/slirn/api/update_task")
    async def update_task(body: dict = Body(default_factory=dict)):
        """保存编辑：任务名 / 截取时间（变更自动重截取）/ 热词三来源（REQ-20260915-003）。"""
        tid = (body.get("task_id") or "").strip()
        name = (body.get("name") or "").strip()
        start = (body.get("start") or "").strip()
        end = (body.get("end") or "").strip()
        inherit_public = bool(body.get("inherit_public"))
        picked_raw = body.get("picked_words")
        manual_text = (body.get("manual_words") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")

        # ---- 热词三来源重合并（与 create_task 同规则） ----
        seen: set[str] = set()
        ordered: list[str] = []
        word_sources: dict[str, str] = {}
        if inherit_public:
            try:
                hwlib = HotwordLibrary(repo_root)
                for _cat, ws in hwlib.list_grouped().items():
                    for w in ws:
                        w = w.strip()
                        if w and w not in seen:
                            seen.add(w)
                            ordered.append(w)
                            word_sources[w] = "inherit"
            except Exception:  # noqa: BLE001
                pass
        if isinstance(picked_raw, list):
            for w in picked_raw:
                w = str(w or "").strip()
                if w and w not in seen:
                    seen.add(w)
                    ordered.append(w)
                    word_sources[w] = "pick"
        for w in _re.split(r"[,\n，;；、]+", manual_text):
            w = w.strip()
            if w and w not in seen:
                seen.add(w)
                ordered.append(w)
                word_sources[w] = "manual"

        # ---- 截取段时间：变更 → 从原视频重截取 ----
        from tasklib.exceptions import InvalidTimeRangeError
        from tasklib.models import TimeSegment
        from tasklib.time_utils import parse_time, segment_filename

        old_seg = t.segment
        seg_changed = False
        new_segment: TimeSegment | None = None
        toast = "✅ 任务已更新"
        if start and end:
            try:
                if parse_time(start).total_seconds() >= parse_time(end).total_seconds():
                    return _err(f"开始时间 ({start}) 必须小于结束时间 ({end})")
            except ValueError as e:
                return _err(f"时间格式错误: {e}")
            if old_seg is None or old_seg.start != start or old_seg.end != end:
                src = t.original_video_source
                if not src.exists():
                    return _err("原视频文件缺失，无法重截取")
                ext = src.suffix.lstrip(".") or "mp4"
                final_path = mgr.tasks_dir / tid / "raw_input" / segment_filename(src.stem, start, end, ext)
                try:
                    from tasklib.video import cut_video
                    cut_video(src, final_path, start, end)
                except Exception as e:  # noqa: BLE001
                    return _err(f"重截取失败: {e}")
                new_segment = TimeSegment(start=start, end=end, path=final_path)
                seg_changed = True
                toast = "✅ 任务已更新（截取段已重截）"
        elif old_seg is not None:
            # 时间清空 → 解除截取段（走完整原视频）
            seg_changed = True
            new_segment = None

        # 已生成字幕 & 截取范围变化 → 字幕已过期，提示重新生成
        had_subtitle = (mgr.tasks_dir / tid / "outputs" / _asr.SUBTITLE_JSON).exists()

        try:
            mgr.update_task(
                tid, name=name or None,
                segment=new_segment, segment_changed=seg_changed,
                hotwords=ordered,
                inherit_public=inherit_public,
                hotword_sources=word_sources if word_sources else None,
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"更新失败: {e}")

        # 清理被替换的旧截取文件
        if seg_changed and old_seg is not None and old_seg.path != (new_segment.path if new_segment else None):
            try:
                if old_seg.path.exists():
                    old_seg.path.unlink()
            except OSError:
                pass

        if seg_changed and had_subtitle:
            toast += " · 截取范围已变，建议重新生成字幕"
        return _ok(_task_list_for_cur_user(), toast=toast, task_id=tid)

    @app.app.post("/slirn/api/workbench")
    async def workbench(body: dict = Body(default_factory=dict)):
        """剪辑工作台 HTML（REQ-20260915-003）。"""
        from starlette.concurrency import run_in_threadpool

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        # 渲染放线程池：长任务工作台 HTML 数 MB、数百 ms 起，同步跑在事件循环里
        # 会卡住所有并发请求（anyio 会把 contextvar 拷进线程，鉴权上下文不丢）
        return _ok(await run_in_threadpool(_render_workbench, tid, mgr))

    # ========== REQ-20260919-061：精剪视频·四素材合成器 API ==========

    @app.app.post("/slirn/api/save_fine_layout")
    async def save_fine_layout(body: dict = Body(default_factory=dict)):
        """保存精剪视频 4 素材的位置/缩放/启用（含 video 的 crop_* 字段）。

        REQ-20260919-061 用户反馈：原版对不存在的 task_id 抛 500（TaskNotFoundError），
        导致前端滑块拖动后看到保存失败但不知道为什么。统一走 try/except 返回友好错误。
        """
        tid = (body.get("task_id") or "").strip()
        layout = body.get("layout") or {}
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception as e:  # noqa: BLE001 — TaskNotFoundError 等
            return _err(f"任务不存在: {tid}")
        if not t:
            return _err(f"任务不存在: {tid}")
        fc = _get_fine_compose(mgr, tid)
        # 字段白名单（含 crop_* + cover.duration + video.crop_aspect_lock）。
        # viewport 已废弃：检测区域「应用」只负责填充 x/y/crop_*，填充后允许
        # 手工自由调整，保存时不再夹紧（历史遗留的 viewport 字段在保存时清除）。
        allowed_keys = (
            "x", "y", "scale", "enabled",
            "crop_x", "crop_y", "crop_w", "crop_h",
            "duration", "crop_aspect_lock",
        )
        for mat_key, val in layout.items():
            if mat_key not in _FINE_LAYOUT_DEFAULTS:
                continue
            existing = fc["layout"].get(mat_key) or {}
            existing.update({k: v for k, v in val.items() if k in allowed_keys})
            # REQ-20260919-062 v18：crop_aspect_lock 强制 bool（前端可能传 true/"on"/1）
            if mat_key == "video":
                if "crop_aspect_lock" in existing:
                    existing["crop_aspect_lock"] = bool(existing["crop_aspect_lock"])
                # 旧数据残留的 viewport 清除（夹紧逻辑已删除，字段不再使用）
                existing.pop("viewport", None)
            fc["layout"][mat_key] = existing
        _save_fine_compose(mgr, tid, fc)
        return _ok(
            toast="位置/缩放已保存",
            layout={k: dict(v) for k, v in fc["layout"].items()},
        )

    @app.app.post("/slirn/api/auto_pick_upstream_material")
    async def auto_pick_upstream_material(body: dict = Body(default_factory=dict)):
        """REQ-20260919-061：把 video/subtitle 的素材来源切到「自动获取上游产物」。

        - video  → outputs/rough_compose.mp4（粗剪合成阶段产物）
        - subtitle → optimize_subtitle.json → build_srt() → tmp/optimized_subs.srt

        上游产物不存在时返回错误，前端 toast 提示并保持当前来源。
        """
        tid = (body.get("task_id") or "").strip()
        kind = (body.get("kind") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        if kind not in _FINE_AUTO_KINDS:
            return _err(f"该素材不支持自动获取: {kind}")
        t = mgr.get(tid)
        if not t:
            return _err(f"任务不存在: {tid}")
        upstream = _fine_upstream_path(tid, kind, mgr)
        if not upstream:
            kind_label = _FINE_MATERIAL_LABELS[kind][1]
            if kind == "video":
                return _err(f"上游无粗剪合成产物（rough_compose.mp4），请先完成「粗剪合成」阶段")
            return _err(f"上游无优化字幕产物，请先完成「优化字幕」阶段并确认保存")
        fc = _get_fine_compose(mgr, tid)
        fc["materials"][kind] = {
            "path": str(upstream.relative_to(mgr.tasks_dir)),
            "type": "video" if kind == "video" else "srt",
            "source": "auto",
        }
        _save_fine_compose(mgr, tid, fc)
        kind_label = _FINE_MATERIAL_LABELS[kind][1]
        # 局部刷新（REQ：不做整页/工作台刷新）：返回重渲染后的单个素材卡，
        # 前端只换 .slirn-fine-upload-card[data-kind=kind] 这一块
        card_html = _render_fine_upload_card(tid, kind, mgr, fc["materials"])
        return _ok(
            path=fc["materials"][kind]["path"],
            card_html=card_html,
            toast=f"已自动从上游获取 {kind_label}",
        )

    @app.app.post("/slirn/api/clear_bg_detect_cache")
    async def clear_bg_detect_cache(body: dict = Body(default_factory=dict)):
        """REQ-20260919-062 v8：清空背景图区域检测的缓存（让用户能重新检测）。
        REQ-20260919-065 升级：同时清 detected_region（正式参数）。
        """
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        try:
            fc = _get_fine_compose(mgr, tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"读取任务失败: {e}")
        if "bg_detect_cache" not in fc and "detected_region" not in fc:
            return _ok(toast="缓存已为空，无需清空")
        fc.pop("bg_detect_cache", None)
        fc.pop("detected_region", None)
        try:
            _save_fine_compose(mgr, tid, fc)
        except Exception as e:  # noqa: BLE001
            return _err(f"保存失败: {e}")
        return _ok(toast="🗑 已清空检测缓存（下次打开会重新渲染）")

    @app.app.post("/slirn/api/detect_bg_white_area")
    async def detect_bg_white_area(request: Request, body: dict = Body(default_factory=dict)):
        """REQ-20260919-062：检测背景图片的白色区域 4 角坐标 + 宽高。

        算法由前端 body.algorithm 选择：
          - "pixel": numpy + PIL 在原图分辨率扫描 RGB 三通道均 ≥ threshold 的像素，
            求连通白色区域的最小外接矩形（bbox）。坐标等比缩放到 1920×1080 设计空间。
          - "ai":    调多模态 LLM，让模型看图描述「白色矩形区域的左上角和右下角像素坐标」，
            再按原图分辨率折算回 1920×1080 设计空间。
        threshold: 像素模式用 RGB 三通道阈值（0-255，默认 250）；AI 模式忽略。
        返回：{x, y, width, height, center_x, center_y, pixel_count, image_native_w/h, algorithm, threshold}
        4 个角点：
          - 左上 (x, y)
          - 右上 (x + width - 1, y)
          - 左下 (x, y + height - 1)
          - 右下 (x + width - 1, y + height - 1)

        REQ-20260919-075：检测动作也写执行历史（之前没有）。
        """
        import numpy as _np
        from PIL import Image as _PILImage

        # REQ-20260919-075：auto 透传；检测区域写日志
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        # REQ-20260920-081：session 透传
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        from slirn_home import execution_history

        # REQ-20260919-075：失败路径也写历史（用包装函数替代裸 return _err）
        _bg_outputs_dir: Path | None = None
        _bg_exec_id: str = ""

        def _bg_finish_err(msg: str):
            """记录失败并返回 _err。"""
            try:
                if _bg_exec_id and _bg_outputs_dir:
                    execution_history.patch_fields(_bg_outputs_dir, _bg_exec_id, {
                        "description": f"检测失败：{msg[:80]}"
                    })
                    execution_history.record_finish(_bg_outputs_dir, _bg_exec_id,
                                                    success=False, error=msg)
            except Exception:  # noqa: BLE001
                pass
            return _err(msg)

        tid = (body.get("task_id") or "").strip()
        algorithm = (body.get("algorithm") or "pixel").strip()
        try:
            threshold = int(body.get("threshold") or 250)
        except (TypeError, ValueError):
            threshold = 250
        threshold = max(200, min(255, threshold))
        if not tid:
            return _err("缺少 task_id")
        if algorithm not in ("pixel", "ai", "ai_color", "center_expand"):
            return _err(f"不支持的算法: {algorithm}")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")

        outputs_dir = mgr.tasks_dir / tid / "outputs"
        _bg_outputs_dir = outputs_dir
        # REQ-20260919-075：开始记录 — 检测区域
        exec_id = execution_history.record_start(
            outputs_dir, execution_history.KIND_FINE_BG_DETECT,
            extra={"algorithm": algorithm, "threshold": threshold},
            auto=_auto,
            auto_session_id=_auto_session_id,
        )
        _bg_exec_id = exec_id

        fc = _get_fine_compose(mgr, tid)
        bg_mat = fc.get("materials", {}).get("bg") or {}
        bg_path_rel = bg_mat.get("path")
        if not bg_path_rel:
            return _bg_finish_err("请先上传背景图片")
        bg_path_abs = _resolve_mat_abs(mgr, tid, fc["materials"], "bg")
        if not bg_path_abs or not bg_path_abs.exists():
            return _bg_finish_err(f"背景图片文件不存在: {bg_path_rel}")

        # 加载图片（保留原图分辨率用于 image_native_w/h 反馈）
        try:
            with _PILImage.open(bg_path_abs) as _im:
                _im.load()
            img = _PILImage.open(bg_path_abs).convert("RGB")
        except Exception as e:  # noqa: BLE001
            return _bg_finish_err(f"背景图片读取失败: {e}")
        iw, ih = img.size
        if iw <= 0 or ih <= 0:
            return _bg_finish_err(f"背景图片尺寸异常: {iw}x{ih}")

        # 用户反馈：先缩放到设计空间（1920×1080）再做白色区域扫描，
        # 避免原图分辨率不同时坐标还要再折算，输出坐标即设计空间像素。
        # - pixel 算法：先 resize 再扫描，scan_img 已是 1920×1080，scale = 1
        # - ai 算法：AI 输出的坐标是原图坐标，仍需按比例折算 → scale_x/y
        #            （resize 会破坏原图细节，对 LLM 视觉判断不利，所以 AI 路径不 resize）
        scale_x = _FINE_DESIGN_W / iw
        scale_y = _FINE_DESIGN_H / ih

        def _result_payload(x_min: int, y_min: int, x_max: int, y_max: int,
                             pixel_count: int, sx: float = 1.0, sy: float = 1.0):
            """统一输出格式（设计空间像素 + 4 角点 + 原图尺寸）。

            sx/sy: 把 (x_min, y_min, x_max, y_max) 视为「当前工作图」坐标，乘以缩放系数
            折算到设计空间（1920×1080）。pixel 路径已 resize 到 1920×1080，sx=sy=1；
            AI 路径未 resize，sx=1920/iw, sy=1080/ih。
            """
            w_native = x_max - x_min + 1
            h_native = y_max - y_min + 1
            return {
                "x": int(round(x_min * sx)),
                "y": int(round(y_min * sy)),
                "width": int(round(w_native * sx)),
                "height": int(round(h_native * sy)),
                "center_x": int(round(((x_min + x_max) / 2) * sx)),
                "center_y": int(round(((y_min + y_max) / 2) * sy)),
                "pixel_count": int(pixel_count),
                "image_native_w": int(iw),
                "image_native_h": int(ih),
                "algorithm": algorithm,
                "threshold": int(threshold) if algorithm == "pixel" else None,
                "corners": {
                    "topleft":     [int(round(x_min * sx)), int(round(y_min * sy))],
                    "topright":    [int(round(x_max * sx)), int(round(y_min * sy))],
                    "bottomleft":  [int(round(x_min * sx)), int(round(y_max * sy))],
                    "bottomright": [int(round(x_max * sx)), int(round(y_max * sy))],
                },
            }

        # 缩放到设计空间（LANCZOS 高质量）— pixel / ai_color 共用
        scan_img = img.resize((_FINE_DESIGN_W, _FINE_DESIGN_H), _PILImage.LANCZOS)

        if algorithm == "pixel":
            # 在 scan_img（已是设计空间）上扫描白色像素
            arr = _np.array(scan_img)  # shape (1080, 1920, 3)
            mask = (
                (arr[:, :, 0] >= threshold)
                & (arr[:, :, 1] >= threshold)
                & (arr[:, :, 2] >= threshold)
            )
            ys, xs = _np.where(mask)
            n = int(xs.size)
            if n == 0:
                return _bg_finish_err(
                    f"未检测到任何白色像素（threshold={threshold}）；"
                    f"请调低阈值或换张图"
                )
            x_min, x_max = int(xs.min()), int(xs.max())
            y_min, y_max = int(ys.min()), int(ys.max())
            # scan_img 已是 1920×1080，sx=sy=1.0，输出即设计空间像素
            payload = _result_payload(x_min, y_min, x_max, y_max, n, 1.0, 1.0)
            _save_bg_detect_cache(mgr, tid, payload)  # v8：检测后存缓存，避免反复检测
            # REQ-20260919-075：检测成功时回填执行情况
            try:
                execution_history.patch_fields(outputs_dir, exec_id, {
                    "description": (f"检测算法 {algorithm} 完成：发现白色区域"
                                    f" ({payload['width']}×{payload['height']} 像素，"
                                    f" {n} 白色像素)")
                })
                execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
            except Exception as _eh:  # noqa: BLE001
                log.warning("[bg_detect][%s] history 记录失败: %s", tid, _eh)
            return _ok(**payload)

        if algorithm == "center_expand":
                # 用户反馈 v3：以上方法检测结果都不对。换思路 ——
            #   1) 以背景图中心点周围 10×10 区域的平均色作为「基本颜色」
            #   2) 从中心向上/下/左/右 4 个方向扩展（每次扩展一行/列像素）
            #   3) 扩展信号是「矩线」（矩形边界线）：新行/列所有像素与基本颜色差值都在 ±10 内才继续
            #   4) 任一方向遇到颜色变化即停
            #   5) 4 条矩线围成的矩形 = 所求区域，输出左上角 + 宽高
            # 这个算法的优势：
            #   - 不需要先知道是什么颜色（自动从中心提取）
            #   - 对「背景图中央有一片同色区域」的典型布局最稳
            #   - 4 方向同时扩展，类似 flood fill 但沿坐标轴（更快、更可预测）
            arr_full = _np.array(scan_img).astype(int)  # (1080, 1920, 3) int
            ih_d, iw_d = arr_full.shape[:2]
            cx, cy = iw_d // 2, ih_d // 2
            # 中心 10×10 平均色（避开单像素噪声）
            patch_r = 5
            x0 = max(0, cx - patch_r); x1 = min(iw_d, cx + patch_r + 1)
            y0 = max(0, cy - patch_r); y1 = min(ih_d, cy + patch_r + 1)
            patch = arr_full[y0:y1, x0:x1, :]
            base_rgb = patch.reshape(-1, 3).mean(axis=0)  # (3,) float
            base_r, base_g, base_b = float(base_rgb[0]), float(base_rgb[1]), float(base_rgb[2])
            color_tol = 10
    
            def _row_match(y: int, x_start: int, x_end: int) -> bool:
                """行 [x_start..x_end] 所有像素与基本色差值是否都在 ±tol 内（用于水平扫描）。"""
                row = arr_full[y, x_start:x_end + 1, :]
                d = _np.abs(row - base_rgb)
                return bool((d <= color_tol).all())
    
            def _col_match(x: int, y_start: int, y_end: int) -> bool:
                """列 [y_start..y_end] 所有像素与基本色差值是否都在 ±tol 内（用于垂直扫描）。"""
                col = arr_full[y_start:y_end + 1, x, :]
                d = _np.abs(col - base_rgb)
                return bool((d <= color_tol).all())
    
            # 左扩展：x_left 一直左移，直到 _row_match 失败
            x_left = cx
            while x_left > 0 and _row_match(cy, x_left - 1, x_left - 1):
                x_left -= 1
            # 右扩展
            x_right = cx
            while x_right < iw_d - 1 and _row_match(cy, x_right + 1, x_right + 1):
                x_right += 1
            # 上扩展
            y_top = cy
            while y_top > 0 and _col_match(cx, y_top - 1, y_top - 1):
                y_top -= 1
            # 下扩展
            y_bottom = cy
            while y_bottom < ih_d - 1 and _col_match(cx, y_bottom + 1, y_bottom + 1):
                y_bottom += 1
    
            x_min, x_max = x_left, x_right
            y_min, y_max = y_top, y_bottom
            w = x_max - x_min + 1
            h = y_max - y_min + 1
            # 像素数估算（外接矩形面积；按真实分布应为同色面积，矩形面积作为上限）
            n_pixels = w * h
            # scan_img 已是 1920×1080，sx=sy=1.0
            payload = _result_payload(x_min, y_min, x_max, y_max, n_pixels, 1.0, 1.0)
            payload["algorithm"] = "center_expand"
            payload["threshold"] = None
            # 把基本颜色也带上，方便 UI 展示
            payload["detected_color"] = [int(round(base_r)), int(round(base_g)), int(round(base_b))]
            payload["color_tolerance"] = color_tol
            rgb_text = "RGB(" + str(int(round(base_r))) + "," + str(int(round(base_g))) + "," + str(int(round(base_b))) + ")"
            _save_bg_detect_cache(mgr, tid, payload)  # v8：检测后存缓存
            return _ok(
                **payload,
                toast=f"✅ 中心扩展找到区域 {w}x{h}（基本色 {rgb_text}）",
            )

        # algorithm == "ai_color"
        # 用户反馈：待检测区域的颜色无法确定时，先让 LLM 看背景图左下角识别主色，
        # 再用像素扫描找该颜色的 bbox。颜色容差 ±10（覆盖 LLM 略微偏差）。
        from slirn_home import llm_config, revision_service

        models = llm_config.list_models(repo_root)
        cur_id = llm_config.get_current(repo_root)
        cur_entry = next((m for m in models if m.get("id") == cur_id), None)
        if cur_entry is None or not revision_service._is_vision_entry(cur_entry):
            vision_alts = [m["id"] for m in models if revision_service._is_vision_entry(m)]
            hint = (
                f" — 已注册可用多模态模型：{', '.join(vision_alts)}，请在顶栏「大模型设置」 切换"
            ) if vision_alts else (
                " — 请在顶栏「大模型设置」 注册多模态模型（推荐 qwen-vl-plus）"
            )
            return _bg_finish_err(
                f"AI 颜色识别需多模态模型，当前模型不支持图片输入{hint}"
            )

        # 切左下角象限（设计空间尺寸的 1/4 = 960×540），保存到临时文件供 LLM 读取
        bl_quad = scan_img.crop((0, _FINE_DESIGN_H // 2, _FINE_DESIGN_W // 2, _FINE_DESIGN_H))
        # 用 bg_path_abs 同目录避免占用系统临时目录（清理方便）
        bl_tmp = bg_path_abs.parent / "_bg_bl_quad_tmp.png"
        try:
            bl_quad.save(bl_tmp)
        except Exception as e:  # noqa: BLE001
            return _bg_finish_err(f"左下角象限保存失败: {e}")

        system_prompt = (
            "你是图像颜色分析助手。用户提供了一张背景图的左下角区域，"
            "里面通常有一大片相同颜色的色块（设计上用来放置视频或装饰）。\n\n"
            "请分析图片，返回该主色块的颜色（RGB 三通道 0-255）。\n"
            "只输出合法 JSON，不要任何解释文字。格式严格如下：\n"
            '{"r": <0-255>, "g": <0-255>, "b": <0-255>}\n\n'
            "约束：r/g/b 都是整数 0-255。如果左下角有多种颜色，"
            "返回面积最大的那个纯色块的 RGB。"
        )
        user_text = (
            "请分析这张图（左下角象限）的最大色块，返回其 RGB 值。"
        )
        try:
            raw_color = revision_service._call_llm_vision(
                system_prompt, user_text, [bl_tmp], entry=cur_entry,
                usage_ctx={"task_id": tid, "stage": "背景检测-配色"},
            )
        except Exception as e:  # noqa: BLE001
            return _bg_finish_err(f"AI 颜色识别失败: {e}")
        import re as _re2
        cleaned = _re2.sub(r"```(?:json)?\s*|\s*```", "", raw_color or "").strip()
        s_idx, e_idx = cleaned.find("{"), cleaned.rfind("}")
        if s_idx < 0 or e_idx <= s_idx:
            return _bg_finish_err(f"AI 颜色输出不是合法 JSON: {(raw_color or '')[:200]}")
        try:
            parsed_color = json.loads(cleaned[s_idx:e_idx + 1])
        except Exception as e:  # noqa: BLE001
            return _err(f"AI 颜色输出解析失败: {e}")
        try:
            cr = int(parsed_color.get("r", -1))
            cg = int(parsed_color.get("g", -1))
            cb = int(parsed_color.get("b", -1))
        except (TypeError, ValueError):
            return _err(f"AI 颜色输出字段类型错: {parsed_color}")
        if not (0 <= cr <= 255 and 0 <= cg <= 255 and 0 <= cb <= 255):
            return _err(
                f"AI 颜色输出 RGB 越界: r={cr}, g={cg}, b={cb}"
            )

        # 像素扫描：在设计空间 scan_img 上找与 AI 识别色容差±10 内的像素
        color_tol = 10
        arr_full = _np.array(scan_img)
        diff = _np.abs(arr_full.astype(int) - _np.array([cr, cg, cb]))
        mask = (diff[:, :, 0] <= color_tol) & (diff[:, :, 1] <= color_tol) & (diff[:, :, 2] <= color_tol)
        ys, xs = _np.where(mask)
        n = int(xs.size)
        if n == 0:
            return _err(
                f"未检测到 RGB({cr},{cg},{cb})±{color_tol} 的像素；"
                f"AI 识别的颜色可能在背景图里不存在（请换图或换算法）"
            )
        x_min, x_max = int(xs.min()), int(xs.max())
        y_min, y_max = int(ys.min()), int(ys.max())
        # scan_img 已是 1920×1080，sx=sy=1.0
        payload = _result_payload(x_min, y_min, x_max, y_max, n, 1.0, 1.0)
        # _result_payload 用的是闭包变量 algorithm（此时是 "ai_color"），
        # 但 _ok 不允许重复 key — 显式 pop 再覆盖，统一返回 algorithm="ai_color"
        payload["algorithm"] = "ai_color"
        payload["threshold"] = None
        payload["detected_color"] = [cr, cg, cb]
        payload["color_tolerance"] = color_tol
        _save_bg_detect_cache(mgr, tid, payload)  # v8：检测后存缓存
        return _ok(
            **payload,
            toast=f"✅ AI 识别主色 RGB({cr},{cg},{cb})，已定位区域",
            model=cur_id,
        )

        # algorithm == "ai"
        from slirn_home import llm_config, revision_service

        models = llm_config.list_models(repo_root)
        cur_id = llm_config.get_current(repo_root)
        cur_entry = next((m for m in models if m.get("id") == cur_id), None)
        if cur_entry is None or not revision_service._is_vision_entry(cur_entry):
            vision_alts = [m["id"] for m in models if revision_service._is_vision_entry(m)]
            hint = (
                f" — 已注册可用多模态模型：{', '.join(vision_alts)}，请在顶栏「大模型设置」 切换"
            ) if vision_alts else (
                " — 请在顶栏「大模型设置」 注册多模态模型（推荐 qwen-vl-plus）"
            )
            return _err(
                f"AI 检测需多模态模型，当前模型不支持图片输入{hint}"
            )
        system_prompt = (
            "你是图像区域检测助手。用户上传了一张背景图，里面通常有一个白色矩形区域"
            "（设计上用来放置视频的位置 — 即背景里的「洞」）。\n\n"
            "请分析图片，返回该白色矩形区域在**原图像素坐标**下的最小外接矩形。"
            "只输出合法 JSON，不要任何解释文字。格式严格如下：\n"
            '{"x_min": <int>, "y_min": <int>, "x_max": <int>, "y_max": <int>}\n\n'
            "约束：坐标都是原图像素坐标（非归一化、非设计空间），左上角原点；"
            "x_min < x_max, y_min < y_max。找不到白色矩形就返回全 0：{\"x_min\":0,\"y_min\":0,\"x_max\":0,\"y_max\":0}"
        )
        user_text = (
            f"请分析这张背景图（原图分辨率 {iw}×{ih}），"
            "输出其中最大白色矩形区域的 bbox（左上角 + 右下角）。"
        )
        try:
            raw = revision_service._call_llm_vision(
                system_prompt, user_text, [bg_path_abs], entry=cur_entry,
                usage_ctx={"task_id": tid, "stage": "背景检测-白区"},
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"AI 检测失败: {e}")
        import re as _re
        cleaned = _re.sub(r"```(?:json)?\s*|\s*```", "", raw or "").strip()
        s_idx, e_idx = cleaned.find("{"), cleaned.rfind("}")
        if s_idx < 0 or e_idx <= s_idx:
            return _err(f"AI 输出不是合法 JSON: {(raw or '')[:200]}")
        try:
            parsed = json.loads(cleaned[s_idx:e_idx + 1])
        except Exception as e:  # noqa: BLE001
            return _err(f"AI 输出解析失败: {e}")
        try:
            x_min = int(parsed.get("x_min", 0))
            y_min = int(parsed.get("y_min", 0))
            x_max = int(parsed.get("x_max", 0))
            y_max = int(parsed.get("y_max", 0))
        except (TypeError, ValueError):
            return _err(f"AI 输出字段类型错: {parsed}")
        if x_max <= x_min or y_max <= y_min:
            return _err(
                f"AI 未识别到有效白色区域（x_min={x_min}, y_min={y_min}, "
                f"x_max={x_max}, y_max={y_max}）；请换张图或改用像素扫描"
            )
        # 像素数未知，估算 = bbox 面积
        pixel_count = (x_max - x_min + 1) * (y_max - y_min + 1)
        # AI 输出是原图坐标，按 scale 折算到设计空间
        payload = _result_payload(x_min, y_min, x_max, y_max, pixel_count, scale_x, scale_y)
        _save_bg_detect_cache(mgr, tid, payload)  # v8：检测后存缓存
        return _ok(
            **payload,
            toast=f"✅ AI 已识别白色区域（{cur_id}）",
            model=cur_id,
        )

    @app.app.post("/slirn/api/parse_reference_layout")
    async def parse_reference_layout(request: Request, body: dict = Body(default_factory=dict)):
        """REQ-20260919-061 Phase C：调多模态模型解析参考位置关系图 → 4 素材布局。

        模型选择：自动扫描已注册 entry，挑第一个 _is_vision_entry() 为真的模型；
        若没有则提示用户在顶栏「大模型设置」 注册一个多模态模型（qwen-vl-plus 等）。
        响应：写回 fine_compose.json: layout.{video,subtitle,cover,bg}.{x,y,scale,enabled}。

        REQ-20260919-075：AI 智能布局写执行历史。
        """
        from slirn_home import execution_history, llm_config, revision_service
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        # REQ-20260920-081：session 透传
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")

        outputs_dir = mgr.tasks_dir / tid / "outputs"
        # REQ-20260919-075：开始记录 — AI 智能布局
        exec_id = execution_history.record_start(
            outputs_dir, execution_history.KIND_FINE_AI_LAYOUT,
            extra={},
            auto=_auto,
            auto_session_id=_auto_session_id,
        )

        # 1. 校验参考图
        fc = _get_fine_compose(mgr, tid)
        ref_mat = fc.get("materials", {}).get("reference") or {}
        ref_path_rel = ref_mat.get("path")
        if not ref_path_rel:
            return _err("请先上传参考位置关系图（精剪视频面板「参考位置关系图」素材卡）")
        ref_path_abs = _resolve_mat_abs(mgr, tid, fc["materials"], "reference")
        if not ref_path_abs or not ref_path_abs.exists():
            return _err(f"参考图文件不存在: {ref_path_rel}")

        # 2. 选 vision entry：严格使用当前选定的模型（尊重用户 ⚙️ 选择，不静默注册）
        models = llm_config.list_models(repo_root)
        cur_id = llm_config.get_current(repo_root)
        cur_entry = next((m for m in models if m.get("id") == cur_id), None)
        if cur_entry is None:
            return _err(
                "未选择当前模型 — 请在顶栏「大模型设置」 选定一个大模型后再试"
            )
        if not revision_service._is_vision_entry(cur_entry):
            # 列出已注册的多模态备选（如有），帮用户快速切换
            vision_alts = [m["id"] for m in models if revision_service._is_vision_entry(m)]
            hint = (
                f" — 已在配置中找到可用的多模态模型：{', '.join(vision_alts)}，"
                "请在顶栏「大模型设置」 切换当前模型"
            ) if vision_alts else (
                " — 请在顶栏「大模型设置」 添加多模态模型（推荐 qwen-vl-plus），"
                "需要环境变量 DASHSCOPE_API_KEY"
            )
            return _err(
                f"当前模型「{cur_id}」不支持图片输入{hint}"
            )
        vision_entry = cur_entry

        # 3. 调多模态模型 — REQ-20260919-061 用户补充：x/y 改为像素（1920×1080 设计空间）
        system_prompt = (
            "你是视频素材布局助手。用户上传了一张参考位置关系图（草图/截图/示意图都可能），"
            "里面展示了 4 个素材应该放在哪个相对位置：\n"
            "- video（主视频，通常占中间大面积）\n"
            "- subtitle（SRT 文件内嵌到视频上的文字）\n"
            "- cover（封面/角标，小图叠加层）\n"
            "- bg（背景图片，视频比例不一致时填充背景）\n\n"
            "请分析图片，输出 4 个素材的位置和缩放（1920×1080 画布的**像素坐标**，原点在左上角）：\n"
            "只返回合法 JSON，不要任何解释文字。格式严格如下：\n"
            "{\"video\":{\"x\":0,\"y\":0,\"scale\":1.0,\"enabled\":true},"
            "\"subtitle\":{\"x\":960,\"y\":972,\"scale\":1.0,\"enabled\":true},"
            "\"cover\":{\"x\":1536,\"y\":108,\"scale\":0.3,\"enabled\":true},"
            "\"bg\":{\"x\":0,\"y\":0,\"scale\":1.0,\"enabled\":false}}\n\n"
            "约束：x 是 -1920–1920 之间的整数像素（允许负数，做'露半边'效果）；"
            "y 是 -1080–1080 之间的整数像素（允许负数）；"
            "scale 是 0.1-2.0 之间的浮点数；enabled 必须是布尔值。"
        )
        user_text = (
            f"请分析这张参考位置关系图（任务 ID: {tid}），"
            f"输出 4 个素材在 {_FINE_DESIGN_W}×{_FINE_DESIGN_H} 画布上的像素坐标。"
        )

        try:
            raw = revision_service._call_llm_vision(
                system_prompt, user_text, [ref_path_abs], entry=vision_entry,
                usage_ctx={"task_id": tid, "stage": "精剪布局"},
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"多模态模型调用失败: {e}")

        # 4. 防御式解析 JSON
        import re as _re
        cleaned = _re.sub(r"```(?:json)?\s*|\s*```", "", raw or "").strip()
        # 截取首个 { 到最后一个 }
        s_idx, e_idx = cleaned.find("{"), cleaned.rfind("}")
        if s_idx < 0 or e_idx <= s_idx:
            return _err(f"模型输出不是合法 JSON: {(raw or '')[:200]}")
        try:
            parsed = json.loads(cleaned[s_idx:e_idx + 1])
        except Exception as e:  # noqa: BLE001
            return _err(f"模型输出不是合法 JSON: {e}")
        if not isinstance(parsed, dict):
            return _err("模型输出不是 JSON 对象")

        # 5. 字段校验 + 合并回 layout（兼容模型返回 0-1 旧版：自动按比例转像素）
        keys = ("video", "subtitle", "cover", "bg")
        merged: dict[str, dict] = {}
        for k in keys:
            v = parsed.get(k)
            if not isinstance(v, dict):
                continue
            try:
                x_raw = float(v.get("x", 0.5))
                y_raw = float(v.get("y", 0.5))
                scale = float(v.get("scale", 1.0))
                enabled = bool(v.get("enabled", True))
            except (TypeError, ValueError):
                continue
            scale = max(0.1, min(2.0, scale))
            # AI 模型可能仍按旧 prompt 输出 0-1：值在 [0,1] 时按比例放大；否则按像素护栏
            if 0.0 <= x_raw <= 1.0 and 0.0 <= y_raw <= 1.0:
                # 边界值 1.0 也按比例放大（避免 y=1.0 误判成「像素 1」）
                x = int(round(x_raw * _FINE_DESIGN_W))
                y = int(round(y_raw * _FINE_DESIGN_H))
            else:
                # v13：画布 X/Y 允许负数（−画布宽到+画布宽，−画布高到+画布高）
                x = max(-_FINE_DESIGN_W, min(_FINE_DESIGN_W, int(round(x_raw))))
                y = max(-_FINE_DESIGN_H, min(_FINE_DESIGN_H, int(round(y_raw))))
            merged[k] = {"x": x, "y": y, "scale": scale, "enabled": enabled}

        if not merged:
            execution_history.patch_fields(outputs_dir, exec_id, {
                "description": f"AI 未输出任何有效布局（{vision_entry['id']}）"
            })
            execution_history.record_finish(outputs_dir, exec_id, success=False,
                                            error="模型未输出任何有效素材布局")
            return _err(f"模型未输出任何有效素材布局: {(raw or '')[:200]}")

        for k, v in merged.items():
            fc["layout"][k] = {**fc["layout"].get(k, {}), **v}
        _save_fine_compose(mgr, tid, fc)
        # REQ-20260919-075：完成时回填具体执行情况
        execution_history.patch_fields(outputs_dir, exec_id, {
            "description": (f"AI 生成布局（{vision_entry['id']}）：{len(merged)} 个素材位置"
                            f"（{', '.join(merged.keys())}）")
        })
        execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
        return _ok(
            layout={k: fc["layout"][k] for k in keys},
            toast=f"✅ AI 已生成布局（{vision_entry['id']}）",
            model=vision_entry["id"],
        )

    @app.app.post("/slirn/api/save_fine_font")
    async def save_fine_font(body: dict = Body(default_factory=dict)):
        """保存精剪视频字幕字体 5 项设置。"""
        tid = (body.get("task_id") or "").strip()
        font = body.get("font") or {}
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception:  # noqa: BLE001 — TaskNotFoundError 等
            return _err(f"任务不存在: {tid}")
        if not t:
            return _err(f"任务不存在: {tid}")
        fc = _get_fine_compose(mgr, tid)
        # 字段白名单
        allowed = set(_FINE_FONT_DEFAULTS.keys())
        # REQ-20260919-062 v19：颜色字段只接受 #RRGGBB（前端 picker 一定给完整 6 位；
        # 拒绝非 hex 字符串以防注入或简写 #fff 导致渲染崩溃）
        for k, v in font.items():
            if k in allowed:
                if k in ("color", "stroke_color", "bg_color") and not _HEX_COLOR_OK.match(str(v)):
                    continue  # 非法值丢弃，保持原值
                fc["font"][k] = v
        _save_fine_compose(mgr, tid, fc)
        return _ok(toast="字体设置已保存")

    @app.app.post("/slirn/api/save_fine_output")
    async def save_fine_output(body: dict = Body(default_factory=dict)):
        """保存精剪视频输出设置（分辨率）。"""
        tid = (body.get("task_id") or "").strip()
        output = body.get("output") or {}
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception:  # noqa: BLE001 — TaskNotFoundError 等
            return _err(f"任务不存在: {tid}")
        if not t:
            return _err(f"任务不存在: {tid}")
        fc = _get_fine_compose(mgr, tid)
        if "resolution" in output and output["resolution"] in ("1080p", "720p", "source"):
            fc["output"]["resolution"] = output["resolution"]
        _save_fine_compose(mgr, tid, fc)
        return _ok(toast="输出设置已保存")

    @app.app.post("/slirn/api/save_fine_audio")
    async def save_fine_audio(body: dict = Body(default_factory=dict)):
        """REQ-20260919-061 扩展：保存背景音乐设置（4 项：启用/音量/淡入/淡出）。"""
        tid = (body.get("task_id") or "").strip()
        audio = body.get("audio") or {}
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception:  # noqa: BLE001 — TaskNotFoundError 等
            return _err(f"任务不存在: {tid}")
        if not t:
            return _err(f"任务不存在: {tid}")
        fc = _get_fine_compose(mgr, tid)
        # 字段白名单 + 类型规整
        allowed = set(_FINE_AUDIO_DEFAULTS.keys())
        for k, v in audio.items():
            if k not in allowed:
                continue
            if k == "enabled":
                fc["audio"][k] = bool(v)
            else:
                try:
                    val = float(v)
                    # REQ-20260922-NNN：volume_db 限定 [-40, 0]（API 直调防越界）
                    if k == "volume_db":
                        val = max(_FINE_VOLUME_DB_MIN, min(_FINE_VOLUME_DB_MAX, val))
                    fc["audio"][k] = val
                except (TypeError, ValueError):
                    pass  # 非法值跳过
        _save_fine_compose(mgr, tid, fc)
        return _ok(toast="背景音乐设置已保存")

    @app.app.post("/slirn/api/save_fine_preview")
    async def save_fine_preview(body: dict = Body(default_factory=dict)):
        """REQ-20260921-NNN：保存精剪·生成预览参数（start_h/m/s + duration）。
        与 layout/font/output/audio 同级，刷新页面/工作台不丢。
        """
        tid = (body.get("task_id") or "").strip()
        preview = body.get("preview") or {}
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception:  # noqa: BLE001
            return _err(f"任务不存在: {tid}")
        if not t:
            return _err(f"任务不存在: {tid}")
        fc = _get_fine_compose(mgr, tid)
        allowed = set(_FINE_PREVIEW_DEFAULTS.keys())
        # 整型字段（时:分:秒三段 + duration），非法值丢弃保持原值
        for k in allowed:
            if k not in preview:
                continue
            try:
                v = int(preview[k])
            except (TypeError, ValueError):
                continue
            # 字段级硬约束：m/s 最大 59；duration 钳到 2-30（与前端 input 一致）
            if k in ("start_m", "start_s"):
                v = max(0, min(59, v))
            elif k == "start_h":
                v = max(0, v)
            elif k == "duration":
                v = max(2, min(30, v))
            fc["preview"][k] = v
        _save_fine_compose(mgr, tid, fc)
        return _ok(toast="预览参数已保存")

    # ---------- 精剪视频·系统默认 BGM（REQ-20260920-078） ----------
    @app.app.post("/slirn/api/list_default_bgms")
    async def list_default_bgms(body: dict = Body(default_factory=dict)):
        """REQ-20260920-078：列出系统默认 BGM（带 available 标记）。

        前端在精剪面板「🎵 背景音乐」块加载时 fetch，渲染下拉选项；
        文件缺失则灰显，避免运行时崩溃。
        """
        return _ok(bgms=_get_default_bgms())

    # REQ-20260921-NNN：流程配置精剪合成阶段背景音乐下拉 — 合并系统默认 + 任务上传
    @app.app.post("/slirn/api/list_bgm_files")
    async def list_bgm_files(body: dict = Body(default_factory=dict)):
        """列出可选 BGM 文件：系统默认 + 任务上传的 audio 素材。

        返回 [{kind: 'default'|'uploaded', name, label, available, path?}]。
        """
        out = []
        # 系统默认（与精剪面板同一来源）
        for bgm in _get_default_bgms():
            out.append({
                "kind": "default",
                "name": bgm.get("filename", ""),
                "label": bgm.get("label") or bgm.get("filename", ""),
                "available": bool(bgm.get("available", False)),
            })
        # 任务上传的 audio 素材
        tid = (body.get("task_id") or "").strip()
        if tid:
            try:
                t = mgr.get(tid)
                if t:
                    fc = _get_fine_compose(mgr, tid)
                    audio_mat = (fc.get("materials") or {}).get("audio") or {}
                    audio_path = audio_mat.get("path") or ""
                    if audio_path:
                        out.append({
                            "kind": "uploaded",
                            "name": Path(audio_path).name,
                            "label": f"已上传：{Path(audio_path).name}",
                            "available": True,
                            "path": audio_path,
                        })
            except Exception:  # noqa: BLE001
                pass
        return _ok("", files=out)

    # ---------- REQ-20260920-088：素材路径详情 ----------
    @app.app.post("/slirn/api/material_info")
    async def material_info(body: dict = Body(default_factory=dict)):
        """查指定素材的物理路径 + 文件元数据（用于「🔍 详情」模态框）。

        Body: {task_id: str, kind: str}
        Return: {ok, kind, source, source_label, fc_path, abs_path, exists,
                 size_bytes, mtime, type, upstream_name, upstream_exists}
        """
        from datetime import datetime

        task_id = (body.get("task_id") or "").strip()
        kind = (body.get("kind") or "").strip()
        if not task_id:
            return _err("缺少 task_id")
        if kind not in _FINE_MATERIAL_KINDS:
            return _err(f"非法素材类型: {kind!r}")
        try:
            mgr.get(task_id)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        fc = _get_fine_compose(mgr, task_id)
        mat = (fc.get("materials") or {}).get(kind) or {}
        fc_path = mat.get("path") or ""
        # 推 source
        source = mat.get("source") or ""
        if not source and fc_path:
            source = "upload"  # 兜底：旧数据没 source 字段
        # audio 来自 materials/audio/<id>.mp3 → 系统提供的 BGM 复制品
        source_label = ""
        if kind == "audio" and fc_path.startswith(f"tasks/{task_id}/materials/audio/"):
            source = "default_bgm"
            source_label = "系统提供的 BGM"
        elif source == "auto":
            source_label = "上游产物"
        elif source == "upload":
            source_label = "用户上传"
        # 解析物理路径（复用 _resolve_mat_abs 3 候选兜底）
        abs_path = None
        if fc_path:
            abs_path = _resolve_mat_abs(mgr, task_id, fc.get("materials", {}), kind)
        # stat
        size_bytes = 0
        mtime = ""
        exists = False
        if abs_path is not None and abs_path.exists():
            exists = True
            try:
                st = abs_path.stat()
                size_bytes = st.st_size
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                pass
        # 上游产物信息（仅 auto kind）
        upstream_name = ""
        upstream_exists = None
        if kind in _FINE_AUTO_KINDS:
            upstream_name = _fine_upstream_label(task_id, kind, mgr)
            upstream_exists = bool(_fine_upstream_path(task_id, kind, mgr))
        # type 字段
        type_str = {"video": "video", "subtitle": "subtitle", "audio": "audio"}.get(kind, "image")
        return _ok(
            kind=kind,
            source=source,
            source_label=source_label,
            fc_path=fc_path,
            abs_path=str(abs_path) if abs_path else "",
            exists=exists,
            size_bytes=size_bytes,
            mtime=mtime,
            type=type_str,
            upstream_name=upstream_name,
            upstream_exists=upstream_exists,
        )

    @app.app.post("/slirn/api/select_default_bgm")
    async def select_default_bgm(body: dict = Body(default_factory=dict)):
        """REQ-20260920-078：选某个系统默认 BGM → 复制到任务目录 + 写 fc。

        流程：
        1. 校验 bgm_id 在白名单内
        2. 校验源文件存在
        3. shutil.copy2 复制到 tasks/{tid}/materials/audio/<id>.mp3
        4. fc.materials.audio = {"path": "materials/audio/<id>.mp3"}
        5. fc.audio.enabled = True（自动启用）
        6. 返回 audio_url 用于前端预览
        """
        import shutil as _sh
        tid = (body.get("task_id") or "").strip()
        bgm_id = (body.get("bgm_id") or "").strip()
        if not tid or not bgm_id:
            return _err("缺少 task_id 或 bgm_id")
        bgm = next((b for b in _DEFAULT_BGMS if b["id"] == bgm_id), None)
        if not bgm:
            return _err(f"未知的 bgm_id: {bgm_id}")
        src = _DEFAULT_BGMS_DIR / bgm["filename"]
        if not src.exists():
            return _err(f"系统默认 BGM 文件不存在: {src}")
        try:
            t = mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        if not t:
            return _err(f"任务不存在: {tid}")

        # 目标路径：tasks/{tid}/materials/audio/<id>.mp3
        mat_dir = mgr.tasks_dir / tid / "materials" / "audio"
        mat_dir.mkdir(parents=True, exist_ok=True)
        dst = mat_dir / f"{bgm_id}.mp3"
        try:
            _sh.copy2(src, dst)
        except OSError as e:
            return _err(f"复制 BGM 失败: {e}")

        # 写 fc：materials.audio.path + audio.enabled=True
        # REQ-20260920-083：路径必须含 tasks/<tid>/ 前缀，让 _resolve_mat_abs cand2 命中
        # （与 upload_fine_material_form 写的路径约定一致；旧版写 'materials/audio/<id>.mp3'
        # 无前缀 → resolver 找不到 → audio 被静默禁用 → 预览/导出没 BGM）
        fc = _get_fine_compose(mgr, tid)
        fc.setdefault("materials", {})["audio"] = {
            "path": f"tasks/{tid}/materials/audio/{bgm_id}.mp3",
        }
        fc.setdefault("audio", {})["enabled"] = True
        _save_fine_compose(mgr, tid, fc)

        audio_url = f"/slirn/api/video/{tid}?src=mat&kind=audio&t={int(time.time())}"
        return _ok(
            audio_url=audio_url,
            name=bgm["name"],
            toast=f"✅ 已选 BGM: {bgm['name']}",
        )

    # ---------- 精剪视频·全局参数模板（REQ-20260919-061 扩展：跨任务复用） ----------
    from slirn_home import fine_profiles as _fine_profiles

    @app.app.post("/slirn/api/list_fine_global_profiles")
    async def list_fine_global_profiles(body: dict = Body(default_factory=dict)):
        """列出所有精剪全局参数模板。"""
        profiles = _fine_profiles.list_profiles(repo_root)
        # 仅返回 UI 需要的字段（params 体积较大，省略除非显式需要）
        out = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "saved_at": p.get("saved_at"),
                "task_id_origin": p.get("task_id_origin"),
            }
            for p in profiles
        ]
        return _ok(profiles=out)

    @app.app.post("/slirn/api/save_fine_global_profile")
    async def save_fine_global_profile(body: dict = Body(default_factory=dict)):
        """把当前 task 的 layout/font/output/audio 保存为命名模板。"""
        tid = (body.get("task_id") or "").strip()
        name = (body.get("name") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        if not name:
            return _err("模板名不能为空")
        if len(name) > 30:
            return _err("模板名不能超过 30 字")
        try:
            t = mgr.get(tid)
        except Exception:  # noqa: BLE001
            return _err(f"任务不存在: {tid}")
        if not t:
            return _err(f"任务不存在: {tid}")
        fc = _get_fine_compose(mgr, tid)
        params = {
            "layout": fc.get("layout") or {},
            "font": fc.get("font") or {},
            "output": fc.get("output") or {},
            "audio": fc.get("audio") or {},
            # REQ-20260920-076：把背景图检测结果也存进模板（与任务级 export 同口径），
            # 旧模板（无 detected_region 字段）→ get() 返回 None，应用时不动目标任务的。
            "detected_region": fc.get("detected_region"),
            # REQ-20260921-NNN-preview-export：生成预览参数也存进模板
            # （与 SAVE_PARAM_KEYS 含 preview 配对，端点实际传值才生效）
            "preview": fc.get("preview"),
        }
        profile = _fine_profiles.save_profile(repo_root, name, params, task_id_origin=tid)
        return _ok(
            profile={
                "id": profile["id"],
                "name": profile["name"],
                "saved_at": profile["saved_at"],
                "task_id_origin": profile["task_id_origin"],
            },
            toast=f"✅ 已保存模板「{profile['name']}」",
        )

    @app.app.post("/slirn/api/apply_fine_global_profile")
    async def apply_fine_global_profile(body: dict = Body(default_factory=dict)):
        """把模板 params 写入当前 task 的 fine_compose（六组全量回填）。

        REQ-20260930-093：覆盖 layout/font/output/audio/detected_region/preview 六组
        （此前 detected_region/preview 只存不回填）。
        不动 materials — 任务自己的素材文件路径保持不变。
        旧模板 params 里缺的键（如 detected_region/preview）→ 不动目标任务对应字段。
        返回新的 wb HTML，UI 直接替换刷新。
        """
        tid = (body.get("task_id") or "").strip()
        profile_id = (body.get("profile_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        if not profile_id:
            return _err("缺少 profile_id")
        try:
            t = mgr.get(tid)
        except Exception:  # noqa: BLE001
            return _err(f"任务不存在: {tid}")
        if not t:
            return _err(f"任务不存在: {tid}")
        profile = _fine_profiles.get_profile(repo_root, profile_id)
        if not profile:
            return _err(f"模板不存在或已删除: {profile_id}")
        fc = _get_fine_compose(mgr, tid)
        params = profile.get("params") or {}
        # 六组全量回填（materials 永不动；旧模板缺的键自动跳过）
        for key in _fine_profiles.PROFILE_PARAM_KEYS:
            if key in params:
                fc[key] = params[key]
        _save_fine_compose(mgr, tid, fc)
        # 返新 wb HTML，前端直接替换 #slirn-tab-workbench-inner
        wb_html = _render_workbench(tid, mgr)
        return _ok(html=wb_html, toast=f"✅ 已应用模板「{profile.get('name')}」")

    @app.app.post("/slirn/api/delete_fine_global_profile")
    async def delete_fine_global_profile(body: dict = Body(default_factory=dict)):
        """从全局 JSON 移除指定模板（不影响已应用的任务）。"""
        profile_id = (body.get("profile_id") or "").strip()
        if not profile_id:
            return _err("缺少 profile_id")
        ok = _fine_profiles.delete_profile(repo_root, profile_id)
        if not ok:
            return _err(f"模板不存在或已删除: {profile_id}")
        return _ok(toast="🗑 已删除模板")

    @app.app.post("/slirn/api/rename_fine_global_profile")
    async def rename_fine_global_profile(body: dict = Body(default_factory=dict)):
        """重命名指定模板（重名时自动加 `(2)` 后缀）。"""
        profile_id = (body.get("profile_id") or "").strip()
        new_name = (body.get("name") or "").strip()
        if not profile_id:
            return _err("缺少 profile_id")
        if not new_name:
            return _err("新名不能为空")
        if len(new_name) > 30:
            return _err("新名不能超过 30 字")
        ok = _fine_profiles.rename_profile(repo_root, profile_id, new_name)
        if not ok:
            return _err(f"模板不存在或已删除: {profile_id}")
        return _ok(toast="✏️ 已重命名")

    @app.app.post("/slirn/api/export_fine_global_profile")
    async def export_fine_global_profile(body: dict = Body(default_factory=dict)):
        """REQ-20260919-070：把单个全局模板参数下载为 JSON 文件。

        与 `export_fine_params`（当前任务）同口径：返回 {filename, content, mime}，
        前端用 Blob 触发下载。模板不含 materials（按 REQ-20260919-061 设计），
        故 payload.materials = {}（与任务导出口径一致，导入端按"无 materials"
        处理）。
        """
        pid = (body.get("profile_id") or "").strip()
        if not pid:
            return _err("缺少 profile_id")
        prof = _fine_profiles.get_profile(repo_root, pid)
        if not prof:
            return _err(f"模板不存在或已删除: {pid}")
        params = prof.get("params") or {}
        payload = {
            "_schema": 3,
            "_exported_at": datetime.now().isoformat(timespec="seconds"),
            "_source_profile_id": pid,
            "_source_profile_name": prof.get("name", ""),
            "_source_task_id": prof.get("task_id_origin", ""),
            "materials": {},  # 全局模板按设计不含素材
            "layout": params.get("layout"),
            "font": params.get("font"),
            "output": params.get("output"),
            "audio": params.get("audio"),
            # REQ-20260920-076：与任务级 export_fine_params 口径一致；旧模板
            # （无 detected_region 字段）→ params.get("detected_region") = None
            "detected_region": params.get("detected_region"),
        }
        # 文件名：清理模板名里的非法字符 + 时间戳
        safe_name = re.sub(r'[\\/:*?"<>|\s]+', "_", prof.get("name") or "profile")[:30]
        filename = (
            f"fine_params_profile_{safe_name}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        try:
            content = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            return _err(f"序列化失败: {e}")
        return _ok(filename=filename, content=content, mime="application/json")

    @app.app.post("/slirn/api/render_fine_preview")
    async def render_fine_preview(request: Request, body: dict = Body(default_factory=dict)):
        """REQ-20260919-061 Phase B：渲染精剪视频预览（ffmpeg overlay）。

        REQ-20260919-061 用户反馈：预览时长可调（2–30 秒，默认 10）。body 里读
        `duration` 字段，无效值兜底 10。

        REQ-20260919-064：新增 `preview_start` 字段（默认 0），让用户能跳到源视频
        任意时间点预览。start + duration 由后端钳到不超出源视频时长
        （start 不会负；不会因 ffmpeg 0-frame 报错）。

        输出：outputs/fine_preview.mp4（相对 slirn-standalone 根）

        REQ-20260920-081：执行历史埋点（KIND_FINE_PREVIEW，同步任务，开始/结束成对记录）。
        """
        from slirn_home import execution_history
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        # REQ-20260920-081：执行历史 — 同步渲染开始时记 running
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        _exec_id = execution_history.record_start(
            outputs_dir,
            execution_history.KIND_FINE_PREVIEW,
            extra={"duration_sec": None, "preview_start": 0.0},  # 下方覆盖
            auto=_auto,
            auto_session_id=_auto_session_id,
        )
        # 解析预览时长，钳到 [2, 30]（前端已有 min/max 限制，后端兜底防越界）
        raw_dur = body.get("duration")
        try:
            preview_dur = float(raw_dur)
        except (TypeError, ValueError):
            preview_dur = 10.0
        preview_dur = max(2.0, min(30.0, preview_dur))
        # REQ-20260919-064：解析预览开始时间，默认 0
        raw_start = body.get("preview_start", 0)
        try:
            preview_start = float(raw_start)
        except (TypeError, ValueError):
            preview_start = 0.0
        preview_start = max(0.0, preview_start)
        # 钳 start+duration 不超出源视频时长（用 ffprobe 拿时长；失败兜底不钳）
        try:
            import subprocess as _sp
            from pathlib import Path as _P
            fc_for_dur = _get_fine_compose(mgr, tid)
            vp = _resolve_mat_abs(mgr, tid, fc_for_dur["materials"], "video")
            if vp and _P(vp).exists():
                _pr = _sp.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", str(vp)],
                    capture_output=True, text=True, timeout=10,
                )
                _vdur = float((_pr.stdout or "0").strip() or "0")
                if _vdur > 0 and preview_start + preview_dur > _vdur:
                    # start 后移，让 start+duration = video_duration
                    preview_start = max(0.0, _vdur - preview_dur)
        except Exception:
            pass  # ffprobe 失败 → 不钳，让 ffmpeg 自己处理（最多产生 0-frame 报错）
        out_path = mgr.tasks_dir / tid / "outputs" / "fine_preview.mp4"
        # REQ-20260920-098：combo 透传（None 时仅依赖 fc.json，向后兼容）
        combo = body.get("combo")
        if not isinstance(combo, dict):
            combo = None
        result = _run_fine_render(tid, mgr, out_path,
                                   duration=preview_dur, preview_start=preview_start,
                                   combo=combo)
        # REQ-20260920-081：执行历史 — 同步渲染完成回填 finish
        if _exec_id:
            try:
                execution_history.patch_extra(
                    outputs_dir, _exec_id,
                    {"duration_sec": preview_dur, "preview_start": preview_start,
                     "output_path": str(out_path)},
                )
                execution_history.record_finish(
                    outputs_dir, _exec_id,
                    success=bool(result.get("ok")),
                    error=str(result.get("error") or "")[:500],
                )
            except Exception:
                pass
        if not result.get("ok"):
            return result
        return _ok(
            url=f"/slirn/api/video/{tid}?src=fine_preview&t={int(time.time())}",
            toast=f"🎬 预览已生成（第 {preview_start:g}–{preview_start + preview_dur:g} 秒）",
        )

    @app.app.post("/slirn/api/export_fine_video")
    async def export_fine_video(request: Request, body: dict = Body(default_factory=dict)):
        """REQ-20260919-074：导出最终精剪视频（异步后台任务）。

        用户反馈：实际最终视频 1-3 小时长，旧的同步 `_run_fine_render` timeout=120s
        完全不够；fetch 同步等 1-3 小时浏览器也会卡死。这次改为：

        1. 本端点立即返回 `{job_id, toast}`（不阻塞，<0.5 秒）
        2. 后台 daemon 线程跑 `_run_fine_render_async`，写 `_JOB_REGISTRY[job_id]`
        3. 前端 GET `/slirn/api/render_status?job_id=X` 轮询拿进度
        4. 完成后前端点关闭 → 调 `/slirn/api/cancel_render` 不会触发（job 已 done）

        REQ-20260920-081：record_start 写一条 KIND_FINE_EXPORT 历史，exec_id 和
        outputs_dir 传给后台线程用于 record_finish（4 个出口：assemble 失败 /
        FileNotFound / 取消 / returncode 非 0 / returncode 0）。auto + session_id
        从 X-Slirn-Auto / X-Slirn-Auto-Session 透传（REQ-20260919-075 + 081）。
        """
        from slirn_home import execution_history
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")

        # 清理过期 job
        _cleanup_stale_jobs(mgr)

        # 同一任务已有运行中/排队的 job → 拒绝重启（避免并发写同一文件）
        with _JOB_LOCK:
            for existing in _JOB_REGISTRY.values():
                if existing.task_id == tid and existing.state in ("queued", "running"):
                    return _err(
                        f"该任务已有运行中的渲染（job_id={existing.job_id}），请等待完成或取消"
                    )

        # 创建 job + 启线程
        job_id = f"job_{int(time.time() * 1000)}_{os.getpid()}"
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        # REQ-20260920-091：开始时间 + 时长（不导完整视频）
        _start = float(body.get("preview_start") or 0.0)
        _dur = body.get("duration")
        _dur_f: float | None = float(_dur) if _dur is not None else None
        # 输出文件名：默认 fine_export.mp4；有 time 参数时加 _t{start}_d{dur}.mp4 后缀防覆盖
        if _start == 0.0 and _dur_f is None:
            out_path = outputs_dir / "fine_export.mp4"
        else:
            _dur_tag = f"{_dur_f:.1f}" if _dur_f is not None else "full"
            out_path = outputs_dir / f"fine_export_t{_start:.1f}_d{_dur_tag}.mp4"
        job = _RenderJob(job_id=job_id, task_id=tid)
        with _JOB_LOCK:
            _JOB_REGISTRY[job_id] = job
        # REQ-20260920-084：写 .export_job.json 落盘文件，供页面刷新后回查
        _write_active_export_job(mgr, tid, job_id, "queued")

        # REQ-20260920-098：combo 透传（None 时仅依赖 fc.json，向后兼容）
        combo = body.get("combo")
        if not isinstance(combo, dict):
            combo = None

        # REQ-20260920-081：执行历史埋点（立即记 running；后台线程 4 个出口都补 finish）
        fc_for_extra = _get_fine_compose(mgr, tid)
        _ext_exec = execution_history.record_start(
            outputs_dir,
            execution_history.KIND_FINE_EXPORT,
            extra={
                "job_id": job_id,
                "resolution": (fc_for_extra.get("output") or {}).get("resolution", "1080p"),
                "bgm_enabled": bool((fc_for_extra.get("audio") or {}).get("enabled", False)),
            },
            auto=_auto,
            auto_session_id=_auto_session_id,
        )

        t = threading.Thread(
            target=_run_fine_render_async,
            args=(job, tid, mgr, out_path, _ext_exec, outputs_dir, _start, _dur_f),
            kwargs={"combo": combo},
            daemon=True,
            name=f"fine-render-{job_id}",
        )
        t.start()

        return _ok(
            job_id=job_id,
            toast=f"🎬 导出已启动（{tid[:8]}），可在本面板下方看进度",
        )

    @app.app.get("/slirn/api/render_status")
    async def render_status(job_id: str):
        """REQ-20260919-074：返回指定 job 的实时进度。

        前端每 1.5 秒轮询一次。完成后保留 5 分钟供最后一次查询拿到 output_url。

        REQ-20260920-084：elapsed_sec 在 GET 时实时算（基于 time.monotonic()），
        避免 ffmpeg init 阶段没输出 out_time_ms= 导致进度时间一直 0。
        """
        if not job_id:
            return _err("缺少 job_id")
        with _JOB_LOCK:
            job = _JOB_REGISTRY.get(job_id)
        if not job:
            return _err(f"job 不存在或已过期（>{_JOB_TTL_SEC // 60} 分钟）: {job_id}")
        # REQ-20260920-084：实时算 elapsed（不依赖 ffmpeg progress 行）
        if job.state == "running" and job.started_at > 0:
            elapsed_live = time.monotonic() - job.started_at
        elif job.state in ("done", "failed", "cancelled") and job.started_at > 0 and job.finished_at > 0:
            elapsed_live = job.finished_at - job.started_at
        else:
            elapsed_live = job.elapsed_sec  # queued 或未启动
        return _ok(
            job_id=job.job_id,
            task_id=job.task_id,
            state=job.state,
            elapsed_sec=round(elapsed_live, 1),
            progress_pct=round(job.progress_pct, 1),
            progress_time_ms=job.progress_time_ms,
            total_duration_ms=job.total_duration_ms,
            speed_x=round(job.speed_x, 2),
            eta_sec=round(job.eta_sec, 1) if job.eta_sec >= 0 else None,
            error=job.error,
            output_url=job.output_url,
            encoder=job.encoder,
            # REQ-20260923-NNN：失败/截断诊断用（done 时为空串）
            stderr_tail=(job.stderr_tail[-1000:] if job.state == "failed" else ""),
        )

    @app.app.get("/slirn/api/active_export_for_task")
    async def active_export_for_task(task_id: str):
        """REQ-20260920-084：查指定 task 是否有 in-flight 导出 job（页面刷新后回显）。

        优先查内存 _JOB_REGISTRY（权威、实时）；落盘 .export_job.json 兜底
        （服务重启但 ffmpeg 仍在的场景）。
        返回：{ok: true, job: {job_id, state, started_at, source, warning?}} 或 job: null
        """
        if not task_id:
            return _err("缺少 task_id")
        try:
            mgr.get(task_id)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")

        # 优先：内存注册表（权威，实时）
        with _JOB_LOCK:
            for j in _JOB_REGISTRY.values():
                if j.task_id == task_id and j.state in ("queued", "running"):
                    return _ok(job={
                        "job_id": j.job_id,
                        "state": j.state,
                        "started_at": j.wall_started_at,
                        "source": "registry",
                    })

        # 兜底：落盘文件（服务重启但 ffmpeg 仍在跑）
        disk = _read_active_export_job(mgr, task_id)
        if not disk:
            return _ok(job=None)
        job_id_disk = disk.get("job_id") or ""
        if not job_id_disk:
            try: _delete_active_export_job(mgr, task_id)
            except Exception: pass
            return _ok(job=None)
        return _ok(job={
            "job_id": job_id_disk,
            "state": disk.get("state", "running"),
            "started_at": disk.get("started_at", 0),
            "source": "disk",
            "warning": "内存无此 job（服务可能已重启）；ffmpeg 状态未知",
        })

    @app.app.post("/slirn/api/cancel_render")
    async def cancel_render(body: dict = Body(default_factory=dict)):
        """REQ-20260919-074：取消正在渲染的 job（SIGTERM → 5 秒后 SIGKILL 兜底）。"""
        job_id = (body.get("job_id") or "").strip()
        if not job_id:
            return _err("缺少 job_id")
        with _JOB_LOCK:
            job = _JOB_REGISTRY.get(job_id)
        if not job:
            return _err(f"job 不存在: {job_id}")
        if job.state not in ("queued", "running"):
            return _err(f"job 已处于终态（{job.state}），无法取消")
        # 先标记 cancelled，再发信号（让后台线程主循环的 stdout 读返回后能立即退出）
        job.state = "cancelled"
        job.finished_at = time.monotonic()
        job.wall_finished_at = time.time()
        if job.proc:
            try:
                job.proc.terminate()
            except Exception as e:
                return _err(f"取消信号发送失败: {e}")
        return _ok(toast="⏹ 已发送取消信号")

    # REQ-20260919-065：精剪参数 JSON 导出/导入
    @app.app.post("/slirn/api/export_fine_params")
    async def export_fine_params(body: dict = Body(default_factory=dict)):
        """导出精剪参数为 JSON 字符串（不含素材文件本体，也不上传素材路径）。

        REQ-20260919-073：materials 是任务本地上传的素材（视频/字幕/封面/BGM/参考图
        的文件路径），跨任务/跨机器复用无意义。导入端始终不动 materials（见
        import_fine_params），故导出端本就不该把 materials 写进文件 — 与全局模板
        `export_fine_global_profile` 同口径。

        返回 {ok, filename, content, mime} → 前端用 Blob 触发下载。
        """
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        fc = _get_fine_compose(mgr, tid)
        payload = {
            # REQ-20260921-NNN-preview-export：v3 → v4 加 preview 字段
            # （「生成预览」开始时间 HH:MM:SS + 预览时长）。
            # v3 文件仍可被 import_fine_params 接受（preview 缺省即不写回）。
            "_schema": 4,
            "_exported_at": datetime.now().isoformat(timespec="seconds"),
            "_source_task_id": tid,
            "materials": {},    # REQ-20260919-073：素材路径不是设置参数，不导出
            "layout": fc.get("layout"),
            "font": fc.get("font"),
            "output": fc.get("output"),
            "audio": fc.get("audio"),
            "detected_region": fc.get("detected_region"),
            "preview": fc.get("preview"),    # v4 新增：start_h/m/s + duration
        }
        filename = f"fine_params_{tid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        try:
            content = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            return _err(f"序列化失败: {e}")
        return _ok(filename=filename, content=content, mime="application/json")

    @app.app.post("/slirn/api/import_fine_params")
    async def import_fine_params(body: dict = Body(default_factory=dict)):
        """导入精剪参数 JSON 字符串 → 覆盖当前任务 layout/font/output/audio/detected_region。
        不修改 materials（视频/封面/BGM 文件保持当前任务的）。
        返回 {ok, applied_fields} → 前端用 toast + 刷新 wb。
        """
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        raw = body.get("content")
        if not isinstance(raw, str):
            return _err("导入内容必须为 JSON 字符串")
        # 64KB 上限（实际参数体积远低于此）
        if len(raw) > 64 * 1024:
            return _err("文件过大（>64KB），拒绝导入")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            return _err(f"JSON 解析失败: {e}")
        if not isinstance(payload, dict):
            return _err("JSON 顶层必须是对象")
        schema = payload.get("_schema")
        if schema not in (2, 3, 4):
            return _err(
                # REQ-20260921-NNN-preview-export：v4 加 preview
                f"参数文件 _schema 不兼容（当前仅支持 2/3/4，文件为 {schema}）"
            )
        # 校验 6 个字段类型（必须 dict 或 null/缺省）
        for key in ("layout", "font", "output", "audio", "detected_region", "preview"):
            v = payload.get(key)
            if v is not None and not isinstance(v, dict):
                return _err(f"{key} 必须是 dict 或 null")
        fc = _get_fine_compose(mgr, tid)
        applied: list[str] = []
        for key in ("layout", "font", "output", "audio", "detected_region"):
            if key in payload:
                fc[key] = payload[key]
                applied.append(key)
        # REQ-20260921-NNN-preview-export：preview 字段级钳制（参照 save_fine_preview 同口径）
        # start_h ≥ 0；start_m/start_s ∈ [0, 59]；duration ∈ [2, 30]
        # 非整数 → 跳过该项；只接受 _FINE_PREVIEW_DEFAULTS 内的字段（未知字段丢弃）
        # 合并到 fc["preview"]，未提供的字段保留原值（不强制重置）
        if "preview" in payload:
            raw = payload["preview"]
            if isinstance(raw, dict):
                clamped: dict = {}
                for pk in _FINE_PREVIEW_DEFAULTS.keys():
                    if pk not in raw:
                        continue
                    try:
                        n = int(raw[pk])
                    except (TypeError, ValueError):
                        continue
                    if pk in ("start_m", "start_s"):
                        n = max(0, min(59, n))
                    elif pk == "start_h":
                        n = max(0, n)
                    elif pk == "duration":
                        n = max(2, min(30, n))
                    clamped[pk] = n
                if clamped:
                    base = fc.get("preview") or dict(_FINE_PREVIEW_DEFAULTS)
                    fc["preview"] = {**base, **clamped}
                    applied.append("preview")
        try:
            _save_fine_compose(mgr, tid, fc)
        except Exception as e:  # noqa: BLE001
            return _err(f"保存失败: {e}")
        return _ok(applied_fields=applied, toast=f"✅ 已应用 {len(applied)} 个字段")

    @app.app.post("/slirn/api/upload_fine_material_form")
    async def upload_fine_material_form(
        task_id: str = _Form(...),
        kind: str = _Form(...),
        file: UploadFile = File(...),
    ):
        """REQ-20260919-061：上传精剪视频素材 — multipart/form-data。
        file 存到 tasks/{tid}/upload/{kind}_{filename}，路径写到 task.json:fine_compose.materials.{kind}.path
        """
        if kind not in _FINE_MATERIAL_KINDS:
            return _err(f"非法素材类型: {kind}")
        t = mgr.get(task_id)
        if not t:
            return _err(f"任务不存在: {task_id}")
        # 防路径穿越 + 安全文件名
        safe_name = Path(file.filename or f"{kind}_upload").name
        save_dir = repo_root / "tasks" / task_id / "upload"
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / f"{kind}_{safe_name}"
        # 写文件
        try:
            content = await file.read()
            save_path.write_bytes(content)
        except Exception as e:  # noqa: BLE001
            return _err(f"保存失败: {e}")
        # 写回 task.json
        fc = _get_fine_compose(mgr, task_id)
        fc["materials"][kind] = {
            "path": str(save_path.relative_to(repo_root)),
            "type": {"video": "video", "subtitle": "srt", "audio": "audio"}.get(kind, "image"),
            "source": "upload",
        }
        _save_fine_compose(mgr, task_id, fc)
        # REQ-20260920-085：上传音频素材时自动启用 BGM（与 select_default_bgm 对齐）
        # 修复「上传 mp3 后生成预览无 BGM」BUG：原代码只写 materials.audio.path，
        # 不写 audio.enabled → _assemble_fine_filter audio gate 把 BGM 静默禁用。
        # 仅当 enabled 仍是默认值 False 时才自动启用（不覆盖用户手动 toggle）：
        #   - 全新任务（fc.audio 由 _get_fine_compose 默认填 enabled=False）→ 自动开 ✅
        #   - 用户已 save_fine_audio 设过 enabled=True → 已是 True（再写幂等）✅
        #   - 用户已 save_fine_audio 设过 enabled=False → 保留 False，不强行打开 ✅
        # volume_db 由 _FINE_AUDIO_DEFAULTS 默认 -8 dB 提供，无需再写。
        if kind == "audio":
            audio_block = fc.setdefault("audio", {})
            if audio_block.get("enabled", _FINE_AUDIO_DEFAULTS["enabled"]) == _FINE_AUDIO_DEFAULTS["enabled"]:
                audio_block["enabled"] = True
            _save_fine_compose(mgr, task_id, fc)
        # REQ-20260920-079：超大图片上传提示（前端 toast；不动原图）
        warning: str | None = None
        if kind in ("bg", "cover", "reference") and save_path.suffix.lower() in _PIL_IMAGE_EXTS:
            try:
                from PIL import Image as _PILImage2
                with _PILImage2.open(save_path) as _im:
                    iw, ih = _im.size
                    if max(iw, ih) > _PRESCALE_THRESHOLD_PX:
                        res = (fc.get("output") or {}).get("resolution", "1080p")
                        tw, th = (1280, 720) if res == "720p" else (1920, 1080)
                        warning = (
                            f"💡 上传图片 {iw}×{ih} 较大，合成视频时会自动缩放至 "
                            f"{tw}×{th}（源文件保留原图）"
                        )
            except Exception:
                pass  # 损坏的图片 / PIL 异常 → 不警告，让后续渲染兜底
        return _ok(
            path=fc["materials"][kind]["path"],
            toast=f"{_FINE_MATERIAL_LABELS[kind][1]}已上传",
            warning=warning,
        )

    # REQ-20260919-063 用户反馈：每个素材都要可预览（图片/视频/音频/SRT 文本）。
    # 直接 GET 这个端点拿到素材文件本体（带正确 Content-Type），前端用 <img>/<video>/<audio>
    # 或 fetch() 文本即可。不经 Gradio 文件白名单（任务文件在 slirn-standalone/tasks/ 下）。
    @app.app.get("/slirn/api/fine_material_file")
    async def serve_fine_material_file(
        task_id: str, kind: str,
    ):
        """返回指定素材文件本体。前端 <img>/<video>/<audio> 直接 src= 此 URL。

        - 图片（cover/bg/reference）→ image/png|jpeg 等
        - 视频（video）→ video/mp4 等
        - 音频（audio）→ audio/mpeg|wav|mp4 等
        - 字幕（subtitle，.srt）→ text/plain；charset=utf-8
        """
        from fastapi.responses import FileResponse as _FR
        from fastapi import HTTPException as _HTTP

        if kind not in _FINE_MATERIAL_KINDS:
            raise _HTTP(400, f"非法素材类型: {kind}")
        try:
            t = mgr.get(task_id)
        except Exception:
            raise _HTTP(404, f"任务不存在: {task_id}")
        if not t:
            raise _HTTP(404, f"任务不存在: {task_id}")
        fc = _get_fine_compose(mgr, task_id)
        mat = fc.get("materials", {}).get(kind) or {}
        path_rel = mat.get("path")
        if not path_rel:
            raise _HTTP(404, f"任务 {task_id} 未上传 {kind}")
        abs_path = _resolve_mat_abs(mgr, task_id, fc["materials"], kind)
        if not abs_path or not abs_path.exists():
            raise _HTTP(404, f"素材文件不存在: {path_rel}")
        # 按扩展名选 Content-Type
        ext = abs_path.suffix.lower()
        _MT = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
            ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
            ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
            ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
            ".srt": "text/plain; charset=utf-8",
        }
        media_type = _MT.get(ext, "application/octet-stream")
        # 不让浏览器强缓存（任务素材可能被替换）
        return _FR(
            abs_path, media_type=media_type,
            headers={"Cache-Control": "no-cache"},
        )

    @app.app.post("/slirn/api/delete_task")
    async def delete_task(body: dict = Body(default_factory=dict)):
        """删除任务（REQ-20260917-034）— 真删：整个任务目录从磁盘移除，不可恢复。

        失败必须如实报错（旧版吞异常假报「已删除」，任务刷新后复活）；
        Windows 下文件被占用（如视频预览握着句柄）是常见失败原因。
        """
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.delete(tid)
        except TaskNotFoundError as e:
            return _err(f"任务不存在或已删除: {e}")
        except OSError as e:
            log.exception("删除任务 %s 失败（真删未完成）", tid)
            return _err(f"删除失败: {e} — 可能有文件正被预览/占用，关闭预览后重试")
        return _ok(_task_list_for_cur_user(), toast="✅ 已删除（任务文件已从磁盘移除）")

    # REQ-20260921-NNN-outputs-browser：列出任务 outputs/ + 任务根下的全部产物
    # 给「📦 查看产物」面板用 — 之前用户必须自己翻目录或记文件名（区间导出是
    # fine_export_t*_d*.mp4，每次文件名都变）。现在一站式列出 + 人类标签 + 预览/下载链接。
    @app.app.post("/slirn/api/list_outputs")
    async def list_outputs(body: dict = Body(default_factory=dict)):
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except TaskNotFoundError:
            return _err("任务不存在或已删除")
        except Exception:  # noqa: BLE001
            return _err("任务不存在或已删除")

        outputs_dir = mgr.tasks_dir / tid / "outputs"
        task_root = mgr.tasks_dir / tid

        # 已知文件名 → (label, kind, previewable) 的标签表
        # kind: video / subtitle / audio / json / image / other
        # previewable: True = 可在 <video>/<audio>/subtitles 容器里直接展示
        KNOWN_FILES = {
            # 视频产物（按文件名 pattern，因为区间导出每次文件名都变）
            "rough_compose.mp4": ("粗剪成片", "video", True),
            "optimize_compose.mp4": ("优化成片（标记删除行已剪除）", "video", True),
            "fine_export.mp4": ("最终导出视频（全片）", "video", True),
            "fine_preview.mp4": ("精剪预览（全片）", "video", True),
            # 字幕产物
            "subtitle.json": ("原始字幕 JSON", "json", False),
            "subtitle.srt": ("原始字幕 SRT", "subtitle", True),
            "optimize.json": ("优化字幕 JSON", "json", False),
            "optimize.srt": ("优化字幕 SRT", "subtitle", True),
            "optimize_subtitle.json": ("优化字幕 JSON", "json", False),
            "optimize_compose.srt": ("优化成片字幕 SRT（时间轴已前移）", "subtitle", True),
            "optimize_cut.json": ("优化成片剪辑记录 JSON", "json", False),
            "revision.json": ("逐条决策结果 JSON", "json", False),
            "cutlist.json": ("切分清单 JSON", "json", False),
            "speaker_link.json": ("说话人归并 JSON", "json", False),
            "rev_speaker_link.json": ("复核说话人 JSON", "json", False),
            "fine_revision.json": ("精剪修订 JSON", "json", False),
            # 流程 / 元数据
            "pipeline.json": ("流程配置 JSON", "json", False),
            "execution_history.json": ("执行历史 JSON", "json", False),
            ".export_job.json": ("当前导出任务 JSON（隐藏）", "json", False),
            # 精剪合成配置（任务根）
            "fine_compose.json": ("精剪合成配置 JSON", "json", False),
        }
        # pattern → label（区间导出 / 精剪预览区间等变长文件名）
        KNOWN_PATTERNS = [
            (re.compile(r"^fine_export_t[\d.]+_d[\d.]+\.mp4$"), "最终导出视频（区间）", "video", True),
            (re.compile(r"^fine_preview_t[\d.]+_d[\d.]+\.mp4$"), "精剪预览（区间）", "video", True),
            (re.compile(r"^subtitle[_.].+\.(srt|ass)$"), "字幕文件", "subtitle", True),
            (re.compile(r"^optimize[_.].+\.(srt|ass)$"), "优化字幕文件", "subtitle", True),
            (re.compile(r"^rough_compose[_.].+\.mp4$"), "粗切成片变体", "video", True),
            (re.compile(r".+\.log$"), "日志文件", "log", False),
        ]

        items: list[dict] = []

        def _classify(fname: str) -> tuple[str, str, bool] | None:
            if fname in KNOWN_FILES:
                return KNOWN_FILES[fname]
            for pat, label, kind, prev in KNOWN_PATTERNS:
                if pat.match(fname):
                    return (label, kind, prev)
            # 兜底：按扩展名粗分
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext in ("mp4", "webm", "mkv", "mov", "avi"):
                return ("视频文件", "video", True)
            if ext in ("srt", "ass"):
                return ("字幕文件", "subtitle", True)
            if ext in ("mp3", "wav", "m4a", "flac"):
                return ("音频文件", "audio", True)
            if ext == "json":
                return ("JSON 元数据", "json", False)
            if ext in ("png", "jpg", "jpeg", "webp"):
                return ("图片文件", "image", True)
            return ("其他文件", "other", False)

        def _collect(root: Path, scope_label: str) -> None:
            if not root.exists() or not root.is_dir():
                return
            try:
                entries = sorted(
                    root.iterdir(),
                    key=lambda p: (not p.is_file(), p.name.lower()),
                )
            except OSError:
                return
            for p in entries:
                if not p.is_file():
                    continue
                # 跳过常见的隐藏 / 临时文件，但 .export_job.json 在 KNOWN_FILES 里
                if p.name.startswith(".") and p.name not in KNOWN_FILES:
                    continue
                cls = _classify(p.name)
                if cls is None:
                    continue
                label, kind, previewable = cls
                try:
                    st = p.stat()
                    size = st.st_size
                    mtime = st.st_mtime
                except OSError:
                    continue
                # 构造预览/下载 URL → /slirn/api/output_file?task_id=...&name=...&root=...
                # （区间导出 fine_export_t*_d*.mp4 用，详见同 app.py list_outputs 注释）
                url = f"/slirn/api/output_file?task_id={tid}&name={urllib.parse.quote(p.name)}&root={scope_label}"
                items.append({
                    "name": p.name,
                    "label": label,
                    "kind": kind,
                    "previewable": previewable,
                    "size": size,
                    "mtime": mtime,
                    "url": url,
                    "scope": scope_label,
                })

        import urllib.parse
        _collect(outputs_dir, "outputs")
        _collect(task_root, "task_root")
        # 按 mtime 倒序（新 → 旧），同名按 scope 优先 outputs
        items.sort(key=lambda x: (-x["mtime"], x["scope"] != "outputs", x["name"]))
        return _ok({"items": items, "task_id": tid})

    @app.app.get("/slirn/api/output_file")
    async def serve_output_file(
        task_id: str, name: str, root: str = "outputs",
    ):
        """REQ-20260921-NNN-outputs-browser：通用 outputs/ 文件下载/预览端点。

        - root=outputs → tasks/<tid>/outputs/<name>
        - root=task_root → tasks/<tid>/<name>（如 fine_compose.json）

        路径遍历保护：name 不允许 / \\ .. 字符；name 必须是文件名（不含路径分隔符）。
        """
        from fastapi import HTTPException as _HTTP
        from fastapi.responses import FileResponse as _FR

        tid = (task_id or "").strip()
        fname = (name or "").strip()
        if not tid or not fname:
            raise _HTTP(400, "缺少 task_id 或 name")
        # 路径遍历保护
        if "/" in fname or "\\" in fname or ".." in fname:
            raise _HTTP(400, f"非法文件名: {fname}")
        # 任务存在性
        try:
            mgr.get(tid)
        except Exception:
            raise _HTTP(404, f"任务不存在: {tid}")
        # 根路径白名单
        if root == "outputs":
            base = mgr.tasks_dir / tid / "outputs"
        elif root == "task_root":
            base = mgr.tasks_dir / tid
        else:
            raise _HTTP(400, f"非法 root: {root}")
        abs_path = base / fname
        # 二次校验（防止 base 之外的 symlink 之类）
        try:
            abs_resolved = abs_path.resolve()
            base_resolved = base.resolve()
            if not str(abs_resolved).startswith(str(base_resolved) + ("\\" if os.name == "nt" else "/")):
                raise _HTTP(400, "路径越界")
        except (OSError, RuntimeError):
            raise _HTTP(404, f"文件不存在或不可访问")
        if not abs_resolved.exists() or not abs_resolved.is_file():
            raise _HTTP(404, f"文件不存在: {fname}")
        # MIME
        ext = abs_resolved.suffix.lower()
        _OUT_MT = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
            ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
            ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
            ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
            ".flac": "audio/flac",
            ".srt": "text/plain; charset=utf-8",
            ".ass": "text/plain; charset=utf-8",
            ".json": "application/json",
            ".log": "text/plain; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
        }
        media_type = _OUT_MT.get(ext, "application/octet-stream")
        return _FR(
            abs_resolved, media_type=media_type,
            headers={"Cache-Control": "no-cache"},
        )

    # ---------- 字幕生成（REQ-20260915-001）----------

    from slirn_home import asr_service as _asr

    # 任务视频流式播放：支持 Range（拖动进度条必需），不经 Gradio 文件白名单
    # （任务视频在 slirn-standalone/tasks/ 下，/gradio_api/file= 只服务 %TEMP%/gradio/）
    # 同样挂原始 ASGI handler，绕开 Gradio 6 rebuild 后的 FastAPI 参数注入 bug
    _VIDEO_MIME = {
        ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
        ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".ts": "video/mp2t",
        ".mpeg": "video/mpeg",
    }

    async def _task_video_asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        from urllib.parse import parse_qs as _parse_qs
        from urllib.parse import unquote as _unquote

        tid = _unquote(scope["path"].rsplit("/", 1)[-1])
        # ?src=original → 完整原视频（编辑页滑全片定位用，REQ-20260915-003）
        # ?src=rough_compose → 粗剪成片（可选阶段产物，REQ-20260916-016）
        # ?src=optimize_compose → 优化成片（REQ-20260923-NNN 查看最终视频）
        # ?src=fine_preview → 精剪视频前 10 秒预览（Phase B，REQ-20260919-061）
        # ?src=fine_export → 精剪视频完整导出（Phase B，REQ-20260919-061）
        src_q = _parse_qs(scope.get("query_string", b"").decode("latin-1")).get("src", [""])[0]
        video: Path | None = None
        try:
            t = mgr.get(tid)
            if src_q == "original" and t.original_video_source.exists():
                video = t.original_video_source
            elif src_q in ("rough_compose", "optimize_compose"):
                from slirn_home import compose_service as _comp_mod

                if src_q == "rough_compose":
                    rc = _comp_mod.rough_compose_path(mgr.tasks_dir / tid / "outputs")
                else:
                    rc = _comp_mod.optimize_compose_path(mgr.tasks_dir / tid / "outputs")
                video = rc if rc.exists() else None
            elif src_q in ("fine_preview", "fine_export"):
                # REQ-20260921-NNN-outputs-browser：支持 ?fname= 显式文件名（区间导出
                # fine_export_t{start}_d{dur}.mp4 用）。fallback 到默认 fine_*.mp4。
                qs2 = _parse_qs(scope.get("query_string", b"").decode("latin-1"))
                fname_q = (qs2.get("fname", [""])[0] or "").strip()
                if fname_q:
                    # 防 path traversal：拒绝 / \ .. 字符
                    if "/" in fname_q or "\\" in fname_q or ".." in fname_q:
                        video = None
                    else:
                        p = mgr.tasks_dir / tid / "outputs" / fname_q
                        video = p if (p.exists() and p.is_file()) else None
                else:
                    p = mgr.tasks_dir / tid / "outputs" / f"{src_q}.mp4"
                    video = p if p.exists() else None
            else:
                video, _label = _resolve_task_video(t)
        except Exception:  # noqa: BLE001
            video = None
        if video is None:
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"ok": false, "error": "task video missing"}'})
            return

        size = video.stat().st_size
        mime = _VIDEO_MIME.get(video.suffix.lower(), "application/octet-stream")
        range_hdr = ""
        for k, v in scope.get("headers", []):
            if k == b"range":
                range_hdr = v.decode("latin-1", errors="replace")
                break

        start, end, status = 0, size - 1, 200
        headers = [
            (b"content-type", mime.encode()),
            (b"accept-ranges", b"bytes"),
            (b"cache-control", b"no-cache"),
        ]
        if range_hdr:
            m2 = _re.search(r"bytes=(\d*)-(\d*)", range_hdr)
            if m2:
                s0, e0 = m2.group(1), m2.group(2)
                start = int(s0) if s0 else 0
                end = min(int(e0), size - 1) if e0 else size - 1
                if start > end or start >= size:
                    await send({"type": "http.response.start", "status": 416,
                                "headers": [(b"content-range", f"bytes */{size}".encode())]})
                    await send({"type": "http.response.body", "body": b""})
                    return
                status = 206
                headers.append((b"content-range", f"bytes {start}-{end}/{size}".encode()))
        headers.append((b"content-length", str(end - start + 1).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})

        # 本地文件按块直读（每块 ≤1MB，SSD 上亚毫秒级，不引入线程池复杂度）
        remaining = end - start + 1
        with open(video, "rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                await send({"type": "http.response.body", "body": chunk,
                            "more_body": remaining > 0})
        if remaining > 0:  # 文件被并发缩短等异常 — 提前收尾
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    class _TaskVideoASGIApp:
        async def __call__(self, scope, receive, send):
            await _task_video_asgi(scope, receive, send)

    app.app.router.add_route(
        "/slirn/api/video/{task_id:path}", _TaskVideoASGIApp(), methods=["GET"],
    )

    @app.app.post("/slirn/api/gen_subtitle")
    async def gen_subtitle(request: Request, body: dict = Body(default_factory=dict)):
        # REQ-20260919-075：auto 透传 — pipeline_service 调时会带 X-Slirn-Auto header
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        # REQ-20260920-081：session 透传 — 同一次自动流的多次操作共用 session_id
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        _ensure_segment_file(t)  # 自愈：截取文件丢失时从原视频重截
        video, _label = _resolve_task_video(t)
        if video is None:
            return _err("任务视频文件缺失（截取段和原视频都不在磁盘上）")
        if not _asr.has_audio_track(video):
            return _err("视频没有音频轨，无法生成字幕")

        hotwords: list[str] = []
        try:
            if t.hotwords_path.exists():
                hotwords = [w for w in t.hotwords_path.read_text(encoding="utf-8").split() if w]
        except Exception:  # noqa: BLE001
            pass

        source, base_off_ms = "original", 0
        if t.segment and t.segment.path.exists():
            source = "segment"
            from tasklib.time_utils import parse_time
            base_off_ms = int(parse_time(t.segment.start).total_seconds() * 1000)

        outputs_dir = mgr.tasks_dir / tid / "outputs"

        def _on_success(segs: list[dict]) -> None:
            mgr.update_status(tid, TaskStatus.SUBTITLE_GENERATED)

        # 说话人分离开关（REQ-20260917-029）：默认开；旧客户端不带该字段 → 开
        sd_val = body.get("sd")
        sd_on = True if sd_val is None else bool(sd_val)
        started = _asr.start_job(
            tid, video, hotwords, outputs_dir,
            source=source, base_offset_ms=base_off_ms, sd=sd_on, on_success=_on_success,
            auto=_auto,
            auto_session_id=_auto_session_id,
        )
        if not started:
            return _ok("", toast="⏳ 该任务已在生成中，请等待完成")
        return _ok("", toast="🎙 字幕生成已开始（后台运行，可离开本页）",
                   job={"state": "running", "stage": "加载模型"})

    @app.app.post("/slirn/api/subtitle_status")
    async def subtitle_status(body: dict = Body(default_factory=dict)):
        import time as _time

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        j = _asr.job_status(tid)
        if j is None:
            # 无内存 job：要么从未生成，要么服务器重启前已完成（结果在磁盘上）
            meta = _asr.load_subtitle(mgr.tasks_dir / tid / "outputs")
            if meta and meta.get("segments"):
                return _ok("", job={"state": "done", "segments_count": len(meta["segments"])})
            return _ok("", job={"state": "idle"})
        j = dict(j)
        j["elapsed_s"] = int((j.get("finished_at") or _time.time()) - j["started_at"])
        return _ok("", job=j)

    @app.app.get("/slirn/api/llm_config")
    async def llm_config_get():
        """模型注册表 + 当前模型 + 各条目 Key 状态（REQ-20260915-008 弹窗初始化）。"""
        from slirn_home import llm_config

        return _ok(
            "",
            models=llm_config.list_models(repo_root),
            current=llm_config.get_current(repo_root),
        )

    @app.app.post("/slirn/api/llm_usage")
    async def llm_usage_get(body: dict = Body(default_factory=dict)):
        """Token 用量账本（输入/输出分开）。body.task_id 传入 → 只看该任务。"""
        from slirn_home import llm_usage

        tid = str(body.get("task_id") or "").strip()
        return _ok("", usage=llm_usage.summary(tid or None))

    @app.app.post("/slirn/api/llm_usage_log")
    async def llm_usage_log(body: dict = Body(default_factory=dict)):
        """逐笔调用明细（时间/模型/任务/阶段/输入/输出/耗时），倒序分页。

        过滤参数：task_id / model / stage / time_from / time_to（时间段）、
        dur_min_ms / dur_max_ms（耗时区间）；非法数值按"未填"处理。
        """
        from slirn_home import llm_usage

        def _num(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        try:
            limit = max(1, min(1000, int(body.get("limit") or 200)))
            offset = max(0, int(body.get("offset") or 0))
        except (TypeError, ValueError):
            limit, offset = 200, 0
        return _ok("", **llm_usage.list_records(
            str(body.get("task_id") or "").strip() or None,
            model=str(body.get("model") or "").strip(),
            stage=str(body.get("stage") or "").strip(),
            time_from=str(body.get("time_from") or "").strip(),
            time_to=str(body.get("time_to") or "").strip(),
            dur_min_ms=_num(body.get("dur_min_ms")),
            dur_max_ms=_num(body.get("dur_max_ms")),
            limit=limit, offset=offset))

    @app.app.get("/slirn/api/settings")
    async def settings_get():
        """系统设置（config/settings.json，机器本地）+ 上下文探测缓存。"""
        from slirn_home import settings_service

        return _ok("", settings=settings_service.get_settings(repo_root),
                   workers_range=[settings_service.MIN_WORKERS,
                                  settings_service.MAX_WORKERS],
                   context_cache=settings_service._read_raw(repo_root)
                   .get("context_cache") or {})

    @app.app.post("/slirn/api/settings/update")
    async def settings_update(body: dict = Body(default_factory=dict)):
        """更新系统设置。llm_workers：大模型并发 worker 数（1-8，下次调用生效）。"""
        from slirn_home import settings_service

        try:
            cur = settings_service.update_settings(repo_root, body)
        except ValueError as e:
            return _err(str(e))
        return _ok("", settings=cur, toast="✅ 设置已保存（并发数在下一次调用时生效）")

    @app.app.post("/slirn/api/llm_probe")
    async def llm_probe(body: dict = Body(default_factory=dict)):
        """并发能力探测：对当前模型阶梯发最小请求，给出建议并发数。

        耗时约 10-30s（取决于模型响应速度），消耗约 20 个微小请求。
        """
        from slirn_home import llm_config, llm_probe as probe_mod

        entry = llm_config.get_current_entry(repo_root)
        if entry is None:
            return _err("未注册任何大模型 — 请先点顶栏「大模型设置」 添加模型")
        result = probe_mod.probe_concurrency(entry)
        return _ok("", **result, model=entry.get("id", ""))

    @app.app.post("/slirn/api/llm_probe_ctx")
    async def llm_probe_ctx(body: dict = Body(default_factory=dict)):
        """上下文窗口探测：元数据接口 → 超长拒答解析 → 阶梯实测。

        结果写入 settings context_cache，字幕修订/优化/精剪的 pack_batches
        随即按「探测窗口 - 输出预留 - 余量」整行打包。被拒请求不计费，
        首个成功请求按实际 tokens 计（通常不超过一次普通调用）。
        """
        from slirn_home import llm_config, llm_probe as probe_mod

        entry = llm_config.get_current_entry(repo_root)
        if entry is None:
            return _err("未注册任何大模型 — 请先点顶栏「大模型设置」 添加模型")
        result = probe_mod.probe_context(entry, repo_root)
        return _ok("", **result, model=entry.get("id", ""))

    @app.app.post("/slirn/api/llm_config/add")
    async def llm_config_add(body: dict = Body(default_factory=dict)):
        """添加模型注册项：{id, provider, base_url, api_key_env, protocol?, vision?}。

        vision（REQ-20260919-061 用户补充）：是否支持图片输入，由用户在 UI 显式勾选。
        """
        from slirn_home import llm_config

        try:
            llm_config.add_model(
                repo_root,
                body.get("id", ""), body.get("provider", ""),
                body.get("base_url", ""), body.get("api_key_env", ""),
                body.get("protocol", "openai"),
                bool(body.get("vision", False)),
            )
        except llm_config.LLMConfigError as e:
            return _err(str(e))
        return _ok("", models=llm_config.list_models(repo_root),
                   current=llm_config.get_current(repo_root),
                   toast=f"✅ 已添加模型 {body.get('id', '')}")

    @app.app.post("/slirn/api/llm_config/update")
    async def llm_config_update(body: dict = Body(default_factory=dict)):
        """修改模型注册项：{id, new_id, provider, base_url, api_key_env, protocol?, vision?}。"""
        from slirn_home import llm_config

        try:
            llm_config.update_model(
                repo_root,
                str(body.get("id") or ""), body.get("new_id", ""),
                body.get("provider", ""), body.get("base_url", ""),
                body.get("api_key_env", ""), body.get("protocol", "openai"),
                bool(body.get("vision", False)),
            )
        except llm_config.LLMConfigError as e:
            return _err(str(e))
        new_id = str(body.get("new_id") or "").strip()
        return _ok("", models=llm_config.list_models(repo_root),
                   current=llm_config.get_current(repo_root),
                   toast=f"✏️ 已更新模型 {new_id}")

    @app.app.post("/slirn/api/llm_config/remove")
    async def llm_config_remove(body: dict = Body(default_factory=dict)):
        """删除模型注册项；删的是当前模型 → 自动切到剩余第一个。"""
        from slirn_home import llm_config

        try:
            llm_config.remove_model(repo_root, str(body.get("id") or ""))
        except llm_config.LLMConfigError as e:
            return _err(str(e))
        return _ok("", models=llm_config.list_models(repo_root),
                   current=llm_config.get_current(repo_root),
                   toast=f"🗑 已删除模型 {body.get('id', '')}")

    @app.app.post("/slirn/api/llm_config/use")
    async def llm_config_use(body: dict = Body(default_factory=dict)):
        """设置当前使用的模型。"""
        from slirn_home import llm_config

        try:
            cur = llm_config.use_model(repo_root, str(body.get("id") or ""))
        except llm_config.LLMConfigError as e:
            return _err(str(e))
        return _ok("", models=llm_config.list_models(repo_root),
                   current=cur, toast=f"⚙️ 当前模型 → {cur}")

    @app.app.post("/slirn/api/llm_test")
    async def llm_test(body: dict = Body(default_factory=dict)):
        """连通性测试：对已注册模型真实调用一次，返回可用性/耗时/可读错误。"""
        import time as _time

        from slirn_home import llm_config, revision_service

        mid = str(body.get("id") or "").strip()
        entry = next((m for m in llm_config.list_models(repo_root)
                      if m.get("id") == mid and "key_present" in m), None)
        if entry is None:
            return _err(f"模型 {mid} 未注册")
        t0 = _time.time()
        try:
            reply = revision_service._call_llm(
                "你是连通性测试助手。", "请只回复两个字：正常", entry=entry, retries=0,
                usage_ctx={"stage": "连通性测试"},
            )
        except Exception as e:  # noqa: BLE001 — 错误信息已是 REQ-007 可读格式
            return _err(str(e))
        return _ok("", model=entry["id"], elapsed_s=round(_time.time() - t0, 2),
                   reply=(reply or "").strip()[:50])

    @app.app.post("/slirn/api/revise_subtitle")
    async def revise_subtitle(request: Request, body: dict = Body(default_factory=dict)):
        """启动大模型字幕分析（REQ-20260915-005）。已有建议时须 force（前端二次确认）。

        rigor 必填（REQ-20260916-003）：high|medium|low|custom（007 自定义档），
        注入提示词并留痕；custom 时 custom_prompt 为用户修改后的提示词
        （空白回退默认底稿），实际使用全文写入 revision.json meta。

        X-Slirn-Auto / X-Slirn-Auto-Session 头透传 start_job：手动点击
        （不带 header）→ execution_history 记 manual；自动流 → pipeline 自记。
        """
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        from slirn_home import llm_config, revision_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        rigor = str(body.get("rigor") or "").strip().lower()
        custom_prompt = str(body.get("custom_prompt") or "")
        if (rigor != revision_service.CUSTOM_RIGOR_KEY
                and rigor not in revision_service.RIGOR_LEVELS):
            return _err("请先选择分析严谨性级别（高 / 中 / 低 / 自定义）")
        try:
            t = mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        sub_meta = _asr.load_subtitle(outputs_dir)
        if not (sub_meta and sub_meta.get("segments")):
            return _err("请先生成字幕（上一阶段）再进行修订分析")
        if revision_service.load_revision(outputs_dir) and not body.get("force"):
            return _err("已存在修订建议 — 重新分析将覆盖建议并重置全部决策，请确认后重试")

        hotwords: list[str] = []
        try:
            if t.hotwords_path.exists():
                hotwords = [w for w in t.hotwords_path.read_text(encoding="utf-8").split() if w]
        except Exception:  # noqa: BLE001
            pass

        entry = llm_config.get_current_entry(repo_root)
        if entry is None:
            return _err("未注册任何大模型 — 请先点顶栏「大模型设置」 添加模型")
        started = revision_service.start_job(
            tid, sub_meta["segments"], t.name, hotwords, outputs_dir,
            entry=entry, rigor=rigor, custom_prompt=custom_prompt,
            auto=_auto, auto_session_id=_auto_session_id,
        )
        if not started:
            return _ok("", toast="⏳ 该任务已在分析中，请等待完成")
        return _ok("", toast="🤖 大模型分析已开始（后台运行，可离开本页）",
                   job={"state": "running", "stage": "准备提示词"})

    @app.app.post("/slirn/api/revise_status")
    async def revise_status(body: dict = Body(default_factory=dict)):
        import time as _time

        from slirn_home import revision_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        j = revision_service.job_status(tid)
        if j is None:
            rev = revision_service.load_revision(mgr.tasks_dir / tid / "outputs")
            if rev and rev.get("entries"):
                return _ok("", job={"state": "done", "entries_count": len(rev["entries"])})
            return _ok("", job={"state": "idle"})
        j = dict(j)
        j["elapsed_s"] = int((j.get("finished_at") or _time.time()) - j["started_at"])
        return _ok("", job=j)

    @app.app.post("/slirn/api/list_logs")
    async def list_logs(body: dict = Body(default_factory=dict)):
        """REQ-20260920-081 + REQ-20260920-086：执行日志分页查询。

        输入 body：
            task_id: 必填；查询该任务的 execution_history.json
            time_from / time_to: 可选 ISO 8601；按 started_at 区间过滤
            kinds: 可选操作类型列表（如 ["rough_compose", "rough_compose_delete"]）
            statuses: 可选状态列表（如 ["success", "failed"]）
            auto: 可选 "manual" | "auto" | "any"（默认 "any"）
            keyword: 可选关键词（搜 error/kind/auto_session_id/description）
            limit / page_size: 可选每页条数（默认 20，上限 1000）
            offset / page: 可选分页偏移（默认 0；page 从 1 起）
        返回：
            {ok: true, items: [...], total: N, page: P, page_size: S, total_pages: T}
        """
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")

        from slirn_home import execution_history

        # ISO 8601 → epoch 秒（带或不带时区都容错）
        def _parse_iso(s: str | None) -> float | None:
            if not s:
                return None
            try:
                from datetime import datetime as _dt
                s = str(s).strip()
                # 兼容带 Z 结尾（UTC）
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                return _dt.fromisoformat(s).timestamp()
            except Exception:
                return None

        time_from_ts = _parse_iso(body.get("time_from"))
        time_to_ts = _parse_iso(body.get("time_to"))
        kinds_in = body.get("kinds") or []
        statuses_in = body.get("statuses") or []
        auto_mode = (body.get("auto") or "any").lower()
        keyword = body.get("keyword") or ""
        # REQ-20261028-NNN：长/短视频执行日志分流 — "long" 只留长视频体系记录，
        # "short" 只留短视频体系记录；非法值按 "any"（不过滤，兼容旧调用）。
        flow_mode = (body.get("flow") or "any").lower()
        if flow_mode not in ("long", "short", "any"):
            flow_mode = "any"

        # REQ-20260920-086：分页参数（page/offset 二选一，都缺省则 offset=0）
        try:
            limit = int(body.get("limit") or body.get("page_size") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 1000))

        if "offset" in body and body.get("offset") not in (None, ""):
            try:
                offset = int(body.get("offset"))
            except (TypeError, ValueError):
                offset = 0
        elif "page" in body and body.get("page") not in (None, ""):
            try:
                page_in = int(body.get("page"))
                offset = (max(1, page_in) - 1) * limit
            except (TypeError, ValueError):
                offset = 0
        else:
            offset = 0
        offset = max(0, offset)

        # task_id 隔离：通过 outputs_dir（每个 task 独立目录）天然隔离
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        items, total = execution_history.query_history_paged(
            outputs_dir,
            kinds=list(kinds_in) if kinds_in else None,
            statuses=list(statuses_in) if statuses_in else None,
            keyword=keyword,
            limit=limit,
            offset=offset,
            time_from_ts=time_from_ts,
            time_to_ts=time_to_ts,
            auto=auto_mode,
            flow=flow_mode,
        )
        page = offset // max(1, limit) + 1
        total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 0
        return _ok(
            "",
            items=items,
            total=total,
            page=page,
            page_size=limit,
            total_pages=total_pages,
        )

    @app.app.post("/slirn/api/save_revision")
    async def save_revision(body: dict = Body(default_factory=dict)):
        """保存用户逐条决策；全部决策完成 → 状态推进 SUBTITLE_REVIEWED。"""
        import json
        import time as _time

        from slirn_home import revision_service

        tid = (body.get("task_id") or "").strip()
        decisions = body.get("decisions")
        if not tid:
            return _err("缺少 task_id")
        if not isinstance(decisions, list):
            return _err("缺少 decisions 列表")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        rev = revision_service.load_revision(outputs_dir)
        if not (rev and rev.get("entries")):
            return _err("尚无修订建议，请先运行大模型分析")
        rev, applied = revision_service.merge_decisions(rev, decisions)
        rev["saved_at"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
        (outputs_dir / revision_service.REVISION_JSON).write_text(
            json.dumps(rev, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        total = len(rev["entries"])
        decided = sum(1 for e in rev["entries"] if e.get("decision") != "pending")
        finished = revision_service.all_decided(rev)
        if finished:
            try:
                mgr.update_status(tid, TaskStatus.SUBTITLE_REVIEWED)
            except Exception as e:  # noqa: BLE001
                revision_service.log.warning("更新任务 %s 状态失败: %s", tid, e)
        remaining = total - decided
        toast = f"✅ 已保存（生效 {applied} 条 · 已决策 {decided}/{total}）"
        if finished:
            toast += " · 字幕修订完成，可进入切分修剪"
        elif remaining:
            toast += f" · 还剩 {remaining} 条未决策"
        return _ok("", toast=toast, decided=decided, total=total, finished=finished)

    @app.app.post("/slirn/api/build_cutlist")
    async def build_cutlist(request: Request, body: dict = Body(default_factory=dict)):
        """生成切分修剪清单（REQ-20260916-008）：决策落盘 cutlist.json + 推进 ROUGH_CUT_DONE。"""
        import time as _time

        # REQ-20260919-075：auto 透传；执行切分修剪写日志
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        # REQ-20260920-081：session 透传
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        from slirn_home import asr_service, cutlist_service, execution_history, revision_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        rev = revision_service.load_revision(outputs_dir)
        if not (rev and rev.get("entries")):
            return _err("尚无修订建议，请先在「字幕修订」阶段完成大模型分析")
        if not revision_service.all_decided(rev):
            pending = sum(1 for e in rev["entries"] if e.get("decision") == "pending")
            return _err(f"字幕修订还有 {pending} 条未决策，请先保存全部决策")
        sub_meta = asr_service.load_subtitle(outputs_dir)
        if not (sub_meta and sub_meta.get("segments")):
            return _err("缺少字幕生成产物（subtitle.json），请先在「字幕生成」阶段生成字幕")
        # REQ-20260919-075：开始记录 — 执行切分修剪
        exec_id = execution_history.record_start(
            outputs_dir, execution_history.KIND_ROUGH_CUT,
            auto=_auto,
            auto_session_id=_auto_session_id,
        )
        try:
            cutlist = cutlist_service.build_cutlist(sub_meta, rev)
            cutlist_service.save_cutlist(outputs_dir, cutlist)
        except Exception as e:  # noqa: BLE001
            execution_history.record_finish(outputs_dir, exec_id, success=False, error=str(e))
            return _err(f"生成切分清单失败: {e}")
        stats = cutlist.get("stats", {})
        try:
            mgr.update_status(tid, TaskStatus.ROUGH_CUT_DONE)
        except Exception as e:  # noqa: BLE001
            revision_service.log.warning("更新任务 %s 状态失败: %s", tid, e)
        saved_at = _time.strftime("%Y-%m-%dT%H:%M:%S")
        # REQ-20260919-075：完成时回填具体执行情况
        execution_history.patch_fields(outputs_dir, exec_id, {
            "description": (f"按切分决策生成切分清单：带入 {stats.get('brought', 0)} 段，"
                            f"剔除 {stats.get('dropped', 0)} 条，切分子段 {stats.get('split_subs', 0)}")
        })
        execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
        return _ok("", toast=(
            f"✅ 切分清单已生成（带入 {stats.get('brought', 0)} 段 · 剔除 {stats.get('dropped', 0)} 条"
            f" · 切分子段 {stats.get('split_subs', 0)}）· 切分修剪完成，可进入精剪字幕"
        ), saved_at=saved_at, stats=stats)

    @app.app.post("/slirn/api/cut_speaker_link")
    async def cut_speaker_link(request: Request, body: dict = Body(default_factory=dict)):
        """切分修剪阶段关联人员ID（REQ-20260917-031）：按时间段重叠对齐 subtitle 段级 spk。

        服务端实时重建切分清单预览（与面板同口径：修订实时 + 已保存手工决策并入），
        返回每行人员编号 + 按人员统计（总数/执行口径保留数）。不落盘 — 再次点击
        即按最新时间窗重算；删除改判的落盘走既有 save_cut_decisions。
        """
        # REQ-20260919-075：auto 透传；关联人员ID写日志
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        # REQ-20260920-081：session 透传
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        from slirn_home import asr_service, cut_speaker, cutlist_service, execution_history, revision_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        rev = revision_service.load_revision(outputs_dir)
        if not (rev and rev.get("entries")):
            return _err("尚无修订建议，请先在「字幕修订」阶段完成大模型分析")
        if not revision_service.all_decided(rev):
            pending = sum(1 for e in rev["entries"] if e.get("decision") == "pending")
            return _err(f"字幕修订还有 {pending} 条未决策，请先保存全部决策")
        sub_meta = asr_service.load_subtitle(outputs_dir)
        if not (sub_meta and sub_meta.get("segments")):
            return _err("缺少字幕生成产物（subtitle.json），请先在「字幕生成」阶段生成字幕")
        saved = cutlist_service.load_cutlist(outputs_dir)
        cutlist = cutlist_service.build_cutlist(
            sub_meta, rev,
            manual_marks=(saved or {}).get("manual_marks") if saved else None,
            actions=(saved or {}).get("actions") if saved else None,
        )
        # REQ-20260919-075：开始记录 — 关联人员ID
        exec_id = execution_history.record_start(
            outputs_dir, execution_history.KIND_ROUGH_CUT_LINK_PERSON,
            extra={"rows": len(cutlist.get("items") or [])},
            auto=_auto,
            auto_session_id=_auto_session_id,
        )
        try:
            link = cut_speaker.link_speakers(sub_meta, cutlist)
            if not link.get("available"):
                execution_history.patch_fields(outputs_dir, exec_id, {
                    "description": "字幕无人员编号（生成字幕时未开启说话人分离），跳过关联"
                })
                execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
                return _err(
                    "字幕无人员编号 — 该任务生成字幕时未开启说话人分离（或为旧任务）。"
                    "请到「字幕生成」阶段开启「区分说话人」重新生成后再关联"
                )
            # REQ-033：关联状态落盘 — 重进面板时徽章 + 统计条直接渲染（渲染端现算，快照备查）
            saved_link = cut_speaker.save_link(outputs_dir, link)
        except Exception as e:  # noqa: BLE001
            execution_history.record_finish(outputs_dir, exec_id, success=False, error=str(e))
            return _err(f"关联失败: {e}")
        # REQ-20260919-075：完成时回填具体执行情况
        # cut_speaker.link_speakers 返回 stats: [{spk, count}, ...] — 取 spk 集合的大小
        stats_list = link.get("stats") or []
        speakers_count = len({s.get("spk") for s in stats_list if s.get("spk") is not None})
        execution_history.patch_fields(outputs_dir, exec_id, {
            "description": (f"切分 {len(cutlist.get('items') or [])} 段关联到人员ID，"
                            f"识别 {speakers_count} 位说话人")
        })
        execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
        return _ok("", link=link, rows=len(cutlist.get("items") or []),
                   linked_at=saved_link.get("linked_at"))

    # REQ-20260919-068：字幕修订阶段关联人员 ID（与切分修剪同口径）。
    # 修订阶段不强制 all_decided（用户可边决策边关联预览；落盘走 save_revision 路径）。
    @app.app.post("/slirn/api/rev_speaker_link")
    async def rev_speaker_link(body: dict = Body(default_factory=dict)):
        """字幕修订阶段关联人员ID（REQ-20260919-068）：按时间段重叠对齐 subtitle 段级 spk。

        返回每行人员编号 + 按人员统计（未删除决策的行数）。落盘
        rev_speaker_link.json — 重进面板时徽章 + 统计条直接渲染（渲染端现算，
        快照仅备查）。修订变化无需重算链接。
        """
        from slirn_home import asr_service, rev_speaker, revision_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        rev = revision_service.load_revision(outputs_dir)
        if not (rev and rev.get("entries")):
            return _err("尚无修订建议，请先在「字幕修订」阶段完成大模型分析")
        sub_meta = asr_service.load_subtitle(outputs_dir)
        if not (sub_meta and sub_meta.get("segments")):
            return _err("缺少字幕生成产物（subtitle.json），请先在「字幕生成」阶段生成字幕")
        link = rev_speaker.link_speakers(sub_meta, rev)
        if not link.get("available"):
            return _err(
                "字幕无人员编号 — 该任务生成字幕时未开启说话人分离（或为旧任务）。"
                "请到「字幕生成」阶段开启「区分说话人」重新生成后再关联"
            )
        saved_link = rev_speaker.save_link(outputs_dir, link)
        return _ok("", link=link, rows=len(rev.get("entries") or []),
                   linked_at=saved_link.get("linked_at"))

    @app.app.post("/slirn/api/save_cut_decisions")
    async def save_cut_decisions(body: dict = Body(default_factory=dict)):
        """保存切分决策（REQ-20260916-011）：子段翻转 manual_marks + 字幕级改判 actions。

        重算并入显示后落盘 cutlist.json（阶段随磁盘判定/幂等推进不变）。
        改判 split 携带的切分后内容（split_targets）回写修订 user_note（S→填内容
        →直接保存的路径闭环）；校验改判 split 必须有切分后内容，残缺决策不落盘。
        """
        import json
        import time as _time

        from slirn_home import asr_service, cutlist_service, revision_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        rev = revision_service.load_revision(outputs_dir)
        if not (rev and rev.get("entries")):
            return _err("尚无修订建议，请先在「字幕修订」阶段完成大模型分析")
        if not revision_service.all_decided(rev):
            pending = sum(1 for e in rev["entries"] if e.get("decision") == "pending")
            return _err(f"字幕修订还有 {pending} 条未决策，请先保存全部决策")
        sub_meta = asr_service.load_subtitle(outputs_dir)
        if not (sub_meta and sub_meta.get("segments")):
            return _err("缺少字幕生成产物（subtitle.json），请先在「字幕生成」阶段生成字幕")

        manual_marks = body.get("manual_marks") or {}
        actions = body.get("actions") or {}
        split_targets = body.get("split_targets") or {}
        if (not isinstance(manual_marks, dict) or not isinstance(actions, dict)
                or not isinstance(split_targets, dict)):
            return _err("manual_marks / actions / split_targets 格式不正确")
        # 改判 split 附带的切分后内容 → 回写修订（decision=split + user_note，
        # 单一事实源；与「✂️ 重新切分」同一落点）
        rev_changed = False
        for k, tv in split_targets.items():
            if str(actions.get(k)) != "split":
                continue
            tv = str(tv or "").strip()
            if not tv:
                continue
            entry = next((e for e in rev["entries"] if str(e.get("i")) == str(k)), None)
            if entry is not None and cutlist_service._split_target(entry) != tv:
                entry["decision"] = "split"
                entry["user_note"] = tv[:500]
                rev_changed = True
        # 改判 split 必须有切分后内容（本次携带 / 修订 user_note / 模型建议），
        # 否则执行口径残缺
        for k, v in actions.items():
            if str(v) not in cutlist_service.ACTIONS:
                return _err(f"未知的决策状态「{v}」（第 {k} 条）")
            if str(v) == "split":
                entry = next((e for e in rev["entries"] if str(e.get("i")) == str(k)), None)
                if entry is None or not cutlist_service._split_target(entry):
                    return _err(f"第 {k} 条已改判切分，但未填写切分后内容 — 请填写后再保存")
        try:
            cutlist = cutlist_service.build_cutlist(sub_meta, rev,
                                                    manual_marks=manual_marks, actions=actions)
        except Exception as e:  # noqa: BLE001
            return _err(f"保存切分决策失败: {e}")
        if rev_changed:  # 修订先落盘、清单后落盘（saved_at 单调，不误报过期黄条）
            rev["saved_at"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
            (outputs_dir / revision_service.REVISION_JSON).write_text(
                json.dumps(rev, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        cutlist_service.save_cutlist(outputs_dir, cutlist)
        try:
            mgr.update_status(tid, TaskStatus.ROUGH_CUT_DONE)  # 幂等：清单在盘即完成
        except Exception as e:  # noqa: BLE001
            revision_service.log.warning("更新任务 %s 状态失败: %s", tid, e)
        stats = cutlist.get("stats", {})
        parts = ["✅ 切分决策已保存"]
        if stats.get("mark_flipped"):
            parts.append(f"手工翻转 {stats['mark_flipped']} 段")
        if stats.get("action_changed"):
            parts.append(f"字幕级改判 {stats['action_changed']} 条")
        return _ok("", toast=" · ".join(parts), stats=stats)

    @app.app.post("/slirn/api/compose_rough")
    async def compose_rough(request: Request, body: dict = Body(default_factory=dict)):
        """启动粗剪合成（REQ-20260916-018，必做阶段）：上游 VideoClipper 方法合成。

        口径与切分修剪面板一致（修订实时 + 已保存手工翻转/改判）；行文本取
        最新确认版（热词替换未撤销的行用 new_text，REQ-20260916-017）—
        「处理之后的字幕 + 原视频」交给上游合成方法（video_clip）。
        不推进任务状态。已在跑 → 返回 running 供前端接续轮询。

        REQ-20260920-081：auto_session_id 透传给 compose_service.start_compose。
        """
        # REQ-20260919-075：auto 透传
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        # REQ-20260920-081：session 透传
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        from slirn_home import asr_service, compose_service, cutlist_service, fine_service, revision_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        rev = revision_service.load_revision(outputs_dir)
        if not (rev and rev.get("entries")):
            return _err("请先完成「字幕修订」")
        if not revision_service.all_decided(rev):
            return _err("字幕修订还有未决策条目，无法确定保留区间")
        sub_meta = asr_service.load_subtitle(outputs_dir)
        if not (sub_meta and sub_meta.get("segments")):
            return _err("缺少字幕生成产物，请先生成字幕")
        saved = cutlist_service.load_cutlist(outputs_dir)
        try:
            cutlist = cutlist_service.build_cutlist(
                sub_meta, rev,
                manual_marks=(saved or {}).get("manual_marks") if saved else None,
                actions=(saved or {}).get("actions") if saved else None,
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"计算保留区间失败: {e}")
        units = cutlist_service.effective_keep_units(cutlist)
        if not units:
            return _err("没有保留内容 — 全部被删除时无需合成")
        intervals_ms = [(int(u["start_ms"]), int(u["end_ms"])) for u in units]
        # 行文本取最新确认版：热词替换（REQ-20260916-017）未撤销的行 → new_text
        fine = fine_service.load_fine(outputs_dir)
        fmap = {str(e["id"]): e["new_text"] for e in (fine or {}).get("entries") or []
                if not e.get("reverted")}
        lines = [{"id": str(u["id"]), "start_ms": int(u["start_ms"]), "end_ms": int(u["end_ms"]),
                  "text": fmap.get(str(u["id"]), str(u["text"]))} for u in units]
        video, _label = _resolve_task_video(mgr.get(tid))
        if video is None:
            return _err("任务视频文件缺失，无法合成")
        keep_ms = sum(int(u["end_ms"]) - int(u["start_ms"]) for u in units)
        n_merged = len(compose_service.merge_intervals_ms(intervals_ms))
        started = compose_service.start_compose(
            tid, video, intervals_ms, compose_service.rough_compose_path(outputs_dir), lines,
            auto=_auto, auto_session_id=_auto_session_id)
        job = compose_service.job_status(tid) or {}
        return _ok("", running=bool(started), job=job,
                   toast="🎬 合成已启动" if started else "🎬 合成已在进行中",
                   keep_ms=keep_ms, segments=n_merged, raw_segments=len(intervals_ms))

    @app.app.post("/slirn/api/compose_rough_status")
    async def compose_rough_status(body: dict = Body(default_factory=dict)):
        """粗剪合成进度轮询（REQ-20260916-016）。"""
        from slirn_home import compose_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        job = compose_service.job_status(tid)
        if job is None:  # 服务重启后 job 丢失 — 产物在即视为完成
            artifact = compose_service.rough_compose_path(mgr.tasks_dir / tid / "outputs")
            if artifact.exists():
                return _ok("", job={"state": "done", "progress": 100.0})
            return _err("没有进行中的合成任务")
        return _ok("", job=job)

    @app.app.post("/slirn/api/compose_rough_delete")
    async def compose_rough_delete(request: Request, body: dict = Body(default_factory=dict)):
        """删除粗剪成片（REQ-20260916-019）：用户主动清理产物以便重合成。

        REQ-20260920-081：auto_session_id 透传给 compose_service.delete_rough_compose。
        """
        # REQ-20260919-075：auto 透传
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        # REQ-20260920-081：session 透传
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        from slirn_home import compose_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        res = compose_service.delete_rough_compose(outputs_dir, auto=_auto,
                                                  auto_session_id=_auto_session_id)
        # REQ-20260922-NNN 标记删除行：粗剪成片是优化成片的源 — 源删了，
        # 优化成片三件产物一并清理（精剪自动回退也就无优化成片可用）
        cut_removed = compose_service._delete_cut_artifacts(outputs_dir)
        if cut_removed:
            res["removed"] = list(res["removed"]) + cut_removed
            res["message"] += " + " + " + ".join(cut_removed)
        return _ok("", deleted=res["deleted"],
                   toast="🗑️ " + res["message"] if res["deleted"] else None,
                   removed=res["removed"], remaining=res["remaining"],
                   message=res["message"])

    @app.app.post("/slirn/api/compose_rough_preview_subs")
    async def compose_rough_preview_subs(body: dict = Body(default_factory=dict)):
        """预览「切分之后的有效字幕」（REQ-20260916-019）：SRT 格式字符串。

        行集口径与 compose_rough 一致（修订实时 + 已保存手工翻转/改判），
        行文本并入热词替换未撤销行的 new_text；按 start_ms 升序输出 SRT。
        """
        from slirn_home import asr_service, compose_service, cutlist_service, fine_service, revision_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        rev = revision_service.load_revision(outputs_dir)
        if not (rev and rev.get("entries")):
            return _err("请先完成「字幕修订」")
        if not revision_service.all_decided(rev):
            return _err("字幕修订还有未决策条目")
        sub_meta = asr_service.load_subtitle(outputs_dir)
        if not (sub_meta and sub_meta.get("segments")):
            return _err("缺少字幕生成产物")
        saved = cutlist_service.load_cutlist(outputs_dir)
        try:
            cutlist = cutlist_service.build_cutlist(
                sub_meta, rev,
                manual_marks=(saved or {}).get("manual_marks") if saved else None,
                actions=(saved or {}).get("actions") if saved else None,
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"计算保留行失败: {e}")
        units = cutlist_service.effective_keep_units(cutlist)
        if not units:
            return _err("没有保留内容")
        fine = fine_service.load_fine(outputs_dir)
        fmap = {str(e["id"]): e["new_text"]
                for e in (fine or {}).get("entries") or [] if not e.get("reverted")}
        srt = compose_service.format_srt(units, fmap)
        keep_ms = sum(int(u["end_ms"]) - int(u["start_ms"]) for u in units)
        return _ok("", srt=srt, lines=len(units), keep_ms=keep_ms)

    @app.app.post("/slirn/api/execution_history")
    async def execution_history_endpoint(body: dict = Body(default_factory=dict)):
        """REQ-20260918-048 — 执行历史记录（字幕生成 + 粗剪合成）。

        返回该任务 outputs/execution_history.json 的全部记录（按 started_at 倒序）。
        工作台面板初始化时调用渲染折叠区，无需实时刷新（落盘数据，下次进入自然新）。
        """
        from slirn_home import execution_history

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        items = execution_history.load_history(outputs_dir)
        # 倒序（最新在前）；同秒多条按 id 倒序兜底
        items.sort(key=lambda x: (float(x.get("started_at") or 0), str(x.get("id") or "")), reverse=True)
        return _ok("", items=items)

    @app.app.post("/slirn/api/execution_history_query")
    async def execution_history_query_endpoint(body: dict = Body(default_factory=dict)):
        """REQ-20260918-053 — 执行日志查询（按阶段/状态过滤 + 关键词搜错误信息）。

        返回倒序最多 N 条（默认 200）；过滤条件全 AND：
        - kinds  (可选) list[str]  — 限定阶段（如 ["rough_compose","optimize"]）
        - statuses (可选) list[str] — 限定状态（running/success/failed）
        - keyword (可选) str        — 搜错误信息 / 阶段串（不区分大小写）
        - limit (可选) int          — 默认 200，上限 1000
        """
        from slirn_home import execution_history

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        kinds = body.get("kinds") or None
        statuses = body.get("statuses") or None
        keyword = body.get("keyword") or ""
        try:
            limit = int(body.get("limit") or 200)
        except Exception:  # noqa: BLE001
            limit = 200
        if limit < 1:
            limit = 1
        if limit > 1000:
            limit = 1000
        # 校验 kinds / statuses 是否合法（防止前端传错）
        if kinds is not None:
            kinds = [k for k in kinds if k in execution_history.ALL_KINDS]
        valid_statuses = {"running", "success", "failed"}
        if statuses is not None:
            statuses = [s for s in statuses if s in valid_statuses]
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        items = execution_history.query_history(
            outputs_dir, kinds=kinds, statuses=statuses, keyword=keyword, limit=limit
        )
        return _ok("", items=items, total=len(items),
                   kinds=kinds or sorted(execution_history.ALL_KINDS))

    # ---------- 流程配置 + 自动执行（REQ-20260918-047）----------
    # 工作台顶部「⚙ 流程」按钮 → 抽屉编辑器 → 配置存 tasks/<tid>/outputs/pipeline.json
    # 后台守护线程（pipeline_service）按 STAGE_ORDER 顺序跑，每步调本组原 stage API
    # （/gen_subtitle、/revise_subtitle、/build_cutlist、/compose_rough、/optimize_subtitle）。
    # 任何阶段报错即停，错误信息写 pipeline.json history；手动按钮始终可用。
    from slirn_home import pipeline_service

    @app.app.post("/slirn/api/pipeline_get")
    async def pipeline_get(body: dict = Body(default_factory=dict)):
        """读取任务的 pipeline 配置 + 当前 job 状态 + 历史 summary。

        没写过 pipeline.json → 返回默认配置（AC-1）。
        """
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        data = pipeline_service.load_pipeline(outputs_dir)
        if data is None:
            # 还没写过 → 返回默认
            data = {
                "version": 1,
                "config": pipeline_service.default_config(),
                "updated_at": None,
                "history": [],
            }
        status = pipeline_service.pipeline_status(tid)
        # REQ-20260921-NNN：附加 fc.json 是否存在的标志，让前端可以动态禁用
        # 「当前参数」选项（fc.json 不存在时只能选模板或导入）。
        fc_root = mgr.tasks_dir / tid / "fine_compose.json"
        has_fc_json = bool(fc_root.exists())
        return _ok("", config=data["config"], updated_at=data.get("updated_at"),
                   history=data.get("history") or [], status=status,
                   has_fc_json=has_fc_json)

    @app.app.post("/slirn/api/pipeline_save")
    async def pipeline_save(body: dict = Body(default_factory=dict)):
        """保存 pipeline 配置；缺字段用默认值补全（容错）。"""
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        cfg = body.get("config") or {}
        ts = pipeline_service.save_pipeline(outputs_dir, cfg)
        return _ok("", saved_at=ts, toast="⚙️ 流程配置已保存")

    # ---------- 流程配置·全局模板（REQ-20260930-092：面板全部参数存为可复用模板） ----------
    # 镜像 fine_profiles 的全局 JSON 模式（tasks/_global_flow_profiles.json）。
    # config 与任务 pipeline.json 的 config 同构，含完整 config 一起返回（体量小）。
    from slirn_home import flow_profiles as _flow_profiles

    @app.app.post("/slirn/api/list_flow_profiles")
    async def list_flow_profiles(body: dict = Body(default_factory=dict)):
        """列出所有流程配置模板（含 config，前端套用/回显匹配用）。"""
        profiles = _flow_profiles.list_profiles(repo_root)
        return _ok(profiles=[
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "saved_at": p.get("saved_at"),
                "task_id_origin": p.get("task_id_origin"),
                "config": p.get("config") or {},
            }
            for p in profiles
        ])

    @app.app.post("/slirn/api/save_flow_profile")
    async def save_flow_profile(body: dict = Body(default_factory=dict)):
        """把面板当前整套配置（红框内全部参数）保存为命名模板。

        config 优先取 body.config（面板实时值）；缺失时兜底读任务已保存的
        pipeline.json（自动保存开启后两者一致）。服务端 normalize 后落盘。
        """
        tid = (body.get("task_id") or "").strip()
        name = (body.get("name") or "").strip()
        config = body.get("config") or {}
        if not tid:
            return _err("缺少 task_id")
        if not name:
            return _err("模板名不能为空")
        if len(name) > 30:
            return _err("模板名不能超过 30 字")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        if not config:
            outputs_dir = mgr.tasks_dir / tid / "outputs"
            data = pipeline_service.load_pipeline(outputs_dir)
            config = (data or {}).get("config") or {}
        profile = _flow_profiles.save_profile(repo_root, name, config,
                                              task_id_origin=tid)
        return _ok(
            profile={
                "id": profile["id"],
                "name": profile["name"],
                "saved_at": profile["saved_at"],
                "task_id_origin": profile["task_id_origin"],
                "config": profile.get("config") or {},
            },
            toast=f"✅ 已保存流程模板「{profile['name']}」",
        )

    @app.app.post("/slirn/api/delete_flow_profile")
    async def delete_flow_profile(body: dict = Body(default_factory=dict)):
        """从全局 JSON 移除指定流程模板（不影响已应用的任务）。"""
        profile_id = (body.get("profile_id") or "").strip()
        if not profile_id:
            return _err("缺少 profile_id")
        ok = _flow_profiles.delete_profile(repo_root, profile_id)
        if not ok:
            return _err(f"模板不存在或已删除: {profile_id}")
        return _ok(toast="🗑 已删除流程模板")

    # ---------- 短视频多素材混剪 ----------
    from slirn_home import short_video_render as _sv_render
    from slirn_home import short_video_service as _sv_service
    from slirn_home import short_video_ui as _sv_ui

    def _visible_short_video_tasks(scope: str = "all") -> list[dict]:
        """短视频列表可见的来源任务。

        REQ-20261012-099 修订四：admin 可选 mine/all（默认 all 保持老行为）；
        普通 / 未登录用户一律钳回 mine（列表里的「全部任务」radio 对他们不渲染，
        传了 scope=all 也在此处被钳掉，与长视频列表语义一致）。
        """
        user = _get_cur_user()
        if user and user.get("is_admin") and scope != "mine":
            summaries = mgr.list()
        else:
            username = (user or {}).get("username", "")
            memberships = auth.list_user_tasks(username) if username else []
            summaries = mgr.list_for_user(username, is_admin=False, member_of=memberships)
        return [{"task_id": s.task_id, "name": s.name} for s in summaries]

    def _inject_stage3_highlights(project: dict) -> None:
        """REQ-20261004-UX：从 stage3/highlights.json 注入 highlights 列表到
        ``pipeline.stage3_analyze.highlights``，供 UI 渲染可编辑条目。

        Why: run_stage3 把 highlights 写到独立 JSON 文件（source of truth），
        不复制到 project state。render_project 只看 state 会拿到空列表。

        REQ-20261004-bugfix：注入前对每条 highlight 做一次字幕区间清洗 ——
        剔除 src_index 对应 SRT 行不在 [start_ms, end_ms] 内的项（LLM 会
        hallucinate 不在该 clip 范围内的 src_index，导致 UI 显示的字幕跟视频
        实际播放内容对不上）。

        REQ-20261012-099：全局提示词设置 + 当前截图注入**不依赖 highlights.json
        存在**（未跑过 Stage 3 的项目也要显示默认提示词/截图上传区）。
        """
        pl = project.setdefault("pipeline", {})
        s3 = pl.setdefault("stage3_analyze", {})
        try:
            s3["prompt_settings"] = _sv_service.load_prompt_settings(repo_root)
            cur_img = _sv_service.stage3_current_image(
                repo_root,
                str(project.get("task_id") or ""),
                str(project.get("id") or ""),
                project=project,
            )
            s3["image"] = cur_img or {}
        except Exception as ps_err:  # noqa: BLE001
            log.warning("[short_video] prompt_settings/image 注入失败: %s", ps_err)

        # REQ-20261004-stage3-prompt-edit + REQ-20261012-099：模板生效文本注入
        # （radio 的 data-prompt 预填）。不依赖 highlights.json 存在——新项目
        # 也要能点快速模板填充提示词。
        try:
            from slirn_home.short_video_analyze import _PROMPT_TEMPLATES
            cp_data = _sv_service.load_custom_prompts(
                repo_root, str(project.get("task_id") or ""), str(project.get("id") or ""),
            )
            s3["prompt_state"] = {
                "custom": cp_data,  # 用户修改过的；不存在 → 空 dict
                "defaults": {name: tmpl for name, tmpl in _PROMPT_TEMPLATES.items()},
            }
        except Exception as ps_err:  # noqa: BLE001
            log.warning("[short_video] prompt_state 注入失败: %s", ps_err)

        try:
            hp = _sv_service.stage_highlights_path(
                repo_root, str(project.get("task_id") or ""), str(project.get("id") or "")
            )
            if not hp.is_file():
                return
            import json as _json
            doc = _json.loads(hp.read_text(encoding="utf-8"))
            hls = doc.get("highlights") if isinstance(doc, dict) else None
            if not isinstance(hls, list) or not hls:
                return

            # 读源 SRT 做清洗（缺 SRT 时跳过清洗，保留原数据）
            srt_path = _sv_service.stage_raw_srt_path(
                repo_root, str(project.get("task_id") or ""), str(project.get("id") or "")
            )
            srt_index_to_line: dict = {}
            srt_lines_dicts: list[dict] = []
            if srt_path.is_file():
                try:
                    from slirn_home.short_video_analyze import (
                        _parse_srt_lines, clean_highlight_subtitles,
                    )
                    srt_lines = _parse_srt_lines(srt_path.read_text(encoding="utf-8"))
                    srt_index_to_line = {ln.index: ln for ln in srt_lines}
                    # REQ-20261004-stage3-restructure：UI 需要按 src_index 展示时间戳，
                    # 把 SrtLine 序列化成 dict list 注入 project state（只读）
                    srt_lines_dicts = [
                        {"index": ln.index, "start_ms": ln.start_ms,
                         "end_ms": ln.end_ms, "text": ln.text}
                        for ln in srt_lines
                    ]
                    if srt_index_to_line:
                        new_hls = []
                        for h in hls:
                            cleaned, _warns = clean_highlight_subtitles(h, srt_index_to_line)
                            # 即使 cleaned 为空也保留 highlight（前端不会崩）
                            nh = dict(h)
                            nh["subtitle_lines"] = cleaned
                            new_hls.append(nh)
                        hls = new_hls
                except Exception as clean_err:  # noqa: BLE001
                    log.warning("[short_video] highlights 字幕清洗失败: %s", clean_err)

            pl = project.setdefault("pipeline", {})
            s3 = pl.setdefault("stage3_analyze", {})
            # 修订七 bugfix：state 镜像是 highlights.json 的**派生数据**，doc 才是
            # source of truth——必须按 doc 覆盖，不能 `if not s3.get("highlights")`
            # 跳过。旧写法会被 rerun_one 留在磁盘上的过期单组镜像卡死：整跑后
            # doc 换成新 5 组，state 里还是旧的那 1 组 → 页面只显示一组（用户
            # 报「只看到 3 号短视频」）。GET 不落盘，覆盖只影响本次渲染。
            s3["highlights"] = hls
            s3["count"] = len(hls)
            # 注入 srt_lines（只在第一次注入，避免重复覆盖）
            if srt_lines_dicts and not s3.get("srt_lines"):
                s3["srt_lines"] = srt_lines_dicts
        except Exception:  # noqa: BLE001 — UI 兜底，不阻断渲染
            pass

    @app.app.post("/slirn/api/short_video_list")
    async def short_video_list(body: dict = Body(default_factory=dict)):
        # REQ-20261012-099 修订四：scope 过滤（我的任务/全部任务）。
        # admin 默认 all（老行为）；普通用户无论传什么都钳回 mine。
        user = _get_cur_user()
        is_admin = bool(user and user.get("is_admin"))
        scope = str(body.get("scope") or "all").strip() or "all"
        if not is_admin:
            scope = "mine"
        tasks = _visible_short_video_tasks(scope)
        task_ids = [t["task_id"] for t in tasks]
        projects = _sv_service.list_projects_for_tasks(repo_root, task_ids)
        return _ok(html=_sv_ui.render_project_list(
            tasks, projects, scope=scope, is_admin=is_admin,
        ))

    @app.app.post("/slirn/api/short_video_create")
    async def short_video_create(body: dict = Body(default_factory=dict)):
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
            project = _sv_service.create_project(
                repo_root, tid,
                str(body.get("name") or "短视频混剪"),
                str(body.get("brief") or ""),
                body.get("config") if isinstance(body.get("config"), dict) else {},
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"创建项目失败: {e}")
        return _ok(project=project, toast="短视频项目已创建")

    @app.app.post("/slirn/api/short_video_get")
    async def short_video_get(body: dict = Body(default_factory=dict)):
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            project = _sv_service.load_project(repo_root, tid, pid)
        except Exception as e:  # noqa: BLE001
            return _err(f"读取项目失败: {e}")
        _inject_stage3_highlights(project)
        return _ok(project=project, html=_sv_ui.render_project(project, repo_root=repo_root))

    @app.app.post("/slirn/api/short_video_save")
    async def short_video_save(body: dict = Body(default_factory=dict)):
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            project = _sv_service.update_project(repo_root, tid, pid, body)
        except Exception as e:  # noqa: BLE001
            return _err(f"保存项目失败: {e}")
        return _ok(project=project, html=_sv_ui.render_project(project, repo_root=repo_root), toast="项目已保存")

    @app.app.post("/slirn/api/short_video_ai_storyboard")
    async def short_video_ai_storyboard(body: dict = Body(default_factory=dict)):
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            update_payload: dict = {}
            if "brief" in body or isinstance(body.get("config"), dict):
                update_payload = {
                    "brief": body.get("brief"),
                    "config": body.get("config"),
                }
            if "base_material_id" in body:
                update_payload["base_material_id"] = body.get("base_material_id")
            if update_payload:
                _sv_service.update_project(repo_root, tid, pid, update_payload)
            project = _sv_service.generate_storyboard(
                repo_root, tid, pid,
                base_material_id=str(body.get("base_material_id") or ""),
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"生成分镜失败: {e}")
        return _ok(
            project=project,
            html=_sv_ui.render_project(project, repo_root=repo_root),
            toast=f"分镜已生成（{(project.get('storyboard') or {}).get('model') or 'local'}）",
        )

    @app.app.post("/slirn/api/short_video_upload_material")
    async def short_video_upload_material(
        task_id: str = _Form(...),
        project_id: str = _Form(...),
        kind: str = _Form("auto"),
        file: UploadFile = File(...),
    ):
        if not task_id or not project_id:
            return _err("缺少 task_id 或 project_id")
        import shutil as _shutil
        import tempfile as _tempfile

        suffix = Path(file.filename or "").suffix
        tmp_path: Path | None = None
        try:
            with _tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = Path(tmp.name)
                await file.seek(0)
                _shutil.copyfileobj(file.file, tmp)
            material = _sv_service.add_material_path(
                repo_root, task_id, project_id, tmp_path, kind=kind,
                display_name=str(file.filename or ""),
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"上传素材失败: {e}")
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        return _ok(material=material, toast="素材已上传")

    @app.app.post("/slirn/api/short_video_render")
    async def short_video_render(body: dict = Body(default_factory=dict)):
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        variant_ids = body.get("variant_ids")
        if not isinstance(variant_ids, list):
            variant_ids = []
        try:
            job = _sv_render.start_render(repo_root, tid, pid, variant_ids)
        except Exception as e:  # noqa: BLE001
            return _err(f"启动渲染失败: {e}")
        return _ok(job=job, toast="短视频渲染已启动")

    @app.app.post("/slirn/api/short_video_render_status")
    async def short_video_render_status(body: dict = Body(default_factory=dict)):
        job_id = (body.get("job_id") or "").strip()
        if not job_id:
            return _err("缺少 job_id")
        job = _sv_render.job_status(job_id)
        if not job:
            return _err(f"渲染任务不存在或已过期: {job_id}")
        return _ok(job=job)

    @app.app.post("/slirn/api/short_video_render_cancel")
    async def short_video_render_cancel(body: dict = Body(default_factory=dict)):
        job_id = (body.get("job_id") or "").strip()
        if not job_id:
            return _err("缺少 job_id")
        ok = _sv_render.cancel_render(job_id)
        if not ok:
            return _err("渲染任务无法取消或已结束")
        return _ok(toast="已发送取消请求")

    @app.app.post("/slirn/api/short_video_delete")
    async def short_video_delete(body: dict = Body(default_factory=dict)):
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            deleted = _sv_service.delete_project(repo_root, tid, pid)
        except Exception as e:  # noqa: BLE001
            return _err(f"删除项目失败: {e}")
        return _ok(deleted=deleted, toast="项目已删除")

    @app.app.post("/slirn/api/short_video_remove_material")
    async def short_video_remove_material(body: dict = Body(default_factory=dict)):
        """REQ-20261001-096：删除项目素材（上传素材连文件删，任务导入只删引用）。"""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        mid = (body.get("material_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        if not mid:
            return _err("缺少 material_id")
        try:
            project = _sv_service.remove_material(repo_root, tid, pid, mid)
        except Exception as e:  # noqa: BLE001
            return _err(f"删除素材失败: {e}")
        return _ok(project=project, html=_sv_ui.render_project(project, repo_root=repo_root), toast="素材已删除")

    @app.app.post("/slirn/api/short_video_stage6_run")
    async def short_video_stage6_run(body: dict = Body(default_factory=dict)):
        """REQ-20261003-098：Stage 6 精剪混编。

        每条 highlight 独立调上游 cut_by_srt.py 子进程，产物落到
        ``tasks/<tid>/short_video/<pid>/stage6/``。运行结束返回最新
        pipeline.stage6_fine 状态（REQ-20261016-148 键重命名）。
        """
        from slirn_home import short_video_finalize as _sv_finalize

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        items = body.get("items")
        if not isinstance(items, list) or not items:
            return _err("缺少 items（每项至少含 index/coarse_mp4/refined_srt）")
        # 兜底允许每个 item 给出 coarse_mp4/refined_srt 的绝对路径
        try:
            state = _sv_finalize.run_stage6_batch(repo_root, tid, pid, items)
        except Exception as e:  # noqa: BLE001
            log.exception("stage6 run failed")
            return _err(f"Stage 6 执行失败: {e}")
        return _ok(stage6=state, toast=f"Stage 6 完成（{len(state.get('highlights') or [])} 条）")

    @app.app.post("/slirn/api/short_video_select_source")
    async def short_video_select_source(body: dict = Body(default_factory=dict)):
        """REQ-20261003-098：Stage 1 选源视频。"""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        mid = (body.get("material_id") or "").strip()
        if not tid or not pid or not mid:
            return _err("缺少 task_id / project_id / material_id")
        try:
            project = _sv_service.select_source_material(repo_root, tid, pid, mid)
        except Exception as e:  # noqa: BLE001
            return _err(f"选源失败: {e}")
        return _ok(project=project, html=_sv_ui.render_project(project, repo_root=repo_root), toast="已选源视频")

    # ---------- REQ-20261022-143：短视频「流程配置」自动执行 ----------
    from slirn_home import short_video_flow as _sv_flow

    @app.app.post("/slirn/api/short_video_flow_load")
    async def short_video_flow_load(body: dict = Body(default_factory=dict)):
        """读流程配置（不存在 → 默认配置）+ 最近执行历史。"""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            cfg = _sv_flow.load_config(repo_root, tid, pid)
            hist = _sv_flow.load_history(repo_root, tid, pid)
        except Exception as e:  # noqa: BLE001
            return _err(f"读取流程配置失败: {e}")
        return _ok(config=cfg, history=list(reversed(hist[-20:])),
                   stages=[{"key": k, "label": v} for k, v in _sv_flow.STAGE_ORDER])

    @app.app.post("/slirn/api/short_video_flow_save")
    async def short_video_flow_save(body: dict = Body(default_factory=dict)):
        """保存流程配置（校验后落盘到项目目录）。"""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            cfg = _sv_flow.save_config(repo_root, tid, pid, body.get("config") or {})
        except Exception as e:  # noqa: BLE001
            return _err(f"保存流程配置失败: {e}")
        return _ok(config=cfg, toast="⚙️ 流程配置已保存")

    @app.app.post("/slirn/api/short_video_flow_run")
    async def short_video_flow_run(body: dict = Body(default_factory=dict)):
        """启动自动执行（后台线程）。since 可选：指定从哪一阶段开跑。"""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        since = (body.get("since") or "").strip() or None
        try:
            started = _sv_flow.run_flow(repo_root, tid, pid, since=since)
        except Exception as e:  # noqa: BLE001
            log.exception("short video flow run failed")
            return _err(f"启动流程失败: {e}")
        job = _sv_flow.get_job(tid, pid) or {}
        return _ok(started=started, job=job,
                   toast="▶ 已启动自动执行" if started else "⏳ 流程已在运行中")

    @app.app.post("/slirn/api/short_video_flow_stop")
    async def short_video_flow_stop(body: dict = Body(default_factory=dict)):
        """请求停止自动执行。"""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        stopped = _sv_flow.stop_flow(tid, pid)
        return _ok(stopped=stopped,
                   toast="⏹ 已请求停止" if stopped else "没有正在运行的流程")

    @app.app.post("/slirn/api/short_video_flow_status")
    async def short_video_flow_status(body: dict = Body(default_factory=dict)):
        """状态轮询：当前阶段 / 进度 / 日志。"""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        job = _sv_flow.get_job(tid, pid)
        if job is None:
            return _ok(job=None, running=False, toast="尚未执行过")
        return _ok(job=job, running=(job.get("state") == "running"))

    @app.app.post("/slirn/api/short_video_stage2_run")
    async def short_video_stage2_run(body: dict = Body(default_factory=dict)):
        """REQ-20261003-098：Stage 2 字幕提取。"""
        from slirn_home import short_video_extract as _sv_extract

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            state = _sv_extract.run_stage2(
                repo_root, tid, pid,
                model=str(body.get("model") or "paraformer"),
                lang=str(body.get("lang") or "zh"),
                hotwords=str(body.get("hotwords") or ""),
                sd_switch=str(body.get("sd_switch") or "no"),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage2 run failed")
            return _err(f"Stage 2 执行失败: {e}")
        return _ok(stage2=state, toast=f"Stage 2 {state.get('status', '')}")

    @app.app.post("/slirn/api/short_video_stage3_run")
    async def short_video_stage3_run(body: dict = Body(default_factory=dict)):
        """REQ-20261012-099：Stage 3 内容提取（字幕 + 用户提示词 + 截图 → AI）。"""
        from slirn_home import llm_config
        from slirn_home import short_video_analyze as _sv_analyze

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        template = str(body.get("template") or _sv_service.HIGHLIGHT_TEMPLATE_HOOK_FIRST)
        n_clips = int(body.get("n_clips") or 4)
        custom_prompt = str(body.get("custom_prompt") or "")
        prompt = str(body.get("prompt") or "")
        use_image = bool(body.get("use_image"))
        # REQ-20261015-120：每条时长区间（秒）直接取用户设置；非法值由后端兜底
        duration_min_sec = body.get("duration_min_sec")
        duration_max_sec = body.get("duration_max_sec")
        entry = llm_config.get_current_entry(repo_root)
        try:
            state = _sv_analyze.run_stage3(
                repo_root, tid, pid,
                template=template, n_clips=n_clips,
                custom_prompt=custom_prompt, entry=entry,
                prompt=prompt, use_image=use_image,
                duration_min_sec=duration_min_sec,
                duration_max_sec=duration_max_sec,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage3 run failed")
            return _err(f"Stage 3 执行失败: {e}")
        return _ok(stage3=state, toast=f"Stage 3 {state.get('status', '')}")

    @app.app.post("/slirn/api/short_video_stage3_rerun_one")
    async def short_video_stage3_rerun_one(body: dict = Body(default_factory=dict)):
        """REQ-20261012-099：单片段重新解析。

        body: task_id / project_id / hl_index / prompt（当前文本域）/
        use_image（是否带截图）。替换该片段的时间段与字幕内容，
        下游 Stage 4/5/6 重置。
        """
        from slirn_home import llm_config
        from slirn_home import short_video_analyze as _sv_analyze

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        if hl_index <= 0:
            return _err("hl_index 无效")
        entry = llm_config.get_current_entry(repo_root)
        try:
            state = _sv_analyze.rerun_stage3_one(
                repo_root, tid, pid, hl_index,
                prompt=str(body.get("prompt") or ""),
                use_image=bool(body.get("use_image")),
                entry=entry,
                duration_min_sec=body.get("duration_min_sec"),
                duration_max_sec=body.get("duration_max_sec"),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage3 rerun-one failed")
            return _err(f"片段 #{hl_index} 重新解析失败: {e}")
        return _ok(stage3=state, toast=f"片段 #{hl_index} 重新解析 {state.get('status', '')}")

    @app.app.post("/slirn/api/short_video_stage3_upload_image")
    async def short_video_stage3_upload_image(
        task_id: str = _Form(...),
        project_id: str = _Form(...),
        file: UploadFile = File(...),
    ):
        """REQ-20261012-099：上传/覆盖 Stage 3 截图（内容提取的输入图）。"""
        if not task_id or not project_id:
            return _err("缺少 task_id 或 project_id")
        import shutil as _shutil
        import tempfile as _tempfile

        suffix = Path(file.filename or "").suffix
        tmp_path: Path | None = None
        try:
            with _tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = Path(tmp.name)
                await file.seek(0)
                _shutil.copyfileobj(file.file, tmp)
            entry = _sv_service.save_stage3_image(repo_root, task_id, project_id, tmp_path)
        except Exception as e:  # noqa: BLE001
            return _err(f"截图上传失败: {e}")
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        project = _sv_service.load_project(repo_root, task_id, project_id)
        _inject_stage3_highlights(project)
        return _ok(
            image=entry, project=project,
            html=_sv_ui.render_project(project, repo_root=repo_root), toast="截图已上传",
        )

    @app.app.post("/slirn/api/short_video_stage3_remove_image")
    async def short_video_stage3_remove_image(body: dict = Body(default_factory=dict)):
        """REQ-20261012-099：移除 Stage 3 截图。"""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            removed = _sv_service.remove_stage3_image(repo_root, tid, pid)
        except Exception as e:  # noqa: BLE001
            return _err(f"移除截图失败: {e}")
        project = _sv_service.load_project(repo_root, tid, pid)
        _inject_stage3_highlights(project)
        return _ok(
            removed=removed, project=project,
            html=_sv_ui.render_project(project, repo_root=repo_root),
            toast="截图已移除" if removed else "本就没有截图",
        )

    @app.app.post("/slirn/api/short_video_load_prompts")
    async def short_video_load_prompts(body: dict = Body(default_factory=dict)):
        """REQ-20261013-108：一次性返回 stage3 + stage4 的全局默认 / 历史 + 系统级
        提示词常量 + 项目级用户覆盖（``task_id`` + ``project_id`` 给出时）。

        前端两个 stage 共享一次 roundtrip；避免 4 个端点轮询。
        """
        from slirn_home import short_video_service as _svc
        all_settings = _svc.load_all_prompt_settings(repo_root)
        system_prompts = {
            "stage3": _svc.STAGE3_SYSTEM_PROMPT,
            "stage4": _svc.STAGE4_SYSTEM_PROMPT,
        }
        result: dict = {
            "settings": all_settings,
            "history_html": {
                s: _sv_ui.render_prompt_history_list(all_settings.get(s) or {})
                for s in ("stage3", "stage4")
            },
            "system_prompts": system_prompts,
        }
        result.pop("html", None)  # 去掉无关空字段
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if tid and pid:
            try:
                project = _svc.load_project(repo_root, tid, pid)
                pl = project.get("pipeline", {}) or {}
                s3 = pl.get("stage3_analyze", {}) or {}
                s4 = pl.get("stage4_optimize", {}) or {}
                result["project_prompts"] = {
                    "stage3": (s3.get("prompt_user") or s3.get("prompt") or ""),
                    "stage4": (s4.get("prompt_user") or s4.get("prompt") or ""),
                }
            except Exception:  # noqa: BLE001
                result["project_prompts"] = {"stage3": "", "stage4": ""}
        else:
            result["project_prompts"] = {"stage3": "", "stage4": ""}
        return _ok(**result)

    @app.app.post("/slirn/api/short_video_save_default_prompt")
    async def short_video_save_default_prompt(body: dict = Body(default_factory=dict)):
        """REQ-20261012-099 + REQ-20261013-108：把当前提示词保存为指定 stage
        的全局默认（stage 默认为 ``stage3``，保持旧调用兼容）。

        服务层会尽力同步进历史列表；``history_html`` 供前端原地刷新
        （默认条目在列表中标 ⭐）。
        """
        prompt = str(body.get("prompt") or "")
        if not prompt.strip():
            return _err("提示词不能为空")
        stage = str(body.get("stage") or "stage3").strip().lower() or "stage3"
        try:
            entry = _sv_service.save_default_prompt(repo_root, prompt, stage=stage)
        except Exception as e:  # noqa: BLE001
            return _err(f"保存默认提示词失败: {e}")
        settings = _sv_service.load_prompt_settings(repo_root, stage=stage)
        return _ok(
            default_prompt=entry,
            history=settings["history"],
            history_html=_sv_ui.render_prompt_history_list(settings),
            toast="已设为默认提示词",
            stage=stage,
        )

    @app.app.post("/slirn/api/short_video_prompt_history_save")
    async def short_video_prompt_history_save(body: dict = Body(default_factory=dict)):
        """REQ-20261012-099 + REQ-20261013-108：提示词存入指定 stage 的历史
        列表（≤ 10 条最新，stage 默认 ``stage3`` 保持兼容）。

        已满且新提示词 → ``status=need_confirm``；前端确认后带
        ``confirm=true`` 重发（覆盖最旧）。成功时附 ``history_html`` 片段，
        前端原地替换历史下拉（自动刷新，无需重开页面）。
        """
        prompt = str(body.get("prompt") or "")
        if not prompt.strip():
            return _err("提示词不能为空")
        confirm = bool(body.get("confirm"))
        stage = str(body.get("stage") or "stage3").strip().lower() or "stage3"
        try:
            result = _sv_service.save_prompt_history(
                repo_root, prompt, confirm=confirm, stage=stage,
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"保存提示词列表失败: {e}")
        if result.get("status") == "need_confirm":
            return _ok(
                status="need_confirm", history=result.get("history"),
                max=result.get("max"),
                toast=f"列表已满 {result.get('max')} 条，是否覆盖最旧一条？",
                stage=stage,
            )
        settings = _sv_service.load_prompt_settings(repo_root, stage=stage)
        return _ok(
            status="saved", history=result.get("history"),
            history_html=_sv_ui.render_prompt_history_list(settings),
            toast="已存入提示词列表",
            stage=stage,
        )

    @app.app.post("/slirn/api/short_video_prompt_history_delete")
    async def short_video_prompt_history_delete(body: dict = Body(default_factory=dict)):
        """REQ-20261016-128：从历史列表删除一条；若是默认则同步清空默认。"""
        prompt = str(body.get("prompt") or "")
        if not prompt.strip():
            return _err("提示词不能为空")
        stage = str(body.get("stage") or "stage3").strip().lower() or "stage3"
        try:
            result = _sv_service.delete_prompt_history(
                repo_root, prompt, stage=stage,
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"删除失败: {e}")
        settings = _sv_service.load_prompt_settings(repo_root, stage=stage)
        suffix = "（默认标记已移除）" if result["removed_default"] else ""
        return _ok(
            removed_default=result["removed_default"],
            history=result["history"],
            history_html=_sv_ui.render_prompt_history_list(settings),
            stage=stage,
            toast=f"已删除历史提示词{suffix}",
        )

    @app.app.post("/slirn/api/short_video_stage4_optimize")
    async def short_video_stage4_optimize(body: dict = Body(default_factory=dict)):
        """REQ-20261013-100 + 108 + REQ-20261016-146：Stage 4「内容优化」（替换原
        Stage 3.5 字幕一致性核验）：把 Stage 3 全部短视频的标题+字幕行交给
        大模型 — 按语义切分长行（子文本拼接须逐字等于原文，时间戳按占比内插）、
        修正与音频内容不符的文字、删除无用行；只用内置系统提示词（无自定义
        提示词，body 里的 prompt 字段被忽略）；删除行记录到
        state.highlights[].deleted_lines 供 UI 保留展示。结果写 Stage 4 自己的
        optimized_highlights.json（REQ-20261017-149 分离存储 — Stage 3 的
        highlights.json 原表不动，两阶段各一套字幕），粗剪及之后作废。

        REQ-20261017-155：可选 ``hl_index`` — 单条重新优化（只重跑该条，
        其余条保留既有优化结果）；不传/0 = 全量（默认）。
        """
        from slirn_home import short_video_optimize as _sv_opt

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        hl_raw = body.get("hl_index")
        hl_index = None
        if hl_raw is not None and str(hl_raw).strip():
            try:
                hl_index = int(hl_raw)
            except (TypeError, ValueError):
                return _err("hl_index 无效")
        try:
            state = _sv_opt.run_stage4_optimize(repo_root, tid, pid,
                                                hl_index=hl_index)
        except Exception as e:  # noqa: BLE001
            log.exception("stage4_optimize run failed")
            return _err(f"Stage 4 内容优化执行失败: {e}")
        return _ok(
            stage4_optimize=state,
            toast=(f"片段 #{hl_index} 重新优化完成" if hl_index
                   else "内容优化完成，删除的行保留在下方展示（🗑 LLM已删）"),
        )

    @app.app.post("/slirn/api/short_video_stage4_line_edit")
    async def short_video_stage4_line_edit(body: dict = Body(default_factory=dict)):
        """REQ-20261013-108：Stage 4 整行替换 text（fix）。

        body: {task_id, project_id, hl_index, src_index, text, remove?}。
        写 per_hl JSON（**不污染**底稿表 — stage3/highlights.json 原表 /
        stage4_optimize/optimized_highlights.json 优化表都不动）。
        REQ-20261017-153：``remove=true`` = ✖ 还原（清掉该行 line_edit）。
        """
        from slirn_home import short_video_optimize as _sv_opt
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        try:
            src_index = int(body.get("src_index") or 0)
        except (TypeError, ValueError):
            return _err("src_index 无效")
        new_text = str(body.get("text") or "")
        remove = bool(body.get("remove"))
        if not tid or not pid or hl_index <= 0 or src_index <= 0:
            return _err("缺少 task_id / project_id / hl_index / src_index")
        try:
            doc = _sv_opt.per_hl_apply_line_edit(
                repo_root, tid, pid, hl_index, src_index, new_text,
                remove=remove,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage4_line_edit failed")
            return _err(f"保存失败: {e}")
        return _ok(per_hl=doc, toast="已保存")

    @app.app.post("/slirn/api/short_video_stage4_line_toggle")
    async def short_video_stage4_line_toggle(body: dict = Body(default_factory=dict)):
        """REQ-20261013-108：Stage 4 整行删除/恢复（toggle）。"""
        from slirn_home import short_video_optimize as _sv_opt
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        try:
            src_index = int(body.get("src_index") or 0)
        except (TypeError, ValueError):
            return _err("src_index 无效")
        if not tid or not pid or hl_index <= 0 or src_index <= 0:
            return _err("缺少 task_id / project_id / hl_index / src_index")
        try:
            doc = _sv_opt.per_hl_toggle_line_mark(
                repo_root, tid, pid, hl_index, src_index,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage4_line_toggle failed")
            return _err(f"操作失败: {e}")
        marked = int(src_index) in [int(x) for x in (doc.get("line_marks") or [])]
        return _ok(per_hl=doc, marked=marked, toast="已删除" if marked else "已恢复")

    @app.app.post("/slirn/api/short_video_stage4_line_reorder")
    async def short_video_stage4_line_reorder(body: dict = Body(default_factory=dict)):
        """REQ-20261013-110：Stage 4 段内重排（白名单）。空 list = 清除重排。

        Body: {task_id, project_id, hl_index, order: [src_index, ...]}
        写入 per-hl line_order 字段；不重算下游 stage（coarse cut 暂不读 per-hl）。
        """
        from slirn_home import short_video_optimize as _sv_opt
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        order_raw = body.get("order")
        if order_raw is None:
            order: list[int] = []
        elif not isinstance(order_raw, list):
            return _err("order 必须是 list[src_index] 或 null")
        else:
            order = [int(x) for x in order_raw if str(x).strip().lstrip("-").isdigit()]
        if not tid or not pid or hl_index <= 0:
            return _err("缺少 task_id / project_id / hl_index")
        try:
            doc = _sv_opt.per_hl_reorder(
                repo_root, tid, pid, hl_index, order,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage4_line_reorder failed")
            return _err(f"重排失败: {e}")
        cleared = not doc.get("line_order")
        return _ok(per_hl=doc, line_order=doc.get("line_order"),
                   toast="已清除重排" if cleared else f"已重排 {len(order)} 行")

    @app.app.post("/slirn/api/short_video_stage4_resplit")
    async def short_video_stage4_resplit(body: dict = Body(default_factory=dict)):
        """REQ-20261016-147：Stage 4 ✂️ 重新切分 = 行内剪词。

        body: {task_id, project_id, hl_index, src_index, text}。
        text = 新文字（原文去掉无用词）；与原文相同/为空 = 恢复整行。
        预览/粗剪时按 token 对齐切掉删掉的字对应的视频段。
        """
        from slirn_home import short_video_optimize as _sv_opt
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        try:
            src_index = int(body.get("src_index") or 0)
        except (TypeError, ValueError):
            return _err("src_index 无效")
        text = str(body.get("text") or "")
        if not tid or not pid or hl_index <= 0 or src_index <= 0:
            return _err("缺少 task_id / project_id / hl_index / src_index")
        try:
            doc = _sv_opt.per_hl_trim(
                repo_root, tid, pid, hl_index, src_index, text,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage4_resplit failed")
            return _err(f"剪词失败: {e}")
        trimmed = str(src_index) in (doc.get("line_trims") or {})
        return _ok(per_hl=doc, toast=(
            "已剪词 — 预览/粗剪会切掉删掉的字对应的视频" if trimmed
            else "已恢复整行（取消剪词）"))

    @app.app.post("/slirn/api/short_video_stage4_unsplit")
    async def short_video_stage4_unsplit(body: dict = Body(default_factory=dict)):
        """REQ-20261013-108：Stage 4 取消切分（unsplit；REQ-20261016-147 起含恢复剪词）。"""
        from slirn_home import short_video_optimize as _sv_opt
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        try:
            src_index = int(body.get("src_index") or 0)
        except (TypeError, ValueError):
            return _err("src_index 无效")
        if not tid or not pid or hl_index <= 0 or src_index <= 0:
            return _err("缺少 task_id / project_id / hl_index / src_index")
        try:
            doc = _sv_opt.per_hl_unsplit(
                repo_root, tid, pid, hl_index, src_index,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage4_unsplit failed")
            return _err(f"取消切分失败: {e}")
        return _ok(per_hl=doc, toast="已取消切分")

    @app.app.post("/slirn/api/short_video_stage4_preview")
    async def short_video_stage4_preview(body: dict = Body(default_factory=dict)):
        """REQ-20261019-214：Stage 4 预览改播放列表（原视频逐段 seek 播放）。

        旧实现（REQ-20261013-108 拼一段 preview mp4 + 内嵌 video）已移除；
        现返回 {video_url, lines[]}，前端浮窗按行窗口在**原视频**上跳转播放
        + 行高亮 + 点任意行从该行起播（short_video.js stage4Preview）。

        body: {task_id, project_id, hl_index}。
        """
        from slirn_home import short_video_coarse as _sv_coarse
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        if not tid or not pid or hl_index <= 0:
            return _err("缺少 task_id / project_id / hl_index")
        try:
            res = _sv_coarse.preview_playlist(repo_root, tid, pid, hl_index)
        except Exception as e:  # noqa: BLE001
            log.exception("stage4_preview failed")
            return _err(f"预览失败: {e}")
        if res.get("status") != "done":
            return _err(res.get("error") or "预览失败")
        return _ok(
            hl_index=hl_index,
            video_url=res.get("video_url"),
            lines=res.get("lines"),
            duration_sec=res.get("duration_sec"),
            segments_count=res.get("segments_count"),
            toast=f"预览 {res.get('segments_count')} 段 / "
                  f"{res.get('duration_sec', 0):.1f}s",
        )

    @app.app.post("/slirn/api/short_video_stage3_reorder_subtitles")
    async def short_video_stage3_reorder_subtitles(body: dict = Body(default_factory=dict)):
        """REQ-20261004-stage3-restructure：拖拽重排字幕行。

        body:
          - task_id / project_id
          - hl_index: highlight 序号 (1-based, 与 highlights.json 一致)
          - src_index: 被拖动行的 src_index
          - before_src_index: 插入到此行之前；None/0 表示移到末尾

        落地：更新 highlights.json + 同步 project state + 重置下游阶段（Stage 4-7）。
        """
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        try:
            src_index = int(body.get("src_index") or 0)
        except (TypeError, ValueError):
            return _err("src_index 无效")
        before_raw = body.get("before_src_index")
        if before_raw is None or before_raw == "" or before_raw == 0:
            before_src_index = None
        else:
            try:
                before_src_index = int(before_raw)
            except (TypeError, ValueError):
                return _err("before_src_index 无效")
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        if hl_index <= 0 or src_index <= 0:
            return _err("hl_index 和 src_index 必须 > 0")

        try:
            updated = _sv_service.reorder_subtitle_line(
                repo_root, tid, pid,
                hl_index=hl_index,
                src_index=src_index,
                before_src_index=before_src_index,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage3_reorder_subtitles failed")
            return _err(f"重排失败: {e}")
        return _ok(highlight=updated, toast="字幕顺序已更新")

    @app.app.post("/slirn/api/short_video_stage3_restore_order")
    async def short_video_stage3_restore_order(body: dict = Body(default_factory=dict)):
        """REQ-20261012-099 修订六：还原某条短视频的字幕顺序。

        body: task_id / project_id / hl_index。把该组 subtitle_lines 按
        初始编号（no）升序排回大模型最初给出的顺序，下游/拼接作废。
        """
        from slirn_home import short_video_analyze as _sv_analyze

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        if hl_index <= 0:
            return _err("hl_index 必须 > 0")
        try:
            updated = _sv_analyze.restore_hl_order(repo_root, tid, pid, hl_index)
        except Exception as e:  # noqa: BLE001
            log.exception("stage3_restore_order failed")
            return _err(f"还原失败: {e}")
        return _ok(highlight=updated, toast=f"#{hl_index} 已还原为大模型最初顺序")

    @app.app.post("/slirn/api/short_video_stage3_remove_hl")
    async def short_video_stage3_remove_hl(body: dict = Body(default_factory=dict)):
        """REQ-20261012-099 修订八：删除一条短视频。

        body: task_id / project_id / hl_index。前端先弹 confirm，用户确认
        后才调到这里真删；其余条目 index 不变，下游/拼接作废。
        """
        from slirn_home import short_video_analyze as _sv_analyze

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            hl_index = int(body.get("hl_index") or 0)
        except (TypeError, ValueError):
            return _err("hl_index 无效")
        if hl_index <= 0:
            return _err("hl_index 必须 > 0")
        try:
            res = _sv_analyze.remove_stage3_hl(repo_root, tid, pid, hl_index)
        except Exception as e:  # noqa: BLE001
            log.exception("stage3_remove_hl failed")
            return _err(f"删除失败: {e}")
        left = int(res.get("count") or 0)
        return _ok(
            removed_index=res.get("removed_index"),
            toast=f"#{hl_index} 已删除" + (f"，剩余 {left} 条" if left else "（已无剩余，可重新解析）"),
        )

    @app.app.post("/slirn/api/short_video_save_custom_prompt")
    async def short_video_save_custom_prompt(body: dict = Body(default_factory=dict)):
        """REQ-20261004-stage3-prompt-edit：保存用户对某个模板的修改提示词。

        body:
          - task_id / project_id
          - template: hook_first / topic_cluster / story_arc
          - prompt: 修改后的完整提示词（base + 策略部分整体替换）
        """
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        template = (body.get("template") or "").strip()
        prompt = str(body.get("prompt") or "")
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        if template not in {"hook_first", "topic_cluster", "story_arc"}:
            return _err(f"未知模板: {template}")
        if not prompt.strip():
            return _err("prompt 不能为空")
        try:
            saved = _sv_service.save_custom_prompt(
                repo_root, tid, pid,
                template=template, prompt=prompt,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("save_custom_prompt failed")
            return _err(f"保存失败: {e}")
        return _ok(custom_prompt=saved, toast=f"已保存 {template} 提示词")

    @app.app.post("/slirn/api/short_video_reset_custom_prompt")
    async def short_video_reset_custom_prompt(body: dict = Body(default_factory=dict)):
        """REQ-20261004-stage3-prompt-edit：恢复某模板到系统默认（删除用户修改）。"""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        template = (body.get("template") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        if template not in {"hook_first", "topic_cluster", "story_arc"}:
            return _err(f"未知模板: {template}")
        try:
            removed = _sv_service.reset_custom_prompt(
                repo_root, tid, pid, template=template,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("reset_custom_prompt failed")
            return _err(f"重置失败: {e}")
        return _ok(
            removed=removed,
            toast="已恢复默认" if removed else "无需重置",
        )

    @app.app.post("/slirn/api/short_video_stage4_run")
    async def short_video_stage4_run(body: dict = Body(default_factory=dict)):
        """REQ-20261003-098：Stage 4 粗剪合成（同步保留 — 异步入口见 stage5_export）。"""
        from slirn_home import short_video_coarse as _sv_coarse

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            state = _sv_coarse.run_stage5(
                repo_root, tid, pid,
                start_ost_ms=int(body.get("start_ost") or 0),
                end_ost_ms=int(body.get("end_ost") or 100),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage4 run failed")
            return _err(f"Stage 4 执行失败: {e}")
        return _ok(stage4=state, toast=f"Stage 4 {state.get('status', '')}")

    # REQ-20261013-103：Stage 5 粗剪按长视频 compose_rough 整套搬（异步 + 状态轮询 + 删除 + 连续 SRT）
    @app.app.post("/slirn/api/short_video_stage5_export")
    async def short_video_stage5_export(request: Request, body: dict = Body(default_factory=dict)):
        """异步启动 Stage 5 粗剪合成（仿 compose_rough / start_compose）。

        REQ-20261013-108：可选 ``hl_index`` — 不传或 0 = 全量（默认，保持向
        后兼容）；int = 只跑一条 highlight（Stage 4 预览跳转用）。其他
        highlight 的 coarse_NN.{mp4,srt} 文件**不会被删**。
        """
        from slirn_home import short_video_coarse as _sv_coarse

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        hl_raw = body.get("hl_index")
        hl_index = None
        if hl_raw is not None and str(hl_raw).strip():
            try:
                hl_index = int(hl_raw)
            except (TypeError, ValueError):
                return _err("hl_index 无效")
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            started = _sv_coarse.start_coarse_async(
                repo_root, tid, pid,
                start_ost_ms=int(body.get("start_ost") or 0),
                end_ost_ms=int(body.get("end_ost") or 100),
                hl_index=hl_index,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage5 export failed")
            return _err(f"Stage 5 启动失败: {e}")
        job = _sv_coarse.coarse_job_status(tid, pid) or {}
        return _ok(
            running=bool(started),
            job=job,
            hl_index=hl_index,
            toast="🎬 粗剪已启动" if started else "⏳ 粗剪已在进行中",
        )

    @app.app.post("/slirn/api/short_video_stage5_export_status")
    async def short_video_stage5_export_status(body: dict = Body(default_factory=dict)):
        """Stage 5 粗剪进度轮询（仿 compose_rough_status）。"""
        from slirn_home import short_video_coarse as _sv_coarse

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        job = _sv_coarse.coarse_job_status(tid, pid)
        if job is None:
            # 服务重启后内存清空 — 兜底：磁盘 stage5_coarse.status=done 即视为完成
            try:
                from slirn_home import short_video_service as _svc
                proj = _svc.load_project(repo_root, tid, pid)
                st = proj.get("pipeline", {}).get("stage5_coarse", {}) or {}
                if st.get("status") == "done" and (st.get("highlights") or []):
                    return _ok("", job={"state": "done", "progress": 100.0,
                                        "count": len(st.get("highlights") or [])})
            except Exception:  # noqa: BLE001
                pass
            return _ok("", job={"state": "idle", "progress": 0.0})
        # 算 elapsed_s
        import time as _time
        job["elapsed_s"] = int(
            (job.get("finished_at") or _time.time()) - job.get("started_at", _time.time())
        )
        return _ok("", job=job)

    @app.app.post("/slirn/api/short_video_stage5_delete")
    async def short_video_stage5_delete(request: Request, body: dict = Body(default_factory=dict)):
        """删除 Stage 5 粗剪成片（仿 compose_rough_delete）。"""
        from slirn_home import short_video_coarse as _sv_coarse

        _auto = request.headers.get("X-Slirn-Auto") == "1"
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            res = _sv_coarse.delete_coarse(
                repo_root, tid, pid,
                auto=_auto, auto_session_id=_auto_session_id,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("stage5 delete failed")
            return _err(f"Stage 5 删除失败: {e}")
        return _ok(
            "", deleted=res["deleted"],
            toast="🗑️ " + res["message"] if res["deleted"] else None,
            removed=res["removed"], remaining=res["remaining"],
            message=res["message"],
        )

    @app.app.post("/slirn/api/short_video_stage5_preview_subs")
    async def short_video_stage5_preview_subs(body: dict = Body(default_factory=dict)):
        """跨 N 条 coarse_NN.srt 拼成连续时间轴 SRT（仿 compose_rough_preview_subs）。"""
        from slirn_home import short_video_coarse as _sv_coarse

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            res = _sv_coarse.preview_continuous_subs(repo_root, tid, pid)
        except Exception as e:  # noqa: BLE001
            log.exception("stage5 preview subs failed")
            return _err(f"Stage 5 连续 SRT 失败: {e}")
        return _ok(
            "", srt=res["srt"], clips=res["clips"], cached=res["cached"],
        )

    # REQ-20261019-227：粗剪成片字幕（按合成后视频 ASR + 行内改字，原文保留）
    @app.app.post("/slirn/api/short_video_subs_extract")
    async def short_video_subs_extract(body: dict = Body(default_factory=dict)):
        """异步启动单条成片字幕提取（funasr → LLM 断句标点 → subs 四件套）。"""
        from slirn_home import llm_config, short_video_subs as _sv_subs

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            idx = int(body.get("clip_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if not tid or not pid or not idx:
            return _err("缺少 task_id / project_id / clip_index")
        # REQ-228：解析 LLM 入口；未配置 → None（提取照跑，跳过整理降级为 ASR 原样）
        entry = None
        try:
            entry = llm_config.get_current_entry(repo_root)
        except Exception:  # noqa: BLE001 — 整理是增强项，未配置不阻断提取
            entry = None
        try:
            started = _sv_subs.start_subs_extract_async(
                repo_root, tid, pid, idx, llm_entry=entry)
        except Exception as e:  # noqa: BLE001
            log.exception("subs extract failed")
            return _err(f"字幕提取启动失败: {e}")
        job = _sv_subs.subs_job_status(tid, pid, idx) or {}
        return _ok(
            running=bool(started), job=job, clip_index=idx,
            llm_ready=entry is not None,
            toast="📝 字幕提取已启动" if started else "⏳ 该条字幕已在提取中",
        )

    @app.app.post("/slirn/api/short_video_subs_status")
    async def short_video_subs_status(body: dict = Body(default_factory=dict)):
        """成片字幕提取进度轮询；内存 job 丢失时按磁盘 subs 文件兜底判 done。"""
        from slirn_home import short_video_service as _svc
        from slirn_home import short_video_subs as _sv_subs

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            idx = int(body.get("clip_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if not tid or not pid or not idx:
            return _err("缺少 task_id / project_id / clip_index")
        job = _sv_subs.subs_job_status(tid, pid, idx)
        if job is None:
            try:
                if _svc.stage5_subs_srt_path(repo_root, tid, pid, idx).is_file():
                    return _ok("", job={"state": "done", "progress": 100.0,
                                        "stage": "完成"})
            except Exception:  # noqa: BLE001
                pass
            return _ok("", job={"state": "idle", "progress": 0.0})
        import time as _time
        job["elapsed_s"] = int(_time.time() - float(job.get("started_at") or 0.0))
        return _ok("", job=job)

    @app.app.post("/slirn/api/short_video_subs_get")
    async def short_video_subs_get(body: dict = Body(default_factory=dict)):
        """字幕编辑器数据：cue 列表（orig=原文 / text=当前 / modified）。

        自愈：state 里挂着 subs_srt 但磁盘文件没了（手动删/异常）→ 清字段，
        前端回到「提取字幕」按钮态。
        """
        from slirn_home import short_video_service as _svc
        from slirn_home import short_video_subs as _sv_subs

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            idx = int(body.get("clip_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if not tid or not pid or not idx:
            return _err("缺少 task_id / project_id / clip_index")
        try:
            cues = _sv_subs.subs_cues(repo_root, tid, pid, idx)
        except Exception as e:  # noqa: BLE001
            log.exception("subs get failed")
            return _err(f"读取字幕失败: {e}")
        if cues:
            return _ok("", has_subs=True, cues=cues)
        # 自愈清字段（有 fields 才写盘，避免每行渲染都空转一次 IO）
        try:
            project = _svc.load_project(repo_root, tid, pid)
            state = _svc.get_stage_state(project, "stage5_coarse")
            hl = next(
                (h for h in (state.get("highlights") or [])
                 if int(h.get("index") or 0) == idx), None,
            )
            if hl is not None and hl.get("subs_srt"):
                hl.pop("subs_srt", None)
                _svc.set_stage_state(project, "stage5_coarse", state)
                _svc.save_project(repo_root, project)
        except Exception:  # noqa: BLE001
            pass
        return _ok("", has_subs=False, cues=[])

    @app.app.post("/slirn/api/short_video_subs_save")
    async def short_video_subs_save(body: dict = Body(default_factory=dict)):
        """保存一条改字并重新物化 subs_NN.srt（text == 原文 = 还原）。"""
        from slirn_home import short_video_subs as _sv_subs

        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        try:
            idx = int(body.get("clip_index") or 0)
            cue = int(body.get("cue_index") or 0)
        except (TypeError, ValueError):
            idx = cue = 0
        text = body.get("text")
        if text is None:
            text = ""
        if not tid or not pid or not idx or not cue:
            return _err("缺少 task_id / project_id / clip_index / cue_index")
        try:
            res = _sv_subs.save_subs_edit(repo_root, tid, pid, idx, cue, str(text))
        except Exception as e:  # noqa: BLE001
            log.exception("subs save failed")
            return _err(f"保存失败: {e}")
        return _ok(
            "", action=res["action"], cue_index=res["cue_index"],
            text=res["text"], cues=res["cues"], modified=res["modified"],
            toast="已还原为原文" if res["action"] == "restored" else "已保存 — 下载/精剪用改后字幕",
        )

    # REQ-20261013-104 的 6 个 Stage 6 字幕优化端点（optimize / save_decisions /
    # resplit / unsplit / batch_search / batch_replace）已随字幕优化阶段整体
    # 移除（REQ-20261016-148）。

    # REQ-20261013-105：Stage 7 精剪 v1 完整版（仿长视频 fine 整套搬）
    @app.app.post("/slirn/api/short_video_stage7_export")
    async def short_video_stage7_export(body: dict = Body(default_factory=dict)):
        """异步启动精剪（仿 compose_service.start_compose）。

        REQ-20261017-157：body.index 传了只渲染该条（其余条 final_mp4
        保留）；不传 = 全量。
        """
        from slirn_home import short_video_fine as _sv_fine
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        idx_raw = body.get("index")
        idx = None
        if idx_raw is not None and str(idx_raw).strip():
            try:
                idx = int(idx_raw)
            except (TypeError, ValueError):
                return _err("index 无效")
        try:
            started = _sv_fine.start_fine_async(repo_root, tid, pid, index=idx)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 export failed")
            return _err(f"Stage 7 启动失败: {e}")
        job = _sv_fine.job_status(tid, pid) or {}
        toast = (f"🎬 片段 #{idx} 精剪已启动" if idx else "🎬 精剪已启动") \
            if started else "⏳ 精剪已在进行中"
        return _ok(running=bool(started), job=job, index=idx, toast=toast)

    @app.app.post("/slirn/api/short_video_stage7_clip_save")
    async def short_video_stage7_clip_save(body: dict = Body(default_factory=dict)):
        """REQ-20261017-157：单条 clip 的标题/视频/字幕覆盖保存。

        body: {task_id, project_id, index, title?/video?/subtitle?} —
        字段出现才写；空串 = 清除覆盖（回退自动继承 Stage 5）。
        """
        from slirn_home import short_video_fine as _sv_fine
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        idx_raw = body.get("index")
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            return _err("index 无效")
        if not tid or not pid or not idx:
            return _err("缺少 task_id / project_id / index")
        fields = {}
        for key in ("title", "video", "subtitle"):
            if key in body:
                fields[key] = str(body.get(key) or "")
        if not fields:
            return _err("没有要保存的字段（title/video/subtitle）")
        try:
            res = _sv_fine.save_clip(repo_root, tid, pid, idx, **fields)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 clip save failed")
            return _err(f"保存失败: {e}")
        return _ok(index=res["index"], clip=res["clip"], toast="✅ 已保存")

    @app.app.post("/slirn/api/short_video_stage7_clip_upload")
    async def short_video_stage7_clip_upload(
        task_id: str = _Form(...),
        project_id: str = _Form(...),
        index: str = _Form(...),
        slot: str = _Form(...),
        file: UploadFile = File(...),
    ):
        """REQ-20261017-157：单条 clip 手动上传视频/字幕（multipart）。

        落 stage6/clipNN_upload.<ext> 并写入该 clip 覆盖（预览/导出改用）。
        """
        if not task_id or not project_id:
            return _err("缺少 task_id 或 project_id")
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return _err("index 无效")
        from slirn_home import short_video_fine as _sv_fine
        import shutil as _shutil
        import tempfile as _tempfile

        suffix = Path(file.filename or "").suffix
        tmp_path: Path | None = None
        try:
            with _tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = Path(tmp.name)
                await file.seek(0)
                _shutil.copyfileobj(file.file, tmp)
            entry = _sv_fine.save_clip_upload(
                repo_root, task_id, project_id, idx, slot, tmp_path)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 clip upload failed")
            return _err(f"上传失败: {e}")
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        _CLIP_SLOT_NAMES = {"video": "视频", "subtitle": "字幕",
                            "bg": "背景图", "audio": "附加音频"}
        return _ok(index=entry["index"], slot=entry["slot"],
                   path=entry["path"], name=entry["name"],
                   toast=f"✅ 已替换该条{(_CLIP_SLOT_NAMES.get(slot) or '素材')}（本条独立，不用公共素材）")

    @app.app.post("/slirn/api/short_video_stage7_clip_clear")
    async def short_video_stage7_clip_clear(body: dict = Body(default_factory=dict)):
        """REQ-20261018-183：清除单条 clip 的手动素材覆盖。

        slot=video/subtitle → 回退自动继承 Stage 5（粗剪视频 / 字幕回退链）；
        slot=bg/audio → 回退公共素材。等效 save_clip 传空串（不清磁盘文件）。
        """
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        slot = (body.get("slot") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            idx = int(body.get("index") or 0)
        except (TypeError, ValueError):
            return _err("index 无效")
        if slot not in ("video", "subtitle", "bg", "audio"):
            return _err(f"未知素材槽: {slot}")
        from slirn_home import short_video_fine as _sv_fine

        try:
            _sv_fine.save_clip(repo_root, tid, pid, idx, **{slot: ""})
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 clip clear failed")
            return _err(f"清除失败: {e}")
        _names = {"video": "视频", "subtitle": "字幕", "bg": "背景图", "audio": "附加音频"}
        _dst = ("已恢复继承上一阶段" if slot in ("video", "subtitle")
                else "已回退公共素材")
        return _ok(index=idx, slot=slot,
                   toast=f"♻️ {_names[slot]}{_dst}")

    @app.app.post("/slirn/api/short_video_stage7_material_upload")
    async def short_video_stage7_material_upload(
        task_id: str = _Form(...),
        project_id: str = _Form(...),
        slot: str = _Form(...),
        file: UploadFile = File(...),
    ):
        """REQ-20261017-159：公共素材上传（cover/bg/audio，multipart）。

        落 stage6/mat_<slot>.<ext> 并写入 params.materials[slot]。
        """
        if not task_id or not project_id:
            return _err("缺少 task_id 或 project_id")
        from slirn_home import short_video_fine as _sv_fine
        import shutil as _shutil
        import tempfile as _tempfile

        suffix = Path(file.filename or "").suffix
        tmp_path: Path | None = None
        try:
            with _tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = Path(tmp.name)
                await file.seek(0)
                _shutil.copyfileobj(file.file, tmp)
            entry = _sv_fine.save_material_upload(
                repo_root, task_id, project_id, slot, tmp_path)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 material upload failed")
            return _err(f"上传失败: {e}")
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        toast = f"✅ 已上传 {entry['name']}"
        if entry.get("caches_cleared"):
            # REQ-20261019-211：换背景图 → 区域缓存已清，提示重检/重析
            toast += "；背景已更换，区域检测/AI 分析缓存已清空，请重新检测或分析"
        return _ok(slot=entry["slot"], path=entry["path"], name=entry["name"],
                   toast=toast)

    @app.app.post("/slirn/api/short_video_stage7_export_status")
    async def short_video_stage7_export_status(body: dict = Body(default_factory=dict)):
        """Stage 7 进度轮询。"""
        from slirn_home import short_video_fine as _sv_fine
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        job = _sv_fine.job_status(tid, pid)
        if job is None:
            return _ok("", job={"state": "idle", "progress": 0.0})
        import time as _t
        job["elapsed_s"] = int(
            (job.get("finished_at") or _t.time()) - job.get("started_at", _t.time())
        )
        return _ok("", job=job)

    @app.app.post("/slirn/api/short_video_stage7_save_params")
    async def short_video_stage7_save_params(body: dict = Body(default_factory=dict)):
        """保存 6 组子字段（materials / layout / font / output / audio / preview）。"""
        from slirn_home import short_video_fine as _sv_fine
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        params = body.get("params") or {}
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            res = _sv_fine.save_params(repo_root, tid, pid, params)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 save params failed")
            return _err(f"Stage 7 保存参数失败: {e}")
        return _ok("", params=res["params"], toast="💾 参数已保存")

    # ---------- REQ-20261022-145：导入内置默认参数 + 帮助文档 ----------
    @app.app.post("/slirn/api/import_default_params")
    async def import_default_params(body: dict = Body(default_factory=dict)):
        """把内置默认参数文件导入到当前任务 / 项目。

        kind=long  → 长视频：六组（layout/font/output/audio/preview/detected_region）
                     写入任务的 fine_compose.json（不动 materials 素材路径）
        kind=short → 短视频：六组写入项目参数 + config 写回项目配置
        """
        from slirn_home import global_params as _gp

        kind = (body.get("kind") or "").strip()
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if kind not in (_gp.KIND_LONG, _gp.KIND_SHORT):
            return _err("kind 必须是 long 或 short")
        defaults = _gp.load_default_params(kind)
        if not defaults:
            return _err("内置默认参数文件缺失或损坏")
        try:
            if kind == _gp.KIND_LONG:
                if not tid:
                    return _err("缺少 task_id")
                t = mgr.get(tid)
                fc = _get_fine_compose(mgr, tid)
                for key in ("layout", "font", "output", "audio", "preview"):
                    val = defaults.get(key)
                    if isinstance(val, dict):
                        fc[key] = dict(val)
                if "detected_region" in defaults:
                    fc["detected_region"] = defaults.get("detected_region")
                # materials 不动（素材是任务自己的资源）
                _save_fine_compose(mgr, tid, fc)
                return _ok(
                    applied=True, kind=kind,
                    html=_render_workbench(tid, mgr),
                    toast="已导入长视频默认参数（素材未改动）",
                )
            # kind == short
            if not tid or not pid:
                return _err("缺少 task_id 或 project_id")
            from slirn_home import short_video_fine as _sv_fine
            from slirn_home import short_video_service as _sv_service
            params = {k: v for k, v in defaults.items()
                      if k not in ("materials", "config")}
            _sv_fine.save_params(repo_root, tid, pid, params)
            cfg = defaults.get("config")
            if isinstance(cfg, dict):
                project = _sv_service.load_project(repo_root, tid, pid)
                project["config"] = _sv_service.sanitize_config(
                    {**(project.get("config") or {}), **cfg})
                _sv_service.save_project(repo_root, project)
            project = _sv_service.load_project(repo_root, tid, pid)
            return _ok(
                applied=True, kind=kind, project=project,
                html=_sv_ui.render_project(project, repo_root=repo_root),
                toast="已导入短视频默认参数",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("import default params failed")
            return _err(f"导入默认参数失败: {e}")

    @app.app.get("/slirn/static/help/{name}.html")
    async def serve_help_doc(name: str):
        """REQ-20261022-145：使用说明文档（静态 HTML，前端弹窗展示）。"""
        allowed = {"long_video", "short_video"}
        if name not in allowed:
            return JSONResponse({"ok": False, "error": "文档不存在"}, status_code=404)
        doc = Path(__file__).parent / "static" / "help" / f"{name}.html"
        if not doc.is_file():
            return JSONResponse({"ok": False, "error": "文档缺失"}, status_code=404)
        return HTMLResponse(doc.read_text(encoding="utf-8"))

    @app.app.post("/slirn/api/short_video_stage7_load_default")
    async def short_video_stage7_load_default(body: dict = Body(default_factory=dict)):
        """返回默认 params（前端首次加载用）。"""
        from slirn_home import short_video_fine as _sv_fine
        return _ok("", params=_sv_fine.load_default_params())

    # REQ-20261018-167：参数导出/导入文件（对齐长视频 export/import_fine_params）
    @app.app.post("/slirn/api/short_video_stage7_params_export")
    async def short_video_stage7_params_export(body: dict = Body(default_factory=dict)):
        """导出当前精剪参数为 JSON 文件内容（不含素材文件本体/路径）。"""
        from slirn_home import short_video_fine as _sv_fine
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            res = _sv_fine.export_params_json(repo_root, tid, pid)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 params export failed")
            return _err(f"导出参数失败: {e}")
        return _ok(filename=res["filename"], content=res["content"],
                   mime=res["mime"], toast="📤 参数已导出")

    @app.app.post("/slirn/api/short_video_stage7_params_import")
    async def short_video_stage7_params_import(body: dict = Body(default_factory=dict)):
        """从 JSON 文件内容导入参数（整组覆盖；不动素材文件路径）。"""
        from slirn_home import short_video_fine as _sv_fine
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        raw = body.get("content")
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            res = _sv_fine.import_params_json(repo_root, tid, pid, raw)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 params import failed")
            return _err(f"导入参数失败: {e}")
        cnt = len(res["applied_fields"])
        return _ok(applied_fields=res["applied_fields"], params=res["params"],
                   toast=f"✅ 已应用 {cnt} 个参数组")

    @app.app.post("/slirn/api/short_video_stage7_preview")
    async def short_video_stage7_preview(body: dict = Body(default_factory=dict)):
        """2-30s 混编预览（REQ-20261018-176：先按 bg_desc 检测区域，再走
        short_video_compose 单次 ffmpeg 合成背景/视频/字幕/头像/导师文字/BGM）。"""
        from slirn_home import short_video_fine as _sv_fine
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        idx = int(body.get("index") or 0)
        start_ms = int(body.get("start_ms") or 0)
        dur = int(body.get("duration_sec") or 5)
        if not tid or not pid or not idx:
            return _err("缺少 task_id / project_id / index")
        try:
            res = _sv_fine.preview_clip(repo_root, tid, pid, idx,
                                         start_ms=start_ms, duration_sec=dur)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 preview failed")
            return _err(f"Stage 7 预览失败: {e}")
        # REQ-20261017-162：直接回可播放的 file_url（/slirn/api/short_video_file）。
        # 前端此前自拼 /gradio_api/file= 只服务经 Gradio 上传的文件 → 磁盘产物 403 黑屏。
        _fname = Path(res["preview_path"]).name
        return _ok("", preview_path=res["preview_path"], size=res["size"],
                   file_url=_sv_ui._file_url(tid, pid, "stage6", _fname),
                   index=res["index"], toast="🎬 预览已生成")

    @app.app.post("/slirn/api/short_video_stage6_cover_export")
    async def short_video_stage6_cover_export(body: dict = Body(default_factory=dict)):
        """导出合成标题后的封面静态图（REQ-20261019-233）。

        同步 ffmpeg 单帧渲染（cover 素材 + 该条卡片标题），落
        stage6/cover_NN.png 并回 file_url 供下载。
        """
        from slirn_home import short_video_compose as _cmp
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        idx = int(body.get("index") or 0)
        if not tid or not pid or not idx:
            return _err("缺少 task_id / project_id / index")
        try:
            res = _cmp.export_cover_image_one(repo_root, tid, pid, idx)
        except Exception as e:  # noqa: BLE001
            log.exception("stage6 cover export failed")
            return _err(f"封面导出失败: {e}")
        _fname = Path(res["cover_path"]).name
        return _ok("", cover_path=res["cover_path"], size=res["size"],
                   file_url=_sv_ui._file_url(tid, pid, "stage6", _fname),
                   index=res["index"], toast="🖼 封面已导出")

    @app.app.post("/slirn/api/short_video_stage7_detect_bg")
    async def short_video_stage7_detect_bg(body: dict = Body(default_factory=dict)):
        """bg 区域检测（REQ-20261018-166 对齐长视频：算法可选 + 阈值）。

        algorithm: center_expand（默认）/ pixel；threshold 仅 pixel 用。
        检测对象 = 公共素材背景图（params.materials.bg）。
        """
        from slirn_home import short_video_bg_detect as _bg
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        algorithm = str(body.get("algorithm") or "center_expand").strip()
        try:
            threshold = int(body.get("threshold") or 250)
        except (TypeError, ValueError):
            threshold = 250
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            region = _bg.detect_bg_white_area(
                repo_root, tid, pid, algorithm=algorithm, threshold=threshold)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 detect bg failed")
            return _err(f"Stage 7 bg 检测失败: {e}")
        if region.get("algorithm") == "center_expand":
            c = region.get("detected_color") or [0, 0, 0]
            base_txt = (f"基本色 RGB({c[0]},{c[1]},{c[2]})"
                        + ("（透明/抠图区）" if region.get("transparent_base") else ""))
            toast = (f"✅ 中心扩展找到区域 {region['width']}×{region['height']}"
                     f"（{base_txt}）")
        else:
            toast = (f"🔍 像素扫描找到白色区域 {region['width']}×{region['height']}"
                     f"（{region.get('pixel_count')} 白色像素）")
        return _ok("", region=region, toast=toast)

    @app.app.post("/slirn/api/short_video_stage7_detect_bg_clear")
    async def short_video_stage7_detect_bg_clear(body: dict = Body(default_factory=dict)):
        """清空 bg 检测缓存（params.detected_region）。"""
        from slirn_home import short_video_bg_detect as _bg
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            res = _bg.clear_detected(repo_root, tid, pid)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 detect bg clear failed")
            return _err(f"清空失败: {e}")
        return _ok("", cleared=res["cleared"], toast="🗑 已清空检测缓存")

    @app.app.post("/slirn/api/short_video_stage7_apply_detected")
    async def short_video_stage7_apply_detected(body: dict = Body(default_factory=dict)):
        """REQ-20261018-172：按区域类型填充到参数区（AI regions 优先，像素检测兜底）。"""
        from slirn_home import short_video_bg_detect as _bg
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            res = _bg.apply_detected_to_layout(repo_root, tid, pid)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 apply detected failed")
            return _err(f"Stage 7 填充失败: {e}")
        # REQ-20261018-173：新组进 toast（导师文字 / 导师头像）
        labels = {"video": "视频", "subtitle": "字幕", "bg": "背景", "cover": "封面",
                  "mentor_text": "导师文字", "avatar": "导师头像"}
        names = "、".join(labels.get(k, k) for k in res["filled"])
        return _ok("", layout=res["layout"], filled=res["filled"],
                   source=res["source"],
                   toast=f"✅ 已填充到参数区域（{names}）")

    @app.app.post("/slirn/api/short_video_stage7_parse_layout")
    async def short_video_stage7_parse_layout(body: dict = Body(default_factory=dict)):
        """分析参数（REQ-20261018-170 v2：说明 → 生成提示词 → LLM 识别区域 → 结果落盘展示）。"""
        from slirn_home import short_video_ai_layout as _ai
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        bg = str(body.get("bg_path") or "")
        style = str(body.get("style_hint") or "现代简洁")
        desc = str(body.get("bg_desc") or "").strip()
        if not tid or not pid:
            return _err("缺少 task_id 或 project_id")
        try:
            out = _ai.run_ai_layout(repo_root, tid, pid,
                                    style_hint=style, bg_path=bg, bg_desc=desc)
        except Exception as e:  # noqa: BLE001
            log.exception("stage7 parse layout failed")
            return _err(f"Stage 7 AI 布局失败: {e}")
        n = len(out["result"]["regions"])
        src = out["result"].get("source") or "llm"
        toast = (f"🤖 AI 识别出 {n} 个区域，布局已应用"
                 if src == "llm" else "⚠️ LLM 不可用，已应用默认布局")
        return _ok("", layout=out["layout"], result=out["result"], toast=toast)

    # ---- BGM 选择（复用长视频 _get_default_bgms 助手） ----
    @app.app.post("/slirn/api/short_video_stage7_list_bgm")
    async def short_video_stage7_list_bgm(body: dict = Body(default_factory=dict)):
        """列出系统默认 BGM（短剧项目级）。"""
        try:
            bgms = _get_default_bgms()  # noqa: F821 — app.py 模块级
        except NameError:
            bgms = []
        return _ok(bgms=bgms)

    @app.app.post("/slirn/api/short_video_stage7_select_bgm")
    async def short_video_stage7_select_bgm(body: dict = Body(default_factory=dict)):
        """选系统默认 BGM → 写入 stage6_fine.params.audio.bgm_path。"""
        from slirn_home import short_video_fine as _sv_fine
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        bgm_id = (body.get("bgm_id") or "").strip()
        if not tid or not pid or not bgm_id:
            return _err("缺少 task_id / project_id / bgm_id")
        # 复用长视频 _DEFAULT_BGMS 列表
        bgm = next((b for b in _DEFAULT_BGMS  # noqa: F821
                    if b.get("id") == bgm_id), None)
        if not bgm:
            return _err(f"未知的 bgm_id: {bgm_id}")
        src = _DEFAULT_BGMS_DIR / bgm["filename"]  # noqa: F821
        if not src.exists():
            return _err(f"系统默认 BGM 文件不存在: {src}")
        # 写入 params.audio
        try:
            root = repo_root
            project = _sv_fine.svc.load_project(root, tid, pid)
            params = _sv_fine._get_params(project)
            params["audio"]["enabled"] = True
            params["audio"]["bgm_path"] = str(src)
            s = _sv_fine.svc.get_stage_state(project, "stage6_fine")
            s["params"] = params
            _sv_fine.svc.set_stage_state(project, "stage6_fine", s)
            _sv_fine.svc.save_project(root, project)
        except Exception as e:  # noqa: BLE001
            log.exception("select_bgm save params failed")
            return _err(f"选 BGM 失败: {e}")
        return _ok("", bgm_path=str(src), toast=f"🎵 已选 BGM: {bgm.get('name', bgm_id)}")

    # ---- 模板管理 6 端点 ----
    @app.app.post("/slirn/api/short_video_stage7_profiles_list")
    async def short_video_stage7_profiles_list(body: dict = Body(default_factory=dict)):
        from slirn_home import short_video_fine as _sv_fine
        return _ok(profiles=_sv_fine.list_profiles(repo_root))

    @app.app.post("/slirn/api/short_video_stage7_profiles_save")
    async def short_video_stage7_profiles_save(body: dict = Body(default_factory=dict)):
        from slirn_home import short_video_fine as _sv_fine
        name = str(body.get("name") or "").strip()
        params = body.get("params") or {}
        pid = str(body.get("profile_id") or "").strip() or None
        if not name:
            return _err("缺少 name")
        try:
            res = _sv_fine.save_profile(repo_root, name=name, params=params, profile_id=pid)
        except Exception as e:  # noqa: BLE001
            log.exception("profiles save failed")
            return _err(f"保存模板失败: {e}")
        return _ok("", profile=res, toast="💾 模板已保存")

    @app.app.post("/slirn/api/short_video_stage7_profiles_apply")
    async def short_video_stage7_profiles_apply(body: dict = Body(default_factory=dict)):
        from slirn_home import short_video_fine as _sv_fine
        tid = (body.get("task_id") or "").strip()
        pid = (body.get("project_id") or "").strip()
        prof_id = str(body.get("profile_id") or "").strip()
        if not tid or not pid or not prof_id:
            return _err("缺少 task_id / project_id / profile_id")
        try:
            res = _sv_fine.apply_profile(repo_root, tid, pid, prof_id)
        except Exception as e:  # noqa: BLE001
            log.exception("profiles apply failed")
            return _err(f"应用模板失败: {e}")
        return _ok("", params=res["params"], toast="📋 模板已应用")

    @app.app.post("/slirn/api/short_video_stage7_profiles_delete")
    async def short_video_stage7_profiles_delete(body: dict = Body(default_factory=dict)):
        from slirn_home import short_video_fine as _sv_fine
        prof_id = str(body.get("profile_id") or "").strip()
        if not prof_id:
            return _err("缺少 profile_id")
        ok = _sv_fine.delete_profile(repo_root, prof_id)
        return _ok("", deleted=ok, toast="🗑️ 已删除" if ok else "❌ 不存在")

    @app.app.post("/slirn/api/short_video_stage7_profiles_rename")
    async def short_video_stage7_profiles_rename(body: dict = Body(default_factory=dict)):
        from slirn_home import short_video_fine as _sv_fine
        prof_id = str(body.get("profile_id") or "").strip()
        new_name = str(body.get("new_name") or "").strip()
        if not prof_id or not new_name:
            return _err("缺少 profile_id / new_name")
        res = _sv_fine.rename_profile(repo_root, prof_id, new_name)
        if not res:
            return _err("模板不存在")
        return _ok("", profile=res, toast="✏️ 已重命名")

    @app.app.post("/slirn/api/short_video_stage7_profiles_export")
    async def short_video_stage7_profiles_export(body: dict = Body(default_factory=dict)):
        from slirn_home import short_video_fine as _sv_fine
        prof_id = str(body.get("profile_id") or "").strip()
        if not prof_id:
            return _err("缺少 profile_id")
        res = _sv_fine.export_profile(repo_root, prof_id)
        if not res:
            return _err("模板不存在")
        return _ok("", profile=res, toast="📤 已导出")

    # REQ-20261016-148：Stage 6 字幕优化阶段整体移除 — 以下 3 端点随之删除：
    # - /short_video_stage5_run（提取短视频字幕 ASR，run_stage5_one）
    # - /short_video_stage5_save（逐行编辑器自动保存，save_stage6_refined）
    # - /short_video_stage6_preview_segments（按编辑后 SRT 拼接预览，REQ-142）
    # 精剪字幕改经 short_video_fine._resolve_subtitle_path 的文件回退链
    # （stage5/refined*.srt → stage4/coarse_NN.srt）直接读取。

    @app.app.get("/slirn/api/short_video_file")
    async def short_video_file(task_id: str, project_id: str, kind: str, name: str):
        from fastapi.responses import FileResponse

        if kind == "material":
            # REQ-20261001-096：按素材 id 取文件 —— 上传素材与任务自动导入的
            # 视频都能取到（asset 只覆盖上传目录，自动导入的源视频不在其中）
            try:
                project = _sv_service.load_project(repo_root, task_id, project_id)
                target = _sv_service.material_abs_path(repo_root, project, name).resolve()
            except Exception as e:  # noqa: BLE001
                return _err(f"读取素材失败: {e}")
            tasks_root = (Path(repo_root) / "tasks").resolve()
            if not target.is_relative_to(tasks_root):
                return _err("非法文件路径")
        elif kind == "stage6":
            # REQ-20261003-098：Stage 6 产物（final mp4 / continuous srt）
            safe_name = Path(name or "").name
            if not safe_name or safe_name != name:
                return _err("非法文件名")
            target = (_sv_service.stage6_dir(repo_root, task_id, project_id) / safe_name).resolve()
            stage_root = _sv_service.stage6_dir(repo_root, task_id, project_id).resolve()
            if not target.is_relative_to(stage_root):
                return _err("非法文件路径")
        elif kind in ("stage2", "stage3", "stage4", "stage5"):
            # REQ-20261003-098：Stage 2/3/4/5 产物（raw.srt / highlights.json / coarse_NN.* / refined_NN.*）
            safe_name = Path(name or "").name
            if not safe_name or safe_name != name:
                return _err("非法文件名")
            stage_dir_fn = {
                "stage2": _sv_service.stage2_dir,
                "stage3": _sv_service.stage3_dir,
                "stage4": _sv_service.stage4_dir,
                "stage5": _sv_service.stage5_dir,
            }[kind]
            stage_root = stage_dir_fn(repo_root, task_id, project_id).resolve()
            target = (stage_root / safe_name).resolve()
            if not target.is_relative_to(stage_root):
                return _err("非法文件路径")
        else:
            safe_name = Path(name or "").name
            if not safe_name or safe_name != name:
                return _err("非法文件名")
            if kind == "asset":
                base = _sv_service.assets_dir(repo_root, task_id, project_id)
            elif kind == "output":
                base = _sv_service.output_dir(repo_root, task_id, project_id)
            else:
                return _err("非法文件类型")
            target = (base / safe_name).resolve()
            if not target.is_relative_to(base.resolve()):
                return _err("非法文件路径")
        if not target.exists() or not target.is_file():
            return _err(f"文件不存在: {name}")
        # REQ-20261003-098：srt 用 text/plain 让浏览器可预览（<video>/<audio> 不受影响）
        if target.suffix.lower() == ".srt":
            return FileResponse(target, media_type="text/plain; charset=utf-8")
        return FileResponse(target)

    @app.app.post("/slirn/api/pipeline_run")
    async def pipeline_run(body: dict = Body(default_factory=dict)):
        """启动后台自动执行（守护线程）；已有 running job → False。

        REQ-20260921-NNN：启动前先做 fine_cut 预检 —— 若配置会跑到精剪合成
        阶段且 cfg.fine_cut.enabled=True，则检查素材是否上传、参数是否就绪。
        失败 → 直接返回错误（不启动守护线程），让前端 toast + 提示用户去
        第 6 阶段详情页补齐。
        """
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        since = body.get("since") or None
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        # REQ-20260921-NNN：fine_cut 预检 —— 只在「会跑到精剪合成」时才严格检查
        # （since 跳过 fine_cut 之后的阶段；或 stop_after < fine_cut；或 run_mode != to_end
        # 且 stop_after 是更早阶段；都视为不会跑到精剪合成）。
        try:
            from slirn_home.pipeline_service import (
                STAGE_ORDER as _STAGE_ORDER,
                STAGE_INDEX as _STAGE_INDEX,
                fine_cut_preflight as _fine_cut_preflight,
            )
            _cfg_data = pipeline_service.load_pipeline(outputs_dir) or {}
            _cfg_full = _cfg_data.get("config") or {}
            _since_idx = _STAGE_INDEX.get(since, -1) if since else -1
            _fine_cut_idx = _STAGE_INDEX.get("fine_cut", 5)
            _will_run_fine_cut = _since_idx <= _fine_cut_idx
            # 进一步：run_mode=stop_after 且 stop_after < fine_cut_idx → 不会跑到精剪
            if _will_run_fine_cut and _cfg_full.get("run_mode") != "to_end":
                _stop_after = _cfg_full.get("stop_after")
                if _stop_after:
                    _stop_idx = _STAGE_INDEX.get(_stop_after, -1)
                    if 0 <= _stop_idx < _fine_cut_idx:
                        _will_run_fine_cut = False
            if _will_run_fine_cut:
                _pre = _fine_cut_preflight(tid, _cfg_full, outputs_dir)
                if not _pre.get("ok"):
                    # REQ-20260921-NNN：ok=True 让前端走 r.preflight 分支显示详细
                    # 错误（缺哪些素材/参数原因）；started=False 不启动守护线程。
                    # 早期版本 ok=False → 前端拿到 r.ok=false 走「未知错误」分支
                    # （preflight 详情被吞掉，用户只看到 toast「未知错误」）。
                    return _ok("", started=False,
                               preflight=_pre,
                               toast=_pre.get("reason")
                                     or "精剪合成预检失败")
        except Exception as _e:  # noqa: BLE001 — 预检异常不该阻塞
            log.warning("[pipeline_run] 预检异常（继续启动）：%s", _e)
        # base url：调度器走 in-process HTTP client（同进程同端口）。
        # 包含 /slirn/api 前缀 — pipeline_service._http_post(api, "/gen_subtitle")
        # 直接拼接 base + path，所以 base 必须含 API 前缀，否则 404。
        # REQ-20260922-NNN-b：端口取 Blocks 的 server_port — launch() 解析出的
        # 实际端口（7861 被占用时 Gradio 自动 +1 换端口）。早期版本读
        # app.app.port：app.app 是 FastAPI 对象，永远没有该属性 → 恒回退 7861，
        # 服务实际跑在别的端口时流程所有阶段报「连接失败: [WinError 10061]」。
        base_url = _os.environ.get("SLIRN_API_BASE") or ""
        if not base_url:
            port = getattr(app, "server_port", None) or 7861
            base_url = f"http://127.0.0.1:{port}/slirn/api"
        # REQ-20261021-139 / D4：把发起者的 slirn_session 透传给后台流水线 ——
        # 内部 HTTP 调用要靠它过鉴权中间件（X-Slirn-Auto 不是旁路）。
        # 在此处（请求线程内）读取，contextvar 有效。
        try:
            _session_token = _get_ctx_token() or ""
        except Exception:  # noqa: BLE001 — 取不到就退化为「无会话」（旁路模式）
            _session_token = ""
        started = pipeline_service.run_pipeline(
            tid, base_url, outputs_dir, since=since, session_token=_session_token)
        if not started:
            return _ok("", started=False,
                       toast="⏳ 流程已在运行 — 等待完成或先点 ⏹ 停止")
        return _ok("", started=True, toast="▶ 流程已启动（后台运行，可在状态条查看进度）")

    @app.app.post("/slirn/api/pipeline_status")
    async def pipeline_status_endpoint(body: dict = Body(default_factory=dict)):
        """读取当前 job 状态：state / current_stage / percent / log / history。"""
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        status = pipeline_service.pipeline_status(tid)
        # 同步补一次 history（落盘数据，状态条初始显示用）
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        disk_data = pipeline_service.load_pipeline(outputs_dir)
        disk_history = (disk_data or {}).get("history") or []
        if status is None:
            # 从未跑过 → state=idle
            return _ok("", state="idle", current_stage=None, percent=0.0,
                       log=[], error=None, history=disk_history)
        return _ok("", **status, history=status.get("history") or disk_history)

    @app.app.post("/slirn/api/pipeline_stop")
    async def pipeline_stop(body: dict = Body(default_factory=dict)):
        """请求停止（设置标志位，下次循环检查时退出）。无 running job → False。"""
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        stopped = pipeline_service.stop_pipeline(tid)
        if not stopped:
            return _ok("", stopped=False, toast="没有正在运行的流程")
        return _ok("", stopped=True, toast="⏹ 已请求停止（当前阶段完成后退出）")

    @app.app.post("/slirn/api/pipeline_reset_stages")
    async def pipeline_reset_stages(body: dict = Body(default_factory=dict)):
        """清理所有阶段生成的产物（REQ-20260921-NNN）。

        删除范围：outputs/ 下 subtitle.json / revision.json /
        rev_speaker_link.json / cutlist.json / cut_speaker_link.json /
        rough_compose.mp4 / optimize_subtitle.json / optimize_compose.mp4（+
        .srt / optimize_cut.json，REQ-20260922-NNN 标记删除行产物）/
        fine_revision.json（旧流程）/ fine_export*.mp4 / fine_preview*.mp4 +
        任务根目录下 fine_compose.json（精剪参数）。
        保留：原视频 / 时间截取 / 热词 / 任务 metadata / materials/。
        清理后 t.status 降回 DRAFT（让工作台顶部绿色对号也清空）。

        前端应做两次确认（按钮旁有 warning 文案 + onClick 内 confirm 弹窗）。
        """
        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        if t is None:
            return _err("任务不存在")

        outputs_dir = mgr.tasks_dir / tid / "outputs"
        task_dir = mgr.tasks_dir / tid  # 任务根目录（含 fine_compose.json 等非 outputs/ 产物）
        materials_dir = mgr.tasks_dir / tid / "materials"

        # REQ-20260921-NNN：要清理的产物（按文件名精确匹配；其他文件不动）
        # 文件名必须与各 service 的常量对齐：
        #   - asr_service.SUBTITLE_JSON       = "subtitle.json"
        #   - revision_service.REVISION_JSON  = "revision.json"
        #   - cutlist_service.CUTLIST_JSON    = "cutlist.json"
        #   - compose_service.ROUGH_COMPOSE_NAME = "rough_compose.mp4"
        #   - optimize_service.OPTIMIZE_JSON  = "optimize_subtitle.json"（不是 opt_subtitle.json）
        #   - fine_service.FINE_JSON          = "fine_revision.json"（旧流程，兼容删除）
        #   - app._save_fine_compose          → 任务根目录下的 fine_compose.json（不在 outputs/）
        # - 字幕生成：subtitle.json
        # - 字幕修订：revision.json + rev_speaker_link.json
        # - 切分修剪：cutlist.json + cut_speaker_link.json
        # - 粗剪合成：rough_compose.mp4（及相关 .json 标记）
        # - 优化字幕：optimize_subtitle.json + 旧流程 fine_revision.json
        # - 精剪合成：fine_compose.json + fine_export*.mp4 + fine_preview*.mp4
        files_to_delete: list[Path] = []
        if outputs_dir.exists():
            file_globs = [
                "subtitle.json",
                "revision.json", "rev_speaker_link.json",
                "cutlist.json", "cut_speaker_link.json",
                "rough_compose.mp4", "rough_compose.json",
                # 优化字幕（REQ-20260917-030 新流程 + REQ-20260917-029 旧流程）
                "optimize_subtitle.json",  # 新流程实际文件名
                # REQ-20260922-NNN 标记删除行：优化成片三件产物 + tmp 半成品
                "optimize_compose.mp4", "optimize_compose.srt",
                "optimize_cut.json", "optimize_compose.tmp.mp4",
                "fine_revision.json",       # 旧流程（fine_service 写），兼容删除
                # fine_export 全篇 + 区间导出（REQ-20260919-061）
                "fine_export.mp4",
                "fine_preview.mp4",
                # 区间导出后缀：fine_export_t{start}_d{dur}.mp4（见 /export_fine_video 6718-6721）
            ]
            for name in file_globs:
                p = outputs_dir / name
                if p.exists():
                    files_to_delete.append(p)
            # glob 区间导出文件
            for p in outputs_dir.glob("fine_export_t*.mp4"):
                files_to_delete.append(p)
            for p in outputs_dir.glob("fine_preview_t*.mp4"):
                files_to_delete.append(p)
        # 精剪合成配置：fine_compose.json 在任务根目录（不在 outputs/）
        # 路径约定见 app.py:3439 _save_fine_compose → mgr.tasks_dir/{tid}/fine_compose.json
        fc_root = task_dir / "fine_compose.json"
        if fc_root.exists():
            files_to_delete.append(fc_root)

        deleted: list[str] = []
        skipped: list[str] = []
        for p in files_to_delete:
            try:
                p.unlink()
                deleted.append(p.name)
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{p.name}（{e}）")

        # 顺手清 pipeline_service 的内存状态（in-memory stages_done + status）
        try:
            pipeline_service.clear_pipeline_state(tid,
                history_path=outputs_dir / "execution_history.json")
        except Exception:
            pass

        # REQ-20260921-NNN：把任务状态回到 DRAFT — 否则 _wb_stage_states() 会
        # 因为 t.status=FINE_CUT_DONE 让所有阶段仍然显示绿色对号（cur_rank>=
        # rank[status_name] 为真）。降到 DRAFT 后：assets 看磁盘（原视频还在
        # 就 done），其他 5 阶段产物已删 + rank 不足 → 全部回到 pending。
        try:
            from tasklib.models import TaskStatus as _TS
            if getattr(t.status, "name", str(t.status)) != _TS.DRAFT.name:
                mgr.update_status(tid, _TS.DRAFT)
                log.info("REQ-20260921-NNN：清理后把状态 %s → DRAFT",
                         getattr(t.status, "name", t.status))
        except Exception as e:  # noqa: BLE001
            log.warning("REQ-20260921-NNN：清理后状态回退失败（不影响主流程）：%s", e)

        return _ok("", deleted=deleted, skipped=skipped,
                   toast=f"🧹 已清理 {len(deleted)} 个阶段产物"
                         + (f"（{len(skipped)} 跳过）" if skipped else ""))

    @app.app.post("/slirn/api/optimize_subtitle")
    async def optimize_subtitle(request: Request, body: dict = Body(default_factory=dict)):
        """启动优化字幕（REQ-20260917-030）：ASR 重识别粗剪成片 → 大模型提取不明确字词。

        已有优化结果时须 force（前端二次确认）。热词为空不阻塞（提示级）。
        """
        # REQ-20260919-075：auto 透传
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        from slirn_home import compose_service, llm_config, optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            t = mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        artifact = compose_service.rough_compose_path(outputs_dir)
        if not artifact.exists():
            return _err("缺少粗剪成片 — 请先在「粗剪合成」阶段合成")
        if optimize_service.load_optimize(outputs_dir) and not body.get("force"):
            return _err("已存在优化结果 — 重新优化将覆盖并重置全部采纳决定，请确认后重试")
        # REQ-20260922-NNN 标记删除行：重优化会重置全部决定（含标记）→
        # 旧优化成片立即失效，force 启动前先清产物（精剪自动回退 rough）
        if body.get("force"):
            compose_service._delete_cut_artifacts(outputs_dir)
        hotwords: list[str] = []
        try:
            if t.hotwords_path.exists():
                hotwords = [w for w in t.hotwords_path.read_text(encoding="utf-8").split() if w]
        except Exception:  # noqa: BLE001
            pass
        entry = llm_config.get_current_entry(repo_root)
        if entry is None:
            return _err("未注册任何大模型 — 请先点顶栏「大模型设置」 添加模型")
        started = optimize_service.start_job(
            tid, artifact, hotwords, t.name, outputs_dir, entry=entry,
            auto=_auto,
        )
        if not started:
            return _ok("", toast="⏳ 该任务已在优化中，请等待完成")
        return _ok("", toast="✨ 优化字幕已开始（直读随片字幕 → 断句整理 → 大模型分析，可离开本页）",
                   job={"state": "running", "stage": "准备字幕行集"})

    @app.app.post("/slirn/api/optimize_subtitle_status")
    async def optimize_subtitle_status(body: dict = Body(default_factory=dict)):
        """优化字幕进度轮询（REQ-20260917-030）。"""
        from slirn_home import optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        j = optimize_service.job_status(tid)
        if j is None:  # 服务重启后 job 丢失 — 产物在即视为完成
            data = optimize_service.load_optimize(mgr.tasks_dir / tid / "outputs")
            if data:
                st = (data.get("stats") or {})
                return _ok("", job={
                    "state": "done",
                    "lines": st.get("lines", 0),
                    "occurrences": st.get("occurrences", 0),
                })
            return _ok("", job={"state": "idle"})
        import time as _time
        j["elapsed_s"] = int((j.get("finished_at") or _time.time()) - j.get("started_at", _time.time()))
        return _ok("", job=j)

    @app.app.post("/slirn/api/save_optimize_subtitle")
    async def save_optimize_subtitle(request: Request, body: dict = Body(default_factory=dict)):
        """保存优化字幕人工替换决定（REQ-20260917-030）：决定合并落盘 + 阶段完成。

        decisions：[{occ_id, applied, after}] 全量口径 — 未列出的出现项一律不
        采纳；after 可为人工编辑值（生效要求非空且 ≠ 原文）。幂等推进
        FINE_SUBTITLE_REVIEWED。

        REQ-20260919-075：「确认保存」动作也写执行日志（之前只有大模型分析记录）。
        """
        # REQ-20260919-075：auto 透传；确认保存写日志
        _auto = request.headers.get("X-Slirn-Auto") == "1"
        # REQ-20260920-081：session 透传
        _auto_session_id = request.headers.get("X-Slirn-Auto-Session") or ""
        from tasklib.models import TaskStatus

        from slirn_home import execution_history, optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        decisions = body.get("decisions")
        if not isinstance(decisions, list):
            return _err("decisions 必须是数组")
        # REQ-20260922-NNN 整句替换：可选 line_edits [{seg, text}]（全量口径，
        # 不传 = 清空；非法项服务端丢弃）
        line_edits = body.get("line_edits")
        if line_edits is not None and not isinstance(line_edits, list):
            return _err("line_edits 必须是数组")
        # REQ-20260922-NNN 标记删除行：可选 line_marks [seg id]（全量口径，
        # 不传 = 清空）；标记行会在保存后从粗剪成片剪除出优化成片
        line_marks = body.get("line_marks")
        if line_marks is not None and not isinstance(line_marks, list):
            return _err("line_marks 必须是数组")
        # REQ-20260923-NNN 切分子段：可选 split_marks {"<seg>": [索引…]}（全量
        # 口径，不传 = 清空）。前端 ✓/✕/🗑️ 翻转即 optAutoSave 全量快照提交 —
        # 这里漏传的话服务端语义 = 每次自动保存都把子段翻转清空（打勾打叉
        # 后「改了存不住」的根源）。
        split_marks = body.get("split_marks")
        if split_marks is not None and not isinstance(split_marks, dict):
            return _err("split_marks 必须是对象")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        # REQ-20260919-075：开始记录 — 确认保存
        exec_id = execution_history.record_start(
            outputs_dir, execution_history.KIND_OPTIMIZE,
            extra={"decisions_count": len(decisions)},
            auto=_auto,
            auto_session_id=_auto_session_id,
        )
        try:
            data, applied_n = optimize_service.save_decisions(
                outputs_dir, decisions, line_edits, line_marks, split_marks)
        except Exception as e:  # noqa: BLE001
            execution_history.record_finish(outputs_dir, exec_id, success=False, error=str(e))
            return _err(f"保存失败: {e}")
        est = optimize_service.effective_stats(data)
        mgr.update_status(tid, TaskStatus.FINE_SUBTITLE_REVIEWED)  # 幂等：结果在盘即完成

        # REQ-20260922-NNN 标记删除行（手动触发语义）：保存只落盘标记，**不触发
        # 剪辑** — 剪辑由「🎬 重新拼接视频」按钮显式触发（/optimize_cut_rekick）。
        # 这里只做状态同步：在跑 → running（线程 R1 复核自清），剪辑计划空 → 清产物
        cut_state = "none"
        plan_now = optimize_service.cut_plan(data)
        marks_now = data.get("line_marks") or []
        from slirn_home import compose_service

        job_st = compose_service.opt_cut_job_status(tid)
        if job_st and job_st.get("state") == "running":
            cut_state = "running"  # 🎬 按钮触发的剪辑在跑，取消标记由线程 R1 复核自清
        elif not optimize_service.plan_has_deletions(plan_now):
            # 计划已空（标记/删除子段全部取消）且无剪辑在跑 → 清掉旧优化成片（同步、毫秒级）
            removed = compose_service._delete_cut_artifacts(outputs_dir)
            cut_state = "cleared" if removed else "none"

        n_edits = int(est.get("line_edits") or 0)
        edit_suffix = f" · 整句替换 {n_edits} 行" if n_edits else ""
        n_marks = int(est.get("line_marks") or 0)
        mark_suffix = f" · 标记删除 {n_marks} 行" if n_marks else ""
        # REQ-20260919-075：完成时回填具体执行情况
        execution_history.patch_fields(outputs_dir, exec_id, {
            "description": (f"优化字幕保存：{len(decisions)} 条人工决定，生效 {applied_n} 处"
                            f"（未采纳 {est.get('skipped', 0)} 处）{edit_suffix}{mark_suffix}")
        })
        execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
        return _ok("", toast=(f"✅ 已保存替换对应关系：生效 {applied_n} 处"
                              f"（未采纳 {est['skipped']} 处）{edit_suffix}{mark_suffix}"),
                   stats=est, cut={"state": cut_state, "marks": marks_now})

    @app.app.post("/slirn/api/optimize_resplit")
    async def optimize_resplit(body: dict = Body(default_factory=dict)):
        """优化字幕行内切分（REQ-20260923-NNN 二次切分 + 重新拼接）。

        {task_id, seg_id, target_text} — 把一行按目标文字切成多子段，立即写盘
        （镜像切分修剪 /resplit_segment 的即时语义）：默认删除子段 = 目标未命中
        的原文 token 洞；重切重置该行子段标记、清除整行标记。毫秒级无 job；
        剪辑产物失效由 /optimize_cut_rekick 的 R1 复核自清（见 compose_service）。
        """
        from slirn_home import optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        try:
            seg_id = int(body.get("seg_id"))
        except (TypeError, ValueError):
            return _err("seg_id 必须是整数")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        res, err = optimize_service.resplit_line(outputs_dir, seg_id,
                                                 str(body.get("target_text") or ""))
        if err:
            return _err(err)
        est = optimize_service.effective_stats(optimize_service.load_optimize(outputs_dir) or {})
        fb = "（⚠️ 无字级时间戳，按文本占比估算）" if res.get("fallback") else ""
        # 2026-09-29 用户反馈：只刷新当前行组（父行 + 新子段行），不再整页刷新
        row_html = _extract_opt_rows(_render_optimize_zone(tid, mgr.get(tid), mgr), seg_id)
        return _ok("", toast=(f"✂️ 第 {seg_id} 条已切成 {len(res['subs'])} 段"
                              f"（默认删除 {res['deleted_default']} 段）{fb}"),
                   resplit=res, stats=est, row_html=row_html)

    @app.app.post("/slirn/api/optimize_unsplit")
    async def optimize_unsplit(body: dict = Body(default_factory=dict)):
        """取消一行的行内切分（REQ-20260923-NNN）：恢复整行，毫秒级。"""
        from slirn_home import optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        try:
            seg_id = int(body.get("seg_id"))
        except (TypeError, ValueError):
            return _err("seg_id 必须是整数")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        res, err = optimize_service.unsplit_line(outputs_dir, seg_id)
        if err:
            return _err(err)
        est = optimize_service.effective_stats(optimize_service.load_optimize(outputs_dir) or {})
        # 2026-09-29 用户反馈：与切分同款 — 只刷新当前行组，不再整页刷新
        row_html = _extract_opt_rows(_render_optimize_zone(tid, mgr.get(tid), mgr), seg_id)
        return _ok("", toast=f"↩️ 第 {seg_id} 条已恢复整行", unsplit=res, stats=est,
                   row_html=row_html)

    @app.app.post("/slirn/api/opt_batch_search")
    async def opt_batch_search(body: dict = Body(default_factory=dict)):
        """批量替换 · 查找预览（REQ-20260929-NNN）：{task_id, word} → 命中行列表。

        每个命中行带前两行 + 本行 + 后两行共 5 行上下文（按 segments 数组序取
        连续 5 行，边界不足自动截短），让用户替换前看清语境。整句替换 / 已切分 /
        已标记删除的命中行只展示并给 block 原因（注入会被整句覆盖 / 破坏子段
        对齐 / 行即将剪除）；与现有生效替换重叠的命中位置标 skipped_overlap。
        纯读不写盘。"""
        from slirn_home import optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        res, err = optimize_service.batch_search(
            mgr.tasks_dir / tid / "outputs", str(body.get("word") or ""))
        if err:
            return _err(err)
        return _ok("", search=res)

    @app.app.post("/slirn/api/opt_batch_replace")
    async def opt_batch_replace(body: dict = Body(default_factory=dict)):
        """批量替换 · 确认（REQ-20260929-NNN）：{task_id, word, replacement}。

        每个可注入位置注入出现项（reason=批量替换，已采纳已处理 — 行 chip /
        词频 / SRT / 后续逐处改判沿用现成管线），立即写盘（与 resplit 同语义，
        不依赖「确认保存」；saved_at 阶段确认标记不动）。返回受影响行组的
        row_html 映射（前端 optSwapRows 逐行局部刷新）+ 词频面板 HTML（新词
        chip 可点选筛选）。服务端重新匹配校验 — 查找与确认之间状态变了也安全。"""
        from slirn_home import optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        res, err = optimize_service.batch_replace(
            outputs_dir, str(body.get("word") or ""),
            str(body.get("replacement") or ""))
        if err:
            return _err(err)
        zone = _render_optimize_zone(tid, mgr.get(tid), mgr)
        rows = {str(seg): _extract_opt_rows(zone, seg) for seg in res["segs"]}
        data = optimize_service.load_optimize(outputs_dir) or {}
        words_html, word_filter_html = _opt_words_panel(data)
        est = optimize_service.effective_stats(data)
        return _ok("", toast=(f"✅ 批量替换完成：{res['lines']} 行 {res['replaced']} 处"
                              f"「{res['word']}」→「{res['replacement']}」"),
                   replace=res, rows=rows, stats=est,
                   words={"list": words_html, "filters": word_filter_html})

    @app.app.post("/slirn/api/optimize_resplice_subs")
    async def optimize_resplice_subs(body: dict = Body(default_factory=dict)):
        """重新拼接字幕（REQ-20260923-NNN 优化字幕页「🔄 重新拼接字幕」）。

        按当前剪辑计划（🗑️ 整行标记 + 切分行删除子段）即时重算
        optimize_compose.srt（删除内容剔除 + 时间轴前移），**不编码视频** —
        字幕先行，视频拼接由「🎬 重新拼接视频」显式触发。剪辑在跑时拒绝
        （完成后 SRT 随片自动生成，同口径）；剪辑计划空 → 报错（清理语义
        归保存钩子 / rekick）。
        """
        from slirn_home import compose_service, optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        data = optimize_service.load_optimize(outputs_dir)
        if data is None:
            return _err("尚无优化字幕数据 — 请先执行「开始优化字幕」")
        job_st = compose_service.opt_cut_job_status(tid)
        if job_st and job_st.get("state") == "running":
            return _err("视频重新拼接正在进行中 — 完成后会自动生成同步字幕，无需手动拼接")
        plan = optimize_service.cut_plan(data)
        if not optimize_service.plan_has_deletions(plan):
            return _err("当前没有要剪除的内容 — 先点 🗑️ 标记整行，或 ✂️ 切分后删除子段")
        marks = data.get("line_marks") or []
        segs = data.get("segments") or []
        del_ms = optimize_service.deleted_intervals_ms(
            segs, marks, data.get("line_splits"), data.get("split_marks"))
        srt = optimize_service.build_srt(
            segs, marks, shift=True,
            line_splits=data.get("line_splits"),
            split_marks=data.get("split_marks"),
            occurrences=data.get("occurrences"))
        (outputs_dir / "optimize_compose.srt").write_text(srt, encoding="utf-8")
        n_entries = len(optimize_service.srt_entries(
            segs, marks, line_splits=data.get("line_splits"),
            split_marks=data.get("split_marks")))
        n_marks = len(plan["marks"])
        n_subs = sum(len(v) for v in plan["split_marks"].values())
        deleted_sec = round(sum(e - s for s, e in del_ms) / 1000.0, 1)
        return _ok("", toast=(f"🔄 字幕已重新拼接：{n_entries} 条（剪除 {n_marks} 行"
                              f"+{n_subs} 子段 · 共 {deleted_sec}s）— "
                              f"视频请点「🎬 重新拼接视频」"),
                   resplice={"entries": n_entries, "marks": n_marks,
                             "split_subs": n_subs, "deleted_sec": deleted_sec})

    @app.app.post("/slirn/api/optimized_srt")
    async def optimized_srt(body: dict = Body(default_factory=dict)):
        """下载优化后成片字幕 SRT（REQ-20260917-030）。

        REQ-20260922-NNN 标记删除行：base="cut" → 优化成片时间基（排除标记行
        + 时间轴前移，配合 optimize_compose.mp4）；默认 "rough" → 粗剪成片时间基
        （排除标记行但不平移）。两者都排除标记删除行 — 被删的行不该出现在字幕里。
        """
        from slirn_home import optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        base = body.get("base") or "rough"
        if base not in ("rough", "cut"):
            return _err("base 只支持 rough / cut")
        data = optimize_service.load_optimize(mgr.tasks_dir / tid / "outputs")
        if data is None:
            return _err("还没有优化结果 — 请先执行「开始优化字幕」")
        if not data.get("saved_at"):
            return _err("尚未确认保存 — 请先「确认替换并保存」")
        segments = data.get("segments") or []
        marks = data.get("line_marks") or []
        srt = optimize_service.build_srt(
            segments, marks, shift=(base == "cut"),
            line_splits=data.get("line_splits"),
            split_marks=data.get("split_marks"),
            occurrences=data.get("occurrences"))
        return _ok("", srt=srt, lines=len(segments), base=base)

    @app.app.post("/slirn/api/optimize_cut_status")
    async def optimize_cut_status(body: dict = Body(default_factory=dict)):
        """优化成片剪辑进度轮询（REQ-20260922-NNN 标记删除行）。

        内存 job 优先；服务重启后按磁盘兜底（sidecar 与当前 marks 匹配 = done /
        只剩 tmp 半成品 = error / 否则 idle — 见 compose_service.opt_cut_status）。
        """
        from slirn_home import compose_service, optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        data = optimize_service.load_optimize(outputs_dir)
        plan = optimize_service.cut_plan(data or {})  # 整行标记 + 删除子段
        st = compose_service.opt_cut_status(tid, outputs_dir, plan)
        ready = compose_service.optimize_cut_ready(outputs_dir, plan)
        return _ok("", job=st,
                   ready={"exists": ready is not None,
                          "path": compose_service.OPTIMIZE_COMPOSE_NAME if ready else ""})

    @app.app.post("/slirn/api/optimize_cut_rekick")
    async def optimize_cut_rekick(body: dict = Body(default_factory=dict)):
        """显式触发优化成片剪辑（REQ-20260923-NNN 优化字幕页「🎬 重新拼接视频」）。

        按当前剪辑计划（🗑️ 整行标记 + 切分行删除子段）对粗剪成片做剪除+拼接。
        剪辑的唯一触发入口（保存不自动触发），供产物失效（rough 重合成 / 轮询
        中断）/ 想手动触发时使用。
        """
        from slirn_home import compose_service, optimize_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        data = optimize_service.load_optimize(outputs_dir)
        if data is None:
            return _err("尚无优化字幕数据 — 请先执行「开始优化字幕」")
        plan = optimize_service.cut_plan(data)
        marks = plan["marks"]
        n_subs = sum(len(v) for v in plan["split_marks"].values())
        # 剪辑在跑 → 不二启，前端直接续上轮询
        job_st = compose_service.opt_cut_job_status(tid)
        if job_st and job_st.get("state") == "running":
            return _ok("", cut={"state": "running", "marks": marks})
        if not optimize_service.plan_has_deletions(plan):
            # 与保存钩子同语义：计划空 → 顺手清掉失效旧产物（如计划已清但
            # job 曾在跑没走到清理分支）
            removed = compose_service._delete_cut_artifacts(outputs_dir)
            if removed:
                return _ok("", cut={"state": "cleared", "marks": []},
                           toast="🗑️ 当前没有要剪除的内容 — 已清掉旧的优化成片")
            return _err("当前没有要剪除的内容 — 先点 🗑️ 标记整行，或 ✂️ 切分后删除子段")
        rough = compose_service.rough_compose_path(outputs_dir)
        if not rough.exists():
            return _err("粗剪成片不存在 — 请先完成「粗剪合成」")
        started = compose_service.start_optimize_cut(tid, outputs_dir, rough)
        if started:
            what = f"{len(marks)} 行" + (f" + {n_subs} 子段" if n_subs else "")
            return _ok("", cut={"state": "started", "marks": marks},
                       toast=f"🎬 已开始重新拼接视频（剪除 {what}）— 完成后精剪合成自动使用")
        return _ok("", cut={"state": "running", "marks": marks},
                   toast="🎬 视频重新拼接正在进行中")

    @app.app.post("/slirn/api/resplit_segment")
    async def resplit_segment_api(body: dict = Body(default_factory=dict)):
        """单段重新切分（REQ-20260916-011）：改切分后内容 → 回写修订 + 重算清单落盘。

        纯逻辑见 cutlist_service.resplit_segment（命中 0 / 无字级时间戳 →
        宁可不切不可错切，返回错误不落盘）。
        """
        import json
        import time as _time

        from slirn_home import asr_service, cutlist_service, revision_service

        tid = (body.get("task_id") or "").strip()
        if not tid:
            return _err("缺少 task_id")
        try:
            si = int(body.get("source_i"))
        except (TypeError, ValueError):
            return _err("缺少 source_i")
        target_text = str(body.get("target_text") or "")
        try:
            mgr.get(tid)
        except Exception as e:  # noqa: BLE001
            return _err(f"任务不存在: {e}")
        outputs_dir = mgr.tasks_dir / tid / "outputs"
        rev = revision_service.load_revision(outputs_dir)
        if not (rev and rev.get("entries")):
            return _err("尚无修订建议，请先在「字幕修订」阶段完成大模型分析")
        sub_meta = asr_service.load_subtitle(outputs_dir)
        if not (sub_meta and sub_meta.get("segments")):
            return _err("缺少字幕生成产物（subtitle.json），请先在「字幕生成」阶段生成字幕")
        saved = cutlist_service.load_cutlist(outputs_dir)
        cutlist, rev2, err = cutlist_service.resplit_segment(
            sub_meta, rev, si, target_text, saved)
        if err:
            return _err(err)
        # 修订先落盘、清单后落盘（saved_at 单调，不误报过期黄条）
        rev2["saved_at"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
        (outputs_dir / revision_service.REVISION_JSON).write_text(
            json.dumps(rev2, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cutlist_service.save_cutlist(outputs_dir, cutlist)
        try:
            mgr.update_status(tid, TaskStatus.ROUGH_CUT_DONE)  # 幂等：清单在盘即完成
        except Exception as e:  # noqa: BLE001
            revision_service.log.warning("更新任务 %s 状态失败: %s", tid, e)
        subs = [it for it in cutlist.get("items", [])
                if int(it.get("source_i", -1)) == si and it.get("kind") == "split"]
        n_keep = sum(1 for s in subs if str(s.get("mark")) == "keep")
        n_del = len(subs) - n_keep
        return _ok("", toast=(
            f"✅ 第 {si} 条已重新切分：{len(subs)} 个子段"
            f"（保留 {n_keep} · 删除洞 {n_del}）— 可预播确认"
        ))

    @app.app.post("/slirn/api/file_selected")
    async def file_selected(body: dict = Body(default_factory=dict)):
        fp = (body.get("path") or "").strip()
        if not fp:
            return _ok(_render_create_task(repo_root))
        from tasklib.video import get_video_duration
        p = Path(fp)
        if not p.exists():
            return _err("文件不存在")
        dur = get_video_duration(p)
        if dur:
            mm, ss = divmod(int(dur), 60)
            hh, mm = divmod(mm, 60)
            dur_str = f"{hh:02d}:{mm:02d}:{ss:02d}"
        else:
            dur_str = "未知"
        size_mb = p.stat().st_size / 1024 / 1024
        return _ok(
            _render_create_task(repo_root),
            file_info={
                "name": p.name,
                "duration": dur_str,
                "duration_seconds": float(dur) if dur else 0.0,
                "size_mb": round(size_mb, 1),
                "path": str(p).replace("\\", "/"),
            },
        )

    @app.app.post("/slirn/api/cut_preview")
    async def cut_preview(body: dict = Body(default_factory=dict)):
        fp = (body.get("path") or "").strip()
        start = (body.get("start") or "").strip()
        end = (body.get("end") or "").strip()
        if not fp or not start or not end:
            return _err("需要路径 + 开始时间 + 结束时间")
        # 服务端兜底校验：开始 < 结束（解析失败也拒）
        from tasklib.exceptions import InvalidTimeRangeError
        from tasklib.time_utils import parse_time
        try:
            s_sec = parse_time(start).total_seconds()
            e_sec = parse_time(end).total_seconds()
        except ValueError as e:
            return _err(f"时间格式错误: {e}")
        if s_sec >= e_sec:
            return _err(f"开始时间 ({start}) 必须小于结束时间 ({end})")
        # 用 slirn-standalone tasklib.video.cut_video（流式 ffmpeg 实现）
        from tasklib.exceptions import VideoProcessingError
        from tasklib.video import cut_video
        src = Path(fp)
        if not src.exists():
            return _err("视频文件不存在")
        # 截取写到 Gradio 缓存目录结构（%TEMP%/gradio/<sha256>/<name>）
        # 这样 /gradio_api/file=<encoded-path> 才能服务（Gradio 白名单仅缓存目录内的文件，
        # 其它路径返回 403 — 这就是「上传的视频没有声音」的根因）
        import hashlib as _hashlib_mod
        import tempfile as _tempfile_mod
        temp_dir = repo_root / ".temp"
        temp_dir.mkdir(exist_ok=True)
        dst = temp_dir / f"cut_{_uuid.uuid4().hex[:8]}_{src.stem}{src.suffix}"
        try:
            cut_video(src, dst, start, end)
        except VideoProcessingError as e:
            return _err(f"截取失败: {e}")
        if not dst.exists():
            return _err("截取后未生成文件")
        # 把文件移到 Gradio 缓存目录（保留原文件名），并按文件内容 hash 分桶
        with open(dst, "rb") as _f:
            _data_bytes = _f.read()
        sha_hex = _hashlib_mod.sha256(_data_bytes).hexdigest()
        gradio_cache = Path(_tempfile_mod.gettempdir()) / "gradio" / sha_hex
        gradio_cache.mkdir(parents=True, exist_ok=True)
        final_dst = gradio_cache / dst.name
        try:
            final_dst.write_bytes(_data_bytes)
            dst.unlink()  # 清理 .temp/ 里的副本
        except Exception:
            # 写缓存失败 — 退回到 .temp/ 路径（前端会 403，但至少服务端不出错）
            final_dst = dst
        size_mb = final_dst.stat().st_size / 1024 / 1024
        # 不返回 html — 不要替换整个 tab（保留原文件预览 + 滑块 + 时间输入）
        return _ok(
            "",
            toast=f"✅ 待剪辑视频已生成 {round(size_mb, 1)} MB",
            cut_done={
                "path": str(final_dst).replace("\\", "/"),
                "size_mb": round(size_mb, 1),
            },
        )

    @app.app.post("/slirn/api/create_task")
    async def create_task(body: dict = Body(default_factory=dict)):
        fp = (body.get("path") or "").strip()
        start = (body.get("start") or "").strip()
        end = (body.get("end") or "").strip()
        name = (body.get("name") or "").strip()
        # 三种来源：① 全部继承（true/false）② 选中（数组）③ 手动（文本）
        inherit_public = bool(body.get("inherit_public"))
        picked_raw = body.get("picked_words")
        manual_text = (body.get("manual_words") or body.get("hotwords") or "").strip()

        if not fp:
            return _err("未选择视频")
        src = Path(fp)
        if not src.exists():
            return _err("视频文件不存在")
        final_name = name or src.stem

        # 组合最终热词（按：全部继承 → 选中 → 手动 去重保序）
        # REQ-20260915-002：同时记录每个词的来源（详情页回显 chips 标签用）
        seen = set()
        ordered = []
        word_sources: dict[str, str] = {}
        if inherit_public:
            try:
                hwlib = HotwordLibrary(repo_root)
                for _cat, ws in hwlib.list_grouped().items():
                    for w in ws:
                        w = w.strip()
                        if w and w not in seen:
                            seen.add(w)
                            ordered.append(w)
                            word_sources[w] = "inherit"
            except Exception:
                pass
        if isinstance(picked_raw, list):
            for w in picked_raw:
                w = str(w or "").strip()
                if w and w not in seen:
                    seen.add(w)
                    ordered.append(w)
                    word_sources[w] = "pick"
        for w in _re.split(r"[,\n，;；、]+", manual_text):
            w = w.strip()
            if w and w not in seen:
                seen.add(w)
                ordered.append(w)
                word_sources[w] = "manual"
        hotwords = ordered
        segment = None
        seg_filename = None
        segment_temp = None  # 无截取参数时不进 if，先定义避免下方 UnboundLocalError
        if start and end:
            # 找最近的截取文件：.temp/（旧路径）+ %TEMP%/gradio/<sha>/（cut_preview 现把
            # 文件放这里过 /gradio_api/file= 白名单）。两处都找，取最新。
            import tempfile as _tempfile_mod

            candidates: list[Path] = []
            temp_dir = repo_root / ".temp"
            if temp_dir.exists():
                candidates.extend(temp_dir.glob(f"cut_*_{src.stem}{src.suffix}"))
            gradio_root = Path(_tempfile_mod.gettempdir()) / "gradio"
            if gradio_root.exists():
                candidates.extend(gradio_root.glob(f"*/cut_*_{src.stem}{src.suffix}"))
            candidates = [c for c in candidates if c.is_file()]
            if candidates:
                segment_temp = max(candidates, key=lambda p: p.stat().st_mtime)
                from tasklib.time_utils import segment_filename
                ext = src.suffix.lstrip(".") or "mp4"
                seg_filename = segment_filename(src.stem, start, end, ext)
                from tasklib.models import TimeSegment
                segment = TimeSegment(start=start, end=end, path=Path("placeholder"))
            else:
                segment_temp = None
        try:
            # REQ-20260926-NNN：记录创建者（未登录/测试 → None，按旧任务兼容）
            creator = _get_cur_user().get("username")
            task = mgr.create(
                name=final_name, original_video=src, segment=segment, hotwords=hotwords,
                inherit_public=inherit_public,
                hotword_sources=word_sources if word_sources else None,
                created_by=creator,
            )
            if creator:
                # 创建者自动成为成员（admin 之外的用户凭成员资格访问）
                auth.add_task_member(task.task_id, creator)
        except Exception as e:
            return _err(f"创建失败: {e}")
        if segment_temp is not None and segment_temp.exists() and seg_filename:
            import shutil
            final_path = repo_root / "tasks" / task.task_id / "raw_input" / seg_filename
            try:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(segment_temp), str(final_path))
            except Exception:
                pass
        return _ok(_task_list_for_cur_user(), toast="✅ 任务已创建", task_id=task.task_id)

    # REQ-20260920-090：诊断 BGM 合成路径（不跑 ffmpeg、不创建临时文件）
    @app.app.post("/slirn/api/diagnose_bgm")
    async def diagnose_bgm(body: dict = Body(default_factory=dict)):
        """根据 fc 状态预测 BGM 合成路径。

        同步探测「如果按当前 fc 状态合成，是否会产生 BGM」——不调用 ffmpeg。
        返回：elements（5 元素 will_render 状态）+ inputs_count + audio_idx
              + predicted_has_bgm + predicted_audio_filters + why_no_bgm
        """
        tid = body.get("task_id")
        if not tid:
            return {"ok": False, "error": "missing task_id"}
        try:
            fc = _get_fine_compose(mgr, tid)
        except FileNotFoundError:
            return {"ok": False, "error": "fc not found"}
        except Exception as exc:
            return {"ok": False, "error": f"读取 fc 失败: {exc}"}
        # REQ-20260920-095：探测源视频时长，让 predicted_audio_filters 的
        # fade_out 起算与 _assemble_fine_filter 真正执行的命令完全一致。
        audio_total_duration: float | None = None
        try:
            _ms = _probe_video_duration_ms(mgr, tid)
            if _ms and _ms > 0:
                audio_total_duration = _ms / 1000.0
        except Exception as exc:
            log.debug("REQ-095 diagnose: probe 源视频时长失败，预测器退回 st=0: %s", exc)
        return _predict_audio_path(fc, audio_total_duration=audio_total_duration)

    # REQ-20260920-090：ffprobe volumedetect 探测 output 文件音频信息
    @app.app.post("/slirn/api/probe_output_audio")
    async def probe_output_audio(body: dict = Body(default_factory=dict)):
        """ffprobe 探测 output 视频的音频流 + volumedetect。

        Body:
          task_id: str (必需)
          output_path: str (可选, 相对 tasks/<tid>/; 默认 outputs/final.mp4)
          bgm_path: str (可选, 相对 tasks/<tid>/; 提供则对比 BGM 源 mean_volume)

        Returns:
          ok, audio_streams (list), duration, mean_volume_db (output),
          max_volume_db (output), bgm_mean_volume_db (if bgm_path),
          bgm_max_volume_db (if bgm_path), match_score (0..1),
          output_abs_path (str, 完整绝对路径，REQ-094 新增——前端给用户展示用)
        """
        import subprocess as _sp
        import json as _json

        tid = body.get("task_id")
        if not tid:
            return {"ok": False, "error": "missing task_id"}
        output_rel = body.get("output_path") or "outputs/final.mp4"
        bgm_rel = body.get("bgm_path")

        task_root = mgr.tasks_dir / tid
        output_abs = (task_root / output_rel).resolve()
        if not output_abs.exists():
            return {"ok": False, "error": f"output 不存在: {output_abs}"}

        def _run_probe(args: list[str]) -> tuple[int, str, str]:
            proc = _sp.Popen(args, stdout=_sp.PIPE, stderr=_sp.PIPE, encoding="utf-8", errors="replace")
            out, err = proc.communicate(timeout=30)
            return proc.returncode, out, err

        # 1. 探测 audio stream 数 + duration
        rc, out, err = _run_probe([
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index,codec_name,duration:format=duration",
            "-of", "json", str(output_abs),
        ])
        audio_streams: list[dict] = []
        duration: float | None = None
        if rc == 0 and out.strip():
            try:
                probe_json = _json.loads(out)
                audio_streams = probe_json.get("streams") or []
                fmt = probe_json.get("format") or {}
                if fmt.get("duration"):
                    duration = float(fmt["duration"])
            except Exception:
                pass

        # 2. volumedetect 探测 output mean_volume
        rc, out, err = _run_probe([
            "ffprobe", "-v", "error", "-af", "volumedetect",
            "-of", "json", str(output_abs),
        ])
        mean_volume_db: float | None = None
        max_volume_db: float | None = None
        if rc == 0 and out.strip():
            try:
                probe_json = _json.loads(out)
                streams = probe_json.get("streams") or []
                if streams:
                    mean_volume_db = float(streams[0].get("mean_volume", "0").replace("dB", "").strip()) if "mean_volume" in streams[0] else None
                    max_volume_db = float(streams[0].get("max_volume", "0").replace("dB", "").strip()) if "max_volume" in streams[0] else None
            except Exception:
                pass

        # 3. （可选）对比 BGM 源
        bgm_mean_volume_db: float | None = None
        bgm_max_volume_db: float | None = None
        match_score: float | None = None
        if bgm_rel:
            bgm_abs = (task_root / bgm_rel).resolve()
            if bgm_abs.exists():
                rc, out, err = _run_probe([
                    "ffprobe", "-v", "error", "-af", "volumedetect",
                    "-of", "json", str(bgm_abs),
                ])
                if rc == 0 and out.strip():
                    try:
                        probe_json = _json.loads(out)
                        streams = probe_json.get("streams") or []
                        if streams:
                            bgm_mean_volume_db = float(streams[0].get("mean_volume", "0").replace("dB", "").strip()) if "mean_volume" in streams[0] else None
                            bgm_max_volume_db = float(streams[0].get("max_volume", "0").replace("dB", "").strip()) if "max_volume" in streams[0] else None
                    except Exception:
                        pass
                # 计算匹配度：mean_volume 差距越小分数越高（0..1）
                if mean_volume_db is not None and bgm_mean_volume_db is not None:
                    diff = abs(mean_volume_db - bgm_mean_volume_db)
                    match_score = max(0.0, 1.0 - diff / 30.0)  # 30dB 差距 = 0 分

        return {
            "ok": True,
            "audio_stream_count": len(audio_streams),
            "duration": duration,
            "mean_volume_db": mean_volume_db,
            "max_volume_db": max_volume_db,
            "bgm_mean_volume_db": bgm_mean_volume_db,
            "bgm_max_volume_db": bgm_max_volume_db,
            "match_score": match_score,
            # REQ-20260920-094：返回绝对路径给前端展示（用户可直接拖到浏览器）
            "output_abs_path": str(output_abs),
        }

    @app.app.post("/slirn/api/cancel_create")
    async def cancel_create():
        return _ok(_task_list_for_cur_user())

    # ===== REQ-20260926-NNN：用户管理 + 会话 =====
    # ===== REQ-20261019-224：IP 登录失败封禁（guard 传给登录端点计数） =====
    from slirn_home.ip_guard import IPGuard
    _ip_guard = IPGuard(repo_root)
    from slirn_home.auth_routes import register_auth_endpoints as _register_auth
    _register_auth(app, auth, repo_root, ip_guard=_ip_guard)
    # 中间件要在所有路由注册完后再 add_middleware（Starlette 顺序）
    from slirn_home.auth import install_auth_middleware
    install_auth_middleware(app.app, auth, task_access=slirn_task_access(mgr, auth))
    # IP 封禁拦截装在 auth 之外（更外层）：在 auth 之后调用 → insert(0) 后进者在外，
    # 覆盖所有 HTTP 路径（含登录/静态/Gradio 路由），而 auth 只守 /slirn/api/*
    from slirn_home.ip_guard import install_ip_guard_middleware
    install_ip_guard_middleware(app.app, _ip_guard)
