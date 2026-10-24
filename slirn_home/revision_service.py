"""字幕修订服务 — REQ-20260915-005。

把上一阶段生成的 subtitle.json 交给大模型（用户在 ⚙️ 注册的当前模型，
OpenAI 兼容 / Anthropic 双协议，API Key 从环境变量读取 — REQ-008/REQ-20260916-001），
逐段产出处理建议（keep 保留 / delete 整行删除 / split 切分修剪 /
fix 内容更正（错字别字，附更正后文本）/ review 人工复核），
落盘 outputs/revision.json；用户逐条决策（decision + user_note）合并回写。
后台线程 job 管理与 asr_service 同模式（内存 job 表 + 阶段级进度）。

设计见 docs/design/DESIGN-20260915-005-subtitle-review.md。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

REVISION_JSON = "revision.json"

# 模型建议类别 → (中文标签, 徽章色)
LLM_CATEGORIES: dict[str, tuple[str, str]] = {
    "keep": ("完整保留", "keep"),
    "delete": ("整行删除", "delete"),
    "split": ("切分修剪", "split"),
    "fix": ("内容更正", "fix"),
    "review": ("人工复核", "review"),
}

# 手动决策类别 → 中文标签（pending = 未决策）
USER_DECISIONS: dict[str, str] = {
    "pending": "未决策",
    "accept": "采纳建议",
    "keep": "保留",
    "delete": "删除",
    "split": "切分",
    "fix": "内容更正",
}

# 长字幕分批：每批段数（qwen-plus 上下文/输出长度安全边际；
# 20 段/批输出约 2K tokens，留足余量，解析失败还会减半重试 — REQ-20260915-007）
BATCH_SIZE = 20

# 分析严谨性级别（REQ-20260916-003，用户必选）：
# - prompt 侧：badge/title/stance/criteria 注入系统提示词，决定模型挑毛病的严格程度
# - UI 侧：desc/example 渲染成单选卡，给操作者体感（描述 + 例子）
RIGOR_LEVELS: dict[str, dict] = {
    "high": {
        "badge": "高",
        "title": "严格打磨",
        "stance": "以成品口播稿的标准逐句精修，任何影响观感的语气词、口癖、重复、寒暄都要处理。",
        "criteria": (
            '- "keep"：有效内容，完整保留。\n'
            '- "delete"：整行删除 — 纯口癖、口头禅、语气词（嗯、啊、呃、那个、就是说、然后等）、'
            "无意义寒暄、与主题无关的废话、整行都是重复啰嗦。\n"
            '- "split"：行内需进一步切分/修剪 — 例如一句话中重复多次的内容只保留一次；'
            "有效内容中夹杂的语气词应剔除。keep_text 字段给出建议保留后的文本。\n"
            '- "review"：无法判断（语义不明、可能依赖上下文）。'
        ),
        "desc": "逐句精修到成品口播稿水平：语气词、口癖、重复、无意义寒暄全部处理。"
                "适合对外发布、追求专业观感的成片。",
        "example": "「嗯呃，那个，就是说，我们今天讲一下神经网络」→ 修剪为「我们今天讲一下神经网络」",
    },
    "medium": {
        "badge": "中",
        "title": "意思正确即可",
        "stance": "只处理影响理解的问题：每句话表达的意思正确、没有太大瑕疵即可，小语气词可以容忍保留。",
        "criteria": (
            '- "keep"：意思表达正确的内容，完整保留 — 个别语气词（嗯、呃、啊等）若不影响理解，'
            "保留即可，不必强求剔除。\n"
            '- "delete"：整行删除 — 仅限完全无意义的内容：纯寒暄、与主题无关的废话、'
            "整行重复啰嗦到没有信息量。\n"
            '- "split"：行内需进一步切分/修剪 — 仅限明显问题：明显口误/用词错误、'
            "同一内容连续重复多次、明显不通顺需小幅修剪的句子。keep_text 字段给出建议保留后的文本。\n"
            '- "review"：无法判断（语义不明、可能依赖上下文）。'
        ),
        "desc": "只处理影响理解的问题：明显口误、连续重复、明显不通顺的句子做小幅修剪；"
                "小语气词可以保留。适合内部学习、速览回看。",
        "example": "「嗯，我们今天讲一下神经网络」→ 整行保留（小语气词「嗯」不影响意思）",
    },
    "low": {
        "badge": "低",
        "title": "只去严重问题",
        "stance": "最大限度保留原文，只处理过多重复和意思混乱的句子；允许字幕存在部分小瑕疵。",
        "criteria": (
            '- "keep"：默认选择 — 句子能看懂大意就保留；语气词、轻微口误、个别小重复均不处理。\n'
            '- "delete"：整行删除 — 仅限严重影响观看的内容：大段纯重复、'
            "完全无意义、与主题无关的废话。\n"
            '- "split"：行内需进一步切分/修剪 — 仅限意思混乱/逻辑断裂、'
            "同一内容过多重复的句子。keep_text 字段给出建议保留后的文本。\n"
            '- "review"：无法判断（语义不明、可能依赖上下文）。'
        ),
        "desc": "最大限度保留原文：只处理大段纯重复和意思混乱的句子，语气词、轻微口误都保留。"
                "适合只要大意、尽快出片。",
        "example": "「所以我们所以我们所以我们看到」→ 修剪为「所以我们看到」（只去掉连续重复）",
    },
}


# 内容更正判定（REQ-20260916-006）— 错字/别字是 ASR 听写层面的文字错误，
# 与严谨性级别无关：任何级别下都应更正（用户原话"直接把字幕内容修订为正确的内容"）。
# 边界：说话人本身口误 / 需要修剪语义 ≠ 文字错误，仍按 keep/delete/split/review 判。
FIX_CRITERIA = (
    '- "fix"：字幕文字存在错字、别字、同音字误识别等**文字错误**时选用'
    "（与严谨性级别无关，任何级别都必须判）：keep_text 给出更正后的完整字幕文本；"
    "note 给出原文与更正的对照说明（原文哪个字有误、应更正为什么）。"
    "注意：说话人本身口误或需修剪语义的情况不判 fix，仍按其余类别处理。"
)


def build_system_prompt(rigor: str = "high") -> str:
    """按严谨性级别组装系统提示词（未知级别回退 high）。"""
    cfg = RIGOR_LEVELS.get(rigor) or RIGOR_LEVELS["high"]
    return (
        "你是专业的视频字幕修订顾问。用户会给你视频字幕段列表（JSON 数组，每项含序号 i、起止时间、文本）。"
        "请对**每一段**判断修订方式，并给出具体分析说明。\n\n"
        f"本次分析严谨性级别：{cfg['badge']}（{cfg['title']}）。{cfg['stance']}\n\n"
        "判定标准：\n"
        f"{cfg['criteria']}\n"
        f"{FIX_CRITERIA}\n\n"
        "输出要求：只输出 JSON 数组，不要任何其他文字。每项格式：\n"
        '{"i": 段序号, "category": "keep|delete|split|fix|review", "keep_text": '
        '"建议保留的文本（split=修剪后文本 / fix=更正后完整文本，其余为 null）", '
        '"note": "具体分析说明：指出问题词、为何删/留/切，切分的依据；fix 需给出原文与更正的对照"}\n\n'
        "note 必须具体（例如：「行首『嗯』为语气词；『大家好』重复 2 次建议保留 1 次」；"
        "「『神精网络』应为『神经网络』（同音误识别），更正文本见 keep_text」），不要泛泛而谈。"
        "必须覆盖输入的每一个 i。"
    )


# 自定义严谨性（REQ-20260916-007）：第四档 — 用户在默认底稿上自由修改提示词。
# 底稿取高档（判定标准最完整），fix 更正与 JSON 输出要求都在底稿里，改坏也有解析防御兜底。
CUSTOM_RIGOR_KEY = "custom"


def default_custom_prompt() -> str:
    """自定义严谨性的默认提示词（底稿 = 高档「严格打磨」完整系统提示词）。"""
    return build_system_prompt("high")


def resolve_system_prompt(rigor: str, custom_prompt: str | None = None) -> str:
    """rigor → 实际使用的系统提示词。

    - custom：原样使用用户提示词（空白回退默认底稿 — 与前端「恢复默认」闭环）
    - 高/中/低：按级别组装；未知级别 ValueError（与 start_job 校验口径一致）
    """
    if rigor == CUSTOM_RIGOR_KEY:
        text = str(custom_prompt or "").strip()
        return text or default_custom_prompt()
    if rigor not in RIGOR_LEVELS:
        raise ValueError(f"未知严谨性级别: {rigor!r}（可选 高 / 中 / 低 / 自定义）")
    return build_system_prompt(rigor)


def build_user_prompt(task_name: str, hotwords: list[str], segments: list[dict]) -> str:
    """构造 user 消息：任务上下文 + 热词（保护专有名词不被误删）+ 段列表。"""
    lines = [f"视频任务：{task_name or '（未命名）'}"]
    if hotwords:
        lines.append(f"任务热词（专有名词/人名等，判定时注意不要误删）：{'、'.join(hotwords)}")
    payload = [
        {"i": int(s["i"]), "start": s.get("start", ""), "end": s.get("end", ""), "text": s.get("text", "")}
        for s in segments
    ]
    lines.append("字幕段列表：")
    lines.append(json.dumps(payload, ensure_ascii=False))
    return "\n".join(lines)


# =============== 大模型调用（拆出便于测试 monkeypatch） ===============

# OpenAI 兼容 / Anthropic 错误码 → 中文说明（让用户一眼知道该怎么办 — REQ-20260915-007/008）
_CODE_ZH: dict[str, str] = {
    "Arrearage": "账户欠费/余额不足，请到对应厂商控制台充值后重试",
    "insufficient_quota": "配额/余额不足，请到对应厂商控制台充值后重试",
    "InvalidApiKey": "API Key 无效，请检查对应环境变量中的 Key",
    "invalid_api_key": "API Key 无效，请检查对应环境变量中的 Key",
    "authentication_error": "API Key 无效，请检查对应环境变量中的 Key",
    "Throttling": "调用被限流（请求过频或配额用尽），请稍后重试",
    "rate_limit_exceeded": "调用被限流，请稍后重试",
    "rate_limit_error": "调用被限流，请稍后重试",
    "InvalidParameter": "请求参数不合法",
    "invalid_request_error": "请求参数不合法",
    "AccessDenied": "无访问权限（可能未开通对应模型服务）",
    "permission_error": "无访问权限（可能未开通对应模型服务）",
    "DataInspectionFailed": "内容安全审查未通过",
    "model_not_found": "模型不存在（检查模型名是否为该厂商提供）",
    "not_found_error": "模型不存在（检查模型名是否为该厂商提供）",
    "overloaded_error": "服务端过载，请稍后重试",
}


def _code_zh(code: str, status: int) -> str:
    if code in _CODE_ZH:
        return _CODE_ZH[code]
    if code and code.startswith("Throttling"):
        return _CODE_ZH["Throttling"]
    return f"HTTP {status}"


class LLMBusinessError(RuntimeError):
    """确定性业务失败（欠费/Key 无效/模型不存在等）— 重试无意义，直接上抛。

    限流/过载类（Throttling*/rate_limit*/overloaded_error）仍抛普通
    RuntimeError，由 _chat_completion 退避重试。
    """


def _is_retryable_code(code: str) -> bool:
    c = str(code or "")
    return (c in ("rate_limit_exceeded", "rate_limit_error", "overloaded_error")
            or c.startswith("Throttling"))


def _extract_openai_resp(resp, entry: dict) -> str:
    """解析 OpenAI 兼容响应 → 助手正文。

    网关业务失败（欠费/限流/Key 无效）返回非 200 + error 对象而非抛异常 —
    必须先查状态码，否则真实错误被掩盖（REQ-20260915-007）。
    输出被截断（finish_reason=length）→ ValueError（上层减半重试）。
    """
    model = entry.get("id", "?")
    provider = entry.get("provider", "未知厂商")
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — 非 JSON 响应按原文报错
        data = None
    if resp.status_code != 200:
        err = data.get("error") if isinstance(data, dict) else None
        err = err if isinstance(err, dict) else {}
        code = str(err.get("code") or resp.status_code)
        msg = str(err.get("message") or resp.text[:300] or "无错误详情")
        rid = resp.headers.get("x-request-id") or resp.headers.get("X-Request-Id") or ""
        err_cls = RuntimeError if _is_retryable_code(code) else LLMBusinessError
        raise err_cls(
            f"{provider}（{model}）[{code}] {_code_zh(code, resp.status_code)}：{msg}"
            + (f"（request_id={rid}）" if rid else "")
        )
    if not isinstance(data, dict):
        raise LLMBusinessError(f"{provider}（{model}）响应不是 JSON 对象")
    if data.get("error"):  # 个别网关 200 也带 error
        err = data.get("error")
        err = err if isinstance(err, dict) else {}
        raise LLMBusinessError(
            f"{provider}（{model}）[{err.get('code') or 'Error'}] {err.get('message') or ''}"
        )
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"{provider}（{model}）响应缺少 choices 正文")
    first = choices[0] if isinstance(choices[0], dict) else {}
    if first.get("finish_reason") == "length":
        raise ValueError("模型输出被截断（finish_reason=length），需减小批量重试")
    content = (first.get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"{provider}（{model}）响应正文为空")
    return content


def _extract_anthropic_resp(resp, entry: dict) -> str:
    """解析 Anthropic 协议响应（``{base_url}/v1/messages``）→ 助手正文。

    错误体 ``{"type":"error","error":{"type","message"}}``；截断
    ``stop_reason == "max_tokens"`` → ValueError（上层减半重试）。
    """
    model = entry.get("id", "?")
    provider = entry.get("provider", "未知厂商")
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — 非 JSON 响应按原文报错
        data = None
    if resp.status_code != 200:
        err = data.get("error") if isinstance(data, dict) else None
        err = err if isinstance(err, dict) else {}
        code = str(err.get("type") or resp.status_code)
        msg = str(err.get("message") or resp.text[:300] or "无错误详情")
        rid = resp.headers.get("request-id") or resp.headers.get("x-request-id") or ""
        err_cls = RuntimeError if _is_retryable_code(code) else LLMBusinessError
        raise err_cls(
            f"{provider}（{model}）[{code}] {_code_zh(code, resp.status_code)}：{msg}"
            + (f"（request_id={rid}）" if rid else "")
        )
    if not isinstance(data, dict):
        raise LLMBusinessError(f"{provider}（{model}）响应不是 JSON 对象")
    if data.get("type") == "error":  # 个别网关 200 也带 error
        err = data.get("error")
        err = err if isinstance(err, dict) else {}
        raise LLMBusinessError(
            f"{provider}（{model}）[{err.get('type') or 'Error'}] {err.get('message') or ''}"
        )
    content = data.get("content")
    text = ""
    if isinstance(content, list):
        text = "".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    if data.get("stop_reason") == "max_tokens":
        raise ValueError("模型输出被截断（stop_reason=max_tokens），需减小批量重试")
    if not text.strip():
        raise RuntimeError(f"{provider}（{model}）响应正文为空")
    return text


def _chat_completion(entry: dict, messages: list[dict],
                     retries: int = 2, timeout: float = 180.0,
                     max_tokens: int | None = None,
                     temperature: float | None = None) -> str:
    """按注册项协议调用大模型（httpx，gradio 既有依赖）— REQ-20260916-001 双协议。

    entry: llm_config 注册项 {"id","provider","base_url","api_key_env","protocol"}
    - protocol=openai（默认）：POST ``{base_url}/chat/completions`` + Bearer，
      body {model, messages, stream:false}
    - protocol=anthropic：POST ``{base_url}/v1/messages``（base 已带 /v1 则
      ``{base_url}/messages``），headers x-api-key + anthropic-version，
      body {model, max_tokens, system, messages}（system 提到顶层，max_tokens 必填；
      max_tokens 入参可覆盖默认 4096——REQ-20261015-119：Stage 3 硬时长约束
      下 4 组×30-45 行输出会顶爆 4096 截断）

    Key 从 entry.api_key_env 指定的系统环境变量读取（界面不存储 Key）。
    瞬时失败退避重试；确定性业务错误 → LLMBusinessError 直接上抛；截断 →
    ValueError（上层减半）。
    """
    import httpx

    env = entry.get("api_key_env", "")
    key = os.environ.get(env, "")
    if not key:
        raise RuntimeError(f"未配置环境变量 {env}（{entry.get('provider', '未知厂商')} 的 API Key）")

    protocol = str(entry.get("protocol") or "openai").strip().lower()
    base = str(entry.get("base_url", "")).rstrip("/")
    if protocol == "anthropic":
        if base.endswith("/v1/messages"):
            url = base
        elif base.endswith("/v1"):
            url = base + "/messages"
        else:
            url = base + "/v1/messages"
        system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        body = {
            "model": entry.get("id", ""),
            "max_tokens": int(max_tokens) if max_tokens else 4096,  # Anthropic 必填；默认 20 段/批输出约 2K tokens
            "messages": [m for m in messages if m.get("role") != "system"],
        }
        if system:
            body["system"] = system
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        extract = _extract_anthropic_resp
    else:
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        body = {"model": entry.get("id", ""), "messages": messages, "stream": False}
        headers = {"Authorization": f"Bearer {key}"}
        extract = _extract_openai_resp

    if temperature is not None:
        # REQ-20261019-228：字幕断句整理要稳定复现，显式压温度；
        # None（默认）不传 → 其他调用方行为不变
        body["temperature"] = float(temperature)

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
            return extract(resp, entry)
        except ValueError:
            raise  # 截断等输出质量问题：同批重试无意义，直接交上层减半
        except LLMBusinessError:
            raise  # 确定性业务失败（欠费/Key 无效等）：重试同样失败，直接上抛
        except Exception as e:  # noqa: BLE001 — httpx 异常类型不稳定
            last_err = e
            if attempt < retries:
                wait = 2.0 * (attempt + 1)
                log.warning("[revise] LLM 调用失败（第 %d 次，%.0fs 后重试）: %s", attempt + 1, wait, e)
                time.sleep(wait)
    raise RuntimeError(f"网络/服务端错误（重试 {retries} 次后仍失败）: {last_err}")


def _call_llm(system: str, user: str, entry: dict | None = None, retries: int = 2,
              max_tokens: int | None = None,
              temperature: float | None = None) -> str:
    """调用注册的大模型 → 文本响应。瞬时失败退避重试（2s/4s）。

    entry: llm_config 注册项（app.py 传入用户选择的当前模型，REQ-20260915-008）；
    None 时回退默认注册项 + SLIRN_LLM_MODEL 环境变量覆盖模型 id（向后兼容）。
    max_tokens: anthropic 协议输出上限（None → 4096 默认）。
    temperature: None 不传（各端默认）；需要稳定复现的整理类任务可显式压低。
    """
    if entry is None:
        from slirn_home.llm_config import DEFAULT_MODELS

        entry = dict(DEFAULT_MODELS[0])
        entry["id"] = os.environ.get("SLIRN_LLM_MODEL", "").strip() or entry["id"]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        return _chat_completion(entry, messages, retries=retries,
                                max_tokens=max_tokens, temperature=temperature)
    except ValueError:
        raise  # 截断：上层减半重试
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"大模型调用失败[{entry.get('id', '?')}]（{entry.get('provider', '')}）: {e}"
        ) from e


def _is_vision_entry(entry: dict) -> bool:
    """判断模型注册项是否支持多模态（图片输入）。

    判定优先级（REQ-20260919-061 用户补充）：
    1. 用户在 ⚙️ 显式声明的 `vision` 字段（True/False）— 优先尊重，避免模型名误导
    2. 缺省时回退启发式（按模型名关键字）— 兼容旧条目
    """
    # 1. 用户显式声明（缺省 None 不算 False，启发式接管）
    v = entry.get("vision")
    if isinstance(v, bool):
        return v
    # 2. 启发式兜底（覆盖主流厂商）
    mid = str(entry.get("id") or "").strip().lower()
    if not mid:
        return False
    if mid.startswith("qwen-vl") or mid.startswith("qvq") or "qwen2-vl" in mid or "qwen2.5-vl" in mid:
        return True
    if mid.startswith("gpt-4o") or "gpt-4-vision" in mid:
        return True
    if mid.startswith("claude-3") or mid.startswith("claude-4"):
        return True
    if any(k in mid for k in ("vl", "vision", "4o", "opus", "sonnet")):
        return True
    return False


def _call_llm_vision(
    system: str,
    user_text: str,
    image_paths: list[str | Path],
    entry: dict | None = None,
    retries: int = 2,
    timeout: float = 180.0,
) -> str:
    """REQ-20260919-061 Phase C：调多模态模型（图片+文本）→ 文本响应。

    双协议（REQ-20260919-061 用户补充 — MiniMax / Anthropic 兼容网关支持 vision）：
    - openai：user.content 是 list，含 text + image_url（data URI，base64 编码）
    - anthropic：messages[0].content 是 list，含 text + image（base64 source）
    """
    import base64
    import httpx

    if entry is None:
        from slirn_home.llm_config import DEFAULT_MODELS

        entry = dict(DEFAULT_MODELS[0])
    protocol = str(entry.get("protocol") or "openai").strip().lower()

    # 1. 读取图片 → base64
    images: list[tuple[str, str]] = []  # (media_type, base64_data)
    for p in image_paths:
        path = Path(p)
        if not path.exists():
            raise RuntimeError(f"图片不存在: {path}")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise RuntimeError(f"图片过大（>10MB）: {path}")
        ext = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                "webp": "webp", "gif": "gif", "bmp": "bmp"}.get(ext, "jpeg")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        images.append((f"image/{mime}", b64))

    # 2. 按协议构造 messages / body / headers
    env = entry.get("api_key_env", "")
    key = os.environ.get(env, "")
    if not key:
        raise RuntimeError(f"未配置环境变量 {env}（{entry.get('provider', '未知厂商')} 的 API Key）")

    if protocol == "anthropic":
        # Anthropic 协议 — content parts: text + image(base64 source)
        content_parts: list[dict] = [{"type": "text", "text": user_text}]
        for media_type, b64 in images:
            content_parts.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
        messages = [{"role": "user", "content": content_parts}]
        base = str(entry.get("base_url", "")).rstrip("/")
        if base.endswith("/v1/messages"):
            url = base
        elif base.endswith("/v1"):
            url = base + "/messages"
        else:
            url = base + "/v1/messages"
        body = {
            "model": entry.get("id", ""),
            "max_tokens": 4096,
            "messages": messages,
        }
        if system:
            body["system"] = system
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        extract = _extract_anthropic_resp
    else:
        # OpenAI 兼容协议（默认）— content parts: text + image_url
        content_parts = [{"type": "text", "text": user_text}]
        for media_type, b64 in images:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64}"},
            })
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content_parts},
        ]
        base = str(entry.get("base_url", "")).rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        body = {"model": entry.get("id", ""), "messages": messages, "stream": False}
        headers = {"Authorization": f"Bearer {key}"}
        extract = _extract_openai_resp

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
            return extract(resp, entry)
        except ValueError:
            raise  # 截断等输出质量问题
        except LLMBusinessError:
            raise  # 确定性业务失败
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                wait = 2.0 * (attempt + 1)
                log.warning("[vision] LLM 调用失败（第 %d 次，%.0fs 后重试）: %s", attempt + 1, wait, e)
                time.sleep(wait)
    raise RuntimeError(f"网络/服务端错误（重试 {retries} 次后仍失败）: {last_err}")


_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```")


def parse_llm_suggestions(raw: str, segments: list[dict]) -> list[dict]:
    """防御式解析模型输出 → 与段一一对应的建议列表。

    - 剥 ```json 围栏；截取首个 '[' 到最后一个 ']'（容忍前后废话）
    - 未知类别 → review；缺 i / i 不在段内 → 丢弃
    - 模型漏答的段 → 回填 review（保证每段必有建议 — REQ-5.2）
    """
    by_id = {int(s["i"]): s for s in segments}
    parsed: dict[int, dict] = {}
    cleaned = _FENCE_RE.sub("", raw or "").strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]
    try:
        arr = json.loads(cleaned)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"模型输出不是合法 JSON: {e}") from e
    if not isinstance(arr, list):
        raise ValueError("模型输出不是 JSON 数组")
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if i not in by_id:
            continue
        cat = str(item.get("category") or "").strip().lower()
        if cat not in LLM_CATEGORIES:
            cat = "review"
        note = str(item.get("note") or "").strip()
        if not note:
            note = "模型未给出说明"
        keep_text = item.get("keep_text") if cat in ("split", "fix") else None
        if not (isinstance(keep_text, str) and keep_text.strip()):
            if cat == "fix":
                # 更正建议必须有更正文本，否则无从执行 → 降级人工复核
                # （note 前加提示，保留模型发现的问题供人工修改 — REQ-20260916-006）
                cat = "review"
                note = f"模型发现文字错误但未给出更正文本，请人工修改：{note}"
            keep_text = None
        parsed[i] = {"category": cat, "keep_text": keep_text, "note": note[:500]}
    # 漏答回填
    out: list[dict] = []
    for s in segments:
        i = int(s["i"])
        sug = parsed.get(i) or {
            "category": "review", "keep_text": None, "note": "模型未返回该段，请人工复核",
        }
        out.append({"i": i, **sug})
    return out


# =============== 后台 job 管理（与 asr_service 同模式） ===============

# task_id → {"state": "running|done|error", "stage": str, "error": str|None,
#            "started_at": float, "finished_at": float|None, "entries_count": int}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def job_status(task_id: str) -> dict | None:
    with _JOBS_LOCK:
        j = _JOBS.get(task_id)
        return dict(j) if j else None


def start_job(
    task_id: str,
    segments: list[dict],
    task_name: str,
    hotwords: list[str],
    outputs_dir: Path,
    on_success: Callable[[list[dict]], None] | None = None,
    entry: dict | None = None,
    rigor: str = "high",
    custom_prompt: str | None = None,
) -> bool:
    """启动大模型分析线程。已在跑 → False。成功后写 revision.json（决策重置 pending，
    split 行的「切分修剪后内容」自动预填建议文本 — REQ-20260916-010）。

    entry：本次分析使用的模型注册项 {"id","provider","base_url","api_key_env"}
    （app.py 传入用户选择的当前模型，REQ-20260915-008），全程使用并写入
    revision.json meta 留痕。
    rigor：分析严谨性级别 high|medium|low|custom（REQ-20260916-003 / 007），
    非法值直接拒绝；级别写入 meta 留痕并注入系统提示词。
    custom_prompt：rigor=custom 时的用户自定义提示词（空白回退默认底稿），
    实际使用的提示词全文写入 meta.custom_prompt 留痕（REQ-20260916-007）。
    """
    system_prompt = resolve_system_prompt(rigor, custom_prompt)
    with _JOBS_LOCK:
        existing = _JOBS.get(task_id)
        if existing and existing.get("state") == "running":
            return False
        _JOBS[task_id] = {
            "state": "running", "stage": "准备提示词", "error": None,
            "started_at": time.time(), "finished_at": None, "entries_count": 0,
        }

    def _run():
        job = _JOBS[task_id]

        def _stage(name: str) -> None:
            job["stage"] = name
            log.info("[revise][%s] %s", task_id, name)

        def _analyze(batch: list[dict]) -> list[dict]:
            """分析一批：解析失败/截断 → 减半重试；单段仍失败 → 回填 review。

            确定性失败（欠费/Key 无效等 RuntimeError）向上抛 → job 终止并透传原因
            —— 这种错误每批都会失败，减半只是浪费调用（REQ-20260915-007）。
            """
            try:
                raw = _call_llm(system_prompt, build_user_prompt(task_name, hotwords, batch), entry)
                return parse_llm_suggestions(raw, batch)
            except ValueError as e:
                if len(batch) == 1:
                    log.warning("[revise][%s] 段 %s 分析失败（%s），回填人工复核", task_id, batch[0].get("i"), e)
                    return [{
                        "i": int(batch[0]["i"]), "category": "review", "keep_text": None,
                        "note": f"模型分析失败（{e}），请人工复核",
                    }]
                mid = len(batch) // 2
                log.warning("[revise][%s] 批解析失败，减半重试（%d 段）: %s", task_id, len(batch), e)
                return _analyze(batch[:mid]) + _analyze(batch[mid:])

        try:
            batches = [segments[k:k + BATCH_SIZE] for k in range(0, len(segments), BATCH_SIZE)] or [[]]
            suggestions: list[dict] = []
            for bi, batch in enumerate(batches):
                _stage(f"调用大模型 ({bi + 1}/{len(batches)})")
                suggestions.extend(_analyze(batch))
                if bi < len(batches) - 1:
                    time.sleep(0.6)  # 批间间隔，降低限流概率

            _stage("保存结果")
            entries = []
            for s, sug in zip(segments, suggestions):
                entries.append({
                    "i": int(s["i"]),
                    "start_ms": int(s.get("start_ms", 0)), "end_ms": int(s.get("end_ms", 0)),
                    "start": s.get("start", ""), "end": s.get("end", ""),
                    "text": s.get("text", ""),
                    "category": sug["category"], "keep_text": sug["keep_text"], "note": sug["note"],
                    "decision": "pending",
                    # split/fix 行自动预填建议文本到行内输入框（REQ-20260916-010 /
                    # 20260917-026）：split=切分修剪后内容、fix=更正后内容，用户在
                    # 此基础上微调即可；清空则切分清单回退 keep_text，语义自洽
                    "user_note": (sug["keep_text"] or "") if sug["category"] in ("split", "fix") else "",
                })
            meta = {
                "version": 1,
                "model": (entry or {}).get("id") or "qwen-plus",
                "provider": (entry or {}).get("provider", ""),
                "protocol": (entry or {}).get("protocol", "openai"),
                "rigor": rigor,
                "custom_prompt": system_prompt if rigor == CUSTOM_RIGOR_KEY else None,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "saved_at": None,
                "segments_count": len(entries),
                "entries": entries,
            }
            outputs_dir.mkdir(parents=True, exist_ok=True)
            (outputs_dir / REVISION_JSON).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            job["entries_count"] = len(entries)
            job["state"] = "done"
            job["stage"] = "完成"
            job["finished_at"] = time.time()
            log.info("[revise][%s] 完成：%d 条建议", task_id, len(entries))
            if on_success:
                try:
                    on_success(entries)
                except Exception as e:  # noqa: BLE001
                    log.warning("[revise][%s] on_success 回调失败: %s", task_id, e)
        except Exception as e:  # noqa: BLE001 — 后台线程必须全兜底
            log.exception("[revise][%s] 分析失败", task_id)
            job["state"] = "error"
            job["error"] = str(e)
            job["finished_at"] = time.time()

    threading.Thread(target=_run, name=f"revise-{task_id}", daemon=True).start()
    return True


# =============== 读取 / 决策合并 ===============

def load_revision(outputs_dir: Path) -> dict | None:
    """读取 revision.json（无/损坏 → None）。"""
    p = Path(outputs_dir) / REVISION_JSON
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("revision.json 损坏: %s", e)
        return None


def merge_decisions(revision: dict, decisions: list[dict]) -> tuple[dict, int]:
    """把用户决策合并进 revision（不落盘）。返回 (新 meta, 生效条数)。

    decisions: [{"i": int, "decision": str, "user_note": str}]；
    非法 decision / 未知 i 的条目跳过。
    """
    by_id = {int(e["i"]): e for e in revision.get("entries", [])}
    applied = 0
    for d in decisions or []:
        try:
            i = int(d.get("i"))
        except (TypeError, ValueError):
            continue
        entry = by_id.get(i)
        if entry is None:
            continue
        dec = str(d.get("decision") or "").strip().lower()
        if dec not in USER_DECISIONS:
            continue
        entry["decision"] = dec
        entry["user_note"] = str(d.get("user_note") or "").strip()[:500]
        applied += 1
    return revision, applied


def all_decided(revision: dict) -> bool:
    """全部条目 decision != pending（修订完成的判定 — REQ-5.4）。"""
    entries = revision.get("entries") or []
    return bool(entries) and all(e.get("decision") != "pending" for e in entries)
