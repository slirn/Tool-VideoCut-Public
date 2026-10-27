"""用户管理 + 会话 + 项目成员 — 扁平文件版（REQ-20260926-NNN）。

零外部依赖，照抄 slirn_home/llm_config.py 的 _load/_save + os.replace 原子写模式。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ===================== 密码哈希（stdlib pbkdf2_sha256）=====================

# 格式：pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>
# 自描述：未来换 bcrypt 路由靠 algo 字段，迁移无痛。
_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ITERS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。salt 留 None 时自动生成 16 字节。"""
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return salt.hex(), h.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expect = bytes.fromhex(hash_hex)
    except (TypeError, ValueError):
        return False
    got = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS
    )
    # 常数时间比较，防时序攻击
    return secrets.compare_digest(got, expect)


# ===================== 扁平文件存取（照抄 llm_config.py）=====================

def _load_json(path: Path, default: Any) -> Any:
    """读 JSON；不存在 / 损坏 → 回退 default，绝不阻塞启动。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:                 # noqa: BLE001
        log.warning("损坏 JSON 读失败 %s: %s — 回退默认", path, e)
        return default


def _save_json(path: Path, data: Any) -> None:
    """原子写：先写 .tmp 再 os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# 全部 AuthStore 实例共享的写锁（模块级 — launch.py 与 app.py 各建实例）。
# 装饰在「读-改-写」方法上，串行化并发登录/建用户/成员变更，防止丢更新；
# 纯读方法不锁。与服务层 threading.Lock 纪律对齐。
_AUTH_LOCK = threading.RLock()


