"""字幕修订阶段关联人员 ID — REQ-20260919-068.

参考切分修剪 cut_speaker.py（REQ-20260917-031/033）的对齐逻辑，但行集合
是 revision entries（每行一个 i + 一个 decision）而不是 cutlist items。

对齐规则：overlap = max(0, min(endA,endB) − max(startA,startB))，取重叠毫秒
最大且 >0 的 subtitle 段的 spk；并列取时间更早的段；无重叠 → 不标注。

持久化：link_speakers 的结果快照落盘 outputs/rev_speaker_link.json
（enabled 标记 + rows/stats 快照）。渲染时不直接用快照——enabled=true 时用
相同纯函数对当前数据现算（对齐确定性，修订变化不错位），快照仅供人工检查。
"""

from __future__ import annotations

import bisect
import json
import time
from pathlib import Path

LINK_FILENAME = "rev_speaker_link.json"


def overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    """两时间窗的重叠毫秒数（无重叠为 0）。"""
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _row_deleted(entry: dict) -> bool:
    """修订阶段：decision=delete 即视为删除（一行一决策，无组级/子段之分）。"""
    return str(entry.get("decision") or "") == "delete"


def link_speakers(subtitle_meta: dict, revision: dict) -> dict:
    """subtitle 段级 spk → revision entries 每行的人员编号 + 按人员统计未删除记录数。

    返回 {available, rows: {行i: spk}, stats: [{spk, count}]}。
    count 口径：decision != 'delete' 的行数（删除决策不计入；用于"确认非主讲
    人员已清除"）。stats 按 spk 升序；行 id 与修订面板一致（整数 1-based）。
    """
    segs: list[dict] = []
    for s in (subtitle_meta or {}).get("segments") or []:
        try:
            spk = int(s.get("spk") or 0)
        except (TypeError, ValueError):
            continue
        if spk < 1:
            continue  # 无人员编号（旧任务 / SD 关闭）的段不参与对齐
        segs.append({
            "spk": spk,
            "start_ms": int(s.get("start_ms", 0)),
            "end_ms": int(s.get("end_ms", 0)),
        })
    if not segs:
        return {"available": False, "rows": {}, "stats": []}

    # ---- 性能优化（进工作台慢的根因）：排序 + bisect + 双指针，替代
    # entries × segs 全量暴力交叉（长视频百万次 overlap_ms，实测 ~600ms；
    # 改后窗口内只扫真重叠的几段）。结果与暴力版逐位一致：segs 按 start
    # 升序扫，只在 ov 严格更大时替换 → 并列时保住「时间更早的段」。
    segs.sort(key=lambda s: s["start_ms"])
    seg_starts = [s["start_ms"] for s in segs]
    entries = list((revision or {}).get("entries") or [])

    def _entry_start(e: dict) -> int:
        try:
            return int(e.get("start_ms", 0))
        except (TypeError, ValueError):
            return 0

    order = sorted(range(len(entries)), key=lambda k: _entry_start(entries[k]))

    rows: dict[str, int] = {}
    counts: dict[int, int] = {}
    lo = 0
    for k in order:
        e = entries[k]
        try:
            i = int(e.get("i"))
            s_ms = int(e.get("start_ms", 0))
            e_ms = int(e.get("end_ms", 0))
        except (TypeError, ValueError):
            continue
        if not (e_ms > s_ms):
            continue
        # 窗口 [lo, hi)：start < e_ms（bisect）且 end > s_ms（lo 按行 start
        # 升序单调推进安全 — end <= 当前行 start 的段对未来行同样不可能重叠）
        hi = bisect.bisect_left(seg_starts, e_ms)
        while lo < hi and segs[lo]["end_ms"] <= s_ms:
            lo += 1
        best_ov, best_spk = 0, None
        for j in range(lo, hi):
            s = segs[j]
            if s["end_ms"] <= s_ms:
                continue
            ov = overlap_ms(s_ms, e_ms, s["start_ms"], s["end_ms"])
            if ov > best_ov:
                best_ov, best_spk = ov, s["spk"]
        if best_spk is None:
            continue
        rows[str(i)] = best_spk
        if best_spk not in counts:  # 有行即入表（全删光也显示 count=0 = 已清零反馈）
            counts[best_spk] = 0
        if not _row_deleted(e):  # 删除决策不计入统计
            counts[best_spk] += 1

    stats = [{"spk": spk, "count": c} for spk, c in sorted(counts.items())]
    return {"available": True, "rows": rows, "stats": stats}


def save_link(outputs_dir: Path, link: dict) -> dict:
    """关联结果落盘：rev_speaker_link.json = enabled 标记 + 结果快照。"""
    payload = {
        "version": 1,
        "enabled": True,
        "linked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": link.get("rows") or {},
        "stats": link.get("stats") or [],
    }
    (Path(outputs_dir) / LINK_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_link(outputs_dir: Path) -> dict | None:
    """读取关联状态；无文件/坏文件/未启用 → None（面板回普通态）。"""
    p = Path(outputs_dir) / LINK_FILENAME
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("enabled") else None