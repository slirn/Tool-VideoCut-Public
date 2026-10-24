"""优化字幕服务（成片重识别 + 不明确字词提取替换）— REQ-20260917-030。

把粗剪合成成片（rough_compose.mp4）+ 任务热词交给 ASR（asr_service 同链路，
sd=False 只识别字幕、不分离说话人），成片时间基的字幕行集再交大模型语义分析：
提取「不明确的字/词」（疑似同音/近音误识别、语义不通、与热词表冲突的片段），
逐处给出替换建议与理由。人工逐处分辨（采纳/不采纳/改替换值）后保存：
替换对应关系 + 替换后行文本（new_text）+ 按目标词聚合统计落盘
optimize_subtitle.json，阶段推进 FINE_SUBTITLE_REVIEWED。

设计要点（DESIGN-20260917-030）：
- 识别复用 asr_service 内部函数（模型单例 + 16k 单声道抽取），绝不写
  subtitle.json/srt —— 那是切分/修订全链路的根基产物
- 替换是局部片段级：before 必须逐字摘自行文本、after ≠ before、重叠剔除
  （复用 fine_service.apply_replacements）—— 模型只提议，人工终审
- 词频按替换目标词（after）分组：同一目标词的多种错误写法计同一词多次出现
- 后台线程 job 与 asr/revision/fine 同模式（内存 job 表 + 阶段级进度）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from slirn_home.fine_service import apply_replacements
from slirn_home import execution_history

log = logging.getLogger(__name__)

OPTIMIZE_JSON = "optimize_subtitle.json"

# 分批行数：与 fine_service 同口径，40 行/批
BATCH_SIZE = 40

OPT_SYSTEM = (
    "你是字幕校对助手。用户给你一段视频重新识别出的字幕行列表（JSON 数组，"
    "每项含行 id 与文本），并附一份热词表（专有名词/人名/术语的正确写法，可能为空）。\n"
    "这段文本由 ASR 对口语录音转写，常混有同音/近音误写。请逐行仔细检查，"
    "提取「不明确的字/词」——疑似语音识别错误的片段。\n\n"
    "重点信号（命中任意一条即标记）：\n"
    "- 同音/近音误写（如「神精网络」应为「神经网络」）\n"
    "- 句中突兀的数字/字母（在口语句里说不通）\n"
    "- 语义不通或与上下文不搭的组合\n"
    "- 与热词表写法不一致的专有名词\n"
    "- 疑似重复识别的叠字（如「视频视频」）\n\n"
    "判定标准：\n"
    "- 只标记局部可疑片段：不得整行改写、不得增删其他内容、不得调整语序\n"
    "- 每处给出最合理的猜测修正（after，只写替换后的文字本身，不要带解释/括号）"
    "和简短理由（reason）\n"
    "- 行内已正确、只是口语化/语气词/书面口语差异 → 不标记\n"
    "- 态度：宁多提议不替人决策——拿不准但值得人工核对的也要标记，最终由人工分辨；"
    "整段录音至少评估出最可疑的几处，确信完全无误才放过\n\n"
    "输出要求：只输出 JSON 数组（没有发现时输出 []），不要任何其他文字。"
    "仅含有发现的行，每项格式：\n"
    '{"id": "行id", "unclear": [{"before": "行内原文片段（必须逐字摘自行文本）", '
    '"after": "建议替换后的文字", "reason": "简短理由"}]}\n'
    "before 必须逐字等于行文本中的一段连续原文；同一行的多个片段互不重叠。"
)

# 首轮零发现时复检用的追加指令 — 口语转写几乎总有可疑处，零发现多半是漏检而非干净
OPT_STRICT_SUFFIX = (
    "\n\n【复检要求】上一轮检查一无所获，但口语 ASR 转写几乎总含可疑片段。"
    "请降低标记门槛重新逐行检查：叠字重复、突兀数字/字母、语义不通、近音误写，"
    "哪怕只有三成把握也要提议（人工最终分辨）。整段至少给出 3 处候选；"
    "若确属完全干净才能输出 []。"
)


def build_user_prompt(task_name: str, hotwords: list[str], lines: list[dict]) -> str:
    """构造 user 消息：任务上下文 + 热词表 + 成片识别行列表（id + 文本）。"""
    parts = [f"视频任务：{task_name or '（未命名）'}"]
    parts.append(f"热词表：{'、'.join(hotwords) if hotwords else '（空）'}")
    parts.append("字幕行列表（粗剪成片重新识别）：")
    parts.append(json.dumps(
        [{"id": str(ln["id"]), "text": str(ln.get("text", ""))} for ln in lines],
        ensure_ascii=False,
    ))
    return "\n".join(parts)


# =============== 解析与校验（纯函数，便于测试） ===============

def _normalize_occ(rep: dict, text: str) -> dict | None:
    """校验并归一单个不明确片段。不合法 → None（丢弃，宁缺勿错）。

    - before 非空且是 text 的连续子串（模型摘抄必须逐字）
    - after 非空且 != before（建议修正不能是原样）
    - reason 可缺省（仅展示用）
    """
    if not isinstance(rep, dict):
        return None
    before = str(rep.get("before") or "")
    after = str(rep.get("after") or "")
    if not before or not after or before == after:
        return None
    if before not in text:
        return None
    return {"before": before, "after": after, "reason": str(rep.get("reason") or "")}


def parse_occurrences(raw: str, lines: list[dict]) -> dict[str, list[dict]]:
    """防御式解析模型输出 → {行 id: 生效出现列表}（只含有发现的行）。

    剥 ```json 围栏、截取首个 '[' 到最后一个 ']'；未知 id 丢弃；
    出现项过 _normalize_occ + apply_replacements（重叠剔除，附 pos）。
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
        occ_in = item.get("unclear")
        if not isinstance(occ_in, list):
            continue
        occs = [o for o in (_normalize_occ(o, by_id[rid]) for o in occ_in) if o]
        if not occs:
            continue
        _, applied = apply_replacements(by_id[rid], occs)
        if applied:
            out[rid] = applied
    return out


# =============== 聚合与统计（纯函数） ===============

def aggregate_words(occurrences: list[dict]) -> list[dict]:
    """按替换目标词（after）聚合：[{word, count}]，次数降序、词升序。

    同一目标词的多种错误写法（「神精网络」「神经网洛」→「神经网络」）
    计为同一词的多次出现 — 回答"这个词出现了几次"。
    """
    counts: dict[str, int] = {}
    for o in occurrences or []:
        if not o.get("applied", True):
            continue
        w = str(o.get("after") or "")
        if w:
            counts[w] = counts.get(w, 0) + 1
    return [{"word": k, "count": counts[k]} for k in sorted(counts, key=lambda x: (-counts[x], x))]


def build_mapping(occurrences: list[dict]) -> list[dict]:
    """替换对应关系汇总：[{after, variants: [{before, count}]}]（只计生效项）。

    与 aggregate_words 同分组口径；variants 展示该目标词下被替换的各种原文写法。
    """
    variants: dict[str, dict[str, int]] = {}
    for o in occurrences or []:
        if not o.get("applied", True):
            continue
        after = str(o.get("after") or "")
        before = str(o.get("before") or "")
        if after and before:
            variants.setdefault(after, {})
            variants[after][before] = variants[after].get(before, 0) + 1
    out = []
    for after in sorted(variants, key=lambda a: (-sum(variants[a].values()), a)):
        out.append({
            "after": after,
            "count": sum(variants[after].values()),
            "variants": [{"before": b, "count": c}
                         for b, c in sorted(variants[after].items(), key=lambda kv: (-kv[1], kv[0]))],
        })
    return out


def apply_to_segments(
    segments: list[dict],
    occurrences: list[dict],
    line_edits: list[dict] | None = None,
) -> list[dict]:
    """把生效出现项应用到行集：每段补 new_text（无生效项的段不加该字段）。

    REQ-20260922-NNN 整句替换：line_edits [{seg, text}] 中的行 **整行改写** —
    new_text 直接用整句文本，跳过该行的局部替换（整句优先于局部）。
    防御（纯函数可单测）：非法 seg / 空白文本 / 与原文相同 → 忽略该条。
    """
    edits_by_seg: dict[str, str] = {}
    for e in line_edits or []:
        if not isinstance(e, dict):
            continue
        try:
            seg_key = str(int(e.get("seg")))
        except (TypeError, ValueError):
            continue
        text = str(e.get("text") or "").strip()
        if text:
            edits_by_seg[seg_key] = text
    by_seg: dict[str, list[dict]] = {}
    for o in occurrences or []:
        if o.get("applied", True):
            by_seg.setdefault(str(o.get("seg")), []).append(o)
    out = []
    for seg in segments:
        s = dict(seg)
        rid = str(s.get("i"))
        edit = edits_by_seg.get(rid)
        if edit is not None and edit != str(s.get("text", "")):
            s["new_text"] = edit  # 整句替换：覆盖本行全部局部替换
            out.append(s)
            continue
        occs = by_seg.get(rid)
        if occs:
            reps = [{"before": o["before"], "after": o["after"]}
                    for o in sorted(occs, key=lambda x: int(x.get("pos", 0)))]
            new_text, _applied = apply_replacements(str(s.get("text", "")), reps)
            if _applied:
                s["new_text"] = new_text
        out.append(s)
    return out


# ---- 标记删除行（REQ-20260922-NNN）----
# ---- 行内切分（REQ-20260923-NNN 二次切分 + 重新拼接）----


def _norm_splits(line_splits: dict | None) -> dict[int, list[dict]]:
    """line_splits 原始数据 → {seg_id: [子段 dict…]}（非法条目剔除）。"""
    out: dict[int, list[dict]] = {}
    for key, subs in (line_splits or {}).items():
        try:
            seg_id = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(subs, list) or not subs:
            continue
        if all(isinstance(s, dict) for s in subs):
            out[seg_id] = subs
    return out


def deleted_sub_indexes(seg_key: str, subs: list[dict],
                        split_marks: dict | None) -> list[int]:
    """切分行的生效删除子段索引。

    split_marks[seg] 存在（哪怕是空列表）→ 权威全量快照（[]=全保留）；
    键缺失 → 回落到切分时算出的默认洞（sub["mark"]=="delete"，即用户
    输入中没命中的原文 token 段）。管线 accept-all 保存不传 split_marks
    → 维持默认洞语义。
    """
    raw = (split_marks or {}).get(seg_key)
    if isinstance(raw, list):
        idx: set[int] = set()
        for v in raw:
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(subs):
                idx.add(i)
        return sorted(idx)
    return [i for i, sub in enumerate(subs)
            if str(sub.get("mark") or "") == "delete"]


def cut_plan(data: dict) -> dict:
    """优化数据的规范化「剪辑计划」（重新拼接视频/字幕的唯一事实源）。

    {"marks": [整行标记（剔除已切分行）],
     "splits": {str(seg): [[s,e], …] — 已删除子段的毫秒区间},
     "split_marks": {str(seg): [生效删除子段索引]}}

    - sidecar optimize_cut.json 与 optimize_cut_ready 都以本结构的规范
      JSON 做相等比较（ marks/splits/split_marks 任一变化 → 需重剪）；
    - 切分行忽略整行标记（切分定义了该行的命运，UI 同步隐藏 🗑️）。
    """
    segments = data.get("segments") or []
    splits = _norm_splits(data.get("line_splits"))
    split_marks = data.get("split_marks") or {}
    known_ids = set()
    try:
        known_ids = {int(s.get("i", -1)) for s in segments}
    except (TypeError, ValueError):
        known_ids = set()
    plan_splits: dict[str, list[list[int]]] = {}
    plan_split_marks: dict[str, list[int]] = {}
    for seg_id, subs in splits.items():
        if seg_id not in known_ids:
            continue  # 段已被重新优化掉 → 计划里剔除
        key = str(seg_id)
        del_idx = deleted_sub_indexes(key, subs, split_marks)
        spans: list[list[int]] = []
        for i in del_idx:
            try:
                spans.append([int(subs[i]["start_ms"]), int(subs[i]["end_ms"])])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        plan_splits[key] = spans
        plan_split_marks[key] = del_idx
    split_keys = set(plan_splits)  # str 键（与 sidecar JSON 对齐）
    marks: set[int] = set()
    for m in data.get("line_marks") or []:
        try:
            mid = int(m)
        except (TypeError, ValueError):
            continue
        if mid in known_ids and str(mid) not in split_keys:
            marks.add(mid)
    return {"marks": sorted(marks), "splits": plan_splits,
            "split_marks": plan_split_marks}


def plan_has_deletions(plan: dict) -> bool:
    """剪辑计划是否有任何删除区间（整行标记或删除子段）。"""
    if not isinstance(plan, dict):
        return False
    if plan.get("marks"):
        return True
    return any(spans for spans in (plan.get("splits") or {}).values())


def deleted_intervals_ms(
    segments: list[dict], line_marks: list[int] | None,
    line_splits: dict | None = None, split_marks: dict | None = None,
) -> list[list[int]]:
    """标记删除行 + 删除子段 → 合并后的毫秒区间（升序，缝隙 <1ms 合并 — 同粗剪口径）。

    优化成片剪辑据此从 rough_compose.mp4 剪除这些区间；时间戳平移据此累计。
    REQ-20260923-NNN：line_splits 切分行的删除子段区间并入；切分行忽略
    整行标记（切分优先，与 cut_plan 同口径）。
    """
    from slirn_home import compose_service

    marks = set()
    for m in line_marks or []:
        try:
            marks.add(int(m))
        except (TypeError, ValueError):
            continue
    split_keys = set(_norm_splits(line_splits))
    spans: list[tuple[int, int]] = []
    for seg_id, subs in _norm_splits(line_splits).items():
        for i in deleted_sub_indexes(str(seg_id), subs, split_marks):
            try:
                spans.append((int(subs[i]["start_ms"]), int(subs[i]["end_ms"])))
            except (KeyError, TypeError, ValueError, IndexError):
                continue
    for s in segments or []:
        try:
            if int(s.get("i", -1)) in marks and int(s.get("i", -1)) not in split_keys:
                spans.append((int(s["start_ms"]), int(s["end_ms"])))
        except (KeyError, TypeError, ValueError):
            continue
    return compose_service.merge_intervals_ms(spans)


def shift_ms(t: int, intervals_ms: list[list[int]] | None) -> int:
    """时间戳 t 前移：减去 [0, t] 与删除区间的重叠总时长（毫秒）。

    半开语义：区间端点本身不被算作删除时长（与 ffmpeg trim 的 [start, end) 一致）。
    """
    moved = 0
    for s, e in intervals_ms or []:
        if s >= t:
            break  # 区间升序，后面都在 t 之后
        moved += max(0, min(e, t) - s)
    return int(t) - moved


def _seg_reps(data_occurrences: list[dict] | None, seg_id: int) -> list[dict]:
    """该行的生效局部替换（供切分子段文本沿用；pos 升序）。"""
    reps = []
    for o in data_occurrences or []:
        if o.get("applied", True) and str(o.get("seg")) == str(seg_id):
            reps.append({"before": o.get("before"), "after": o.get("after")})
    return reps


def _sub_text(base: str, reps: list[dict]) -> str:
    """子段文本先按行内局部替换口径过一遍（整句替换在切分行上忽略）。"""
    if not reps:
        return base
    try:
        text, _n = apply_replacements(base, reps)
        return text
    except Exception:  # noqa: BLE001
        return base


def build_srt(segments: list[dict], line_marks: list[int] | None = None,
              *, shift: bool = False, line_splits: dict | None = None,
              split_marks: dict | None = None,
              occurrences: list[dict] | None = None) -> str:
    """优化后行集 → 标准 SRT（new_text 优先；与 subtitle.srt 同构）。

    REQ-20260922-NNN 标记删除行：line_marks 命中的行不进 SRT；shift=True 时
    保留行时间戳按删除区间累计前移（优化成片 optimize_compose.mp4 时间基）。

    REQ-20260923-NNN 行内切分：切分行的保留子段各自成为独立字幕条目
    （编号 seg.N；文本 = 对齐切出的原文片段，行内生效局部替换沿用、整句
    替换忽略）；删除子段与整行标记的区间一并计入平移基准。
    """
    from slirn_home import asr_service

    marks = set()
    for m in line_marks or []:
        try:
            marks.add(int(m))
        except (TypeError, ValueError):
            continue
    splits = _norm_splits(line_splits)
    del_ms = deleted_intervals_ms(segments, line_marks, line_splits, split_marks)
    norm = []
    for s in segments:
        sid = int(s.get("i", -1))
        subs = splits.get(sid)
        if subs:
            reps = _seg_reps(occurrences, sid)
            for i, sub in enumerate(subs):
                if i in deleted_sub_indexes(str(sid), subs, split_marks):
                    continue
                item = {"i": f"{sid}.{i + 1}",
                        "start": sub.get("start", ""), "end": sub.get("end", ""),
                        "text": _sub_text(str(sub.get("text", "")), reps)}
                if shift and del_ms:
                    from slirn_home import compose_service

                    item["start"] = compose_service._ms_to_srt_time(
                        shift_ms(int(sub["start_ms"]), del_ms))
                    item["end"] = compose_service._ms_to_srt_time(
                        shift_ms(int(sub["end_ms"]), del_ms))
                norm.append(item)
            continue
        if sid in marks:
            continue
        item = {"i": s["i"], "start": s["start"], "end": s["end"],
                "text": str(s.get("new_text") or s.get("text", ""))}
        if shift and del_ms:
            from slirn_home import compose_service

            item["start"] = compose_service._ms_to_srt_time(
                shift_ms(int(s["start_ms"]), del_ms))
            item["end"] = compose_service._ms_to_srt_time(
                shift_ms(int(s["end_ms"]), del_ms))
        norm.append(item)
    return asr_service.segments_to_srt(norm)


def srt_entries(segments: list[dict], line_marks: list[int] | None = None,
                *, shift: bool = False, line_splits: dict | None = None,
                split_marks: dict | None = None,
                occurrences: list[dict] | None = None) -> list[dict]:
    """优化后行集 → [{"start_ms", "end_ms", "text"}]（排除删除行；shift 同 build_srt）。

    精剪合成字幕素材 auto 来源的内存直供数据 — 不落共享 tmp 文件，避免与
    下载端点/其他素材写同一个 tmp/optimized_subs.srt 产生竞态。
    REQ-20260923-NNN：切分行保留子段各自成条（与 build_srt 同口径）。
    """
    marks = set()
    for m in line_marks or []:
        try:
            marks.add(int(m))
        except (TypeError, ValueError):
            continue
    splits = _norm_splits(line_splits)
    del_ms = deleted_intervals_ms(segments, line_marks, line_splits, split_marks)
    out: list[dict] = []
    for s in segments:
        sid = int(s.get("i", -1))
        subs = splits.get(sid)
        if subs:
            reps = _seg_reps(occurrences, sid)
            for i, sub in enumerate(subs):
                if i in deleted_sub_indexes(str(sid), subs, split_marks):
                    continue
                sm, em = int(sub["start_ms"]), int(sub["end_ms"])
                if shift and del_ms:
                    sm, em = shift_ms(sm, del_ms), shift_ms(em, del_ms)
                out.append({"start_ms": sm, "end_ms": em,
                            "text": _sub_text(str(sub.get("text", "")), reps)})
            continue
        if sid in marks:
            continue
        sm, em = int(s["start_ms"]), int(s["end_ms"])
        if shift and del_ms:
            sm, em = shift_ms(sm, del_ms), shift_ms(em, del_ms)
        out.append({"start_ms": sm, "end_ms": em,
                    "text": str(s.get("new_text") or s.get("text", ""))})
    return out


def effective_stats(data: dict) -> dict:
    """生效统计：识别行数 / 提取出现数 / 生效替换数 / 未采纳数 / 涉及词数 /
    整句替换行数（REQ-20260922-NNN，与局部替换正交单列）/ 标记删除行数 /
    行内切分统计（REQ-20260923-NNN：切分行数 / 子段总数 / 删除子段数）。"""
    occs = data.get("occurrences") or []
    live = [o for o in occs if o.get("applied", True)]
    splits = _norm_splits(data.get("line_splits"))
    split_subs = sum(len(v) for v in splits.values())
    split_subs_del = sum(
        len(deleted_sub_indexes(str(k), v, data.get("split_marks")))
        for k, v in splits.items())
    return {
        "lines": len(data.get("segments") or []),
        "occurrences": len(occs),
        "applied": len(live),
        "skipped": len(occs) - len(live),
        "words": len(aggregate_words(occs)),
        "line_edits": len(data.get("line_edits") or []),
        "line_marks": len(data.get("line_marks") or []),
        "line_splits": len(splits),
        "split_subs": split_subs,
        "split_subs_delete": split_subs_del,
    }


# =============== 后台 job 管理（与 fine_service 同模式） ===============

# task_id → {"state": "running|done|error", "stage": str, "progress": 0-100,
#            "error": str|None, "started_at": float, "finished_at": float|None,
#            "lines": int, "occurrences": int}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def job_status(task_id: str) -> dict | None:
    with _JOBS_LOCK:
        j = _JOBS.get(task_id)
        return dict(j) if j else None


def start_job(
    task_id: str,
    video_path: Path,
    hotwords: list[str],
    task_name: str,
    outputs_dir: Path,
    entry: dict | None = None,
    on_success: Callable[[dict], None] | None = None,
    *,
    auto: bool = False,
) -> bool:
    """启动优化字幕线程（ASR 识别成片 → 大模型提取 → 落盘）。已在跑 → False。

    video_path：粗剪成片（rough_compose.mp4）；entry：本次使用的模型注册项。

    REQ-20260919-075：auto 透传到 execution_history（pipeline 自动调用标记）。
    """
    with _JOBS_LOCK:
        existing = _JOBS.get(task_id)
        if existing and existing.get("state") == "running":
            return False
        _JOBS[task_id] = {
            "state": "running", "stage": "加载模型", "progress": 0.0, "error": None,
            "started_at": time.time(), "finished_at": None,
            "lines": 0, "occurrences": 0,
        }

    # REQ-20260918-053：执行日志 — 优化字幕（LLM 调用，分钟级）
    # REQ-20260919-075：auto 透传；用户手动调为 False，pipeline 自动调为 True
    exec_id = execution_history.record_start(
        outputs_dir, execution_history.KIND_OPTIMIZE,
        extra={"model": (entry or {}).get("id") or ""},
        auto=auto)

    def _run():
        job = _JOBS[task_id]

        def _stage(name: str, pct: float) -> None:
            job["stage"] = name
            job["progress"] = round(pct, 1)
            log.info("[opt][%s] %s", task_id, name)

        try:
            # ---- ① ASR 识别成片（复用字幕生成链路；不写 subtitle.json） ----
            from slirn_home import asr_service

            _stage("加载模型", 5.0)
            hotword_str = " ".join(w.strip() for w in hotwords if w and w.strip())
            _stage("识别粗剪成片", 15.0)
            state = asr_service._run_recognition(str(video_path), hotword_str, False)
            segments = asr_service.segments_from_sentences(state.get("sentences") or [])
            if not segments:
                raise RuntimeError("成片识别结果为空（无有效字幕行）")
            job["lines"] = len(segments)
            _stage("识别完成", 40.0)
            log.info("[opt][%s] 成片识别：%d 行", task_id, len(segments))

            # ---- ② 大模型分批提取不明确字词 ----
            def _analyze(batch: list[dict], strict: bool = False) -> dict[str, list[dict]]:
                """分析一批：解析失败/截断 → 减半重试；单行仍失败 → 放弃该批
                （提议性质的提取，不阻塞流程，宁缺勿错）。"""
                from slirn_home.revision_service import _call_llm

                system = OPT_SYSTEM + (OPT_STRICT_SUFFIX if strict else "")
                try:
                    raw = _call_llm(system, build_user_prompt(task_name, hotwords, batch), entry)
                    return parse_occurrences(raw, batch)
                except ValueError as e:
                    if len(batch) == 1:
                        log.warning("[opt][%s] 行 %s 分析失败（%s），跳过",
                                    task_id, batch[0].get("id"), e)
                        return {}
                    mid = len(batch) // 2
                    log.warning("[opt][%s] 批解析失败，减半重试（%d 行）: %s",
                                task_id, len(batch), e)
                    return {**_analyze(batch[:mid], strict), **_analyze(batch[mid:], strict)}

            def _analyze_all(strict: bool = False) -> dict[str, list[dict]]:
                found: dict[str, list[dict]] = {}
                for bi, batch in enumerate(batches):
                    _stage(f"大模型分析 ({bi + 1}/{len(batches)})" + ("·复检" if strict else ""),
                           40 + (bi + 1) / max(1, len(batches)) * 50)
                    found.update(_analyze(batch, strict))
                    if bi < len(batches) - 1:
                        time.sleep(0.6)  # 批间间隔，降低限流概率
                return found

            lines = [{"id": str(s["i"]), "text": str(s.get("text", ""))} for s in segments]
            batches = [lines[k:k + BATCH_SIZE] for k in range(0, len(lines), BATCH_SIZE)] or [[]]
            found = _analyze_all()
            # 全部批次零发现时严格复检一轮 — 大模型抽样波动下可能漏掉整段，
            # 复检一轮显著降低「点了开始却什么都没提出来」的概率（仍零则视为确实干净）。
            if not found and len(lines) >= 5:
                log.info("[opt][%s] 首轮零发现，严格复检一轮（%d 行）", task_id, len(lines))
                _stage("零发现复检", 92.0)
                found = _analyze_all(strict=True)

            # ---- ③ 落盘（出现项默认全部"待采纳"，applied=True 待人工改判；
            #      reviewed=False 待人工处理 — REQ-038 词级处理进度口径）----
            _stage("保存结果", 95.0)
            occurrences = []
            for rid in sorted(found, key=int):
                for occ in found[rid]:
                    occurrences.append({
                        "occ_id": len(occurrences),
                        "seg": int(rid), "pos": int(occ.get("pos", 0)),
                        "before": str(occ["before"]), "after": str(occ["after"]),
                        "reason": str(occ.get("reason") or ""),
                        "applied": True,
                        "reviewed": False,
                    })
            data = {
                "version": 1,
                "video": Path(video_path).name,
                "asr": {"model": "seaco-paraformer", "sd": False},
                "model": (entry or {}).get("id") or "qwen-plus",
                "provider": (entry or {}).get("provider", ""),
                "protocol": (entry or {}).get("protocol", "openai"),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "saved_at": None,
                "hotwords": list(hotwords),
                "segments": segments,
                "occurrences": occurrences,
                "words": aggregate_words(occurrences),
                "mapping": build_mapping(occurrences),
                "stats": {"lines": len(segments), "occurrences": len(occurrences)},
            }
            out_dir = Path(outputs_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / OPTIMIZE_JSON).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            job["occurrences"] = len(occurrences)
            job["state"] = "done"
            job["stage"] = "完成"
            job["progress"] = 100.0
            job["finished_at"] = time.time()
            log.info("[opt][%s] 完成：识别 %d 行 · 不明确出现 %d 处（%d 个词）",
                     task_id, len(segments), len(occurrences), len(data["words"]))
            # REQ-20260918-053：执行日志 — 补结果摘要 + 标记 success
            try:
                execution_history.patch_extra(outputs_dir, exec_id,
                                                {"lines": len(segments),
                                                 "occurrences": len(occurrences),
                                                 "words": len(data["words"])})
                # REQ-20260919-075：完成时回填具体执行情况（行数 + 词数 + 出现次数）
                execution_history.patch_fields(outputs_dir, exec_id, {
                    "description": (f"优化字幕保存：识别 {len(segments)} 行字幕，"
                                    f"提取 {len(occurrences)} 处不明确字词（{len(data['words'])} 个词）")
                })
                execution_history.record_finish(outputs_dir, exec_id, success=True, error="")
            except Exception as eh:  # noqa: BLE001
                log.warning("[opt][%s] history record_finish 失败: %s", task_id, eh)
            if on_success:
                try:
                    on_success(data)
                except Exception as e:  # noqa: BLE001 — 回调失败不影响结果
                    log.warning("[opt][%s] on_success 回调失败: %s", task_id, e)
        except Exception as e:  # noqa: BLE001 — 后台线程必须全兜底
            log.exception("[opt][%s] 优化字幕失败", task_id)
            msg = str(e)
            if isinstance(e, MemoryError) or "Unable to allocate" in msg:
                msg += "（内存不足：请关闭其他应用（如上游 FunClip 服务）后重试）"
            job["state"] = "error"
            job["error"] = msg
            job["finished_at"] = time.time()
            # REQ-20260918-053：执行日志 — 失败落盘
            try:
                execution_history.record_finish(outputs_dir, exec_id, success=False, error=msg)
            except Exception as eh:  # noqa: BLE001
                log.warning("[opt][%s] history record_finish(failed) 失败: %s", task_id, eh)

    threading.Thread(target=_run, name=f"optimize-{task_id}", daemon=True).start()
    return True


# =============== 读取 / 决策保存 ===============

def load_optimize(outputs_dir: Path) -> dict | None:
    """读取 optimize_subtitle.json（无/损坏 → None）。"""
    p = Path(outputs_dir) / OPTIMIZE_JSON
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("optimize_subtitle.json 损坏: %s", e)
        return None


def save_decisions(
    outputs_dir: Path,
    decisions: list[dict],
    line_edits: list[dict] | None = None,
    line_marks: list[int] | None = None,
    split_marks: dict | None = None,
) -> tuple[dict, int]:
    """把人工决定合并落盘。返回 (data, 生效条数)。

    decisions：[{"occ_id": int, "applied": bool, "after": str, "reviewed": bool}]
    （全量口径 — 未列出的出现项视为不采纳/未处理）。生效要求 after 非空且
    != before（人工编辑改回原文 = 不采纳，服务端兜底）。保存时重算各行
    new_text、词频与对应关系汇总，saved_at 置当前时间（阶段完成标记）。

    REQ-20260922-NNN 整句替换：line_edits [{"seg": int, "text": str}]，同样
    全量口径 — 不传（None/[]）= 清空已有整句替换。校验：非 dict / 非法 seg /
    未知 seg / 空白文本 / 与原文相同 → 丢弃。整句行跳过局部替换（见
    apply_to_segments）。

    REQ-20260922-NNN 标记删除行：line_marks [seg id, …]，全量口径 — 不传
    （None/[]）= 清空。校验：非法 int / 未知 seg → 丢弃；去重升序。保存后有
    标记行的剪辑由 /save_optimize_subtitle 端点 kick（见 compose_service.
    start_optimize_cut）。

    REQ-20260923-NNN 行内切分：split_marks {"<seg>": [子段索引…]}，全量口径
    — 不传（None）= 清空所有手工翻转（回落到切分默认洞：输入未命中的原文
    token 段视为删除）。校验：非 dict / 未知切分行 / 越界索引 → 丢弃，去重
    升序。整行标记与切分互斥：已切分行的整行标记被丢弃（切分优先）。
    """
    data = load_optimize(outputs_dir)
    if data is None:
        raise RuntimeError("optimize_subtitle.json 不存在")
    dec_by_id: dict[int, dict] = {}
    for d in decisions or []:
        try:
            dec_by_id[int(d.get("occ_id"))] = d
        except (TypeError, ValueError):
            continue
    applied_n = 0
    for occ in data.get("occurrences") or []:
        d = dec_by_id.get(int(occ["occ_id"]))
        after = str((d or {}).get("after", occ.get("after")))
        want = bool((d or {}).get("applied"))
        # REQ-038：处理进度落盘 — 出现处切换过 ✓/✕ 或编辑过替换值即 reviewed
        occ["reviewed"] = bool((d or {}).get("reviewed"))
        if want and after and after != str(occ.get("before")):
            occ["applied"] = True
            occ["after"] = after  # 人工编辑过的替换值
            applied_n += 1
        else:
            occ["applied"] = False
    # 整句替换：全量快照校验（服务端兜底，前端同款收集但不可信）
    seg_texts = {str(s.get("i")): str(s.get("text", "")) for s in data.get("segments") or []}
    edits: list[dict] = []
    for e in line_edits or []:
        if not isinstance(e, dict):
            continue
        try:
            seg_key = str(int(e.get("seg")))
        except (TypeError, ValueError):
            continue
        text = str(e.get("text") or "").strip()
        if not text or seg_key not in seg_texts or text == seg_texts[seg_key]:
            continue
        edits.append({"seg": int(seg_key), "text": text})
    edits.sort(key=lambda x: x["seg"])
    data["line_edits"] = edits
    # 标记删除行：全量快照校验 + 去重升序
    marks: set[int] = set()
    for m in line_marks or []:
        try:
            key = str(int(m))
        except (TypeError, ValueError):
            continue
        if key in seg_texts:
            marks.add(int(key))
    # REQ-20260923-NNN：切分行剪除死数据（段已被重新优化掉）+ 整行标记互斥
    live_splits = _norm_splits(data.get("line_splits"))
    split_keys = {str(k) for k in live_splits}
    if live_splits:
        data["line_splits"] = {str(k): v for k, v in live_splits.items()}
        targets = data.get("split_targets") or {}
        data["split_targets"] = {k: v for k, v in targets.items() if k in split_keys}
        marks = {m for m in marks if str(m) not in split_keys}
    data["line_marks"] = sorted(marks)
    # REQ-20260923-NNN：切分子段标记全量快照（键缺失/越界索引丢弃）
    norm_split_marks: dict[str, list[int]] = {}
    if isinstance(split_marks, dict):
        for key, idx_list in split_marks.items():
            seg_id = _safe_int(key)
            subs = live_splits.get(seg_id)
            if subs is None or not isinstance(idx_list, list):
                continue
            idx: set[int] = set()
            for v in idx_list:
                try:
                    i = int(v)
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(subs):
                    idx.add(i)
            norm_split_marks[str(seg_id)] = sorted(idx)  # 键归一到 str(int)，与 line_splits 对齐
    data["split_marks"] = norm_split_marks
    data["segments"] = apply_to_segments(data.get("segments") or [], data["occurrences"], edits)
    data["words"] = aggregate_words(data["occurrences"])
    data["mapping"] = build_mapping(data["occurrences"])
    data["stats"] = effective_stats(data)
    data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p = Path(outputs_dir) / OPTIMIZE_JSON
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data, applied_n


def _safe_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---- 行内切分：/optimize_resplit、/optimize_unsplit 的服务端（REQ-20260923-NNN）----

def resplit_line(outputs_dir: Path, seg_id: int, target_text: str) -> tuple[dict | None, str]:
    """把一行优化字幕按目标文字切分成多个子段（立即写入 optimize_subtitle.json）。

    机制与切分修剪阶段同源（cutlist_service 的 token 对齐）：
    - 有字级时间戳（tokens/token_ts 等长）→ align_tokens + partition_tokens
      完整划分（keep 块 = 目标文字命中的连续 token 段，delete 洞 = 间隙），
      洞边界外扩贴邻（与 cutlist _split_items 同口径），零长洞不产段；
    - 无字级时间戳 → 按文本长度比例在 [start_ms, end_ms] 内插值降级
      （子段带 fallback:true，UI 显示 ⚠️）；
    - 目标与原 token 全对不上（命中 0）→ 报错不写。

    重切（同一行再次调用）重置该行的子段标记为默认洞、清除整行标记。
    返回 ({"seg", "subs", "fallback"}, "") 或 (None, 错误信息)。
    """
    from slirn_home import cutlist_service as _cl

    data = load_optimize(outputs_dir)
    if data is None:
        return None, "optimize_subtitle.json 不存在"
    target = str(target_text or "").strip()
    if not target:
        return None, "切分后内容不能为空"
    seg = None
    for s in data.get("segments") or []:
        if _safe_int(s.get("i")) == _safe_int(seg_id):
            seg = s
            break
    if seg is None:
        return None, f"未找到第 {seg_id} 条字幕"
    seg_start_ms, seg_end_ms = int(seg.get("start_ms", 0)), int(seg.get("end_ms", 0))
    aligned = _cl._segment_tokens(seg)

    subs: list[dict] = []
    fallback = False
    if aligned:
        orig_tokens, token_ts = aligned
        keep_marks = _cl.align_tokens(orig_tokens, _cl.tokenize(target))
        if keep_marks:
            parts = _cl.partition_tokens(len(orig_tokens), keep_marks)
            prev_e = seg_start_ms
            for mark, a, b in parts:
                if mark == "keep":
                    s_ms, e_ms = token_ts[a][0], token_ts[b][1]
                else:  # delete 洞外扩：贴前段终点 / 下一 keep 块首 token 起点
                    s_ms = prev_e
                    e_ms = token_ts[b + 1][0] if b + 1 < len(token_ts) else seg_end_ms
                s_ms = max(s_ms, prev_e)  # ts 非单调防御
                if e_ms - s_ms <= 0:
                    continue  # 零长洞不产段
                subs.append({
                    "mark": mark,
                    "start_ms": s_ms, "end_ms": e_ms,
                    "start": _cl.ms2srt(s_ms), "end": _cl.ms2srt(e_ms),
                    "text": _cl.join_tokens(orig_tokens[a:b + 1]),
                    "fallback": False,
                })
                prev_e = e_ms
        if not subs:
            return None, "切分后内容与原行文字对不上（命中 0 字）— 请检查内容后重试"
    if not aligned:
        # 比例降级：目标 token 在原文本 token 序列上贪心对齐拿字符边界，
        # 时间按 token 字符长度占比在行时间窗内插值
        fallback = True
        text_tokens = _cl.tokenize(str(seg.get("text", "")))
        target_tokens = _cl.tokenize(target)
        hit = _cl.align_tokens(text_tokens, target_tokens)
        if not hit:
            return None, "切分后内容与原行文字对不上（命中 0 字）— 请检查内容后重试"
        total_chars = sum(len(t) for t in text_tokens) or 1
        win = max(0, seg_end_ms - seg_start_ms)
        cum = 0
        bounds: list[int] = []  # 每个 token 的 (起始字符累计)
        token_char_start = []
        for t in text_tokens:
            token_char_start.append(cum)
            cum += len(t)
        prev_e = seg_start_ms
        for mark, a, b in _cl.partition_tokens(len(text_tokens), hit):
            s_frac = token_char_start[a] / total_chars
            e_frac = (token_char_start[b] + len(text_tokens[b])) / total_chars
            s_ms = seg_start_ms + int(round(s_frac * win))
            e_ms = seg_start_ms + int(round(e_frac * win))
            s_ms = max(s_ms, prev_e)
            if e_ms - s_ms <= 0:
                continue
            subs.append({
                "mark": mark,
                "start_ms": s_ms, "end_ms": e_ms,
                "start": _cl.ms2srt(s_ms), "end": _cl.ms2srt(e_ms),
                "text": _cl.join_tokens(text_tokens[a:b + 1]),
                "fallback": True,
            })
            prev_e = e_ms
        if not subs:
            return None, "切分后没有产生任何子段"

    # 写回：切分几何 + 默认删除索引（洞） + 用户输入（重切预填）；清整行标记
    key = str(int(seg_id))
    line_splits = data.get("line_splits") or {}
    line_splits[key] = subs
    data["line_splits"] = line_splits
    split_marks = data.get("split_marks") or {}
    split_marks[key] = [i for i, sub in enumerate(subs) if sub["mark"] == "delete"]
    data["split_marks"] = split_marks
    targets = data.get("split_targets") or {}
    targets[key] = target[:500]
    data["split_targets"] = targets
    data["line_marks"] = [m for m in (data.get("line_marks") or []) if str(m) != key]
    data["stats"] = effective_stats(data)
    p = Path(outputs_dir) / OPTIMIZE_JSON
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return ({"seg": int(seg_id), "subs": subs, "fallback": fallback,
             "deleted_default": len(split_marks[key])}, "")


def unsplit_line(outputs_dir: Path, seg_id: int) -> tuple[dict | None, str]:
    """取消一行的切分，恢复整行（弹出 line_splits / split_marks / split_targets）。"""
    data = load_optimize(outputs_dir)
    if data is None:
        return None, "optimize_subtitle.json 不存在"
    key = str(int(seg_id))
    splits = data.get("line_splits") or {}
    if key not in splits:
        return None, f"第 {seg_id} 条没有切分"
    splits.pop(key, None)
    data["line_splits"] = splits
    marks = data.get("split_marks") or {}
    marks.pop(key, None)
    data["split_marks"] = marks
    targets = data.get("split_targets") or {}
    targets.pop(key, None)
    data["split_targets"] = targets
    data["stats"] = effective_stats(data)
    p = Path(outputs_dir) / OPTIMIZE_JSON
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"seg": int(seg_id), "subs_left": len(splits)}, ""


# ---- 批量替换（REQ-20260929-NNN）：/opt_batch_search、/opt_batch_replace ----
# 用户需求：搜指定词 → 每处命中展示前两行 + 本行 + 后两行共 5 行上下文 →
# 输入替换文字 → 确认按钮一次替换全部。复用「出现项」机制注入（reason=批量替换，
# 已采纳已处理）— 行内 chip / 词频面板 / SRT 生成 / 后续逐处改判全部沿用现成管线。

BATCH_REPLACE_REASON = "批量替换"


def _batch_find_positions(text: str, word: str) -> list[int]:
    """大小写不敏感地找 word 在 text 中的全部不重叠出现位置。

    位置按原文计（供 before 精确切片与 occ.pos 落盘）；.lower() 改变长度的
    罕见折叠字符（如 İ）会让小写索引错位 → 回退精确匹配保平安。
    """
    low_t, low_w = text.lower(), word.lower()
    if len(low_t) != len(text) or len(low_w) != len(word):
        out: list[int] = []
        start = 0
        while True:
            p = text.find(word, start)
            if p < 0:
                return out
            out.append(p)
            start = p + len(word)
    out = []
    start = 0
    while True:
        p = low_t.find(low_w, start)
        if p < 0:
            return out
        out.append(p)
        start = p + len(word)


def _applied_spans(data: dict) -> dict[int, list[tuple[int, int]]]:
    """现有**已采纳**出现项在各行原文中占用的区间（镜像 apply_replacements 的
    占位扫描 — 未采纳项保存时不占文本区间，不算重叠）。"""
    from slirn_home.fine_service import apply_replacements

    by_seg: dict[str, list[dict]] = {}
    for o in data.get("occurrences") or []:
        if o.get("applied", True):
            by_seg.setdefault(str(o.get("seg")), []).append(o)
    text_by_seg = {str(s.get("i")): str(s.get("text") or "")
                   for s in data.get("segments") or []}
    spans: dict[int, list[tuple[int, int]]] = {}
    for rid, occs in by_seg.items():
        text = text_by_seg.get(rid)
        if text is None:
            continue
        reps = [{"before": str(o["before"]), "after": str(o.get("after") or "")}
                for o in occs if str(o.get("before") or "")]
        _, applied = apply_replacements(text, reps)
        spans[int(rid)] = [(a["pos"], a["pos"] + len(a["before"])) for a in applied]
    return spans


def _batch_matches(data: dict, word: str) -> list[dict]:
    """批量替换的统一匹配口径（search 预览与 replace 注入共用同一份结果）。

    每个命中行：{seg, text(原文), eff(生效文本), positions(全部命中位),
    free_positions(可注入位 = 排除阻断行与现有替换重叠), block, context(前后
    各两行 + 本行，按 segments 数组序取连续 5 行)}. 阻断行仍返回（预览需要
    展示并标注），但 free_positions 为空 → replace 跳过。
    """
    segments = data.get("segments") or []
    edits = {str(int(e.get("seg"))) for e in data.get("line_edits") or []
             if isinstance(e, dict) and str(e.get("text") or "").strip()}
    splits = set(_norm_splits(data.get("line_splits")))
    marks = {str(m) for m in data.get("line_marks") or []}
    spans = _applied_spans(data)
    matches: list[dict] = []
    for k, s in enumerate(segments):
        try:
            seg_id = int(s.get("i"))
        except (TypeError, ValueError):
            continue
        rid = str(seg_id)
        text = str(s.get("text") or "")
        positions = _batch_find_positions(text, word)
        if not positions:
            continue
        if rid in edits:
            block = "full_edit"  # 整句替换覆盖本行全部局部替换，注入无意义
        elif seg_id in splits:
            block = "split"  # 行已切分子段，occ 与子段文本对齐会被破坏
        elif rid in marks:
            block = "deleted"  # 行已标记删除，成片将剪除该行
        else:
            block = ""
        free = [] if block else [
            p for p in positions
            if all(p >= e or p + len(word) <= b for b, e in spans.get(seg_id, []))]
        matches.append({
            "seg": seg_id, "text": text,
            "eff": str(s.get("new_text") or text),
            "positions": positions, "free_positions": free, "block": block,
            "eligible": bool(free),
            "skipped_overlap": (len(positions) - len(free)) if not block else 0,
            "context": [{
                "seg": int(s2.get("i")),
                "text": str(s2.get("new_text") or s2.get("text") or ""),
                "deleted": 1 if str(s2.get("i")) in marks else 0,
            } for s2 in segments[max(0, k - 2): k + 3]],
        })
    return matches


def batch_search(outputs_dir: Path, word: str) -> tuple[dict | None, str]:
    """查找预览：命中行 + 每处前后各两行（共 5 行）上下文，不写盘。"""
    data = load_optimize(outputs_dir)
    if data is None:
        return None, "尚无优化字幕数据 — 请先执行「开始优化字幕」"
    word = (word or "").strip()
    if not word:
        return None, "请输入要查找的词"
    matches = _batch_matches(data, word)
    hits = sum(len(m["positions"]) for m in matches)
    return {
        "word": word, "matches": matches,
        "lines": len(matches), "hits": hits,
        "eligible_lines": sum(1 for m in matches if m["free_positions"]),
        "free_hits": sum(len(m["free_positions"]) for m in matches),
        "blocked_lines": sum(1 for m in matches if m["block"]),
    }, ""


def batch_replace(outputs_dir: Path, word: str, replacement: str) -> tuple[dict | None, str]:
    """确认替换：给每个可注入位注入出现项（reason=批量替换，已采纳已处理），
    立即写盘（与 resplit 同语义 — 不依赖「确认保存」，阶段确认标记 saved_at
    不动）。occ_id 接在现有最大值之后递增；重算 new_text / 词频 / 对应关系。"""
    data = load_optimize(outputs_dir)
    if data is None:
        return None, "尚无优化字幕数据 — 请先执行「开始优化字幕」"
    word = (word or "").strip()
    replacement = (replacement or "").strip()
    if not word:
        return None, "请输入要查找的词"
    if not replacement:
        return None, "请输入替换后的文字"
    if replacement == word:
        return None, "替换文字与要查找的词相同，无需替换"
    matches = _batch_matches(data, word)
    occurrences = list(data.get("occurrences") or [])
    next_id = 1 + max((int(o["occ_id"]) for o in occurrences
                       if isinstance(o, dict) and o.get("occ_id") is not None),
                      default=-1)
    text_by_seg = {int(s.get("i")): str(s.get("text") or "")
                   for s in data.get("segments") or [] if s.get("i") is not None}
    injected = 0
    segs_touched: list[int] = []
    for m in matches:
        if not m["free_positions"]:
            continue
        text = text_by_seg.get(m["seg"], "")
        for p in m["free_positions"]:
            occurrences.append({
                "occ_id": next_id, "seg": m["seg"], "pos": p,
                "before": text[p:p + len(word)], "after": replacement,
                "reason": BATCH_REPLACE_REASON, "applied": True, "reviewed": True,
            })
            next_id += 1
            injected += 1
        segs_touched.append(m["seg"])
    if not injected:
        return None, ("没有可替换的位置 — 命中行均为整句替换/已切分/已标记删除，"
                      "或与现有替换重叠")
    data["occurrences"] = occurrences
    data["segments"] = apply_to_segments(
        data.get("segments") or [], occurrences, data.get("line_edits"))
    data["words"] = aggregate_words(occurrences)
    data["mapping"] = build_mapping(occurrences)
    data["stats"] = effective_stats(data)
    p = Path(outputs_dir) / OPTIMIZE_JSON
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"word": word, "replacement": replacement, "replaced": injected,
            "lines": len(segs_touched), "segs": segs_touched}, ""