def _locked(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _AUTH_LOCK:
            return fn(*args, **kwargs)

    return wrapper


# ===================== AuthStore =====================

class AuthStore:
    """users + sessions + task_memberships 三文件存取；不感知 FastAPI/Gradio。"""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        self.data_dir = self.repo_root / "data"
        self.users_path = self.data_dir / "users.json"
        self.sessions_path = self.data_dir / "sessions.json"
        self.memberships_path = self.data_dir / "task_memberships.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 用户 ----------

    def list_users(self) -> list[dict]:
        return _load_json(self.users_path, [])

    def get_user(self, username: str) -> dict | None:
        for u in self.list_users():
            if u.get("username") == username:
                return u
        return None

    @_locked
    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        is_admin: bool = False,
    ) -> dict:
        users = self.list_users()
        if self.get_user(username) is not None:
            raise ValueError(f"用户已存在: {username}")
        salt_hex, hash_hex = hash_password(password)
        user = {
            "username": username,
            "display_name": display_name or username,
            "pw_salt": salt_hex,
            "pw_hash": hash_hex,
            "is_admin": bool(is_admin),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        users.append(user)
        _save_json(self.users_path, users)
        log.info("创建用户 %s (admin=%s)", username, is_admin)
        return user

    @_locked
    def update_user_password(self, username: str, new_password: str) -> bool:
        users = self.list_users()
        for u in users:
            if u.get("username") == username:
                salt_hex, hash_hex = hash_password(new_password)
                u["pw_salt"] = salt_hex
                u["pw_hash"] = hash_hex
                _save_json(self.users_path, users)
                log.info("更新用户 %s 密码", username)
                return True
        return False

    @_locked
    def delete_user(self, username: str) -> bool:
        users = self.list_users()
        new_users = [u for u in users if u.get("username") != username]
        if len(new_users) == len(users):
            return False
        _save_json(self.users_path, new_users)
        # 清理该用户的会话和成员关系
        sessions = _load_json(self.sessions_path, [])
        _save_json(self.sessions_path, [s for s in sessions if s.get("username") != username])
        mems = _load_json(self.memberships_path, {})
        changed = False
        for tid in list(mems.keys()):
            if username in mems[tid]:
                mems[tid] = [u for u in mems[tid] if u != username]
                if not mems[tid]:
                    del mems[tid]
                changed = True
        if changed:
            _save_json(self.memberships_path, mems)
        log.info("删除用户 %s", username)
        return True

    # REQ-20261021-142：登录日志（data/login_log.json，500 条滚动）
    _LOGIN_LOG_MAX = 500

    @_locked
    def record_login_event(self, username: str, ip: str, result: str,
                           detail: str = "") -> None:
        """追加一条登录事件。result ∈ {success, fail, logout}。"""
        events = _load_json(self.repo_root / "data" / "login_log.json", [])
        events.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "username": username,
            "ip": ip or "",
            "result": result,
            "detail": detail or "",
        })
        if len(events) > self._LOGIN_LOG_MAX:
            events = events[-self._LOGIN_LOG_MAX:]
        _save_json(self.repo_root / "data" / "login_log.json", events)

    def get_login_events(self, username: str, limit: int = 50) -> list[dict]:
        """该用户的登录事件，新 → 旧。只给本人看（不做 admin 越权面）。"""
        events = _load_json(self.repo_root / "data" / "login_log.json", [])
        own = [e for e in events if e.get("username") == username]
        own.reverse()
        return own[:limit]

    # REQ-20261021-141：系统默认 admin 账号（admin 不存在时创建，不覆盖）
    DEFAULT_ADMIN_USERNAME = "admin"
    DEFAULT_ADMIN_PASSWORD = "admin123!@#"

    def ensure_default_admin(self) -> bool:
        """确保默认 admin 存在；返回是否新建。

        admin 已存在（含用户已改密码）一律不动 —— 幂等且尊重用户变更。
        由 launch.py slirn 分支调用（不放构造器：测试依赖「空库 = 0 用户」语义）。
        """
        if self.get_user(self.DEFAULT_ADMIN_USERNAME) is not None:
            return False
        self.create_user(self.DEFAULT_ADMIN_USERNAME, "Administrator",
                         self.DEFAULT_ADMIN_PASSWORD, is_admin=True)
        return True

    def count_users(self) -> int:
        return len(self.list_users())

    def is_default_password(self, username: str) -> bool:
        """该账号当前是否仍在使用出厂默认密码（首登强制改密的判定依据）。

        只对「密码哈希 == DEFAULT_ADMIN_PASSWORD 的哈希」为真 —— 用户改过
        密码后即为假；用户把自己的密码改成默认口令同样会命中（此时提示
        改密也是正确行为）。
        """
        u = self.get_user(username)
        if u is None:
            return False
        salt_hex = u.get("pw_salt", "")
        hash_hex = u.get("pw_hash", "")
        if not salt_hex or not hash_hex:
            return False
        return verify_password(self.DEFAULT_ADMIN_PASSWORD, salt_hex, hash_hex)

    def verify_credentials(self, username: str, password: str) -> dict | None:
        """验证密码；正确返回 {username, display_name, is_admin}，错误返 None。

        无论用户是否存在，都走一次 pbkdf2 防止时序探测（用户名枚举）。
        """
        u = self.get_user(username)
        if u is None:
            # 用户不存在：仍做一次 hash 消耗大致相同时间，避免时序探测
            hash_password(password)
            return None
        salt_hex = u.get("pw_salt", "")
        hash_hex = u.get("pw_hash", "")
        if not salt_hex or not hash_hex:
            return None
        if not verify_password(password, salt_hex, hash_hex):
            return None
        return {
            "username": u["username"],
            "display_name": u.get("display_name", u["username"]),
            "is_admin": u.get("is_admin", False),
        }

    # ---------- 会话 ----------

    @_locked
    def create_session(self, username: str, ttl_days: int = 30) -> str:
        sessions = _load_json(self.sessions_path, [])
        token = secrets.token_hex(32)
        now = time.time()
        new = {
            "token": token,
            "username": username,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "expires_at": now + ttl_days * 86400,
        }
        # REQ-20261021-142：单会话 —— 同用户旧会话全部失效（后登踢前），
        # 只保留本次新会话。登录 / bootstrap 共用本方法，一处生效。
        sessions = [s for s in sessions if s.get("username") != username] + [new]
        _save_json(self.sessions_path, sessions)
        return token

    def user_session_count(self, username: str) -> int:
        """该用户当前有效会话数（登录日志「踢掉几个旧会话」用）。"""
        now = time.time()
        return sum(1 for s in _load_json(self.sessions_path, [])
                   if s.get("username") == username
                   and s.get("expires_at", 0) >= now)

    @_locked
    def user_from_token(self, token: str) -> dict | None:
        """查 user；过期自动清理。返 {username, display_name, is_admin}。"""
        if not token:
            return None
        sessions = _load_json(self.sessions_path, [])
        now = time.time()
        kept: list[dict] = []
        result: dict | None = None
        expired_to_purge = False
        for s in sessions:
            if s.get("token") == token:
                if s.get("expires_at", 0) >= now:
                    result = s
                    kept.append(s)          # 有效会话必须保留（否则变单次会话）
                else:
                    expired_to_purge = True  # 过期的才删
            else:
                kept.append(s)
        if expired_to_purge or (kept and len(kept) < len(sessions)):
            _save_json(self.sessions_path, kept)
        if result is None:
            return None
        u = self.get_user(result["username"])
        if u is None:
            return None
        return {"username": u["username"], "display_name": u.get("display_name", u["username"]),
                "is_admin": u.get("is_admin", False)}

    @_locked
    def delete_session(self, token: str) -> bool:
        sessions = _load_json(self.sessions_path, [])
        kept = [s for s in sessions if s.get("token") != token]
        if len(kept) == len(sessions):
            return False
        _save_json(self.sessions_path, kept)
        return True

    @_locked
    def purge_expired_sessions(self) -> int:
        sessions = _load_json(self.sessions_path, [])
        now = time.time()
        kept = [s for s in sessions if s.get("expires_at", 0) >= now]
        n = len(sessions) - len(kept)
        if n > 0:
            _save_json(self.sessions_path, kept)
        return n

    # ---------- 成员（集中式 task_memberships.json） ----------

    def _load_memberships(self) -> dict[str, list[str]]:
        return _load_json(self.memberships_path, {})

    def _save_memberships(self, data: dict[str, list[str]]) -> None:
        _save_json(self.memberships_path, data)

    def list_task_members(self, task_id: str) -> list[str]:
        return list(self._load_memberships().get(f"task_{task_id}", []))

    @_locked
    def add_task_member(self, task_id: str, username: str) -> bool:
        mems = self._load_memberships()
        key = f"task_{task_id}"
        cur = mems.get(key, [])
        if username in cur:
            return False
        cur = cur + [username]
        mems[key] = cur
        self._save_memberships(mems)
        return True

    @_locked
    def remove_task_member(self, task_id: str, username: str) -> bool:
        mems = self._load_memberships()
        key = f"task_{task_id}"
        if key not in mems or username not in mems[key]:
            return False
        mems[key] = [u for u in mems[key] if u != username]
        if not mems[key]:
            del mems[key]
        else:
            # 保持排序稳定性（按插入序）
            pass
        self._save_memberships(mems)
        return True

    def list_user_tasks(self, username: str) -> list[str]:
        """返该用户被加入的所有 task_id（去掉 'task_' 前缀）。"""
        mems = self._load_memberships()
        out: list[str] = []
        for k, users in mems.items():
            if username in users:
                out.append(k[len("task_"):] if k.startswith("task_") else k)
        return out


