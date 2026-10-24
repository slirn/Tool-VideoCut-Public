"""REQ-20261013-100：Stage 4「内容优化」（替换原 Stage 3.5 字幕一致性核验）。

把 Stage 3 生成的全部短视频（标题 + 字幕行）逐条交给大模型。
REQ-20261019-219：优化手段收紧为**删除式**——不允许增加音频文字之外的任何
文字（不改写 / 不扩写 / 不合并造句），只有两种决策：
- **行内删词**（trim，即「重新切分」）：新文字 = 原文去掉无用词，服务端
  逐 token 校验「有序子序列」对不上即保留原行（宁可不改不可改错）；命中
  则 token 对齐算出视频保留段 keep_segments（带时间戳/时间段，预览/粗剪
  把洞切掉），同 REQ-20261016-147 手动 ✂️ 口径。
- **整行删除**：输出中缺席的编号即删除。
（旧 fix 改写 / split 拆行 / LLM 段内重排已下线；重排保留为用户手动功能。）
结果写 Stage 4 自己的 ``stage4_optimize/optimized_highlights.json``（REQ-20261017-149
分离存储 — Stage 3 的 ``stage3/highlights.json`` 原表不再被改写，两阶段各一套
字幕）；粗剪及之后作废。老项目（优化结果曾写回 highlights.json）在下次优化时
自动还原原表。

LLM 客户端沿用 ``revision_service._call_llm``（与 Stage 3 解析同款）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from slirn_home import cutlist_service as _cl  # REQ-20261016-147：长视频 token 对齐算法
from slirn_home import revision_service
from slirn_home import short_video_service as svc

log = logging.getLogger(__name__)

WARNINGS_CAP = 20

# REQ-20261013-108：使用 short_video_service.STAGE4_SYSTEM_PROMPT 作为系统提示词
# 旧版 _OPTIMIZE_SYSTEM_PROMPT 保留为别名（向后兼容 — 任何外部导入都不破）。
_OPTIMIZE_SYSTEM_PROMPT = svc.STAGE4_SYSTEM_PROMPT

_OPTIMIZE_USER_TMPL = """短视频标题：{title}

字幕行：
{lines}

