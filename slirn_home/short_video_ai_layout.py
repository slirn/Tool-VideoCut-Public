"""REQ-20261013-105：短视频精剪 AI 智能布局（v1）→ REQ-20261018-170 v2 改版。

v2 流程（用户语义）：
1. 读取「背景区检测说明」（params.materials.bg_desc；说明指定要检测哪些
   区域、每个区域的作用、期望检出什么内容）
2. 按说明生成 AI 识别提示词（说明是提示词的核心指令段）
3. 调 LLM 识别说明里点名的区域（regions 列表 + 映射到 4 个标准槽位 layout）
4. 识别结果落 ``params.ai_layout_result``（regions / layout / 生成提示词 /
   时间），前端在检测区下方渲染结果卡

v1 简化保留：
- 不引入多模态 LLM SDK；调 ``revision_service._call_llm`` 走 text 通道
- 防御式解析：剥 ```json 围栏 + 截首个 {..}；非法字段丢弃/钳制
- LLM 不可用：fallback 默认布局（1080x1920 全屏 + 字幕底部 1/4），
  result.source = "fallback"
- ``parse_reference_layout``（只返回 4 区域 layout 的旧口径）保留为薄包装，
  供旧测试与调用方使用

REQ-20261017-163：参考图槽移除 —— 布局输入改为背景图 + 背景图说明
（函数名 parse_reference_layout 为历史命名，保留）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from slirn_home import short_video_service as svc

log = logging.getLogger(__name__)


_AI_LAYOUT_SYSTEM_PROMPT = """你是短视频背景图区域识别专家。用户会给出一段「背景区检测说明」，说明里指定了要在背景图（1080x1920 竖屏）上检测哪些区域、每个区域的作用、期望检出什么内容。

你的任务：按说明逐条识别这些区域在 1080x1920 画布上的位置和大小，并映射到合成用的 4 个标准槽位。

输出 JSON：
{
  "regions": [
    {"name": "视频区", "purpose": "放视频", "x": 0, "y": 648, "w": 1080, "h": 624, "reason": "说明指定中间透明抠图区放视频"},
    {"name": "字幕条", "purpose": "放字幕", "x": 60, "y": 1620, "w": 960, "h": 240, "reason": "底部横条"}
  ],
  "layout": {
    "video": {"x": 0, "y": 648, "w": 1080, "h": 624, "reason": "映射自视频区"},
    "subtitle": {"x": 60, "y": 1620, "w": 960, "h": 240, "reason": "映射自字幕条"},
    "cover": {"x": 0, "y": 0, "w": 1080, "h": 1080, "reason": "顶部封面"},
    "bg": {"x": 0, "y": 0, "w": 1080, "h": 1920, "reason": "整图"}
  }
}

规则：
- regions 必须覆盖说明里点名的每一个区域，name 用说明里的称呼，purpose 写该区域的作用
- layout 映射：放视频的区域 → video；字幕/文字条 → subtitle；封面/顶部展示 → cover；bg 恒为整图 (0,0,1080,1920)
- 说明没提到的槽位用默认：video 全屏 (0,0,1080,1920)、subtitle 底部 (60,1620,960,240)、cover 顶部 (0,0,1080,1080)
- 所有坐标 + 宽高必须 0..1080 / 0..1920 之内；reason 一句话
只输出 JSON，不要任何解释或代码块标记。"""


_AI_LAYOUT_USER_TMPL = """任务：按照「背景区检测说明」识别背景图的各个区域。
{bg_note}
{bg_desc_note}
布局风格偏好：{style_hint}

