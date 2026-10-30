"""短视频单源 AI 拆条工作台的服务端 HTML 渲染。

REQ-20261003-098：管线（选源 → 抽字幕 → 解析 → 内容优化 → 粗剪合成 → 精剪混编）。
REQ-20261016-148：Stage 6 字幕优化阶段整体移除（原精剪 7 上移为 6）；
「解析短视频内容」改名「解析内容」、「粗剪」改名「粗剪合成」。
REQ-20261017-150：前三阶段改名 —「选择源视频」→「视频选择」、「提取字幕」→
「字幕提取」、「解析内容」→「内容解析」。
REQ-20261017-151：「内容解析」再改名「内容提取」。
REQ-20261012-099：Stage 3 由「AI 拆条」改名并改版 —— 用户录入解析提示词（默认 +
最新 10 条历史两级复用）、可选上传截图一起发给多模态 AI；每个片段可单独重新解析。
修订（验收反馈）：移除快速模板 UI；历史条目可展开查看完整内容、⭐ 标默认；
截图区块标题改为「智能纪要课程重点截图」；保存提示词后历史下拉原地自动刷新。
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from urllib.parse import quote

from .short_video_service import (
    STAGE3_SYSTEM_PROMPT,
    stage3_system_prompt_template,
)
from . import short_video_optimize as _sv_opt


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# REQ-20261016-126：Stage 2 字幕预览（直接渲染在卡片下方，替代弹窗/新页）
# 4 字段格式与 REQ-20261013-110 Stage 3/4 一致（#N / 时间段 / 文字），
# 但只读、不带编辑按钮。SRT 文件 ≤ 几 MB 时整体渲染到 HTML 即可。


def _fmt_srt_time_ms(ms: int) -> str:
    """毫秒 → SRT 文本时间戳 ``HH:MM:SS,mmm``。"""
    h, ms = divmod(int(ms), 3600 * 1000)
    m, ms = divmod(ms, 60 * 1000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_srt_entries(srt_text: str) -> list[dict]:
    """解析 SRT 文本为 ``{no, start_ms, end_ms, text}`` 列表（按出现顺序）。

    漏掉无法解析的块而不是 raise——单条坏数据不该让整页没 SRT 预览。
    """
    import re

    if not srt_text:
        return []
    # 去 BOM、按空行切块、过滤空块
    blocks = [b for b in re.split(r"\r?\n\r?\n+", srt_text.lstrip("﻿").strip()) if b.strip()]
    entries: list[dict] = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        try:
            no = int(lines[0].strip())
        except ValueError:
            continue
        m = re.match(r"\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})", lines[1])
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        start_ms = (h1 * 3600 + m1 * 60 + s1) * 1000 + ms1
        end_ms = (h2 * 3600 + m2 * 60 + s2) * 1000 + ms2
        text = "\n".join(lines[2:]).strip()
        entries.append({"no": no, "start_ms": start_ms, "end_ms": end_ms, "text": text})
    return entries


# REQ-20261030-017：SRT 预览每页行数。
# 2 小时视频的 raw.srt 有 2900 条，全量内联进 HTML 是 800KB（行标记开销远
# 大于文本本身）—— 外网带宽下光这一个预览就要好几秒。改成按页取：首屏只带
# 一页，用户点「加载更多」再续（完整内容仍可看，也保留 raw.srt 下载）。
_SRT_PREVIEW_PAGE = 200


def _srt_read_entries(srt_path: str) -> list[dict]:
    """读 SRT 并解析成条目（读不到 → 空列表）。"""
    if not srt_path:
        return []
    try:
        text = Path(srt_path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    return _parse_srt_entries(text)


def _srt_rows_html(entries: list[dict], offset: int, limit: int) -> tuple:
    """渲染一页 SRT 行 + 「加载更多」按钮。

    返回 (rows_html, more_html, total, shown_to) —— more_html 是「还有 N 行」
    的加载按钮（没有更多时为空串）。
    """
    total = len(entries)
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or _SRT_PREVIEW_PAGE))
    page = entries[offset:offset + limit]
    rows = []
    for e in page:
        time_range = f"{_fmt_srt_time_ms(e['start_ms'])} → {_fmt_srt_time_ms(e['end_ms'])}"
        rows.append(
            f'<div class="slirn-stage2-srt-row">'
            f'<span class="slirn-stage2-srt-no" title="SRT 块序号">#{e["no"]}</span>'
            f'<span class="slirn-stage2-srt-time" title="本段时间戳">{time_range}</span>'
            f'<span class="slirn-stage2-srt-text">{esc(e["text"])}</span>'
            f'</div>'
        )
    shown_to = offset + len(page)
    more = ""
    if shown_to < total:
        more = (
            f'<div class="slirn-stage2-srt-more">'
            f'<button type="button" class="slirn-btn-mini" data-action="sv-srt-more" '
            f'data-offset="{shown_to}" data-limit="{limit}">'
            f'加载更多（还有 {total - shown_to} 行）</button></div>'
        )
    return "".join(rows), more, total, shown_to


def _render_srt_preview(srt_path: str, *, offset: int = 0,
                        limit: int = _SRT_PREVIEW_PAGE) -> str:
    """渲染 SRT 预览面板（内联在 Stage 2 卡片下方，按页取）。

    SRT 读不到/解析失败时返回空串（不影响卡片）。
    """
    entries = _srt_read_entries(srt_path)
    if not entries:
        return ""
    text_chars = sum(len(e["text"]) for e in entries)
    rows, more, total, shown_to = _srt_rows_html(entries, offset, limit)
    summary = (f'📄 字幕预览 · 共 {total} 行 / {text_chars} 字'
               f'（已显示 {shown_to} 行）')
    return (
        f'<div class="slirn-stage2-srt-preview" data-total="{total}">'
        f'<div class="slirn-sub-meta" style="margin-top:8px;">{summary}</div>'
        f'<div class="slirn-stage2-srt-rows">{rows}</div>'
        f'{more}'
        f'</div>'
    )


def render_srt_preview_page(srt_path: str, *, offset: int = 0,
                             limit: int = _SRT_PREVIEW_PAGE) -> dict:
    """SRT 预览「加载更多」的分页数据（供 /sv_srt_preview 端点用）。

    html = 追加到 .slirn-stage2-srt-rows 的行块（含新的「加载更多」按钮）。
    """
    entries = _srt_read_entries(srt_path)
    rows, more, total, shown_to = _srt_rows_html(entries, offset, limit)
    return {"html": rows + more, "rows_html": rows, "more_html": more,
            "total": total, "shown_to": shown_to,
            "offset": max(0, int(offset or 0)),
            "limit": max(1, int(limit or _SRT_PREVIEW_PAGE))}


def _file_url(task_id: str, project_id: str, kind: str, name: str) -> str:
    return (
        "/slirn/api/short_video_file?"
        f"task_id={quote(str(task_id))}&project_id={quote(str(project_id))}"
        f"&kind={quote(kind)}&name={quote(name)}"
    )


def _source_video_url(project: dict) -> str:
    """当前选源视频的可访问 URL；未选源返回空串。"""
    task_id = str(project.get("task_id") or "")
    project_id = str(project.get("id") or "")
    materials = project.get("materials") or []
    base_id = project.get("base_material_id") or ""
    src = next(
        (m for m in materials if m.get("id") == base_id and m.get("kind") == "video"),
        None,
    )
    if not src:
        return ""
    return _file_url(task_id, project_id, "material", str(src.get("id") or ""))


def _duration_str(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _format_ms(ms: int) -> str:
    if ms <= 0:
        return "00:00"
    s = ms / 1000.0
    return _duration_str(s)


def render_prompt_history_list(prompt_settings: dict) -> str:
    """渲染全局提示词历史列表（REQ-099 修订：可展开查看完整内容 + 标出默认）。

    既用于 Stage 3 面板初始渲染，也作为 ``short_video_save_default_prompt`` /
    ``short_video_prompt_history_save`` 端点的 ``history_html`` 片段返回 —— 前端
    保存后原地替换 ``.slirn-stage3-hist-list``，实现「存入列表后下拉自动刷新」。
    """
    default_prompt = str(((prompt_settings.get("default_prompt") or {}).get("prompt")) or "")
    items = []
    for item in prompt_settings.get("history") or []:
        p = str(item.get("prompt") or "")
        if not p.strip():
            continue
        head = p.strip().replace("\n", " ")
        head = head[:80] + ("…" if len(head) > 80 else "")
        when = str(item.get("updated_at") or "")[:16].replace("T", " ")
        star = (
            '<span class="slirn-sv-badge slirn-sv-badge-ok slirn-stage3-hist-default">⭐ 默认</span>'
            if default_prompt and p.strip() == default_prompt.strip() else ""
        )
        items.append(
            f'<div class="slirn-stage3-hist-item">'
            f'<div class="slirn-stage3-hist-head">'
            f'<button type="button" class="slirn-stage3-hist-toggle" '
            f'data-action="sv-stage3-toggle-hist" title="展开 / 收起完整内容">{esc(head)}{star}'
            f'<span class="slirn-sv-muted slirn-stage3-hist-time">{esc(when)}</span></button>'
            f'<button type="button" class="slirn-btn-mini" data-action="sv-stage3-load-prompt" '
            f'data-prompt="{esc(p)}" title="载入到提示词文本域">载入</button>'
            # REQ-20261016-128：每条历史后加 🗑 删除；若是默认会同时清除默认标记
            f'<button type="button" class="slirn-btn-mini slirn-btn-mini-danger" '
            f'data-action="sv-stage3-delete-prompt" data-stage="stage3" '
            f'data-prompt="{esc(p)}" '
            f'title="从历史列表删除这一条（若是默认会同时清除默认标记）">🗑 删除</button>'
            f'</div>'
            f'<pre class="slirn-stage3-hist-full" hidden>{esc(p)}</pre>'
            f'</div>'
        )
    return "".join(items) or '<div class="slirn-sv-muted">尚无保存的提示词</div>'


# ---------- 项目列表（保持兼容） ----------

def render_project_list(
    tasks: list[dict],
    projects: list[dict],
    scope: str = "all",
    is_admin: bool = False,
) -> str:
    """短视频项目列表 HTML。

    REQ-20261012-099 修订四：对齐长视频列表 —
    - scope radio（我的任务/全部任务，仅 admin；普通用户固定「我的」由后端钳制）
    - 按短视频任务名（项目名）搜索（前端 client-side 过滤）
    - 「➕ 新建短视频任务」按钮从 hero 挪入列表头部
    """
    task_options = "".join(
        f'<option value="{esc(t.get("task_id"))}">{esc(t.get("task_id"))} · {esc(t.get("name"))}</option>'
        for t in tasks
    )
    cards = []
    for p in projects:
        kind = str(p.get("kind") or "mixcut")
        kind_label = "AI 拆条" if kind == "split" else "多素材混剪（旧）"
        cards.append(
            f'<div class="slirn-sv-card" data-task-id="{esc(p.get("task_id"))}" '
            f'data-project-id="{esc(p.get("id"))}">'
            f'<div class="slirn-sv-card-head"><strong>{esc(p.get("name"))}</strong>'
            f'<span class="slirn-sv-badge">{esc(kind_label)}</span></div>'
            f'<div class="slirn-sv-muted">任务 {esc(p.get("task_id"))} · 素材 {len(p.get("materials") or [])}</div>'
            f'<div class="slirn-sv-card-actions">'
            f'<button class="slirn-btn slirn-btn-primary" data-action="sv-open" '
            f'data-task-id="{esc(p.get("task_id"))}" data-project-id="{esc(p.get("id"))}">打开</button>'
            f'<button class="slirn-btn slirn-btn-danger" data-action="sv-delete" '
            f'data-task-id="{esc(p.get("task_id"))}" data-project-id="{esc(p.get("id"))}">删除</button>'
            f'</div></div>'
        )
    if not cards:
        cards.append('<div class="slirn-empty"><div class="slirn-empty-text">还没有短视频项目</div></div>')
    # scope radio（admin 才显示「我的任务/全部任务」切换；普通用户固定「我的」）
    scope_html = (
        '<div class="slirn-scope-toggle">'
        '<label><input type="radio" name="slirn-sv-scope" value="mine"'
        + (' checked' if scope == "mine" else '') + '> 我的任务</label>'
        '<label><input type="radio" name="slirn-sv-scope" value="all"'
        + (' checked' if scope != "mine" else '') + '> 全部任务</label>'
        '</div>'
    ) if is_admin else ''
    return f'''<div id="slirn-short-video-inner" class="slirn-tab-inner">
  <div class="slirn-sv-header">
    <div><h2>单源 AI 短视频内容拆条</h2><p>从一段完整视频出发，AI 通过字幕重组+重排序产生 3-5 个独立短视频。</p></div>
    <div class="slirn-sv-header-actions">
      <button class="slirn-btn slirn-btn-primary" data-action="sv-new">➕ 新建短视频任务</button>
      <button class="slirn-btn" data-action="sv-refresh">刷新</button>
    </div>
  </div>
  <div class="slirn-sv-create" id="slirn-sv-create">
    <div class="slirn-sv-section-title">新建短视频任务</div>
    <div class="slirn-sv-form-grid">
      <label>来源任务<select id="slirn-sv-create-task">{task_options}</select></label>
      <label>项目名<input id="slirn-sv-create-name" placeholder="例如：公开课拆条"></label>
      <button class="slirn-btn slirn-btn-primary slirn-sv-create-btn" data-action="sv-create">创建项目</button>
    </div>
  </div>
  <div class="slirn-toolbar">
    {scope_html}
    <div class="slirn-search-wrap">
      <input class="slirn-search-box" id="slirn-sv-search" placeholder="搜索短视频任务名 / 任务 ID..." />
    </div>
  </div>
  <div class="slirn-sv-section-title">项目列表</div>
  <div class="slirn-sv-grid">{"".join(cards)}</div>
</div>'''


# ---------- 6 阶段管线详情页 ----------

def _kind_label(kind: object) -> str:
    return {"video": "视频", "image": "图片", "audio": "音频", "bg_image": "背景图片", "file": "文件"}.get(
        str(kind or ""), str(kind or "素材"),
    )


def _stage_badge(status: str | None) -> str:
    """阶段状态徽章（pending / running / done / failed）。"""
    s = str(status or "pending").lower()
    label = {"pending": "○ 未开始", "running": "⏳ 处理中", "done": "✓ 完成",
             "failed": "✗ 失败", "running_asr": "⏳ ASR 中", "user_editing": "✎ 待修正",
             "confirmed": "✓ 已确认"}.get(s, s)
    return f'<span class="slirn-stage-badge slirn-stage-{s}">{esc(label)}</span>'


def _stage_elapsed(state: dict) -> str:
    """渲染阶段已用时（仅 running/running_asr 时显示）。

    REQ-20261004-UX：用户希望在「处理中」状态看到已经执行多长时间。
    起始时间戳在 ``state["started_at"]``（ISO）；客户端每秒刷新一次文本。
    """
    status = str(state.get("status") or "").lower()
    if status not in ("running", "running_asr"):
        return ""
    started = str(state.get("started_at") or "")
    if not started:
        return ""
    return (
        f'<span class="slirn-stage-elapsed" '
        f'data-stage-elapsed data-started-at="{esc(started)}">0:00</span>'
    )


def _stage_logs(state: dict) -> str:
    """渲染阶段执行日志（可折叠 details）。

    REQ-20261004-UX：每个阶段记录自身操作过程（启动/完成/失败/警告），用户点击展开查看。
    日志为空时不显示面板。
    """
    logs = state.get("logs") or []
    if not logs:
        return ""
    rows = "".join(
        f'<div class="slirn-stage-log-row">{esc(line)}</div>' for line in logs
    )
    return (
        f'<details class="slirn-stage-logs"><summary>执行日志（{len(logs)} 条）</summary>'
        f'<div class="slirn-stage-log-body">{rows}</div></details>'
    )


# REQ-20261013-110：Stage 4 优化详情 + 警告的折叠面板。
# 与 _stage_logs 同款 <details> shell，但 body 走浅色（不混入 dark monospace，
# 因为 stats 行的 badge 在暗色底上会失真）。
def _stage4_detail(detail_html: str, warn_html: str) -> str:
    body = (detail_html or "") + (warn_html or "")
    if not body.strip():
        return ""
    return (
        f'<details class="slirn-stage4-detail">'
        f'<summary>📊 优化详情与警告</summary>'
        f'<div class="slirn-stage4-detail-body">{body}</div>'
        f'</details>'
    )


# REQ-20261013-110：毫秒 → SRT 格式 00:00:00,000（项目惯例 2 位小时）。
# 与 slirn_home.short_video_coarse._fmt_srt_time / compose_service._ms_to_srt_time 同款。
# 负数 / None 钳到 0（防御性）。
def _fmt_srt_time(ms: int | float | None) -> str:
    ms_i = max(0, int(ms or 0))
    h, rem = divmod(ms_i, 3600 * 1000)
    m, rem = divmod(rem, 60 * 1000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"



def _material_options(materials: list[dict], kind: str, selected: str = "",
                       empty_label: str = "（无可用素材）") -> str:
    rows = []
    for m in materials:
        if m.get("kind") != kind:
            continue
        value = str(m.get("id") or "")
        label = f'{m.get("name")} · {_kind_label(m.get("kind"))}'
        sel = " selected" if value == selected else ""
        rows.append(f'<option value="{esc(value)}"{sel}>{esc(label)}</option>')
    if not rows:
        rows.append(f'<option value="">{esc(empty_label)}</option>')
    return "".join(rows)


def _stage1_select_source(project: dict) -> str:
    task_id = str(project.get("task_id") or "")
    project_id = str(project.get("id") or "")
    base_id = str(project.get("base_material_id") or "")
    s1 = project.get("pipeline", {}).get("stage1_source", {}) or {}
    materials = project.get("materials") or []
    options = _material_options(materials, "video", base_id, empty_label="（请先上传视频素材）")
    src_material = next((m for m in materials if m.get("id") == base_id and m.get("kind") == "video"), None)
    src_preview = ""
    if src_material:
        url = _file_url(task_id, project_id, "material", str(src_material.get("id") or ""))
        src_preview = (
            f'<div class="slirn-stage-source-preview">'
            f'<video controls preload="metadata" src="{esc(url)}" '
            f'data-action="sv-preview" data-url="{esc(url)}" data-kind="video" '
            f'data-name="{esc(src_material.get("name") or "")}"></video>'
            f'<div class="slirn-stage-source-meta">'
            f'<strong>{esc(src_material.get("name") or "")}</strong>'
            f'<span>· {esc(str(src_material.get("duration") or ""))}s · '
            f'{esc(str(src_material.get("width") or ""))}x{esc(str(src_material.get("height") or ""))}</span>'
            f'</div></div>'
        )
    # REQ-20261003-098 + REQ-20261001-095/096/097：素材库复用（UI 改用于源视频选择）
    material_cards = []
    for m in materials:
        url = _file_url(task_id, project_id, "material", str(m.get("id") or ""))
        kind = str(m.get("kind") or "file")
        source_label = "任务导入" if m.get("source") == "auto" else "上传"
        material_cards.append(
            f'<div class="slirn-sv-material">'
            f'<span class="slirn-sv-kind-badge slirn-sv-kind-{esc(kind)}">{esc(_kind_label(kind))}</span>'
            f'<strong class="slirn-sv-material-name" title="{esc(m.get("name"))}">{esc(m.get("name"))}</strong>'
            f'<span class="slirn-sv-material-meta">{esc(source_label)}</span>'
            f'<div class="slirn-sv-material-actions">'
            f'<button class="slirn-btn-mini" data-action="sv-preview" data-url="{esc(url)}" '
            f'data-kind="{esc(kind)}" data-name="{esc(m.get("name"))}">查看</button>'
            f'<button class="slirn-btn-mini slirn-btn-mini-danger" data-action="sv-remove-material" '
            f'data-material-id="{esc(m.get("id"))}">删</button>'
            f'</div>'
            f'</div>'
        )
    upload_section = (
        f'<div class="slirn-stage-body"><div class="slirn-sv-form-grid">'
        f'<label>上传新视频素材（加入素材库）<input type="file" id="slirn-sv-upload-file" accept="video/*"></label>'
        f'</div>'
        f'<div class="slirn-sv-card-actions"><button class="slirn-btn" data-action="sv-upload">上传</button></div></div>'
    )
    materials_block = (
        '<div class="slirn-sv-materials">'
        + ("".join(material_cards) or '<span class="slirn-sv-muted">暂无素材</span>')
        + '</div>'
    )
    return f'''<div class="slirn-stage" data-stage="1">
  <div class="slirn-stage-head">
    <span class="slirn-stage-num">1</span>
    <strong>视频选择</strong>
    {_stage_badge(s1.get("status"))}
  </div>
  <div class="slirn-stage-body">
    <div class="slirn-sv-form-grid">
      <label class="slirn-sv-wide">从素材库选一个视频作为拆条源
        <select id="slirn-stage1-source">{options}</select>
      </label>
    </div>
    <button class="slirn-btn slirn-btn-primary" data-action="sv-stage1-select">选为源视频</button>
    {src_preview}
    <details style="margin-top:10px"><summary>素材库（{len(materials)} 个）</summary>
      {upload_section}
      {materials_block}
    </details>
  </div>
</div>'''


def _stage2_extract(project: dict) -> str:
    task_id = str(project.get("task_id") or "")
    project_id = str(project.get("id") or "")
    s2 = project.get("pipeline", {}).get("stage2_extract", {}) or {}
    srt_path_rel = s2.get("srt_path") or ""
    srt_url = _file_url(task_id, project_id, "stage2", "raw.srt") if s2.get("status") == "done" else ""
    srt_chars = int(s2.get("srt_chars") or 0)
    error_html = ""
    if s2.get("status") == "failed":
        err = (s2.get("error") or "失败") + " · " + (s2.get("stderr_tail") or "")[-200:]
        error_html = f'<div class="slirn-stage-error">{esc(err)}</div>'
    # REQ-20261016-126：完成后 SRT 全文直接内联在卡片下方（500px 滚动容器）。
    srt_preview_html = ""
    if s2.get("status") == "done" and srt_path_rel:
        repo_root = project.get("_repo_root")
        if repo_root:
            srt_abs = Path(str(repo_root)) / srt_path_rel
        else:
            srt_abs = Path(srt_path_rel)
        srt_preview_html = _render_srt_preview(str(srt_abs))
    # 「查看 raw.srt」作为下载原文件兜底（用户可右键另存为/换行符处理）
    preview_btn = (
        f'<a class="slirn-btn-mini" href="{esc(srt_url)}" target="_blank" rel="noopener">查看 raw.srt</a>'
        if srt_url else ""
    )
    meta = f'<span class="slirn-sv-muted">{srt_chars} 字</span>' if srt_chars else ""
    # REQ-20261015-123：热词随 Stage 2 运行落盘，重渲染/刷新后回填输入框。
    hotwords_v = esc(s2.get("hotwords") or "")
    return f'''<div class="slirn-stage" data-stage="2">
  <div class="slirn-stage-head">
    <span class="slirn-stage-num">2</span>
    <strong>字幕提取</strong>
    {_stage_badge(s2.get("status"))}
    {_stage_elapsed(s2)}
    {meta}
  </div>
  <div class="slirn-stage-body">
    <div class="slirn-sv-form-grid">
      <label class="slirn-sv-wide">热词（可选）<input id="slirn-stage2-hotwords" value="{hotwords_v}" placeholder="半角逗号分隔，提升专有名词识别"></label>
    </div>
    <div class="slirn-sv-card-actions">
      <button id="slirn-sv-stage2-run-btn" class="slirn-btn slirn-btn-primary" data-action="sv-stage2-run">字幕提取</button>
      {preview_btn}
    </div>
    <div class="slirn-rc-status-card" id="slirn-sv-stage2-status" style="display:none;">
      <div class="slirn-sub-meta">🎧 funasr 字幕提取</div>
      <div class="slirn-progress" style="margin-top:6px;">
        <div class="slirn-progress-bar slirn-sv-stage2-bar" style="width:0%;"></div>
      </div>
      <div class="slirn-status-msg slirn-sv-stage2-msg" style="display:none;"></div>
    </div>
    {srt_preview_html}
    {error_html}
    {_stage_logs(s2)}
  </div>
</div>'''


def _stage3_analyze(project: dict, source_url: str = "", srt_lines: list | None = None) -> str:
    s3 = project.get("pipeline", {}).get("stage3_analyze", {}) or {}
    n = int(s3.get("n_clips") or 4)
    # REQ-20261015-120：时长区间输入初值 = 上次运行的生效区间（缺省 60-90）
    try:
        _dr = s3.get("duration_range") or []
        dmin_v = int(float(_dr[0]))
        dmax_v = int(float(_dr[1]))
    except (TypeError, ValueError, IndexError):
        dmin_v, dmax_v = 60, 90
    dmin_v = min(max(dmin_v, 10), 600)
    dmax_v = min(max(dmax_v, dmin_v + 5), 900)
    template = str(s3.get("template") or "hook_first")
    prompt_state = s3.get("prompt_state") or {}
    custom_prompts = prompt_state.get("custom") or {}
    default_prompts = prompt_state.get("defaults") or {}

    # REQ-20261012-099 修订：快速模板 UI 已移除；模板仅作为提示词文本域的
    # 初始填充兜底（全局默认优先，历史自定义覆盖仍优先于系统默认文本）。
    def _tpl_effective_text(key: str) -> str:
        custom_entry = custom_prompts.get(key) or {}
        return str(custom_entry.get("prompt") or default_prompts.get(key) or "")

    # REQ-20261012-099：提示词文本域初始值 — 全局默认提示词 → 当前模板生效文本
    prompt_settings = s3.get("prompt_settings") or {}
    global_default = prompt_settings.get("default_prompt") or {}
    initial_prompt = str(global_default.get("prompt") or "") or _tpl_effective_text(template)
    default_badge = (
        '<span class="slirn-sv-badge slirn-sv-badge-muted">初始已填默认提示词</span>'
        if global_default.get("prompt") else ""
    )

    # 历史提示词列表（≤ 10 条，最新在前；⭐ 标默认；条目可展开查看完整内容）
    hist_html = render_prompt_history_list(prompt_settings)

    # REQ-20261012-099：截图（上传 / 缩略图 / 移除 / 发送开关）
    cur_image = s3.get("image") or {}
    img_name = str(cur_image.get("name") or "")
    task_id = str(project.get("task_id") or "")
    project_id = str(project.get("id") or "")
    if img_name:
        img_url = _file_url(task_id, project_id, "stage3", img_name)
        image_area = (
            f'<div class="slirn-stage3-image-row">'
            f'<a href="{esc(img_url)}" target="_blank" rel="noopener" title="查看大图">'
            f'<img class="slirn-stage3-image-thumb" src="{esc(img_url)}" alt="解析截图"></a>'
            f'<label class="slirn-stage3-image-send">'
            f'<input type="checkbox" id="slirn-stage3-use-image" checked> 发送截图给 AI</label>'
            f'<button class="slirn-btn-mini slirn-btn-mini-danger" '
            f'data-action="sv-stage3-remove-image">✖ 移除截图</button>'
            f'</div>'
        )
    else:
        image_area = (
            '<div class="slirn-stage3-image-row">'
            '<span class="slirn-sv-muted">未上传截图（可选：上传后解析时先提取文字作为参考方向）</span>'
            '</div>'
        )

    # 修订六：截图 OCR 提取结果提示（提取到的文字已作为参考方向拼进提示词）
    ocr_info = s3.get("ocr") or {}
    ocr_note = ""
    if ocr_info.get("source_image"):
        ocr_chars = int(ocr_info.get("chars") or 0)
        ocr_cached = "（缓存复用）" if ocr_info.get("cached") else ""
        ocr_note = (
            f'<div class="slirn-stage3-ocr-note">'
            f'<span class="slirn-sv-badge slirn-sv-badge-ok">✅ 截图文字已提取{ocr_cached}</span>'
            f'<span class="slirn-sv-muted">{ocr_chars} 字 · 作为参考方向拼入提示词</span>'
            f'<button class="slirn-btn-mini" data-action="sv-preview-srt" '
            f'data-url="{_file_url(task_id, project_id, "stage3", "ocr_text.txt")}" '
            f'title="查看从截图提取出的全部文字">📖 查看提取文字</button>'
            f'</div>'
        )

    error_html = ""
    if s3.get("status") == "failed":
        err = s3.get("error") or "失败"
        error_html = f'<div class="slirn-stage-error">{esc(err)}</div>'

    # REQ-20261004-stage3-restructure：把 srt_lines 转成 {idx → (start_ms, end_ms)} 查表，
    # 字幕行 UI 改显示 [HH:MM:SS] text + drag handle
    srt_lines = srt_lines or []
    srt_index_to_time: dict[int, tuple[int, int]] = {}
    for ln in srt_lines:
        try:
            idx = int(ln.get("index") or 0)
            sm = int(ln.get("start_ms") or 0)
            em = int(ln.get("end_ms") or 0)
        except (TypeError, ValueError):
            continue
        if idx > 0:
            srt_index_to_time[idx] = (sm, em)

    def _ts(ms: int) -> str:
        # 修订七（用户验收）：行内时间戳要完整 hh:mm:ss,ms（与 LLM 输出/SRT
        # 时间格式一致，长视频 mm:ss 会看不出在哪个小时）
        ms = max(0, int(ms))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, milli = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"

    # 高亮条目可编辑标题 + 删除 + 完整字幕预览（REQ-20261004-UX：用户要直接看到全部文字）
    # 修订六：一组 = 一条短视频（围绕一个标题，字幕合计约 70s）；组内每行带
    # 唯一编号 no + 时间戳（拖拽重排后编号随行移动不变，「还原顺序」按 no 升序）
    hl_rows = []
    # 修订九：N 条短视频逐条页签（点页签切换，不再整页长滚动）；首签默认选中
    hl_tabs = []
    for hl in s3.get("highlights") or []:
        idx = hl.get("index") or len(hl_rows) + 1
        title = hl.get("title") or f"短视频 #{idx}"
        is_first = len(hl_rows) == 0
        # REQ-20261016-130：tab 不再截断到 10 字；做成"大按钮"——完整标题在按钮
        # 内直接可见（#N + 标题 + ▾ 箭头；箭头在 active 态旋转 180° 暗示展开）。
        # 行为不变：点击仍是互斥切换 pane（sv-stage3-hl-tab → switchHlTab）。
        hl_tabs.append(
            f'<button class="slirn-stage3-hl-tab{" active" if is_first else ""}" '
            f'data-action="sv-stage3-hl-tab" data-hl-index="{idx}" '
            f'title="{esc(title)}">'
            f'<span class="slirn-stage3-hl-tab-no">#{idx}</span>'
            f'<span class="slirn-stage3-hl-tab-title">{esc(title)}</span>'
            f'<span class="slirn-stage3-hl-tab-arrow" aria-hidden="true">▾</span>'
            f'</button>'
        )
        start = hl.get("start_ms") or 0
        end = hl.get("end_ms") or 0
        sub_lines = hl.get("subtitle_lines") or []
        sub_count = len(sub_lines)
        # 字幕合计时长（修订六验收：每条约 70 秒）——行级时间戳优先，缺失查源行
        total_ms = 0
        nos: list[int] = []
        for pos, sl in enumerate(sub_lines, start=1):
            no = int(sl.get("no") or pos)
            nos.append(no)
            times = None
            try:
                _s = int(sl.get("start_ms") or 0)
                _e = int(sl.get("end_ms") or 0)
                if _e > _s:
                    times = (_s, _e)
            except (TypeError, ValueError):
                times = None
            if times is None:
                times = srt_index_to_time.get(int(sl.get("src_index") or 0))
            if times:
                total_ms += max(0, times[1] - times[0])
        full_text = "\n".join(
            f"[{int(sl.get('src_index') or 0)}] {str(sl.get('text') or '')}"
            for sl in sub_lines
        )
        preview_items = []
        # 修订八：预览要按各字幕行的时间戳**逐段**截取播放（与拼接产物一致），
        # 不再从组起点连播到组终点。窗口 = 行时间戳 + 100ms 防切词尾（同拼接）。
        seg_times: list[tuple[int, int]] = []
        for pos, sl in enumerate(sub_lines, start=1):
            t = esc(str(sl.get("text") or ""))
            src = int(sl.get("src_index") or 0)
            no = int(sl.get("no") or pos)
            try:
                _s = int(sl.get("start_ms") or 0)
                _e = int(sl.get("end_ms") or 0)
                times = (_s, _e) if _e > _s else srt_index_to_time.get(src)
            except (TypeError, ValueError):
                times = srt_index_to_time.get(src)
            if times:
                seg_times.append((times[0], times[1] + 100))
                # 修订七：行格式 = 编号. [hh:mm:ss,ms → hh:mm:ss,ms] 字幕文本（带方括号）
                range_html = f'[{_ts(times[0])} → {_ts(times[1])}]'
                tip = f"源字幕 #{src} @ {_ts(times[0])}-{_ts(times[1])}"
            else:
                range_html = "[??:??:??,???]"
                tip = f"源字幕 #{src}"
            preview_items.append(
                f'<div class="slirn-stage3-hl-sub" draggable="true" '
                f'data-hl-index="{idx}" data-src-index="{src}" title="{esc(tip)}">'
                f'<span class="slirn-stage3-hl-drag" aria-hidden="true">⋮⋮</span>'
                f'<span class="slirn-stage3-hl-no">{no}.</span>'
                f'<span class="slirn-stage3-hl-ts">{range_html}</span>'
                f'<span class="slirn-stage3-hl-text">{t}</span>'
                f'</div>'
            )
        sub_preview = "".join(preview_items) or '<div class="slirn-sv-muted">无字幕（拖拽排序需先有字幕）</div>'
        # 修订八：data-segments 按当前行序给出逐段切割窗（毫秒，已含 +100ms
        # 词尾余量，与 run_stage3_splice 完全一致）——悬浮预览逐段 seek 播放
        segs_attr = (
            f' data-segments="{esc(json.dumps(seg_times))}"' if seg_times else ""
        )
        # 源视频未选 → 预览按钮禁用（避免点了没反应）
        preview_btn = (
            f'<button class="slirn-btn-mini slirn-btn-mini-primary" '
            f'data-action="sv-stage3-preview" '
            f'data-src-url="{esc(source_url)}" '
            f'data-start-ms="{int(start)}" data-end-ms="{int(end)}"{segs_attr} '
            f'data-title="{esc(title)}" '
            f'title="按各字幕行时间戳逐段截取预览（浏览器 seek，无需生成 mp4）">'
            f'🎬 预览片段</button>'
            if source_url else
            '<button class="slirn-btn-mini" disabled title="请先在 Stage 1 选源视频">🎬 预览片段</button>'
        )
        # 还原顺序：当前行序 == 初始编号序（no 升序）时无需还原 → 禁用
        is_original = nos == sorted(nos)
        restore_btn = (
            f'<button class="slirn-btn-mini" data-action="sv-stage3-restore-order" '
            f'data-hl-index="{idx}" '
            f'title="把该条短视频的字幕行还原为大模型最初给出的顺序（编号不变）">'
            f'↩️ 还原顺序</button>'
            if not is_original else
            f'<button class="slirn-btn-mini" data-action="sv-stage3-restore-order" '
            f'data-hl-index="{idx}" disabled title="当前已是大模型最初顺序">↩️ 还原顺序</button>'
        )
        # REQ-20261015-120：合计时长越出当前区间 → ⚠ 标记（区间来自上次运行）
        total_sec = total_ms / 1000.0
        dur_flag = (
            f' ⚠️ 未达 {dmin_v}s' if total_sec < dmin_v
            else (f' ⚠️ 超 {dmax_v}s' if total_sec > dmax_v else "")
        )
        hl_rows.append(
            # 修订九：每条短视频包一层页签面板（tab bar 在列表头部），点页签切换显示
            f'<div class="slirn-stage3-hl-pane{" active" if is_first else ""}" data-hl-index="{idx}">'
            f'<div class="slirn-stage3-hl-row" data-hl-index="{idx}" data-sub-text="{esc(full_text)}">'
            f'<div class="slirn-stage3-hl-head">'
            f'<span class="slirn-sv-row-no">#{idx}</span>'
            f'<input type="text" class="slirn-stage3-hl-title" data-hl-field="title" value="{esc(title)}" placeholder="标题">'
            f'<span class="slirn-sv-muted slirn-stage3-hl-meta">{_format_ms(start)} - {_format_ms(end)} · 字幕合计 {total_sec:.0f}s{dur_flag} · {sub_count} 行</span>'
            f'{preview_btn}'
            f'{restore_btn}'
            f'<button class="slirn-btn-mini" data-action="sv-stage3-rerun" data-hl-index="{idx}" '
            f'title="针对该条的标题/主题，用当前提示词+截图参考单独重新解析（时间段与字幕内容会替换）">'
            f'🔄 重新解析</button>'
            f'<button class="slirn-btn-mini slirn-btn-mini-danger" data-action="sv-stage3-remove-hl" '
            f'data-hl-index="{idx}" title="删除这条短视频（需确认）">删除</button>'
            f'</div>'
            f'<div class="slirn-stage3-hl-preview" data-hl-index="{idx}">{sub_preview}</div>'
            f'<div class="slirn-sv-muted slirn-stage3-hl-hint">⋮⋮ 拖动调整本条短视频内字幕的先后顺序（行首编号保持不变，拼接按当前行序）；↩️ 还原顺序 = 恢复大模型最初给出的顺序</div>'
            f'</div>'
            f'</div>'
        )
    hl_list = (
        '<div class="slirn-stage3-hl-tabs">' + "".join(hl_tabs) + '</div>' + "".join(hl_rows)
        if hl_rows else
        '<div class="slirn-sv-muted">解析后将列出各条短视频</div>'
    )

    # REQ-20261013-111：Stage 3 解析进度卡（仿 stage5/7 status-card 结构）
    stage3_status_card = (
        f'<div class="slirn-rc-status-card" id="slirn-sv-stage3-status" '
        f'data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}" '
        f'style="display:none;">'
        f'<div class="slirn-sub-meta">🔍 内容提取</div>'
        f'<div class="slirn-progress" style="margin-top:6px;">'
        f'<div class="slirn-progress-bar slirn-sv-stage3-bar" style="width:0%;"></div>'
        f'</div>'
        f'<div class="slirn-status-msg slirn-sv-stage3-msg" style="display:none;"></div>'
        f'</div>'
    )

    prompt_badge = ""
    if s3.get("prompt_source") == "user" or s3.get("custom_prompt_used"):
        prompt_badge = '<span class="slirn-sv-badge slirn-sv-badge-warning">使用自定义提示词</span>'
    img_used_badge = (
        '<span class="slirn-sv-badge slirn-sv-badge-warning">已带截图</span>'
        if s3.get("image_used") else ""
    )

    return f'''<div class="slirn-stage" data-stage="3">
  <div class="slirn-stage-head">
    <span class="slirn-stage-num">3</span>
    <strong>内容提取</strong>
    {_stage_badge(s3.get("status"))}
    {_stage_elapsed(s3)}
  </div>
  <div class="slirn-stage-body">
    <label class="slirn-sv-wide slirn-stage3-prompt-label2">解析提示词 {default_badge}
      <textarea id="slirn-stage3-prompt" class="slirn-stage3-prompt-input" rows="8"
        placeholder="录入提取短视频内容所用的提示词；可「引用系统提示词」「设为默认」或「存入列表」复用">{esc(initial_prompt)}</textarea>
    </label>
    <div class="slirn-stage3-prompt-toolbar">
      <div class="slirn-stage3-prompt-actions2">
        <button class="slirn-btn-mini" data-action="sv-stage3-use-system-prompt"
          data-prompt="{esc(STAGE3_SYSTEM_PROMPT)}"
          data-prompt-tmpl="{esc(stage3_system_prompt_template())}"
          title="把系统内置的解析提示词填入文本域（独立于默认/历史；可继续编辑后再解析或设为默认）；条数与时长上下限按当前设置值自动代入">🧭 引用系统提示词<span class="slirn-sv-muted">（可带入片段数和时长限制）</span></button>
        <button class="slirn-btn-mini slirn-btn-mini-primary" data-action="sv-stage3-save-default"
          title="把当前文本域提示词保存为默认提示词（全局生效，新项目初始填充）">💾 设为默认</button>
        <button class="slirn-btn-mini" data-action="sv-stage3-save-history"
          title="存入提示词列表（最多保留最新 10 条）">📜 存入列表</button>
        <button class="slirn-btn-mini" data-action="sv-stage3-clear-prompt"
          title="清空文本域">🧹 清空</button>
      </div>
    </div>
    <details class="slirn-stage3-prompts-section">
      <summary>📜 历史提示词（最新 10 条 · ⭐ 为默认 · 点击条目查看完整内容）</summary>
      <div class="slirn-stage3-hist-list">{hist_html}</div>
    </details>
    <div class="slirn-stage3-image-block">
      <span class="slirn-sv-muted">智能纪要课程重点截图：</span>
      <input type="file" id="slirn-stage3-image-file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp">
      <button class="slirn-btn-mini" data-action="sv-stage3-upload-image">📤 上传</button>
      {image_area}
      {ocr_note}
    </div>
    <div class="slirn-sv-form-grid slirn-sv-form-grid--3col">
      <label title="本次解析要挑选出的短视频条数（1-10）">片段数<input type="number" min="1" max="10" id="slirn-stage3-n" value="{n}"></label>
      <label title="每个片段的总时长（选中字幕行秒数之和）必须 ≥ 该下限；解析提示词与结果核查都按此执行">每条时长下限(秒)<input type="number" min="10" max="600" step="5" id="slirn-stage3-dmin" value="{dmin_v}"></label>
      <label title="每个片段的总时长（选中字幕行秒数之和）必须 ≤ 该上限；解析提示词与结果核查都按此执行">每条时长上限(秒)<input type="number" min="20" max="900" step="5" id="slirn-stage3-dmax" value="{dmax_v}"></label>
    </div>
    <div class="slirn-sv-card-actions">
      <button id="slirn-sv-stage3-run-btn" class="slirn-btn slirn-btn-primary" data-action="sv-stage3-run">🔍 解析</button>
      <span class="slirn-sv-muted">{int(s3.get("count") or 0)} 条内容片段</span>
      {prompt_badge}{img_used_badge}
    </div>
    {stage3_status_card}
    <div class="slirn-stage3-hl-list">{hl_list}</div>
    {error_html}
    {_stage_logs(s3)}
  </div>
</div>'''


def _render_stage4_sub_row(hl_index: int, sl: dict, per_hl: dict,
                           eff_idx: int | None = None) -> str:
    """渲染一条 Stage 4 编辑行（每条字幕行 1 行）。

    REQ-20261017-153：原文保留 — 主行文字恒显用户修改前的原文
    （trimmed_from / edited_from；无修改 = 底稿文字），有修改时行下方
    渲染内嵌输入框（值 = 修改后文字），预览/粗剪用输入框文字。
    REQ-20261019-214：``eff_idx`` = 本行在 include_deleted 渲染表里的下标
    （渲染成 data-eff-idx）—— 预览播放列表的 row_idx 与它对齐，前端据此
    高亮当前播放行 / 点击行从该行起播。
    """
    sid = sl.get("src_index") or sl.get("no")
    text = sl.get("text") or ""
    splits = (per_hl.get("split_marks") or {}).get(str(sid)) or []
    trims = per_hl.get("line_trims") or {}
    edits = per_hl.get("line_edits") or {}
    marks = [int(x) for x in (per_hl.get("line_marks") or [])]
    # REQ-20261016-146：include_deleted 渲染口径下 marked 行带标记 → 保留可见（带删除线）
    is_marked = bool(sl.get("marked_deleted")) or int(sid) in marks
    is_split = bool(splits)
    # REQ-20261016-147：剪词行（keep_segments 由 effective 阶段按 line_trims 算出）
    trimmed = sl.get("keep_segments") is not None or str(sid) in trims
    trimmed_from = str(sl.get("trimmed_from") or "")
    edited_from = sl.get("edited_from")
    # REQ-20261017-153：orig/eff 分离 — orig 显主行，eff 进输入框；
    # 后两个 elif 是「只有 per-hl 状态、无 effective 字段」的兜底
    # REQ-20261019-223：剪词行上叠改字（ASR 错词纠错，line_edits 覆盖层）
    # → 框切到「修改后」模式（orig=剪后文字，eff=最终文字）；✂ 徽标仍按
    # 剪词层（原文 vs 剪后文字）算字数，另加 ✏ 已改
    trim_eff = None
    if trimmed_from:
        if edited_from is not None:
            orig_text, eff_text, box_mode = str(edited_from), str(text), "edit"
            trim_eff = str(edited_from)
        else:
            orig_text, eff_text, box_mode = trimmed_from, str(text), "trim"
            trim_eff = str(text)
    elif edited_from is not None:
        orig_text, eff_text, box_mode = str(edited_from), str(text), "edit"
    elif str(sid) in trims:
        orig_text, eff_text, box_mode = str(text), str(trims[str(sid)]), "trim"
    elif str(sid) in edits:
        orig_text, eff_text, box_mode = str(text), str(edits[str(sid)]), "edit"
    else:
        orig_text, eff_text, box_mode = str(text), str(text), ""
    cls = "slirn-stage4-sub-row"
    if is_marked:
        cls += " slirn-stage4-sub-row-deleted"
    if is_split or trimmed:
        cls += " slirn-stage4-sub-row-split"
    badge = ""
    if is_marked:
        badge = '<span class="slirn-sv-badge slirn-sv-badge-failed">🗑 已删</span>'
    elif trimmed:
        cut = max(0, len(trimmed_from) - len(trim_eff or eff_text))
        edit_extra = (' <span class="slirn-sv-badge slirn-sv-badge-warning">✏ 已改</span>'
                      if edited_from is not None else "")
        badge = (f'<span class="slirn-sv-badge slirn-sv-badge-warning" '
                 f'title="原文：{esc(trimmed_from)}">✂ 已剪 {cut} 字</span>'
                 f'{edit_extra}')
    elif box_mode == "edit":
        badge = '<span class="slirn-sv-badge slirn-sv-badge-warning">✏ 已改</span>'
    elif is_split:
        badge = f'<span class="slirn-sv-badge slirn-sv-badge-warning">✂ 已切 {len(splits)} 段</span>'
    # REQ-20261013-110：4 字段 — 井N / @SRT#K / 时间段 / 文字
    new_no = int(sl.get("no") or 0)
    raw_src = int(sl.get("src_index") or 0)
    split_from = sl.get("split_from")
    # split 子行优先显示父 src_index（更易对应回原 SRT）
    display_src = int(split_from) if split_from is not None else raw_src
    start_ms = int(sl.get("start_ms") or 0)
    end_ms = int(sl.get("end_ms") or start_ms)
    time_range = f"{_fmt_srt_time(start_ms)} → {_fmt_srt_time(end_ms)}"
    # split 子行是碎片，不提供剪词（回父行操作）
    resplit_btn = "" if split_from is not None else (
        f'<button type="button" class="slirn-btn-mini" data-action="sv-stage4-line-resplit" '
        f'data-seg-id="{esc(sid)}" data-hl-index="{hl_index}" '
        f'title="剪掉无用词（原文保留，行下方输入框编辑；预览/粗剪会同步切视频）">✂️</button>'
    )
    restore_btn = (
        f'<button type="button" class="slirn-btn-mini" data-action="sv-stage4-line-unsplit" '
        f'data-seg-id="{esc(sid)}" data-hl-index="{hl_index}" '
        f'title="恢复整行（取消剪词）">↩️</button>'
    ) if trimmed else ""
    # REQ-20261019-210：段内顺序手工调整 — ↑/↓ 推行（split 子行在 rest 尾部，
    # 跟随父行整体，不单独提供移动）；拖拽见 short_video.js bindStage4Sort
    move_btns = "" if split_from is not None else (
        f'<button type="button" class="slirn-btn-mini" data-action="sv-stage4-line-move" '
        f'data-dir="up" data-seg-id="{esc(sid)}" data-hl-index="{hl_index}" '
        f'title="上移一行（顺序自动保存，粗剪合成按此顺序剪切拼接）">↑</button>'
        f'<button type="button" class="slirn-btn-mini" data-action="sv-stage4-line-move" '
        f'data-dir="down" data-seg-id="{esc(sid)}" data-hl-index="{hl_index}" '
        f'title="下移一行（顺序自动保存，粗剪合成按此顺序剪切拼接）">↓</button>'
    )
    # REQ-20261019-210：data-src-index = 重排白名单键（原始 src_index）；
    # split 子行带 data-split-from（JS 拖拽/收序跳过）；可排序行 draggable
    split_attr = (f' data-split-from="{int(split_from)}"'
                  if split_from is not None else "")
    drag_attr = "" if split_from is not None else ' draggable="true"'
    # REQ-20261019-214：eff_idx → data-eff-idx（预览行定位键）；未删行可点击
    # 从本行起播（title 提示；删除行/LLM 删除行不参与播放）
    eff_attr = f' data-eff-idx="{int(eff_idx)}"' if eff_idx is not None else ""
    row_title = "" if is_marked else (
        ' title="点击从本行开始预览播放（在原视频上按字幕时间戳逐段跳播）；'
        '拖拽可调整顺序"')
    row_html = (
        f'<div class="{cls}" data-seg-id="{esc(sid)}" data-hl-index="{hl_index}" '
        f'data-src-index="{raw_src}"{split_attr}{drag_attr}{eff_attr}{row_title} '
        f'data-start-ms="{start_ms}" data-end-ms="{end_ms}" '
        f'data-orig-text="{esc(orig_text)}" data-eff-text="{esc(eff_text)}" '
        f'data-deleted="{1 if is_marked else 0}">'
        f'<span class="slirn-stage4-sub-no" title="本段在 effective 顺序里的位置（编辑后重编）">#{new_no}</span>'
        f'<span class="slirn-stage4-sub-srt" title="原始 SRT 行号（编辑/重排前固定）">@SRT#{display_src}</span>'
        f'<span class="slirn-stage4-sub-time" title="本段时间戳（来自原 SRT）">{time_range}</span>'
        f'<span class="slirn-stage4-sub-text">{esc(orig_text)}</span>'
        f'<span class="slirn-stage4-sub-actions">'
        f'{badge}'
        f'{move_btns}'
        f'<button type="button" class="slirn-btn-mini" data-action="sv-stage4-line-edit" '
        f'data-seg-id="{esc(sid)}" data-hl-index="{hl_index}" '
        f'title="修改文字（原文保留，行下方输入框编辑）">✏️</button>'
        f'{resplit_btn}'
        f'{restore_btn}'
        f'<button type="button" class="slirn-btn-mini" data-action="sv-stage4-line-toggle" '
        f'data-seg-id="{esc(sid)}" data-hl-index="{hl_index}" '
        f'title="{"恢复" if is_marked else "删除"}本行">'
        f'{"✚" if is_marked else "🗑️"}</button>'
        f'</span>'
        f'</div>'
    )
    # REQ-20261017-153：修改/剪词行的内嵌输入框（紧跟主行下方；服务端渲染
    # 已持久化的修改，✏️/✂️ 点击的「新框」由 JS 就地创建，结构一致）
    if not box_mode or is_marked:
        return row_html
    box_label = "剪词后" if box_mode == "trim" else "修改后"
    box_html = (
        f'<div class="slirn-stage4-sub-editbox" data-mode="{box_mode}" '
        f'data-persisted="1" data-seg-id="{esc(sid)}" data-hl-index="{hl_index}">'
        f'<span class="slirn-stage4-sub-editbox-label">{box_label}</span>'
        f'<input type="text" class="slirn-stage4-sub-input" '
        f'value="{esc(eff_text)}" data-orig-text="{esc(orig_text)}">'
        f'<button type="button" class="slirn-btn-mini slirn-btn-mini-primary" '
        f'data-action="sv-stage4-line-save" data-mode="{box_mode}" '
        f'data-seg-id="{esc(sid)}" data-hl-index="{hl_index}" '
        f'title="保存（预览/粗剪用修改后文字）">💾</button>'
        f'<button type="button" class="slirn-btn-mini" '
        f'data-action="sv-stage4-line-discard" data-mode="{box_mode}" '
        f'data-seg-id="{esc(sid)}" data-hl-index="{hl_index}" '
        f'title="还原为原文（去掉本条修改）">✖</button>'
        f'</div>'
    )
    return row_html + box_html


def _render_stage4_llm_deleted_row(hl_index: int, dl: dict) -> str:
    """REQ-20261016-146：LLM 删除行 — 保留展示（删除线 + 🗑 LLM已删 徽标，无编辑按钮）。"""
    sid = int(dl.get("src_index") or 0)
    text = str(dl.get("text") or "")
    start_ms = int(dl.get("start_ms") or 0)
    end_ms = int(dl.get("end_ms") or start_ms)
    time_range = f"{_fmt_srt_time(start_ms)} → {_fmt_srt_time(end_ms)}"
    return (
        f'<div class="slirn-stage4-sub-row slirn-stage4-sub-row-deleted" '
        f'data-hl-index="{hl_index}" data-src-index="{sid}" data-llm-deleted="1">'
        f'<span class="slirn-stage4-sub-no" title="LLM 删除行，不参与编号">—</span>'
        f'<span class="slirn-stage4-sub-srt" title="原始 SRT 行号">@SRT#{sid}</span>'
        f'<span class="slirn-stage4-sub-time">{time_range}</span>'
        f'<span class="slirn-stage4-sub-text">{esc(text)}</span>'
        f'<span class="slirn-stage4-sub-actions">'
        f'<span class="slirn-sv-badge slirn-sv-badge-failed">🗑 LLM已删</span>'
        f'</span>'
        f'</div>'
    )


def _render_stage4_clip_card(hl_index: int, hl: dict, per_hl: dict,
                              effective: list[dict],
                              llm_deleted: list[dict] | None = None,
                              can_reopt: bool = False) -> str:
    """渲染一条 highlight 的编辑卡（标题 + sub 列表 + 预览/初剪按钮）。

    REQ-20261016-146：llm_deleted（本轮 LLM 删除行）默认按原 src 顺序就地穿插
    （track-changes 式，能看到完整优化情况）；若用户手动设置了 line_order 重排，
    则追加在行列表尾部（不打乱自定义顺序）。

    REQ-20261017-155：``can_reopt``（= 本项目已跑过整体优化）时按钮区加
    「🔄 重新优化」— 只对该条从 Stage 3 原表重跑 LLM，其余条不动。
    """
    title = esc(hl.get("title") or f"片段 {hl_index}")
    head = esc(hl.get("title") or f"片段 {hl_index}")
    llm_deleted = [dict(d) for d in (llm_deleted or [])]

    def _src_key(sl: dict) -> int:
        split_from = sl.get("split_from")
        return int(split_from) if split_from is not None else int(sl.get("src_index") or 0)

    if llm_deleted and per_hl.get("line_order"):
        # 手动重排过的顺序优先；删除行追加尾部 + 分隔说明
        sub_rows = "".join(
            _render_stage4_sub_row(hl_index, s, per_hl, i)
            for i, s in enumerate(effective))
        sub_rows += ('<div class="slirn-sv-muted">── LLM 删除的行 '
                     f'（{len(llm_deleted)}）──</div>')
        sub_rows += "".join(
            _render_stage4_llm_deleted_row(hl_index, d) for d in llm_deleted
        )
    elif llm_deleted:
        # 就地穿插：删除行插到「第一个 src 更大的有效行」之前；
        # 有效行保持 effective 顺序不动（REQ-20261019-210：显示序必须 ==
        # 有效序，否则拖拽/推行按 DOM 收序会把展示排序的偏移带进 line_order
        # —— 此前 merged.sort 按 src 全排，src=0 的行会被顶到最前但显示 #N）
        merged: list[tuple[dict, bool]] = [(s, True) for s in effective]
        for d in llm_deleted:
            dk = _src_key(d)
            pos = len(merged)
            for i, (s, is_eff) in enumerate(merged):
                if is_eff and _src_key(s) > dk:
                    pos = i
                    break
            merged.insert(pos, (d, False))
        # REQ-20261019-214：eff_idx 只对有效行递增（= effective 下标），
        # LLM 删除行穿插不带 —— data-eff-idx 是预览行定位键
        _eff_i = 0
        parts: list[str] = []
        for s, is_eff in merged:
            if is_eff:
                parts.append(_render_stage4_sub_row(hl_index, s, per_hl, _eff_i))
                _eff_i += 1
            else:
                parts.append(_render_stage4_llm_deleted_row(hl_index, s))
        sub_rows = "".join(parts)
    else:
        sub_rows = "".join(
            _render_stage4_sub_row(hl_index, s, per_hl, i)
            for i, s in enumerate(effective))
    if not sub_rows:
        sub_rows = '<div class="slirn-sv-muted">（无有效字幕行）</div>'
    # REQ-20261017-155：已优化过的项目才显示单条「🔄 重新优化」
    reopt_btn = (
        f'<button type="button" class="slirn-btn-mini slirn-stage4-reopt-btn" '
        f'data-action="sv-stage4-optimize-one" data-hl-index="{hl_index}" '
        f'title="只对该条从 Stage 3 原表重新跑大模型优化（其余条不变），'
        f'粗剪及之后作废">🔄 重新优化</button>'
        if can_reopt else ""
    )
    return f'''<div class="slirn-stage4-clip" data-hl-index="{hl_index}">
  <div class="slirn-stage4-clip-head">
    <span class="slirn-stage4-clip-no">#{hl_index}</span>
    <span class="slirn-stage4-clip-title">{head}</span>
  </div>
  <div class="slirn-stage4-clip-subs">{sub_rows}</div>
  <div class="slirn-sv-muted slirn-stage4-reorder-hint">↕ 拖拽字幕行或点 ↑↓ 调整顺序，每次调整自动保存；粗剪合成按此顺序剪切拼接视频与字幕</div>
  <div class="slirn-stage4-clip-actions">
    <button type="button" class="slirn-btn-mini" data-action="sv-stage4-preview"
      data-hl-index="{hl_index}" title="在原视频上按字幕时间戳逐段跳转预览（不生成文件）；播放到的行会高亮，点击任意字幕行可从该行起播">▶ 预览该高亮</button>
    {reopt_btn}
  </div>
</div>'''


def _stage4_optimize(project: dict, *, task_id: str = "", project_id: str = "",
                      repo_root: str | Path | None = None) -> str:
    """REQ-20261013-100 + 108 + REQ-20261016-146：Stage 4「内容优化」。

    - 顶部：「✨ 内容优化」按钮（只用内置系统提示词，REQ-20261016-146 起无提示词 UI）
      + 每条 highlight 统计 + warnings。
    - 下部：每条 highlight 一张编辑卡（含 sub 行 + ✏️/✂️/🗑️ 按钮 + 预览/初剪按钮）；
      LLM 删除的行按原位置保留展示（🗑 LLM已删），手动标删的行也保留可见（🗑 已删）。
    - 用户手动编辑只写 per-hl JSON，不污染 stage3/highlights.json。
    """
    s4o = project.get("pipeline", {}).get("stage4_optimize", {}) or {}
    s3 = project.get("pipeline", {}).get("stage3_analyze", {}) or {}
    status = s4o.get("status") or "pending"
    hl_rows = s4o.get("highlights") or []
    warnings = s4o.get("warnings") or []
    error_html = ""
    if status == "failed" and not hl_rows:
        err = s4o.get("error") or "内容优化失败"
        error_html = f'<div class="slirn-stage-error">{esc(err)}</div>'

    # 是否允许运行：Stage 3 必须 done 且至少有 1 条 highlight
    s3_done = s3.get("status") == "done"
    # REQ-20261017-149：底稿优先读 Stage 4 分离存储（stage4 done →
    # optimized_highlights.json 优化表；否则回退 Stage 3 原表 = 优化前预览）
    root: Path | None = None
    if repo_root:
        try:
            root = Path(str(repo_root))
        except Exception:  # noqa: BLE001
            root = None
    hl_list = s3.get("highlights") or []
    if root and task_id and project_id:
        try:
            base_doc = _sv_opt.load_stage4_base_doc(root, task_id, project_id,
                                                    project=project)
            hl_list = (base_doc or {}).get("highlights") or hl_list
        except Exception:  # noqa: BLE001
            pass
    can_run = s3_done and bool(hl_list)
    btn_label = "✨ 重新优化" if status == "done" else "✨ 内容优化"
    btn_class = "slirn-btn" if status == "done" else "slirn-btn slirn-btn-primary"
    disabled_attr = "" if can_run else " disabled title=\"请先完成 Stage 3 内容提取\""
    run_btn = (
        f'<button id="slirn-sv-stage4-run-btn" class="{btn_class}" '
        f'data-action="sv-stage4-optimize-run"{disabled_attr}>'
        f'{btn_label}</button>'
    )

    # REQ-20261013-111：Stage 4 重新优化进度卡（仿 stage5/7 status-card 结构）
    stage4_status_card = (
        f'<div class="slirn-rc-status-card" id="slirn-sv-stage4-status" '
        f'data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}" '
        f'style="display:none;">'
        f'<div class="slirn-sub-meta">🔄 内容优化进度</div>'
        f'<div class="slirn-progress" style="margin-top:6px;">'
        f'<div class="slirn-progress-bar slirn-sv-stage4-bar" style="width:0%;"></div>'
        f'</div>'
        f'<div class="slirn-status-msg slirn-sv-stage4-msg" style="display:none;"></div>'
        f'</div>'
    )

    # REQ-20261016-146：本轮 LLM 删除行（按 index 对齐）— 渲染时保留展示
    s4_rows_by_index: dict[int, dict] = {}
    for _r in hl_rows:
        try:
            s4_rows_by_index[int(_r.get("index") or 0)] = _r
        except (TypeError, ValueError):
            continue

    # 每条短视频的优化统计行
    detail_rows = []
    for hl in hl_rows:
        idx = hl.get("index") or len(detail_rows) + 1
        st = hl.get("status") or "pending"
        badge_cls = {
            "ok": "slirn-sv-badge-ok",
            "kept_original": "slirn-sv-badge-warning",
            "failed": "slirn-sv-badge-failed",
        }.get(st, "slirn-sv-badge-muted")
        badge_text = {
            "ok": "✅ 已优化",
            "kept_original": "⚠️ 保留原行",
            "failed": "❌ 失败",
        }.get(st, st)
        detail_rows.append(
            f'<div class="slirn-stage4-opt-row">'
            f'<span class="slirn-sv-row-no">#{idx}</span>'
            f'<span class="slirn-sv-badge {badge_cls}">{badge_text}</span>'
            f'<span class="slirn-sv-muted">'
            f'{int(hl.get("lines_before") or 0)} 行 → {int(hl.get("lines_after") or 0)} 行 · '
            f'保留 {int(hl.get("kept") or 0)} · 修改 {int(hl.get("fixed") or 0)} · '
            f'切分 {int(hl.get("split") or 0)} · 删除 {int(hl.get("deleted") or 0)}'
            f'</span>'
            f'</div>'
        )
    detail_html = "".join(detail_rows)
    warn_html = ""
    if warnings:
        items = "".join(f'<li>{esc(w)}</li>' for w in warnings[:8])
        more = f'<li>… 其余 {len(warnings) - 8} 条见日志</li>' if len(warnings) > 8 else ""
        warn_html = f'<ul class="slirn-stage4-opt-warns">{items}{more}</ul>'

    # 每条 highlight 的编辑卡（REQ-20261013-108 + REQ-20261013-109 tab 化）
    # tab 化后：tab bar 顶部 + 每个 highlight 套一层 .slirn-stage4-hl-pane，
    # 第 1 个 tab / pane 加 active；切换由前端 sv-stage4-hl-tab action 触发
    # switchHlTab（共用 stage3 一套 CSS + 状态记忆 window._slirnSvHlTab）。
    clip_tabs: list[str] = []
    clip_panes: list[str] = []
    if root and task_id and project_id and hl_list:
        for i, hl in enumerate(hl_list, start=1):
            try:
                per_hl = _sv_opt.load_per_hl(root, task_id, project_id, i)
                # REQ-20261016-146：渲染口径 include_deleted=True — 手动标删行保留可见
                effective = _sv_opt.effective_subtitle_lines(
                    root, task_id, project_id, i, include_deleted=True)
            except Exception:  # noqa: BLE001
                per_hl = {}
                effective = []
            llm_deleted = (s4_rows_by_index.get(i) or {}).get("deleted_lines") or []
            title = hl.get("title") or f"片段 {i}"
            is_first = len(clip_panes) == 0
            _tab_label = title if len(title) <= 10 else title[:10] + "…"
            clip_tabs.append(
                f'<button class="slirn-stage4-hl-tab{" active" if is_first else ""}" '
                f'data-action="sv-stage4-hl-tab" data-hl-index="{i}" '
                f'title="{esc(title)}">#{i} {esc(_tab_label)}</button>'
            )
            clip_panes.append(
                f'<div class="slirn-stage4-hl-pane{" active" if is_first else ""}" '
                f'data-hl-index="{i}">'
                f'{_render_stage4_clip_card(i, hl, per_hl, effective, llm_deleted, can_reopt=(status == "done"))}'
                f'</div>'
            )
    if clip_panes:
        clip_html = (
            '<div class="slirn-stage4-clips">'
            '<div class="slirn-stage4-hl-tabs">' + "".join(clip_tabs) + '</div>'
            + "".join(clip_panes) +
            '</div>'
        )
    else:
        clip_html = '<div class="slirn-stage4-clips">（无 highlight，请先完成 Stage 3）</div>'

    return f'''<div class="slirn-stage" data-stage="4">
  <div class="slirn-stage-head">
    <span class="slirn-stage-num">4</span>
    <strong>内容优化</strong>
    {_stage_badge(status)}
    {_stage_elapsed(s4o)}
  </div>
  <div class="slirn-stage-body">
    <div class="slirn-sv-muted slirn-stage4-opt-help">
      把 Stage 3 每条短视频的字幕完整交给大模型自动优化（内置优化规则，无需录入提示词）：
      <strong>按语义切分长行</strong>（子句拼接须逐字等于原文，时间戳按占比估算）、
      <strong>修正与音频内容不符的文字</strong>（去口水/错字）、<strong>删除无用行</strong>
      （删除的行仍保留在下方展示，标「🗑 LLM已删」）。
      结果写回 Stage 3 字幕（首次优化前自动备份），下方可逐条手动微调；拼接与后续阶段将作废重跑。<br>
      手动微调说明：✂️ <strong>重新切分</strong> = 行内剪词（删掉无用的词；预览/粗剪时，
      删掉的字对应的视频片段会被自动切掉，↩️ 可恢复整行）；✏️ <strong>文字修改</strong>
      只改显示的字幕文字，<strong>不</strong>对视频做任何操作。
    </div>

    <div class="slirn-sv-card-actions">
      {run_btn}
    </div>
    {stage4_status_card}
    {error_html}
    {_stage4_detail(detail_html, warn_html)}
    {_stage_logs(s4o)}

    <div class="slirn-stage4-section-title">逐条编辑（手动覆盖：仅本项目，不回写 Stage 3）</div>
    {clip_html}
  </div>
</div>'''


def _stage5_coarse(project: dict) -> str:
    """Stage 5 粗剪 UI（REQ-20261013-103）：仿长视频 compose_rough 顶部状态卡 +
    异步重新合成 + 删除 + 连续 SRT 复制/下载；每条 clip 卡片内加单条 SRT 操作。

    REQ-20261017-154：clip 改**行列表**——每条短视频一行，标题后紧跟现有按钮
    （▶ 预览 / 下载视频 / 复制 SRT / 下载 SRT）；异常信息在行下方折叠层
    （<details>，可折叠；有异常时服务端带 open 自动展开）。
    """
    task_id = str(project.get("task_id") or "")
    project_id = str(project.get("id") or "")
    s4 = project.get("pipeline", {}).get("stage5_coarse", {}) or {}
    has_artifact = bool(s4.get("highlights"))
    total_sec = float(s4.get("total_duration_sec") or 0)
    count_n = int(s4.get("count") or 0)

    # ---- 顶部状态卡（仿长视频 _render_rough_compose_zone L1200-1216）----
    stats_line = (
        f'{count_n} 条 · 总时长 {total_sec:.1f}s'
        if has_artifact
        else '尚未合成'
    )
    # progress 条：状态 done/failed → 100/0；其他 → 占位 0（异步轮询会持续更新）
    s4_status = str(s4.get("status") or "pending")
    progress_pct = 100 if s4_status == "done" else (0 if s4_status != "running" else 30)
    progress_color = (
        "slirn-progress-bar--done" if s4_status == "done"
        else ("slirn-progress-bar--failed" if s4_status == "failed" else "")
    )
    compose_btn_text = "🎬 重新合成" if has_artifact else "🎬 粗剪合成"
    delete_btn = (
        f'<button class="slirn-btn slirn-btn-danger" data-action="sv-stage5-delete" '
        f'data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}" '
        f'style="margin-left:8px;">🗑️ 删除粗剪成品</button>'
        if has_artifact else ""
    )
    # ---- 连续 SRT 复制/下载（仅在有产物时显示）----
    subs_actions = ""
    if has_artifact:
        subs_actions = f'''<div class="slirn-sv-card-actions" style="margin-top:8px;">
      <button class="slirn-btn slirn-btn-sm" data-action="sv-stage5-subs-copy"
              data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}">📋 复制连续 SRT</button>
      <button class="slirn-btn slirn-btn-sm" data-action="sv-stage5-subs-download"
              data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}">⬇️ 下载连续 SRT</button>
    </div>'''
    status_card = f'''<div class="slirn-rc-status-card" id="slirn-sv-stage5-status"
        data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}">
      <div class="slirn-sub-meta">🎞️ 粗剪成片 · {esc(stats_line)}</div>
      <div class="slirn-progress" style="margin-top:6px;">
        <div class="slirn-progress-bar {progress_color}"
             style="width:{progress_pct}%;"></div>
      </div>
      <div class="slirn-status-msg slirn-sv-stage5-msg" style="display:none;"></div>
    </div>'''

    # ---- clip 行列表（REQ-20261017-154：每条短视频一行，标题后跟现有按钮）----
    rows = []
    for hl in (s4.get("highlights") or []):
        idx = hl.get("index") or len(rows) + 1
        mp4_name = Path(str(hl.get("coarse_mp4") or "")).name
        srt_name = Path(str(hl.get("coarse_srt") or "")).name
        url = _file_url(task_id, project_id, "stage4", mp4_name) if mp4_name else ""
        srt_url = _file_url(task_id, project_id, "stage4", srt_name) if srt_name else ""
        # REQ-20261019-227：成片字幕（按合成后视频 ASR + 用户改字）。存在 →
        # 「提取字幕」变展开切换键，预览/下载/精剪全部优先此文件
        subs_name = Path(str(hl.get("subs_srt") or "")).name
        subs_url = _file_url(task_id, project_id, "stage5", subs_name) if subs_name else ""
        dur = float(hl.get("duration_sec") or 0)
        err = hl.get("error") or ""
        status = hl.get("status") or "pending"
        # REQ-20261019-221：该条字幕在 Stage 4 改过 → 旧粗剪已作废（产物
        # 引用清空）。行内给「🎬 合成」单条入口 + 原因提示；其余条不受影响。
        stale_reason = str(hl.get("stale_reason") or "").strip()
        # 标题来自 stage3 highlights.title —— 无标题时用 `片段 #N` 占位（永不为空）
        raw_title = str(hl.get("title") or "").strip()
        title_text = raw_title if raw_title else f"片段 #{idx}"
        # 现有按钮全部收进行内（标题后面）；无产物时不渲染对应按钮
        # （状态徽标已表达「未生成」，异常原因见行下方折叠层）
        actions = []
        if url:
            # REQ-20261017-155：单条重新合成（复用 hl_index 单条模式端点，
            # 其余条粗剪产物保留；该条精剪作废重跑）
            actions.append(
                f'<button type="button" class="slirn-btn-mini slirn-stage5-recompose-btn" '
                f'data-action="sv-stage5-export-one" data-clip-index="{idx}" '
                f'data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}" '
                f'title="只重新合成该条粗剪成片（其余条保留），该条精剪作废重跑">'
                f'🔄 重新合成</button>'
            )
            # REQ-20261019-215：预览按钮带 data-srt-url —— 浮窗下方字幕条跟随
            # 播放滚动 + 当前行高亮。REQ-20261019-227：已提取成片字幕时优先
            # subs_NN.srt（ASR+改字版），否则粗剪自产 SRT；无 SRT 时省略属性
            eff_srt_url = subs_url or srt_url
            srt_attr = f' data-srt-url="{esc(eff_srt_url)}"' if eff_srt_url else ""
            actions.append(
                f'<button type="button" class="slirn-btn-mini slirn-stage5-preview-btn" '
                f'data-action="sv-stage5-preview" data-url="{esc(url)}"{srt_attr} '
                f'data-clip-index="{idx}" data-clip-name="#{esc(str(idx))}">'
                f'▶ 预览</button>'
            )
            # REQ-20261019-225：粗剪重跑是同名文件原地替换，href 必须带渲染期
            # _ts 防浏览器缓存旧片；download 属性给显式文件名（_ts 不入文件名）
            actions.append(
                f'<a class="slirn-btn-mini" href="{esc(url)}&_ts={int(time.time())}" '
                f'download="{esc(mp4_name)}">下载视频</a>'
            )
            # REQ-20261019-227：按合成后的视频提取字幕（ASR）；已提取 → 变成
            # 展开/收起编辑器切换键（改字、原文保留、↩ 还原）
            if subs_url:
                actions.append(
                    f'<button type="button" class="slirn-btn-mini slirn-stage5-subs-btn" '
                    f'data-action="sv-stage5-subs-toggle" data-clip-index="{idx}" '
                    f'data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}" '
                    f'title="展开/收起本条字幕（可改文字，原文保留）">📝 字幕</button>'
                )
                # REQ-20261019-229：已提取的条也给重提入口（LLM 整理升级后
                # 旧字幕想刷新，不必为此重合成粗剪；重提会清旧改字）
                actions.append(
                    f'<button type="button" class="slirn-btn-mini slirn-stage5-subs-btn" '
                    f'data-action="sv-stage5-subs-reextract" data-clip-index="{idx}" '
                    f'data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}" '
                    f'title="按合成后的视频重新提取字幕（LLM 断句+标点；原有改字会清空）">'
                    f'🔄 重新提取</button>'
                )
            else:
                actions.append(
                    f'<button type="button" class="slirn-btn-mini slirn-stage5-subs-btn" '
                    f'data-action="sv-stage5-subs-extract" data-clip-index="{idx}" '
                    f'data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}" '
                    f'title="按合成后的视频跑 ASR 提取字幕（可在下方展开修改）">'
                    f'📝 提取字幕</button>'
                )
        elif stale_reason:
            # REQ-20261019-221：作废行（字幕改过）— 单条合成入口
            # （产物引用已清空 → 不给预览/下载死链，重新合成后按钮恢复）
            actions.append(
                f'<button type="button" class="slirn-btn-mini slirn-stage5-recompose-btn" '
                f'data-action="sv-stage5-export-one" data-clip-index="{idx}" '
                f'data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}" '
                f'title="该条字幕已修改，重新合成后才可用于精剪（其余条不受影响）">'
                f'🎬 合成</button>'
            )
        if srt_url:
            # REQ-20261019-227：行内「📋 复制 SRT」按钮按需求移除（顶部连续
            # SRT 两键保留）；下载仍走 JS（优先 subs 改字版，见 stage5ClipSubsDownload）
            actions.append(
                f'<button class="slirn-btn-mini" data-action="sv-stage5-clip-subs-download" '
                f'data-clip-index="{idx}" data-task-id="{esc(task_id)}" '
                f'data-project-id="{esc(project_id)}">⬇ 下载 SRT</button>'
            )
        # REQ-20261017-154：异常折叠层 —— 行下方、可折叠；有异常自动展开（open）
        err_html = (
            f'<details class="slirn-stage5-row-error" open>'
            f'<summary>⚠️ 异常信息</summary>'
            f'<div class="slirn-stage5-row-error-body">{esc(err)}</div>'
            f'</details>'
            if err else ""
        )
        # REQ-20261019-227：字幕编辑器容器（行下方、默认收起）——JS 首次展开
        # 时 POST /short_video_subs_get 拉取 cues 现渲染，不占首屏
        subs_editor = (
            f'<div class="slirn-stage5-subs-editor" data-clip-index="{idx}" '
            f'data-loaded="0" hidden></div>'
            if subs_url else ""
        )
        rows.append(
            f'<div class="slirn-stage5-row" data-clip-index="{idx}">'
            f'<div class="slirn-stage5-row-main">'
            f'<span class="slirn-sv-row-no">#{idx}</span>'
            f'<span class="slirn-stage5-row-title" title="{esc(title_text)}">{esc(title_text)}</span>'
            f'{_stage_badge(status)}'
            + (f'<span class="slirn-sv-muted">{esc(stale_reason)}</span>'
               if stale_reason else f'<span class="slirn-sv-muted">{dur:.1f}s</span>')
            + f'<span class="slirn-stage5-row-actions">{"".join(actions)}</span>'
            f'</div>'
            f'{subs_editor}'
            f'{err_html}'
            f'</div>'
        )

    # REQ-20261013-117：空列表提示按上游真实状态分流。旧版只要没点过
    # 「合成粗剪」就一律显示「先完成 Stage 3 再粗剪」——Stage 3 早已 done
    # 时文案在误导（用户截图反馈）。Stage 3 done → 引导点合成按钮。
    s3_done = str((project.get("pipeline", {}).get("stage3_analyze") or {}).get("status")) == "done"
    empty_hint_div = (
        '<div class="slirn-sv-muted">尚无粗剪成品 — 点击上方「🎬 粗剪合成」生成</div>'
        if s3_done else
        '<div class="slirn-sv-muted">先完成 Stage 3 解析后再粗剪</div>'
    )

    return f'''<div class="slirn-stage" data-stage="5">
  <div class="slirn-stage-head">
    <span class="slirn-stage-num">5</span>
    <strong>粗剪合成</strong>
    {_stage_badge(s4.get("status"))}
    {_stage_elapsed(s4)}
    <span class="slirn-sv-muted">{count_n} 条</span>
  </div>
  <div class="slirn-stage-body">
    {status_card}
    <div class="slirn-sv-card-actions" style="margin-top:10px;">
      <button class="slirn-btn slirn-btn-primary" data-action="sv-stage5-export"
              data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}">{compose_btn_text}</button>
      {delete_btn}
    </div>
    {subs_actions}
    <div class="slirn-stage5-row-list">{"".join(rows) or empty_hint_div}</div>
    {_stage_logs(s4)}
  </div>
</div>'''
# REQ-20261016-148：Stage 6 字幕优化面板（_stage6_refine + _render_stage6_srt_rows）
# 已随阶段整体移除；精剪面板上移为 Stage 6（_stage6_fine）。


def _stage6_fine(project: dict) -> str:
    """Stage 6 精剪混编（REQ-20261017-157 区域重构）。

    两段布局：
    1. 每条短视频一个处理区（``slirn-stage6-list``）：标题/视频/字幕自动
       继承 Stage 5（可改标题、手动上传视频和字幕替换），🎬 生成预览 +
       💾 导出最终视频 融入卡内（每条自己的事，不是公共资源）。
    2. 公共素材和参数设置区域（一份，全部条共用）：上传素材（4 项，不含
       视频/字幕槽）+ 参数导入导出 + 保存参数为模板 + 背景检测/画布/
       字体/裁剪/片头封面/BGM/输出设置。

    per-clip 覆盖（标题/视频/字幕）落 ``pipeline.stage6_fine.clips``；
    短/长隔离：params 落 project.json → stage6_fine.params；模板落
    tasks/_global_short_video_fine_profiles.json（不写长视频）。
    """
    task_id = str(project.get("task_id") or "")
    project_id = str(project.get("id") or "")
    root = Path(str(project.get("_repo_root") or "."))
    s6 = project.get("pipeline", {}).get("stage6_fine", {}) or {}
    s5 = project.get("pipeline", {}).get("stage5_coarse", {}) or {}
    p = s6.get("params") or {}
    font = p.get("font") or {}
    output = p.get("output") or {}
    audio = p.get("audio") or {}
    preview = p.get("preview") or {}
    layout = p.get("layout") or {}
    materials = p.get("materials") or {}
    detected = p.get("detected_region")
    res = output.get("resolution") or "720p"
    audio_enabled = "checked" if audio.get("enabled") else ""
    preview_dur = int(preview.get("duration_sec") or 5)

    # ---- 异步状态卡（仿 Stage 5 顶部卡） ----
    s5_hls = s5.get("highlights") or []
    status_card = f'''<div class="slirn-rc-status-card" id="slirn-sv-stage7-status"
        data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}">
      <div class="slirn-sub-meta">🎞️ 精剪混编 · {esc(res)} · {len(s5_hls)} 条</div>
      <div class="slirn-progress" style="margin-top:6px;">
        <div class="slirn-progress-bar" style="width:0%;"></div>
      </div>
      <div class="slirn-status-msg slirn-sv-stage7-msg" style="display:none;"></div>
    </div>'''

    # ---------- 公共素材和参数设置区域（一份，全部 clip 共用） ----------

    # REQ-20261017-157：video/subtitle 移出公共素材（每条短视频自己管理）
    # REQ-20261017-159：每槽加 📂 上传（此前只有文本框，用户无法上传素材）
    # REQ-20261018-171：每槽再加 👁 查看 —— 已上传文件浮窗预览（图片/音频）
    def _mat_preview_url(val: str) -> tuple[str, str]:
        """素材值 → (kind, 文件名)，仅项目内 stage2-6 目录可预览。

        上传素材落 stage6/mat_<slot>.<ext>；粗剪等继承素材可能在 stage4。
        返回 ("", "") 表示不可预览（手输外部路径等）。
        """
        v = str(val or "").strip().replace("\\", "/")
        marker = f"tasks/{task_id}/short_video/{project_id}/"
        if not v.startswith(marker):
            return "", ""
        parts = v[len(marker):].split("/")
        if len(parts) != 2 or parts[0] not in (
                "stage2", "stage3", "stage4", "stage5", "stage6"):
            return "", ""
        name = parts[1]
        if not name or name in (".", ".."):
            return "", ""
        return parts[0], name

    mat_rows = []
    for slot, label, hint, val in [
        ("cover",     "🖼 封面",     "可选 · 顶部封面图",      materials.get("cover", "")),
        ("bg",        "🎨 背景",     "可选 · 背景图 · AI 布局输入", materials.get("bg", "")),
        ("audio",     "🎵 附加音频", "可选 · 独立音轨",       materials.get("audio", "")),
        # REQ-20261017-164：导师头像（配参数区的导师昵称/标题文字设置）
        ("avatar",    "👤 导师头像", "可选 · 导师头像图",      materials.get("avatar", "")),
    ]:
        accept = "audio/*" if slot == "audio" else "image/*"
        media = "audio" if slot == "audio" else "image"
        pk, pname = _mat_preview_url(val)
        view_btn = ""
        if pk:
            view_btn = (
                f'<button type="button" class="slirn-btn-mini slirn-sv-mat-view" '
                f'data-action="sv-mat-preview" data-media="{media}" '
                f'data-url="/slirn/api/short_video_file?task_id={esc(task_id)}'
                f'&amp;project_id={esc(project_id)}&amp;kind={esc(pk)}'
                f'&amp;name={esc(pname)}" '
                f'title="查看已上传的 {esc(pname)}">👁 查看</button>'
            )
        mat_rows.append(
            f'<div class="slirn-sv-mat-row" data-slot="{esc(slot)}" '
            f'title="点击 📂 上传 选择文件，或把{esc(label[2:])}文件拖拽到本行上传；'
            f'已上传可 👁 查看预览">'
            f'<label class="slirn-sv-mat-label">{esc(label)}</label>'
            f'<input type="text" class="slirn-sv-mat-input" data-mat-slot="{esc(slot)}" '
            f'value="{esc(str(val))}" placeholder="{esc(hint)}" '
            f'style="flex:1; min-width:200px; padding:4px 8px;">'
            f'{view_btn}'
            f'<label class="slirn-btn-mini slirn-stage6-clip-mat-upload" '
            f'title="上传{esc(label[2:])}文件（公共，全部短视频共用）">📂 上传'
            f'<input type="file" class="slirn-sv-mat-upload" hidden '
            f'data-mat-slot="{esc(slot)}" accept="{esc(accept)}">'
            f'</label>'
            f'</div>'
        )
    # REQ-20261018-191：data-collapse-key 供 JS 记忆折叠状态（server 默认
    # 仍渲染展开，用户收起过则 localStorage 恢复收起）
    upload_html = (
        f'<details class="slirn-fine-section" data-collapse-key="sv_mat_section" open>'
        f'<summary class="slirn-fine-section-summary">📁 上传素材（4 项 · 公共）</summary>'
        f'<div class="slirn-sv-mat-grid">{"".join(mat_rows)}</div>'
        f'</details>'
    )
    # 背景图区域检测（公共参数；REQ-20261018-166 对齐长视频：
    # 算法可选 center_expand 中心扩展 / pixel 白扫 + 阈值 + 4 角点结果卡）
    has_bg = bool(str(materials.get("bg") or "").strip())
    det = detected if isinstance(detected, dict) else {}
    _has_det = bool(det)
    _det_algo = str(det.get("algorithm") or "")



    def _det_val(v) -> str:
        return "—" if v is None else str(v)


    def _det_corner(name: str) -> str:
        c = (det.get("corners") or {}).get(name) if isinstance(det.get("corners"), dict) else None
        if not c or not isinstance(c, list) or len(c) != 2:
            return "—"
        return f"({c[0]}, {c[1]})"


    if not has_bg:
        _bg_badge = '<span class="slirn-fine-section-status">⏳ 未上传背景图</span>'
    elif _has_det and det.get("width"):
        _bg_badge = (f'<span class="slirn-fine-section-status">✅ 已检测 '
                     f'{_det_val(det.get("width"))} × {_det_val(det.get("height"))}</span>')
    else:
        _bg_badge = '<span class="slirn-fine-section-status">⚠️ 未检测</span>'
    _det_color = det.get("detected_color")
    _det_color_txt = ""
    if isinstance(_det_color, list) and len(_det_color) >= 3:
        _det_color_txt = (f"基准色 RGB({_det_color[0]},{_det_color[1]},{_det_color[2]})"
                          + ("（透明/抠图区）" if det.get("transparent_base") else ""))
    # REQ-20261018-170：AI 识别结果卡（params.ai_layout_result，检测区下方渲染）
    from . import short_video_bg_detect as _sv_bg
    ai_res = p.get("ai_layout_result")
    ai_res = ai_res if isinstance(ai_res, dict) else {}
    _ai_regions = [r for r in (ai_res.get("regions") or [])
                   if isinstance(r, dict)]
    # REQ-20261030-012：分析只出结果，不写参数区 —— 结果卡要能区分
    # 「已写入参数区」（用户点过填充）/「尚未写入」（还要手动点填充）
    _ai_applied = _sv_bg.is_result_applied(ai_res, layout)
    _ai_rows = [f'<span class="slirn-fine-bg-detect-key">🤖 AI 识别结果</span>'
                f'<span class="slirn-fine-bg-detect-val">{len(_ai_regions)} 个区域 · '
                f'{esc(str(ai_res.get("detected_at") or "—"))}'
                + (' · ⚠️ LLM 兜底默认' if ai_res.get("source") == "fallback" else '')
                + (' · ✅ 已写入参数区' if _ai_applied else '')
                + '</span>']
    for _r in _ai_regions:
        _extra = str(_r.get("purpose") or _r.get("reason") or "").strip()
        _ai_rows.append(
            f'<span class="slirn-fine-bg-detect-key">{esc(str(_r.get("name") or "未命名"))}</span>'
            f'<span class="slirn-fine-bg-detect-val">({_r.get("x")}, {_r.get("y")}) '
            f'{_r.get("w")} × {_r.get("h")}'
            + (f' · {esc(_extra)}' if _extra else '') + '</span>')
    if _ai_applied:
        _ai_hint = (f'识别结果<b>已写入参数区</b>'
                     f'（{esc(str(ai_res.get("applied_at") or ""))}）；'
                     f'再次「🤖 分析参数」后需重新点「✅ 填充到参数区域」才会更新。')
    else:
        _ai_hint = ('识别结果<b>尚未写入参数区</b>；'
                    '点上方「✅ 填充到参数区域」才会应用'
                    '（不会自动覆盖你已有的参数）。')
    ai_result_card = (
        f'<div class="slirn-fine-bg-detect-result" {"" if ai_res else "hidden"}>'
        f'<div class="slirn-fine-bg-detect-grid">{"".join(_ai_rows)}</div>'
        # REQ-20261030-012：分析只出结果，不写参数区 —— 明确提示要手动填充
        f'<div class="slirn-form-hint">{_ai_hint}</div>'
        f'<details style="margin:4px 0;">'
        f'<summary class="slirn-sv-muted" style="cursor:pointer;">生成的识别提示词</summary>'
        f'<pre style="white-space:pre-wrap;font-size:11px;line-height:1.5;'
        f'margin:4px 0;max-height:200px;overflow:auto;">'
        f'{esc(str(ai_res.get("prompt") or "—"))}</pre>'
        f'</details>'
        f'</div>'
    )
    bg_detect_block = (
        # REQ-20261018-191：data-collapse-key 折叠状态记忆（同上传素材块）
        f'<details class="slirn-fine-section" data-collapse-key="sv_bg_detect" open>'
        f'<summary class="slirn-fine-section-summary">🎨 背景图区域检测（4 角点 + 宽高）'
        f'{_bg_badge}</summary>'
        f'<div class="slirn-fine-bg-detect-block">'
        # REQ-20261018-169：说明挪进检测区内部、算法行上方 —— 先写检测指令
        # （给 AI：要检测哪些区域/各区域作用/期望内容），再选算法跑检测。
        # 类名/data 属性不变（自动保存 + 参数收集的 JS 钩子零改动）。
        f'<div class="slirn-sv-mat-desc-row">'
        f'<label class="slirn-sv-mat-label">📝 背景区检测说明</label>'
        f'<textarea class="slirn-sv-mat-desc" data-desc-slot="bg" rows="10" '
        f'placeholder="告诉 AI 要检测背景图的哪些区域、每个区域的作用是什么、'
        f'期望检出哪些内容。例如：中间透明抠图区用来放视频（要检出位置和尺寸）；'
        f'底部横条放字幕；右上角圆形位放导师头像；顶部留标题文字">'
        f'{esc(str(materials.get("bg_desc") or ""))}</textarea>'
        f'</div>'
        f'<div class="slirn-form-hint">这段说明是「🤖 分析参数」的检测指令；'
        f'输入停顿后自动保存。</div>'
        # REQ-20261018-175：两个主按钮挪到算法行上方 —— 原先埋在下方像素
        # 检测结果卡内部，没跑过检测时整卡 hidden，按钮跟着看不见。
        # data-action / 顺序不变（JS 派发零改动）。
        f'<div class="slirn-sv-card-actions slirn-sv-detect-actions" style="margin:8px 0 10px;">'
        f'<button class="slirn-btn-mini slirn-sv-detect-action slirn-sv-detect-action-ai" '
        f'data-action="sv-stage7-parse-layout" '
        f'title="读取背景区检测说明 → 生成识别提示词 → AI 识别说明里点名的区域，'
        f'结果显示在下方；只出结果、不动参数区（不会覆盖你已有的参数），'
        f'要写入参数区请点「✅ 填充到参数区域」">🤖 分析参数</button>'
        f'<button class="slirn-btn-mini slirn-sv-detect-action slirn-sv-detect-action-fill" '
        f'data-action="sv-stage7-apply-detected" '
        f'title="按识别结果的区域类型，把坐标填充到下方参数区对应的区域组'
        f'（视频/字幕/背景/封面）；无 AI 结果时用像素检测区域填充视频区域；'
        f'只覆盖这些组的坐标（X/Y/宽/高），组内其它设置保留；'
        f'这是唯一写入参数区的入口 ——「🤖 分析参数」只出结果，不会自动写入">✅ 填充到参数区域</button>'
        f'</div>'
        f'<div class="slirn-fine-bg-detect-controls">'
        f'<span class="slirn-fine-actions-label">算法</span>'
        f'<select class="slirn-sv-bg-detect-algo">'
        f'<option value="center_expand" {"selected" if _det_algo != "pixel" else ""}>🎯 中心扩展（默认；颜色未知/抠图时推荐）</option>'
        f'<option value="pixel" {"selected" if _det_algo == "pixel" else ""}>🔍 像素扫描白色（已知是白色）</option>'
        f'</select>'
        f'<span class="slirn-fine-bg-detect-threshold-row" '
        f'style="{"display:inline-flex" if _det_algo == "pixel" else "display:none"};align-items:center;gap:4px;">'
        f'<span class="slirn-fine-actions-label">阈值</span>'
        f'<select class="slirn-sv-bg-detect-threshold-sel">'
        f'<option value="255">纯白 (255)</option>'
        f'<option value="250" selected>近白 (250)</option>'
        f'<option value="240">宽松 (240)</option>'
        f'<option value="230">很宽松 (230)</option>'
        f'</select>'
        f'<input type="number" class="slirn-fine-num slirn-sv-bg-detect-threshold-num" '
        f'min="200" max="255" step="1" value="250" aria-label="手动输入阈值（200-255）">'
        f'</span>'
        f'<button class="slirn-btn-mini" data-action="sv-stage7-detect-bg" '
        f'{"disabled" if not has_bg else ""} '
        f'title="{esc("请先在素材区上传背景图") if not has_bg else esc("运行区域检测（检测背景图上放视频的区域）")}">'
        f'🔍 检测区域</button>'
        f'<button class="slirn-btn-mini" data-action="sv-stage7-detect-bg-clear" '
        f'{"disabled" if not _has_det else ""} title="清空检测缓存（之后可重新检测）">'
        f'🗑 清空缓存</button>'
        f'</div>'
        f'<div class="slirn-fine-bg-detect-result" {"" if _has_det else "hidden"}>'
        f'<div class="slirn-fine-bg-detect-grid">'
        f'<span class="slirn-fine-bg-detect-key">左上角 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val">{_det_corner("topleft")}</span>'
        f'<span class="slirn-fine-bg-detect-key">右上角 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val">{_det_corner("topright")}</span>'
        f'<span class="slirn-fine-bg-detect-key">左下角 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val">{_det_corner("bottomleft")}</span>'
        f'<span class="slirn-fine-bg-detect-key">右下角 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val">{_det_corner("bottomright")}</span>'
        f'<span class="slirn-fine-bg-detect-key">宽 × 高 (px)</span>'
        f'<span class="slirn-fine-bg-detect-val">{_det_val(det.get("width"))} × {_det_val(det.get("height"))}</span>'
        f'<span class="slirn-fine-bg-detect-key">中心 (X, Y)</span>'
        f'<span class="slirn-fine-bg-detect-val">{_det_val(det.get("center_x"))}, {_det_val(det.get("center_y"))}</span>'
        f'<span class="slirn-fine-bg-detect-key">像素数</span>'
        f'<span class="slirn-fine-bg-detect-val">{_det_val(det.get("pixel_count"))}</span>'
        f'<span class="slirn-fine-bg-detect-key">原图分辨率</span>'
        f'<span class="slirn-fine-bg-detect-val">{_det_val(det.get("image_native_w"))} × {_det_val(det.get("image_native_h"))}</span>'
        f'<span class="slirn-fine-bg-detect-key">检测算法</span>'
        f'<span class="slirn-fine-bg-detect-val">{esc(_det_algo or "—")}{esc(" · " + _det_color_txt if _det_color_txt else "")}</span>'
        f'<span class="slirn-fine-bg-detect-key">检测时间</span>'
        f'<span class="slirn-fine-bg-detect-val">{esc(str(det.get("detected_at") or "—"))}</span>'
        f'</div>'
        f'</div>'
        f'{ai_result_card}'
        f'</div>'
        f'<div class="slirn-form-hint">中心扩展：从背景图中心取基准色（或抠图透明区），'
        f'向四方扩展到颜色变化处 —— 检出的就是放视频的区域；白色 = R/G/B 均 ≥ 阈值。</div>'
        f'</details>'
    )

    # 参数条：保存 + JSON 文件导出/导入（REQ-20261018-167 对齐长视频；
    # 此前「📤 导出参数」实为存服务器、「📥 导入参数」是无派发的死按钮）
    params_bar = (
        f'<div class="slirn-fine-actions-bar">'
        f'<button class="slirn-btn" data-action="sv-stage7-save-params" '
        f'title="把当前所有参数保存到 project（自动适配/分辨率归一在保存时生效）；'
        f'参数修改后已自动保存，此按钮用于手动确认">💾 保存参数</button>'
        f'<button class="slirn-btn" data-action="sv-stage7-params-export" '
        f'title="下载所有参数（布局/导师/字体/输出/音频/检测区域/预览）为 JSON 文件">'
        f'📤 导出参数</button>'
        f'<button class="slirn-btn" data-action="sv-stage7-params-import" '
        f'title="从 JSON 文件导入参数（覆盖当前参数；不动素材文件）">'
        f'📥 导入参数</button>'
        f'<span class="slirn-sv-autosave-hint" data-autosave-hint>修改后自动保存</span>'
        f'<span class="slirn-sv-muted">公共参数对每条短视频生效；成片输出在 stage6/final_*.mp4</span>'
        f'</div>'
    )

    # 模板条（公共）
    template_bar = (
        f'<div class="slirn-fine-actions-bar">'
        f'<button class="slirn-btn slirn-btn-primary" data-action="sv-stage7-profiles-save">💾 保存参数为模板</button>'
        f'<span class="slirn-fine-actions-label">模板名</span>'
        f'<input type="text" class="slirn-sv-profile-name" '
        f'placeholder="（可选）填了名另存为模板" maxlength="30" autocomplete="off">'
        f'<button class="slirn-btn" data-action="sv-stage7-profiles-apply">📥 引用参数（本地）</button>'
        f'<span class="slirn-sv-muted">未保存</span>'
        f'</div>'
    )

    # REQ-20261017-164：参数区改常显两列（仿长视频精剪合成 —— 左列
    # 视频/裁剪/背景/音乐，右列 字幕/字体/导师/封面/输出），不再折叠。
    # REQ-20261018-193：五组「区域 + 样式」合并成一张卡 —— _xy_inner
    # 是 xy 区域的「标题行 + 参数网格」内层（无卡片壳），合并块复用。
    def _xy_inner(mat_key: str, icon: str, name: str, d: dict,
                  extra_ctrl: str = "", extra_label: str = "适配方式") -> str:
        d = d or {}
        x = int(d.get("x") or 0)
        y = int(d.get("y") or 0)
        w = int(d.get("w") or 0)
        h = int(d.get("h") or 0)
        extra_row = (
            f'<div class="slirn-fine-param">'
            f'<span class="slirn-fine-param-label">{esc(extra_label)}</span>'
            f'{extra_ctrl}'
            f'</div>' if extra_ctrl else ""
        )
        return (
            f'<div class="slirn-fine-layout-title">'
            f'<label><input type="checkbox" class="slirn-fine-enabled" data-key="{esc(mat_key)}" '
            f'{"checked" if d.get("enabled", True) else ""}> {esc(icon)} {esc(name)}</label>'
            f'</div>'
            f'<div class="slirn-fine-params">'
            f'<div class="slirn-fine-param">'
            f'<span class="slirn-fine-param-label">X (-1080–1080)</span>'
            f'<input type="number" class="slirn-fine-num" data-key="{esc(mat_key)}.x" '
            f'value="{x}" min="-1080" max="1080" step="1">'
            f'</div>'
            f'<div class="slirn-fine-param">'
            f'<span class="slirn-fine-param-label">Y (-1920–1920)</span>'
            f'<input type="number" class="slirn-fine-num" data-key="{esc(mat_key)}.y" '
            f'value="{y}" min="-1920" max="1920" step="1">'
            f'</div>'
            f'<div class="slirn-fine-param">'
            f'<span class="slirn-fine-param-label">宽</span>'
            f'<input type="number" class="slirn-fine-num" data-key="{esc(mat_key)}.w" '
            f'value="{w}" min="0" max="1080" step="1">'
            f'</div>'
            f'<div class="slirn-fine-param">'
            f'<span class="slirn-fine-param-label">高</span>'
            f'<input type="number" class="slirn-fine-num" data-key="{esc(mat_key)}.h" '
            f'value="{h}" min="0" max="1920" step="1">'
            f'</div>'
            f'{extra_row}'
            f'</div>'
        )

    def _xy_block(mat_key: str, icon: str, name: str, d: dict,
                  extra_ctrl: str = "", extra_label: str = "适配方式") -> str:
        """独立 xy 卡片（背景区域 / 导师头像区域等不参与合并的区域用）。"""
        return (
            '<div class="slirn-fine-layout-block">'
            + _xy_inner(mat_key, icon, name, d, extra_ctrl, extra_label)
            + '</div>'
        )
    # REQ-20261029-010：「适配方式」改为复选框 —— 勾选 = 截取的视频拉伸
    # 铺满视频区域的宽高（REQ-20261018-189 起的固定语义，保持向后兼容）；
    # 不勾选 = **宽度优先**：先把截取视频的宽度适配到视频区域的宽度，
    # 高度按截取视频自身的宽高比自动算出（不变形、不用背景补边；高度可能
    # 超出区域高度，此时在区域内居中）。键 = layout.video.video_fit
    # （默认 True）。
    _video_fit_on = bool((layout.get("video") or {}).get("video_fit", True))
    fit_hint = (
        f'<div class="slirn-fine-fit-ctrl">'
        f'<label class="slirn-fine-fit-label">'
        f'<input type="checkbox" class="slirn-sv-video-fit" '
        f'data-key="video.video_fit" {"checked" if _video_fit_on else ""}> '
        f'铺满视频区域</label>'
        f'<span class="slirn-fine-fit-hint">'
        f'勾选 = 拉伸铺满视频区域的宽高；不勾选 = 优先适配视频区域的宽度，'
        f'高度按截取视频的宽高比自动计算（不变形、不用背景补边）'
        f'</span>'
        f'</div>'
    )
    bg_xy_block = _xy_block("bg", "🎨", "背景区域", layout.get("bg"))

    # 字幕字体设置 11 项（公共参数）+ 导师文字 6 项（REQ-20261017-164）
    def _fam_opts(cur: str) -> str:
        """字体下拉两项（字幕字体与导师字体共用）。"""
        cur = str(cur or "STHeitiMedium.ttc")
        return (
            f'<option value="STHeitiMedium.ttc" {"selected" if "STHeiti" in cur else ""}>STHeitiMedium（系统）</option>'
            f'<option value="Noto Sans CJK SC" {"selected" if "Noto" in cur else ""}>思源黑体 Noto Sans CJK SC</option>'
        )
    font_family_options = _fam_opts(font.get("family"))
    def _font_row(label: str, ctrl: str) -> str:
        return (
            f'<div class="slirn-fine-font-row">'
            f'<span class="slirn-fine-font-label">{esc(label)}</span>'
            f'{ctrl}'
            f'</div>'
        )
    # 字段字符串提到外面（f-string 不支持内嵌反斜杠转义）
    family_ctrl = (
        '<select class="slirn-fine-font-sel" data-font-key="family">'
        + font_family_options
        + '</select>'
    )
    size_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-font-key="size" '
        'min="12" max="96" value="' + str(int(font.get("size") or 48)) + '">'
    )
    bold_chk = " checked" if font.get("bold") else ""
    bold_ctrl = (
        '<label><input type="checkbox" class="slirn-fine-font-chk" '
        'data-font-key="bold"' + bold_chk + '> 启用</label>'
    )
    align_v = font.get("align") or "center"
    align_ctrl = (
        '<select class="slirn-fine-font-sel" data-font-key="align">'
        + ('<option value="left"' + (' selected' if align_v == "left" else "") + '>左对齐</option>')
        + ('<option value="center"' + (' selected' if align_v == "center" else "") + '>居中</option>')
        + ('<option value="right"' + (' selected' if align_v == "right" else "") + '>右对齐</option>')
        + '</select>'
    )
    offset_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-font-key="offset_ms" '
        'min="-1920" max="1920" step="1" value="'
        + str(int(font.get("offset_ms") or 0)) + '">'
    )
    stroke_w_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-font-key="stroke_width" '
        'min="0" max="10" value="' + str(int(font.get("stroke_width") or 2)) + '">'
    )
    color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-font-key="color" '
        'value="' + esc(str(font.get("color") or "#FFFFFF")) + '">'
    )
    stroke_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-font-key="stroke_color" '
        'value="' + esc(str(font.get("stroke_color") or "#000000")) + '">'
    )
    bg_chk = " checked" if font.get("bg_enabled") else ""
    bg_enabled_ctrl = (
        '<label><input type="checkbox" class="slirn-fine-font-chk" '
        'data-font-key="bg_enabled"' + bg_chk + '> 启用</label>'
    )
    bg_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-font-key="bg_color" '
        'value="' + esc(str(font.get("bg_color") or "#000000")) + '">'
    )
    bg_op = str(float(font.get("bg_opacity") or 0.5))
    # REQ-20261019-204：透明度行 = 数字（左，可手输）+ 滑块（收窄）同一行；
    # 数字带同一 data-font-key（联动后两值一致，collect/autosave 都能触发）
    bg_opacity_ctrl = (
        '<div class="slirn-fine-slider-row">'
        '<input type="number" class="slirn-fine-num" data-font-key="bg_opacity" '
        'min="0" max="1" step="0.05" value="' + bg_op + '">'
        '<input type="range" class="slirn-fine-font-slider" data-font-key="bg_opacity" '
        'min="0" max="1" step="0.05" value="' + bg_op + '">'
        '</div>'
    )
    # REQ-20261018-193：字幕字体设置并入「字幕」合并块（卡内小节）——
    # 只留字体行，不再单独成块
    font_inner = (
        f'{_font_row("字体本体", family_ctrl)}'
        f'{_font_row("字体大小（px）", size_ctrl)}'
        f'{_font_row("粗体", bold_ctrl)}'
        f'{_font_row("屏幕对齐", align_ctrl)}'
        f'{_font_row("偏移量（px）", offset_ctrl)}'
        f'{_font_row("描边宽度（px）", stroke_w_ctrl)}'
        f'{_font_row("文字颜色", color_ctrl)}'
        f'{_font_row("描边颜色", stroke_color_ctrl)}'
        f'{_font_row("背景框", bg_enabled_ctrl)}'
        f'{_font_row("背景颜色", bg_color_ctrl)}'
        f'{_font_row("背景透明度", bg_opacity_ctrl)}'
    )

    # REQ-20261017-164：导师文字（昵称 / 标题）字体及大小 + 文字本体
    mentor = p.get("mentor") or {}
    # 控件字符串提到外面（f-string 不支持内嵌反斜杠转义）
    nickname_text_ctrl = (
        '<input type="text" class="slirn-fine-font-text" data-mentor-key="nickname" '
        'maxlength="40" value="' + esc(str(mentor.get("nickname") or "")) + '">'
    )
    nickname_family_ctrl = (
        '<select class="slirn-fine-font-sel" data-mentor-key="nickname_font">'
        + _fam_opts(mentor.get("nickname_font")) + '</select>'
    )
    nickname_size_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-mentor-key="nickname_size" '
        'min="12" max="96" value="' + str(int(mentor.get("nickname_size") or 42)) + '">'
    )
    # REQ-20261018-180：导师头衔多行 textarea —— 一个导师多个 title，
    # 回车分行、各行进成片（rows 按存量行数给，JS 输入时自动长高）
    _mt_val = str(mentor.get("title") or "")
    title_text_ctrl = (
        '<textarea class="slirn-fine-font-text" data-mentor-key="title" rows="'
        + str(min(4, max(1, _mt_val.count("\n") + 1)))
        + '" maxlength="120" title="回车换行（多个头衔各占一行）">'
        + esc(_mt_val) + '</textarea>'
    )
    title_family_ctrl = (
        '<select class="slirn-fine-font-sel" data-mentor-key="title_font">'
        + _fam_opts(mentor.get("title_font")) + '</select>'
    )
    title_size_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-mentor-key="title_size" '
        'min="12" max="96" value="' + str(int(mentor.get("title_size") or 36)) + '">'
    )
    # REQ-20261018-195：头衔行间距（px；头衔可多行/折行，昵称单行无此参数）
    title_line_spacing_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-mentor-key="title_line_spacing" '
        'min="-20" max="60" value="' + str(int(mentor.get("title_line_spacing") if mentor.get("title_line_spacing") is not None else 0)) + '">'
    )
    # REQ-20261018-182：颜色 + 描边可选（enabled / 颜色 / 宽度；口径同
    # 字幕字体块的描边三件套）
    nickname_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-mentor-key="nickname_color" '
        'value="' + esc(str(mentor.get("nickname_color") or "#FFFFFF")) + '">'
    )
    nickname_ol_chk = " checked" if mentor.get("nickname_outline_enabled", True) else ""
    nickname_ol_enabled_ctrl = (
        '<label><input type="checkbox" class="slirn-fine-font-chk" '
        'data-mentor-key="nickname_outline_enabled"' + nickname_ol_chk + '> 启用</label>'
    )
    nickname_ol_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-mentor-key="nickname_outline_color" '
        'value="' + esc(str(mentor.get("nickname_outline_color") or "#000000")) + '">'
    )
    nickname_ol_width_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-mentor-key="nickname_outline_width" '
        'min="0" max="10" value="' + str(int(mentor.get("nickname_outline_width") if mentor.get("nickname_outline_width") is not None else 2)) + '">'
    )
    title_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-mentor-key="title_color" '
        'value="' + esc(str(mentor.get("title_color") or "#FFE9B8")) + '">'
    )
    title_ol_chk = " checked" if mentor.get("title_outline_enabled", True) else ""
    title_ol_enabled_ctrl = (
        '<label><input type="checkbox" class="slirn-fine-font-chk" '
        'data-mentor-key="title_outline_enabled"' + title_ol_chk + '> 启用</label>'
    )
    title_ol_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-mentor-key="title_outline_color" '
        'value="' + esc(str(mentor.get("title_outline_color") or "#000000")) + '">'
    )
    title_ol_width_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-mentor-key="title_outline_width" '
        'min="0" max="10" value="' + str(int(mentor.get("title_outline_width") if mentor.get("title_outline_width") is not None else 2)) + '">'
    )
    # REQ-20261018-193：导师文字（昵称/标题）并入「导师文字」合并块
    mentor_inner = (
        f'{_font_row("导师昵称", nickname_text_ctrl)}'
        f'{_font_row("昵称字体", nickname_family_ctrl)}'
        f'{_font_row("昵称大小（px）", nickname_size_ctrl)}'
        f'{_font_row("昵称文字颜色", nickname_color_ctrl)}'
        f'{_font_row("昵称描边", nickname_ol_enabled_ctrl)}'
        f'{_font_row("昵称描边颜色", nickname_ol_color_ctrl)}'
        f'{_font_row("昵称描边宽度（px）", nickname_ol_width_ctrl)}'
        f'{_font_row("标题文字", title_text_ctrl)}'
        f'{_font_row("标题字体", title_family_ctrl)}'
        f'{_font_row("标题大小（px）", title_size_ctrl)}'
        f'{_font_row("标题行间距（px）", title_line_spacing_ctrl)}'
        f'{_font_row("标题文字颜色", title_color_ctrl)}'
        f'{_font_row("标题描边", title_ol_enabled_ctrl)}'
        f'{_font_row("标题描边颜色", title_ol_color_ctrl)}'
        f'{_font_row("标题描边宽度（px）", title_ol_width_ctrl)}'
        f'<div class="slirn-form-hint">配合素材区的「👤 导师头像」使用；'
        f'昵称/标题 + 头像会在成片渲染时叠加（渲染器接入后生效）。</div>'
    )

    # REQ-20261018-173：导师文字区域 / 导师头像区域（位置可被
    # 「✅ 填充到参数区域」按识别类型自动填；头像可选矩形/圆形）
    shape_v = str(layout.get("avatar_shape") or "circle")
    shape_ctrl = (
        '<select class="slirn-sv-avatar-shape" style="padding:3px 6px;">'
        + ('<option value="circle"' + (' selected' if shape_v == "circle" else "") + '>⭕ 圆形头像</option>')
        + ('<option value="rect"' + (' selected' if shape_v == "rect" else "") + '>⬛ 矩形头像</option>')
        + '</select>'
    )
    mentor_text_xy_inner = _xy_inner(
        "mentor_text", "🧑‍🏫", "导师文字区域", layout.get("mentor_text"))
    avatar_xy_block = _xy_block(
        "avatar", "👤", "导师头像区域", layout.get("avatar"),
        extra_ctrl=shape_ctrl, extra_label="头像形状")

    # REQ-20261018-177：视频标题样式 —— 字体/颜色/大小/阴影；
    # REQ-20261018-179（修订二）：无标题文字输入 —— 文字取自精剪混编区
    # 该条卡片顶部的标题（每条各自），参数区只管统一样式
    tcfg = p.get("title") or {}
    t_align_v = tcfg.get("align") or "center"
    title_family_ctrl = (
        '<select class="slirn-fine-font-sel" data-title-key="family">'
        + _fam_opts(tcfg.get("family")) + '</select>'
    )
    title_size_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-title-key="size" '
        'min="12" max="200" value="' + str(int(tcfg.get("size") or 64)) + '">'
    )
    title_bold_ctrl = (
        '<label><input type="checkbox" class="slirn-fine-font-chk" '
        'data-title-key="bold"' + (" checked" if tcfg.get("bold", True) else "") + '> 启用</label>'
    )
    title_align_ctrl = (
        '<select class="slirn-fine-font-sel" data-title-key="align">'
        + ('<option value="left"' + (' selected' if t_align_v == "left" else "") + '>左对齐</option>')
        + ('<option value="center"' + (' selected' if t_align_v == "center" else "") + '>居中</option>')
        + ('<option value="right"' + (' selected' if t_align_v == "right" else "") + '>右对齐</option>')
        + '</select>'
    )
    title_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-title-key="color" '
        'value="' + esc(str(tcfg.get("color") or "#FFFFFF")) + '">'
    )
    title_shadow_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-title-key="shadow_color" '
        'value="' + esc(str(tcfg.get("shadow_color") or "#000000")) + '">'
    )
    t_shadow_op = str(float(tcfg.get("shadow_opacity") if tcfg.get("shadow_opacity") is not None else 0.6))
    # REQ-20261019-204：同 bg_opacity —— 数字（左，可手输）+ 滑块同一行
    title_shadow_op_ctrl = (
        '<div class="slirn-fine-slider-row">'
        '<input type="number" class="slirn-fine-num" data-title-key="shadow_opacity" '
        'min="0" max="1" step="0.05" value="' + t_shadow_op + '">'
        '<input type="range" class="slirn-fine-font-slider" data-title-key="shadow_opacity" '
        'min="0" max="1" step="0.05" value="' + t_shadow_op + '">'
        '</div>'
    )
    title_shadow_depth_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-title-key="shadow_depth" '
        'min="0" max="20" value="' + str(int(tcfg.get("shadow_depth") if tcfg.get("shadow_depth") is not None else 4)) + '">'
    )
    # REQ-20261018-195：行间距（px，折行行与行之间；0=默认行距）
    title_line_spacing_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-title-key="line_spacing" '
        'min="-20" max="60" value="' + str(int(tcfg.get("line_spacing") if tcfg.get("line_spacing") is not None else 0)) + '">'
    )
    # REQ-20261018-193：视频标题样式并入「视频标题」合并块。
    # REQ-20261018-195：区域提到样式前（xy 内层自带复选框标题行，
    # 代表是否启用标题区域相关信息）；样式小节用虚线标题分隔
    title_inner = (
        f'{_font_row("标题字体", title_family_ctrl)}'
        f'{_font_row("标题大小（px）", title_size_ctrl)}'
        f'{_font_row("行间距（px）", title_line_spacing_ctrl)}'
        f'{_font_row("粗体", title_bold_ctrl)}'
        f'{_font_row("对齐方式", title_align_ctrl)}'
        f'{_font_row("文字颜色", title_color_ctrl)}'
        f'{_font_row("阴影颜色", title_shadow_color_ctrl)}'
        f'{_font_row("阴影不透明度", title_shadow_op_ctrl)}'
        f'{_font_row("阴影偏移（px）", title_shadow_depth_ctrl)}'
        f'<div class="slirn-form-hint">标题文字取自精剪混编区该条卡片顶部的'
        f'标题（每条短视频各自的标题，不在此统一设置）；预览/导出时按上方'
        f'「标题区域」的大小自动折行、按区域位置显示。</div>'
    )
    title_xy_inner = _xy_inner(
        "title", "🏷", "标题区域", layout.get("title"))

    # REQ-20261018-185：封面标题样式 + 封面标题区域 —— 封面片头段烧录的
    # 标题（位置/字体/颜色/大小独立于视频标题；文字同源该条卡片标题）
    ctcfg = p.get("cover_title") or {}
    ct_align_v = ctcfg.get("align") or "center"
    ct_family_ctrl = (
        '<select class="slirn-fine-font-sel" data-ct-key="family">'
        + _fam_opts(ctcfg.get("family")) + '</select>'
    )
    ct_size_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-ct-key="size" '
        'min="12" max="200" value="' + str(int(ctcfg.get("size") or 64)) + '">'
    )
    ct_bold_ctrl = (
        '<label><input type="checkbox" class="slirn-fine-font-chk" '
        'data-ct-key="bold"' + (" checked" if ctcfg.get("bold", True) else "") + '> 启用</label>'
    )
    ct_align_ctrl = (
        '<select class="slirn-fine-font-sel" data-ct-key="align">'
        + ('<option value="left"' + (' selected' if ct_align_v == "left" else "") + '>左对齐</option>')
        + ('<option value="center"' + (' selected' if ct_align_v == "center" else "") + '>居中</option>')
        + ('<option value="right"' + (' selected' if ct_align_v == "right" else "") + '>右对齐</option>')
        + '</select>'
    )
    ct_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-ct-key="color" '
        'value="' + esc(str(ctcfg.get("color") or "#FFFFFF")) + '">'
    )
    ct_shadow_color_ctrl = (
        '<input type="color" class="slirn-fine-font-color" data-ct-key="shadow_color" '
        'value="' + esc(str(ctcfg.get("shadow_color") or "#000000")) + '">'
    )
    ct_shadow_op = str(float(ctcfg.get("shadow_opacity") if ctcfg.get("shadow_opacity") is not None else 0.6))
    # REQ-20261019-204：同 bg_opacity —— 数字（左，可手输）+ 滑块同一行
    ct_shadow_op_ctrl = (
        '<div class="slirn-fine-slider-row">'
        '<input type="number" class="slirn-fine-num" data-ct-key="shadow_opacity" '
        'min="0" max="1" step="0.05" value="' + ct_shadow_op + '">'
        '<input type="range" class="slirn-fine-font-slider" data-ct-key="shadow_opacity" '
        'min="0" max="1" step="0.05" value="' + ct_shadow_op + '">'
        '</div>'
    )
    ct_shadow_depth_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-ct-key="shadow_depth" '
        'min="0" max="20" value="' + str(int(ctcfg.get("shadow_depth") if ctcfg.get("shadow_depth") is not None else 4)) + '">'
    )
    # REQ-20261018-195：行间距（px；语义同视频标题）
    ct_line_spacing_ctrl = (
        '<input type="number" class="slirn-fine-font-num" data-ct-key="line_spacing" '
        'min="-20" max="60" value="' + str(int(ctcfg.get("line_spacing") if ctcfg.get("line_spacing") is not None else 0)) + '">'
    )
    # REQ-20261018-193：封面标题样式并入「封面标题」合并块（区域在前）
    cover_title_inner = (
        f'{_font_row("标题字体", ct_family_ctrl)}'
        f'{_font_row("标题大小（px）", ct_size_ctrl)}'
        f'{_font_row("行间距（px）", ct_line_spacing_ctrl)}'
        f'{_font_row("粗体", ct_bold_ctrl)}'
        f'{_font_row("对齐方式", ct_align_ctrl)}'
        f'{_font_row("文字颜色", ct_color_ctrl)}'
        f'{_font_row("阴影颜色", ct_shadow_color_ctrl)}'
        f'{_font_row("阴影不透明度", ct_shadow_op_ctrl)}'
        f'{_font_row("阴影偏移（px）", ct_shadow_depth_ctrl)}'
        f'<div class="slirn-form-hint">封面片头段显示的标题（文字同视频标题，'
        f'取自精剪混编区该条卡片顶部的标题）；只在封面展示期间显示，样式与'
        f'视频标题相互独立。</div>'
    )
    cover_title_xy_inner = _xy_inner(
        "cover_title", "🖼", "封面标题区域", layout.get("cover_title"))

    # 视频源裁剪（公共参数；REQ-20261017-164 常显不折叠；
    # REQ-20261018-174 对齐长视频：X/Y 起点 + 宽高真实输入，取代占位文案）
    vid_lay = layout.get("video") or {}
    crop_vals = {
        "crop_x": int(vid_lay.get("crop_x") or 0),
        "crop_y": int(vid_lay.get("crop_y") or 0),
        "crop_w": int(vid_lay.get("crop_w") or 1920),
        "crop_h": int(vid_lay.get("crop_h") or 1080),
    }
    crop_aspect_lock = bool(vid_lay.get("crop_aspect_lock", True))
    # REQ-20261018-198：按视频区域宽高比锁定（与 16:9 锁互斥；基准 =
    # 显示区域 W/H，如 1080×624 → 0.578，随区域手改实时变）
    crop_region_lock = bool(vid_lay.get("crop_region_lock"))
    region_w = int(vid_lay.get("w") or 0)
    region_h = int(vid_lay.get("h") or 0)
    region_aspect = (region_h / region_w if region_w > 0 else 0)
    crop_aspect = (crop_vals["crop_h"] / crop_vals["crop_w"]
                   if crop_vals["crop_w"] > 0 else 0)
    crop_param_rows = "".join(
        f'<div class="slirn-fine-param">'
        f'<span class="slirn-fine-param-label">{lbl}</span>'
        f'<input type="number" class="slirn-fine-num" data-key="video.{ck}" '
        f'value="{crop_vals[ck]}" min="0" max="{mx}" step="1">'
        f'</div>'
        for ck, lbl, mx in (
            ("crop_x", "X 起点（0–1920）", 1920),
            ("crop_y", "Y 起点（0–1080）", 1080),
            ("crop_w", "宽度（0–1920）", 1920),
            ("crop_h", "高度（0–1080）", 1080),
        )
    )
    # REQ-20261018-193：视频源裁剪并入「视频」合并块（区域在前）
    crop_inner = (
        f'<div class="slirn-fine-crop-presets">'
        f'<button class="slirn-btn-mini" data-action="sv-stage7-crop-preset" data-preset="full">📐 全幅（不裁剪）</button>'
        f'<button class="slirn-btn-mini" data-action="sv-stage7-crop-preset" data-preset="16x9">🎯 16:9 居中</button>'
        f'<button class="slirn-btn-mini" data-action="sv-stage7-crop-preset" data-preset="1x1">⬛ 1:1 居中</button>'
        f'</div>'
        f'<div class="slirn-sv-crop-locks">'
        f'<label><input type="checkbox" class="slirn-sv-crop-aspect-link" '
        f'data-key="video.crop_aspect_lock" '
        f'{"checked" if crop_aspect_lock else ""}> '
        f'🔗 锁定 16:9 比例（防变形；关闭后可任意调整）</label>'
        f'<label><input type="checkbox" class="slirn-sv-crop-region-link" '
        f'data-key="video.crop_region_lock" '
        f'{"checked" if crop_region_lock else ""}> '
        f'📐 按照视频区域的宽高比来锁定</label>'
        f'</div>'
        f'<div class="slirn-sv-muted">当前矩形比例 ≈ '
        f'<span class="slirn-sv-crop-aspect-val">{crop_aspect:.3f}</span>'
        f'（目标 16:9 = {16 / 9:.3f}，1:1 = 1.000）；'
        f'视频区域宽高比 ≈ <span class="slirn-sv-region-aspect-val">'
        f'{region_aspect:.3f}</span>（宽 {region_w} × 高 {region_h}，联动基准）；'
        f'以源画面 1920×1080 设计空间为准，渲染时按源视频实际尺寸等比换算</div>'
        f'<div class="slirn-fine-params">'
        f'{crop_param_rows}'
        f'</div>'
    )

    # 片头封面（公共参数；REQ-20261017-164 常显不折叠）
    cover_cfg = layout.get("cover") or {}
    cover_dur = float(cover_cfg.get("duration", 2.0) or 2.0)
    # REQ-20261018-197：回填 checked —— 此前模板漏了条件，刷新后永远显示
    # 未勾选；且下次自动保存会把已存的 enabled=true 反向覆盖成 false
    cover_on = "checked" if cover_cfg.get("enabled", True) else ""
    cover_block = (
        f'<div class="slirn-fine-cover-block">'
        f'<div class="slirn-fine-font-title">🎞 片头封面（全屏展示 N 秒后切视频）</div>'
        f'<label><input type="checkbox" class="slirn-fine-enabled" data-key="cover" {cover_on}> 启用片头封面</label>'
        f'<div class="slirn-fine-params">'
        f'<div class="slirn-fine-param">'
        f'<span class="slirn-fine-param-label">展示时长（0–10 秒）</span>'
        f'<input type="number" class="slirn-fine-num" data-key="cover.duration" value="{cover_dur:.1f}" min="0" max="10" step="0.5">'
        f'</div>'
        f'</div>'
        f'<div class="slirn-form-hint">设为 0 秒 = 不显示片头；建议 1–3 秒（短视频一般不用）。</div>'
        f'</div>'
    )

    # 背景音乐（公共参数；REQ-20261017-164 常显不折叠）
    vol_db = int(audio.get("volume_db") or -6)
    fade_in = int((audio.get("fade_in_ms") or 500) / 1000 * 10) / 10
    fade_out = int((audio.get("fade_out_ms") or 1000) / 1000 * 10) / 10
    audio_block = (
        f'<div class="slirn-fine-audio-block">'
        f'<div class="slirn-fine-font-title">🎵 背景音乐（与原声混合播放，保留说话人语音）</div>'
        f'<label><input type="checkbox" class="slirn-sv-audio-enabled" {audio_enabled}> 启用背景音乐</label>'
        f'<div class="slirn-sv-card-actions" style="margin:6px 0;">'
        f'<button class="slirn-btn-mini" data-action="sv-stage7-list-bgm">🎵 选系统 BGM</button>'
        f'<span class="slirn-sv-muted" style="margin-left:6px;">当前：{esc(str(audio.get("bgm_path") or "未选"))}</span>'
        f'</div>'
        f'<div class="slirn-fine-params">'
        f'<div class="slirn-fine-param">'
        f'<span class="slirn-fine-param-label">音量（dB 衰减，0 = 原曲）</span>'
        f'<input type="number" class="slirn-fine-num" data-key="audio.volume_db" value="{vol_db}" min="-40" max="0" step="1">'
        f'</div>'
        f'<div class="slirn-fine-param">'
        f'<span class="slirn-fine-param-label">淡入（0–5 秒）</span>'
        f'<input type="number" class="slirn-fine-num" data-key="audio.fade_in_sec" value="{fade_in}" min="0" max="5" step="0.5">'
        f'</div>'
        f'<div class="slirn-fine-param">'
        f'<span class="slirn-fine-param-label">淡出（0–5 秒）</span>'
        f'<input type="number" class="slirn-fine-num" data-key="audio.fade_out_sec" value="{fade_out}" min="0" max="5" step="0.5">'
        f'</div>'
        f'</div>'
        f'</div>'
    )

    # 输出设置（公共参数；REQ-20261017-164 常显不折叠 + 分辨率扩两项）
    output_block = (
        f'<div class="slirn-fine-output-block">'
        f'<div class="slirn-fine-font-title">📺 输出设置</div>'
        f'<div class="slirn-fine-font-row">'
        f'<span class="slirn-fine-font-label">分辨率</span>'
        f'<select class="slirn-sv-output-resolution" style="margin:0 6px; padding:3px 6px;">'
        # REQ-20261018-187：横竖屏分族显式标注。注意 720p/1080p 的**实际输出
        # 一直是竖屏**（720×1280 / 1080×1920，旧标签把宽高写反了）——按真实
        # 方向归入 9:16 族；16:9 族是新增值；REQ-20261018-190：异比例输出
        # 整体拉伸铺满（无黑边，宽高比有变形）
        f'<option value="1280x720" {"selected" if res == "1280x720" else ""}>16:9 720p（1280×720 横屏）</option>'
        f'<option value="1920x1080" {"selected" if res == "1920x1080" else ""}>16:9 1080p（1920×1080 横屏）</option>'
        f'<option value="720p" {"selected" if res == "720p" else ""}>9:16 720p（720×1280 竖屏）</option>'
        f'<option value="1080p" {"selected" if res == "1080p" else ""}>9:16 1080p（1080×1920 竖屏）</option>'
        f'<option value="960x1280" {"selected" if res == "960x1280" else ""}>3:4（960×1280 竖屏）</option>'
        f'<option value="1086x1448" {"selected" if res == "1086x1448" else ""}>3:4（1086×1448 竖屏）</option>'
        f'<option value="source" {"selected" if res == "source" else ""}>源分辨率</option>'
        f'<option value="bg_res" {"selected" if res == "bg_res" else ""}>按照背景图片分辨率输出</option>'
        f'</select>'
        f'</div>'
        f'<div class="slirn-form-hint">「按照背景图片分辨率输出」在成片渲染时读取'
        f'背景图尺寸（未上传背景图时退回 720p 竖屏）；非同比例的输出'
        f'（如竖版内容选 16:9 横屏、3:4）整体拉伸铺满画面——'
        f'无黑边、不裁内容，但宽高比会有变形。</div>'
        f'</div>'
    )

    # REQ-20261018-193：五组「区域 + 样式」合并成一张卡（用户指定）——
    # 视频（区域+源裁剪）/ 字幕（区域+字体）/ 导师文字（区域+昵称标题）/
    # 视频标题（区域+样式，REQ-195 区域提前）/ 封面标题（区域+样式）。
    # 卡内两半用虚线小节标题分隔；各半的控件 data-key 不变（JS 收集/
    # 自动保存零改动）
    def _merged_card(title: str, *parts: str) -> str:
        return (
            f'<div class="slirn-fine-merged-block">'
            f'<div class="slirn-fine-font-title">{title}</div>'
            + "".join(parts)
            + '</div>'
        )

    def _sub_title(text: str) -> str:
        return f'<div class="slirn-fine-sub-title">{text}</div>'

    video_merged = _merged_card(
        "🎬 视频（显示区域 + 源裁剪）",
        _xy_inner("video", "🎬", "视频区域",
                  layout.get("video"), extra_ctrl=fit_hint),
        _sub_title("🎥 视频源裁剪（从源画面里截一个矩形范围）"),
        crop_inner,
    )
    subtitle_merged = _merged_card(
        "📝 字幕（显示区域 + 字体设置）",
        _xy_inner("subtitle", "📝", "字幕区域", layout.get("subtitle")),
        _sub_title("🔤 字幕字体设置（11 项）"),
        font_inner,
    )
    mentor_merged = _merged_card(
        "🧑‍🏫 导师文字（显示区域 + 昵称/标题）",
        mentor_text_xy_inner,
        _sub_title("✍️ 昵称 / 标题（文字 + 样式）"),
        mentor_inner,
    )
    # REQ-20261018-195：视频标题「区域在前、样式在后」——xy 内层的
    # 「🏷 标题区域」标题行带复选框（= 是否启用标题区域相关信息），
    # 提到卡首当开关；样式小节用虚线标题分隔（对齐封面标题卡结构）
    title_merged = _merged_card(
        "🏷 视频标题（显示区域 + 样式）",
        title_xy_inner,
        _sub_title("🏷 视频标题样式"),
        title_inner,
    )
    # 用户口径：封面标题「区域在前、样式在后」
    cover_title_merged = _merged_card(
        "🖼 封面标题（显示区域 + 样式）",
        cover_title_xy_inner,
        _sub_title("🖼 封面标题样式"),
        cover_title_inner,
    )

    # REQ-20261017-164：两列常显组装（仿长视频精剪合成）。
    # REQ-20261019-203：原先 左列 3 块 / 右列 7 块 → 左列 ~860px 就结束、
    # 右列 ~3000px，左下大片空白（用户截图红框）。把 视频标题/片头封面/
    # 封面标题/输出设置 挪到左列 —— 右列只留文字类（字幕/导师文字/头像），
    # 两列高度大体均衡（左 ~2270 / 右 ~1510）。
    # REQ-20261019-212：输出设置再从左列挪回右列末尾（用户指定；左列
    # ~2000 / 右列 ~1780，均衡度反而更好）
    params_cols = (
        f'<div class="slirn-fine-cols">'
        f'<div class="slirn-fine-col">'
        f'{video_merged}'
        f'{bg_xy_block}'
        f'{audio_block}'
        f'{title_merged}'
        f'{cover_block}'
        f'{cover_title_merged}'
        f'</div>'
        f'<div class="slirn-fine-col">'
        f'{subtitle_merged}'
        f'{mentor_merged}'
        f'{avatar_xy_block}'
        f'{output_block}'
        f'</div>'
        f'</div>'
    )

    common_area = (
        f'<div class="slirn-fine-common" id="slirn-sv-fine-common">'
        f'<div class="slirn-fine-common-title">🧰 公共素材和参数设置'
        f'<span class="slirn-sv-muted">（多短视频混剪时共用的素材与参数）</span></div>'
        f'{upload_html}'
        f'{bg_detect_block}'
        f'{params_bar}'
        f'{template_bar}'
        f'{params_cols}'
        f'</div>'
    )

    # ---------- 每条短视频处理区（标题/视频/字幕继承 + 预览 + 导出） ----------

    from slirn_home import short_video_fine as _fine
    fine_by_idx = {int(h.get("index") or 0): h
                   for h in (s6.get("highlights") or []) if h.get("index")}

    def _clip_view_url(abs_path, rel: str) -> str:
        """REQ-20261017-158：素材文件名可点击查看 —— 落在 stage4/5/6
        目录内的文件走 short_video_file 端点出 URL（其余位置不点击）。"""
        try:
            if not rel or not Path(abs_path).is_file():
                return ""
            kind = Path(abs_path).parent.name
        except (OSError, TypeError, ValueError):
            return ""
        if kind not in ("stage4", "stage5", "stage6"):
            return ""
        return _file_url(task_id, project_id, kind, Path(rel).name)

    def _clip_mat_row(idx: int, slot: str, label: str, rel: str,
                      manual: bool, accept: str, abs_path="",
                      origin: str = "") -> str:
        name = Path(rel).name if rel else (
            "（未找到）" if slot in ("video", "subtitle") else "（未设置）")
        if not origin:
            origin = "✋ 手动替换" if manual else "自动继承 Stage 5"
        view_url = _clip_view_url(abs_path, rel)
        # REQ-20261018-183：四槽都可查看 —— 视频 → 浮窗播放；字幕 → 文本
        # 弹层；bg/audio → 复用 REQ-171 公共素材浮窗（image/audio）
        if view_url and slot == "video":
            name_html = (
                f'<button type="button" class="slirn-stage6-clip-mat-name '
                f'slirn-stage6-clip-mat-view" data-action="sv-stage6-view-video" '
                f'data-url="{esc(view_url)}" title="{esc(rel)}（点击查看）">👁 {esc(name)}</button>'
            )
        elif view_url and slot == "subtitle":
            name_html = (
                f'<button type="button" class="slirn-stage6-clip-mat-name '
                f'slirn-stage6-clip-mat-view" data-action="sv-preview-srt" '
                f'data-url="{esc(view_url)}" title="{esc(rel)}（点击查看）">👁 {esc(name)}</button>'
            )
        elif view_url and slot in ("bg", "audio"):
            media = "audio" if slot == "audio" else "image"
            name_html = (
                f'<button type="button" class="slirn-stage6-clip-mat-name '
                f'slirn-stage6-clip-mat-view" data-action="sv-mat-preview" '
                f'data-media="{media}" data-url="{esc(view_url)}" '
                f'title="{esc(rel)}（点击查看）">👁 {esc(name)}</button>'
            )
        else:
            name_html = (f'<span class="slirn-stage6-clip-mat-name" '
                         f'title="{esc(rel)}">{esc(name)}</span>')
        # REQ-20261018-183：手动替换的素材可撤销 —— 视频/字幕「♻️ 继承」
        # （清除覆盖即回退 Stage 5；成片必需素材删除后必然回到继承，
        # 删除与继承是同一操作）；bg/audio「🗑 删除」（回退公共素材）
        clear_html = ""
        if manual and slot in ("video", "subtitle"):
            clear_html = (
                f'<button type="button" class="slirn-btn-mini '
                f'slirn-stage6-clip-mat-inherit" data-action="sv-stage6-clip-inherit" '
                f'data-clip-index="{esc(idx)}" data-mat-slot="{esc(slot)}" '
                f'title="清除手动替换，恢复从上一阶段（粗剪合成）继承">♻️ 继承</button>'
            )
        elif manual and slot in ("bg", "audio"):
            clear_html = (
                f'<button type="button" class="slirn-btn-mini '
                f'slirn-stage6-clip-mat-del" data-action="sv-stage6-clip-mat-del" '
                f'data-clip-index="{esc(idx)}" data-mat-slot="{esc(slot)}" '
                f'title="删除本条独立{esc(label[2:])}，回退公共素材">🗑 删除</button>'
            )
        return (
            f'<div class="slirn-stage6-clip-mat" data-mat="{esc(slot)}">'
            f'<span class="slirn-stage6-clip-mat-label">{esc(label)}</span>'
            f'{name_html}'
            f'<span class="slirn-sv-muted">{esc(origin)}</span>'
            f'{clear_html}'
            f'<label class="slirn-btn-mini slirn-stage6-clip-mat-upload" '
            f'title="手动选择文件替换该条的{esc(label[2:])}（预览/导出改用该文件）">📂 选择文件'
            f'<input type="file" class="slirn-stage6-clip-upload" hidden '
            f'data-clip-index="{esc(idx)}" data-mat-slot="{esc(slot)}" accept="{esc(accept)}">'
            f'</label>'
            f'</div>'
        )

    def _render_one_card(idx: int, ch: dict) -> str:
        """单条处理区：标题/视频/字幕（继承+覆盖）+ 预览 + 导出 + 成片。"""
        src = _fine.resolved_clip_sources(root, project, idx)
        # REQ-20261017-161：本条独立 背景/音频（覆盖则不用公共素材）
        mats = _fine.resolved_clip_materials(root, project, idx)
        fh = fine_by_idx.get(idx) or {}
        err = fh.get("error") or ""
        dur = float(fh.get("duration_sec") or ch.get("duration_sec") or 0)
        final_mp4 = str(fh.get("final_mp4") or "")
        continuous_srt = str(fh.get("continuous_srt") or "")
        status = "done" if final_mp4 else ("failed" if err else "pending")
        mp4_name = Path(final_mp4).name if final_mp4 else ""
        srt_name = Path(continuous_srt).name if continuous_srt else ""
        mp4_url = _file_url(task_id, project_id, "stage6", mp4_name) if mp4_name else ""
        srt_url = _file_url(task_id, project_id, "stage6", srt_name) if srt_name else ""
        # REQ-20261019-232：成片不再内嵌播放（卡片区太小、全屏又太大）——
        # 改「▶ 预览成片」按钮走共享视频浮窗（标题栏拖动 + CSS resize 调大小）
        result_block = (
            "" if mp4_url
            else '<span class="slirn-sv-muted">未生成（点「💾 导出最终视频」渲染）</span>'
        )
        err_html = f'<div class="slirn-stage-error">{esc(err)}</div>' if err else ""
        dl_html = (
            f'<button class="slirn-btn-mini" data-action="sv-stage6-final-preview" '
            f'data-clip-index="{esc(idx)}" data-url="{esc(mp4_url)}" '
            f'title="浮窗播放成片（标题栏拖动、右下角调大小）">▶ 预览成片</button> '
            f'<a class="slirn-btn-mini" href="{esc(mp4_url)}" download>下载 mp4</a> '
            f'<button class="slirn-btn-mini" data-action="sv-preview-srt" data-url="{esc(srt_url)}">查看 srt</button>'
            if mp4_url else ""
        )
        # REQ-20261019-233：导出合成标题后的封面（不依赖成片，配了封面素材
        # 且该条有标题即可）；未就绪时不渲染按钮（服务端仍有兜底报错）
        cover_ready = (
            bool(str(materials.get("cover") or "").strip())
            and bool((layout.get("cover") or {}).get("enabled"))
            and bool((layout.get("cover_title") or {}).get("enabled", True))
            and bool(str(src["title"]).strip())
        )
        cover_btn = (
            f'<button class="slirn-btn-mini" data-action="sv-stage6-cover-export" '
            f'data-clip-index="{esc(idx)}" '
            f'title="导出封面图（合成该条标题，下载 png）">🖼 导出封面</button> '
            if cover_ready else ""
        )
        actions2 = cover_btn + dl_html
        # REQ-20261018-180：标题多行 textarea —— 回车换行、换行位置带入
        # 成片（rows 按存量行数给，JS 输入时自动长高）
        title_rows = min(4, max(1, str(src["title"]).count("\n") + 1))
        # REQ-20261019-232：未生成才渲染提示区；已生成时该区整个不出
        # （成片看预览浮窗/下载），空 div 不留
        result_html = (
            f'<div class="slirn-stage6-clip-result">{result_block}</div>'
            if result_block else "")
        return (
            f'<div class="slirn-stage6-clip slirn-fine-card" data-clip-index="{esc(idx)}">'
            f'<div class="slirn-stage6-clip-head">'
            f'<span class="slirn-sv-row-no">#{esc(idx)}</span>'
            f'<textarea class="slirn-stage6-clip-title" data-clip-index="{esc(idx)}" '
            f'rows="{title_rows}" maxlength="120" '
            f'title="标题自动继承自上一阶段，可直接修改；回车换行，换行位置带入成片">'
            f'{esc(src["title"])}</textarea>'
            f'{_stage_badge(status)}'
            f'<span class="slirn-sv-muted">{dur:.1f}s</span>'
            f'</div>'
            f'<div class="slirn-stage6-clip-mats">'
            f'{_clip_mat_row(idx, "video", "🎬 视频", src["video_rel"], src["video_manual"], "video/*", src["video_abs"])}'
            f'{_clip_mat_row(idx, "subtitle", "📝 字幕", src["subtitle_rel"], src["subtitle_manual"], ".srt", src["subtitle_abs"])}'
            f'{_clip_mat_row(idx, "bg", "🎨 背景", mats["bg_rel"], mats["bg_manual"], "image/*", mats["bg_abs"], "✋ 本条独立" if mats["bg_manual"] else "继承公共素材")}'
            f'{_clip_mat_row(idx, "audio", "🎵 音频", mats["audio_rel"], mats["audio_manual"], "audio/*", mats["audio_abs"], "✋ 本条独立" if mats["audio_manual"] else "继承公共素材")}'
            f'</div>'
            f'<div class="slirn-sv-card-actions slirn-stage6-clip-actions">'
            f'<button class="slirn-btn slirn-btn-primary" data-action="sv-stage7-export" '
            f'data-clip-index="{esc(idx)}" title="只渲染该条成片（其余条保留）">💾 导出最终视频</button>'
            f'<button class="slirn-btn" data-action="sv-stage7-preview" '
            f'data-clip-index="{esc(idx)}">🎬 生成预览</button>'
            f'<span class="slirn-fine-actions-label">预览时长</span>'
            f'<input type="number" class="slirn-sv-preview-duration" data-clip-index="{esc(idx)}" '
            f'min="2" max="30" value="{preview_dur}" style="width:60px; padding:3px 6px;">'
            f'<span class="slirn-sv-muted">秒（2-30）</span>'
            f'</div>'
            f'{result_html}'
            f'<div class="slirn-sv-card-actions">{actions2}</div>'
            f'{err_html}'
            f'</div>'
        )

    cards = []
    for i, ch in enumerate(s5_hls, start=1):
        idx = int(ch.get("index") or i)
        cards.append(_render_one_card(idx, ch))
    clip_list = (
        f'<div class="slirn-stage6-list">{"".join(cards)}</div>' if cards
        else '<div class="slirn-form-hint">先完成 Stage 5 粗剪合成，这里会自动带入每条短视频的标题、视频和字幕。</div>'
    )

    return f'''<div class="slirn-stage" data-stage="6">
  <div class="slirn-stage-head">
    <span class="slirn-stage-num">6</span>
    <strong>精剪混编</strong>
    {_stage_badge(s6.get("status"))}
    {_stage_elapsed(s6)}
  </div>
  <div class="slirn-stage-body">
    {status_card}
    <div class="slirn-form-hint">每条短视频一个处理区：标题/视频/字幕自动继承 Stage 5（可手动替换）；下方公共素材和参数对全部片段生效。</div>
    {clip_list}
    {common_area}
    {_stage_logs(s6)}
  </div>
</div>'''


_SV_STAGE_NAV = (
    ("stage1", "1", "视频选择", "从任务视频中选择本项目的唯一源视频。", "stage1_source"),
    ("stage2", "2", "字幕提取", "运行 ASR 并生成后续解析所需字幕。", "stage2_extract"),
    ("stage3", "3", "内容提取", "使用提示词和可选截图解析片段内容。", "stage3_analyze"),
    ("stage4", "4", "内容优化", "按标题让大模型切分长行、修改文字、删除冗余字幕行。", "stage4_optimize"),
    ("stage5", "5", "粗剪合成", "按解析结果生成候选短视频片段。", "stage5_coarse"),
    # REQ-20261016-148：Stage 6 字幕优化移除，精剪 7 上移为 6
    ("stage6", "6", "精剪混编", "生成最终短视频文件并检查导出结果。", "stage6_fine"),
)


def _stage_nav_state(status: object) -> str:
    value = str(status or "pending").lower()
    if value in ("done", "confirmed"):
        return "done"
    if value in ("running", "running_asr", "user_editing"):
        return "current"
    if value == "failed":
        return "failed"
    return "pending"


def _exec_logs_pane(task_id: str) -> str:
    """REQ-20261022-144 — 短视频「📜 执行日志」面板（服务端只出骨架，列表由 JS 拉）。

    与长视频工作台的执行日志面板同一套 CSS（slirn-logs-*）+ 同一个查询端点
    （/slirn/api/list_logs，读任务级 outputs/execution_history.json）。
    """
    from slirn_home import execution_history

    # REQ-20261028-NNN：长/短视频日志分流 — 本面板 chips 只有短视频 kind，且
    # 查询端 flow=short 服务端过滤（点「清除」也只是放开短视频各 kind 的选择，
    # 不再会漏出长视频记录）；chips 默认全不勾 = 列出全部短视频记录。
    kind_chips = "".join(
        f'<button type="button" class="slirn-chip" data-sv-log-kind="{esc(k)}" '
        f'title="{esc(v)}">{esc(v)}</button>'
        for k, v in execution_history.KIND_LABELS.items()
        if k.startswith("short_video")
    )
    return (
        f'<div class="slirn-sv-stage-pane-inner" data-task-id="{esc(task_id)}">'
        f'  <div class="slirn-sv-stage-pane-title">📜 执行日志 '
        f'<span class="slirn-logs-count" data-sv-logs-count>--</span></div>'
        f'  <div class="slirn-form-hint">短视频各阶段的执行历史（含耗时、错误信息）。'
        f'    长视频的执行日志请到长视频工作台查看（两套日志互不混排）。</div>'
        f'  <div class="slirn-logs-filter">'
        f'    <div class="slirn-logs-filter-row">'
        f'      <span class="slirn-logs-filter-label">短视频操作：</span>{kind_chips}'
        f'      <button type="button" class="slirn-chip slirn-chip-clear" data-action="sv-logs-clear-kinds" title="清除操作类型过滤">清除</button>'
        f'    </div>'
        f'    <div class="slirn-logs-filter-row">'
        f'      <span class="slirn-logs-filter-label">状态：</span>'
        f'      <button type="button" class="slirn-chip" data-sv-log-status="success" title="仅看成功的">✅ 成功</button>'
        f'      <button type="button" class="slirn-chip" data-sv-log-status="failed" title="仅看失败的">❌ 失败</button>'
        f'      <button type="button" class="slirn-chip" data-sv-log-status="running" title="仅看运行中的">⏳ 运行中</button>'
        f'      <button type="button" class="slirn-chip slirn-chip-clear" data-action="sv-logs-clear-statuses" title="清除状态过滤">清除</button>'
        f'    </div>'
        f'    <div class="slirn-logs-filter-row">'
        f'      <span class="slirn-logs-filter-label">模式：</span>'
        f'      <button type="button" class="slirn-chip" data-sv-log-auto="any" title="不限（手动 + 自动）">全部</button>'
        f'      <button type="button" class="slirn-chip" data-sv-log-auto="manual" title="仅看手动触发的">👆 手动</button>'
        f'      <button type="button" class="slirn-chip" data-sv-log-auto="auto" title="仅看流程配置自动执行的">⚙ 自动</button>'
        f'    </div>'
        f'    <div class="slirn-logs-filter-row">'
        f'      <input type="text" class="slirn-input slirn-logs-keyword" data-sv-logs-keyword '
        f'             placeholder="🔍 搜错误信息或操作描述（不区分大小写）" />'
        f'      <button type="button" class="slirn-btn slirn-btn-sm" data-action="sv-logs-refresh" title="按当前过滤条件重新查询">🔄 刷新</button>'
        f'    </div>'
        f'  </div>'
        f'  <div class="slirn-logs-list" id="slirn-sv-logs-list" data-task-id="{esc(task_id)}">'
        f'    <div class="slirn-form-hint slirn-logs-empty">尚未查询。点击「🔄 刷新」或切换过滤条件自动加载。</div>'
        f'  </div>'
        f'  <div class="slirn-logs-pager" id="slirn-sv-logs-pager" data-page="1" data-page-size="20" data-total="0">'
        f'    <button type="button" class="slirn-pager-btn" data-sv-pager="first" disabled title="首页">«</button>'
        f'    <button type="button" class="slirn-pager-btn" data-sv-pager="prev" disabled title="上一页">‹</button>'
        f'    <span class="slirn-pager-info">第 <span data-bind="page">1</span> / <span data-bind="total-pages">1</span> 页 · 共 <span data-bind="total">0</span> 条</span>'
        f'    <button type="button" class="slirn-pager-btn" data-sv-pager="next" disabled title="下一页">›</button>'
        f'    <button type="button" class="slirn-pager-btn" data-sv-pager="last" disabled title="末页">»</button>'
        f'  </div>'
        f'</div>'
    )


# REQ-20261030-017：短视频阶段面板键（懒渲染用）。
# render_project 与 /slirn/api/sv_pane 端点共用同一份渲染函数 —— 避免两处
# 渲染逻辑漂移（懒渲染拉回来的面板必须和首屏内联的那份一模一样）。
_SV_STAGE_PANE_KEYS = ("stage1", "stage2", "stage3", "stage4", "stage5",
                       "stage6", "logs")


def _sv_stage_label(key: str) -> str:
    """阶段显示名（占位壳文案用）。"""
    for k, _num, title, _desc, _stage_key in _SV_STAGE_NAV:
        if k == key:
            return title
    return "执行日志" if key == "logs" else key


def _sv_stage_pane_content(key: str, project: dict, *, task_id: str = "",
                           project_id: str = "",
                           repo_root: str | Path | None = None) -> str:
    """单个阶段面板的内容 HTML（render_project 与 sv_pane 端点共用）。

    抽出来是为了懒渲染：render_project 只内联焦点阶段，其余留占位壳；
    用户点阶段时前端 POST /slirn/api/sv_pane 只渲染那一个阶段。
    """
    pipeline = project.get("pipeline") or {}
    if key == "stage1":
        return _stage1_select_source(project)
    if key == "stage2":
        return _stage2_extract(project)
    if key == "stage3":
        return _stage3_analyze(
            project,
            source_url=_source_video_url(project),
            srt_lines=(pipeline.get("stage3_analyze", {}) or {}).get("srt_lines") or [],
        )
    if key == "stage4":
        return _stage4_optimize(project, task_id=task_id, project_id=project_id,
                                 repo_root=repo_root)
    if key == "stage5":
        return _stage5_coarse(project)
    if key == "stage6":
        return _stage6_fine(project)
    if key == "logs":
        return _exec_logs_pane(task_id)
    return ""


def _sv_lazy_shell(key: str) -> str:
    """懒面板占位壳（点阶段时才真正渲染内容）。

    壳里带一个「重新加载」按钮 —— 外网偶发失败时用户能手动重试，不必刷新整页。
    """
    label = _sv_stage_label(key)
    return (
        f'<div class="slirn-sv-pane-lazy" data-sv-lazy-shell="1">'
        f'<span class="slirn-sv-pane-lazy-icon">⏳</span>'
        f'<span class="slirn-sv-pane-lazy-text">「{esc(label)}」尚未加载</span>'
        f'<button type="button" class="slirn-btn-mini" data-action="sv-pane-reload" '
        f'data-stage-key="{esc(key)}">加载</button>'
        f'</div>'
    )


def render_stage_pane(key: str, project: dict, *,
                      repo_root: str | Path | None = None) -> str:
    """对外：渲染单个阶段面板内容（/slirn/api/sv_pane 端点用）。"""
    if key not in _SV_STAGE_PANE_KEYS:
        raise ValueError(f"未知阶段面板: {key}")
    task_id = str(project.get("task_id") or "")
    project_id = str(project.get("id") or "")
    p = project
    if repo_root is not None:
        # 注入到 project 副本（不污染原对象），下游 stage 渲染可读
        p = dict(project)
        p["_repo_root"] = str(repo_root)
    return _sv_stage_pane_content(key, p, task_id=task_id, project_id=project_id,
                                  repo_root=repo_root)


def render_project(project: dict, all_tasks: list[dict] | None = None,
                    *, repo_root: str | Path | None = None,
                    lazy: bool = True,
                    focus_key: str | None = None) -> str:
    task_id = str(project.get("task_id") or "")
    project_id = str(project.get("id") or "")
    pipeline = project.get("pipeline") or {}
    if repo_root is not None:
        # 注入到 project dict 副本（不污染原对象），下游 stage 渲染可读
        project = dict(project)
        project["_repo_root"] = str(repo_root)
    stage_states = {
        key: _stage_nav_state((pipeline.get(stage_key) or {}).get("status"))
        for key, _num, _title, _desc, stage_key in _SV_STAGE_NAV
    }
    focus_key = focus_key or next(
        (key for key, _num, _title, _desc, _stage_key in _SV_STAGE_NAV
         if stage_states[key] != "done"),
        _SV_STAGE_NAV[-1][0],
    )
    nav_items = []
    for key, number, title, desc, _stage_key in _SV_STAGE_NAV:
        state = stage_states[key]
        # REQ-20261016-131：mark 永远是数字（不用 ✓ / ! 表达完成/失败，
        # 改由卡片背景 + mark 颜色表达状态；数字不被遮挡）。
        mark = number
        active = key == focus_key
        nav_items.append(
            f'<button type="button" class="slirn-sv-stage-nav {state}'
            f'{" active" if active else ""}" data-action="sv-stage-nav" '
            f'data-stage-key="{esc(key)}" aria-current="{"step" if active else "false"}">'
            f'<span class="slirn-sv-stage-mark">{esc(mark)}</span>'
            # REQ-20261016-131：text 区域只显示 title（数字在左侧 mark 里，不重复）。
            f'<span class="slirn-sv-stage-copy"><strong>{esc(title)}</strong>'
            f'<small>{esc(desc)}</small></span></button>'
        )
    # REQ-20261030-017：懒渲染 —— 只内联焦点阶段，其余留占位壳（点阶段时前端
    # POST /slirn/api/sv_pane 单独拉）。短视频首屏原本一次性渲染 7 个阶段
    # （实测大项目 1.25MB：stage2 内联整份 SRT 801KB + stage4 356KB），
    # 外网带宽下进项目页要等好几秒。
    pane_parts = []
    for key in _SV_STAGE_PANE_KEYS:
        is_focus = (key == focus_key)
        if lazy and not is_focus and key != "logs":
            body = _sv_lazy_shell(key)
            lazy_attr = ' data-sv-lazy="1"'
        else:
            body = _sv_stage_pane_content(
                key, project, task_id=task_id, project_id=project_id,
                repo_root=project.get("_repo_root"))
            lazy_attr = ''
        pane_parts.append(
            f'<section class="slirn-sv-stage-pane{" active" if is_focus else ""}" '
            f'id="slirn-sv-stage-pane-{esc(key)}" '
            f'data-stage-key="{esc(key)}"{lazy_attr}>{body}</section>'
        )
    pane_html = "".join(pane_parts)
    return f'''<div id="slirn-short-video-inner" class="slirn-tab-inner"
  data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}">
  <div class="slirn-sv-header">
    <div>
      <h2>{esc(project.get("name"))}</h2>
      <p>任务 {esc(task_id)} · REQ-098 单源 AI 拆条</p>
    </div>
    <div class="slirn-sv-card-actions">
      <button class="slirn-btn" data-action="sv-back">返回项目列表</button>
      <button class="slirn-btn" data-action="sv-refresh">刷新状态</button>
    </div>
  </div>
  <!-- REQ-20261022-143：流程配置面板 + 状态条挂载点（内容由 short_video_flow.js 渲染）
       面板自带 task/project 上下文 → 挂载不依赖「哪个脚本先执行」 -->
  <div id="slirn-sv-flow-status" class="slirn-pipe-status"
       data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}" hidden></div>
  <section id="slirn-sv-flow-panel" class="slirn-pipe-panel"
           data-task-id="{esc(task_id)}" data-project-id="{esc(project_id)}"></section>
  <div class="slirn-sv-pipeline">
    <aside class="slirn-sv-stages-card" aria-label="短视频阶段">
      <div class="slirn-sv-stages-title">阶段</div>
      <label class="slirn-sv-autonext" title="勾选后：当前阶段的工作完成时，自动打开下一阶段的页面。不勾选则保持在原页面，由用户手动切到下一阶段。">
        <input type="checkbox" id="slirn-sv-autonext"> 完成后，自动打开下一阶段界面
      </label>
      <div class="slirn-sv-stage-nav-list">{"".join(nav_items)}</div>
      <!-- REQ-20261022-144：执行日志入口（阶段列表下方；不属于流水线阶段）-->
      <div class="slirn-sv-logs-divider" title="执行日志是历史视图，不属于流水线阶段（不参与自动跳转）">
        📜 执行日志（不属于流水线阶段）
      </div>
      <button type="button" class="slirn-sv-stage-nav slirn-sv-stage-nav-logs pending"
              data-action="sv-logs-open" title="查看本任务的执行历史（含耗时、错误信息，支持分页 + 过滤）">
        <span class="slirn-sv-stage-mark">📜</span>
        <span class="slirn-sv-stage-copy"><strong>执行日志</strong>
        <small>所有阶段执行历史（提取/解析/优化/合成/导出）</small></span>
      </button>
    </aside>
    <div class="slirn-sv-stage-panes">{pane_html}</div>
  </div>
</div>'''
