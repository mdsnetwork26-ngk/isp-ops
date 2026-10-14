"""Uji otomatis ISP-OPS.

Jalankan:  cd isp-ops && python -m pytest -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer



import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal, init_db
from app.mikrotik.fake import FakeRouter
from app.main import app
from app.models import Customer, Router
from app.services import pool
from app.util import format_uptime, norm_mac, onu_score, parse_uptime, pretty_mac


# --------------------------------------------------------------------- util

def test_parse_uptime():
    assert parse_uptime("1w2d3h4m5s") == 788645
    assert parse_uptime("3m12s") == 192
    assert parse_uptime("45s") == 45
    assert parse_uptime("") == 0
    assert parse_uptime(None) == 0


def test_format_uptime():
    assert format_uptime(0) == "-"
    assert format_uptime(45) == "45d"
    assert format_uptime(192) == "3m 12d"
    assert format_uptime(3725) == "1j 2m"
    assert format_uptime(90061) == "1 hari 1 jam"
    assert format_uptime(788645) == "1 minggu 2 hari"


def test_mac():
    assert norm_mac("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"
    assert pretty_mac("aabbccddeeff") == "AA:BB:CC:DD:EE:FF"
    assert pretty_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"


def test_onu_score_mac_cocok():
    body = "<html><title>ZTE F670L</title> MAC Address 4C:5E:0C:11:22:33 GPON ONU</html>"
    score, reason = onu_score(body, {"server": "mini web"}, "4c5e0c112233")
    assert score >= 90
    assert "MAC cocok" in reason
    score2, _ = onu_score(body, {}, "deadbeef0000")
    assert score2 < score


# --------------------------------------------------------------------- fixtures

@pytest.fixture(scope="session")
def client():
    # JANGAN os.remove file db: pool engine bisa masih memegang inode lama,
    # sehingga tulisannya masuk inode terhapus dan commit gagal "readonly".
    # Rekayasa ulang skema di engine hidup = deterministik dan aman.
    init_db()
    from app.database import Base, engine
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    fake = FakeRouter()
    pool.set_demo(fake)
    db = SessionLocal()
    # mulai dari keadaan bersih: satu router uji saja
    for r in db.query(Router).all():
        db.delete(r)
    db.commit()
    db.add(
        Router(
            name="TEST-Router", host="demo", port=8728, username="demo",
            password="demo", site="uji", enabled=True, poll_interval=5,
            manage_ip="10.10.10.1",
        )
    )
    db.commit()
    db.close()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def fake_router() -> FakeRouter:
    """Instance router demo yang SEDANG dipakai aplikasi (bukan tiruan lain)."""
    assert pool._demo is not None, "mode demo belum aktif"  # noqa: SLF001
    return pool._demo  # noqa: SLF001


# --------------------------------------------------------------------- API

def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_halaman_monitor(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Monitoring ONU" in res.text


def test_status_ada_pelanggan(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["routers"], "harus ada minimal 1 router terpantau"
    snap = data["routers"][0]
    assert snap["online"] is True, snap["message"]
    assert snap["stats"]["total"] >= 15
    sesi = snap["sessions"][0]
    for field in ("name", "ip", "mac", "uptime", "status"):
        assert field in sesi


def test_sinkron_pelanggan_dari_ppp_secret(client):
    rid = client.get("/api/routers").json()["routers"][0]["id"]
    res = client.post(f"/api/customers/sync?router_id={rid}")
    assert res.status_code == 200
    body = res.json()
    assert body["total_secret"] >= 15
    custs = client.get(f"/api/customers?router_id={rid}").json()["customers"]
    assert len(custs) >= 15
    assert any(c["name"] == "budi-santoso" for c in custs)


def test_isolir_lalu_buka(client, fake_router):
    rid = client.get("/api/routers").json()["routers"][0]["id"]
    client.post(f"/api/customers/sync?router_id={rid}")
    custs = client.get(f"/api/customers?router_id={rid}").json()["customers"]

    # ambil satu pelanggan yang benar-benar sedang online
    snap = client.post("/api/refresh", params={"router_id": rid}).json()["router"]
    online = {s["name"]: s for s in snap["sessions"] if s["status"] == "up"}
    assert online, "minimal harus ada satu sesi online di router demo"
    nama = sorted(online)[0]
    row = online[nama]
    ip_awal = row["ip"]
    cust = [c for c in custs if c["name"] == nama][0]

    # 1. isolir
    res = client.post("/api/isolir", json={"id": cust["id"], "reason": "belum bayar 2 bulan"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["ip"] == ip_awal
    assert ip_awal in fake_router.address_list_addresses(settings.isolir_list)
    assert fake_router.ppp_secrets()
    secret = [s for s in fake_router.ppp_secrets() if s.name == nama][0]
    assert secret.disabled is True, "PPP secret harus ikut di-disable"

    # 2. status di tabel berubah jadi isolated
    snap = client.post("/api/refresh", params={"router_id": rid}).json()["router"]
    row = [s for s in snap["sessions"] if s["name"] == nama][0]
    assert row["status"] == "isolated", row
    assert snap["stats"]["isolated"] >= 1

    # 3. sesi benar-benar putus dan tidak bisa konek lagi selama masih terisolir
    assert all(r["name"] != nama for r in fake_router.ppp_active())

    # 4. log tercatat
    logs = client.get("/api/logs").json()["logs"]
    assert any(l["action"] == "isolate" and l["customer_name"] == nama for l in logs)

    # 5. buka isolir
    res = client.post("/api/buka", json={"id": cust["id"]})
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert ip_awal not in fake_router.address_list_addresses(settings.isolir_list)
    secret = [s for s in fake_router.ppp_secrets() if s.name == nama][0]
    assert secret.disabled is False


def test_isolir_pelanggan_tidak_dikenal(client):
    res = client.post("/api/isolir", json={"name": "tidak-ada-namanya"})
    assert res.status_code == 404


def test_filter_status_dan_pencarian(client):
    rid = client.get("/api/routers").json()["routers"][0]["id"]
    data = client.get("/api/status", params={"router_id": rid, "status": "isolated"}).json()
    assert all(s["status"] == "isolated" for r in data["routers"] for s in r["sessions"])

    data = client.get("/api/status", params={"router_id": rid, "q": "budi-santoso"}).json()
    hits = [s for r in data["routers"] for s in r["sessions"]]
    assert len(hits) == 1 and hits[0]["name"] == "budi-santoso"

    # pencarian juga jalan lewat MAC tanpa titik dua
    mac = hits[0]["mac"].replace(":", "").lower()
    data = client.get("/api/status", params={"router_id": rid, "q": mac}).json()
    assert len([s for r in data["routers"] for s in r["sessions"]]) == 1


# --------------------------------------------------------------------- ONU

class _OnuHandler(BaseHTTPRequestHandler):
    mac = "4C:5E:0C:AA:BB:CC"

    def do_GET(self):  # noqa: N802
        body = (
            "<html><head><title>ZTE ZXHN F670L</title></head><body>"
            f"<h1>GPON ONU</h1><td>MAC Address</td><td>{self.mac}</td>"
            "<a href='/status.asp'>status</a>"
            f"<img src='http://{self.server.server_address[0]}:{self.server.server_address[1]}/logo.png'>"
            "</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Server", "Mini web server")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def onu_server():
    _OnuHandler.mac = "4C:5E:0C:AA:BB:CC"
    srv = HTTPServer(("127.0.0.1", 0), _OnuHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address
    srv.shutdown()


def test_proxy_halaman_onu(client, onu_server):
    ip, port = onu_server
    rid = client.get("/api/routers").json()["routers"][0]["id"]
    res = client.get(f"/proxy/{rid}/http/{ip}:{port}/")
    assert res.status_code == 200
    assert "GPON ONU" in res.text
    assert f'<base href="/proxy/{rid}/http/{ip}:{port}/">' in res.text
    assert f"http://{ip}:{port}/logo.png" not in res.text  # link absolut ikut dialihkan


def test_proxy_tolak_ip_publik(client):
    rid = client.get("/api/routers").json()["routers"][0]["id"]
    res = client.get(f"/proxy/{rid}/http/8.8.8.8/")
    assert res.status_code == 403


def test_nama_pelanggan_beda_tanda_tetap_cocok(client):
    """Regresi: 'budi-santoso' (DB) vs 'budi_santoso' (PPP secret) = orang yang sama."""
    from app.services import norm_name

    assert norm_name("Budi-Santoso") == norm_name("budi_santoso") == norm_name("budi.santoso")

    rid = client.get("/api/routers").json()["routers"][0]["id"]
    client.post(f"/api/customers/sync?router_id={rid}")
    snap = client.post("/api/refresh", params={"router_id": rid}).json()["router"]
    cocok = [s for s in snap["sessions"] if norm_name(s["name"]) == "budisantoso"]
    assert len(cocok) == 1, cocok
    assert cocok[0]["customer_id"] is not None, "harus terhubung ke data pelanggan di database"

    data = client.get("/api/status", params={"router_id": rid, "q": "budi-santoso"}).json()
    hits = [s for r in data["routers"] for s in r["sessions"]]
    assert len(hits) == 1 and hits[0]["customer_id"] is not None
