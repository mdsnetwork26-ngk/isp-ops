"""Allow-list host router untuk proxy ONU ber-IP publik (situs luar via VPN L2TP
yang ONU-nya dijangkau lewat port-mapping publik). IP publik selain host router
terdaftar tetap wajib ditolak (anti open-proxy)."""
from __future__ import annotations

from tests.test_remote_onu_multipage import client_onu  # noqa: F401  (fixture)

import pytest

from app.onu import boleh_proxy


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, content=None, headers=None):
        import httpx
        raise httpx.ConnectError("stub-tak-ada-rute")


def test_boleh_proxy_unit():
    assert boleh_proxy("10.1.1.1")
    assert boleh_proxy("192.168.1.1", None)
    assert boleh_proxy("100.64.0.5")               # CGNAT = privat
    assert not boleh_proxy("8.8.8.8")
    assert not boleh_proxy("8.8.8.8", set())
    assert not boleh_proxy("8.8.8.8", {"1.1.1.1"})
    assert boleh_proxy("8.8.8.8", {"8.8.8.8"})
    assert boleh_proxy("10.1.1.1", {"8.8.8.8"})


def test_host_publik_hanya_untuk_router_terdaftar(client_onu, monkeypatch):  # noqa: F811
    from app.database import SessionLocal
    from app.models import Router

    db = SessionLocal()
    db.query(Router).delete()
    db.add(Router(name="SITE-L2TP", host="93.184.216.34", port=45536,
                  username="u", password="p", site="luar"))
    db.commit()
    db.close()

    # IP publik BUKAN router terdaftar -> 403 (tanpa mencoba koneksi)
    r = client_onu.get("/", headers={"host": "onu-8-8-8-8-80.onu.test"})
    assert r.status_code == 403
    assert "bukan IP privat" in r.text

    # IP publik YANG TERDAFTAR sebagai router -> guard lolos: aplikasi mencoba
    # koneksi; dengan AsyncClient palsu hasilnya 502 deterministik berisi URL
    from app import onu as onu_mod
    monkeypatch.setattr(onu_mod.httpx, "AsyncClient", _FakeClient)
    r = client_onu.get("/", headers={"host": "onu-93-184-216-34-80.onu.test"})
    assert r.status_code == 502, r.status_code
    assert "http://93.184.216.34/" in r.text
