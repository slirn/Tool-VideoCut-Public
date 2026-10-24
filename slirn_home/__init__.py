"""slirn_home — Slirn 自定义首页 Gradio Blocks。"""

from slirn_home.app import (
    _register_slirn_api,
    build_app,
    slirn_home_static_css,
    slirn_home_static_head,
)
from slirn_home.paths import (
    ensure_tasklib_importable,
    find_repo_root,
    find_slirn_standalone_root,
)

__all__ = [
    "_register_slirn_api",
    "build_app",
    "ensure_tasklib_importable",
    "find_repo_root",
    "find_slirn_standalone_root",
    "slirn_home_static_css",
    "slirn_home_static_head",
]
