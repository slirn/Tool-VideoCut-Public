"""切分修剪清单服务 — REQ-20260916-008。

前一阶段（字幕修订）全部决策完成后，把最终决策翻译成切分修剪清单：
- keep（完整保留）/ fix（内容更正）→ 带入本阶段，继承原编号不变；
  fix 采用更正后的文本（keep_text）
- delete → 剔除，不带入本阶段
- split（切分修剪）→ 把原字幕内容与切分后文字逐 token 对齐（字级时间戳），
  从原段音频中切出目标文字对应的字幕时间段：父级编号保留，
  切出的每个连续时间段为一个子段 — 原编号 10 切出 3 段 → 10.1 / 10.2 / 10.3
  （用户原话：保留父级编号、对切分出来的每一段加一个子编号；
    从上一阶段继承过来的序号尽量保持不变，方便音频与文字对照查看）

产物 outputs/cutlist.json（服务端预览现算不落盘，「生成切分清单」时才落盘 +
推进 ROUGH_CUT_DONE），后续阶段以它为输入。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

CUTLIST_JSON = "cutlist.json"

# 切分条目类型 → (中文标签, 徽章色) — 徽章色沿用修订阶段（keep 绿 / split 紫 / fix 蓝）
CUT_KINDS: dict[str, tuple[str, str]] = {
    "keep": ("完整保留", "keep"),
    "fix": ("内容更正", "fix"),
    "split": ("切分子段", "split"),
}

# token 切分 — 与上游 funclip/utils/subtitle_utils.str2list 同款正则。
# FunASR 的字级 timestamp 正是与该口径的 token 一一对应
# （上游 generate_srt_clip 即按此假设切片对齐），口径必须一致才能对齐时间。
_TOKEN_RE = re.compile("[\\u4e00-\\u9fff]|[\\w-]+")


def tokenize(text: str) -> list[str]:
    """文本 → token 列表（中文单字、英文/数字连字）。"""
    return _TOKEN_RE.findall(str(text or ""))


def join_tokens(tokens: list[str]) -> str:
    """token 列表 → 显示文本（中文直接拼、英文词空格 — 与 Text2SRT.text() 一致）。"""
    res = ""
    for w in tokens:
        if 0x4E00 <= ord(w[:1]) <= 0x9FFF:  # 中文字符直接拼（Text2SRT 同口径）
            res += w
        else:
            res += " " + w
    return res.lstrip().rstrip(" 、。，")


def ms2srt(ms: int) -> str:
    """毫秒 → 'HH:MM:SS,mmm'（与上游 time_convert 输出一致，subtitle.json 同款）。"""
    ms = max(0, int(ms))
    tail = ms % 1000
    s = ms // 1000
    mi, s = divmod(s, 60)
    h, mi = divmod(mi, 60)
    return f"{h:02d}:{mi:02d}:{s:02d},{tail:03d}"


def align_tokens(orig: list[str], keep: list[str]) -> list[int]:
    """keep tokens 按序在 orig 中贪心匹配 → 命中的 orig 索引列表（最靠前优先）。

    修剪目标是原文子集（去掉语气词/重复），子序列匹配即可；对不上的
    token（用户改写过）跳过，保序不回溯。
    """
    marks: list[int] = []
    pos = 0
    for kt in keep:
        for j in range(pos, len(orig)):
            if orig[j] == kt:
                marks.append(j)
                pos = j + 1
                break
    return marks


def contiguous_blocks(marks: list[int]) -> list[tuple[int, int]]:
    """命中索引 → 连续块 [(起, 止), …]（每块 = 一个切分子段）。"""
    blocks: list[tuple[int, int]] = []
    for idx in marks:
        if blocks and idx == blocks[-1][1] + 1:
            blocks[-1] = (blocks[-1][0], idx)
        else:
            blocks.append((idx, idx))
    return blocks


# 字幕级改判（决策状态）合法值 — REQ-20260916-011 第二步
ACTIONS: tuple[str, ...] = ("keep", "delete", "split")


def partition_tokens(n_tokens: int, keep_marks: list[int]) -> list[tuple[str, int, int]]:
    """命中索引 → 完整交替划分 [(mark, 首 token 索引, 尾 token 索引), …] — REQ-20260916-011。

    覆盖 [0, n_tokens) 不重不漏：keep 块（命中连续块）与 delete 洞
    （块间/首/尾的间隙）交替；编号阶段直接按本列表顺序产 父.N
    （时间序编号不分标记 — 编号是位置，标记是决策，两者解耦）。
    """
    parts: list[tuple[str, int, int]] = []
    cur = 0
    for a, b in contiguous_blocks(keep_marks):
        if a > cur:
            parts.append(("delete", cur, a - 1))
        parts.append(("keep", a, b))
        cur = b + 1
    if cur < n_tokens:
        parts.append(("delete", cur, n_tokens - 1))
    return parts


def _final_kind(entry: dict) -> str | None:
    """条目最终生效类别：手动改判（keep/delete/split/fix）优先，accept=采纳模型建议。

    pending → None（本阶段入口要求修订完成，防御兜底）。
    """
    d = str(entry.get("decision") or "")
    if d in ("keep", "delete", "split", "fix"):
        return d
    if d == "accept":
        cat = str(entry.get("category") or "")
        return cat if cat in ("keep", "delete", "split", "fix") else None
    return None


def _split_target(entry: dict) -> str:
    """split 的切分后文字：手动填写的「切分修剪后内容」优先，回退模型建议 keep_text。"""
    return str(entry.get("user_note") or "").strip() or str(entry.get("keep_text") or "").strip()


def _fix_target(entry: dict) -> str:
    """fix 的更正后文字（REQ-20260917-026）：手动填写/微调的「更正后内容」
    （user_note，修订面板输入框预填模型更正、可直接改）优先，回退模型建议 keep_text。
    """
    return str(entry.get("user_note") or "").strip() or str(entry.get("keep_text") or "").strip()


def _segment_tokens(seg: dict) -> tuple[list[str], list[list[int]]] | None:
    """段的 (tokens, token_ts) — 仅当两列表非空且等长（字级时间戳可信）。"""
    tokens = seg.get("tokens")
    ts = seg.get("token_ts")
    if (not isinstance(tokens, list) or not isinstance(ts, list)
            or not tokens or len(tokens) != len(ts)):
        return None
    try:
        norm = [[int(t[0]), int(t[1])] for t in ts]
    except (TypeError, ValueError, IndexError):
        return None
    return [str(w) for w in tokens], norm


def _split_items(seg: dict, entry: dict, i: int, manual_marks: dict) -> tuple[list[dict], bool]:
    """split 单父段 → 完整子段列表 + 是否降级 — REQ-20260916-011 第二步。

    - 对齐成功 → partition_tokens 完整划分（keep 块 + delete 洞交替，
      时间序编号 父.N 不分标记）；delete 洞外扩边界：向前贴前一段终点、
      向后贴下一 keep 块首 token 起点（首尾洞用父段边界）——父段被
      keep/delete 完全二分，删掉洞后 keep 段自动首尾紧贴
    - 无字级时间戳 / 目标空 / 全对不上 → 整段单子段 fallback（mark=keep）
    - manual_marks「父.子」覆盖建议标记（mark_manual=True）
    """
    target = _split_target(entry)
    aligned = _segment_tokens(seg)
    keep_marks: list[int] = []
    if aligned and target:
        keep_marks = align_tokens(aligned[0], tokenize(target))
    if aligned and target and keep_marks:
        # 命中 0（文字全对不上）→ 不猜对齐，走 fallback
        orig_tokens, token_ts = aligned
        parts = partition_tokens(len(orig_tokens), keep_marks)
        seg_start_ms = int(seg.get("start_ms", 0))
        seg_end_ms = int(seg.get("end_ms", 0))
        items: list[dict] = []
        prev_e = seg_start_ms
        sub = 0
        for mark, a, b in parts:
            if mark == "keep":
                s_ms, e_ms = token_ts[a][0], token_ts[b][1]
            else:  # delete 洞外扩
                s_ms = prev_e
                e_ms = token_ts[b + 1][0] if b + 1 < len(token_ts) else seg_end_ms
            s_ms = max(s_ms, prev_e)  # ts 非单调防御
            if e_ms - s_ms <= 0:
                continue  # 零长洞（相邻 token 无缝）不产段
            sub += 1
            mark_manual = False
            mm = str(manual_marks.get(f"{i}.{sub}") or "")
            if mm in ("keep", "delete") and mm != mark:
                mark, mark_manual = mm, True
            items.append({
                "id": f"{i}.{sub}", "source_i": i, "kind": "split", "sub": sub,
                "mark": mark, "mark_manual": mark_manual,
                "start_ms": s_ms, "end_ms": e_ms,
                "start": ms2srt(s_ms), "end": ms2srt(e_ms),
                "text": join_tokens(orig_tokens[a:b + 1]),
                "orig_text": str(seg.get("text", "")), "target_text": target,
                "fallback": False,
            })
            prev_e = e_ms
        if items:
            return items, False
    # fallback：整段单子段（mark 建议 keep，可手工翻转为整段删）
    mark, mark_manual = "keep", False
    mm = str(manual_marks.get(f"{i}.1") or "")
    if mm in ("keep", "delete"):
        mark, mark_manual = mm, mm != "keep"
    return [{
        "id": f"{i}.1", "source_i": i, "kind": "split", "sub": 1,
        "mark": mark, "mark_manual": mark_manual,
        "start_ms": int(seg.get("start_ms", 0)), "end_ms": int(seg.get("end_ms", 0)),
        "start": str(seg.get("start", "")), "end": str(seg.get("end", "")),
        "text": target or str(seg.get("text", "")),
        "orig_text": str(seg.get("text", "")), "target_text": target or None,
        "fallback": True,
    }], True


def build_cutlist(subtitle_meta: dict, revision: dict,
                  manual_marks: dict | None = None, actions: dict | None = None) -> dict:
    """字幕生成 + 修订决策 → 切分修剪清单（纯函数，不落盘）。

    - keep/fix：整段带入，id=原编号（字符串），fix 文本=更正后
      （user_note 手动微调优先 → keep_text，REQ-20260917-026）
    - delete：剔除
    - split：完整时间轴划分（keep 块 + delete 洞，REQ-20260916-011），
      id=「父编号.子序号」时间序不分标记；子段 mark 双态建议（keep/delete）
    - manual_marks：子段级手工翻转 {"父.子": "keep"|"delete"}（覆盖建议）
    - actions：字幕级改判 {"<source_i>": keep|delete|split}，执行优先级
      action > 子段 mark > 原类别：
      * delete → 整条剔除（条目仍产出供渲染置灰，执行口径见 effective_keep_units）
      * keep（原 split）→ 不再切，整段保留
      * split（原 keep/fix）→ 按切分后文字划子段（内容缺失 → 防御维持原状）
    - keep_duration_ms 按**执行口径**计（effective_keep_units）
    - 继承编号不重排（删除留下的空洞保留，方便与修订阶段对照）
    """
    manual_marks = {str(k): v for k, v in (manual_marks or {}).items()}
    actions_n: dict[int, str] = {}
    for k, v in (actions or {}).items():
        if str(v) in ACTIONS:
            try:
                actions_n[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
    seg_by_i = {int(s["i"]): s for s in (subtitle_meta or {}).get("segments") or []}
    items: list[dict] = []
    stats = {"kept": 0, "fixed": 0, "split_parents": 0, "split_subs": 0,
             "split_subs_delete": 0, "mark_flipped": 0, "dropped": 0, "fallback": 0,
             "action_changed": 0, "keep_duration_ms": 0}
    for e in (revision or {}).get("entries") or []:
        i = int(e.get("i", 0))
        seg = seg_by_i.get(i)
        if seg is None:
            continue
        kind = _final_kind(e)
        if kind == "delete":
            stats["dropped"] += 1
            continue
        if kind is None:  # pending / 采纳 review 等 — 防御：按整段保留带入
            kind = "keep"
        action = actions_n.get(i)
        if action == "keep" and kind == "split":
            # 改判保留：原 split 整段保留，不再切（子段划分作废）
            items.append({
                "id": str(i), "source_i": i, "kind": "keep", "sub": None,
                "start_ms": int(seg.get("start_ms", 0)), "end_ms": int(seg.get("end_ms", 0)),
                "start": str(seg.get("start", "")), "end": str(seg.get("end", "")),
                "text": str(seg.get("text", "")),
                "orig_text": str(seg.get("text", "")),
                "target_text": None, "fallback": False,
            })
            stats["kept"] += 1
            continue
        if kind == "split" or (action == "split" and _split_target(e)):
            sub_items, fb = _split_items(seg, e, i, manual_marks)
            items.extend(sub_items)
            stats["split_parents"] += 1
            stats["split_subs"] += len(sub_items)
            stats["split_subs_delete"] += sum(1 for s in sub_items if s["mark"] == "delete")
            stats["mark_flipped"] += sum(1 for s in sub_items if s.get("mark_manual"))
            stats["fallback"] += int(fb)
            continue
        if kind in ("keep", "fix"):
            # fix 文本：输入框微调值（user_note）优先 → 模型更正（keep_text）→ 原文兜底
            text = _fix_target(e) if kind == "fix" else str(seg.get("text", ""))
            if kind == "fix" and not text:
                text = str(seg.get("text", ""))  # 更正文本缺失 → 原文兜底
            items.append({
                "id": str(i), "source_i": i, "kind": kind, "sub": None,
                "start_ms": int(seg.get("start_ms", 0)), "end_ms": int(seg.get("end_ms", 0)),
                "start": str(seg.get("start", "")), "end": str(seg.get("end", "")),
                "text": text, "orig_text": str(seg.get("text", "")),
                "target_text": None, "fallback": False,
            })
            stats["kept" if kind == "keep" else "fixed"] += 1
            continue

    stats["brought"] = len(items)
    stats["action_changed"] = len(actions_n)
    out = {
        "version": 2,
        "created_at": (revision or {}).get("saved_at") or (revision or {}).get("created_at") or "",
        "entries_count": len((revision or {}).get("entries") or []),
        "stats": stats,
        "items": items,
    }
    if manual_marks:
        out["manual_marks"] = manual_marks
    if actions_n:
        out["actions"] = {str(k): v for k, v in actions_n.items()}
    # 保留时长按执行口径（action=delete 整条剔除、split mark=delete 子段剔除）
    stats["keep_duration_ms"] = sum(
        max(0, int(u.get("end_ms", 0)) - int(u.get("start_ms", 0)))
        for u in effective_keep_units(out)
    )
    return out


def effective_keep_units(cutlist: dict) -> list[dict]:
    """执行口径：action > 子段 mark > 原类别 → 最终保留单元列表。

    下游（精剪字幕/成片）与预计保留时长共用此函数：
    - actions[source_i]=delete → 整条剔除（渲染置灰，但执行不参与）
    - split 子段 mark=delete → 剔除；mark=keep 保留
    - keep/fix 整段与改判 keep 整段保留（后者已在 build 时归一为整段条目）
    """
    actions: dict[int, str] = {}
    for k, v in (cutlist.get("actions") or {}).items():
        try:
            v = str(v)
            if v in ACTIONS:
                actions[int(k)] = v
        except (TypeError, ValueError):
            continue
    units: list[dict] = []
    for it in cutlist.get("items") or []:
        try:
            si = int(it.get("source_i", 0))
        except (TypeError, ValueError):
            continue
        if actions.get(si) == "delete":
            continue
        if it.get("kind") == "split" and str(it.get("mark") or "keep") == "delete":
            continue
        units.append(it)
    return units


def resplit_segment(subtitle_meta: dict, revision: dict, source_i: int,
                    target_text: str, saved: dict | None = None) -> tuple[dict | None, dict | None, str]:
    """单段重新切分（REQ-20260916-011 第三步）— 纯函数，落盘由端点负责。

    - 回写修订 entries 的 user_note（单一事实源，_split_target 口径不漂移）
      并置 decision=split（手动改判语义）
    - 清该父段的字幕级改判（决策已落地修订，不双写）与子段翻转
      （划分变了旧 mark 不再对应）；其它父段的 manual_marks / actions 原样保留
    - 命中 0 / 无字级时间戳 / 目标空 → 宁可不切不可错切，返回错误、不改动

    返回 (cutlist, revision, err)：err 非空则前两者为 None。
    """
    target = str(target_text or "").strip()
    if not target:
        return None, None, "切分后内容不能为空"
    try:
        si = int(source_i)
    except (TypeError, ValueError):
        return None, None, "source_i 不合法"
    seg = next((s for s in (subtitle_meta or {}).get("segments") or []
                if int(s.get("i", -1)) == si), None)
    entry = next((e for e in (revision or {}).get("entries") or []
                  if int(e.get("i", -1)) == si), None)
    if seg is None or entry is None:
        return None, None, f"未找到第 {si} 条字幕"
    aligned = _segment_tokens(seg)
    if not aligned:
        return None, None, ("该段缺少字级时间戳（旧格式字幕），无法精确重切 — "
                            "请先在「字幕生成」阶段重新生成字幕")
    if not align_tokens(aligned[0], tokenize(target)):
        return None, None, "切分后内容与原段文字对不上（命中 0 字）— 请检查内容后重试"
    entry["decision"] = "split"
    entry["user_note"] = target[:500]
    prefix = f"{si}."
    mm = {k: v for k, v in ((saved or {}).get("manual_marks") or {}).items()
          if not str(k).startswith(prefix)}
    acts = {k: v for k, v in ((saved or {}).get("actions") or {}).items()
            if str(k) != str(si)}
    return build_cutlist(subtitle_meta, revision, manual_marks=mm, actions=acts), revision, ""


def save_cutlist(outputs_dir: Path, cutlist: dict) -> Path:
    """清单落盘 outputs/cutlist.json。"""
    import time

    p = Path(outputs_dir) / CUTLIST_JSON
    cutlist = dict(cutlist)
    cutlist["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cutlist, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_cutlist(outputs_dir: Path) -> dict | None:
    """读取 cutlist.json（无/损坏 → None）。"""
    p = Path(outputs_dir) / CUTLIST_JSON
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("cutlist.json 损坏: %s", e)
        return None
