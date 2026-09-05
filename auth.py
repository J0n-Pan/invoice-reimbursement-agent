"""Local account, session and audit helpers for the company deployment."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from typing import Any

from invoice_agent import connect, now_iso


ROLE_LABELS = {
    "employee": "员工",
    "finance": "财务",
    "admin": "管理员",
}
ALLOWED_ROLES = set(ROLE_LABELS)
SESSION_COOKIE = "invoice_agent_session"
SESSION_HOURS = 8
PASSWORD_ITERATIONS = 310_000
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,40}$")


class AuthenticationError(ValueError):
    """A user must authenticate again or supplied credentials are invalid."""


class AuthorizationError(PermissionError):
    """The authenticated user is not allowed to perform an action."""


def init_auth_schema() -> None:
    """Create account/session/audit tables and migrate an existing database."""
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                department TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
            CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                csrf_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                actor_user_id TEXT,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_logs(created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_logs(actor_user_id);
            """
        )


def setup_required() -> bool:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def _password_hash(password: str) -> str:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(PASSWORD_ITERATIONS, salt.hex(), digest.hex())


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def _validate_user_input(username: str, role: str) -> tuple[str, str]:
    username = str(username or "").strip()
    role = str(role or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("用户名需为 3-40 位字母、数字、下划线、短横线或点号")
    if role not in ALLOWED_ROLES:
        raise ValueError("不支持的用户角色")
    return username, role


def _public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "username": data["username"],
        "display_name": data["display_name"],
        "role": data["role"],
        "role_label": ROLE_LABELS.get(data["role"], data["role"]),
        "department": data.get("department", ""),
        "active": bool(data.get("active", 0)),
        "created_at": data.get("created_at", ""),
    }


def list_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, username, display_name, role, department, active, created_at FROM users ORDER BY username"
        ).fetchall()
    return [_public_user(row) for row in rows]


