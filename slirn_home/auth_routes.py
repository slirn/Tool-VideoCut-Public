"""REQ-20260926-NNN：用户管理 + 会话 + 项目成员 端点。

全部用 @app.app.post/get 散挂（与现有风格一致）；
中间件已在白名单放行 /auth/login /auth/logout /auth/me /auth/bootstrap，
其他端点要求中间件已注入 slirn_user。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import Body
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request as _StarletteRequest

from slirn_home.auth import AuthStore, get_current_user_from_context
from slirn_home.ip_guard import (IPGuard, banned_response, extract_client_ip,
                                 is_loopback)

log = logging.getLogger(__name__)


def _set_session_cookie(response: JSONResponse, token: str, max_age: int = 30 * 86400) -> None:
    """Set-Cookie: slirn_session=...; HttpOnly; SameSite=Lax; Path=/"""
    response.set_cookie(
        key="slirn_session",
        value=token,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        # secure=False — 本地 HTTP 部署；HTTPS 时再开
    )


def _clear_session_cookie(response: JSONResponse) -> None:
    response.delete_cookie("slirn_session", path="/")


_LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VideoCut — 登录</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: linear-gradient(160deg, #10141c 0%, #1a2233 100%);
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; color: #e8ecf2;
  }
  .card {
    width: 340px; padding: 34px 30px; border-radius: 14px;
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.09);
    box-shadow: 0 12px 40px rgba(0,0,0,0.45);
  }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { font-size: 12px; color: #8b96a8; margin-bottom: 22px; }
  label { display: block; font-size: 12px; color: #a7b2c4; margin: 12px 0 6px; }
  input {
    width: 100%; padding: 10px 12px; border-radius: 8px; font-size: 14px;
    border: 1px solid rgba(255,255,255,0.14); background: rgba(0,0,0,0.28); color: #e8ecf2;
    outline: none; transition: border-color .15s;
  }
  input:focus { border-color: #4f8ef7; }
  button {
    width: 100%; margin-top: 20px; padding: 11px 0; border: 0; border-radius: 8px;
    font-size: 15px; cursor: pointer; color: #fff;
    background: linear-gradient(90deg, #3b7ddd, #3568c9); transition: filter .15s;
  }
  button:hover { filter: brightness(1.1); }
  .hint { min-height: 18px; margin-top: 12px; font-size: 12px; color: #ff8d8d; }
  .hint.ok { color: #7ede9c; }
  .brand { text-align: center; margin-bottom: 18px; font-size: 30px; }
  .alt { margin-top: 14px; text-align: center; }
  .alt a { color: #6fa8ff; font-size: 12px; cursor: pointer; text-decoration: none; }
  .alt a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="card">
  <div class="brand">&#127916;</div>
  <h1>VideoCut</h1>
  <div class="sub">本地视频自动剪辑工作台 — 请登录后继续</div>

  <div id="login-form" style="display:none;">
    <label>用户名</label>
    <input id="u" autocomplete="username" placeholder="用户名">
    <label>密码</label>
    <input id="p" type="password" autocomplete="current-password" placeholder="密码">
    <button id="go">登 录</button>
    <div class="hint" id="hint"></div>
  </div>

  <div id="boot-form" style="display:none;">
    <label>创建管理员 · 用户名</label>
    <input id="bu" autocomplete="username" placeholder="管理员用户名">
    <label>创建管理员 · 密码（至少 4 位）</label>
    <input id="bp" type="password" autocomplete="new-password" placeholder="密码">
    <button id="bgo">创建并进入</button>
    <div class="hint" id="bhint">首次使用：尚无任何用户，请先创建管理员。</div>
  </div>

  <div class="hint" id="loading">正在检查登录状态…</div>
</div>
<script>
(function () {
  var API = '/slirn/api';
  var loading = document.getElementById('loading');
  var loginForm = document.getElementById('login-form');
  var bootForm = document.getElementById('boot-form');

  function show(el) {
    loading.style.display = 'none';
    el.style.display = '';
  }

  function post(path, body) {
    return fetch(API + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) { j._status = r.status; return j; });
    });
  }

  function enter() { location.href = '/'; }

  function doLogin() {
    var u = document.getElementById('u').value.trim();
    var p = document.getElementById('p').value;
    var hint = document.getElementById('hint');
    hint.className = 'hint';
    hint.textContent = '';
    if (!u || !p) { hint.textContent = '用户名和密码必填'; return; }
    post('/auth/login', { username: u, password: p }).then(function (r) {
      if (r.ok) enter();
      else hint.textContent = '登录失败：' + (r.error || '未知错误') + (r._status ? ' [HTTP ' + r._status + ']' : '');
    }).catch(function (e) {
      hint.textContent = '网络错误：' + e;
    });
  }

  function doBootstrap() {
    var u = document.getElementById('bu').value.trim();
    var p = document.getElementById('bp').value;
    var hint = document.getElementById('bhint');
    hint.className = 'hint';
    if (!u || p.length < 4) { hint.textContent = '用户名必填，密码至少 4 位'; return; }
    post('/auth/bootstrap', { username: u, password: p }).then(function (r) {
      if (r.ok) enter();
      else hint.textContent = '创建失败：' + (r.error || '未知错误') + (r._status ? ' [HTTP ' + r._status + ']' : '');
    }).catch(function (e) {
      hint.textContent = '网络错误：' + e;
    });
  }

  document.getElementById('go').addEventListener('click', doLogin);
  document.getElementById('bgo').addEventListener('click', doBootstrap);
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') {
      if (loginForm.style.display !== 'none') doLogin();
      else if (bootForm.style.display !== 'none') doBootstrap();
    }
  });

  // 已登录 → 直接进工作台；否则按 bootstrap 状态展示对应表单
  fetch(API + '/auth/me').then(function (r) { return r.json(); }).then(function (me) {
    // /auth/me 匿名时也返回 {ok:true, user:null} —— 判 user 而不是 ok（评审 R1）
    if (me && me.ok && me.user) { enter(); return; }
    fetch(API + '/auth/bootstrap').then(function (r) { return r.json(); }).then(function (bs) {
      if (bs && bs.ok && bs.needs_bootstrap) show(bootForm);
      else show(loginForm);
    }).catch(function () { show(loginForm); });
  }).catch(function () { show(loginForm); });
})();
</script>
</body>
</html>
"""