# ===================== 中间件 =====================

import contextvars as _contextvars

# REQ-20260926-NNN：用 contextvar 在中间件与端点间传递 user（绕开 FastAPI Request 注入的 Gradio 6 bug）
_sliRN_CURRENT_USER: _contextvars.ContextVar[dict | None] = _contextvars.ContextVar(
    "slirn_user", default=None
)


def get_current_user_from_context() -> dict:
    """端点用：从中间件设的 contextvar 读当前 user。未登录返 {}。"""
    return _sliRN_CURRENT_USER.get() or {}


# REQ-20261021-139 / D4：会话 token 的 contextvar（与 user contextvar 并列）。
# 中间件已从 cookie 解析出 token，这里再存一份，供「需要在后台线程里继续以
# 发起者身份调用 /slirn/api/*」的场景使用 —— contextvars 不跨线程传播，
# 故端点必须在启动线程前取出 token 并显式透传（见 pipeline_service）。
_sliRN_CURRENT_SESSION_TOKEN: _contextvars.ContextVar[str | None] = _contextvars.ContextVar(
    "slirn_session_token", default=None
)


def get_session_token_from_context() -> str:
    """端点用：读中间件记下的 slirn_session cookie 值。未登录/旁路模式返 ""。"""
    return _sliRN_CURRENT_SESSION_TOKEN.get() or ""


from starlette.responses import JSONResponse, RedirectResponse

