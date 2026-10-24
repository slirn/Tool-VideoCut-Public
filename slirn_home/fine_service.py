"""精剪修订服务（热词替换）— REQ-20260916-017。

把切分修剪执行口径下的保留行（effective_keep_units 的行文本）交给大模型
（用户在 ⚙️ 注册的当前模型，双协议见 revision_service._chat_completion），
按**任务热词**找出字幕中的误识别文字并替换：ASR 常把专有名词听写成
同音/近音错字（「神精网络」→「神经网络」），本阶段只做这一件事。

产物 outputs/fine_revision.json：只含有替换的行（entries，含替换对与撤销
标记）+ 全局统计。用户逐处确认（撤销误替换）后保存 → 阶段完成。

设计要点：
- 替换是**局部片段级**（before 必须逐字摘自行文本、after 必须包含热词表
  中的词），模型整行改写/编造热词一律防御丢弃 — 宁缺勿错
- 行集口径与切分面板/粗剪合成一致（切分之后的字幕 = 执行口径保留行）
- 后台线程 job 管理与 asr/revision/compose 同模式（内存 job 表 + 阶段级进度）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

FINE_JSON = "fine_revision.json"

# 分批行数：热词替换输出很小（只有需要替换的行），40 行/批安全
BATCH_SIZE = 40

FINE_SYSTEM = (
    "你是字幕热词校对助手。用户给你视频字幕行列表（JSON 数组，每项含行 id 与文本）"
    "和一份热词表（专有名词/人名/术语的正确写法）。\n"
    "语音识别常把热词听写成错误文字（同音/近音误识别，"
    "如「神精网络」应为「神经网络」、「爱」应为「AI」）。请逐行检查："
    "哪些行里的文字应当替换为热词表中的某个词。\n\n"
    "判定标准：\n"
    "- 只处理与热词直接相关的误识别 — 行内存在热词的错误写法\n"
    "- 替换必须只改错误部分：不得改写整行、不得增删其他内容、不得调整语序\n"
    "- 行内已正确出现热词 → 无需返回该行\n"
    "- 拿不准（可能是说话人口误而非识别错误、语义不明）→ 不返回，宁缺勿错\n\n"
    "输出要求：只输出 JSON 数组（没有需要替换的行时输出 []），不要任何其他文字。"
    "仅包含需要替换的行，每项格式：\n"
    '{"id": "行id", "replacements": [{"before": "行内原文片段（必须逐字摘自行文本）", '
    '"after": "替换后的文字", "hotword": "热词表中对应的词"}]}\n'
    "before 必须逐字等于行文本中的一段连续原文；after = before 的修正（包含对应热词）；"
    "同一行的多个替换互不重叠。"
)


def build_user_prompt(task_name: str, hotwords: list[str], lines: list[dict]) -> str:
    """构造 user 消息：任务上下文 + 热词表 + 行列表（id + 文本）。"""
    parts = [f"视频任务：{task_name or '（未命名）'}", f"热词表：{'、'.join(hotwords)}"]
    parts.append("字幕行列表（切分修剪后保留的行）：")
    parts.append(json.dumps(
        [{"id": str(ln["id"]), "text": str(ln.get("text", ""))} for ln in lines],
        ensure_ascii=False,
    ))
    return "\n".join(parts)


# =============== 解析与校验（纯函数，便于测试） ===============

def _normalize_rep(rep: dict, text: str, hotwords: list[str]) -> dict | None:
    """校验并归一单个替换对。不合法 → None（丢弃，宁缺勿错）。

    - before 非空且是 text 的连续子串（模型摘抄必须逐字）
    - after 非空且 != before
    - hotword 归一：不在热词表 → 从 after 中包含的热词回填；仍无 → 丢弃
      （替换结果必须落到热词表上，防止模型自由发挥改写）
    """
    if not isinstance(rep, dict):
        return None
    before = str(rep.get("before") or "")
    after = str(rep.get("after") or "")
    if not before or not after or before == after:
        return None
    if before not in text:
        return None
    hotword = str(rep.get("hotword") or "")
    if hotword not in hotwords:
        hotword = next((w for w in hotwords if w in after), "")
        if not hotword:
            return None
    if hotword not in after:
        return None
    return {"before": before, "after": after, "hotword": hotword}


def apply_replacements(text: str, reps: list[dict]) -> tuple[str, list[dict]]:
    """按首次出现位置应用替换对（重叠跳过）。返回 (新文本, 实际生效的替换)。

    生效替换附 pos（before 在原文中的起始字符位），供前端精确高亮。
    """
    spans: list[tuple[int, int]] = []
    applied: list[dict] = []
    out = []
    cur = 0
    for rep in sorted(reps, key=lambda r: text.find(r["before"])):
        # 找 before 第一个不与已占用区间重叠的出现位置（可能在行内多处出现）
        pos = text.find(rep["before"])
        while pos >= 0:
            if all(pos >= e or pos + len(rep["before"]) <= s for s, e in spans):
                break
            pos = text.find(rep["before"], pos + 1)
        if pos < 0:
            log.warning("[fine] 替换片段重叠/缺失，跳过: %r", rep["before"])
            continue
        out.append(text[cur:pos])
        out.append(rep["after"])
        spans.append((pos, pos + len(rep["before"])))
        applied.append({**rep, "pos": pos})
        cur = pos + len(rep["before"])
    out.append(text[cur:])
    return "".join(out), applied


def parse_replacements(raw: str, lines: list[dict], hotwords: list[str]) -> dict[str, list[dict]]:
    """防御式解析模型输出 → {行 id: 生效替换列表}（只含有替换的行）。

    剥 ```json 围栏、截取首个 '[' 到最后一个 ']'；未知 id 丢弃；
    替换对过 _normalize_rep + apply_replacements（重叠剔除）。
    """
    import re

    by_id = {str(ln["id"]): str(ln.get("text", "")) for ln in lines}
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw or "").strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start >= 0 and end > start:
        cleaned = cleaned[start:end + 1]
    try:
        arr = json.loads(cleaned)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"模型输出不是合法 JSON: {e}") from e
    if not isinstance(arr, list):
        raise ValueError("模型输出不是 JSON 数组")
    out: dict[str, list[dict]] = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or "")
        if rid not in by_id:
            continue
        reps_in = item.get("replacements")
        if not isinstance(reps_in, list):
            continue
        reps = [r for r in (_normalize_rep(r, by_id[rid], hotwords) for r in reps_in) if r]
        if not reps:
            continue
        _, applied = apply_replacements(by_id[rid], reps)
        if applied:
            out[rid] = applied
    return out


# =============== 后台 job 管理（与 revision_service 同模式） ===============

# task_id → {"state": "running|done|error", "stage": str, "progress": 0-100,
#            "error": str|None, "started_at": float, "finished_at": float|None,
#            "replaced_lines": int, "replacements": int}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def job_status(task_id: str) -> dict | None:
    with _JOBS_LOCK:
        j = _JOBS.get(task_id)
        return dict(j) if j else None


def start_job(
    task_id: str,
    lines: list[dict],
    task_name: str,
    hotwords: list[str],
    outputs_dir: Path,
    entry: dict | None = None,
    cutlist_saved_at: str = "",
    on_success: Callable[[dict], None] | None = None,
) -> bool:
    """启动热词替换分析线程。已在跑 → False。成功后写 fine_revision.json。

    lines：切分执行口径的保留行 [{"id","start_ms","end_ms","text"}…]；
    entry：本次使用的模型注册项（写入 meta 留痕）；
    cutlist_saved_at：口径快照时间（过期判定用）。
    """
    with _JOBS_LOCK:
        existing = _JOBS.get(task_id)
        if existing and existing.get("state") == "running":
            return False
        _JOBS[task_id] = {
            "state": "running", "stage": "准备提示词", "progress": 0.0, "error": None,
            "started_at": time.time(), "finished_at": None,
            "replaced_lines": 0, "replacements": 0,
        }

    def _run():
        job = _JOBS[task_id]

        def _stage(name: str, pct: float) -> None:
            job["stage"] = name
            job["progress"] = round(pct, 1)
            log.info("[fine][%s] %s", task_id, name)

        def _analyze(batch: list[dict]) -> dict[str, list[dict]]:
            """分析一批：解析失败/截断 → 减半重试；单行仍失败 → 放弃该批替换
            （本阶段是锦上添花的校对，不阻塞流程，宁缺勿错）。"""
            from slirn_home.revision_service import _call_llm

            sub_prompt = build_user_prompt(task_name, hotwords, batch)
            try:
                raw = _call_llm(FINE_SYSTEM, sub_prompt, entry)
                return parse_replacements(raw, batch, hotwords)
            except ValueError as e:
                if len(batch) == 1:
                    log.warning("[fine][%s] 行 %s 分析失败（%s），跳过", task_id, batch[0].get("id"), e)
                    return {}
                mid = len(batch) // 2
                log.warning("[fine][%s] 批解析失败，减半重试（%d 行）: %s", task_id, len(batch), e)
                return {**_analyze(batch[:mid]), **_analyze(batch[mid:])}

        try:
            batches = [lines[k:k + BATCH_SIZE] for k in range(0, len(lines), BATCH_SIZE)] or [[]]
            replaced: dict[str, list[dict]] = {}
            for bi, batch in enumerate(batches):
                _stage(f"调用大模型 ({bi + 1}/{len(batches)})", bi / max(1, len(batches)) * 100)
                replaced.update(_analyze(batch))
                if bi < len(batches) - 1:
                    time.sleep(0.6)  # 批间间隔，降低限流概率

            _stage("保存结果", 99.0)
            entries = []
            for ln in lines:
                reps = replaced.get(str(ln["id"]))
                if not reps:
                    continue
                new_text, _ = apply_replacements(str(ln.get("text", "")), reps)
                entries.append({
                    "id": str(ln["id"]),
                    "start_ms": int(ln.get("start_ms", 0)), "end_ms": int(ln.get("end_ms", 0)),
                    "text": str(ln.get("text", "")), "new_text": new_text,
                    "replacements": reps, "reverted": False,
                })
            fine = {
                "version": 1,
                "model": (entry or {}).get("id") or "qwen-plus",
                "provider": (entry or {}).get("provider", ""),
                "protocol": (entry or {}).get("protocol", "openai"),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "saved_at": None,
                "hotwords": list(hotwords),
                "cutlist_saved_at": cutlist_saved_at or "",
                "lines_count": len(lines),
                "stats": {
                    "replaced_lines": len(entries),
                    "replacements": sum(len(e["replacements"]) for e in entries),
                },
                "entries": entries,
            }
            out_dir = Path(outputs_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / FINE_JSON).write_text(
                json.dumps(fine, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            job["replaced_lines"] = len(entries)
            job["replacements"] = fine["stats"]["replacements"]
            job["state"] = "done"
            job["stage"] = "完成"
            job["progress"] = 100.0
            job["finished_at"] = time.time()
            log.info("[fine][%s] 完成：%d 行 / %d 处替换",
                     task_id, len(entries), fine["stats"]["replacements"])
            if on_success:
                try:
                    on_success(fine)
                except Exception as e:  # noqa: BLE001
                    log.warning("[fine][%s] on_success 回调失败: %s", task_id, e)
        except Exception as e:  # noqa: BLE001 — 后台线程必须全兜底
            log.exception("[fine][%s] 热词替换分析失败", task_id)
            job["state"] = "error"
            job["error"] = str(e)
            job["finished_at"] = time.time()

    threading.Thread(target=_run, name=f"fine-revise-{task_id}", daemon=True).start()
    return True


# =============== 读取 / 决策合并 / 统计 ===============

def load_fine(outputs_dir: Path) -> dict | None:
    """读取 fine_revision.json（无/损坏 → None）。"""
    p = Path(outputs_dir) / FINE_JSON
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("fine_revision.json 损坏: %s", e)
        return None


def save_decisions(outputs_dir: Path, reverted_ids: list[str]) -> tuple[dict, int]:
    """把用户撤销决定合并落盘。返回 (fine, 生效条数)。saved_at 置当前时间（阶段完成标记）。"""
    fine = load_fine(outputs_dir)
    if fine is None:
        raise RuntimeError("fine_revision.json 不存在")
    reverted = {str(x) for x in reverted_ids or []}
    changed = 0
    for e in fine.get("entries") or []:
        want = e.get("id") in reverted
        if bool(e.get("reverted")) != want:
            e["reverted"] = want
            changed += 1
    fine["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p = Path(outputs_dir) / FINE_JSON
    p.write_text(json.dumps(fine, ensure_ascii=False, indent=2), encoding="utf-8")
    return fine, changed


def word_stats(fine: dict) -> list[tuple[str, int]]:
    """热词替换频次（撤销的行不计）：[(hotword, count)…] 按次数降序、词升序。"""
    counter: Counter[str] = Counter()
    for e in fine.get("entries") or []:
        if e.get("reverted"):
            continue
        for r in e.get("replacements") or []:
            counter[str(r.get("hotword") or "")] += 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def effective_stats(fine: dict) -> dict:
    """生效（未撤销）的统计：行数 / 替换数 / 撤销数。"""
    entries = fine.get("entries") or []
    live = [e for e in entries if not e.get("reverted")]
    return {
        "replaced_lines": len(live),
        "replacements": sum(len(e.get("replacements") or []) for e in live),
        "reverted": len(entries) - len(live),
    }
