"""任务列表渲染 + 操作回调 — REQ-B §3.3 / §3.4。"""

from __future__ import annotations

import json
from pathlib import Path

from tasklib import TASK_STATUS_LABEL, TaskManager, TaskNotFoundError


def render_task_list(mgr: TaskManager) -> tuple[list[list[str]], dict]:
    """渲染任务列表为 Dataframe 接受的 (rows, metadata) 格式。

    Returns:
        (rows, metadata) — rows 是 list of list，metadata 是可选的 dict（用于显示详情）。
    """
    rows: list[list[str]] = []
    for s in mgr.list():
        rows.append([
            s.task_id,
            s.name,
            s.original_video,
            TASK_STATUS_LABEL[s.status],
            s.created_at.strftime("%Y-%m-%d %H:%M"),
            s.updated_at.strftime("%Y-%m-%d %H:%M"),
        ])
    return rows, {"当前任务数": len(rows)}


def show_detail(mgr: TaskManager, task_id: str) -> str:
    """返回任务完整元数据的 Markdown 字符串（只读）。"""
    try:
        task = mgr.get(task_id)
    except TaskNotFoundError:
        return f"❌ 任务不存在: {task_id}"

    seg = "（无）"
    if task.segment:
        seg = f"{task.segment.start} → {task.segment.end}\n- 路径: `{task.segment.path}`"

    return f"""### 任务详情: {task.task_id}

| 字段 | 值 |
|---|---|
| 任务名 | {task.name} |
| 状态 | {TASK_STATUS_LABEL[task.status]} |
| 原视频 | `{task.original_video_source}` |
| 截取段 | {seg} |
| 热词文件 | `{task.hotwords_path}` |
| 创建时间 | {task.created_at.isoformat()} |
| 更新时间 | {task.updated_at.isoformat()} |
"""


def arm_delete() -> tuple[str, str, bool]:
    """第一步：把删除按钮变成「⚠️ 确认删除？」，显示取消按钮。

    Returns: (delete_btn_text, cancel_btn_text, cancel_visible)
    """
    return "⚠️ 确认删除？", "取消", True


def cancel_delete() -> tuple[str, str, bool]:
    """取消删除：恢复按钮文字，隐藏取消按钮。"""
    return "🗑️ 删除", "", False


def confirm_delete(mgr: TaskManager, task_id: str) -> tuple[list[list[str]], dict, str, str, bool]:
    """第二步：真正执行删除。

    Returns: (new_rows, new_metadata, status_msg, delete_btn_text, cancel_visible)
    """
    try:
        mgr.delete(task_id)
        msg = f"✅ 已删除任务 {task_id}"
    except TaskNotFoundError:
        msg = f"❌ 任务不存在: {task_id}"

    rows, meta = render_task_list(mgr)
    return rows, meta, msg, "🗑️ 删除", False


def placeholder_action(feature: str) -> str:
    """占位按钮的回调：弹 toast 提示后续 REQ 实现。"""
    return f"⏳ 该功能（「{feature}」）在后续 REQ 实现"


def format_path_for_display(p: Path | str) -> str:
    """把 Path 对象格式化为 Markdown 显示用的代码字符串。"""
    return f"`{p}`"