def _extract_cookie_token(scope: dict) -> str:
    """从 ASGI scope 抠 slirn_session cookie（logout 记日志用）。"""
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == "slirn_session":
                    return val
    return ""


def _login_fail_response(remain: int) -> "JSONResponse":
    """REQ-20261021-141：登录失败统一反馈 —— 剩余次数 + 锁定时长。

    三条失败路径（ip_guard 计数 / 回环管理员豁免 / 无守卫）共用，
    防止文案漂移。封禁态不在此列（banned_response 带精确解封时间，信息更全）。
    """
    from slirn_home.ip_guard import _cfg_ban_sec
    ban_min = max(1, round(_cfg_ban_sec() / 60))
    if remain > 0:
        msg = f"用户名或密码错误（还剩 {remain} 次机会；用尽后锁定 {ban_min} 分钟）"
    else:
        msg = f"用户名或密码错误（机会已用尽，锁定 {ban_min} 分钟）"
    return JSONResponse({"ok": False, "error": msg}, status_code=401)


def register_auth_endpoints(app: Any, auth: AuthStore, repo_root: Path,
                            ip_guard: IPGuard | None = None) -> None:
    """挂 /slirn/api/auth/* 端点到 app.app。

    ip_guard（可选，REQ-20261019-224）：传入则登录失败计数 / 封禁生效；
    不传（旧测试兼容）时登录行为与从前一致。
    """

    # ---------- 登录（公开） ----------

    async def login(request: _StarletteRequest,
                    body: dict = Body(default_factory=dict)):
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return JSONResponse(
                {"ok": False, "error": "用户名和密码必填"}, status_code=400
            )
        ip = ""
        exempt = False
        if ip_guard is not None:
            ip = extract_client_ip(request.scope)
            if ip:
                # REQ-226：回环（服务器本机）仅管理员账号豁免；非管理员账号
                # 照常计数/提示剩余次数/封禁（封禁只拦登录，不全站拦截）
                admin_try = bool((auth.get_user(username) or {}).get("is_admin"))
                exempt = is_loopback(ip) and admin_try
                banned, _rec = ip_guard.is_banned(ip)
                if banned and not exempt:
                    # 封禁期内的登录尝试在计数前就被拒（不续期）
                    auth.record_login_event(username, ip, "fail", "IP 封禁期内被拒")
                    return banned_response(_rec, api=True)
        user = auth.verify_credentials(username, password)
        if user is None:
            if ip_guard is not None and ip and not exempt:
                res = ip_guard.record_failure(ip)
                if res.get("banned"):
                    # 第 5 次：当场告知已被封（含精确解封时间）
                    return banned_response(
                        {"banned_until": res.get("banned_until")}, api=True)
                from slirn_home.ip_guard import _cfg_max_failures
                remain = max(0, _cfg_max_failures() - int(res.get("failures") or 0))
                auth.record_login_event(
                    username, ip, "fail",
                    f"密码错误（还剩 {remain} 次机会）")
                return _login_fail_response(remain)
            # 豁免（回环+管理员账号）或无守卫：不计数，但仍反馈满额剩余次数
            # 与锁定时长（REQ-20261021-141：每次失败都要反馈）
            from slirn_home.ip_guard import _cfg_max_failures
            auth.record_login_event(username, ip, "fail", "密码错误（豁免路径，不计数）")
            return _login_fail_response(_cfg_max_failures())
        if ip_guard is not None and ip:
            # 登录成功清零该 IP 失败计数（打错几次后登对的正常人不清算；
            # 回环上管理员登对同样清零 — 天然的自救解封路径）
            ip_guard.clear_failures(ip)
        # REQ-20261021-142：单会话 —— 记录本次登录踢掉了几个旧会话
        old_sessions = auth.user_session_count(username)
        token = auth.create_session(username, ttl_days=30)
        kick_note = (f"同账号旧会话 {old_sessions} 个已失效"
                     if old_sessions > 0 else "")
        auth.record_login_event(username, ip, "success", kick_note)
        resp = JSONResponse({"ok": True, "user": user})
        _set_session_cookie(resp, token)
        log.info("登录: %s%s", username, f"（踢掉旧会话 {old_sessions} 个）" if kick_note else "")
        return resp

    # ---------- REQ-20261021-140：独立登录页（公开，自包含） ----------
    # 登录墙（SlirnAuthMiddleware）把未登录请求 302 到这里；页面不加载
    # 工作台任何资源（router.js / home.css / Gradio），公开面最小。
    async def login_page():
        return HTMLResponse(_LOGIN_PAGE_HTML)

    app.app.get("/login")(login_page)

    # bootstrap 状态（公开）：0 用户时登录页展示「创建首个管理员」。
    # 只暴露布尔值，不泄露用户名 / 数量。
    async def bootstrap_status():
        return {"ok": True, "needs_bootstrap": auth.count_users() == 0}

    app.app.get("/slirn/api/auth/bootstrap")(bootstrap_status)

    app.app.post("/slirn/api/auth/login")(login)

    # ---------- REQ-20261021-142：登录日志（需登录，仅本人） ----------
    async def login_log():
        user = get_current_user_from_context()
        username = (user or {}).get("username") or ""
        if not username:
            return JSONResponse(
                {"ok": False, "error": "未登录"}, status_code=401)
        events = auth.get_login_events(username, limit=50)
        return {"ok": True, "events": events}

    app.app.get("/slirn/api/auth/login_log")(login_log)

    # ---------- 登出（公开） ----------

    async def logout(request: _StarletteRequest,
                     body: dict = Body(default_factory=dict)):
        token = (body.get("token") or "").strip()
        if not token:
            # 从 cookie 删即可
            token = _extract_cookie_token(request.scope)
        logged_user = auth.user_from_token(token) if token else None
        if logged_user is not None:
            # REQ-20261021-142：登出也落登录日志（本人可见）
            auth.record_login_event(
                logged_user["username"],
                extract_client_ip(request.scope) or "", "logout")
        if token:
            auth.delete_session(token)
        resp = JSONResponse({"ok": True})
        _clear_session_cookie(resp)
        return resp

    app.app.post("/slirn/api/auth/logout")(logout)

    # ---------- 当前用户（公开：无登录返 user:null） ----------

    async def me(request: _StarletteRequest):                                # noqa: ARG001 — starlette Request
        # 中间件已挂白名单放行 /me，未登录 → user:null
        user = getattr(getattr(request, "state", None), "slirn_user", None)
        if user is None:
            # 兜底：直接从 cookie 读（无中间件时）
            from slirn_home.auth import SlirnAuthMiddleware
            token = None
            for k, v in request.scope.get("headers", []):
                if k == b"cookie":
                    for part in v.decode("latin-1").split(";"):
                        name, _, val = part.strip().partition("=")
                        if name == "slirn_session":
                            token = val
                            break
                    if token:
                        break
            user = auth.user_from_token(token) if token else None
        return {"ok": True, "user": user}

    app.app.get("/slirn/api/auth/me")(me)

    # ---------- bootstrap：仅无用户时可用 ----------

    async def bootstrap(body: dict = Body(default_factory=dict)):
        if auth.count_users() > 0:
            return JSONResponse(
                {"ok": False, "error": "系统已有用户，禁止 bootstrap"},
                status_code=403,
            )
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return JSONResponse(
                {"ok": False, "error": "用户名和密码必填"}, status_code=400
            )
        if len(password) < 4:
            return JSONResponse(
                {"ok": False, "error": "密码至少 4 位"}, status_code=400
            )
        try:
            user = auth.create_user(username, username, password, is_admin=True)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        token = auth.create_session(username, ttl_days=30)
        resp = JSONResponse({"ok": True, "user": {
            "username": user["username"],
            "display_name": user["display_name"],
            "is_admin": True,
        }})
        _set_session_cookie(resp, token)
        log.info("bootstrap 创建 admin: %s", username)
        return resp

    app.app.post("/slirn/api/auth/bootstrap")(bootstrap)

    # ---------- 用户管理（需登录；写操作需 admin） ----------

    async def list_users():
        users = auth.list_users()
        # 抹掉 salt/hash 字段，对外只暴露公开摘要
        safe = [{
            "username": u["username"],
            "display_name": u.get("display_name", u["username"]),
            "is_admin": u.get("is_admin", False),
            "created_at": u.get("created_at"),
        } for u in users]
        return {"ok": True, "users": safe}

    app.app.get("/slirn/api/auth/users")(list_users)

    async def create_user_endpoint(body: dict = Body(default_factory=dict)):
        me_user = get_current_user_from_context()
        if not me_user.get("is_admin"):
            return JSONResponse({"ok": False, "error": "需 admin 权限"},
                                status_code=403)
        username = (body.get("username") or "").strip()
        display_name = (body.get("display_name") or username).strip()
        password = body.get("password") or ""
        is_admin = bool(body.get("is_admin", False))
        if not username or not password:
            return JSONResponse(
                {"ok": False, "error": "用户名和密码必填"}, status_code=400
            )
        if len(password) < 4:
            return JSONResponse(
                {"ok": False, "error": "密码至少 4 位"}, status_code=400
            )
        try:
            u = auth.create_user(username, display_name, password, is_admin=is_admin)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "user": {
            "username": u["username"], "display_name": u["display_name"],
            "is_admin": u["is_admin"], "created_at": u["created_at"],
        }}

    app.app.post("/slirn/api/auth/users")(create_user_endpoint)

    async def delete_user_endpoint(username: str):
        me_user = get_current_user_from_context()
        if not me_user.get("is_admin"):
            return JSONResponse({"ok": False, "error": "需 admin 权限"},
                                status_code=403)
        if username == me_user.get("username"):
            return JSONResponse(
                {"ok": False, "error": "不能删除自己"}, status_code=400
            )
        ok = auth.delete_user(username)
        if not ok:
            return JSONResponse({"ok": False, "error": "用户不存在"}, status_code=404)
        return {"ok": True}

    app.app.delete("/slirn/api/auth/users/{username}")(delete_user_endpoint)

    async def change_password_endpoint(body: dict = Body(default_factory=dict)):
        me_user = get_current_user_from_context()
        target = (body.get("username") or me_user.get("username") or "").strip()
        new_pw = body.get("new_password") or ""
        # admin 可改任何人；非 admin 只能改自己
        if target != me_user.get("username") and not me_user.get("is_admin"):
            return JSONResponse({"ok": False, "error": "无权修改他人密码"},
                                status_code=403)
        if len(new_pw) < 4:
            return JSONResponse({"ok": False, "error": "密码至少 4 位"},
                                status_code=400)
        if not auth.update_user_password(target, new_pw):
            return JSONResponse({"ok": False, "error": "用户不存在"},
                                status_code=404)
        return {"ok": True}

    app.app.post("/slirn/api/auth/users/change_password")(change_password_endpoint)

    # ---------- 任务成员管理（需登录 + 是该任务成员/admin） ----------

    async def list_members_endpoint(tid: str):
        return {"ok": True, "members": auth.list_task_members(tid)}

    app.app.get("/slirn/api/auth/tasks/{tid}/members")(list_members_endpoint)

    async def add_member_endpoint(tid: str, body: dict = Body(default_factory=dict)):
        me_user = get_current_user_from_context()
        # 成员管理端点：必须 admin 或 创建者（成员加成员）
        from tasklib import TaskManager            # noqa: PLC0415
        from tasklib.exceptions import TaskNotFoundError  # noqa: PLC0415
        # 复用 slirn_home.app 内的 mgr（通过 repo_root 拿到 — 简化：从 TaskManager(repo_root) 构造）
        mgr = TaskManager(repo_root)
        try:
            task = mgr.get(tid)
        except TaskNotFoundError:
            return JSONResponse({"ok": False, "error": "任务不存在"},
                                status_code=404)
        creator = getattr(task, "created_by", None)
        if not (me_user.get("is_admin") or me_user.get("username") == creator):
            return JSONResponse(
                {"ok": False, "error": "需 admin 权限或是任务创建者"},
                status_code=403,
            )
        username = (body.get("username") or "").strip()
        if not username:
            return JSONResponse({"ok": False, "error": "username 必填"},
                                status_code=400)
        if auth.get_user(username) is None:
            return JSONResponse({"ok": False, "error": "用户不存在"},
                                status_code=404)
        added = auth.add_task_member(tid, username)
        return {"ok": True, "added": added, "members": auth.list_task_members(tid)}

    app.app.post("/slirn/api/auth/tasks/{tid}/members")(add_member_endpoint)

    async def remove_member_endpoint(tid: str, username: str):
        me_user = get_current_user_from_context()
        from tasklib import TaskManager            # noqa: PLC0415
        from tasklib.exceptions import TaskNotFoundError  # noqa: PLC0415
        mgr = TaskManager(repo_root)
        try:
            task = mgr.get(tid)
        except TaskNotFoundError:
            return JSONResponse({"ok": False, "error": "任务不存在"},
                                status_code=404)
        creator = getattr(task, "created_by", None)
        if not (me_user.get("is_admin") or me_user.get("username") == creator):
            return JSONResponse(
                {"ok": False, "error": "需 admin 权限或是任务创建者"},
                status_code=403,
            )
        removed = auth.remove_task_member(tid, username)
        return {"ok": True, "removed": removed, "members": auth.list_task_members(tid)}

    app.app.delete("/slirn/api/auth/tasks/{tid}/members/{username}")(remove_member_endpoint)

    # ---------- IP 封禁观测/解封（REQ-20261019-224，需 admin） ----------

    if ip_guard is not None:

        async def ip_guard_list():
            me_user = get_current_user_from_context()
            if not me_user.get("is_admin"):
                return JSONResponse({"ok": False, "error": "需 admin 权限"},
                                    status_code=403)
            return {"ok": True, "records": ip_guard.list_records()}

        app.app.get("/slirn/api/auth/ip_guard")(ip_guard_list)

        async def ip_guard_unban(body: dict = Body(default_factory=dict)):
            me_user = get_current_user_from_context()
            if not me_user.get("is_admin"):
                return JSONResponse({"ok": False, "error": "需 admin 权限"},
                                    status_code=403)
            ip = (body.get("ip") or "").strip()
            if not ip:
                return JSONResponse({"ok": False, "error": "ip 必填"},
                                    status_code=400)
            removed = ip_guard.lift(ip)
            return {"ok": True, "removed": removed,
                    "records": ip_guard.list_records()}

        app.app.post("/slirn/api/auth/ip_guard/unban")(ip_guard_unban)