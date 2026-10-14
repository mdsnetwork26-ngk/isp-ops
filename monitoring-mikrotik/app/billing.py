"""Integrasi status tagihan dengan aplikasi penagihan yang sudah berjalan.

Default: NONAKTIF (status bayar ditentukan manual oleh teknisi / otomatis
di-set 'lunas' saat tombol Buka Isolir diklik).

Kalau mau otomatis, set di .env:
    BILLING_ENABLED=true
    BILLING_DB_HOST=127.0.0.1
    BILLING_DB_USER=billing_ro
    BILLING_DB_PASS=xxxx
    BILLING_DB_NAME=billing
    BILLING_SQL="SELECT customer_code AS code FROM invoices WHERE status='unpaid' AND due_date < CURDATE()"

Query harus mengembalikan satu kolom bernama `code` yang berisi kode pelanggan
(sama dengan kolom billing_code di tabel customers ISP-OPS).
Butuh: pip install pymysql
"""
from __future__ import annotations

import time

from .config import settings

_cache: set[str] = set()
_cache_at: float = 0.0
_last_error: str = ""


def status() -> dict:
    return {
        "enabled": settings.billing_enabled,
        "cached": len(_cache),
        "age_seconds": int(time.time() - _cache_at) if _cache_at else None,
        "last_error": _last_error,
    }


def unpaid_map(force: bool = False) -> set[str]:
    """Kembalikan himpunan kode pelanggan yang masih menunggak.

    Set kosong artinya 'tidak ada yang menunggak' atau integrasi nonaktif.
    """
    global _cache, _cache_at, _last_error
    if not settings.billing_enabled:
        return set()
    if not force and _cache_at and (time.time() - _cache_at) < settings.billing_cache_ttl:
        return _cache
    try:
        import pymysql  # type: ignore

        conn = pymysql.connect(
            host=settings.billing_host,
            port=settings.billing_port,
            user=settings.billing_user,
            password=settings.billing_password,
            database=settings.billing_name,
            connect_timeout=5,
            read_timeout=10,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(settings.billing_sql)
                rows = cur.fetchall()
        finally:
            conn.close()
        _cache = {str(r[0]).strip() for r in rows if r and r[0]}
        _cache_at = time.time()
        _last_error = ""
    except Exception as exc:  # noqa: BLE001
        _last_error = f"{type(exc).__name__}: {exc}"
    return _cache
