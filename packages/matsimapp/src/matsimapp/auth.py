"""認証: パスワードハッシュ、セッション、CSRF 対策、ログイン試行制限。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response
from matsimapi.db import connect, get_db

from . import settings

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "matsimapp"
MAX_FAILURES = 5
LOCK_SECONDS = 300

_failures: dict[str, tuple[int, float]] = {}
_failures_lock = threading.Lock()


SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
"""


def init_sessions() -> None:
    conn = connect()
    try:
        conn.executescript(SESSIONS_SCHEMA)
    finally:
        conn.close()


@dataclass
class CurrentUser:
    id: int
    username: str


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            maxmem=2**27, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r),
                                p=int(p), maxmem=2**27, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# 存在しないユーザーのログイン試行でも同じ計算時間をかけるためのダミー
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(db: sqlite3.Connection, user_id: int, request: Request, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=settings.SESSION_DAYS)
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (_now().isoformat(),))
    db.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
               (_token_hash(token), user_id, expires.isoformat()))
    response.set_cookie(
        settings.SESSION_COOKIE, token, max_age=settings.SESSION_DAYS * 86400, httponly=True,
        samesite="strict", secure=settings.COOKIE_SECURE or request.url.scheme == "https", path="/")


def destroy_session(db: sqlite3.Connection, request: Request, response: Response) -> None:
    token = request.cookies.get(settings.SESSION_COOKIE)
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    response.delete_cookie(settings.SESSION_COOKIE, path="/")


def check_csrf(request: Request) -> None:
    """状態を変更するリクエストは、独自ヘッダーと同一オリジンを要求する。"""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, "不正なリクエストです")
    origin = request.headers.get("origin")
    # HTTPS の中継を通ると Host が書き換わることがあるため、中継が付ける元のホスト名も認める。
    # (他サイトのページは、利用者のブラウザにこのヘッダーを付けさせることができない)
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
    if origin and urlsplit(origin).netloc not in hosts:
        raise HTTPException(403, "不正なリクエストです")


def current_user(request: Request, db: sqlite3.Connection = Depends(get_db, scope="function")) -> CurrentUser:
    check_csrf(request)
    token = request.cookies.get(settings.SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "ログインが必要です")
    row = db.execute(
        "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (_token_hash(token), _now().isoformat())).fetchone()
    if row is None:
        raise HTTPException(401, "ログインが必要です")
    return CurrentUser(row["id"], row["username"])


def _failure_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "?"
    return f"{host}|{username.lower()}"


def check_login_allowed(request: Request, username: str) -> None:
    with _failures_lock:
        count, locked_until = _failures.get(_failure_key(request, username), (0, 0.0))
    if count >= MAX_FAILURES and time.monotonic() < locked_until:
        raise HTTPException(429, "ログイン失敗が続いたため、しばらく待ってから再度お試しください")


def record_login_result(request: Request, username: str, success: bool) -> None:
    key = _failure_key(request, username)
    with _failures_lock:
        if success:
            _failures.pop(key, None)
            return
        count, locked_until = _failures.get(key, (0, 0.0))
        if count >= MAX_FAILURES and time.monotonic() >= locked_until:
            count = 0
        _failures[key] = (count + 1, time.monotonic() + LOCK_SECONDS)
        if len(_failures) > 10_000:
            _failures.clear()
