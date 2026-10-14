"""Kuota router berlangganan (ROUTER_LIMIT): tambah baru dibatasi,
ubah router lama tetap bebas, 0 = tanpa batas."""
from __future__ import annotations

from app.config import settings
from tests.test_app import client  # noqa: F401  (fixture)


def _payload(nama):
    return {"name": nama, "host": "127.0.0.1", "port": 8728,
            "username": "u", "password": "p"}


def test_kuota_menahan_router_baru(client):  # noqa: F811
    asal = settings.router_limit
    settings.router_limit = 1  # fixture client sudah punya 1 router
    try:
        r = client.post("/api/routers", json=_payload("R2"))
        assert r.status_code == 403
        assert "kuota" in r.json()["detail"].lower()

        # mengubah router LAMA tidak kena kuota (nama unik biar tak konflik)
        rid = client.get("/api/routers").json()["routers"][0]["id"]
        ubah = dict(_payload("UBAH-Router"), id=rid)
        assert client.post("/api/routers", json=ubah).status_code == 200
    finally:
        settings.router_limit = asal


def test_kuota_nol_tanpa_batas(client):  # noqa: F811
    assert settings.router_limit == 0
    r = client.post("/api/routers", json=_payload("RB"))
    assert r.status_code == 200
