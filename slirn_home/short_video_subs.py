"""REQ-20261019-227 / REQ-20261019-228 — Stage 5 粗剪成片字幕。

REQ-227：按合成后视频提取 + 行内改字（原文保留）。
REQ-228：ASR 直出断句不可靠（一句话拆两段 / 两句话拼一条 / 无标点）→
提取管线加一道 **LLM 断句+标点整理**（带提示词），失败回退 ASR 原样。
REQ-230：整理出的句子可能一条五六秒、屏幕一行放不下 → ``_split_long_cues``
时长护栏：每条目标 2~3 秒、硬上限 4 秒，长句在停顿标点处拆条。
REQ-231：拆条规则不变，但每条**行尾任何标点剥除**（行中逗号保留）——
``_strip_cue_terminal_punct`` 在时长护栏之后执行。

四文件制（都在 ``stage5/``）：

- ``subs_NN.raw? asr.srt``     FunASR 直出（冻结，最底层备份）
- ``subs_NN.base.srt``         整理后的基准（LLM 成功 = 断句+标点版；失败 =
  ASR 原样拷贝）。**编辑器原文基线**，冻结不改写
- ``subs_NN.edits.json``       改字覆盖层 ``{"<cue_index>": "新文字", ...}``
- ``subs_NN.srt``              最终消费文件（base + edits 物化；下载/预览/
  精剪烧录统一读这个）

设计要点：

- cue_index 用解析**枚举位**（1-based），不用 SRT 块自带序号。
- 物化 = base 版逐 cue 替换文字、时间戳逐字保留 → 编辑永远不破坏时间轴。
- LLM 整理**只许动断句和标点**：输出按「parts（行号+文字片段）」还原每行
  原文（去标点逐字比对），任何丢字/改字/乱序 → 整体放弃回退 ASR 原样。
- 跨行合并的 cue 时间 = 首段起 → 末段止；行内拆分按去标点字数比例切时间。
- 粗剪（重）合成该条 / 删除粗剪成品 → ``invalidate_subs`` 删四件套。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path

from slirn_home import short_video_service as svc

log = logging.getLogger(__name__)


# ---------- 内存 job 表（仿 short_video_coarse._JOBS；key 多一维 clip index） ----------

_JOBS: dict[tuple[str, str, int], dict] = {}
_JOBS_LOCK = threading.Lock()


def subs_job_status(task_id: str, project_id: str, clip_index: int) -> dict | None:
    """读内存 job 状态；服务重启后内存清空 → None（前端按磁盘兜底）。"""
    with _JOBS_LOCK:
        j = _JOBS.get((str(task_id or ""), str(project_id or ""), int(clip_index or 0)))
        return dict(j) if j else None


def _set_job(task_id: str, project_id: str, clip_index: int, **fields) -> None:
    with _JOBS_LOCK:
        key = (str(task_id or ""), str(project_id or ""), int(clip_index or 0))
        cur = _JOBS.get(key) or {"state": "idle", "progress": 0.0, "stage": "",
                                 "error": None, "started_at": 0.0, "finished_at": None}
        cur.update(fields)
        _JOBS[key] = cur


# ---------- SRT 解析 / 物化 ----------

def _load_cues(path: Path) -> list[dict]:
    """解析 SRT 为 cue 列表（枚举位 1-based）。文件缺失/为空 → []。

    读入先洗掉 BOM：funasr 直出首行「\\ufeff1」的 BOM 会让解析器把序号行
    误判成文字、粘进第 1 条 cue（REQ-228 实测）。"""
    if not path.is_file():
        return []
    from slirn_home.short_video_analyze import _parse_srt_lines

    raw = path.read_text(encoding="utf-8", errors="replace").replace("﻿", "")
    lines = _parse_srt_lines(raw)
    return [
        {"i": pos, "start_ms": ln.start_ms, "end_ms": ln.end_ms, "text": ln.text}
        for pos, ln in enumerate(lines, start=1)
    ]


def _load_edits(edits_path: Path) -> dict[str, str]:
    if not edits_path.is_file():
        return {}
    try:
        data = json.loads(edits_path.read_text(encoding="utf-8", errors="replace"))
        return {str(k): str(v) for k, v in (data or {}).items()} if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001 — 覆盖层损坏不致命，按无改字处理
        log.warning("[subs] edits.json 解析失败（按无改字处理）: %s", e)
        return {}


def _fmt_srt_time(ms: int) -> str:
    h, rem = divmod(max(0, int(ms)), 3600 * 1000)
    m, rem = divmod(rem, 60 * 1000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _write_srt_file(path: Path, cues: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for c in cues:
        out.append(str(c["i"]))
        out.append(f"{_fmt_srt_time(c['start_ms'])} --> {_fmt_srt_time(c['end_ms'])}")
        out.append(str(c["text"]))
        out.append("")
    # 末尾补空行：块间/块后统一空行（标准 SRT 形态，ASR 原样拷贝可逐字节相等）
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _materialize_subs_srt(base_path: Path, subs_path: Path, edits: dict[str, str]) -> int:
    """base 版 + edits 覆盖层 → 重写 subs_NN.srt（时间戳逐字保留）。返回 cue 数。"""
    cues = _load_cues(base_path)
    out_cues = [
        {**c, "text": edits.get(str(c["i"]), c["text"])} for c in cues
    ]
    _write_srt_file(subs_path, out_cues)
    return len(out_cues)


# ---------- REQ-228：LLM 断句 + 标点整理 ----------

# 比对用剥离：空白 + 中西标点全列（时间比例也按去标点字数算——标点零时长）
_STRIP_RE = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()《》【】\[\]{}—…·~～　「」『』]+")
_LATIN_RE = re.compile(r"[A-Za-z0-9]")

# 行数护栏：超长输出顶爆 max_tokens → 直接放弃整理（本管线片段为短视频，
# 实测 90s ≈ 37 行；120 行 ≈ 5 分钟，足够宽）
_REFINE_MAX_CUES = 120

# 阶段 1 提示词：只做断句合拆（不带标点任务——单任务显著提高 verbatim
# 合规率；标点交阶段 2 + 程序兜底）
_GROUP_SYSTEM_PROMPT = (
    "你是字幕断句整理助手。输入是视频 ASR 字幕（中文，断句不可靠）。任务："
    "**只做断句重组，不碰标点**：\n"
    "1. 同一句话被 ASR 拆成多行的，合并为一条；\n"
    "2. 两句不同的话被 ASR 拼在同一行的，必须在句子边界拆开——上一句的结尾"
    "和下一句的开头绝不能留在同一条字幕里；\n"
    "3. 不构成句子的碎片（纯语气词/半截词）并入语义最连贯的相邻一条——"
    "不能独立成句的短行不许单独成条。\n\n"
    "输出严格 JSON（不要解释、不要 Markdown 代码块）：\n"
    '{"cues": [{"parts": [{"i": 行号, "t": "该行进入本条的文字'
    '（原文照抄，不加任何标点）"}]}]}\n'
    "约束：每条 cues 的 parts 按行号升序；同一行被拆进两条时在两条里各给一个 "
    "part；全部 parts 按行号拼起来必须与该行原文一字不差。\n\n"
    "示例——输入：\n"
    "第1行 [0.0-2.5s] 今天天气很好\n"
    "第2行 [2.5-5.0s] 我们出去走走吧\n"
    "输出：\n"
    '{"cues": [{"parts": [{"i": 1, "t": "今天天气很好"},'
    '{"i": 2, "t": "我们出去走走吧"}]}]}'
)

# 阶段 2 提示词：只补标点（逐条独立校验，失败该条回退原文+程序兜底）
_PUNCT_SYSTEM_PROMPT = (
    "你是字幕标点整理助手。输入是一批无标点（或标点不全）的中文字幕条，"
    "每条是一个完整句子的文字。任务：**只补标点**：\n"
    "1. 句中按语义补正确的中文标点（，、：；）；\n"
    "2. 每条句末必须以终止标点（。？！之一）收尾——逗号不能作为一条的结尾；\n"
    "3. 严禁改写：除标点外不得增字、删字、换字、同义替换。\n\n"
    "输出严格 JSON（不要解释、不要 Markdown 代码块）：\n"
    '{"texts": ["第1条加标点后的文字", "第2条加标点后的文字", …]}\n'
    "约束：texts 条数与输入完全一致、顺序一致；每条去标点后与输入一字不差。\n\n"
    "示例——输入：\n"
    "第1条 今天天气很好我们出去走走吧\n"
    "第2条 这个功能很好用\n"
    "输出：\n"
    '{"texts": ["今天天气很好，我们出去走走吧！", "这个功能很好用。"]}'
)


def _strip_for_cmp(s: str) -> str:
    return _STRIP_RE.sub("", s or "")


def _join_parts(texts: list[str]) -> str:
    """拼 cue 文字；拉丁/数字边界补一个空格（跨行拼接不粘词）。"""
    out = ""
    for t in texts:
        if out and t and _LATIN_RE.match(out[-1]) and _LATIN_RE.match(t[0]):
            out += " "
        out += t
    return out


def _ensure_terminal_punct(text: str) -> str:
    """接受条目兜底补终止标点（REQ-228 实测模型对碎片段不给句末标点）。

    纯标点变更（字符校验已保证文字一字不差，标点本就属允许改动）：
    尾部逗号/顿号/分号 → 换句号（逗号结尾的字幕看起来断掉）；无标点 → 补
    句号；已带终止标点（。？！…）不动；冒号结尾不动（枚举引导句，补句号
    反而难看）。只用于 LLM 接受条目，回退原行保持 ASR 原样。
    """
    t = (text or "").rstrip()
    while t and t[-1] in "，,、；;—–·":  # 逗/顿/分/破折/间隔 → 剥掉换句号
        t = t[:-1]
    if not t:
        return text
    if t.endswith(("。", "？", "！", "?", "!", "…", "：", ":")):
        return t
    return t + "。"


def _llm_group_cues(cues: list[dict], entry) -> tuple[list, dict] | None:
    """阶段 1（断句）：行 → 分组（合句/拆句），**逐行接受**。

    「全对才要」在真实 LLM 上几乎必败——30 行 1 处偏差就全盘报废；逐行
    接受把损失面缩到单行，且**改字永不落盘**：

    - 每行去标点游标：part 必须严格接续（丢字/换字/乱序即弃）；
    - 行内拆分 all-or-nothing：行被任何坏 cue 触碰 → 整行回退原 cue；
    - 行游标最终没到底（半行没消费）→ 整行回退；
    - 已接受 cue 引用坏行 → 该 cue 也弃（防半合并/重复文本），迭代到稳定。

    返回 (接受分组列表, line_map)；全被拒 / 调用失败 → None。
    """
    if not cues or len(cues) > _REFINE_MAX_CUES:
        return None
    from slirn_home.revision_service import _call_llm
    from slirn_home.short_video_analyze import _strip_json_block

    payload = "\n".join(
        f"第{c['i']}行 [{c['start_ms'] / 1000:.1f}-{c['end_ms'] / 1000:.1f}s] {c['text']}"
        for c in cues
    )
    user = f"ASR 字幕行（行号 [起-止秒] 文字）：\n{payload}\n\n请整理输出 JSON。"
    try:
        raw = _call_llm(_GROUP_SYSTEM_PROMPT, user, entry=entry, retries=1,
                        max_tokens=8192, temperature=0.2)
        data = _strip_json_block(raw)
    except Exception as e:  # noqa: BLE001 — LLM 挂/超时/输出非 JSON 都按未整理降级
        log.warning("[subs] 断句调用失败（回退 ASR 原样）: %s", e)
        return None
    out_cues = data.get("cues") if isinstance(data, dict) else None
    if not isinstance(out_cues, list) or not out_cues:
        return None

    # 每行剥离基准 + 游标
    line_map: dict[int, dict] = {}
    for c in cues:
        stripped = _strip_for_cmp(c["text"])
        if not stripped:
            return None  # 行内没有可比对字符（罕见），保守整体放弃
        line_map[c["i"]] = {
            "stripped": stripped, "pos": 0, "ok": True,
            "start_ms": c["start_ms"], "end_ms": c["end_ms"], "text": c["text"],
        }

    # 第一遍：逐 cue 校验；坏 cue 触碰的行全标坏
    built_all: list[tuple[list, set] | None] = []
    for cue in out_cues:
        parts = cue.get("parts") if isinstance(cue, dict) else None
        if not isinstance(parts, list) or not parts:
            built_all.append(None)
            continue
        built: list[tuple[int, str, str, int, int]] = []
        touched: set[int] = set()
        bad = ""
        for p in parts:
            if not isinstance(p, dict):
                bad = "part 非对象"
                break
            try:
                li = int(p.get("i"))
            except (TypeError, ValueError):
                bad = f"行号非法: {p.get('i')!r}"
                break
            lm = line_map.get(li)
            if lm is None:
                bad = f"行号不存在: {li}"
                break
            t = str(p.get("t") or "")
            ts = _strip_for_cmp(t)
            if not ts:
                bad = f"第{li}行 part 无有效文字（纯标点?）"
                break
            if lm["stripped"][lm["pos"]:lm["pos"] + len(ts)] != ts:
                want = lm["stripped"][lm["pos"]:lm["pos"] + len(ts)]
                bad = f"第{li}行文字不接续: 期望「{want[:20]}」得到「{ts[:20]}」"
                break
            built.append((li, t, ts, lm["pos"], lm["pos"] + len(ts)))
            lm["pos"] += len(ts)
            touched.add(li)
        if bad:
            log.info("[subs] LLM 整理拒一条 cue（%s）→ 涉及行回退原样", bad)
            for li in touched:
                line_map[li]["ok"] = False
            built_all.append(None)
        else:
            built_all.append((built, touched))

    # 半行没消费 → 整行回退；坏行传染引用它的已接受 cue，迭代到稳定
    changed = True
    while changed:
        changed = False
        for li, lm in line_map.items():
            if lm["ok"] and lm["pos"] != len(lm["stripped"]):
                lm["ok"] = False
                changed = True
        for k, item in enumerate(built_all):
            if item is None:
                continue
            built, touched = item
            if any(not line_map[li]["ok"] for li in touched):
                log.info("[subs] 坏行传染：拒一条引用行 %s 的 cue", sorted(touched))
                for li in touched:
                    line_map[li]["ok"] = False
                built_all[k] = None
                changed = True

    accepted = [b for b in built_all if b is not None]
    if not accepted:
        log.warning("[subs] 断句全被拒（逐行校验均不过）→ 整体回退 ASR 原样")
        return None
    return accepted, line_map


def _llm_punctuate_texts(texts: list[str], entry) -> list[str | None]:
    """阶段 2（标点）：逐条补标点，**逐条接受**；失败条 → None（原文保留）。

    调用失败/结构不对 → 全 None（上层用原文 + 兜底补句号）。
    """
    from slirn_home.revision_service import _call_llm
    from slirn_home.short_video_analyze import _strip_json_block

    out: list[str | None] = [None] * len(texts)
    if not texts:
        return out
    payload = "\n".join(f"第{k}条 {t}" for k, t in enumerate(texts, start=1))
    user = f"字幕条（条号 文字）：\n{payload}\n\n请补标点输出 JSON。"
    try:
        raw = _call_llm(_PUNCT_SYSTEM_PROMPT, user, entry=entry, retries=1,
                        max_tokens=8192, temperature=0.2)
        data = _strip_json_block(raw)
    except Exception as e:  # noqa: BLE001 — 标点是增强项，失败降级
        log.warning("[subs] 标点整理调用失败（原文保留+兜底句号）: %s", e)
        return out
    got = data.get("texts") if isinstance(data, dict) else None
    if not isinstance(got, list) or len(got) != len(texts):
        log.info("[subs] 标点输出条数不符（%s vs %s）→ 全部原文保留",
                 len(got) if isinstance(got, list) else "?", len(texts))
        return out
    n_ok = 0
    for k, (src, new) in enumerate(zip(texts, got)):
        new = str(new or "")
        if new and _strip_for_cmp(new) == _strip_for_cmp(src):
            out[k] = new
            n_ok += 1
        else:
            log.info("[subs] 第%s条标点校验不过（改字/丢字）→ 原文保留", k + 1)
    log.info("[subs] 标点整理接受 %s/%s 条", n_ok, len(texts))
    return out


def _llm_refine_cues(cues: list[dict], entry) -> list[dict] | None:
    """LLM 断句+标点（两阶段）：断句（阶段1 逐行接受）→ 标点（阶段2 逐条
    接受 + 程序兜底）→ 按起点重编号。全被拒 → None（调用方回退 ASR 原样）。

    时间：行内拆分按去标点字数比例切；跨行合并 = 首段起 → 末段止；
    单调性兜底（起点不早于上一条）。文本：
    - 阶段2 接受条 → 标点版（句末无终止标点再程序补「。」）；
    - 阶段2 拒绝的分组条 → verbatim + 程序补「。」；
    - 阶段2 拒绝的回退行 → ASR 原样（不动）。
    """
    grouped = _llm_group_cues(cues, entry)
    if grouped is None:
        return None
    accepted, line_map = grouped

    # 组装条目（时间 + verbatim 文字 + 是否整理分组），按起点排序
    entries: list[dict] = []
    for built, _t in accepted:
        segs = []
        for (li, t, ts, o0, o1) in built:
            lm = line_map[li]
            dur = max(0, lm["end_ms"] - lm["start_ms"])
            n = max(1, len(lm["stripped"]))
            segs.append((
                lm["start_ms"] + int(round(o0 / n * dur)),
                lm["start_ms"] + int(round(o1 / n * dur)),
                t,
            ))
        entries.append({
            "start_ms": segs[0][0], "end_ms": segs[-1][1],
            "raw": _join_parts([t for (_, _, t) in segs]),
            "is_group": True,
        })
    for li, lm in line_map.items():
        if not lm["ok"]:
            entries.append({
                "start_ms": lm["start_ms"], "end_ms": lm["end_ms"],
                "raw": lm["text"], "is_group": False,
            })
    entries.sort(key=lambda e: e["start_ms"])

    # 阶段 2：标点（全部条目一起送，逐条接受）
    punct = _llm_punctuate_texts([e["raw"] for e in entries], entry)
    result: list[dict] = []
    prev_start = -1
    for k, ent in enumerate(entries):
        start_ms = max(ent["start_ms"], prev_start)
        end_ms = max(ent["end_ms"], start_ms + 1)
        prev_start = start_ms
        p = punct[k] if k < len(punct) else None
        if p is not None:
            text = _ensure_terminal_punct(p)
        elif ent["is_group"]:
            text = _ensure_terminal_punct(ent["raw"])
        else:
            text = ent["raw"]  # 回退行保持 ASR 原样（原文基线语义）
        result.append({"i": k + 1, "start_ms": start_ms, "end_ms": end_ms,
                       "text": text})
    return result


# ---------- REQ-20261019-230：字幕条时长护栏（目标 2~3s / 硬上限 4s） ----------

_SPLIT_TARGET_MS = 3000   # 目标：每条 2~3 秒
_SPLIT_AIM_MS = 3400      # 贪心选切点的软上限（目标 + 缓冲，最后残段也 ≤ 此值）
_SPLIT_HARD_MS = 4000     # 硬上限：切完不允许有条超过 4 秒
_SPLIT_MIN_MS = 900       # 碎片护栏：预估短于此的段尽量不独立成条
# 在这些标点**之后**切（片段自然以停顿标点收尾）；终止标点在列 = 句界
_SPLIT_AFTER = "，、；：,;:。？！…?!"
# REQ-20261019-231（修订）：行尾**任何**标点都剥——不只句号类，拆条补的
# 逗号、顿/分号、引号括号书名号等一并；字符类与 _STRIP_RE 同源，锚定行尾。
_TRAILING_PUNCT_RE = re.compile(_STRIP_RE.pattern[:-1] + "+$")


def _strip_cue_terminal_punct(cues: list[dict]) -> int:
    """REQ-20261019-231（修订）：去掉每条字幕**结尾**的所有标点。

    用户语义：一行字幕中间可以有逗号，但结尾**不要有任何标点符号**
    （烧录/展示更干净）。只剥行尾（含尾随空白，可连续多个），剥到
    空串则原样保留；不动行中标点，不动任何文字字符。
    返回发生变更的条数。
    """
    changed = 0
    for c in cues:
        t = str(c.get("text") or "")
        t2 = _TRAILING_PUNCT_RE.sub("", t)
        if t2 and t2 != t:
            c["text"] = t2
            changed += 1
    return changed


def _split_long_cues(cues: list[dict]) -> list[dict]:
    """REQ-20261019-230：超长字幕条在停顿标点处二切。

    用户语义：一条字幕屏幕一行放不下 → 每条控制在 2~3 秒、**不超过 4 秒**，
    长句在适合断句的地方（逗/顿/分号 = LLM 标点版给出的语义停顿）拆开。
    纯程序后置，整理档与回退原行统一生效：

    - 切点优先级：≤AIM 的最远标点切点 → ≤HARD 且剩余 ≥MIN 的最远标点切点
      → 按去标点字数比例硬切（无标点文本的唯一选择）；
    - 只动断条与时间，不动任何文字字符；硬切产生的非末条尾部无标点时
      补一个「，」（纯标点变更，同 REQ-228 兜底句号口径）；
    - 段内时间按去标点字数比例分摊、逐段衔接，末段终点 = 原条终点。
    """
    out: list[dict] = []
    for c in cues:
        dur = int(c["end_ms"]) - int(c["start_ms"])
        if dur <= _SPLIT_TARGET_MS or not str(c.get("text") or "").strip():
            out.append(dict(c))
            continue
        out.extend(_split_one_cue(c))
    for pos, c in enumerate(out, start=1):
        c["i"] = pos
    return out


def _split_one_cue(cue: dict) -> list[dict]:
    text = str(cue["text"])
    start, end = int(cue["start_ms"]), int(cue["end_ms"])
    total = end - start
    n = len(text)
    stripped_total = len(_strip_for_cmp(text)) or 1

    def est(a: int, b: int) -> float:
        """段 [a,b) 的预估时长（按去标点字数占比）。"""
        return total * len(_strip_for_cmp(text[a:b])) / stripped_total

    cuts = [i + 1 for i, ch in enumerate(text[:-1]) if ch in _SPLIT_AFTER]
    spans: list[tuple[int, int]] = []
    a = 0
    while a < n:
        if est(a, n) <= _SPLIT_AIM_MS:
            spans.append((a, n))  # 剩余不超软上限 → 收尾
            break
        best = None
        for cut in cuts:
            if cut <= a or est(a, cut) > _SPLIT_AIM_MS or est(a, cut) < _SPLIT_MIN_MS:
                continue
            if best is None or cut > best:
                best = cut
        if best is None:
            for cut in cuts:
                if (cut <= a or est(a, cut) > _SPLIT_HARD_MS
                        or est(a, cut) < _SPLIT_MIN_MS or est(cut, n) < _SPLIT_MIN_MS):
                    continue
                if best is None or cut > best:
                    best = cut
        if best is None:
            if n - a <= 1 or est(a, n) <= _SPLIT_HARD_MS:
                spans.append((a, n))  # 切不动（单字符/必出碎片）→ 保整条
                break
            # 无可用标点切点且剩余超硬上限 → 比例硬切（硬上限优先于碎片护栏，
            # 宁可出现较短碎片也不允许 >4s 的条）
            frac = min(0.8, max(0.2, _SPLIT_TARGET_MS / max(est(a, n), 1.0)))
            best = a + max(1, int(round((n - a) * frac)))
            best = max(a + 1, min(n - 1, best))
            spans.append((a, best))
            a = best
            continue
        spans.append((a, best))
        a = best

    # 碎片护栏：非末段过短且并后不超硬上限 → 并入后段
    k = 0
    while k < len(spans) - 1:
        a1, b1 = spans[k]
        a2, b2 = spans[k + 1]
        if est(a1, b1) < _SPLIT_MIN_MS and est(a1, b2) <= _SPLIT_HARD_MS:
            spans[k:k + 2] = [(a1, b2)]
            k = max(0, k - 1)
        else:
            k += 1
    if len(spans) > 1 and est(*spans[-1]) < _SPLIT_MIN_MS \
            and est(spans[-2][0], spans[-1][1]) <= _SPLIT_HARD_MS:
        spans[-2:] = [(spans[-2][0], spans[-1][1])]

    pieces: list[dict] = []
    t = start
    cum = 0
    for k, (sa, sb) in enumerate(spans):
        cum += len(_strip_for_cmp(text[sa:sb]))
        last = k == len(spans) - 1
        e = end if last else max(start + int(round(total * cum / stripped_total)), t + 1)
        seg = text[sa:sb]
        if not last and seg and seg[-1] not in _SPLIT_AFTER:
            seg += "，"  # 硬切断条补停顿标点（纯标点变更）
        pieces.append({"start_ms": t, "end_ms": e, "text": seg})
        t = e
    return pieces


# ---------- 对外查询 / 编辑 ----------

def _ensure_base(root: Path, task_id: str, project_id: str, idx: int) -> Path | None:
    """编辑基线路径（REQ-227 存量自愈：无 base 有 asr → 拷一份当 base）。"""
    base = svc.stage5_subs_base_srt_path(root, task_id, project_id, idx)
    if base.is_file():
        return base
    asr = svc.stage5_subs_asr_srt_path(root, task_id, project_id, idx)
    if asr.is_file():
        shutil.copyfile(asr, base)
        log.info("[subs] #%s 存量无 base（REQ-227 数据），asr 拷贝自愈为 base", idx)
        return base
    return None


def subs_cues(repo_root: Path | str, task_id: str, project_id: str, clip_index: int) -> list[dict]:
    """编辑器数据：cue 列表（orig=原文，text=当前生效文字，modified=是否改过）。"""
    root = Path(repo_root)
    idx = int(clip_index or 0)
    base = _ensure_base(root, task_id, project_id, idx)
    if base is None:
        return []
    edits = _load_edits(svc.stage5_subs_edits_path(root, task_id, project_id, idx))
    out: list[dict] = []
    for c in _load_cues(base):
        cur = edits.get(str(c["i"]), c["text"])
        out.append({
            "i": c["i"], "start_ms": c["start_ms"], "end_ms": c["end_ms"],
            "orig": c["text"], "text": cur, "modified": cur != c["text"],
        })
    return out


def save_subs_edit(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    clip_index: int,
    cue_index: int,
    text: str,
) -> dict:
    """保存一条改字（text == 原文 = 还原：edits 删 key）并重新物化消费文件。"""
    root = Path(repo_root)
    idx = int(clip_index or 0)
    base_path = _ensure_base(root, task_id, project_id, idx)
    cues = _load_cues(base_path) if base_path else []
    if not cues:
        raise svc.ShortVideoError("字幕尚未提取（base 版不存在）")
    ci = int(cue_index or 0)
    if not 1 <= ci <= len(cues):
        raise svc.ShortVideoError(f"字幕行 #{ci} 不存在")
    orig = cues[ci - 1]["text"]

    edits_path = svc.stage5_subs_edits_path(root, task_id, project_id, idx)
    edits = _load_edits(edits_path)
    new_text = str(text or "")
    if new_text.strip() == orig.strip():
        edits.pop(str(ci), None)
        action = "restored"
    else:
        edits[str(ci)] = new_text
        action = "edited"
    edits_path.parent.mkdir(parents=True, exist_ok=True)
    edits_path.write_text(
        json.dumps(edits, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    subs_path = svc.stage5_subs_srt_path(root, task_id, project_id, idx)
    count = _materialize_subs_srt(base_path, subs_path, edits)
    modified = sum(1 for c in cues if edits.get(str(c["i"]), c["text"]) != c["text"])
    return {"action": action, "cue_index": ci, "orig": orig, "text": new_text,
            "cues": count, "modified": modified}


def invalidate_subs(repo_root: Path | str, task_id: str, project_id: str, clip_index: int) -> list[str]:
    """删该条四件套（粗剪重合成/删除成品时调；文件没了 = 回到未提取态）。"""
    root = Path(repo_root)
    idx = int(clip_index or 0)
    removed: list[str] = []
    for p in (
        svc.stage5_subs_srt_path(root, task_id, project_id, idx),
        svc.stage5_subs_base_srt_path(root, task_id, project_id, idx),
        svc.stage5_subs_asr_srt_path(root, task_id, project_id, idx),
        svc.stage5_subs_edits_path(root, task_id, project_id, idx),
    ):
        if p.is_file():
            try:
                p.unlink()
                removed.append(svc._rel(root, p))
            except OSError as e:
                log.warning("[subs] 作废删除失败 %s: %s", p, e)
    return removed


# ---------- 提取（ASR on coarse mp4 + LLM 整理） ----------

def run_subs_extract(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    clip_index: int,
    *,
    llm_entry: dict | None = None,
) -> dict:
    """粗剪成片跑 funasr → LLM 断句标点（REQ-228）→ 冻结 base + 物化消费文件。

    同步执行（ASR + LLM 耗时）；异步入口见 ``start_subs_extract_async``。
    LLM 未配置/失败 → base = ASR 原样（整理降级，不阻断提取）。
    """
    from slirn_home.short_video_extract import _diagnose_extract_error, _run_funasr_extract

    root = Path(repo_root)
    idx = int(clip_index or 0)
    project = svc.load_project(root, task_id, project_id)
    state = svc.get_stage_state(project, "stage5_coarse")
    hl = next(
        (h for h in (state.get("highlights") or [])
         if int(h.get("index") or 0) == idx),
        None,
    )
    if hl is None:
        raise svc.ShortVideoError(f"片段 #{idx} 没有粗剪记录，请先粗剪合成")
    rel = str(hl.get("coarse_mp4") or "")
    if not rel:
        raise svc.ShortVideoError(f"片段 #{idx} 没有粗剪成片，无法提取字幕")
    mp4 = (root / rel).resolve()
    if not mp4.is_file():
        raise svc.ShortVideoError("粗剪成片文件不存在，请重新合成后再提取")

    asr_path = svc.stage5_subs_asr_srt_path(root, task_id, project_id, idx)
    # 参数沿用 Stage 2 落盘配置（模型/语言/热词），与整片提取保持一致口径
    st2 = svc.get_stage_state(project, "stage2_extract") or {}
    _set_job(task_id, project_id, idx, stage="ASR 识别中", progress=10.0)
    result = _run_funasr_extract(
        input_video=mp4, output_srt=asr_path, repo_root=root,
        model=str(st2.get("model") or "paraformer"),
        lang=str(st2.get("lang") or "zh"),
        hotwords=str(st2.get("hotwords") or ""),
        sd_switch="no",
    )
    # 上游约定会在输出目录留 state.json（raw 快照）——stage5 目录共用的，
    # 提完顺手清掉避免与 preview_subs.srt 等混放
    state_json = asr_path.parent / "state.json"
    if state_json.is_file():
        try:
            state_json.unlink()
        except OSError:
            pass
    if result.returncode != 0 or not asr_path.is_file() or asr_path.stat().st_size < 4:
        raise svc.ShortVideoError(
            f"funasr 退出码 {result.returncode}"
            + _diagnose_extract_error(result.returncode, result.stderr_tail)
            + (f"；stderr 尾部: {result.stderr_tail[-300:]}" if result.stderr_tail else "")
        )

    raw_cues = _load_cues(asr_path)
    if not raw_cues:
        raise svc.ShortVideoError("ASR 未识别出任何字幕（成片可能无语音）")

    # REQ-228：LLM 断句 + 标点（失败回退 ASR 原样，不阻断）
    refined: list[dict] | None = None
    if llm_entry is not None:
        _set_job(task_id, project_id, idx, stage="LLM 断句标点中", progress=70.0)
        try:
            refined = _llm_refine_cues(raw_cues, llm_entry)
        except Exception as e:  # noqa: BLE001 — 整理是增强项，失败可降级
            log.warning("[subs] #%s LLM 整理失败（回退 ASR 原样）: %s", idx, e)
            refined = None
        if refined is None:
            log.info("[subs] #%s LLM 整理未生效（未配置/校验不过），用 ASR 原样", idx)
    base_cues = refined if refined else raw_cues
    # REQ-20261019-230：超长条在停顿标点处二切（目标 2~3s / 硬上限 4s）。
    # 整理档的切点 = LLM 标点版里的逗/顿号（语义停顿）；回退原行无标点 →
    # 按字数比例硬切。整理档与回退档统一走这道护栏。
    n_before_split = len(base_cues)
    base_cues = _split_long_cues(base_cues)
    if len(base_cues) != n_before_split:
        log.info("[subs] #%s 时长护栏：%s → %s 条（目标 2~3s，上限 4s）",
                 idx, n_before_split, len(base_cues))
    # REQ-20261019-231（修订）：行尾任何标点剥除（行中标点保留）。放在时长
    # 护栏**之后** —— 拆条仍能把句号当切点，拆完再剥各条行尾。
    n_unpunct = _strip_cue_terminal_punct(base_cues)
    if n_unpunct:
        log.info("[subs] #%s 行尾标点：%s 条剥除行尾标点", idx, n_unpunct)

    base_path = svc.stage5_subs_base_srt_path(root, task_id, project_id, idx)
    # REQ-20261019-229：重新提取时清掉旧 edits —— cue 编号已全部变化，
    # 旧 {cue_index: 改字} 会错位套到新句子上，必须整体作废
    edits_path = svc.stage5_subs_edits_path(root, task_id, project_id, idx)
    edits_path.unlink(missing_ok=True)
    _write_srt_file(base_path, base_cues)
    subs_path = svc.stage5_subs_srt_path(root, task_id, project_id, idx)
    _write_srt_file(subs_path, base_cues)  # 初次提取无 edits

    hl["subs_srt"] = svc._rel(root, subs_path)
    hl["subs_refined"] = refined is not None
    svc.set_stage_state(project, "stage5_coarse", state)
    svc.save_project(root, project)
    log.info("[subs] #%s 提取完成 raw=%s cues → base=%s cues（refined=%s）",
             idx, len(raw_cues), len(base_cues), refined is not None)
    return {"cues": len(base_cues), "raw_cues": len(raw_cues),
            "refined": refined is not None,
            "subs_srt": hl["subs_srt"], "elapsed_sec": result.elapsed_sec}


def start_subs_extract_async(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    clip_index: int,
    *,
    llm_entry: dict | None = None,
) -> bool:
    """启动提取线程（仿 start_coarse_async）。已在跑 → False。"""
    root = Path(repo_root)
    idx = int(clip_index or 0)
    key = (str(task_id or ""), str(project_id or ""), idx)
    with _JOBS_LOCK:
        existing = _JOBS.get(key)
        if existing and existing.get("state") == "running":
            return False
        _JOBS[key] = {"state": "running", "progress": 5.0, "stage": "ASR 识别中",
                      "error": None, "started_at": time.time(), "finished_at": None}

    def _run():
        try:
            res = run_subs_extract(root, task_id, project_id, idx,
                                   llm_entry=llm_entry)
            _set_job(task_id, project_id, idx,
                     state="done", progress=100.0, stage="完成",
                     finished_at=time.time(), cues=res["cues"],
                     refined=bool(res.get("refined")))
        except Exception as e:  # noqa: BLE001 — 后台线程兜底
            log.exception("[subs][%s/%s#%s] 提取失败", task_id, project_id, idx)
            _set_job(task_id, project_id, idx,
                     state="failed", stage="失败",
                     error=str(e), finished_at=time.time())

    threading.Thread(target=_run, name=f"sv-subs-{task_id}-{idx}",
                     daemon=True).start()
    return True