_PUBLIC_PREFIXES = (
    "/slirn/api/auth/login",
    "/slirn/api/auth/logout",
    "/slirn/api/auth/me",
    "/slirn/api/auth/bootstrap",
    "/slirn/api/health",
    "/slirn/static/",
)

# REQ-20261021-140：登录页本身公开（独立 /login，不加载工作台资源）。
# 页面级白名单用精确匹配（无子路径），前缀白名单只留给 API/静态。
_PAGE_PUBLIC_EXACT = ("/login",)


def _is_public_api_path(path: str) -> bool:
    """REQ-20261021-142：公开 API 路径判定改为「路径段边界」匹配。

    早前的 ``startswith(p)`` 会把同前缀的新路由误判为公开——实测
    ``/slirn/api/auth/login_log``（登录日志，需登录）撞上了
    ``/slirn/api/auth/login``（登录，公开）前缀，被放行且无 user 上下文。
    现规则：要么整条相等；要么 p 以 ``/`` 结尾的目录前缀（如 static）。
    """
    for p in _PUBLIC_PREFIXES:
        if path == p:
            return True
        if p.endswith("/") and path.startswith(p):
            return True
    return False

# 测试模式开关：每次请求读 env var（不依赖模块导入时机）。
# 设置 SLIRN_AUTH_BYPASS=1 时中间件直接放行（挂 test-admin 假 user）。
# 用法：tests/conftest.py 里 os.environ.setdefault(...)= '1'；生产环境绝对不要设置。


def _extract_session_token(scope: dict) -> str | None:
    """从 ASGI scope 的 headers 里抠 slirn_session cookie（绕开 Request 注入 bug）。"""
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == "slirn_session":
                    return val
    return None


def _extract_task_id_from_query(scope: dict) -> str | None:
    """从 query string 抠 task_id（GET 端点：output_file/fine_material_file/…）。"""
    qs = scope.get("query_string", b"").decode("latin-1")
    for part in qs.split("&"):
        name, _, val = part.partition("=")
        if name in ("task_id", "tid") and val:
            return val
    return None


async def _read_full_body(receive) -> bytes | None:
    """收干请求 body；http.disconnect 返 None（调用方直接放行）。"""
    chunks: list[bytes] = []
    while True:
        msg = await receive()
        if msg.get("type") == "http.disconnect":
            return None
        chunk = msg.get("body", b"") or b""
        if chunk:
            chunks.append(chunk)
        if not msg.get("more_body", False):
            return b"".join(chunks)


def _replay_receive(body: bytes):
    """构造一个回放 body 的 receive — 中间件读了 body 后下游还能再读。"""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


def _is_json_request(scope: dict) -> bool:
    for k, v in scope.get("headers", []):
        if k == b"content-type" and b"json" in v.lower():
            return True
    return False


def _bypass_active(scope: dict) -> bool:
    """SLIRN_AUTH_BYPASS=1 是否对本请求生效。

    硬化规则：环境变量开启 **且** 客户端来自回环。服务以 --listen 0.0.0.0
    对局域网暴露时，远程请求即使误设了 bypass 也不放行（测试旁路只能
    在本机自调 /slirn/api 的场景下使用，那正是它存在的目的）。
    """
    if os.environ.get("SLIRN_AUTH_BYPASS") != "1":
        return False
    client = scope.get("client") or ("", 0)
    return str(client[0]) in ("127.0.0.1", "::1", "localhost")