def get_user(user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _public_user(row) if row else None


def create_user(
    username: str,
    password: str,
    role: str,
    display_name: str = "",
    department: str = "",
) -> dict[str, Any]:
    username, role = _validate_user_input(username, role)
    display_name = str(display_name or "").strip() or username
    department = str(department or "").strip()[:80]
    user_id = "USR-" + secrets.token_hex(8).upper()
    stamp = now_iso()
    password_hash = _password_hash(str(password or ""))
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, username, display_name, password_hash, role, department,
                                   active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (user_id, username, display_name, password_hash, role, department, stamp, stamp),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("用户名已存在") from exc
    return get_user(user_id) or {}


def create_first_admin(username: str, password: str, display_name: str = "") -> dict[str, Any]:
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 0:
            raise AuthorizationError("管理员初始化已完成")
    return create_user(username, password, "admin", display_name)


def update_user(user_id: str, **changes: Any) -> dict[str, Any]:
    allowed: dict[str, Any] = {}
    if "display_name" in changes:
        allowed["display_name"] = str(changes["display_name"] or "").strip()[:80]
    if "department" in changes:
        allowed["department"] = str(changes["department"] or "").strip()[:80]
    if "role" in changes:
        _, allowed["role"] = _validate_user_input("abc", changes["role"])
    if "active" in changes:
        allowed["active"] = 1 if bool(changes["active"]) else 0
    if not allowed:
        raise ValueError("没有可更新的用户字段")
    allowed["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in allowed)
    with connect() as conn:
        current = conn.execute("SELECT role, active FROM users WHERE id=?", (user_id,)).fetchone()
        if not current:
            raise ValueError("用户不存在")
        next_role = allowed.get("role", current["role"])
        next_active = allowed.get("active", current["active"])
        if current["role"] == "admin" and current["active"] and (next_role != "admin" or not next_active):
            active_admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
            ).fetchone()[0]
            if active_admins <= 1:
                raise ValueError("系统至少需要保留一名启用中的管理员")
        result = conn.execute(
            f"UPDATE users SET {assignments} WHERE id=?",
            (*allowed.values(), user_id),
        )
        if result.rowcount != 1:
            raise ValueError("用户不存在")
    return get_user(user_id) or {}


def reset_password(user_id: str, password: str) -> dict[str, Any]:
    password_hash = _password_hash(str(password or ""))
    with connect() as conn:
        result = conn.execute(
            "UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL, updated_at=? WHERE id=?",
            (password_hash, now_iso(), user_id),
        )
        if result.rowcount != 1:
            raise ValueError("用户不存在")
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return get_user(user_id) or {}


def change_password(user_id: str, current_password: str, new_password: str) -> None:
    """Verify the current password, replace it, and revoke every active session."""
    with connect() as conn:
        row = conn.execute(
            "SELECT password_hash, active FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not row or not row["active"]:
            raise AuthenticationError("账号不存在或已停用")
        if not _verify_password(str(current_password or ""), row["password_hash"]):
            raise ValueError("当前密码不正确")
        password_hash = _password_hash(str(new_password or ""))
        conn.execute(
            "UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL, updated_at=? WHERE id=?",
            (password_hash, now_iso(), user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def _session_expired(value: str) -> bool:
    try:
        return datetime.fromisoformat(value) <= datetime.now()
    except (TypeError, ValueError):
        return True


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate(username: str, password: str) -> dict[str, Any]:
    username = str(username or "").strip()
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or not row["active"]:
            raise AuthenticationError("用户名或密码错误")
        if row["locked_until"] and not _session_expired(row["locked_until"]):
            raise AuthenticationError("登录失败次数过多，请 15 分钟后重试")
        if not _verify_password(str(password or ""), row["password_hash"]):
            attempts = int(row["failed_attempts"] or 0) + 1
            locked_until = (datetime.now() + timedelta(minutes=15)).isoformat(sep=" ", timespec="seconds") if attempts >= 5 else None
            conn.execute(
                "UPDATE users SET failed_attempts=?, locked_until=?, updated_at=? WHERE id=?",
                (attempts, locked_until, now_iso(), row["id"]),
            )
            raise AuthenticationError("用户名或密码错误")
        conn.execute(
            "UPDATE users SET failed_attempts=0, locked_until=NULL, updated_at=? WHERE id=?",
            (now_iso(), row["id"]),
        )
    return _public_user(row)


def start_session(user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    stamp = datetime.now()
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _hash_token(token),
                user_id,
                csrf_token,
                stamp.replace(microsecond=0).isoformat(sep=" "),
                (stamp + timedelta(hours=SESSION_HOURS)).replace(microsecond=0).isoformat(sep=" "),
                now_iso(),
            ),
        )
    return token, csrf_token


def current_user(cookie_header: str | None) -> dict[str, Any] | None:
    if not cookie_header:
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header)
    except (CookieError, ValueError):
        return None
    morsel = cookies.get(SESSION_COOKIE)
    if not morsel or not morsel.value:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT u.*, s.csrf_token, s.expires_at
            FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=?
            """,
            (_hash_token(morsel.value),),
        ).fetchone()
        if not row or not row["active"] or _session_expired(row["expires_at"]):
            if row:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(morsel.value),))
            return None
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now_iso(), _hash_token(morsel.value)))
    user = _public_user(row)
    user["csrf_token"] = row["csrf_token"]
    user["session_token"] = morsel.value
    return user


def end_session(cookie_header: str | None) -> None:
    user = current_user(cookie_header)
    if user and user.get("session_token"):
        with connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(user["session_token"]),))


def verify_csrf(user: dict[str, Any], provided: str | None) -> None:
    if not provided or not hmac.compare_digest(str(user.get("csrf_token", "")), str(provided)):
        raise AuthorizationError("请求校验已失效，请刷新页面后重试")


def require_role(user: dict[str, Any] | None, roles: set[str]) -> dict[str, Any]:
    if not user:
        raise AuthenticationError("请先登录")
    if user.get("role") not in roles:
        raise AuthorizationError("当前账号没有执行该操作的权限")
    return user


def record_audit(
    actor: dict[str, Any] | None,
    action: str,
    target_type: str,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, target_type, target_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "AUD-" + secrets.token_hex(8).upper(),
                actor.get("id") if actor else None,
                action,
                target_type,
                target_id,
                json.dumps(detail or {}, ensure_ascii=False, default=str),
                now_iso(),
            ),
        )


def cookie_header(token: str, *, secure: bool = False) -> str:
    flags = [f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={SESSION_HOURS * 3600}"]
    if secure:
        flags.append("Secure")
    return "; ".join(flags)


def clear_cookie_header() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
