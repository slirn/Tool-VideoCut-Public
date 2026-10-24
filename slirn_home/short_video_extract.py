"""短视频工作台 Stage 2（字幕提取）执行模块。

REQ-20261003-098：

- Stage 2: 把源视频的音频交给 funasr，产物 ``stage2/raw.srt`` + ``stage2/raw.json``.

原 Stage 5 重 ASR（粗剪 mp4 → ``stage5/refined_NN_asr.srt``）已随字幕优化阶段
移除（REQ-20261016-148）；老项目的 ``stage5/refined_NN*.srt`` 磁盘文件保留，
精剪经文件回退链继续读取。

funasr 1.3.9 的注册表只认短名（SeacoParaformer / Paraformer / SenseVoiceSmall）；
extract_subtitle.py 内部用的是长路径（iic/speech_seaco_paraformer_large_...），
所以子进程起之前必须先 patch 注册表。patch 在子进程内重做（每个进程独立）；
为简化这里走 ``python -c "<patch + runpy.run_path>"`` 模式，等价于
``tools/_launch_upstream.py`` 在 Gradio 启动时的做法。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from slirn_home import short_video_service as svc

log = logging.getLogger(__name__)


# ---------- 子进程包装 ----------

# Skill 在两种仓库布局下都存在（路径差异）：
#   1) funclip-main 根 / slirn/  (submodule mount) → skill/video-subtitle-extractor/scripts/
#   2) slirn-standalone 根       → skill/video-subtitle-extractor/scripts/
# 在 live 上下文中 repo_root 是 slirn-standalone；在 smoke 测试里是 funclip-main。
# _resolve_extract_subtitle 按候选顺序找到第一个能解析的脚本路径。
EXTRACT_SUBTITLE_RELPATH_CANDIDATES = (
    Path("slirn") / "skill" / "video-subtitle-extractor" / "scripts" / "extract_subtitle.py",
    Path("skill") / "video-subtitle-extractor" / "scripts" / "extract_subtitle.py",
)


# 在子进程内跑的 boot 脚本：先 patch funasr，再把 funclip 包所在目录的 PARENT
# 加到 sys.path[0]，然后 runpy 执行目标。仅依赖标准库 + 已安装的 funasr/funclip，
# 避免把 slirn_home 的内部 import 串进来。
# REQ-20261004-bugfix：在 live 上下文 repo_root=slirn-standalone，funclip 包位于
# sibling FunClip-main/funclip/；smoke 上下文 repo_root=FunClip-main，funclip 就在
# <repo_root>/funclip。runner 按候选顺序找 funclip 包的上级目录，insert sys.path[0]。
_RUNNER_BODY = r'''
import os, sys, runpy
# patch funasr 1.3.9 model registry（与 _launch_upstream.py 等价）
import funasr  # noqa: F401
from funasr.register import tables
ALIASES = [
    ("iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch", "SeacoParaformer"),
    ("iic/speech_paraformer_asr-en-16k-vocab4199-pytorch", "Paraformer"),
    ("iic/SenseVoiceSmall", "SenseVoiceSmall"),
]
for long_name, short_name in ALIASES:
    cls = tables.model_classes.get(short_name)
    if cls is not None and long_name not in tables.model_classes:
        tables.model_classes[long_name] = cls

# 把 FunClip-main 根 + funclip 包目录 都加到 sys.path，避免 ModuleNotFoundError。
# 关键点：funclip/videoclipper.py 内部既需要 `import funclip.videoclipper`（要
# FunClip-main 根在 sys.path），又需要 `from utils.subtitle_utils import ...`（要
# funclip/ 目录本身在 sys.path，这样 utils 作为 funclip.utils 的兄弟被找到）。
# 两者必须同时插入。
# 候选：repo_root 的 funclip 子目录、repo_root 的 sibling FunClip-main/funclip、
# cwd 的 funclip 子目录、cwd 的 sibling FunClip-main/funclip。
_arg_repo = os.environ.get("SLIRN_SV_REPO_ROOT")
_cwd = os.getcwd()
_funclip_dirs = []  # funclip/ 目录路径
_funclip_parents = []  # funclip 的父目录（FunClip-main 根）
for base in [_arg_repo, _cwd]:
    if not base:
        continue
    base_path = base if os.path.isabs(base) else os.path.abspath(base)
    _funclip_dirs.append(os.path.join(base_path, "funclip"))
    _funclip_dirs.append(os.path.join(os.path.dirname(base_path), "FunClip-main", "funclip"))
for d in _funclip_dirs:
    if os.path.isdir(d):
        # 1) 先把 funclip 父目录（FunClip-main 根）插到 sys.path 末位
        parent = os.path.dirname(d)
        if parent not in sys.path:
            sys.path.append(parent)
        # 2) 再把 funclip 目录插到 sys.path[0]（优先级最高），让 `from utils...`
        # 解析到 funclip.utils
        if d not in sys.path:
            sys.path.insert(0, d)
        break

target = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(target, run_name="__main__")
'''


def _resolve_extract_subtitle(repo_root: Path) -> Path:
    tried: list[Path] = []
    for rel in EXTRACT_SUBTITLE_RELPATH_CANDIDATES:
        cand = (repo_root / rel).resolve()
        if cand.is_file():
            return cand
        tried.append(cand)
    raise FileNotFoundError(
        "找不到上游 extract_subtitle.py（候选: "
        + " | ".join(str(p) for p in tried)
        + f"；repo_root={repo_root}）"
    )


def _runner_path() -> Path:
    """子进程内的 boot 脚本写到 tmp，避免污染仓库。"""
    p = Path(tempfile.gettempdir()) / "_slirn_sv_runner.py"
    if not p.is_file() or p.read_text(encoding="utf-8", errors="replace") != _RUNNER_BODY:
        p.write_text(_RUNNER_BODY, encoding="utf-8")
    return p


# ---------- 通用 funasr 子进程调用 ----------

# 与 extract_subtitle.py 的 argparse choices 保持同步（漏同步 = 子进程
# invalid choice 退出码 2 直接失败）。
_FUNASR_MODEL_WHITELIST = ("paraformer", "fun-asr-nano", "sensevoice")


def _norm_hotwords(hotwords: str) -> str:
    """REQ-20261015-122：UI 热词改半角逗号分隔；funasr hotword 参数要空格分隔。

    把半角/全角逗号切开重排为空格分隔；空格分隔的旧输入原样通过。
    """
    parts = [p.strip() for p in re.split(r"[,，]", hotwords or "")]
    return " ".join(p for p in parts if p)


@dataclass
class FunasrRunResult:
    srt_path: Path
    raw_json: dict | None
    elapsed_sec: int
    stderr_tail: str
    returncode: int


def _run_funasr_extract(
    *,
    input_video: Path,
    output_srt: Path,
    repo_root: Path,
    model: str = "paraformer",
    lang: str = "zh",
    hotwords: str = "",
    sd_switch: str = "no",
    timeout_sec: int = 1800,
) -> FunasrRunResult:
    """跑 extract_subtitle.py 子进程；产物 = output_srt。

    输出 ``raw.json`` 不由上游直接生成，但若 output_srt 同目录下有 ``state.json``
    （上游 videoclipper 调用约定）则一并拷贝到 raw.json。
    """
    input_video = input_video.resolve()
    output_srt = output_srt.resolve()
    output_srt.parent.mkdir(parents=True, exist_ok=True)
    target = _resolve_extract_subtitle(repo_root)
    runner = _runner_path()

    # REQ-20261015-122：模型/语言不再由用户选择。白名单外的值（如历史选项
    # seaco_paraformer，argparse 不收）一律回默认 paraformer-zh。
    if model not in _FUNASR_MODEL_WHITELIST:
        model = "paraformer"
    hotwords = _norm_hotwords(hotwords)

    cmd = [
        sys.executable, str(runner), str(target),
        "--input", str(input_video),
        "--output", str(output_srt),
        "--model", model,
        "--lang", lang,
        "--hotwords", hotwords,
        "--sd-switch", sd_switch,
    ]
    log.info("[funasr] %s", " ".join(cmd))

    t0 = time.time()
    try:
        # REQ-20261004-bugfix：把 repo_root 通过环境变量传给 runner，runner 据此
        # 把 funclip 包所在目录插入 sys.path（slirn-standalone 上下文）。
        proc = subprocess.run(
            cmd, cwd=str(repo_root),
            capture_output=True, text=True,
            timeout=timeout_sec, encoding="utf-8", errors="replace",
            env={**os.environ, "SLIRN_SV_REPO_ROOT": str(repo_root)},
        )
    except subprocess.TimeoutExpired:
        return FunasrRunResult(
            srt_path=output_srt, raw_json=None,
            elapsed_sec=timeout_sec,
            stderr_tail=f"timeout after {timeout_sec}s",
            returncode=-1,
        )
    elapsed = int(time.time() - t0)
    stderr_tail = (proc.stderr or "")[-1500:]
    if proc.returncode != 0 or not output_srt.is_file() or output_srt.stat().st_size < 4:
        return FunasrRunResult(
            srt_path=output_srt, raw_json=None,
            elapsed_sec=elapsed, stderr_tail=stderr_tail,
            returncode=proc.returncode,
        )

    # 试着从同目录找 state.json（上游约定），拷贝为 raw.json
    raw_json: dict | None = None
    state_json = output_srt.parent / "state.json"
    if state_json.is_file():
        try:
            data = json.loads(state_json.read_text(encoding="utf-8", errors="replace"))
            raw_json = data if isinstance(data, dict) else {"_raw": data}
        except Exception as e:  # noqa: BLE001
            log.warning("state.json parse failed: %s", e)
    return FunasrRunResult(
        srt_path=output_srt, raw_json=raw_json,
        elapsed_sec=elapsed, stderr_tail=stderr_tail,
        returncode=proc.returncode,
    )


# ---------- Stage 2 ----------

def _diagnose_extract_error(returncode: int, stderr_tail: str) -> str:
    """REQ-20261016-125：把 funasr 子进程崩溃翻译成用户可行动的中文提示。

    实测两类崩溃同源（内存耗尽）：``_ArrayMemoryError`` 退出码 1（numpy
    分配失败），以及原生层 0xC0000005 退出码 3221225477（临界时原生分配
    直接访问冲突）。长视频整段音频加载是数 GB 级分配，系统提交内存不足
    即触发。
    """
    tail = stderr_tail or ""
    if "Unable to allocate" in tail or "_ArrayMemoryError" in tail or "MemoryError" in tail:
        return "（内存不足：长视频音频加载需要数 GB 内存，请关闭其他占内存的程序后重试）"
    if returncode == 3221225477:
        return "（原生层崩溃 0xC0000005：多为内存耗尽所致，请关闭其他占内存的程序后重试）"
    return ""


def run_stage2(
    repo_root: Path | str,
    task_id: str,
    project_id: str,
    *,
    model: str = "paraformer",
    lang: str = "zh",
    hotwords: str = "",
    sd_switch: str = "no",
) -> dict:
    """Stage 2: 对源视频跑 funasr → ``stage2/raw.srt`` + ``stage2/raw.json``。

    源视频 = ``project.base_material_id`` 对应的素材。
    返回最新 pipeline.stage2_extract 状态。
    """
    root = Path(repo_root)
    project = svc.load_project(root, task_id, project_id)
    src_id = project.get("base_material_id") or ""
    src = next(
        (m for m in project.get("materials") or []
         if m.get("id") == src_id and m.get("kind") == "video"),
        None,
    )
    if not src:
        raise svc.ShortVideoError("Stage 2 需要先在 Stage 1 选源视频")

    state = svc.get_stage_state(project, "stage2_extract")
    state["status"] = "running"
    state["started_at"] = svc.now_iso()
    state["model"] = model
    state["lang"] = lang
    # REQ-20261015-123：热词落盘（存用户原始输入，归一在 _run_funasr_extract
    # 内做），UI 重渲染时回填输入框，否则提取完一刷新就丢。
    state["hotwords"] = hotwords
    state["logs"] = list(state.get("logs") or []) + [
        f"[{svc.now_iso()}] stage2 start, model={model} lang={lang}",
    ]
    svc.set_stage_state(project, "stage2_extract", state)
    svc.save_project(root, project)

    # REQ-20261022-144：写执行日志（字幕提取）
    exec_id = ""
    try:
        from slirn_home import execution_history
        exec_id = execution_history.record_start(
            svc.sv_logs_dir(root, task_id),
            execution_history.KIND_SHORT_VIDEO_EXTRACT,
            extra={"project_id": project_id, "model": model, "lang": lang,
                   "hotwords": hotwords, "sd_switch": sd_switch},
        )
    except Exception:  # noqa: BLE001
        pass

    srt_path = svc.stage_raw_srt_path(root, task_id, project_id)
    input_video = svc.material_abs_path(root, project, src["id"])
    try:
        result = _run_funasr_extract(
            input_video=input_video, output_srt=srt_path,
            repo_root=root, model=model, lang=lang,
            hotwords=hotwords, sd_switch=sd_switch,
        )
    except Exception as e:  # noqa: BLE001
        try:
            from slirn_home import execution_history
            execution_history.record_finish(
                svc.sv_logs_dir(root, task_id), exec_id,
                success=False, error=str(e))
        except Exception:  # noqa: BLE001
            pass
        raise

    state = svc.get_stage_state(project, "stage2_extract")
    if result.returncode != 0 or not result.srt_path.is_file() or result.srt_path.stat().st_size < 4:
        state["status"] = "failed"
        state["finished_at"] = svc.now_iso()
        state["error"] = f"funasr 退出码 {result.returncode}" + _diagnose_extract_error(
            result.returncode, result.stderr_tail)
        state["stderr_tail"] = result.stderr_tail
        state["elapsed_sec"] = result.elapsed_sec
    else:
        raw_json_path = svc.stage_raw_json_path(root, task_id, project_id)
        if result.raw_json is not None:
            raw_json_path.write_text(
                json.dumps(result.raw_json, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        state["status"] = "done"
        state["finished_at"] = svc.now_iso()
        state["srt_path"] = svc._rel(root, srt_path)
        state["raw_json_path"] = svc._rel(root, raw_json_path) if result.raw_json else ""
        state["srt_chars"] = len(srt_path.read_text(encoding="utf-8", errors="replace"))
        state["elapsed_sec"] = result.elapsed_sec
        # REQ-20261004-bugfix：清掉上一次失败的 error/stderr_tail，避免 UI 显示陈旧报错。
        state.pop("error", None)
        state.pop("stderr_tail", None)
        # Stage 2 成功 → Stage 3-6 全部作废（除非已 done）
        svc.reset_downstream_stages(project, "stage2_extract")
    svc.set_stage_state(project, "stage2_extract", state)
    svc.save_project(root, project)
    # REQ-20261022-144：回填执行日志（成功/失败 + 耗时）
    try:
        from slirn_home import execution_history
        execution_history.record_finish(
            svc.sv_logs_dir(root, task_id), exec_id,
            success=(state.get("status") == "done"),
            error=str(state.get("error") or ""),
        )
    except Exception:  # noqa: BLE001
        pass
    return state


# REQ-20261016-148：Stage 6 字幕优化阶段整体移除 —
# run_stage5_one（粗剪 mp4 重 ASR）与 save_stage6_refined（编辑保存）随之删除。
# 老项目 stage5/refined_NN*.srt 磁盘文件保留，精剪走文件回退链读取。


# ---------- CLI ----------

def _cli_stage2() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Stage 2: extract subtitle")
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--model", default="paraformer")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--hotwords", default="")
    ap.add_argument("--sd-switch", choices=["yes", "no"], default="no")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = run_stage2(
        args.repo_root, args.task_id, args.project_id,
        model=args.model, lang=args.lang,
        hotwords=args.hotwords, sd_switch=args.sd_switch,
    )
    print(json.dumps({k: v for k, v in state.items() if k != "logs"}, ensure_ascii=False, indent=2))
    return 0 if state.get("status") == "done" else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p2 = sub.add_parser("stage2")
    p2.add_argument("--repo-root", required=True)
    p2.add_argument("--task-id", required=True)
    p2.add_argument("--project-id", required=True)
    p2.add_argument("--model", default="paraformer")
    p2.add_argument("--lang", default="zh")
    p2.add_argument("--hotwords", default="")
    p2.add_argument("--sd-switch", choices=["yes", "no"], default="no")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # REQ-20261016-148：stage5 子命令随字幕优化阶段移除，仅保留 stage2
    state = run_stage2(
        args.repo_root, args.task_id, args.project_id,
        model=args.model, lang=args.lang,
        hotwords=args.hotwords, sd_switch=args.sd_switch,
    )
    print(json.dumps({k: v for k, v in state.items() if k != "logs"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if state.get("status") == "done" else 1)