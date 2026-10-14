"""Uji skala & regresi yang ditemukan saat menyiapkan jaringan 1500 pelanggan."""
from __future__ import annotations

import os
import sys



import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal, init_db
from app.main import app
from app.mikrotik.fake import FakeRouter
from app.models import Customer, Router
from app.services import pool


@pytest.fixture(scope="module")
def client_besar():
    # jangan hapus file db (inode lama di pool -> "readonly database");
    # bangun ulang skema di engine hidup
    init_db()
    from app.database import Base, engine
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    pool.set_demo(FakeRouter(jumlah=1500))
    db = SessionLocal()
    for r in db.query(Router).all():
        db.delete(r)
    db.commit()
    db.add(Router(name="SKALA", host="demo", port=8728, username="demo",
                  password="demo", enabled=True, poll_interval=10, manage_ip="10.10.10.1"))
    db.commit()
    db.close()
    with TestClient(app) as c:
        yield c


def _rid(c):
    return c.get("/api/routers").json()["routers"][0]["id"]


def test_last_seen_tidak_crash_karena_timezone(client_besar):
    """Regresi: SQLite memberi datetime naive; membandingkannya dengan
    datetime.now(timezone.utc) melempar TypeError dan mematikan poller."""
    rid = _rid(client_besar)
    assert client_besar.post(f"/api/customers/sync?router_id={rid}").status_code == 200
    # dua putaran: putaran kedua membandingkan last_seen yang sudah tersimpan
    for _ in range(2):
        r = client_besar.post("/api/refresh", params={"router_id": rid})
        assert r.status_code == 200, r.text
        assert r.json()["router"]["stats"]["total"] == 1500
    db = SessionLocal()
    ada = db.query(Customer).filter(Customer.last_seen.isnot(None)).count()
    db.close()
    assert ada > 0, "last_seen harus terisi untuk pelanggan yang online"


def test_limit_mengurangi_kiriman_tapi_statistik_tetap_penuh(client_besar):
    rid = _rid(client_besar)
    client_besar.post("/api/refresh", params={"router_id": rid})
    d = client_besar.get(f"/api/status?router_id={rid}&limit=100").json()["routers"][0]
    assert len(d["sessions"]) == 100
    assert d["matched"] == 1500
    assert d["stats"]["total"] == 1500, "statistik harus dihitung dari semua pelanggan"

    semua = client_besar.get(f"/api/status?router_id={rid}&limit=0").json()["routers"][0]
    assert len(semua["sessions"]) == 1500


def test_pencarian_di_1500_pelanggan(client_besar):
    rid = _rid(client_besar)
    d = client_besar.get(f"/api/status?router_id={rid}&q=pelanggan-01234&limit=100").json()
    hits = [s for r in d["routers"] for s in r["sessions"]]
    assert len(hits) == 1
    assert hits[0]["name"] == "pelanggan-01234"
