# -*- coding: utf-8 -*-
"""REQ-20261030-015：/slirn/api/* JSON 响应 gzip 压缩（纯 ASGI 中间件）。

工作台 / 短视频项目页的 HTML 响应可达 13MB（内嵌数千字幕/剪辑行），
未压缩时本机要白白写 13MB、远程（--listen）访问更是数秒起步；压缩后
~1MB。全仓 /slirn/api 无流式端点（无 StreamingResponse/SSE），缓冲整包
再压是安全的。

规则（宁缺毋滥 —— 视频流绝不动）：
- 只处理 /slirn/api/ 开头的路径，且**排除 /slirn/api/video/**（Range 视频流，
  206 分段、拖进度条依赖，压缩/缓冲都会破坏）
- 仅 200 响应、无既有 content-encoding、方法非 HEAD
- 客户端 Accept-Encoding 含 gzip
- content-type 须是 json / html / text / javascript / css 之一
- 缓冲完整 body 后统一压（API 响应都是一次性 JSON，最大 ~13MB）；
  小于 64KB 不压（得不偿失，原样直发）

挂载方式与 auth.install_auth_middleware 同款：launch.py 在 Gradio 6
launch() 重建 app 后调 install_gzip_middleware（user_middleware 头插 +
强制重建 middleware_stack，两条启动路径都要调）。
"""
from __future__ import annotations

import gzip
import logging

log = logging.getLogger(__name__)

_COMPRESSIBLE_CT = ("json", "html", "text/", "javascript", "css")
_MIN_SIZE = 64 * 1024  # 小于 64KB 不值得压


class SlirnGZipMiddleware:
    """缓冲式 gzip（见模块 docstring 的取舍）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if (not path.startswith("/slirn/api/")
                or path.startswith("/slirn/api/video/")
                or scope.get("method") == "HEAD"):
            await self.app(scope, receive, send)
            return
        accept = ""
        for k, v in scope.get("headers", []):
            if k.decode("latin-1").lower() == "accept-encoding":
                accept = v.decode("latin-1").lower()
                break
        if "gzip" not in accept:
            await self.app(scope, receive, send)
            return

        # 扣下 start + 攒齐 body，再决定压或原样重放
        state = {"status": 0, "headers": [], "chunks": []}

        async def _send(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = list(message.get("headers") or [])
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            state["chunks"].append(message.get("body") or b"")
            if message.get("more_body", False):
                return
            raw = b"".join(state["chunks"])
            hdrs = state["headers"]
            lower = [(k.lower(), v) for k, v in hdrs]
            ct = next((v for k, v in lower if k == b"content-type"), b"")
            ct_s = ct.decode("latin-1", "replace").lower()
            has_ce = any(k == b"content-encoding" for k, _ in lower)
            use_gz = (
                state["status"] == 200
                and not has_ce
                and len(raw) >= _MIN_SIZE
                and any(t in ct_s for t in _COMPRESSIBLE_CT)
            )
            if use_gz:
                gz = gzip.compress(raw, 6)
                if len(gz) < len(raw):
                    hdrs = [(k, v) for k, v in hdrs
                            if k.lower() not in (b"content-length",
                                                 b"content-encoding")]
                    hdrs.append((b"content-encoding", b"gzip"))
                    hdrs.append((b"content-length", str(len(gz)).encode()))
                    hdrs.append((b"vary", b"accept-encoding"))
                    await send({"type": "http.response.start",
                                "status": state["status"], "headers": hdrs})
                    await send({"type": "http.response.body", "body": gz})
                    return
            # 不压 —— 原样重放（含原 content-length）
            await send({"type": "http.response.start",
                        "status": state["status"], "headers": hdrs})
            await send({"type": "http.response.body", "body": raw})

        await self.app(scope, receive, _send)


def install_gzip_middleware(app) -> None:
    """往 app.user_middleware 头插 gzip 中间件并强制重建 middleware_stack。

    与 slirn_home.auth.install_auth_middleware 同一挂法：Gradio 6 launch()
    重建 app 后 user_middleware 为空、middleware_stack 未建，直接插入即可；
    恢复路径（launch 抛错后 uvicorn 已起）需要手动重建一次。
    头插 = 最外层：auth 等内层中间件的响应在此统一压缩。
    """
    from starlette.middleware import Middleware as _Middleware

    if any(m.cls is SlirnGZipMiddleware for m in app.user_middleware):
        return  # 幂等（两条启动路径都调时防重复）
    app.user_middleware.insert(0, _Middleware(SlirnGZipMiddleware))
    try:
        app.middleware_stack = app.build_middleware_stack()
    except Exception:  # noqa: BLE001 — 老版本 Starlette 无公开 build
        if hasattr(app, "_middleware_stack"):
            app._middleware_stack = None
