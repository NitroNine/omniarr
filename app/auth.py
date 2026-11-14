"""User accounts, password hashing and login sessions.

Roles: 'admin' (everything) and 'user' (browse + request). Per-user flags an
admin controls: auto_approve (requests are added immediately) and allow_uhd
(may request titles for the 4K library).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

from . import db

SESSION_COOKIE = "omniarr_session"
SESSION_DAYS = 30
_ITER = 240_000

# tables are created in db.SCHEMA (users, sessions, requests)


def ensure_schema() -> None:
    db.connect()


# ------------------------------------------------------------- passwords --
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, it, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def _check_new(username: str | None, password: str | None) -> None:
    if username is not None and not re.fullmatch(r"[A-Za-z0-9._@-]{2,40}", username):
        raise ValueError("Username must be 2–40 characters: letters, numbers, . _ @ -")
    if password is not None and len(password) < 8:
        raise ValueError("Password must be at least 8 characters")


# ----------------------------------------------------------------- users --
PUBLIC_FIELDS = "id, username, role, auto_approve, allow_uhd, enabled, created, last_login"


def user_count() -> int:
    ensure_schema()
    return db.one("SELECT COUNT(*) c FROM users")["c"]


def create_user(username: str, password: str, role: str = "user", auto_approve: bool = False,
                allow_uhd: bool = False) -> dict:
    _check_new(username, password)
    if role not in ("admin", "user"):
        raise ValueError("role must be 'admin' or 'user'")
    if db.one("SELECT 1 FROM users WHERE username=?", (username,)):
        raise ValueError("That username is taken")
    uid = db.insert("users", {"username": username, "password_hash": hash_password(password), "role": role,
                              "auto_approve": int(auto_approve or role == "admin"),
                              "allow_uhd": int(allow_uhd or role == "admin")})
    return get_user(uid)


def get_user(uid: int) -> dict | None:
    return db.one(f"SELECT {PUBLIC_FIELDS} FROM users WHERE id=?", (uid,))


def list_users() -> list[dict]:
    return db.q(f"SELECT {PUBLIC_FIELDS} FROM users ORDER BY role, username COLLATE NOCASE")


def _admins_left(excluding: int) -> int:
    return db.one("SELECT COUNT(*) c FROM users WHERE role='admin' AND enabled=1 AND id<>?", (excluding,))["c"]


def update_user(uid: int, changes: dict, acting_user: dict) -> dict:
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise ValueError("User not found")
    data: dict = {}
    if "role" in changes:
        if changes["role"] not in ("admin", "user"):
            raise ValueError("role must be 'admin' or 'user'")
        data["role"] = changes["role"]
    for flag in ("auto_approve", "allow_uhd", "enabled"):
        if flag in changes:
            data[flag] = int(bool(changes[flag]))
    if changes.get("password"):
        _check_new(None, changes["password"])
        data["password_hash"] = hash_password(changes["password"])
    demoting = u["role"] == "admin" and (data.get("role") == "user" or data.get("enabled") == 0)
    if demoting and _admins_left(uid) == 0:
        raise ValueError("There must always be at least one enabled admin")
    if uid == acting_user["id"] and data.get("enabled") == 0:
        raise ValueError("You can't disable your own account")
    db.update("users", uid, data)
    if data.get("enabled") == 0 or "password_hash" in data:
        db.ex("DELETE FROM sessions WHERE user_id=?", (uid,))  # log them out everywhere
    return get_user(uid)


def delete_user(uid: int, acting_user: dict) -> None:
    u = get_user(uid)
    if not u:
        return
    if uid == acting_user["id"]:
        raise ValueError("You can't delete your own account")
    if u["role"] == "admin" and _admins_left(uid) == 0:
        raise ValueError("There must always be at least one admin")
    db.ex("DELETE FROM users WHERE id=?", (uid,))


def change_own_password(user: dict, current: str, new: str) -> None:
    row = db.one("SELECT password_hash FROM users WHERE id=?", (user["id"],))
    if not verify_password(current, row["password_hash"]):
        raise ValueError("Current password is wrong")
    _check_new(None, new)
    db.update("users", user["id"], {"password_hash": hash_password(new)})


# -------------------------------------------------------------- sessions --
_failures: dict[str, list[float]] = {}


def _throttled(key: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < 300]
    _failures[key] = recent
    return len(recent) >= 10  # 10 failed logins per 5 minutes per IP


def login(username: str, password: str, client_ip: str) -> tuple[str, dict]:
    if _throttled(client_ip):
        raise PermissionError("Too many failed logins. Try again in a few minutes.")
    row = db.one("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not row or not row["enabled"] or not verify_password(password, row["password_hash"]):
        _failures.setdefault(client_ip, []).append(time.time())
        raise PermissionError("Wrong username or password")
    token = secrets.token_urlsafe(32)
    db.insert("sessions", {"token_hash": _h(token), "user_id": row["id"], "expires": time.time() + SESSION_DAYS * 86400})
    db.ex("UPDATE users SET last_login=datetime('now') WHERE id=?", (row["id"],))
    db.ex("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    return token, get_user(row["id"])


def logout(token: str | None) -> None:
    if token:
        db.ex("DELETE FROM sessions WHERE token_hash=?", (_h(token),))


def user_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    row = db.one("SELECT u.id, u.enabled, s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                 (_h(token),))
    if not row or not row["enabled"] or row["expires"] < time.time():
        return None
    return get_user(row["id"])


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bootstrap_from_env() -> None:
    """Upgrading from the single-password version: OMNIARR_PASSWORD becomes the admin account."""
    pw = os.environ.get("OMNIARR_PASSWORD")
    if pw and user_count() == 0:
        name = os.environ.get("OMNIARR_USERNAME") or "admin"
        db.insert("users", {"username": name, "password_hash": hash_password(pw), "role": "admin",
                            "auto_approve": 1, "allow_uhd": 1})
        db.log(f"Created admin account '{name}' from OMNIARR_PASSWORD")


# ---------------------------------------------------------- permissions --
# What a non-admin may call. Everything else under /api is admin-only.
USER_ROUTES = [
    ("GET", r"/api/auth/me"), ("POST", r"/api/auth/logout"), ("POST", r"/api/auth/password"),
    ("GET", r"/api/lookup/(movie|series|collection)"), ("GET", r"/api/collection/\w+/\d+"),
    ("GET", r"/api/providers(/\d+/(movie|tv))?"), ("GET", r"/api/new/(movie|tv)"),
    ("GET", r"/api/movies(/\d+)?"), ("GET", r"/api/series(/\d+)?"), ("GET", r"/api/collections"),
    ("GET", r"/api/info/(movie|tv)/\d+"),
    ("GET", r"/api/requests"), ("POST", r"/api/requests"), ("DELETE", r"/api/requests/\d+"),
]
_USER_RX = [(m, re.compile(p + "$")) for m, p in USER_ROUTES]


def user_may(method: str, path: str) -> bool:
    return any(m == method and rx.match(path) for m, rx in _USER_RX)