输出 JSON：regions（说明点名的每个区域：name/purpose/x/y/w/h/reason）+
layout（映射到 video/subtitle/cover/bg 四槽位），坐标在 1080x1920 内。"""


def _strip_json(raw: str) -> str:
    text = str(raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence:
        return fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _normalize_region(d: dict) -> dict | None:
    if not isinstance(d, dict):
        return None
    try:
        return {
            "x": max(0, min(1080, int(d.get("x") or 0))),
            "y": max(0, min(1920, int(d.get("y") or 0))),
            "w": max(0, min(1080, int(d.get("w") or 0))),
            "h": max(0, min(1920, int(d.get("h") or 0))),
            "reason": str(d.get("reason") or "").strip(),
        }
    except (TypeError, ValueError):
        return None


def _normalize_ai_region(d: dict) -> dict | None:
    """regions 列表项：_normalize_region + name/purpose 字段。"""
    base = _normalize_region(d)
    if not base:
        return None
    return {
        "name": str(d.get("name") or "未命名区域").strip()[:30] or "未命名区域",
        "purpose": str(d.get("purpose") or "").strip()[:80],
        **base,
    }


def _default_layout() -> dict[str, dict]:
    """无背景图 / LLM 失败时的 fallback 默认布局。"""
    return {
        "video": {"x": 0, "y": 0, "w": 1080, "h": 1920, "reason": "主视频全屏"},
        "subtitle": {"x": 60, "y": 1620, "w": 960, "h": 240, "reason": "字幕底部"},
        "cover": {"x": 0, "y": 0, "w": 1080, "h": 1080, "reason": "顶部封面"},
        "bg": {"x": 0, "y": 0, "w": 1080, "h": 1920, "reason": "背景全屏"},
    }


def _build_prompts(bg_path: str, desc: str,
                   style_hint: str) -> tuple[str, str]:
    """REQ-20261018-170：按「背景区检测说明」生成识别提示词。

    说明是提示词的核心指令段（要检测哪些区域/各区域作用/期望内容）；
    无说明时给通用识别指令（仅 parse_reference_layout 旧口径会走到）。
    """
    root_note = ""
    if bg_path:
        bg_note = f"背景图路径：{bg_path}（text 通道不真读图，按说明推理）"
    else:
        bg_note = "无背景图（按说明与默认布局输出）"
    desc = str(desc or "").strip()
    if desc:
        bg_desc_note = (f"背景区检测说明（识别指令，逐条识别其中点名的区域）："
                        f"{desc[:800]}")
    else:
        bg_desc_note = ("背景区检测说明：（用户未填写；按通用规则识别常见区域："
                        "视频区 / 字幕条 / 封面 / 背景）")
    user_prompt = _AI_LAYOUT_USER_TMPL.format(
        bg_note=bg_note,
        bg_desc_note=bg_desc_note,
        style_hint=style_hint or "现代简洁",
    )
    return _AI_LAYOUT_SYSTEM_PROMPT, user_prompt


def _parse_llm_payload(data: dict) -> tuple[dict, list[dict]]:
    """LLM JSON → (layout 4 槽位, regions 列表)。

    兼容两种返回：v2 {regions, layout} 与 v1 顶层 4 键（layout 缺省时
    读顶层 video/subtitle/cover/bg）。
    """
    layout = _default_layout()
    nested = data.get("layout") if isinstance(data.get("layout"), dict) else {}
    for k in ("video", "subtitle", "cover", "bg"):
        norm = _normalize_region(nested.get(k) or data.get(k))
        if norm:
            layout[k] = norm
    regions: list[dict] = []
    raw_regions = data.get("regions")
    if isinstance(raw_regions, list):
        for r in raw_regions:
            if not isinstance(r, dict):
                continue  # 非 dict 项直接丢弃，不生成零区域
            norm = _normalize_ai_region(r)
            if norm:
                regions.append(norm)
    return layout, regions


def _stored_desc(repo_root: Path, task_id: str, project_id: str) -> tuple[str, str]:
    """读已存 params：materials.bg（背景图）+ materials.bg_desc（说明）。"""
    project = svc.load_project(repo_root, task_id, project_id)
    from slirn_home import short_video_fine as _fine
    params = _fine._get_params(project)
    mats = params.get("materials") or {}
    return str(mats.get("bg") or ""), str(mats.get("bg_desc") or "").strip()


def run_ai_layout(repo_root: Path | str, task_id: str, project_id: str,
                  *, style_hint: str = "现代简洁", bg_path: str = "",
                  bg_desc: str = "") -> dict:
    """REQ-20261018-170 v2 主入口：说明 → 生成提示词 → LLM 识别 → 落盘。

    1. 说明/背景图未显式传时读已存 params（「首先要读取背景区检测说明」）
    2. 说明为空 → 报错（说明是识别指令的来源，没有它无从识别）
    3. LLM 失败/解析失败 → fallback 默认布局，result.source = "fallback"
    4. layout 写入 params.layout；识别结果写 params.ai_layout_result
    返回 {layout, result}。
    """
    from slirn_home import revision_service

    root = Path(repo_root)
    bg, desc = str(bg_path or ""), str(bg_desc or "").strip()
    if not desc or not bg:
        s_bg, s_desc = _stored_desc(root, task_id, project_id)
        bg = bg or s_bg
        desc = desc or s_desc
    if not desc:
        raise svc.ShortVideoError(
            "请先填写「背景区检测说明」（要检测哪些区域、各区域的作用、"
            "期望检出什么内容），AI 按这段说明生成识别提示词")

    system_prompt, user_prompt = _build_prompts(bg, desc, style_hint)
    source = "llm"
    raw = ""
    try:
        # ⚠️ 只传位置参数 —— 真实 _call_llm(system, user, entry, retries,
        # max_tokens) 没有 timeout 形参；带错 kwarg 会 TypeError 被下方
        # except 吞掉、静默回落默认布局（REQ-163 前真机从未调通 LLM）。
        raw = revision_service._call_llm(  # noqa: SLF001
            system_prompt, user_prompt,
        )
        data = json.loads(_strip_json(raw))
        if not isinstance(data, dict):
            raise ValueError("LLM 顶层输出不是 JSON 对象")
        layout, regions = _parse_llm_payload(data)
    except Exception as e:  # noqa: BLE001 — LLM 不可用兜底默认
        log.warning("[ai-layout] LLM 调用/解析失败，用默认布局: %s", e)
        layout, regions = _default_layout(), []
        source = "fallback"

    apply_ai_layout(root, task_id, project_id, layout)
    result: dict[str, Any] = {
        "regions": regions,
        "layout": layout,
        "prompt": user_prompt,
        "desc": desc[:800],
        "source": source,
        "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _persist_ai_result(root, task_id, project_id, result)
    return {"layout": layout, "result": result}


def _persist_ai_result(repo_root: Path, task_id: str, project_id: str,
                       result: dict) -> None:
    """识别结果落 params.ai_layout_result（前端检测区下方渲染结果卡）。"""
    project = svc.load_project(repo_root, task_id, project_id)
    from slirn_home import short_video_fine as _fine
    params = _fine._get_params(project)
    params["ai_layout_result"] = result
    s = svc.get_stage_state(project, "stage6_fine")
    s["params"] = params
    svc.set_stage_state(project, "stage6_fine", s)
    svc.save_project(repo_root, project)


def parse_reference_layout(repo_root: Path | str, task_id: str, project_id: str,
                           *, style_hint: str = "现代简洁",
                           bg_path: str = "",
                           bg_desc: str = "") -> dict:
    """旧口径薄包装：调 LLM 只返回 4 区域 layout（v2 用 run_ai_layout）。

    REQ-20261017-163：bg_desc = 用户写的背景图说明，直接进提示词 ——
    text 通道不真读图，说明是目前 LLM 理解背景图的唯一画面线索。
    """
    from slirn_home import revision_service

    system_prompt, user_prompt = _build_prompts(bg_path, bg_desc, style_hint)
    try:
        raw = revision_service._call_llm(  # noqa: SLF001
            system_prompt, user_prompt,
        )
        data = json.loads(_strip_json(raw))
        if not isinstance(data, dict):
            raise ValueError("LLM 顶层输出不是 JSON 对象")
        layout, _ = _parse_llm_payload(data)
        return layout
    except Exception as e:  # noqa: BLE001 — LLM 不可用兜底默认
        log.warning("[ai-layout] LLM 调用失败，用默认布局: %s", e)
        return _default_layout()


def apply_ai_layout(repo_root: Path | str, task_id: str, project_id: str,
                    layout: dict) -> dict:
    """把 LLM 解析的 layout 写入 params.layout。"""
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    from slirn_home import short_video_fine as _fine
    params = _fine._get_params(project)
    # 校验 4 区域
    for k in ("video", "subtitle", "cover", "bg"):
        norm = _normalize_region((layout or {}).get(k))
        if norm:
            (params.setdefault("layout", {}))[k] = norm
    s = svc.get_stage_state(project, "stage6_fine")
    s["params"] = params
    svc.set_stage_state(project, "stage6_fine", s)
    svc.save_project(root, project)
    return {"layout": params["layout"]}
