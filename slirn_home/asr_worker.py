"""ASR 子进程 worker — REQ-20261105-001（P1-3/4 内存隔离）。

由 ``asr_service._run_recognition_subprocess`` 以
``python -m slirn_home.asr_worker`` 启动，在独立进程里跑同一条识别链路
（``asr_service._run_recognition``）：torch/funasr/librosa 的 ~1-2GB 常驻与
模型加载峰值都被限制在本进程内，跑完进程退出、内存全部归还 —— 8GB 小内存
部署机上不再把系统 commit 顶满、掐断 HTTP 连接。

约定：

- 结果以 state JSON 写入 ``--out`` 指定文件（父进程负责回读与错误归类）。
- numpy 标量（如 speakers 统计里的 int64）转 python 原生类型后再序列化。
- 本模块 import 时机刻意延迟：``asr_service`` import 即挂 funclip sys.path，
  真正的 torch/funasr 加压发生在 ``_run_recognition`` 内部 —— 与进程内
  慢路径完全一致。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _json_default(o):
    """numpy 标量 → python 原生；其余对象兜底 str。"""
    item = getattr(o, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:  # noqa: BLE001
            pass
    return str(o)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ASR 识别子进程（slirn_home 内部用）")
    ap.add_argument("--video", help="待识别视频路径（单发模式必填）")
    ap.add_argument("--hotwords", default="", help="热词（逗号分隔）")
    ap.add_argument("--sd", choices=("0", "1"), default="1", help="是否说话人分离")
    ap.add_argument("--out", help="state JSON 输出路径（单发模式必填）")
    # 窗口化模式（REQ-20261107-003）：输入/输出都走工作目录（request.json/state.json）
    ap.add_argument("--pipeline", action="store_true", help="启用窗口化阶段机")
    ap.add_argument("--work-dir", help="asr_work 工作目录（--pipeline 必填）")
    ap.add_argument("--idx", type=int, default=0, help="worker 序号（0..N-1）")
    args = ap.parse_args(argv)

    if args.pipeline:
        if not args.work_dir:
            ap.error("--pipeline 需要 --work-dir")
        from slirn_home.asr_pipeline import run_pipeline

        return run_pipeline(Path(args.work_dir), args.idx)

    if not args.video or not args.out:
        ap.error("单发模式需要 --video 与 --out")
    # 延迟 import：见模块 docstring
    from slirn_home import asr_service

    try:
        state = asr_service._run_recognition(args.video, args.hotwords, args.sd == "1")
    except Exception as e:  # noqa: BLE001 — 原样冒泡给父进程（stderr 归类）
        print(f"[asr_worker] recognition failed: {e}", file=sys.stderr)
        raise
    Path(args.out).write_text(
        json.dumps(state, ensure_ascii=False, default=_json_default),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
