"""热词分析服务 — REQ-20260926-NNN：从文本/URL 提取候选热词（同步 LLM 调用）。

设计要点：
- 抽独立模块（不塞 revision_service）保持领域独立；
- prompt 抄 optimize_service.OPT_SYSTEM 的「判断信号 + 输出 JSON」骨架；
- 复用 revision_service._call_llm（双协议 + 退避重试）；
- JSON 解析抄 parse_llm_suggestions 的四层防御（剥围栏 + 截取 + 失败抛 ValueError）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```")


def build_system_prompt() -> str:
    """热词提取系统提示词。"""
    return (
        "你是中文文本「热词提取」助手。用户会给你一段文本（含专有名词/人名/术语/书名等），"
        "请提取值得作为 ASR 热词增强的「词或短语」列表。\n\n"
        "提取规则：\n"
        "- 优先：人名/讲师名/嘉宾名、机构/产品/品牌名、专业术语/技术名词、"
        "书名/课程名、英文专有名词\n"
        "- 同义/同音近形只保留最规范的 1 个写法\n"
        "- 排除：通用动词/形容词/连词（「我们」「那么」「然后」等）、单字语气词\n"
        "- 给出 5-30 个候选，按重要度从高到低排序\n"
        "- 词长 2-12 字（含英文短语），避免整句\n\n"
        "输出要求：只输出 JSON 对象，不要任何其他文字、解释或 markdown 围栏。"
        "格式严格遵循：\n"
        '{"words": [{"word": "词1", "category": "人物|机构|技术|品牌|书名|其他"},'
        '{"word": "词2", "category": "..."}]}\n'
        "category 可选，缺省归到「其他」。"
    )


def build_user_prompt(source: str, page_hint: str = "") -> str:
    """热词提取用户提示词。source 是拼接后的纯文本（输入框 + URL 内容）。
    page_hint（可选）：用户对 URL 页面的区域/选择器/文字说明（如
    "#article"、"正文部分"），用于指导 LLM 聚焦相关文字区域。
    """
    head = "请从以下文本中提取 ASR 热词候选（5-30 个，按重要度排序）："
    if page_hint:
        head += (
            "\n\n【页面区域提示】用户指定重点关注：\n"
            f"{page_hint}\n"
            "（可能是 CSS 选择器如 #article / .post-content，或文字描述如「找正文/评论区/目录」。"
            "请优先从与提示匹配的区域提取词；无匹配时退回全文。）"
        )
    return head + "\n\n" + source


def parse_llm_hotwords(raw: str) -> list[dict[str, str]]:
    """防御式解析模型输出 → [{"word": ..., "category": ...}, ...]。

    容错：
    - 剥 ````json` 围栏；
    - 优先截取首个 '{' 到末个 '}'（对象形态）；fallback 首个 '[' 到末个 ']'
      （数组形态）；
    - 支持三种输出形态：
        (a) {"words": ["词1", "词2"]}
        (b) {"words": [{"word":"词1","category":"人物"}, ...]}
        (c) ["词1", {"word":"词2",...}]
    - 失败抛 ValueError。
    """
    cleaned = _FENCE_RE.sub("", raw or "").strip()
    # 看首个非空白字符决定外层形态（不能只看 find('{')，否则数组里的 dict
    # 元素的 { 会被误认为外层对象起点，截出无效 JSON）
    first_brace = next((c for c in cleaned if c in "{["), None)
    if first_brace == "{":
        start, end = cleaned.find("{"), cleaned.rfind("}")
    elif first_brace == "[":
        start, end = cleaned.find("["), cleaned.rfind("]")
    else:
        raise ValueError("模型输出既不是 JSON 对象也不是数组")
    if start < 0 or end <= start:
        raise ValueError(f"模型输出找不到匹配的 JSON 边界（首字符 {first_brace!r}）")
    candidate = cleaned[start:end + 1]
    try:
        obj: Any = __import__("json").loads(candidate)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"模型输出不是合法 JSON: {e}") from e

    items: list[Any]
    if isinstance(obj, dict):
        items = obj.get("words", [])
        if not isinstance(items, list):
            raise ValueError("模型输出对象的 'words' 字段不是数组")
    elif isinstance(obj, list):
        items = obj
    else:
        raise ValueError("模型输出既不是 JSON 对象也不是数组")

    out: list[dict[str, str]] = []
    for it in items:
        if isinstance(it, str):
            w = it.strip()
            if w:
                out.append({"word": w, "category": "其他"})
        elif isinstance(it, dict):
            w = str(it.get("word") or "").strip()
            if not w:
                continue
            cat = str(it.get("category") or "其他").strip() or "其他"
            out.append({"word": w, "category": cat})
    if not out:
        raise ValueError("模型输出解析后无有效热词")
    return out


def extract_hotwords(source: str, *, entry: dict | None = None,
                     page_hint: str = "") -> list[dict[str, str]]:
    """调 LLM 提取热词 → 返回 [{"word","category"}, ...]。失败抛 ValueError/RuntimeError。

    page_hint: 透传到 build_user_prompt，指导 LLM 在 URL 内容里聚焦特定区域。
    """
    from slirn_home import revision_service  # 延迟导入避免循环

    raw = revision_service._call_llm(
        build_system_prompt(), build_user_prompt(source, page_hint=page_hint),
        entry=entry, retries=2,
    )
    return parse_llm_hotwords(raw)