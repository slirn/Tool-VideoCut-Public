"""REQ-20260918-048 — 执行历史记录（生成字幕 / 生成粗剪视频）。

单文件落盘 `tasks/<tid>/outputs/execution_history.json`；列表追加、单写锁防并发。
每次执行都有一条记录：起始/截止时间、时长、状态（running/success/failed）、错误信息。
服务重启后历史仍在（落盘而非内存）。

供 app 层的 `gen_subtitle` / `compose_rough` 启动时 record_start，完成/失败时 record_finish。
工作台面板展示用 load_history() 取倒序最近 N 条。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 落盘文件名（与 tasklib 解耦：tasklib 不感知 history 文件，删除任务整体 rm 即可）
HISTORY_FILENAME = "execution_history.json"

# 历史条数上限 — 单文件最大 1MB 量级（按每条约 200 字节估算 → 5000 条），远高于现实预期
_HARD_LIMIT = 5000

# 模块级单写锁（跨任务共享；同一时刻只有一个写者 + 多读）
_WRITE_LOCK = threading.Lock()

# 已知 kind 常量（便于 app 层枚举）
KIND_SUBTITLE_GENERATION = "subtitle_generation"
KIND_SUBTITLE_REVIEW = "subtitle_review"
KIND_ROUGH_CUT = "rough_cut"
KIND_ROUGH_CUT_LINK_PERSON = "rough_cut_link_person"
KIND_ROUGH_COMPOSE = "rough_compose"
KIND_ROUGH_COMPOSE_DELETE = "rough_compose_delete"
KIND_OPTIMIZE = "optimize"
KIND_OPTIMIZE_CUT = "optimize_cut"
KIND_FINE_AI_LAYOUT = "fine_ai_layout"
KIND_FINE_BG_DETECT = "fine_bg_detect"
KIND_FINE_PREVIEW = "fine_preview"
KIND_FINE_EXPORT = "fine_export"
KIND_SHORT_VIDEO_AI = "short_video_ai"
KIND_SHORT_VIDEO_RENDER = "short_video_render"
# REQ-20261013-103：短视频 Stage 5 粗剪（按长视频 compose_rough 整套搬）
KIND_SHORT_VIDEO_COARSE = "short_video_coarse"
KIND_SHORT_VIDEO_COARSE_DELETE = "short_video_coarse_delete"
# REQ-20261013-105：短视频 Stage 7 精剪（按长视频 fine 整套搬）
KIND_SHORT_VIDEO_FINE = "short_video_fine"
# REQ-20261022-144：短视频「每个主要功能都写一条日志」补齐的 kind
KIND_SHORT_VIDEO_SOURCE = "short_video_source"
KIND_SHORT_VIDEO_EXTRACT = "short_video_extract"
KIND_SHORT_VIDEO_ANALYZE = "short_video_analyze"
KIND_SHORT_VIDEO_ANALYZE_RERUN = "short_video_analyze_rerun"
KIND_SHORT_VIDEO_OPTIMIZE = "short_video_optimize"
KIND_SHORT_VIDEO_SUBS = "short_video_subs"

# 阶段中文标签（前端展示用）
KIND_LABELS: dict[str, str] = {
    KIND_SUBTITLE_GENERATION: "生成字幕",
    KIND_SUBTITLE_REVIEW: "字幕修订",
    KIND_ROUGH_CUT: "执行切分修剪",
    KIND_ROUGH_CUT_LINK_PERSON: "关联人员ID",
    KIND_ROUGH_COMPOSE: "合成初剪视频",
    KIND_ROUGH_COMPOSE_DELETE: "删除粗剪成品",
    KIND_OPTIMIZE: "确认保存",
    KIND_OPTIMIZE_CUT: "优化成片剪辑",
    KIND_FINE_AI_LAYOUT: "AI智能布局",
    KIND_FINE_BG_DETECT: "检测区域",
    KIND_FINE_PREVIEW: "生成预览",
    KIND_FINE_EXPORT: "最终导出视频",
    KIND_SHORT_VIDEO_AI: "短视频分镜",
    KIND_SHORT_VIDEO_RENDER: "短视频渲染",
    KIND_SHORT_VIDEO_COARSE: "粗剪合成（短）",
    KIND_SHORT_VIDEO_COARSE_DELETE: "删除粗剪成品（短）",
    KIND_SHORT_VIDEO_FINE: "精剪混编（短）",
    KIND_SHORT_VIDEO_SOURCE: "选择源视频（短）",
    KIND_SHORT_VIDEO_EXTRACT: "提取字幕（短）",
    KIND_SHORT_VIDEO_ANALYZE: "内容提取（短）",
    KIND_SHORT_VIDEO_ANALYZE_RERUN: "内容提取·单条重跑（短）",
    KIND_SHORT_VIDEO_OPTIMIZE: "内容优化（短）",
    KIND_SHORT_VIDEO_SUBS: "成片字幕提取（短）",
}

# kind → 所属工作台阶段 key（前端分组 / 颜色）
KIND_TO_STAGE: dict[str, str] = {
    KIND_SUBTITLE_GENERATION: "subtitle",
    KIND_SUBTITLE_REVIEW: "subtitle_review",
    KIND_ROUGH_CUT: "rough_cut",
    KIND_ROUGH_CUT_LINK_PERSON: "rough_cut",
    KIND_ROUGH_COMPOSE: "rough_compose",
    KIND_ROUGH_COMPOSE_DELETE: "rough_compose",
    KIND_OPTIMIZE: "fine_review",
    KIND_OPTIMIZE_CUT: "fine_review",
    KIND_FINE_AI_LAYOUT: "fine_cut",
    KIND_FINE_BG_DETECT: "fine_cut",
    KIND_FINE_PREVIEW: "fine_cut",
    KIND_FINE_EXPORT: "fine_cut",
    # 短视频是独立工作台，不属于长视频六阶段；日志页阶段桶暂归入 fine_cut，
    # 具体功能仍由 kind/label 区分（短视频分镜 / 短视频渲染）。
    KIND_SHORT_VIDEO_AI: "fine_cut",
    KIND_SHORT_VIDEO_RENDER: "fine_cut",
    KIND_SHORT_VIDEO_COARSE: "stage5_coarse",
    KIND_SHORT_VIDEO_COARSE_DELETE: "stage5_coarse",
    KIND_SHORT_VIDEO_FINE: "stage6_fine",
    # REQ-20261022-144：补齐短视频各阶段的日志 kind → 阶段映射
    KIND_SHORT_VIDEO_SOURCE: "stage1_source",
    KIND_SHORT_VIDEO_EXTRACT: "stage2_extract",
    KIND_SHORT_VIDEO_ANALYZE: "stage3_analyze",
    KIND_SHORT_VIDEO_ANALYZE_RERUN: "stage3_analyze",
    KIND_SHORT_VIDEO_OPTIMIZE: "stage4_optimize",
    KIND_SHORT_VIDEO_SUBS: "stage5_coarse",
}

# 默认 description 模板（调用方未传 description 时使用）
DEFAULT_DESCRIPTIONS: dict[str, str] = {
    KIND_SUBTITLE_GENERATION: "FunASR seaco-paraformer 识别原始视频，提取字幕段",
    KIND_SUBTITLE_REVIEW: "字幕修订保存",
    KIND_ROUGH_CUT: "按切分决策生成粗剪片段",
    KIND_ROUGH_CUT_LINK_PERSON: "切分片段关联到人员ID",
    KIND_ROUGH_COMPOSE: "ffmpeg 拼接片段，输出初剪视频（含随片字幕）",
    KIND_ROUGH_COMPOSE_DELETE: "删除上一轮粗剪成品（mp4 + srt 副产物）",
    KIND_OPTIMIZE: "优化字幕保存：识别成片 + 大模型提取不明确字词",
    KIND_OPTIMIZE_CUT: "把标记删除行的时间区间从粗剪成片剪除，输出优化成片（字幕时间轴随片前移）",
    KIND_FINE_AI_LAYOUT: "LLM 分析视频画面，生成精剪布局建议",
    KIND_FINE_BG_DETECT: "检测视频主体区域，记录到 detected_region",
    KIND_FINE_PREVIEW: "生成精剪预览切片（可调起止时间）",
    KIND_FINE_EXPORT: "ffmpeg 渲染精剪视频（异步后台任务）",
    KIND_SHORT_VIDEO_AI: "生成短视频多版本结构化分镜",
    KIND_SHORT_VIDEO_RENDER: "渲染短视频 9:16 成片",
    KIND_SHORT_VIDEO_COARSE: "短视频粗剪合成（每条 highlight 独立 mp4）",
    KIND_SHORT_VIDEO_COARSE_DELETE: "删除短视频粗剪成品（N 条 mp4 + 占位 srt）",
    KIND_SHORT_VIDEO_FINE: "短视频精剪混编（ffmpeg 切 + 烧字幕 + BGM）",
    KIND_SHORT_VIDEO_SOURCE: "为本项目选择源视频素材",
    KIND_SHORT_VIDEO_EXTRACT: "FunASR 识别源视频，提取字幕（SRT + 原始结果）",
    KIND_SHORT_VIDEO_ANALYZE: "按提示词与时长区间解析出 N 条独立短视频片段",
    KIND_SHORT_VIDEO_ANALYZE_RERUN: "单条重新解析（只重跑选中的那条片段）",
    KIND_SHORT_VIDEO_OPTIMIZE: "大模型切分长行 / 修正文字 / 删除冗余字幕行",
    KIND_SHORT_VIDEO_SUBS: "对粗剪成片重新识别字幕并生成连续 SRT",
}

# 全部合法 kind（路由层校验非法请求）
ALL_KINDS = {KIND_SUBTITLE_GENERATION, KIND_SUBTITLE_REVIEW,
             KIND_ROUGH_CUT, KIND_ROUGH_CUT_LINK_PERSON,
             KIND_ROUGH_COMPOSE, KIND_ROUGH_COMPOSE_DELETE,
             KIND_OPTIMIZE, KIND_OPTIMIZE_CUT, KIND_FINE_AI_LAYOUT,
             KIND_FINE_BG_DETECT, KIND_FINE_PREVIEW, KIND_FINE_EXPORT,
             KIND_SHORT_VIDEO_AI, KIND_SHORT_VIDEO_RENDER,
             KIND_SHORT_VIDEO_COARSE, KIND_SHORT_VIDEO_COARSE_DELETE,
             KIND_SHORT_VIDEO_FINE, KIND_SHORT_VIDEO_SOURCE,
             KIND_SHORT_VIDEO_EXTRACT, KIND_SHORT_VIDEO_ANALYZE,
             KIND_SHORT_VIDEO_ANALYZE_RERUN, KIND_SHORT_VIDEO_OPTIMIZE,
             KIND_SHORT_VIDEO_SUBS}

# REQ-20261028-NNN：长/短视频执行日志分流。两套体系的记录仍落同一个任务级文件
# （tasks/<tid>/outputs/execution_history.json），但查询端按 kind 家族过滤，
# 各自工作台只看各自的记录（用户明确要求分开，不混排）。
SHORT_VIDEO_KIND_PREFIX = "short_video_"


def is_short_video_kind(kind: str | None) -> bool:
    """kind 是否属于短视频体系（KIND_SHORT_VIDEO_* 均以 short_video_ 开头）。"""
    return bool(kind) and str(kind).startswith(SHORT_VIDEO_KIND_PREFIX)


# REQ-20261022-144：自动执行标记的 contextvar 上下文。
# 流程配置自动跑时，编排器用 auto_context(session_id) 包住阶段调用 →
# 该次调用链里 record_start 写出的日志自动带 auto=true + auto_session_id，
# 前端「⚙ 自动」筛选据此聚合同一轮自动流。不用改任何阶段函数签名。
_AUTO_CTX: contextvars.ContextVar = contextvars.ContextVar(
    "slirn_exec_auto_ctx", default=None)


@contextlib.contextmanager
def auto_context(session_id: str = ""):
    """标记「本段调用链属于流程配置自动执行」。"""
    token = _AUTO_CTX.set({"auto": True, "session_id": str(session_id or "")})
    try:
        yield
    finally:
        _AUTO_CTX.reset(token)


def _auto_ctx() -> dict:
    ctx = _AUTO_CTX.get()
    return ctx if isinstance(ctx, dict) else {}


def _now_iso(ts: float) -> str:
    """float → ISO8601 字符串（含本地时区偏移）。"""
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _outputs_dir(tid: str) -> Path:
    """历史落盘目录：tasks/<tid>/outputs/。调用方传完整 path 时由 record_* 内部解析。"""
    raise NotImplementedError("使用 history_for_outputs(outputs_dir) 显式传目录")


def _read(outputs_dir: Path) -> list[dict]:
    """读取历史；文件不存在或损坏 → 返回空列表。"""
    path = outputs_dir / HISTORY_FILENAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("[history][%s] 读取失败，当作空: %s", outputs_dir, e)
        return []


def _write_atomic(outputs_dir: Path, items: list[dict]) -> None:
    """原子写：先写 .tmp 再 rename，避免崩溃半写。"""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    tmp = outputs_dir / (HISTORY_FILENAME + ".tmp")
    payload = json.dumps(items, ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(outputs_dir / HISTORY_FILENAME)


def record_start(outputs_dir: Path, kind: str, extra: dict | None = None, *,
                 description: str | None = None, auto: bool = False,
                 auto_session_id: str = "") -> str:
    """记录一次执行启动（status=running）。返回本次 execution id（start 时生成，
    finish 时按 id 定位同一记录）。

    REQ-20260919-075：新增 description（操作描述）和 auto（流程自动触发标识）。
    description 为空时按 kind 从 DEFAULT_DESCRIPTIONS 取默认；stage 自动从
    KIND_TO_STAGE 查（前端分组用）。

    REQ-20260920-081：新增 auto_session_id（流程配置自动执行时的会话 ID），
    手动调用为空字符串；前端按 session_id 聚合显示「同一次自动流」的多次操作。

    失败也吞掉（历史记录失败不影响主流程）。
    """
    try:
        ts = time.time()
        desc = description if description else DEFAULT_DESCRIPTIONS.get(kind, "")
        # REQ-20261022-144：自动执行上下文（auto_context）覆盖显式入参
        _ctx = _auto_ctx()
        if _ctx.get("auto"):
            auto = True
        if not auto_session_id and _ctx.get("session_id"):
            auto_session_id = str(_ctx["session_id"])
        item: dict[str, Any] = {
            "id": "exh-" + datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")
                  + "-" + uuid.uuid4().hex[:6],
            "kind": kind,
            "stage": KIND_TO_STAGE.get(kind, ""),
            "started_at": ts,
            "started_at_iso": _now_iso(ts),
            "finished_at": None,
            "finished_at_iso": None,
            "duration_ms": None,
            "status": "running",
            "description": desc,
            "auto": bool(auto),
            "auto_session_id": str(auto_session_id or ""),
            "error": "",
            "extra": extra or {},
        }
        with _WRITE_LOCK:
            items = _read(outputs_dir)
            items.append(item)
            # 截断最旧的避免单文件无限增长
            if len(items) > _HARD_LIMIT:
                items = items[-_HARD_LIMIT:]
            _write_atomic(outputs_dir, items)
        return item["id"]
    except Exception as e:  # noqa: BLE001 — 历史写失败不影响主流程
        log.warning("[history][%s] record_start 失败: %s", outputs_dir, e)
        return ""


def record_finish(outputs_dir: Path, exec_id: str, *,
                  success: bool, error: str = "") -> None:
    """记录一次执行完成（成功/失败）。按 id 定位同一记录并回填 finished_at/duration/status。"""
    if not exec_id:
        return  # record_start 失败时返回了空 id；不写
    try:
        ts = time.time()
        with _WRITE_LOCK:
            items = _read(outputs_dir)
            for it in items:
                if it.get("id") == exec_id:
                    it["finished_at"] = ts
                    it["finished_at_iso"] = _now_iso(ts)
                    if it.get("started_at"):
                        it["duration_ms"] = int((ts - float(it["started_at"])) * 1000)
                    it["status"] = "success" if success else "failed"
                    if not success:
                        it["error"] = (error or "")[:500]  # 截断避免异常对象塞满文件
                    break
            else:
                # 找不到对应 start 记录 → 追加一条 finish（兜底，正常不会进）
                log.warning("[history][%s] record_finish 找不到 id=%s 的启动记录", outputs_dir, exec_id)
                items.append({
                    "id": exec_id, "kind": "unknown",
                    "started_at": ts, "started_at_iso": _now_iso(ts),
                    "finished_at": ts, "finished_at_iso": _now_iso(ts),
                    "duration_ms": 0,
                    "status": "success" if success else "failed",
                    "error": (error or "")[:500],
                    "extra": {},
                })
            if len(items) > _HARD_LIMIT:
                items = items[-_HARD_LIMIT:]
            _write_atomic(outputs_dir, items)
    except Exception as e:  # noqa: BLE001
        log.warning("[history][%s] record_finish 失败: %s", outputs_dir, e)


def patch_extra(outputs_dir: Path, exec_id: str, extra: dict) -> None:
    """合并覆写指定记录的 extra（用于完成时补 segments / speakers 等结果摘要）。"""
    if not exec_id or not extra:
        return
    try:
        with _WRITE_LOCK:
            items = _read(outputs_dir)
            for it in items:
                if it.get("id") == exec_id:
                    it["extra"] = {**(it.get("extra") or {}), **extra}
                    break
            _write_atomic(outputs_dir, items)
    except Exception as e:  # noqa: BLE001
        log.warning("[history][%s] patch_extra 失败: %s", outputs_dir, e)


def patch_fields(outputs_dir: Path, exec_id: str, fields: dict) -> None:
    """REQ-20260919-075：合并覆写指定记录的顶层字段（如 description）。

    仅允许白名单字段写入，避免外部乱改 status / kind / id。
    """
    if not exec_id or not fields:
        return
    allowed = {"description", "stage"}
    safe = {k: v for k, v in fields.items() if k in allowed}
    if not safe:
        return
    try:
        with _WRITE_LOCK:
            items = _read(outputs_dir)
            for it in items:
                if it.get("id") == exec_id:
                    for k, v in safe.items():
                        it[k] = v
                    break
            _write_atomic(outputs_dir, items)
    except Exception as e:  # noqa: BLE001
        log.warning("[history][%s] patch_fields 失败: %s", outputs_dir, e)


def load_history(outputs_dir: Path) -> list[dict]:
    """读全量历史（不截断，调用方决定展示多少条）。"""
    with _WRITE_LOCK:
        return _read(outputs_dir)


def mark_orphans_failed(outputs_dir: Path, *, older_than_sec: float = 300.0) -> int:
    """把「孤儿 running 记录」标为失败（服务重启后残留）。

    2026-10-28 现场：服务重启时正在跑的合成 job 被杀，它的 record_start
    已落盘但 record_finish 永远没来 → 执行日志里那条永远显示「执行中」，
    用户以为还在跑（实际早就没了）。

    判定：status=running 且 started_at 距今超过 older_than_sec。服务启动时
    调一次即可（进程刚起，没有任何本进程内的 running 记录）。
    返回被标记的条数。
    """
    try:
        ts = time.time()
        with _WRITE_LOCK:
            items = _read(outputs_dir)
            changed = 0
            for it in items:
                if it.get("status") != "running":
                    continue
                started = float(it.get("started_at") or 0)
                if started and (ts - started) < older_than_sec:
                    continue
                it["status"] = "failed"
                it["finished_at"] = ts
                it["finished_at_iso"] = _now_iso(ts)
                if started:
                    it["duration_ms"] = int((ts - started) * 1000)
                it["error"] = (it.get("error") or "服务重启导致执行中断（未收到完成回执）")[:500]
                changed += 1
            if changed:
                _write_atomic(outputs_dir, items)
        return changed
    except Exception as e:  # noqa: BLE001 — 清理失败不影响启动
        log.warning("[history][%s] mark_orphans_failed 失败: %s", outputs_dir, e)
        return 0


def query_history_paged(outputs_dir: Path, *,
                        kinds: list[str] | None = None,
                        statuses: list[str] | None = None,
                        keyword: str = "",
                        limit: int = 20,
                        offset: int = 0,
                        time_from_ts: float | None = None,
                        time_to_ts: float | None = None,
                        auto: str = "any",
                        flow: str = "any") -> tuple[list[dict], int]:
    """REQ-20260920-086：分页版 query_history — 返回 (items, total)。

    - items: 当前页的记录（按 started_at 倒序，已切片 offset:offset+limit）
    - total: 过滤后总记录数（用于前端算 total_pages）

    与 query_history 的区别：query_history 早 break 限制返回数量（性能友好），
    但拿不到精确 total；本函数不早 break，先收完所有 matched 再切片。

    复用 query_history 的过滤逻辑：为了避免重复，把过滤逻辑提到 _apply_filters 内部函数。

    REQ-20261028-NNN：flow = "long" 只留长视频体系记录，"short" 只留短视频
    体系记录（kind 前缀 short_video_），"any" 不过滤（缺省，兼容旧调用）。
    """
    items = load_history(outputs_dir)
    # 倒序：最新在前
    items = sorted(items, key=lambda x: x.get("started_at") or 0, reverse=True)
    kw = (keyword or "").strip().lower()
    kind_set = set(kinds) if kinds else None
    status_set = set(statuses) if statuses else None
    auto_mode = (auto or "any").lower()
    flow_mode = (flow or "any").lower()
    matched: list[dict] = []
    for it in items:
        if flow_mode == "long" and is_short_video_kind(it.get("kind")):
            continue
        if flow_mode == "short" and not is_short_video_kind(it.get("kind")):
            continue
        if kind_set is not None and it.get("kind") not in kind_set:
            continue
        if status_set is not None and it.get("status") not in status_set:
            continue
        started = it.get("started_at")
        if time_from_ts is not None and started is not None and started < time_from_ts:
            continue
        if time_to_ts is not None and started is not None and started > time_to_ts:
            continue
        if auto_mode == "manual" and it.get("auto"):
            continue
        if auto_mode == "auto" and not it.get("auto"):
            continue
        if kw:
            blob = (str(it.get("error") or "") + "\n" + str(it.get("kind") or "")
                    + "\n" + str(it.get("auto_session_id") or "")
                    + "\n" + str(it.get("description") or "")).lower()
            if kw not in blob:
                continue
        matched.append(it)
    total = len(matched)
    page_items = matched[offset:offset + limit] if limit > 0 else matched[offset:]
    return page_items, total


def query_history(outputs_dir: Path, *,
                  kinds: list[str] | None = None,
                  statuses: list[str] | None = None,
                  keyword: str = "",
                  limit: int = 200,
                  time_from_ts: float | None = None,
                  time_to_ts: float | None = None,
                  auto: str = "any",
                  flow: str = "any") -> list[dict]:
    """REQ-20260918-053：执行日志查询（按阶段/状态过滤 + 关键词搜错误信息）。

    REQ-20260920-081：新增 time_from_ts / time_to_ts（epoch 秒，按 started_at 区间
    过滤）和 auto（"manual" | "auto" | "any"，按执行模式过滤）。
    REQ-20261028-NNN：新增 flow（"long" | "short" | "any"，按长/短视频体系过滤，
    见 query_history_paged 同名参数）。

    返回倒序最近 N 条；前端分页用 limit 控制。过滤条件全 AND。
    """
    items = load_history(outputs_dir)
    # 倒序：最新在前
    items = sorted(items, key=lambda x: x.get("started_at") or 0, reverse=True)
    kw = (keyword or "").strip().lower()
    if kinds:
        kind_set = set(kinds)
    else:
        kind_set = None
    if statuses:
        status_set = set(statuses)
    else:
        status_set = None
    auto_mode = (auto or "any").lower()
    flow_mode = (flow or "any").lower()
    out: list[dict] = []
    for it in items:
        if flow_mode == "long" and is_short_video_kind(it.get("kind")):
            continue
        if flow_mode == "short" and not is_short_video_kind(it.get("kind")):
            continue
        if kind_set is not None and it.get("kind") not in kind_set:
            continue
        if status_set is not None and it.get("status") not in status_set:
            continue
        # 时间段过滤（按 started_at）
        started = it.get("started_at")
        if time_from_ts is not None and started is not None and started < time_from_ts:
            continue
        if time_to_ts is not None and started is not None and started > time_to_ts:
            continue
        # 模式过滤（manual = auto=False；auto = auto=True）
        if auto_mode == "manual" and it.get("auto"):
            continue
        if auto_mode == "auto" and not it.get("auto"):
            continue
        if kw:
            blob = (str(it.get("error") or "") + "\n" + str(it.get("kind") or "")
                    + "\n" + str(it.get("auto_session_id") or "")
                    + "\n" + str(it.get("description") or "")).lower()
            if kw not in blob:
                continue
        out.append(it)
        if len(out) >= limit:
            break
    return out


def format_duration(ms: int | None) -> str:
    """duration_ms → 人读字符串（35s / 1m23s / 0s）。"""
    if not ms or ms < 0:
        return "-"
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"
