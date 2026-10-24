"""REQ-20260926-NNN：首次启动引导 — 创建第一个 admin 用户。

运行方式：
    .venv/Scripts/python.exe -m slirn_home.bootstrap_admin

要求：data/users.json 不存在或为空。已存在用户则拒绝创建。
"""
from __future__ import annotations

import getpass
import sys

from slirn_home.paths import find_slirn_standalone_root


def main() -> int:
    from slirn_home.auth import AuthStore

    repo = find_slirn_standalone_root()
    auth = AuthStore(repo)
    if auth.count_users() > 0:
        print(f"✗ 系统已有 {auth.count_users()} 个用户，拒绝创建（请用 Web 登录或现有 admin 操作）")
        return 1

    print("== 首次启动引导：创建第一个 admin ==")
    print(f"  仓库根 = {repo}")
    print(f"  用户文件 = {auth.users_path}")
    print()
    username = input("admin 用户名: ").strip()
    if not username:
        print("✗ 用户名不能为空")
        return 1
    pw = getpass.getpass("密码（至少 4 位）: ")
    if len(pw) < 4:
        print("✗ 密码至少 4 位")
        return 1
    pw2 = getpass.getpass("再次输入密码: ")
    if pw != pw2:
        print("✗ 两次密码不一致")
        return 1

    auth.create_user(username, username, pw, is_admin=True)
    print(f"✓ 已创建 admin: {username}")
    print(f"  请用此账号登录 http://127.0.0.1:<端口>")
    return 0


if __name__ == "__main__":
    sys.exit(main())