class SlirnAuthMiddleware:
    """REQ-20260926-NNN：cookie 会话中间件（纯 ASGI，非 BaseHTTPMiddleware）。

    为什么不用 BaseHTTPMiddleware：它的 call_next 会把下游 app 放到另一个
    task/context 里跑，contextvar 隔离 — 端点里 get_current_user_from_context()
    永远拿不到 user（实测 refresh_tasks 拿到空 dict）。纯 ASGI 中间件与下游
    在同一协程链里 await，contextvar 直达端点（sync 端点经 anyio to_thread
    也会拷贝 context，同样可达）。

    - 公开端点（登录/登出/me/bootstrap/health/static）直通。
    - 其他 /slirn/api/*：读 slirn_session cookie → 查 sessions.json。
      未登录返 401；登录则把 user 写 scope["state"]["slirn_user"] + contextvar。
    - task_access 钩子（可选）：非 admin 请求带 task_id（query 或 JSON body）
      时调用 task_access(user, task_id)，False → 403。这样 ~80 个任务端点
      一处统一拦截，不用逐个加 _require_member。
    """

    def __init__(self, app, auth: AuthStore, task_access=None):
        self.app = app
        self.auth = auth
        self.task_access = task_access

    async def __call__(self, scope, receive, send):
        # REQ-20261021-140：登录墙对 websocket 同样生效 ——
        # 未登录的 ws 升级直接 close(1008)，不触达下游（Gradio 队列等内部 ws）；
        # 已登录的 ws 与 http 走同一 _run_with_user（scope state + contextvar
        # 挂 user，防止下游 ws 处理器看起来「匿名」）。
        if scope.get("type") == "websocket":
            if _bypass_active(scope):
                await self.app(scope, receive, send)
                return
            _tok = _extract_session_token(scope)
            _user = self.auth.user_from_token(_tok) if _tok else None
            if _user is None:
                await send({"type": "websocket.close", "code": 1008})
                return
            await self._run_with_user(scope, receive, send, _user,
                                      session_token=_tok or "")
            return
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "?")
        # REQ-20260926-NNN 排查：记每个 /slirn/api 请求的路径+方法+是否带 cookie。
        # 默认关闭（排查已结束，避免在仓库根无限追加日志文件）；
        # 需要时设 SLIRN_MIDDLEWARE_DEBUG=1，写到 .temp/（已被 .gitignore 覆盖）。
        if os.environ.get("SLIRN_MIDDLEWARE_DEBUG") == "1":
            try:
                import time as _t
                ck = _extract_session_token(scope)
                _dbg_dir = self.auth.repo_root / ".temp"
                _dbg_dir.mkdir(exist_ok=True)
                _dbg = _dbg_dir / "middleware_debug.log"
                with open(_dbg, "a", encoding="utf-8") as _f:
                    _f.write(f"{_t.strftime('%H:%M:%S')} {method} {path} cookie={'Y' if ck else 'N'}\n")
            except Exception:  # noqa: BLE001
                pass
        if _is_public_api_path(path) or path in _PAGE_PUBLIC_EXACT:
            await self.app(scope, receive, send)
            return
        # 测试模式 bypass：每次读 env（不依赖模块加载时机）。
        # 硬化：仅在客户端来自回环时生效 —— --listen 0.0.0.0 对外暴露时，
        # 局域网请求即使误设了 SLIRN_AUTH_BYPASS=1 也不再匿名放行
        if _bypass_active(scope):
            user = {"username": "test-admin", "display_name": "test-admin",
                    "is_admin": True}
            await self._run_with_user(scope, receive, send, user, session_token="")
            return
        token = _extract_session_token(scope)
        user = self.auth.user_from_token(token) if token else None
        if user is None:
            # ---- 未登录（REQ-20261021-140 登录墙）----
            # API 维持既有 401 JSON 语义（零回退）；
            # 其他一切路由 → 登录墙：GET/HEAD 302 到 /login，
            # 非 GET（内部 POST 等）与 API 同样返 401，不重定向。
            if path.startswith("/slirn/api/") or method not in ("GET", "HEAD"):
                resp = JSONResponse(
                    {"ok": False, "error": "未登录或会话已过期"},
                    status_code=401,
                )
            else:
                resp = RedirectResponse("/login", status_code=302)
            await resp(scope, receive, send)
            return
        if not path.startswith("/slirn/api/"):
            # 已登录的非 API 请求（页面 / Gradio 内部）：直通，行为同旧。
            await self.app(scope, receive, send)
            return
        # ---- task 级守卫：非 admin + 请求带 task_id → 校验成员资格 ----
        if self.task_access is not None and not user.get("is_admin"):
            blocked, recv_or_resp = await self._check_task_access(scope, receive, user)
            if blocked:
                await recv_or_resp(scope, receive, send)
                return
            receive = recv_or_resp  # 未拦：可能是回放版 receive（body 已读）
        await self._run_with_user(scope, receive, send, user, session_token=token or "")

    async def _check_task_access(self, scope, receive, user: dict):
        """返 (blocked, receive)。blocked=True 时 receive 位是 403 JSONResponse。

        读 body 仅发生在 JSON 请求（multipart 上传不走此路径，receive 原样透传）；
        读过的 body 会包装成回放 receive 交给下游。
        """
        tid = _extract_task_id_from_query(scope)
        recv_downstream = receive
        if not tid and _is_json_request(scope):
            body = await _read_full_body(receive)
            if body is None:
                return False, receive  # 断连：放行（下游自然报错）
            recv_downstream = _replay_receive(body)
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
                if isinstance(data, dict):
                    tid = (data.get("task_id") or data.get("tid") or "").strip() or None
            except Exception:                      # noqa: BLE001
                tid = None  # 非 JSON（如被 Gradio 代理的怪请求）→ 不拦
        if not tid:
            return False, recv_downstream
        try:
            allowed = bool(self.task_access(user, tid))
        except Exception as e:                     # noqa: BLE001
            log.warning("task_access 校验异常（放行由端点兜底）: %s", e)
            return False, recv_downstream
        if not allowed:
            return True, JSONResponse(
                {"ok": False, "error": f"用户 {user.get('username')} 无权访问任务 {tid}"},
                status_code=403,
            )
        return False, recv_downstream

    async def _run_with_user(self, scope, receive, send, user: dict,
                             session_token: str = "") -> None:
        """挂 user（scope state + contextvar）后放行下游，结束复位 contextvar。

        REQ-20261021-139 / D4：同时挂 session_token contextvar —— 端点若要
        在后台线程里继续以发起者身份调内部端点（自动流水线），必须在此刻
        读到 token（contextvars 不跨线程）。
        """
        # request.state.slirn_user：Starlette Request.state 底层就是 scope["state"]
        scope.setdefault("state", {})["slirn_user"] = user
        cv_token = _sliRN_CURRENT_USER.set(user)
        cv_st = _sliRN_CURRENT_SESSION_TOKEN.set(session_token or None)
        try:
            await self.app(scope, receive, send)
        finally:
            _sliRN_CURRENT_SESSION_TOKEN.reset(cv_st)
            _sliRN_CURRENT_USER.reset(cv_token)


