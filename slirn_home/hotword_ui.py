"""公共热词库 UI — REQ-D 维护 + 选择。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import gradio as gr

from slirn_home.paths import ensure_tasklib_importable

_ensure = ensure_tasklib_importable

from tasklib import HotwordLibrary  # noqa: E402

log = logging.getLogger(__name__)

_PAGE_SIZE = 50   # 5 列 × 10 行
_GRID_COLS = 5
_GRID_ROWS = 10


# ===================== 维护 tab handlers =====================

def on_add_word(
    word: str,
    category: str,
    repo_root: Path,
) -> tuple[dict, str]:
    """添加词 → 刷新库列表 dict + toast。

    Returns:
        (grouped_for_accordion, toast_msg)
    """
    hwlib = HotwordLibrary(repo_root)
    try:
        ok = hwlib.add(word, category=category or "默认")
    except Exception as e:
        return on_refresh_library(repo_root), f"❌ 添加失败: {e}"

    if not ok:
        return on_refresh_library(repo_root), f"⚠️ {word} 已存在"

    return on_refresh_library(repo_root), f"✅ 已添加 {category or '默认'}:{word}"


def on_remove_word(word: str, repo_root: Path) -> tuple[dict, str]:
    """删除词。"""
    hwlib = HotwordLibrary(repo_root)
    try:
        ok = hwlib.remove(word)
    except Exception as e:
        return on_refresh_library(repo_root), f"❌ 删除失败: {e}"
    if not ok:
        return on_refresh_library(repo_root), f"⚠️ 不存在: {word}"
    return on_refresh_library(repo_root), f"✅ 已删除 {word}"


def on_assign_category(
    word: str,
    new_category: str,
    repo_root: Path,
) -> tuple[dict, str]:
    """修改词的分类。"""
    hwlib = HotwordLibrary(repo_root)
    try:
        ok = hwlib.assign_category(word, new_category or "默认")
    except Exception as e:
        return on_refresh_library(repo_root), f"❌ 修改失败: {e}"
    if not ok:
        return on_refresh_library(repo_root), f"⚠️ 词不存在: {word}"
    return on_refresh_library(repo_root), f"✅ {word} → [{new_category or '默认'}]"


def on_refresh_library(repo_root: Path) -> dict:
    """刷新库显示：返回 ``{分类: [词]}`` 字典供 Accordion 渲染。"""
    hwlib = HotwordLibrary(repo_root)
    return hwlib.list_grouped()


# ===================== 选择面板 handlers =====================

def on_search_words(query: str, repo_root: Path) -> tuple[list, str]:
    """搜索过滤 → 返回第 1 页网格 + 页码信息。

    Returns:
        (grid_rows_for_dataframe, status_text)
    """
    hwlib = HotwordLibrary(repo_root)
    filtered = hwlib.search(query)
    page1 = filtered[:_PAGE_SIZE]
    total = len(filtered)
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    status = f"匹配 {total} 个，第 1/{pages} 页"
    return _to_grid(page1), status


def on_page_change(
    query: str,
    page_num: int,
    repo_root: Path,
) -> tuple[list, str]:
    """翻页。"""
    hwlib = HotwordLibrary(repo_root)
    filtered = hwlib.search(query)
    start = page_num * _PAGE_SIZE
    page = filtered[start:start + _PAGE_SIZE]
    total = len(filtered)
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    status = f"匹配 {total} 个，第 {page_num + 1}/{pages} 页"
    return _to_grid(page), status


def on_toggle_select(
    row_idx: int,
    page_start: int,
    query: str,
    selected_state: list,
    repo_root: Path,
) -> tuple[list, list, str]:
    """点击网格某行 → 切换选中状态。

    Returns:
        (new_grid_rows, new_selected_list, status)
    """
    hwlib = HotwordLibrary(repo_root)
    filtered = hwlib.search(query)
    if row_idx < 0 or row_idx >= len(filtered):
        # 选不到有效词，重渲当前页
        start = page_start
        page = filtered[start:start + _PAGE_SIZE]
        return _to_grid_selected(page, set(selected_state)), list(selected_state), "无效选择"

    word = filtered[row_idx]
    sel = set(selected_state)
    if word in sel:
        sel.discard(word)
    else:
        sel.add(word)
    new_selected = sorted(sel)
    # 重渲当前页（应用新选中标记）
    start = page_start
    page = filtered[start:start + _PAGE_SIZE]
    return _to_grid_selected(page, sel), new_selected, f"已选 {len(sel)} 个"


def on_confirm_selection(
    selected: list,
    existing_text: str,
) -> tuple[str, str]:
    """确认选择：把选中词追加到现有 Textbox。"""
    existing_raw = existing_text or ""
    existing = [w.strip() for w in re.split(r"[,\n，;；、]+", existing_raw) if w.strip()]
    merged = list(dict.fromkeys(existing + selected))  # 去重保持顺序
    # REQ-20260926-NNN：用半角逗号拼回（不能再用空格，否则多词热词 +
    # 相邻词会被空格合到一起，下游用新分隔符解析时塌成一个词）
    new_text = ",".join(merged)
    return new_text, f"✅ 已添加 {len(selected)} 个（共 {len(merged)} 个）"


def on_clear_selection() -> tuple[list, str]:
    """清空选择。"""
    return [], "已清空选择"


# ===================== UI 构造 =====================

def _to_grid(words: list[str]) -> list[list[str]]:
    """词列表 → 5 列网格（5×10 = 50/页，词不足时填空字符串）。"""
    grid: list[list[str]] = []
    for r in range(_GRID_ROWS):
        row: list[str] = []
        for c in range(_GRID_COLS):
            idx = r * _GRID_COLS + c
            row.append(words[idx] if idx < len(words) else "")
        grid.append(row)
    return grid


def _to_grid_selected(words: list[str], selected: set[str]) -> list[list[str]]:
    """词列表 + 选中集合 → 网格（选中的词加 ✓ 前缀）。"""
    raw = _to_grid(words)
    return [[f"✓ {w}" if w in selected and w else w for w in row] for row in raw]


def build_hotword_library_components(repo_root: Path) -> dict:
    """构造「热词库管理」tab 的组件。

    Returns:
        dict 含 ``add_word_box / add_category_box / category_select / add_btn /
        refresh_btn / library_accordion / library_md / status_md``
    """
    initial_grouped = on_refresh_library(repo_root)

    with gr.Column() as root_col:
        gr.Markdown("### 📚 热词库管理")

        # ---- 添加 ----
        gr.Markdown("**➕ 添加新词**")
        with gr.Row():
            add_word_box = gr.Textbox(label="词", placeholder="例如：张老师")
            add_category_box = gr.Textbox(label="分类", placeholder="例如：讲师（留空 = 默认）")
            add_word_btn = gr.Button("➕ 添加", variant="primary")

        gr.Markdown("**➕ 添加新分类**（仅记录到内存，无词也可）")
        with gr.Row():
            new_category_box = gr.Textbox(label="新分类名", placeholder="例如：产品名")
            add_category_btn = gr.Button("➕ 添加分类")

        # ---- 现有词列表 ----
        gr.Markdown("**📋 现有词**（按分类折叠展示）")
        library_md = gr.JSON(
            value=initial_grouped,
            label="分类 → 词列表",
            show_label=True,
        )
        with gr.Row():
            refresh_btn = gr.Button("🔄 刷新", variant="secondary")
            remove_word_box = gr.Textbox(label="要删除的词", placeholder="")
            remove_btn = gr.Button("🗑️ 删除", variant="stop")

        status_md = gr.Markdown("")

    # ---------- 事件绑定 ----------

    add_word_btn.click(
        fn=lambda w, c: on_add_word(w, c, repo_root),
        inputs=[add_word_box, add_category_box],
        outputs=[library_md, status_md],
    )

    add_category_btn.click(
        fn=lambda c: (on_refresh_library(repo_root), f"✅ 分类「{c}」已记录（添加词时归入该分类）"),
        inputs=[new_category_box],
        outputs=[library_md, status_md],
    )

    refresh_btn.click(
        fn=lambda: (on_refresh_library(repo_root), "已刷新"),
        inputs=[],
        outputs=[library_md, status_md],
    )

    remove_btn.click(
        fn=lambda w: on_remove_word(w, repo_root),
        inputs=[remove_word_box],
        outputs=[library_md, status_md],
    )

    return {
        "root_col": root_col,
        "add_word_box": add_word_box,
        "add_category_box": add_category_box,
        "add_word_btn": add_word_btn,
        "new_category_box": new_category_box,
        "add_category_btn": add_category_btn,
        "refresh_btn": refresh_btn,
        "remove_word_box": remove_word_box,
        "remove_btn": remove_btn,
        "library_md": library_md,
        "status_md": status_md,
    }


def build_hotword_picker_components(
    repo_root: Path,
    target_textbox: gr.Textbox,
) -> dict:
    """构造「从公共库选择」面板（在新建任务 tab 内嵌）。

    Args:
        repo_root: 仓库根
        target_textbox: 要追加到的任务热词 Textbox（由 app.py 引用注入）
    """
    initial_grid, initial_status = on_search_words("", repo_root)

    with gr.Accordion("🔥 从公共库选择", open=False) as picker_acc:
        gr.Markdown("**🔍 搜索 + 5×10 网格选择**（点击行切换选中）")

        search_box = gr.Textbox(label="🔍 搜索", placeholder="输入过滤词（留空 = 全部）")

        page_state = gr.State(0)            # 当前页码（0-based）
        selected_state = gr.State([])       # 选中词列表

        grid_df = gr.Dataframe(
            value=initial_grid,
            headers=["", "", "", "", ""],
            interactive=False,
            wrap=True,
            label="热词网格（5×10）",
        )
        status_md = gr.Markdown(initial_status)

        with gr.Row():
            prev_btn = gr.Button("⬅️ 上一页")
            next_btn = gr.Button("➡️ 下一页")

        with gr.Row():
            confirm_btn = gr.Button("✅ 确认选择", variant="primary")
            clear_btn = gr.Button("🗑️ 清空选择")

    # ---------- 事件 ----------

    search_box.change(
        fn=lambda q: (*on_search_words(q, repo_root), 0),
        inputs=[search_box],
        outputs=[grid_df, status_md, page_state],
    )

    grid_df.select(
        fn=lambda q, p, sel, evt: on_toggle_select(
            evt.index[0] if evt.index else -1,
            p * _PAGE_SIZE,
            q,
            sel,
            repo_root,
        ),
        inputs=[search_box, page_state, selected_state],
        outputs=[grid_df, selected_state, status_md],
    )

    prev_btn.click(
        fn=lambda q, p: (q, max(0, p - 1), *on_page_change(q, max(0, p - 1), repo_root)),
        inputs=[search_box, page_state],
        outputs=[search_box, page_state, grid_df, status_md],
    )

    next_btn.click(
        fn=lambda q, p: (q, p + 1, *on_page_change(q, p + 1, repo_root)),
        inputs=[search_box, page_state],
        outputs=[search_box, page_state, grid_df, status_md],
    )

    confirm_btn.click(
        fn=lambda sel, existing: on_confirm_selection(sel, existing),
        inputs=[selected_state, target_textbox],
        outputs=[target_textbox, status_md],
    )

    clear_btn.click(
        fn=lambda: ([], "已清空选择", []),
        inputs=[],
        outputs=[grid_df, status_md, selected_state],
    )

    return {
        "picker_acc": picker_acc,
        "search_box": search_box,
        "grid_df": grid_df,
        "status_md": status_md,
        "prev_btn": prev_btn,
        "next_btn": next_btn,
        "confirm_btn": confirm_btn,
        "clear_btn": clear_btn,
        "page_state": page_state,
        "selected_state": selected_state,
    }
