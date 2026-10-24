"""IP 登录失败封禁 — REQ-20261019-224。

设计文档：design/DESIGN-20261019-224-ip-login-ban.md

- 同一 IP 滚动 30 分钟窗口内登录失败（凭据错误 401）达 5 次 → 封禁 30 分钟，
  期间拒绝该 IP 的**所有** HTTP 访问并反馈原因（NAT/账号均不区分）。
- 状态落盘 data/ip_guard.json（原子写，重启不失效）；单进程内存态 + 变更时写盘。
- IP 口径 = 直连 socket 对端；SLIRN_TRUST_PROXY=1 时取 X-Forwarded-For 最右条目。
- 回环（服务器本机，REQ-226 收窄豁免）：**不走全站拦截**（进程内 pipeline
  调度器从 127.0.0.1 自调 API，全站封=自残），但登录失败按账号区分 —
  **管理员账号豁免不计数；非管理员账号照常计数/提示剩余次数/封禁**（回环
  封禁只拦登录端点，管理员登录与调度器不受影响；政策在 auth_routes.login，
  守卫本身纯机械记账）。
- 阈值/窗口 env 可覆盖（测试提速）：SLIRN_IPGUARD_MAX_FAILURES /
  SLIRN_IPGUARD_WINDOW_SEC / SLIRN_IPGUARD_BAN_SEC（每次现读，与
  SLIRN_AUTH_BYPASS 同模式）。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse

log = logging.getLogger(__name__)

# 回环集（归一化后比对）— 全站拦截豁免（调度器自调用自保护）；登录计数
# 豁免仅限管理员账号（REQ-226，政策在 auth_routes.login）
_LOOPBACK = {"127.0.0.1", "::1"}


def _now() -> float:
    return time.time()


def _cfg_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _cfg_max_failures() -> int:
    return _cfg_int("SLIRN_IPGUARD_MAX_FAILURES", 5)


def _cfg_window_sec() -> int:
    return _cfg_int("SLIRN_IPGUARD_WINDOW_SEC", 1800)


def _cfg_ban_sec() -> int:
    return _cfg_int("SLIRN_IPGUARD_BAN_SEC", 1800)


def normalize_ip(raw) -> str:
    """归一化：小写 + IPv6-mapped IPv4（::ffff:1.2.3.4）→ 1.2.3.4。"""
    ip = str(raw or "").strip().lower()
    if ip.startswith("::ffff:") and "." in ip:
        ip = ip[7:]
    return ip


def is_loopback(ip: str) -> bool:
    return normalize_ip(ip) in _LOOPBACK


def extract_client_ip(scope: dict) -> str:
    """从 ASGI scope 取客户端 IP（计数与拦截共用同一口径）。

    默认直连对端 scope["client"][0]；SLIRN_TRUST_PROXY=1 时优先取
    X-Forwarded-For **最右**条目（由可信反向代理追加，左边的可伪造）。
    拿不到 → 空串（调用方跳过计数/拦截 — 无法归因，宁可不误伤）。
    """
    if os.environ.get("SLIRN_TRUST_PROXY") == "1":
        for k, v in scope.get("headers", []):
            if k == b"x-forwarded-for":
                entries = [e.strip() for e in v.decode("latin-1").split(",")
                           if e.strip()]
                if entries:
                    return normalize_ip(entries[-1])
    client = scope.get("client")
    if client and client[0]:
        return normalize_ip(client[0])
    return ""


# ===================== IPGuard（状态存取） =====================

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:                 # noqa: BLE001
        log.warning("损坏 JSON 读失败 %s: %s — 回退默认", path, e)
        return default


def _save_json(path: Path, data) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


class IPGuard:
    """登录失败计数 + 封禁状态（扁平文件，照抄 auth.py 存储惯例）。

    记录结构 data/ip_guard.json：
    ``{"<ip>": {"failures": [ts…], "banned_until": ts|null, "banned_at": ts,
    "ban_count": n}}``
    单进程单实例：内存态懒加载、变更即写盘（登录失败频次极低，无性能问题）。
    """

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root).resolve()
        self.path = self.repo_root / "data" / "ip_guard.json"
        self._data: dict | None = None

    # ---------- 内部 ----------

    def _records(self, *, prune: bool = True) -> dict:
        if self._data is None:
            self._data = _load_json(self.path, {})
            if not isinstance(self._data, dict):
                self._data = {}
        if prune:
            self._prune()
        return self._data

    def _prune(self) -> None:
        """滚动窗口剔除旧失败；封禁到期/空记录整键删除。变更才写盘。"""
        now = _now()
        window = _cfg_window_sec()
        changed = False
        for ip in list(self._data.keys()):
            rec = self._data[ip]
            if not isinstance(rec, dict):
                del self._data[ip]
                changed = True
                continue
            failures = [t for t in (rec.get("failures") or [])
                        if now - float(t or 0) < window]
            if failures != rec.get("failures"):
                rec["failures"] = failures
                changed = True
            banned_until = float(rec.get("banned_until") or 0)
            if banned_until and banned_until <= now:
                del self._data[ip]          # 封禁到期 → 整体清除（全新开始）
                changed = True
            elif not failures and not banned_until:
                del self._data[ip]          # 无失败无封禁 → 空记录
                changed = True
        if changed:
            _save_json(self.path, self._data)

    # ---------- 对外 ----------

    def record_failure(self, ip: str) -> dict:
        """记 1 次登录失败。返 {banned, failures, banned_until, ban_count}。

        纯机械记账（回环也照记 — 豁免政策在调用方 login 端点按账号类型
        决定，REQ-226）；空 IP / 已封禁不计（已封禁走拦截层，到不了这里 —
        防御性直接返回现状）。
        """
        ip = normalize_ip(ip)
        if not ip:
            return {"banned": False, "failures": 0, "banned_until": None,
                    "ban_count": 0}
        data = self._records()
        rec = data.get(ip)
        if rec is None or not isinstance(rec, dict):
            rec = {"failures": [], "banned_until": None, "banned_at": None,
                   "ban_count": 0}
            data[ip] = rec
        if float(rec.get("banned_until") or 0) > _now():
            return {"banned": True,
                    "failures": len(rec.get("failures") or []),
                    "banned_until": rec.get("banned_until"),
                    "ban_count": rec.get("ban_count", 0)}
        rec["failures"] = list(rec.get("failures") or []) + [_now()]
        banned = len(rec["failures"]) >= _cfg_max_failures()
        if banned:
            rec["banned_until"] = _now() + _cfg_ban_sec()
            rec["banned_at"] = _now()
            rec["ban_count"] = int(rec.get("ban_count") or 0) + 1
            log.warning("IP %s 登录失败达 %d 次，封禁至 %s", ip,
                        _cfg_max_failures(),
                        time.strftime("%H:%M:%S",
                                      time.localtime(rec["banned_until"])))
        _save_json(self.path, data)
        return {"banned": banned, "failures": len(rec["failures"]),
                "banned_until": rec.get("banned_until") if banned else None,
                "ban_count": rec.get("ban_count", 0)}

    def clear_failures(self, ip: str) -> None:
        """登录成功 → 清零该 IP 的记录（整键删除，含封禁中）。

        REQ-226：回环+管理员账号在封禁期内也能登对（豁免通道）→ 清整个
        记录 = 管理员的自救解封。外部封禁 IP 到不了登录端点（中间件先拦），
        语义不变。
        """
        ip = normalize_ip(ip)
        if not ip:
            return
        data = self._records()
        if ip in data:
            del data[ip]
            _save_json(self.path, data)

    def is_banned(self, ip: str) -> tuple[bool, dict | None]:
        """返 (是否封禁中, 记录)。纯查表 — 豁免判断在调用方（中间件只查
        非回环；登录端点对回环按账号豁免，REQ-226）。"""
        ip = normalize_ip(ip)
        if not ip:
            return False, None
        rec = self._records().get(ip)
        if not isinstance(rec, dict):
            return False, None
        if float(rec.get("banned_until") or 0) > _now():
            return True, rec
        return False, None

    def lift(self, ip: str) -> bool:
        """admin 解封：整键删除。返是否真删了。"""
        ip = normalize_ip(ip)
        if not ip:
            return False
        data = self._records()
        if ip in data:
            del data[ip]
            _save_json(self.path, data)
            log.info("IP %s 已被 admin 解封", ip)
            return True
        return False

    def list_records(self) -> list[dict]:
        """admin 观测用快照（已 prune）。"""
        data = self._records()
        now = _now()
        out = []
        for ip, rec in sorted(data.items()):
            if not isinstance(rec, dict):
                continue
            out.append({
                "ip": ip,
                "failures": len(rec.get("failures") or []),
                "banned": bool(float(rec.get("banned_until") or 0) > now),
                "banned_until": rec.get("banned_until"),
                "ban_count": int(rec.get("ban_count") or 0),
            })
        return out


# ===================== 拒绝响应（共用） =====================

def _ban_parts(record: dict | None) -> tuple[str, int]:
    """(原因文字, 剩余秒)。"""
    until = float((record or {}).get("banned_until") or 0) or (_now() + _cfg_ban_sec())
    retry = max(0, int(until - _now()))
    until_str = time.strftime("%H:%M:%S", time.localtime(until))
    win_min = max(1, round(_cfg_window_sec() / 60))
    msg = (f"该 IP 因 {win_min} 分钟内 {_cfg_max_failures()} 次登录失败"
           f"被暂时封禁，{until_str} 后自动恢复")
    return msg, retry


def banned_response(record: dict | None, *, api: bool):
    """封禁拒绝响应：API → JSON 403；其他 → 可读 HTML 403。均带 Retry-After。"""
    msg, retry = _ban_parts(record)
    headers = {"Retry-After": str(retry)}
    if api:
        return JSONResponse(
            {"ok": False, "code": "ip_banned", "error": msg,
             "banned_until": time.strftime(
                 "%Y-%m-%dT%H:%M:%S", time.localtime(
                     float((record or {}).get("banned_until") or 0) or _now())),
             "retry_after_sec": retry},
            status_code=403, headers=headers)
    esc = (msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>403 访问被拒绝"
        f"</title></head><body style='font-family:sans-serif;padding:40px'>"
        f"<h1>403 访问被拒绝</h1><p>{esc}</p></body></html>",
        status_code=403, headers=headers)


# ===================== 中间件 =====================

class IPGuardMiddleware:
    """封禁拦截（装在 SlirnAuthMiddleware 外层，覆盖所有 HTTP 路径）。

    纯 ASGI（不用 BaseHTTPMiddleware — contextvar 教训见 auth.py）。
    封禁中的一切 HTTP 请求（含登录/静态/Gradio 路由）→ 403 + 原因；
    websocket scope → accept 后 close(1008)（防御性，Gradio 主用 SSE）。
    回环永不走全站拦截（调度器自调 + 管理员豁免通道，REQ-226）：回环上
    非管理员账号的登录封禁由 auth_routes.login 端点自己执行。
    """

    def __init__(self, app, guard: IPGuard):
        self.app = app
        self.guard = guard

    async def __call__(self, scope, receive, send):
        stype = scope.get("type")
        if stype not in ("http", "websocket"):
            await self.app(scope, receive, send)   # lifespan 等直通
            return
        ip = extract_client_ip(scope)
        banned, rec = (False, None)
        if ip and not is_loopback(ip):
            banned, rec = self.guard.is_banned(ip)
        if not banned:
            await self.app(scope, receive, send)
            return
        if stype == "websocket":
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1008,
                        "reason": "ip banned"})
            return
        resp = banned_response(rec, api=str(scope.get("path", "")).startswith("/slirn/api/"))
        await resp(scope, receive, send)


def install_ip_guard_middleware(app, guard: IPGuard) -> None:
    """挂到 user_middleware 最外层（在 install_auth_middleware **之后**调用：
    后 insert(0) 者在外）。同样处理 Gradio 6 launch 重建 + 恢复路径的
    middleware_stack 强制重建（详见 auth.install_auth_middleware 注释）。"""
    from starlette.middleware import Middleware as _Middleware
    app.user_middleware.insert(0, _Middleware(IPGuardMiddleware, guard=guard))
    try:
        app.middleware_stack = app.build_middleware_stack()
    except Exception:                       # noqa: BLE001
        if hasattr(app, "_middleware_stack"):
            app._middleware_stack = None
