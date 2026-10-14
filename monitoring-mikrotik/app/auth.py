"""Sesi login berbasis cookie + hash password (stdlib, aman di Python 3.8)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

SESSION_TTL = 12 * 3600  # 12 jam
_sessions: dict = {}  # token -> {"user","role","exp"}


def hash_password(pwd: str, salt: str = "") -> tuple:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac(
        "sha256", pwd.encode("utf-8"), bytes.fromhex(salt), 120_000
    ).hex()
    return h, salt


def verify_password(pwd: str, salt: str, expect: str) -> bool:
    h = hashlib.pbkdf2_hmac(
        "sha256", pwd.encode("utf-8"), bytes.fromhex(salt), 120_000
    ).hex()
    return hmac.compare_digest(h, expect)


def create_session(user: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"user": user, "role": role, "exp": time.time() + SESSION_TTL}
    return token


def get_session(token: str):
    s = _sessions.get(token)
    if not s:
        return None
    if s["exp"] < time.time():
        _sessions.pop(token, None)
        return None
    return s


def destroy_session(token: str) -> None:
    _sessions.pop(token, None)


def seed_users() -> None:
    """Isi tabel users sekali dari .env (AUTH_USER=admin, TECH_USERS=teknisi)."""
    from .config import settings
    from .database import Base, SessionLocal, engine
    from .models import User

    Base.metadata.create_all(bind=engine)  # idempoten: pastikan tabel users ada
    db = SessionLocal()
    try:
        if db.query(User).count():
            return
        if settings.auth_password:
            h, salt = hash_password(settings.auth_password)
            db.add(User(username=settings.auth_user, password_hash=h,
                        salt=salt, role="admin"))
        for bagian in settings.tech_users.split(","):
            bagian = bagian.strip()
            if not bagian:
                continue
            nama, _, sandi = bagian.partition(":")
            if nama.strip() and sandi.strip():
                h, salt = hash_password(sandi.strip())
                db.add(User(username=nama.strip(), password_hash=h,
                            salt=salt, role="teknisi"))
        db.commit()
    finally:
        db.close()
