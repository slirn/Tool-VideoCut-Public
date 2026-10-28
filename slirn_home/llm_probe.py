# -*- coding: utf-8 -*-
"""并发能力探测 — 用最小代价摸清当前大模型入口能吃几路并发。

方法（阶梯探测）：按 2 → 4 → 8 阶梯，每级同时发 k 个**最小补全请求**
（「请只回复 ok」，retries=0），看这一级有没有被限流/失败：

- 全部成功 → 下一级；
- 出现限流（429/Throttling/rate_limit/overloaded）或失败 → 上一级全成功
  的 k 就是建议值；2 都不行 → 建议 1（并在 note 里说明原因）。

注意：探测基于「瞬时并发」表现，厂商限流多是 RPM/并发数混合，建议值是
经验参考而非硬边界 — 撞限流时调用侧会退避重试，不会出错。每次探测
消耗约 20 个微小请求（几百 tokens），记账 stage=并发探测。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

LEVELS = (2, 4, 8)
_PROBE_SYSTEM = "你是连通性测试助手。"
_PROBE_USER = "请只回复两个字符：ok"

_THROTTLE_KEYS = ("throttl", "rate_limit", "ratelimit", "429", "overloaded", "too many requests")


class _Result:
    __slots__ = ("ok", "throttled", "err", "ms")

    def __init__(self):
        self.ok = False
        self.throttled = False
        self.err = ""
        self.ms = 0


def _probe_one(entry: dict) -> _Result:
    """发一个最小请求并归类结果（throttled 与其他失败分开 — 决定建议值语义）。"""
    from slirn_home import revision_service
    r = _Result()
    t0 = time.time()
    try:
        revision_service._chat_completion(
            entry,
            [{"role": "system", "content": _PROBE_SYSTEM},
             {"role": "user", "content": _PROBE_USER}],
            retries=0, timeout=30.0,
            usage_ctx={"stage": "并发探测"},
        )
        r.ok = True
    except Exception as e:  # noqa: BLE001 — 分类后回传，不中断探测
        msg = str(e).lower()
        r.throttled = any(k in msg for k in _THROTTLE_KEYS)
        r.err = str(e)[:200]
    finally:
        r.ms = int((time.time() - t0) * 1000)
    return r


def probe_concurrency(entry: dict, levels: tuple[int, ...] = LEVELS) -> dict:
    """阶梯探测当前模型入口的可用并发数。返回 {suggested, levels, note}。"""
    suggested = 1
    detail: list[dict] = []
    lock = threading.Lock()

    for k in levels:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=k, thread_name_prefix="llm-probe") as pool:
            rs = list(pool.map(lambda _: _probe_one(entry), range(k)))
        ok = sum(1 for r in rs if r.ok)
        throttled = sum(1 for r in rs if r.throttled)
        failed = k - ok - throttled
        level = {
            "k": k, "ok": ok, "throttled": throttled, "failed": failed,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "sample_err": next((r.err for r in rs if not r.ok), ""),
        }
        with lock:
            detail.append(level)
        log.info("[probe] 并发 %d：成功 %d 限流 %d 失败 %d", k, ok, throttled, failed)
        if ok == k:
            suggested = k
            continue
        # 这一级没全过：非限流失败（网络/配置）说明探测环境有问题，直接收手
        if throttled == 0 and failed > 0:
            note = f"并发 {k} 时出现非限流失败（{level['sample_err'][:80]}），建议先检查模型连通性"
            return {"suggested": suggested, "levels": detail, "note": note}
        return {
            "suggested": suggested, "levels": detail,
            "note": f"并发 {k} 撞到限流（成功 {ok}/{k}），建议 {suggested} 路",
        }

    return {
        "suggested": suggested,
        "levels": detail,
        "note": f"并发 {levels[-1]} 全部成功，已达探测上限（可放心用 {suggested} 路）",
    }


# ---------- 上下文窗口探测（决定 pack_batches 每批装多少行） ----------
#
# 三层策略，代价从零到小：
# ① 模型元数据接口（免费）：不少网关在 GET /models/{id} 里带 context_length /
#    max_model_len 等字段（DeepSeek/SiliconFlow/vLLM 常见；OpenAI/Anthropic 官方不带）；
# ② 超长拒答解析（免费）：发一个 ~128K tokens 的 max_tokens=1 请求，绝大多数
#    模型会在计费前拒绝并报明上限（OpenAI 原文 "maximum context length is 131072"）；
#    若被接受（模型上下文 ≥128K，如 Gemini），按 128K 计 — 一次计费请求；
# ③ 阶梯实测（首个成功计费一次，被拒不计费）：64K → 32K → 16K → 8K 逐级
#    下降，第一级成功的即为窗口值（下限）。
# 结果写入 settings context_cache，revision_service.pack_batches 立即采用。

_OVERSIZE = 131072          # ② 的首发目标（覆盖绝大多数在售模型）
_DESCENT = (65536, 32768, 16384, 8192)
_CTX_META_KEYS = ("context_length", "max_context_length", "context_window",
                  "max_model_len", "max_input_tokens", "context_size")


def _parse_ctx_from_error(msg: str) -> int | None:
    """从拒答报文里抠上下文上限。各家文案不一，三组模式由具体到宽泛；
    「N tokens … context」（数字在 context 前）多是请求体大小而非上限，
    不抓 — 解析不出来还有阶梯实测兜底。"""
    import re

    m = re.search(r"maximum context length\D{0,20}(\d{4,9})", msg, re.I)
    if not m:
        m = re.search(r"context\D{0,60}?(\d{4,9})\s*tokens", msg, re.I)
    if not m:
        m = re.search(r"context\D{0,60}?(\d{4,9})(?!\d)", msg, re.I)
    return int(m.group(1)) if m else None


def _filler(n_tokens: int) -> str:
    """生成 ≈n_tokens 的填充文本。数字+空格按 3.2 字符/token 保守放大
    （实际 ~3-4），确保目标规模**至少**到位 — 宁可高估被拒，不可低估白测。"""
    n_chars = int(n_tokens * 3.2)
    unit = "0123456789 "
    return (unit * (n_chars // len(unit) + 1))[:n_chars]


def _meta_lookup(entry: dict) -> int | None:
    """① 查模型元数据接口（带上下文字段的网关才有结果）。"""
    import os

    import httpx

    key = os.environ.get(entry.get("api_key_env", ""), "")
    if not key:
        return None
    base = str(entry.get("base_url", "")).rstrip("/")
    if str(entry.get("protocol") or "openai").lower() == "anthropic":
        url = (base if base.endswith("/v1/models") else
               base + "/models" if base.endswith("/v1") else base + "/v1/models")
        url += "/" + str(entry.get("id", ""))
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        url = (base if base.endswith("/models") else base + "/models")
        url += "/" + str(entry.get("id", ""))
        headers = {"Authorization": f"Bearer {key}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=15.0)
        data = resp.json()
    except Exception:  # noqa: BLE001 — 元数据查不到就走实测，不是错误
        return None
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]  # 个别网关包一层 data
    if not isinstance(data, dict):
        return None
    vals = [int(data[k]) for k in _CTX_META_KEYS
            if isinstance(data.get(k), int) and data[k] > 0]
    return max(vals) if vals else None


def _ctx_attempt(entry: dict, n_tokens: int) -> tuple[bool, str]:
    """发一个超长 max_tokens=1 请求 → (输入是否被接受, 错误原文)。

    max_tokens=1 的响应必然 stop_reason=max_tokens（正常截断）— 那是
    ValueError「截断」，但恰说明**输入被完整接受**（上下文装得下），按成功
    处理；只有 4xx/网络类异常才算被拒。
    """
    from slirn_home import revision_service as rs

    try:
        rs._chat_completion(
            entry,
            [{"role": "system", "content": "你是上下文测试助手。"},
             {"role": "user", "content": "以下是填充文本，请忽略，只回复 ok：" + _filler(n_tokens)}],
            retries=0, timeout=120.0, max_tokens=1,
            usage_ctx={"stage": "上下文探测"},
        )
        return True, ""
    except ValueError:
        return True, ""  # 输出按预算截断 = 输入已被接受
    except Exception as e:  # noqa: BLE001 — 被拒是预期路径
        return False, str(e)


def probe_context(entry: dict, repo_root=None) -> dict:
    """探测当前模型的上下文窗口，返回 {context_tokens, source, suggested_batch,
    probe_cost_tokens, note}，并写入 settings context_cache。"""
    from slirn_home import revision_service as rs
    from slirn_home import settings_service

    def _done(tokens: int, source: str, note: str, cost: int = 0) -> dict:
        settings_service.set_context(repo_root, str(entry.get("id") or ""), tokens)
        return {
            "context_tokens": tokens, "source": source,
            "suggested_batch": rs.suggest_batch(tokens),
            "probe_cost_tokens": cost,
            "note": note + " — 结果已缓存，分批打包立即按此生效",
        }

    # ① 元数据（免费）
    meta = _meta_lookup(entry)
    if meta:
        return _done(meta, "模型元数据接口", f"GET /models 报文带上下文字段（{meta} tokens）")

    # ② 超长首发：被拒的报文常写明上限；被接受说明窗口 ≥128K
    ok, err = _ctx_attempt(entry, _OVERSIZE)
    if ok:
        return _done(_OVERSIZE, f"实测（≥{_OVERSIZE // 1024}K）",
                     f"超长请求被接受，窗口不小于 {_OVERSIZE // 1024}K tokens", _OVERSIZE)
    n = _parse_ctx_from_error(err)
    if n:
        return _done(n, "拒答报文解析", f"网关拒绝超长请求并报明上限：{err[:120]}")

    # ③ 阶梯下降：第一级成功的即窗口值（下限）；被拒不计费
    for size in _DESCENT:
        ok, err = _ctx_attempt(entry, size)
        if ok:
            return _done(size, f"实测（≥{size // 1024}K）",
                         f"{_OVERSIZE // 1024}K 被拒后阶梯下降，{size // 1024}K 成功", size)

    return {
        "context_tokens": rs.DEFAULT_CTX_TOKENS,
        "source": "未知",
        "suggested_batch": rs.suggest_batch(rs.DEFAULT_CTX_TOKENS),
        "probe_cost_tokens": 0,
        "note": f"各级请求均被拒（{err[:80]}），未能确定窗口 — 暂按默认 "
                f"{rs.DEFAULT_CTX_TOKENS} 保守分批，请检查模型连通性后重试",
    }
