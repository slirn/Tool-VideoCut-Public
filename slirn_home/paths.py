"""路径解析 — 单根模型：代码根 = 数据根 = 仓库根。

2026-10-21 合并 FunClip-main + slirn-standalone 为单一工程后：
`tasklib/` 已并入仓库根，`data/ config/ tasks/ hotwords/` 也落在仓库根，
因此不再需要「sibling 目录 / submodule mount / 环境变量」三路探测。

保留 `find_slirn_standalone_root` 作为向后兼容别名（旧调用点无需改），
新代码一律用 `find_repo_root`。
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_repo_root() -> Path:
    """本工程仓库根（含 slirn_home/ 与 tasklib/）。"""
    return Path(__file__).resolve().parent.parent


def find_slirn_standalone_root() -> Path:
    """兼容别名 — 合并后与仓库根同为一个目录。

    历史语义是「slirn-standalone 仓库根（tasklib + 运行时数据）」，
    合并后该目录已被并入仓库根，故直接返回 find_repo_root()。
    """
    return find_repo_root()


def ensure_tasklib_importable() -> Path:
    """把仓库根加到 sys.path（tasklib 已是顶层包），返回该路径。"""
    root = find_repo_root()
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root
