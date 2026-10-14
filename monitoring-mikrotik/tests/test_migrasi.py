"""Regresi: database lama harus tetap bisa menambah router (tidak HTTP 500).

Kasus nyata di lapangan: menu Pengaturan -> tambah mikrotik -> 'Gagal
menyimpan: HTTP 500'. Sebabnya INSERT memuat kolom api_ssl yang belum ada,
karena migrasi lama return lebih awal bila unique index customers sudah
ada (index polos buatan versi terdahulu).
"""
from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import text

from app.config import settings
from app.database import Base, engine, init_db

TABEL_LAMA = """
CREATE TABLE routers (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(80) NOT NULL UNIQUE,
    host VARCHAR(120) NOT NULL,
    port INTEGER,
    username VARCHAR(80),
    password VARCHAR(160),
    site VARCHAR(80),
    enabled BOOLEAN,
    poll_interval INTEGER,
    manage_ip VARCHAR(60),
    created_at DATETIME
);
CREATE TABLE customers (
    id INTEGER NOT NULL PRIMARY KEY,
    router_id INTEGER,
    name VARCHAR(120)
);
"""


@pytest.fixture()
def pulihkan_skema():
    """Kembalikan skema baru sesudah tes, supaya tes lain tidak ketularan."""
    yield
    Base.metadata.drop_all(bind=engine)
    init_db()


def _db_lama(indeks: str) -> None:
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        for stmt in [x for x in (TABEL_LAMA + indeks).split(";") if x.strip()]:
            conn.execute(text(stmt))


def _simpan_router() -> None:
    from fastapi.testclient import TestClient
    from app.main import app

    auth = {"Authorization": "Basic " + base64.b64encode(
        f"{settings.auth_user}:{settings.auth_password}".encode()).decode()}
    with TestClient(app) as c:
        r = c.post("/api/routers", json={
            "id": None, "name": "MIKROTIK-LAMA", "host": "192.168.12.1", "port": 8729,
            "username": "ispops", "password": "x", "site": "", "enabled": True,
            "poll_interval": 15, "manage_ip": "", "api_ssl": "no",
        }, headers=auth)
        assert r.status_code == 200, f"menyimpan router gagal: {r.status_code} {r.text}"
        rid = r.json()["id"]
        row = [x for x in c.get("/api/routers", headers=auth).json()["routers"]
               if x["id"] == rid][0]
        assert row["api_ssl"] == "no"


@pytest.mark.parametrize("indeks", [
    "CREATE UNIQUE INDEX uq_customer_router_name ON customers (router_id, name)",
    "CREATE UNIQUE INDEX uq_customer_router_name ON customers (router_id, LOWER(name))",
], ids=["index-polos", "index-ekspresi"])
def test_db_lama_tetap_bisa_simpan_router(indeks, pulihkan_skema):
    _db_lama(indeks)
    init_db()  # seperti saat service start di server user
    with engine.begin() as conn:
        kolom = [r[1] for r in conn.execute(text("PRAGMA table_info(routers)"))]
    assert "api_ssl" in kolom, "migrasi harus menambah kolom api_ssl duluan"
    _simpan_router()
