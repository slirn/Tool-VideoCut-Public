"""新建任务 tab — REQ-C UI 实现。"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from pathlib import Path

import gradio as gr

# 确保 tasklib 可导入（必须在 from slirn_home.task_list import 之前调用，
# 因为 task_list.py 顶层有 from tasklib import ...）
from slirn_home.paths import ensure_tasklib_importable

ensure_tasklib_importable()

from tasklib import TaskManager, TimeSegment, VideoProcessingError  # noqa: E402
from tasklib.time_utils import segment_filename  # noqa: E402
from tasklib.video import get_video_duration  # noqa: E402

from slirn_home.task_list import render_task_list

log = logging.getLogger(__name__)


def _get_temp_dir(repo_root: Path) -> Path:
    """slirn-standalone/.temp/ — 截取预览临时目录。"""
    d = repo_root / ".temp"
    d.mkdir(exist_ok=True)
    return d


def _to_repo_relative(p: Path, repo_root: Path) -> str:
    """Path → 相对仓库根的 POSIX 字符串。"""
    try:
        return str(p.resolve().relative_to(repo_root.resolve())).replace("\\", "/")
    except ValueError:
        return str(p.resolve()).replace("\\", "/")


def on_upload_video(file_path: str | None, repo_root: Path) -> tuple[str, str]:
    """文件选择/拖拽后回调：显示文件名 + 时长。

    Returns: (filename_display, status_msg)
    """
    if not file_path:
        return "", "请先选择或拖拽视频文件"
    p = Path(file_path)
    if not p.exists():
        return "", f"文件不存在: {p}"

    duration = get_video_duration(p)
    size_mb = p.stat().st_size / 1024 / 1024

    if duration is None:
        info = f"📁 {p.name}（{size_mb:.1f} MB，时长未知）"
    else:
        mm, ss = divmod(int(duration), 60)
        hh, mm = divmod(mm, 60)
        info = f"📁 {p.name}（{size_mb:.1f} MB，{hh:02d}:{mm:02d}:{ss:02d}）"
    return info, "✅ 文件已加载"


def on_cut_preview(
    file_path: str | None,
    start_time: str,
    end_time: str,
    repo_root: Path,
) -> tuple[str, str | None, str, str]:
    """截取预览：ffmpeg 截取到临时目录，返回截取段路径给 Gradio Video 组件。

    Returns: (preview_path_or_None, status_msg, start_box_clear, end_box_clear)
    """
    if not file_path:
        return None, "⚠️ 请先选择视频文件", "", ""
    if not start_time.strip() or not end_time.strip():
        return None, "⚠️ 请填写开始和结束时间（或都留空表示不截取）", "", ""

    src = Path(file_path)
    if not src.exists():
        return None, f"❌ 原视频不存在: {src}", "", ""

    # 校验 start < end
    try:
        from tasklib.time_utils import validate_segment
        validate_segment(start_time.strip(), end_time.strip())
    except Exception as e:
        return None, f"❌ 时间校验失败: {e}", "", ""

    # 截取到临时目录
    temp_dir = _get_temp_dir(repo_root)
    temp_name = f"cut_{uuid.uuid4().hex[:8]}_{src.stem}{src.suffix}"
    dst = temp_dir / temp_name

    try:
        from tasklib.video import cut_video
        cut_video(src, dst, start_time.strip(), end_time.strip())
    except VideoProcessingError as e:
        return None, f"❌ 截取失败: {e}", "", ""

    cut_dur = get_video_duration(dst)
    if cut_dur is not None:
        mm, ss = divmod(int(cut_dur), 60)
        hh, mm = divmod(mm, 60)
        dur_str = f"{hh:02d}:{mm:02d}:{ss:02d}"
    else:
        dur_str = "未知"
    size_mb = dst.stat().st_size / 1024 / 1024

    msg = f"✅ 截取成功：{dst.name}（{size_mb:.1f} MB，{dur_str}）\n📁 临时路径：{dst}"
    return str(dst), msg, start_time, end_time


def on_create_task(
    file_path: str | None,
    task_name: str,
    start_time: str,
    end_time: str,
    preview_path: str | None,
    hotwords_text: str,
    repo_root: Path,
) -> tuple[list[list[str]], dict, str]:
    """创建任务：把截取段从 .temp/ 移到 task_dir，调 TaskManager.create()。

    Returns: (new_rows, new_meta, status_msg)
    """
    if not file_path:
        return [], {"当前任务数": 0}, "⚠️ 请先选择视频文件"

    src = Path(file_path)
    if not src.exists():
        return [], {"当前任务数": 0}, f"❌ 原视频不存在: {src}"

    # 默认任务名 = 文件名 stem
    final_name = task_name.strip() or src.stem

    # 解析热词（REQ-20260926-NNN：半角逗号 / 换行分隔，空格不算分隔符）
    hotwords = [
        w.strip() for w in re.split(r"[,\n，;；、]+", hotwords_text)
        if w.strip()
    ]

    # 处理截取段（如有）
    segment: TimeSegment | None = None
    segment_temp: Path | None = None
    if start_time.strip() and end_time.strip() and preview_path:
        # 截取段存在
        segment_temp = Path(preview_path)
        if not segment_temp.exists():
            return [], {"当前任务数": 0}, f"❌ 截取段文件丢失: {preview_path}"
        seg_filename = segment_filename(src.stem, start_time.strip(), end_time.strip(), src.suffix.lstrip(".") or "mp4")
        # 先创建任务拿到 task_id，再 move 文件
        segment = TimeSegment(
            start=start_time.strip(),
            end=end_time.strip(),
            path=Path("PLACEHOLDER"),  # 临时占位，create 后会被覆盖
        )

    mgr = TaskManager(repo_root)
    try:
        task = mgr.create(
            name=final_name,
            original_video=src,
            segment=segment,
            hotwords=hotwords,
        )
    except Exception as e:
        return [], {"当前任务数": 0}, f"❌ 创建失败: {e}"

    # 把截取段从 .temp/ 移到 task_dir/raw_input/
    if segment_temp and segment_temp.exists():
        final_segment_path = repo_root / "tasks" / task.task_id / "raw_input" / seg_filename
        try:
            shutil.move(str(segment_temp), str(final_segment_path))
            # 更新 metadata.json 中 segment.path
            import json

            from tasklib.schema import task_from_dict, task_to_dict
            meta_path = repo_root / "tasks" / task.task_id / "metadata.json"
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            # 重新构造 segment.path 为相对路径
            data["segment"]["path"] = _to_repo_relative(final_segment_path, repo_root)
            meta_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.warning("移动截取段失败: %s", e)

    rows, meta = render_task_list(mgr)
    return rows, meta, f"✅ 已创建任务 {task.task_id}: {final_name}"


def on_cancel_create(
    repo_root: Path,
) -> tuple[list[list[str]], dict, str, str, str, str, str, None]:
    """取消：清空所有输入 + 切回任务列表。"""
    mgr = TaskManager(repo_root)
    rows, meta = render_task_list(mgr)
    return rows, meta, "", "", "", "", "已取消", None


def build_create_task_components(mgr: TaskManager) -> dict:
    """构造新建任务 tab 的 Gradio 组件，返回 dict 供 app.py 引用。

    返回的 dict 含所有需要引用的组件 key。
    """
    repo_root = mgr.repo_root

    with gr.Column() as root_col:
        gr.Markdown("### ➕ 新建任务")

        # 文件上传
        gr.Markdown("**📁 视频文件**（点击选择或拖拽）")
        file_input = gr.File(
            label="选择视频",
            file_types=[".mp4", ".avi", ".mkv", ".mov", ".webm", ".ts", ".mpeg"],
            type="filepath",
        )
        file_info = gr.Markdown("")

        # 视频播放器 + 时间标尺
        gr.Markdown("**🎥 视频预览**（拖动下方滑块定位到要截取的位置）")
        player_video = gr.Video(
            label="源视频",
            visible=False,
            interactive=False,
        )
        with gr.Row():
            set_start_btn = gr.Button("⏱ 设为开始", variant="secondary")
            set_end_btn = gr.Button("⏱ 设为结束", variant="secondary")
            seek_slider = gr.Slider(
                minimum=0,
                maximum=600,
                step=0.1,
                value=0,
                label="当前时间（秒）",
                interactive=True,
            )
        time_msg = gr.Markdown("")

        # 时间截取
        gr.Markdown("**⏱ 时间截取**（留空 = 不截取，使用完整视频）")
        with gr.Row():
            start_box = gr.Textbox(label="开始时间 (HH:MM:SS)", placeholder="00:00:00", value="")
            end_box = gr.Textbox(label="结束时间 (HH:MM:SS)", placeholder="00:00:00", value="")
        with gr.Row():
            cut_btn = gr.Button("🎬 截取预览", variant="secondary")

        gr.Markdown("**🎬 截取预览**（截取后显示）")
        preview_video = gr.Video(label="截取段预览", visible=False)
        cut_msg = gr.Markdown("")

        # 任务元数据
        gr.Markdown("**📝 任务信息**")
        task_name_box = gr.Textbox(label="任务名（留空 = 用文件名）", placeholder="")

        gr.Markdown("**🔥 任务级热词**（手动输入 + 可从公共库挑选）")
        hotwords_box = gr.Textbox(
            label="热词",
            placeholder="例如：FunASR 张老师",
            lines=2,
        )

        with gr.Row():
            create_btn = gr.Button("✅ 创建任务", variant="primary")
            cancel_btn = gr.Button("❌ 取消", variant="stop")

    # ---------- 事件绑定 ----------

    def on_file_selected(fp):
        """上传后：显示文件信息 + 加载播放器 + 把 slider 最大值设为视频时长。"""
        info, msg = on_upload_video(fp, repo_root)
        duration = get_video_duration(Path(fp)) if fp else 0
        max_t = max(duration, 0.1) if duration else 600
        # 把 duration 缓存到 gr.State 由 time_msg 显示
        dur_str = f"视频时长：{int(duration // 60):02d}:{int(duration % 60):02d}" if duration else ""
        # player_video 显式赋值（Gradio 6 需要这样触发加载）
        return (
            info,
            gr.update(value=fp, visible=True) if fp else gr.update(visible=False),
            gr.update(maximum=max_t, value=0),
            dur_str,
            msg,
        )

    file_input.change(
        fn=on_file_selected,
        inputs=[file_input],
        outputs=[file_info, player_video, seek_slider, time_msg, cut_msg],
    )

    file_input.upload(
        fn=on_file_selected,
        inputs=[file_input],
        outputs=[file_info, player_video, seek_slider, time_msg, cut_msg],
    )

    def on_set_start(seconds: float):
        """把滑块当前秒数格式化为 HH:MM:SS.mmm 填到开始时间框。"""
        return _seconds_to_hms(seconds), f"✅ 开始时间已设为 {_seconds_to_hms(seconds)}"

    def on_set_end(seconds: float):
        return _seconds_to_hms(seconds), f"✅ 结束时间已设为 {_seconds_to_hms(seconds)}"

    set_start_btn.click(
        fn=on_set_start,
        inputs=[seek_slider],
        outputs=[start_box, time_msg],
    )

    set_end_btn.click(
        fn=on_set_end,
        inputs=[seek_slider],
        outputs=[end_box, time_msg],
    )

    cut_btn.click(
        fn=lambda fp, s, e: on_cut_preview(fp, s, e, repo_root),
        inputs=[file_input, start_box, end_box],
        outputs=[preview_video, cut_msg, start_box, end_box],
    )

    # 注意：create_btn 的事件不在这里绑定（app.py 已绑定 on_create_and_return_to_list）
    # 这里绑定会导致事件覆盖，且 app.py 的 outputs 不会被填充

    return {
        "root_col": root_col,
        "file_input": file_input,
        "file_info": file_info,
        "player_video": player_video,
        "seek_slider": seek_slider,
        "set_start_btn": set_start_btn,
        "set_end_btn": set_end_btn,
        "time_msg": time_msg,
        "start_box": start_box,
        "end_box": end_box,
        "cut_btn": cut_btn,
        "preview_video": preview_video,
        "cut_msg": cut_msg,
        "task_name_box": task_name_box,
        "hotwords_box": hotwords_box,
        "create_btn": create_btn,
        "cancel_btn": cancel_btn,
    }


def _seconds_to_hms(seconds: float) -> str:
    """把秒数格式化为 HH:MM:SS.mmm（与 time_utils 一致）。"""
    if seconds is None or seconds < 0:
        return ""
    # 用整数毫秒避免浮点误差
    ms_total = int(round(seconds * 1000))
    hh, rem = divmod(ms_total, 3600 * 1000)
    mm, rem = divmod(rem, 60 * 1000)
    ss, ms = divmod(rem, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"


def build_picker_after(mgr: TaskManager, hotwords_box) -> dict:
    """在 create_task 主组件构造完毕后，由 app.py 调用 — 在 root_col 内插入「从公共库选择」面板。

    Args:
        mgr: TaskManager
        hotwords_box: 上面已构造的任务热词 Textbox（用于接收选中词）

    Returns:
        dict 含 picker 组件 key
    """
    from slirn_home.hotword_ui import build_hotword_picker_components

    repo_root = mgr.repo_root
    return build_hotword_picker_components(repo_root, hotwords_box)