对上面每行给出决策（keep/trim 之一，或省略=删除整行），输出：
{{"lines": [{{"no": 1, "op": "keep"}}, {{"no": 2, "op": "trim", "text": "删掉无用词后的整行文字"}}]}}
trim 的 text 必须是原行文字去掉若干字词后的结果（不新增任何文字）。
未出现在 lines 里的编号 = 删除该行。只输出 JSON。"""


def _strip_json_block(raw: str) -> str:
    """宽容提取 LLM 输出里的 JSON 对象（剥 ```json 围栏与前后杂文）。"""
    text = str(raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        return fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _norm_text(s: str) -> str:
    """切分校验用：去所有空白与常见标点差异口径（逐字比对）。"""
    return re.sub(r"\s+", "", str(s or ""))


def _is_deletion_only(orig: str, target: str) -> bool:
    """REQ-20261019-219：target 是否为 orig 的**纯删除**结果。

    逐 token 有序子序列匹配（cutlist_service.align_tokens 贪心保序）：
    target 的每个 token 都必须在 orig 中按序命中——任何一个 token 对不上
    = LLM 新增/改写了文字 → False（不允许，保留原行）。align_tokens 对
    对不上的 token 会静默跳过，所以必须比对命中数 == target token 数。
    """
    orig_tokens = _cl.tokenize(orig)
    target_tokens = _cl.tokenize(target)
    if not target_tokens:
        return False
    hit = _cl.align_tokens(orig_tokens, target_tokens)
    return len(hit) == len(target_tokens)


def _parse_optimize_result(raw: str, src_lines: list[dict]) -> tuple[dict, list[str]]:
    """解析 LLM JSON → {no: op}；op 为 "keep" / {"op": "trim", "text"}。

    REQ-20261019-219 删除式优化校验（宁可不改不可改错）：
    - trim（及旧习惯输出的 fix）文字必须是原文的**纯删除**结果（有序子序列，
      逐 token 校验）——新增/改写任何文字 → 降级 keep + 告警；
    - trim 文字为空 / 与原文相同 → 降级 keep（空告警，相同静默）；
    - split（拆行）已随删除式优化下线 → 降级 keep + 告警；
    - 未知 no / 非法 op → 忽略 + 告警。
    """
    warnings: list[str] = []
    ops: dict[int, dict] = {}
    by_no = {int(sl.get("no") or 0): sl for sl in src_lines}
    try:
        data = json.loads(_strip_json_block(raw))
    except Exception as e:  # noqa: BLE001
        raise svc.ShortVideoError(f"内容优化输出不是合法 JSON: {e}") from e
    lines = (data or {}).get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list):
        raise svc.ShortVideoError("内容优化输出缺少 lines 数组")

    for item in lines:
        if not isinstance(item, dict):
            continue
        try:
            no = int(item.get("no") or 0)
        except (TypeError, ValueError):
            continue
        src = by_no.get(no)
        if src is None:
            warnings.append(f"未知行号 {no} 被忽略")
            continue
        op = str(item.get("op") or "").strip().lower()
        src_text = str(src.get("text") or "")
        if op == "keep":
            ops[no] = {"op": "keep"}
        elif op in ("trim", "fix"):
            # fix 是旧提示词的习惯输出 — 一律按删除式 trim 校验
            #（REQ-20261019-219：不允许新增文字，改写型 fix 直接拒绝）
            text = str(item.get("text") or "").strip()
            if not text:
                warnings.append(f"#{no} {op} 文字为空，保留原行")
                ops[no] = {"op": "keep"}
            elif _norm_text(text) == _norm_text(src_text):
                ops[no] = {"op": "keep"}  # 删了个寂寞 = 未修改，静默按 keep
            elif not _is_deletion_only(src_text, text):
                warnings.append(
                    f"#{no} {op} 不是纯删除（新增/改写了文字），保留原行"
                    "（优化只允许删除，不允许增加音频文字之外的文字）")
                ops[no] = {"op": "keep"}
            else:
                ops[no] = {"op": "trim", "text": text}
        elif op == "split":
            warnings.append(f"#{no} split 已下线（优化 = 删除式），保留原行")
            ops[no] = {"op": "keep"}
        else:
            # REQ-20261013-110：兼容 LLM 误用 `op: "delete"`（prompt 明确说 delete = 省略，
            # 但部分 LLM 仍会输出 "delete"）。静默忽略，不警告（避免噪音）。
            if op == "delete":
                continue
            warnings.append(f"#{no} 未知 op「{op}」，保留原行")
            ops[no] = {"op": "keep"}
    return ops, warnings


def _hl_window(hl: dict) -> None:
    """按当前行重算组窗口 start/end_ms/total_ms（与解析器同口径）。"""
    times = [(int(sl.get("start_ms") or 0), int(sl.get("end_ms") or 0))
             for sl in hl.get("subtitle_lines") or []]
    if not times:
        return
    hl["start_ms"] = min(t[0] for t in times)
    hl["end_ms"] = max(t[1] for t in times)
    hl["total_ms"] = sum(max(0, t[1] - t[0]) for t in times)


def _optimize_user_payload(title: str, lines: list[dict]) -> str:
    rows = "\n".join(
        f"{int(sl.get('no') or i + 1)}. {sl.get('text') or ''}"
        for i, sl in enumerate(lines)
    )
    return _OPTIMIZE_USER_TMPL.format(title=title or "（无标题）", lines=rows)


# ---------- REQ-20261017-149：Stage 4 分离存储 ----------

def stage4_optimized_doc_path(repo_root: Path | str, task_id: str,
                              project_id: str) -> Path:
    """Stage 4 自己的优化结果表（与 stage3/highlights.json 分离）。"""
    return (svc.stage4_optimize_dir(repo_root, task_id, project_id)
            / "optimized_highlights.json")


def load_stage4_base_doc(repo_root: Path | str, task_id: str, project_id: str,
                         *, project: dict | None = None) -> dict | None:
    """Stage 4 的底稿（REQ-20261017-149 分离存储，Stage 3/4 字幕各一套）。

    - stage4_optimize 已 done 且 ``optimized_highlights.json`` 存在 → 返回该表
      （Stage 4 优化后的独立字幕集）；
    - 否则回退 ``stage3/highlights.json``（优化前预览 / 老项目 / 解析后重置）。

    返回 None = 两处都没有（Stage 3 未跑）。按 status 门控（而非纯文件存在），
    Stage 3 重新解析把 stage4 重置成 pending 后，残留的旧优化表自动失效。
    """
    root = Path(repo_root)
    try:
        proj = project if project is not None else svc.load_project(root, task_id, project_id)
    except Exception:  # noqa: BLE001
        proj = {}
    st = ((proj.get("pipeline") or {}).get("stage4_optimize") or {}).get("status")
    if st == "done":
        p = stage4_optimized_doc_path(root, task_id, project_id)
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass  # 坏文件落回 stage3 原表
    hp = svc.stage_highlights_path(root, task_id, project_id)
    if not hp.is_file():
        return None
    try:
        return json.loads(hp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def run_stage4_optimize(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    *,
    entry: dict | None = None,
    hl_index: int | None = None,
) -> dict:
    """Stage 4「内容优化」公开入口（REQ-20261022-144：带执行日志的包装层）。"""
    exec_id = ""
    try:
        from slirn_home import execution_history
        exec_id = execution_history.record_start(
            svc.sv_logs_dir(repo_root, task_id),
            execution_history.KIND_SHORT_VIDEO_OPTIMIZE,
            extra={"project_id": project_id, "hl_index": hl_index},
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        state = _run_stage4_optimize_inner(
            repo_root, task_id, project_id, entry=entry, hl_index=hl_index)
    except Exception as e:  # noqa: BLE001
        try:
            from slirn_home import execution_history
            execution_history.record_finish(
                svc.sv_logs_dir(repo_root, task_id), exec_id,
                success=False, error=str(e))
        except Exception:  # noqa: BLE001
            pass
        raise
    try:
        from slirn_home import execution_history
        execution_history.record_finish(
            svc.sv_logs_dir(repo_root, task_id), exec_id,
            success=(state or {}).get("status") == "done",
            error=str((state or {}).get("error") or ""),
        )
    except Exception:  # noqa: BLE001
        pass
    return state


def _run_stage4_optimize_inner(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    *,
    entry: dict | None = None,
    hl_index: int | None = None,
) -> dict:
    """Stage 4「内容优化」：LLM 逐条优化 Stage 3 字幕，结果写 Stage 4 自己的
    optimized_highlights.json（REQ-20261017-149 分离存储，不写回 highlights.json）。

    REQ-20261016-146：不再接受自定义提示词（只用内置 STAGE4_SYSTEM_PROMPT）；
    LLM 删除的行记录到 state.highlights[].deleted_lines 供 UI 保留展示。

    REQ-20261017-155：``hl_index`` 可选 — **单条重新优化**（None/0 = 全量，
    向后兼容）。只对 index==hl_index 的那条从 Stage 3 原表重新跑 LLM；其余条
    保留 optimized_highlights.json 里的既有结果（分离表不存在时维持原表内容）。
    state.highlights 统计按 index 合并（不清空其余条）。

    Args:
        entry: llm_config 注册项；None 时用当前默认模型。
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)

    # REQ-20261017-149 老项目迁移：优化结果曾写回 highlights.json（Stage 3/4
    # 同一套字幕）。stage4 done + 分离文件不存在 + 首次备份在 → 把
    # highlights.json 还原为最初原表、s3 镜像同步还原，本次从原表重新优化。
    opt_dir = svc.stage4_optimize_dir(root, task_id, project_id)
    opt_doc_path = stage4_optimized_doc_path(root, task_id, project_id)
    backup = opt_dir / "backup_highlights.json"
    hp = svc.stage_highlights_path(root, task_id, project_id)
    if (not opt_doc_path.is_file() and backup.is_file()
            and svc.get_stage_state(project, "stage4_optimize").get("status") == "done"):
        restored = json.loads(backup.read_text(encoding="utf-8"))
        hp.write_text(json.dumps(restored, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        s3m = svc.get_stage_state(project, "stage3_analyze")
        s3m["highlights"] = [dict(h) for h in restored.get("highlights") or []]
        svc.set_stage_state(project, "stage3_analyze", s3m)
        svc.save_project(root, project)
    if not hp.is_file():
        raise svc.ShortVideoError("Stage 4 需要先完成 Stage 3 内容提取")
    doc = json.loads(hp.read_text(encoding="utf-8"))
    highlights = [h for h in (doc.get("highlights") or []) if h.get("subtitle_lines")]
    if not highlights:
        raise svc.ShortVideoError("Stage 3 没有可优化的字幕行")

    # REQ-20261017-155：单条重新优化 — 目标条从 Stage 3 原表重新跑 LLM，
    # 其余条保留分离表（optimized_highlights.json）里的既有结果；分离表
    # 不存在/读失败时退化为原表内容（与全量首跑同底稿）。work_doc 是写
    # 分离表的完整底稿（index 与 Stage 3 对齐 — 消费侧按位置取 hl_index）。
    single_mode = False
    work_doc = doc
    if hl_index:
        want = int(hl_index)
        if want > 0:
            single_mode = True
            base_hls = list(doc.get("highlights") or [])
            if opt_doc_path.is_file():
                try:
                    prev = json.loads(opt_doc_path.read_text(encoding="utf-8"))
                    prev_by_idx = {int(h.get("index") or 0): dict(h)
                                   for h in prev.get("highlights") or []}
                    base_hls = [
                        prev_by_idx.get(int(h.get("index") or 0), h)
                        if int(h.get("index") or 0) != want else h
                        for h in base_hls
                    ]
                except Exception as prev_err:  # noqa: BLE001
                    log.warning("[stage4_optimize] 读分离表失败，按原表处理: %s",
                                prev_err)
            targets = [h for h in base_hls if int(h.get("index") or 0) == want]
            if not targets:
                raise svc.ShortVideoError(f"hl#{want} 不存在")
            if not targets[0].get("subtitle_lines"):
                raise svc.ShortVideoError(f"hl#{want} 没有可优化的字幕行")
            work_doc = {"highlights": base_hls}
            highlights = targets

    # 状态：running
    state = svc.get_stage_state(project, "stage4_optimize")
    state["status"] = "running"
    state["started_at"] = svc.now_iso()
    state["count"] = len(highlights)
    # REQ-20261016-146：提示词 UI 已移除，清掉旧项目的遗留字段
    state.pop("prompt_user", None)
    state.pop("prompt", None)
    state["logs"] = list(state.get("logs") or []) + [
        f"[{svc.now_iso()}] stage4_optimize start, highlights={len(highlights)}"
        + (f", single hl#{int(hl_index)}" if single_mode else ""),
    ]
    svc.set_stage_state(project, "stage4_optimize", state)
    svc.save_project(root, project)

    system = _OPTIMIZE_SYSTEM_PROMPT

    # 首次优化前备份原表（已存在不覆盖 — 分离存储后的双保险）
    opt_dir.mkdir(parents=True, exist_ok=True)
    if not backup.is_file():
        backup.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    all_warnings: list[str] = []
    hl_states: list[dict] = []
    failed = 0
    # REQ-20261016-146：删除行时间回填用（src_lines 可能缺 start_ms/end_ms）
    srt_times = _resolve_srt_line_times(root, task_id, project_id)
    try:
        for hl in highlights:
            idx = int(hl.get("index") or 0) or (len(hl_states) + 1)
            src_lines = [dict(sl) for sl in hl.get("subtitle_lines") or []]
            user_p = _optimize_user_payload(str(hl.get("title") or ""), src_lines)
            raw = revision_service._call_llm(system, user_p, entry=entry, retries=1)
            (opt_dir / f"llm_raw_hl{idx}.txt").write_text(
                str(raw), encoding="utf-8",
            )
            ops, warns = _parse_optimize_result(str(raw), src_lines)
            all_warnings.extend(f"#{idx} {w}" for w in warns)

            # REQ-20261019-219：LLM 剪词只对唯一 sid 行生效（与手动 ✂️ 同口径 —
            # 共用父 src_index 的行剪词窗口有歧义）
            sid_counts: dict[int, int] = {}
            for sl in src_lines:
                sid_counts[int(sl.get("src_index") or 0)] = \
                    sid_counts.get(int(sl.get("src_index") or 0), 0) + 1

            new_lines: list[dict] = []
            deleted_lines: list[dict] = []
            kept = trimmed = deleted = 0
            for sl in src_lines:
                no = int(sl.get("no") or 0)
                op = ops.get(no) or {"op": "delete"}
                if op["op"] == "delete":
                    deleted += 1
                    # REQ-20261016-146：删除行不再凭空消失 — 记录下来供 UI 保留展示
                    sid = int(sl.get("src_index") or 0)
                    sm, em = srt_times.get(
                        sid,
                        (int(sl.get("start_ms") or 0), int(sl.get("end_ms") or 0)),
                    )
                    deleted_lines.append({
                        "src_index": sid,
                        "no": no,
                        "text": str(sl.get("text") or ""),
                        "start_ms": sm,
                        "end_ms": em,
                    })
                elif op["op"] == "trim":
                    # REQ-20261019-219：行内删词 = 重新切分 — token 对齐出
                    # keep_segments（字幕文字对应的时间戳/时间段，预览/粗剪
                    # 按保留段切视频，洞被物理剪掉）
                    sid = int(sl.get("src_index") or 0)
                    src_text = str(sl.get("text") or "")
                    sm, em = srt_times.get(
                        sid,
                        (int(sl.get("start_ms") or 0), int(sl.get("end_ms") or 0)),
                    )
                    try:
                        if sid_counts.get(sid, 0) > 1:
                            raise svc.ShortVideoError("该行 src_index 与切分行共用")
                        if em <= sm:
                            raise svc.ShortVideoError("无有效时间窗")
                        segs = trim_keep_segments(src_text, op["text"], sm, em)
                    except svc.ShortVideoError as trim_err:
                        # 解析侧已挡新增文字；这里挡共用 sid / 时间窗异常
                        all_warnings.append(
                            f"#{idx} no={no} 剪词失败（{trim_err}），保留原行")
                        kept += 1
                        new_lines.append(dict(sl))
                    else:
                        trimmed += 1
                        new_lines.append({
                            **sl,
                            "text": op["text"],
                            "trimmed_from": src_text,
                            "keep_segments": segs,
                            "start_ms": int(segs[0]["start_ms"]),
                            "end_ms": int(segs[-1]["end_ms"]),
                        })
                else:
                    kept += 1
                    new_lines.append(dict(sl))
            # 行序不变；no 按当前行序重编 1..M（删除后原 no 不连续）
            for i, sl in enumerate(new_lines):
                sl["no"] = i + 1
            hl["subtitle_lines"] = new_lines
            _hl_window(hl)

            st = "ok" if new_lines else "failed"
            if not new_lines:
                failed += 1
                all_warnings.append(f"#{idx} 优化后没有任何字幕行，保留原行")
                hl["subtitle_lines"] = src_lines  # 宁可不改
                _hl_window(hl)
                st = "kept_original"
                deleted_lines = []  # 兜底保原行：删除实际未生效，不记录
            hl_states.append({
                "index": idx,
                "status": st,
                "lines_before": len(src_lines),
                "lines_after": len(hl["subtitle_lines"]),
                "kept": kept, "trimmed": trimmed, "deleted": deleted,
                "deleted_lines": deleted_lines,
            })
    except svc.ShortVideoError as e:
        state = svc.get_stage_state(project, "stage4_optimize")
        state["status"] = "failed"
        state["error"] = str(e)
        state["finished_at"] = svc.now_iso()
        state["logs"] = list(state.get("logs") or []) + [
            f"[{svc.now_iso()}] stage4_optimize failed: {e}",
        ]
        svc.set_stage_state(project, "stage4_optimize", state)
        svc.save_project(root, project)
        return state

    # REQ-20261017-149：优化结果写 Stage 4 自己的表（与 Stage 3 分离 —
    # highlights.json 原表不动，Stage 3/4 各一套字幕）。写 work_doc 的完整
    # highlights 列表（含无字幕行的空片段，保持 index 与 Stage 3 对齐 —
    # 消费侧按位置取 hl_index；单条模式 work_doc 已带其余条的既有优化结果）。
    # REQ-20261019-221：写盘前先留旧表快照 — 之后按「该条字幕行是否真的
    # 变了」逐条作废下游；没变的条保留既有粗剪/精剪成果。
    def _sig(hl: dict) -> str:
        return json.dumps(hl.get("subtitle_lines") or [],
                          ensure_ascii=False, sort_keys=True)

    prev_by_idx: dict[int, str] = {}
    if opt_doc_path.is_file():
        try:
            prev_doc = json.loads(opt_doc_path.read_text(encoding="utf-8"))
            prev_by_idx = {
                int(h.get("index") or 0): _sig(h)
                for h in (prev_doc.get("highlights") or [])
                if isinstance(h, dict)
            }
        except Exception as prev_err:  # noqa: BLE001 — 旧表读失败按全变处理
            log.warning("[stage4_optimize] 读旧优化表失败，按全量作废: %s",
                        prev_err)
    changed_idx = [
        int(h.get("index") or 0)
        for h in (work_doc.get("highlights") or [])
        if isinstance(h, dict) and _sig(h) != prev_by_idx.get(
            int(h.get("index") or 0))
    ]
    opt_doc = {
        "highlights": [dict(h) for h in work_doc.get("highlights") or []],
        "optimized_at": svc.now_iso(),
    }
    opt_doc_path.write_text(
        json.dumps(opt_doc, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # 下游作废（REQ-20261019-221 按条）：只有字幕行实际变化的条作废
    # 该条的粗剪条目 + 精剪输出；其余条已完成的粗剪/精剪不受影响。
    for idx in changed_idx:
        svc.invalidate_hl_downstream(project, idx, after_stage="stage4_optimize")
    pipeline = project.setdefault("pipeline", {})
    pipeline.pop("stage3_splice", None)

    # done（state.highlights = 统计行；字幕本体在 optimized_highlights.json）
    state = svc.get_stage_state(project, "stage4_optimize")
    state["status"] = "failed" if failed == len(highlights) else "done"
    state["finished_at"] = svc.now_iso()
    # REQ-20261017-155：单条模式统计按 index 合并（其余条既有统计不清空）
    if single_mode:
        old_by_idx = {int(h.get("index") or 0): h
                      for h in (state.get("highlights") or [])}
        for h in hl_states:
            old_by_idx[int(h.get("index") or 0)] = h
        state["highlights"] = sorted(old_by_idx.values(),
                                     key=lambda h: int(h.get("index") or 0))
    else:
        state["highlights"] = hl_states
    state["warnings"] = all_warnings[:WARNINGS_CAP]
    state["logs"] = list(state.get("logs") or []) + [
        f"[{svc.now_iso()}] stage4_optimize done, "
        f"hls={len(highlights)}, failed={failed}, warnings={len(all_warnings)}",
    ]
    svc.set_stage_state(project, "stage4_optimize", state)
    svc.save_project(root, project)
    return state


# ---------- REQ-20261013-108：Stage 4 per-highlight 用户编辑状态 ----------
# 路径：<proj>/stage4_optimize/per_hl/<hl_index>.json
# 形态：{line_edits: {src_index: text}, line_marks: [src_index, ...], split_marks: {src_index: [text, ...]},
#        line_trims: {src_index: target_text},  # REQ-20261016-147：✂️ 行内剪词（新文字=原文去掉无用词）
#        line_order: [src_index, ...] | None,   # REQ-20261013-110：段内重排白名单
#        line_restores: [src_index, ...]}       # REQ-20261019-219：还原 LLM 剪词行（↩️ 恢复整行）
# 注意：仅存用户手动编辑，**不污染** stage3/highlights.json（LLM 真相源）。
# 读侧用 _effective_subtitle_lines 合并 highlights + per_hl 后再消费。

_PER_HL_FILENAME = "subs.json"


def per_hl_dir(repo_root: Path | str, task_id: str, project_id: str) -> Path:
    """per-highlight 用户编辑状态的目录。"""
    opt_dir = svc.stage4_optimize_dir(repo_root, task_id, project_id)
    d = opt_dir / "per_hl"
    d.mkdir(parents=True, exist_ok=True)
    return d


def per_hl_path(repo_root: Path | str, task_id: str, project_id: str, hl_index: int) -> Path:
    """per-highlight 用户编辑状态文件路径。"""
    return per_hl_dir(repo_root, task_id, project_id) / f"{int(hl_index):02d}_{_PER_HL_FILENAME}"


def _empty_per_hl() -> dict:
    return {"line_edits": {}, "line_marks": [], "split_marks": {},
            "line_trims": {}, "line_order": None, "line_restores": [],
            "line_bases": {}}


def load_per_hl(repo_root: Path | str, task_id: str, project_id: str, hl_index: int) -> dict:
    """读 per-hl 用户编辑状态。无 / 损坏 → 空。"""
    p = per_hl_path(repo_root, task_id, project_id, hl_index)
    if not p.is_file():
        return _empty_per_hl()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return _empty_per_hl()
    if not isinstance(data, dict):
        return _empty_per_hl()
    out = _empty_per_hl()
    le = data.get("line_edits") or {}
    if isinstance(le, dict):
        for k, v in le.items():
            if str(v or "").strip():
                out["line_edits"][str(k)] = str(v)
    lm = data.get("line_marks") or []
    if isinstance(lm, list):
        for x in lm:
            try:
                out["line_marks"].append(int(x))
            except (TypeError, ValueError):
                pass
    sm = data.get("split_marks") or {}
    if isinstance(sm, dict):
        for k, v in sm.items():
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                out["split_marks"][str(k)] = [str(x) for x in v]
    # REQ-20261016-147：line_trims 行内剪词目标文本（同 line_edits 解析口径）
    lt = data.get("line_trims") or {}
    if isinstance(lt, dict):
        for k, v in lt.items():
            if str(v or "").strip():
                out["line_trims"][str(k)] = str(v)
    # REQ-20261019-223：line_bases = 带改字的剪词对齐基准（sid → 基准全文；
    # 与默认基准（原文/trimmed_from）相同时不落盘）
    lb = data.get("line_bases") or {}
    if isinstance(lb, dict):
        for k, v in lb.items():
            if str(v or "").strip():
                out["line_bases"][str(k)] = str(v)
    # REQ-20261013-110：line_order 是段内重排白名单（None = 保持原序）
    lo = data.get("line_order")
    if isinstance(lo, list) and lo:
        clean = []
        for x in lo:
            try:
                clean.append(int(x))
            except (TypeError, ValueError):
                pass
        if clean:
            out["line_order"] = clean
    # REQ-20261019-219：line_restores = 还原 LLM 剪词的行（src_index 列表）
    lr = data.get("line_restores") or []
    if isinstance(lr, list):
        for x in lr:
            try:
                out["line_restores"].append(int(x))
            except (TypeError, ValueError):
                pass
    return out


def save_per_hl(repo_root: Path | str, task_id: str, project_id: str,
                hl_index: int, line_edits: dict | None = None,
                line_marks: list | None = None,
                split_marks: dict | None = None,
                line_trims: dict | None = None,
                reorder: list | None = None,
                restores: list | None = None,
                line_bases: dict | None = None) -> dict:
    """写 per-hl 用户编辑状态（合并覆盖，未传字段保留旧值）。

    REQ-20261013-110：``reorder`` 接受 ``None``（清除重排）or ``list[int]``
    （段内重排白名单）。传 ``[]`` 视为 None（空列表清空）。
    REQ-20261016-147：``line_trims`` 同 ``line_edits`` 的合并覆盖语义。
    REQ-20261019-219：``restores`` 同 ``line_marks`` 语义（list[int] 整组替换）。
    REQ-20261019-223：``line_bases`` 同 ``line_trims`` 语义（sid → 剪词对齐
    基准全文；带改字的剪词落盘，effective/粗剪同口径消费）。

    Returns: 写入后的完整 doc。
    """
    cur = load_per_hl(repo_root, task_id, project_id, hl_index)
    if line_edits is not None:
        cur["line_edits"] = {str(k): str(v) for k, v in (line_edits or {}).items()
                              if str(v or "").strip()}
    if line_marks is not None:
        cur["line_marks"] = [int(x) for x in (line_marks or [])
                              if str(x).strip().lstrip("-").isdigit()]
    if split_marks is not None:
        cur["split_marks"] = {str(k): [str(x) for x in (v or [])]
                               for k, v in (split_marks or {}).items()
                               if isinstance(v, list) and v}
    if line_trims is not None:
        cur["line_trims"] = {str(k): str(v) for k, v in (line_trims or {}).items()
                             if str(v or "").strip()}
    if reorder is not None:
        # None 或 [] → 清除重排
        if not reorder:
            cur["line_order"] = None
        else:
            cur["line_order"] = [int(x) for x in reorder
                                 if str(x).strip().lstrip("-").isdigit()]
    if restores is not None:
        cur["line_restores"] = [int(x) for x in (restores or [])
                                 if str(x).strip().lstrip("-").isdigit()]
    if line_bases is not None:
        cur["line_bases"] = {str(k): str(v) for k, v in (line_bases or {}).items()
                             if str(v or "").strip()}
    p = per_hl_path(repo_root, task_id, project_id, hl_index)
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur


def _interpolate_subs_time(orig: dict, subs: list[str]) -> list[dict]:
    """按字数占比在原行 [start_ms, end_ms] 内插子段时间戳（无字级时间戳时回退）。"""
    start_ms = int(orig.get("start_ms") or 0)
    end_ms = int(orig.get("end_ms") or start_ms)
    total_ms = max(1, end_ms - start_ms)
    total_len = sum(len(str(s or "")) for s in subs) or 1
    out: list[dict] = []
    cursor = start_ms
    for i, text in enumerate(subs):
        share = len(str(text or "")) / total_len
        sub_start = cursor
        sub_end = start_ms + int(total_ms * (sum(len(str(s or "")) for s in subs[: i + 1])) / total_len) \
            if i < len(subs) - 1 else end_ms
        out.append({
            "no": int(orig.get("no") or 0) * 1000 + i + 1,  # 子行 no 编码（与 src_index 不冲突）
            "src_index": int(orig.get("src_index") or 0) * 1000 + i + 1,  # 子行 src_index 编码
            "text": str(text or ""),
            "start_ms": int(sub_start),
            "end_ms": int(sub_end),
            "split_from": int(orig.get("src_index") or 0),  # 标记父行 src_index
        })
        cursor = sub_end
    return out


_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_]*")


def _pad_latin(text: str) -> str:
    """拉丁/数字 run 两侧补空格（REQ-20261019-223）。

    ``cutlist_service._TOKEN_RE`` 的 ``[\\w-]+`` 在 Python 里 \\w 含中文 —
    拉丁 run 后紧跟的中文会被粘成一个 token（「BOM原文」= 1 token），剪词
    目标里的「BOM」永远对不上。补空格后 tokenize 按拉丁/中文切分。纯中文
    文本 = 恒等（既有纯中文剪词行为零变化）。
    """
    return _LATIN_RUN_RE.sub(lambda m: f" {m.group(0)} ", str(text or ""))


def _token_align_map(orig_toks: list[str], eff_toks: list[str]) -> dict[int, int]:
    """eff token 按序在 orig 中贪心匹配 → {eff 索引: orig 索引}（未命中缺席）。

    与 cutlist_service.align_tokens 同算法，但保留 eff 侧位置（对齐缺口 =
    改字/插入位置）。"""
    pos = 0
    m: dict[int, int] = {}
    for i, t in enumerate(eff_toks):
        for j in range(pos, len(orig_toks)):
            if orig_toks[j] == t:
                m[i] = j
                pos = j + 1
                break
    return m


def _splice_corrections(base_text: str, eff_text: str) -> str:
    """REQ-20261019-223：把 eff（带改字）里与 base 对不上的 token 拼回 base。

    已剪词行又做了改字（ASR 错词修正）再剪词时，对齐基准 = 原基准 + 把改字
    token 缀在其后继命中锚点之前（无后继锚点 → 追加尾部；无前锚点 → 行首
    由「插在后继前」自然覆盖）。被替换的原词**保留**在基准里（它的音频是
    真实的；改字只换显示文字）。得到的新基准与剪词目标构成纯删除关系，
    trim_keep_segments 可继续按 token 占比切窗（改字词分得其锚点邻域的
    时长，音频不会被切掉）。
    """
    bt = _cl.tokenize(_pad_latin(base_text))
    et = _cl.tokenize(_pad_latin(eff_text))
    if not et:
        return str(base_text)
    m = _token_align_map(bt, et)
    # 连续未命中段（改字 run）→ 插到其后继命中锚点的 orig 索引前
    insert_at: dict[int, list[str]] = {}
    tail: list[str] = []
    run: list[str] = []
    for i, t in enumerate(et):
        if i in m:
            if run:
                insert_at.setdefault(m[i], []).extend(run)
                run = []
        else:
            run.append(t)
    if run:
        tail = run
    out: list[str] = []
    for j, t in enumerate(bt):
        if j in insert_at:
            out.extend(insert_at[j])
        out.append(t)
    out.extend(tail)
    return _cl.join_tokens(out)


def trim_keep_segments(orig_text: str, target_text: str,
                       start_ms: int, end_ms: int) -> list[dict]:
    """REQ-20261016-147：新文字 vs 原文 token 对齐 → 视频保留段（占比内插）。

    同长视频重新切分口径（cutlist_service 的 token 对齐 + optimize_service
    的比例降级 — raw.srt 无字级时间戳）：keep 块 = 目标文字命中的连续 token
    段，洞 = 间隙（对应视频被切掉）。时间按 token 字符占比在
    [start_ms, end_ms] 内插；命中 0 字 → ShortVideoError（宁可不切不可错切）。
    REQ-20261019-223：tokenize 前 ``_pad_latin`` — 拉丁 run（如 BOM）后紧跟
    的中文会被 ``[\\w-]+`` 粘成一个 token，纠错词永远对不上；纯中文零变化。
    """
    orig_tokens = _cl.tokenize(_pad_latin(orig_text))
    target_tokens = _cl.tokenize(_pad_latin(target_text))
    hit = _cl.align_tokens(orig_tokens, target_tokens)
    if not hit:
        raise svc.ShortVideoError("剪词后内容与原行文字对不上（命中 0 字）")
    total_chars = sum(len(t) for t in orig_tokens) or 1
    win = max(0, int(end_ms) - int(start_ms))
    token_char_start: list[int] = []  # 每个 token 的起始字符累计
    cum = 0
    for t in orig_tokens:
        token_char_start.append(cum)
        cum += len(t)
    segs: list[dict] = []
    prev_e = int(start_ms)
    for mark, a, b in _cl.partition_tokens(len(orig_tokens), hit):
        if mark != "keep":
            continue  # delete 洞 = 不产视频段（切掉）
        s_ms = int(start_ms) + int(round(token_char_start[a] / total_chars * win))
        e_ms = int(start_ms) + int(round(
            (token_char_start[b] + len(orig_tokens[b])) / total_chars * win))
        s_ms = max(s_ms, prev_e)  # 单调防御
        if e_ms - s_ms <= 0:
            continue  # 零长段不产
        segs.append({
            "start_ms": s_ms, "end_ms": e_ms,
            "text": _cl.join_tokens(orig_tokens[a:b + 1]),
        })
        prev_e = e_ms
    if not segs:
        raise svc.ShortVideoError("剪词后没有产生任何保留段")
    return segs


def share_split_windows(sib_texts: list[str], start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """REQ-20261016-147：兄弟行（共用父 src_index）按字数占比顺序切分父窗口。

    末行贴 end_ms（与 _interpolate_subs_time 同口径）；返回与 sib_texts
    等长的窗口列表。effective_subtitle_lines 与粗剪 _resolve_line_windows
    共用，保证两消费端口径一致。
    """
    sib_texts = [str(t or "") for t in sib_texts]
    total_len = sum(len(t) for t in sib_texts) or 1
    win = max(1, int(end_ms) - int(start_ms))
    windows: list[tuple[int, int]] = []
    cursor = int(start_ms)
    for j, _t in enumerate(sib_texts):
        w_start = cursor
        if j < len(sib_texts) - 1:
            w_end = int(start_ms) + int(
                win * sum(len(x) for x in sib_texts[: j + 1]) / total_len)
        else:
            w_end = int(end_ms)
        windows.append((int(w_start), int(w_end)))
        cursor = w_end
    return windows


def _resolve_srt_line_times(repo_root: Path, task_id: str, project_id: str) -> dict[int, tuple[int, int]]:
    """raw.srt → {src_index: (start_ms, end_ms)}。空 / 失败 → {}。"""
    try:
        srt_path = svc.stage_raw_srt_path(repo_root, task_id, project_id)
    except Exception:  # noqa: BLE001
        return {}
    if not srt_path.is_file():
        return {}
    try:
        from slirn_home.short_video_analyze import _parse_srt_lines
        lines = _parse_srt_lines(srt_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {int(ln.index): (int(ln.start_ms), int(ln.end_ms)) for ln in lines
            if int(ln.end_ms) > int(ln.start_ms)}


def apply_line_order(rows: list[dict], line_order) -> list[dict]:
    """REQ-20261019-210：line_order 白名单重排（展示/预览/粗剪共用一口径）。

    仅对原始 src_index 行（split_from 为 None）按 line_order 顺序重排；
    未列入白名单的行按原序追加到末尾；split 子行不参与重排，整体追加在
    尾部（与 effective 展示序一致 — 所见即所得，粗剪剪切/SRT 顺序同此）。
    line_order 空 / None → 原样返回（不拷贝，调用方自行处理）。
    """
    if not line_order:
        return rows
    by_src: dict[int, dict] = {}
    rest: list[dict] = []
    for s in rows:
        if s.get("split_from") is not None:
            rest.append(s)  # split 子行不参与重排（跟随父行）
            continue
        sid = int(s.get("src_index") or 0)
        by_src[sid] = s
    ordered: list[dict] = []
    for sid in line_order:
        if sid in by_src:
            ordered.append(by_src.pop(sid))
    # 未列入 reorder 的剩余原始行按原序追加；split 子行穿插在原位之后整体追加
    ordered.extend(by_src.values())
    ordered.extend(rest)
    return ordered


def effective_subtitle_lines(repo_root: Path | str, task_id: str, project_id: str,
                              hl_index: int, *,
                              include_deleted: bool = False) -> list[dict]:
    """合并 Stage 4 底稿 + per-hl 后的实际字幕行（用于预览 / 粗剪 / 展示）。

    底稿 = optimized_highlights.json（stage4 done 时，REQ-20261017-149 分离
    存储）；否则回退 stage3/highlights.json（优化前预览）。
    顺序：先按 line_marks 删 → split_marks 切分 → line_trims 剪词
    （REQ-20261016-147）→ line_edits 改 text → line_order 重排。
    每行输出：{src_index, text, start_ms, end_ms, no} — start_ms/end_ms 来自
    raw.srt 按 src_index 查询；找不到则用 highlight 全段时间（按字数占比内插）。
    切分子行用原行 [start_ms, end_ms] 按字数内插。
    REQ-20261016-147：src_index 重复（LLM split 子行）按子行字数占比切分父
    raw.srt 窗口（父窗口不重复）；剪词行带 keep_segments（视频保留段）+
    trimmed_from（原文），start/end 收敛到保留跨度 — 消费侧（预览/粗剪）按
    keep_segments 切视频。

    REQ-20261016-146：include_deleted=True 时不剔除 marked 行，改为打
    ``marked_deleted: True`` 标记照常参与后续合并（渲染用 — 删除行保留可见）；
    默认 False 行为不变（preview / 消费侧仍剔除）。
    """
    root = Path(repo_root)
    doc = load_stage4_base_doc(root, task_id, project_id)
    if not doc:
        return []
    highlights = doc.get("highlights") or []
    if hl_index < 1 or hl_index > len(highlights):
        return []
    hl = highlights[hl_index - 1]
    base = [dict(sl) for sl in (hl.get("subtitle_lines") or [])]
    srt_index_to_time = _resolve_srt_line_times(root, task_id, project_id)
    hl_start = int(hl.get("start_ms") or 0)
    hl_end = int(hl.get("end_ms") or hl_start)
    # REQ-20261016-147：src_index 计数 — LLM split 子行共用父 src_index，
    # 直接按 sid 重查 raw.srt 会让父窗口重复 N 次；重复 sid 的兄弟行按
    # 字数占比顺序切分父 raw.srt 窗口（末行贴 end），不再重复。
    sid_counts: dict[int, int] = {}
    for s in base:
        sid = int(s.get("src_index") or 0)
        sid_counts[sid] = sid_counts.get(sid, 0) + 1
    dup_windows: dict[int, list[tuple[int, int]]] = {}
    for sid, cnt in sid_counts.items():
        if cnt > 1 and sid in srt_index_to_time:
            psm, pem = srt_index_to_time[sid]
            sib_texts = [str(x.get("text") or "") for x in base
                         if int(x.get("src_index") or 0) == sid]
            dup_windows[sid] = share_split_windows(sib_texts, psm, pem)
    dup_pos: dict[int, int] = {}  # 重复 sid 的消费游标
    per = load_per_hl(root, task_id, project_id, hl_index)
    # REQ-20261019-219：line_restores = 用户还原了 LLM 剪词行（↩️）→ 脱掉
    # 优化表烘焙的剪词：text 回原文、去 keep_segments/trimmed_from，
    # 时间窗自然回到 raw.srt 整窗（下方按 sid 重查）
    restores = {int(x) for x in per.get("line_restores") or []}
    # REQ-20261019-223：line_bases = 剪词时烘进去的改字基准（含纠错词）—
    # 对齐用它而不是表内 text，保证「改字不切音频」在重渲染时口径一致
    bases = per.get("line_bases") or {}
    # 给每行补上 start_ms / end_ms / no
    for i, s in enumerate(base):
        sid = int(s.get("src_index") or 0)
        if s.get("keep_segments") is not None and sid in restores:
            s["text"] = str(s.get("trimmed_from") or s.get("text") or "")
            s.pop("keep_segments", None)
            s.pop("trimmed_from", None)
        if sid in dup_windows:
            j = dup_pos.get(sid, 0)
            dup_pos[sid] = j + 1
            sm, em = dup_windows[sid][min(j, len(dup_windows[sid]) - 1)]
        elif sid in srt_index_to_time:
            sm, em = srt_index_to_time[sid]
        else:
            # 行内自带有效时间 → 直接用（Stage 3 不写时间，LLM split 内插值
            # 是 0/1ms 垃圾会走下一个兜底）
            inline_sm = int(s.get("start_ms") or 0)
            inline_em = int(s.get("end_ms") or 0)
            if inline_em > inline_sm:
                sm, em = inline_sm, inline_em
            else:
                # fallback: 整个 highlight 区间按字数占比内插
                total = sum(len(str(x.get("text") or "")) for x in base) or 1
                cum = sum(len(str(base[j].get("text") or "")) for j in range(i))
                cur_len = len(str(s.get("text") or ""))
                sm = hl_start + int((hl_end - hl_start) * cum / total)
                em = hl_start + int((hl_end - hl_start) * (cum + cur_len) / total)
        s["start_ms"] = sm
        s["end_ms"] = em
        s["no"] = i + 1
        # REQ-20261019-219：LLM 剪词行（未还原）— 行时间收敛到保留跨度
        # （keep_segments 每段自带时间戳，预览/粗剪按段切洞）
        doc_keep = s.get("keep_segments")
        if doc_keep and s.get("trimmed_from"):
            s["start_ms"] = int(doc_keep[0]["start_ms"])
            s["end_ms"] = int(doc_keep[-1]["end_ms"])
    marks = set(int(x) for x in per.get("line_marks") or [])
    if marks:
        if include_deleted:
            # REQ-20261016-146：渲染口径 — 标删行保留，打标记继续走后续合并
            for s in base:
                if int(s.get("src_index") or 0) in marks:
                    s["marked_deleted"] = True
        else:
            base = [s for s in base if int(s.get("src_index") or 0) not in marks]
    # split_marks：按子段替换原行（子行内插时间）
    splits = per.get("split_marks") or {}
    if splits:
        new_base: list[dict] = []
        for s in base:
            sid = str(s.get("src_index") or 0)
            if sid in splits:
                new_base.extend(_interpolate_subs_time(s, splits[sid]))
            else:
                new_base.append(s)
        base = new_base
    # REQ-20261016-147：line_trims 行内剪词 — 新文字 = 原文去掉无用词；
    # keep_segments = 视频保留段（预览/粗剪按此切掉洞）。sid 不唯一
    # （LLM split 共用）或对不上 → 跳过（宁可不切不可错切）。
    # REQ-20261019-219：LLM 已剪过的行再手动剪 — 以原文（trimmed_from）为
    # 基准重新对齐：先脱掉烘焙的剪词，时间窗回到 raw.srt 整窗再占比内插。
    trims = per.get("line_trims") or {}
    if trims:
        for s in base:
            sid = int(s.get("src_index") or 0)
            target = str(trims.get(str(sid)) or "").strip()
            if not target or sid_counts.get(sid, 0) > 1:
                continue
            if s.get("keep_segments") is not None:
                s["text"] = str(s.get("trimmed_from") or s.get("text") or "")
                s.pop("keep_segments", None)
                s.pop("trimmed_from", None)
                if sid in srt_index_to_time:
                    s["start_ms"], s["end_ms"] = srt_index_to_time[sid]
            # REQ-20261019-223：对齐基准 = line_bases（烘进去的改字）优先，
            # 否则行文字（脱烘焙后 = 原文）。「剪回原文」判据是**原文**（不是
            # 基准 — target==基准≠原文时仍是剪词态，不是还原）
            base_text = str(bases.get(str(sid)) or s.get("text") or "")
            if _norm_text(target) == _norm_text(str(s.get("text") or "")):
                # 剪回原文 = 恢复（上面的脱烘焙已把行还原成原文）
                continue
            try:
                segs = trim_keep_segments(base_text, target,
                                          int(s.get("start_ms") or 0),
                                          int(s.get("end_ms") or 0))
            except svc.ShortVideoError:
                continue
            s["trimmed_from"] = base_text
            s["text"] = target
            s["keep_segments"] = segs
            s["start_ms"] = int(segs[0]["start_ms"])
            s["end_ms"] = int(segs[-1]["end_ms"])
    # line_edits：整行替换 text（保留 start_ms / end_ms / keep_segments —
    # REQ-20261019-223：剪词行也可改字（ASR 错词纠错）：只换显示文字，
    # 视频切窗仍由 line_trims 决定，改字层永远不触碰）
    edits = per.get("line_edits") or {}
    if edits:
        for s in base:
            sid = str(s.get("src_index") or 0)
            if sid in edits:
                # REQ-20261017-153：edited_from 记用户改前原文（渲染口径
                # 「原文保留 + 行下方输入框」）；与原文相同不记（无差异不显示框）
                if str(edits[sid]) != str(s.get("text") or ""):
                    s["edited_from"] = str(s.get("text") or "")
                s["text"] = edits[sid]
    # REQ-20261013-110：line_order 段内重排白名单（仅对原始 src_index 行重排；
    # split 子行带 split_from 字段，随父行一起走）。未列入的按原序追加到末尾。
    base = apply_line_order(base, per.get("line_order"))
    # 重新按出现顺序编 no 1..M
    for i, s in enumerate(base):
        s["no"] = i + 1
    return base


def _invalidate_hl_if_changed(repo_root: Path | str, task_id: str,
                              project_id: str, hl_index: int,
                              before: dict, after: dict) -> None:
    """REQ-20261019-221：per-hl 手工编辑落盘后 — 状态**实际变化**才作废
    该条的下游（粗剪条目 + 精剪输出，见 svc.invalidate_hl_downstream）；
    其余条已完成的工作不受影响。before/after 是编辑前后的 per-hl doc。"""
    norm = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False)  # noqa: E731
    if norm(before or {}) == norm(after or {}):
        return
    try:
        project = svc.load_project(repo_root, task_id, project_id)
    except Exception:  # noqa: BLE001 — 项目读不到时只保状态不追作废
        return
    svc.invalidate_hl_downstream(project, int(hl_index),
                                 after_stage="stage4_optimize")
    svc.save_project(repo_root, project)


def per_hl_apply_line_edit(repo_root: Path | str, task_id: str, project_id: str,
                            hl_index: int, src_index: int, new_text: str,
                            *, remove: bool = False) -> dict:
    """整行替换 text（fix）。Returns 写入后的 per-hl doc。

    REQ-20261016-147：该行已有剪词（line_trims）时 → 路由到 per_hl_trim
    （改剪词目标），否则编辑会被 trim 阶段跳过造成「静默无效」。
    REQ-20261017-153：``remove=True`` = ✖ 还原（清掉该行的 line_edit；已剪行
    则走剪词「改回原文」= 恢复整行）；new_text 与底稿原文相同 → 同样视为
    还原（与剪词「改回原文 = 恢复」对称，避免出现无差异的空输入框）。
    REQ-20261019-223：**已剪词行的 ✏️ 改为独立改字层**（不再路由到剪词）—
    ASR 错词（如「邦表」→「BOM表」）不在原文里，纯删除校验必拒；改字层
    只换显示/字幕文字，**不动视频窗口**（keep_segments 保持剪词结果）。
    ✖ = 去掉改字层（保留剪词）；整行还原仍走 ↩️（per_hl_unsplit）。
    """
    root = Path(repo_root)
    cur = load_per_hl(root, task_id, project_id, hl_index)
    sid = str(int(src_index))
    row = _base_line_row(root, task_id, project_id, hl_index, int(src_index))
    if sid in (cur.get("line_trims") or {}) or _is_doc_trimmed(row):
        # REQ-20261019-223：剪词行改字 = line_edits 叠加层（只换文字）
        edits = dict(cur.get("line_edits") or {})
        eff = str((cur.get("line_trims") or {}).get(sid) or "").strip()
        if not eff and row is not None:
            # LLM 烘焙行：生效文字 = 烘焙 target（被还原过的行 = 原文）
            eff = (str(row.get("trimmed_from") or "")
                   if int(src_index) in (cur.get("line_restores") or [])
                   else str(row.get("text") or "")).strip()
        if remove or (eff and _norm_text(str(new_text)) == _norm_text(eff)):
            edits.pop(sid, None)  # ✖ / 改回生效文字 = 去掉改字层（剪词保留）
        else:
            edits[sid] = str(new_text)
        save_per_hl(root, task_id, project_id, hl_index, line_edits=edits)
        after = load_per_hl(root, task_id, project_id, hl_index)
        _invalidate_hl_if_changed(root, task_id, project_id, hl_index, cur, after)
        return after
    edits = dict(cur.get("line_edits") or {})
    orig = _base_line_text(root, task_id, project_id, hl_index, int(src_index))
    if remove or (orig is not None
                  and _norm_text(str(new_text)) == _norm_text(orig)):
        edits.pop(sid, None)
    else:
        edits[sid] = str(new_text)
    save_per_hl(root, task_id, project_id, hl_index, line_edits=edits)
    after = load_per_hl(root, task_id, project_id, hl_index)
    _invalidate_hl_if_changed(root, task_id, project_id, hl_index, cur, after)
    return after


def per_hl_toggle_line_mark(repo_root: Path | str, task_id: str, project_id: str,
                             hl_index: int, src_index: int) -> dict:
    """整行删除/恢复（toggle）。Returns 写入后的 per-hl doc。"""
    cur = load_per_hl(repo_root, task_id, project_id, hl_index)
    marks = list(cur.get("line_marks") or [])
    sid = int(src_index)
    if sid in marks:
        marks.remove(sid)
    else:
        marks.append(sid)
    save_per_hl(repo_root, task_id, project_id, hl_index, line_marks=marks)
    after = load_per_hl(repo_root, task_id, project_id, hl_index)
    _invalidate_hl_if_changed(repo_root, task_id, project_id, hl_index,
                              cur, after)
    return after


def per_hl_resplit(repo_root: Path | str, task_id: str, project_id: str,
                    hl_index: int, src_index: int, target_subs: list[str]) -> dict:
    """把一行切分为多段（split）。Returns 写入后的 per-hl doc。

    .. deprecated:: REQ-20261016-147
       ✂️ 重新切分已改为**行内剪词**（per_hl_trim）；本函数仅为旧数据兼容
       保留（读侧 split_marks 路径仍在），新调用不应再走这里。
    """
    if not target_subs or not all(str(x or "").strip() for x in target_subs):
        raise svc.ShortVideoError("切分子段不能为空")
    before = load_per_hl(repo_root, task_id, project_id, hl_index)
    save_per_hl(repo_root, task_id, project_id, hl_index,
                split_marks={str(src_index): [str(x) for x in target_subs]})
    after = load_per_hl(repo_root, task_id, project_id, hl_index)
    _invalidate_hl_if_changed(repo_root, task_id, project_id, hl_index,
                              before, after)
    return after


def _base_line_text(repo_root: Path | str, task_id: str, project_id: str,
                    hl_index: int, src_index: int) -> str | None:
    """REQ-20261017-153：查底稿行原文（load_stage4_base_doc 同口径 — stage4
    done 读优化表，否则 stage3 原表）。行不存在 / sid 重复（LLM split 共用）
    → None（调用方自定语义）。"""
    doc = load_stage4_base_doc(Path(repo_root), task_id, project_id) or {}
    highlights = doc.get("highlights") or []
    if hl_index < 1 or hl_index > len(highlights):
        return None
    rows = [sl for sl in (highlights[hl_index - 1].get("subtitle_lines") or [])
            if isinstance(sl, dict)
            and int(sl.get("src_index") or 0) == int(src_index)]
    if len(rows) != 1:
        return None
    # REQ-20261019-219：LLM 剪词行的「原文」= trimmed_from（剪词前文字）
    if _is_doc_trimmed(rows[0]):
        return str(rows[0].get("trimmed_from") or rows[0].get("text") or "")
    return str(rows[0].get("text") or "")


def _base_line_row(repo_root: Path | str, task_id: str, project_id: str,
                   hl_index: int, src_index: int) -> dict | None:
    """REQ-20261019-219：查底稿行 dict（唯一行；不存在 / sid 重复 → None）。

    供「该行是否被 LLM 剪过词」判断（keep_segments + trimmed_from 烘焙在
    优化表行上）。"""
    doc = load_stage4_base_doc(Path(repo_root), task_id, project_id) or {}
    highlights = doc.get("highlights") or []
    if hl_index < 1 or hl_index > len(highlights):
        return None
    rows = [sl for sl in (highlights[hl_index - 1].get("subtitle_lines") or [])
            if isinstance(sl, dict)
            and int(sl.get("src_index") or 0) == int(src_index)]
    return rows[0] if len(rows) == 1 else None


def _is_doc_trimmed(row: dict | None) -> bool:
    """该底稿行是否被 LLM 剪过词（优化表烘焙的 keep_segments/trimmed_from）。"""
    return bool(row and row.get("keep_segments") is not None
                and row.get("trimmed_from"))


def per_hl_trim(repo_root: Path | str, task_id: str, project_id: str,
                hl_index: int, src_index: int, target_text: str) -> dict:
    """REQ-20261016-147：✂️ 重新切分 = 行内剪词（新文字 = 原文去掉无用词）。

    - target 与原文（归一后）相同或为空 → 清除剪词 = 恢复整行；
    - 剪词取代该行的 split_marks / line_edits（一并清除，单一事实源）；
    - sid 在该 highlight 的行里出现 ≠1 次（LLM split 共用父 src_index）
      → 报错不写（宁可不切不可错切）；
    - 保存即校验（trim_keep_segments 命中 0 字 → 报错）。

    REQ-20261017-153：原文改读 ``load_stage4_base_doc``（stage4 done 时 =
    优化表，否则 stage3 原表）— 与 effective_subtitle_lines 的剪词口径
    对齐（此前直读 highlights.json，优化后行的校验原文是错的）。
    REQ-20261019-219：底稿行被 LLM 剪过词时，剪词基准 = **原文**
    （trimmed_from）；改回原文 = 记 line_restores（还原 LLM 剪词），再剪 =
    清 line_restores（新剪词覆盖还原）。
    REQ-20261019-223：**带改字的剪词** — 该行已有改字层（line_edits）时，
    对齐基准 = 改字后的文字（未剪行直接用；已剪行把改字拼回原基准
    ``_splice_corrections``），基准落盘 ``line_bases``（effective/粗剪同
    口径消费），改字层随之清空（最终文字单一事实源 = line_trims）。

    Returns: 写入后的 per-hl doc。
    """
    root = Path(repo_root)
    doc = load_stage4_base_doc(root, task_id, project_id)
    if not doc:
        raise svc.ShortVideoError("底稿不存在（先完成 Stage 3 内容提取）")
    highlights = doc.get("highlights") or []
    if hl_index < 1 or hl_index > len(highlights):
        raise svc.ShortVideoError(f"hl#{hl_index} 不存在")
    rows = [sl for sl in (highlights[hl_index - 1].get("subtitle_lines") or [])
            if isinstance(sl, dict)
            and int(sl.get("src_index") or 0) == int(src_index)]
    if not rows:
        raise svc.ShortVideoError(f"@SRT#{src_index} 不在该短视频的字幕行里")
    if len(rows) > 1:
        raise svc.ShortVideoError(
            f"@SRT#{src_index} 已被切分成多行共用，无法剪词（请逐行原文操作）")
    # REQ-20261019-219：LLM 剪词行的用户剪词以原文（trimmed_from）为基准
    doc_trimmed = _is_doc_trimmed(rows[0])
    orig_text = (str(rows[0].get("trimmed_from"))
                 if doc_trimmed else str(rows[0].get("text") or ""))
    target = str(target_text or "").strip()
    sid = str(int(src_index))
    cur = load_per_hl(root, task_id, project_id, hl_index)
    trims = dict(cur.get("line_trims") or {})
    bases = dict(cur.get("line_bases") or {})
    edits = dict(cur.get("line_edits") or {})
    restores = list(cur.get("line_restores") or [])
    restores_changed = False
    bases_changed = False
    # REQ-20261019-223：对齐基准 — 改字层存在时把改字烘进基准
    if sid in edits:
        if sid in trims:
            align_base = _splice_corrections(
                str(bases.get(sid) or orig_text), str(edits[sid]))
        else:
            align_base = str(edits[sid])
    else:
        align_base = str(bases.get(sid) or orig_text)
    if not target or _norm_text(target) == _norm_text(orig_text):
        # 改回原文 = 恢复整行：清剪词 + 改字层（顺带清残留 split）
        trims.pop(sid, None)
        edits.pop(sid, None)
        if sid in bases:
            bases.pop(sid)
            bases_changed = True
        if doc_trimmed and int(src_index) not in restores:
            # 底稿本身被 LLM 剪过 → 记还原（effective/粗剪据此脱掉烘焙剪词）
            restores.append(int(src_index))
            restores_changed = True
    else:
        # 保存即校验：命中 0 字 → 报错不写
        trim_keep_segments(align_base, target, 0, max(1, len(align_base) * 1000))
        trims[sid] = target
        edits.pop(sid, None)  # 改字已烘进基准/目标（单一事实源）
        if align_base != orig_text:
            bases[sid] = align_base
            bases_changed = True
        elif sid in bases:
            bases.pop(sid)
            bases_changed = True
        if doc_trimmed and int(src_index) in restores:
            # 重新剪词覆盖还原（effective 会先脱烘焙、再按新 target 对齐基准）
            restores.remove(int(src_index))
            restores_changed = True
    # 剪词取代该行的 split_marks / line_edits
    edits = {k: v for k, v in edits.items() if k != sid}
    splits = {k: v for k, v in (cur.get("split_marks") or {}).items() if k != sid}
    kw: dict = {"line_trims": trims, "line_edits": edits, "split_marks": splits}
    if bases_changed:
        kw["line_bases"] = bases
    if restores_changed:
        kw["restores"] = restores
    save_per_hl(root, task_id, project_id, hl_index, **kw)
    after = load_per_hl(root, task_id, project_id, hl_index)
    # REQ-20261019-221：剪词改变了该条的字幕/切窗 → 作废该条粗剪+精剪
    _invalidate_hl_if_changed(root, task_id, project_id, hl_index, cur, after)
    return after


def per_hl_unsplit(repo_root: Path | str, task_id: str, project_id: str,
                    hl_index: int, src_index: int) -> dict:
    """取消切分（unsplit，含 REQ-20261016-147 剪词恢复）。Returns 写入后的 doc。

    REQ-20261019-219：底稿行被 LLM 剪过词（优化表烘焙）时，仅清 line_trims
    不够 — 记 ``line_restores`` 才能把行还原成原文（effective/粗剪据此脱掉
    烘焙的 keep_segments/trimmed_from）。
    REQ-20261019-223：顺带清该行的 line_edits / line_bases（恢复整行 =
    三层全撤，回到底稿原样）。
    """
    cur = load_per_hl(repo_root, task_id, project_id, hl_index)
    splits = dict(cur.get("split_marks") or {})
    splits.pop(str(src_index), None)
    trims = dict(cur.get("line_trims") or {})
    trims.pop(str(src_index), None)
    edits = dict(cur.get("line_edits") or {})
    edits.pop(str(src_index), None)
    bases = dict(cur.get("line_bases") or {})
    bases.pop(str(src_index), None)
    kw: dict = {"split_marks": splits, "line_trims": trims,
                "line_edits": edits, "line_bases": bases}
    row = _base_line_row(Path(repo_root), task_id, project_id,
                         hl_index, int(src_index))
    if _is_doc_trimmed(row):
        restores = list(cur.get("line_restores") or [])
        if int(src_index) not in restores:
            restores.append(int(src_index))
        kw["restores"] = restores
    save_per_hl(repo_root, task_id, project_id, hl_index, **kw)
    after = load_per_hl(repo_root, task_id, project_id, hl_index)
    # REQ-20261019-221：取消切分/恢复整行 → 作废该条下游（其余条不动）
    _invalidate_hl_if_changed(repo_root, task_id, project_id, hl_index,
                              cur, after)
    return after


def per_hl_reorder(repo_root: Path | str, task_id: str, project_id: str,
                   hl_index: int, order: list[int]) -> dict:
    """REQ-20261013-110：段内重排（白名单）。

    ``order`` 是 src_index 列表，按出现顺序应用。**未列入的原始 src_index
    按原序追加到末尾**（白名单语义，不会丢行）。传 ``[]`` 或 ``None`` = 清除
    重排，恢复原 src 顺序。

    REQ-20261019-221：重排改变粗剪的剪切拼接顺序 → 落盘即作废**该条**
    下游（粗剪条目 + 精剪输出；其余条不动），重新合成即按新顺序拼接。
    """
    if order is not None and not isinstance(order, list):
        raise svc.ShortVideoError("order 必须是 list[int] 或 None")
    before = load_per_hl(repo_root, task_id, project_id, hl_index)
    if not order:
        # 清除重排
        save_per_hl(repo_root, task_id, project_id, hl_index, reorder=[])
    else:
        # 校验：所有 src_index 必须是 int，且至少 1 个
        clean = [int(x) for x in order if str(x).strip().lstrip("-").isdigit()]
        if not clean:
            raise svc.ShortVideoError("order 至少包含 1 个有效 src_index")
        save_per_hl(repo_root, task_id, project_id, hl_index, reorder=clean)
    after = load_per_hl(repo_root, task_id, project_id, hl_index)
    _invalidate_hl_if_changed(repo_root, task_id, project_id, hl_index,
                              before, after)
    return after