def install_auth_middleware(app, auth: AuthStore, task_access=None) -> None:
    """REQ-20260926-NNN：往 app.user_middleware 插入 auth 中间件（绕开 Gradio 6 重建问题）。

    用法：launch.py 在 _register_slirn_api 之后调一次。
    原理：launch(prevent_thread_lock=True) 重建 app.app（新 FastAPI 实例），
    新实例的 middleware_stack=None、user_middleware=[]，可直接插入；
    uvicorn 启动时会把 user_middleware 装进 middleware_stack。
    SlirnAuthMiddleware 是纯 ASGI 中间件（不走 BaseHTTPMiddleware — 其 call_next
    的 task 隔离会让 contextvar 到不了端点）。
    task_access(user, task_id) -> bool：非 admin 访问带 task_id 的端点时校验
    （app.py 传入闭包，用 TaskManager + AuthStore 判断 admin/创建者/成员）。

    ⚠️ 恢复路径（launch 抛 httpx 异常后）：uvicorn 在抛错前可能已经启动并构建
    了 middleware_stack（缓存到 _middleware_stack），仅修改 user_middleware 不
    会触发重建 —— 必须手动重建一次，否则中间件形同虚设（实测未登录返 200）。
    """
    from starlette.middleware import Middleware as _Middleware
    app.user_middleware.insert(
        0, _Middleware(SlirnAuthMiddleware, auth=auth, task_access=task_access)
    )
    # 强制重建 middleware_stack：清缓存后下一次 ASGI 调用会重新 build。
    # 直接赋值也安全（build_middleware_stack 返纯 ASGI app）。
    try:
        app.middleware_stack = app.build_middleware_stack()
    except Exception:                       # noqa: BLE001
        # 某些 Starlette 版本上没有 build_middleware_stack 公开 — 退到清缓存
        if hasattr(app, "_middleware_stack"):
            app._middleware_stack = None


# ===================== Helpers（给端点用） =====================

def read_user_from_request(request) -> dict:
    """从 request（Starlette/FastAPI Request）读当前 user。

    优先级：scope["state"]["slirn_user"]（中间件写的）→ contextvar。
    都没有返 {}（未登录 / 中间件未装 — 端点自己决定兜底行为）。
    """
    u = (getattr(request, "scope", {}) or {}).get("state", {}).get("slirn_user")
    if u:
        return u
    return get_current_user_from_context()