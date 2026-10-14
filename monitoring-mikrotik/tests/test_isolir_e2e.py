"""Klik tombol Isolir harus MENULIS firewall ke mikrotik.

Dibuktikan ujung-ke-ujung lewat HTTP + server API tiruan yang ketat:
rule forward-drop dibuat, IP masuk address-list 'isolir', secret di-disable,
sesi diputus; dan tombol Buka membersihkan semuanya.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Customer
from tests.test_protocol import FakeMikrotik


def _perintah(srv, cmd):
    return [w for c, w in srv.commands if c == cmd]


def test_tombol_isolir_menulis_firewall_ke_mikrotik():
    srv = FakeMikrotik().start()
    try:
        with TestClient(app) as c:
            rid = c.post("/api/routers", json={
                "id": None, "name": "E2E-ISOLIR", "host": "127.0.0.1", "port": srv.port,
                "username": "ispops", "password": "rahasia", "site": "", "enabled": True,
                "poll_interval": 10, "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            assert c.post(f"/api/customers/sync?router_id={rid}").status_code == 200
            db = SessionLocal()
            cust = db.query(Customer).filter(Customer.router_id == rid).first()
            cid = cust.id
            db.close()

            r = c.post("/api/isolir", json={"id": cid, "reason": "belum bayar"})
            assert r.status_code == 200, r.text
            det = r.json().get("detail", "")
            assert "rule firewall" in det and "address-list" in det

            # 1) rule firewall forward ditulis ke mikrotik
            assert _perintah(srv, "/ip/firewall/filter/insert"), "rule isolir tidak ditulis"
            rule = _perintah(srv, "/ip/firewall/filter/insert")[0]
            assert "=chain=forward" in rule and "=action=drop" in rule
            assert "=src-address-list=isolir" in rule

            # 2) IP pelanggan masuk address-list 'isolir'
            add = _perintah(srv, "/ip/firewall/address-list/add")
            assert any("=address=10.20.3.4" in w and "=list=isolir" in w for w in add)

            # 3) secret di-disable dan sesi aktif diputus
            assert _perintah(srv, "/ppp/secret/set")
            assert _perintah(srv, "/ppp/active/remove")

            # 4) status di layar jadi ISOLIR
            d = c.get(f"/api/status?router_id={rid}").json()["routers"][0]
            baris = [s for s in d["sessions"] if s["name"] == "budi-santoso"][0]
            assert baris["status"] == "isolated"

            # ---- tombol Buka membersihkan ----
            r2 = c.post("/api/buka", json={"id": cid})
            assert r2.status_code == 200, r2.text
            assert _perintah(srv, "/ip/firewall/address-list/remove")
            d = c.get(f"/api/status?router_id={rid}").json()["routers"][0]
            baris = [s for s in d["sessions"] if s["name"] == "budi-santoso"][0]
            assert baris["status"] != "isolated"

            c.post(f"/api/routers/{rid}/delete", json={})
    finally:
        srv.stop()


def test_ping_dan_isolir_tanpa_baris_pelanggan():
    """Pelanggan terlihat di mikrotik tapi belum ada di database (belum sinkron):
    Ping dan Isolir tetap harus jalan; baris pelanggan dibuat otomatis."""
    srv = FakeMikrotik().start()
    try:
        with TestClient(app) as c:
            rid = c.post("/api/routers", json={
                "id": None, "name": "TANPA-SYNC", "host": "127.0.0.1", "port": srv.port,
                "username": "ispops", "password": "rahasia", "site": "", "enabled": True,
                "poll_interval": 10, "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            c.post("/api/refresh", params={"router_id": rid})

            p1 = c.post("/api/ping", json={"name": "budi-santoso", "router_id": rid})
            assert p1.status_code == 200, p1.text
            assert p1.json()["ok"] is True

            r = c.post("/api/isolir", json={"name": "budi-santoso", "router_id": rid,
                                            "reason": "belum bayar"})
            assert r.status_code == 200, r.text

            db = SessionLocal()
            ada = db.query(Customer).filter(Customer.name == "budi-santoso",
                                            Customer.router_id == rid).first()
            db.close()
            assert ada is not None and ada.isolated

            c.post(f"/api/routers/{rid}/delete", json={})
    finally:
        srv.stop()


# ============================================================================
# mode suspend (ISOLIR_MODE=suspend)
# ============================================================================
def test_isolir_mode_suspend():
    """Mode suspend: secret dipindah ke profil SUSPENDED (bukan di-disable),
    pool + profil + rule subnet dibuat otomatis, dan Buka memulihkan profil
    asli. Tidak boleh ada operasi address-list dari sisi aplikasi."""
    from app.config import settings

    srv = FakeMikrotik().start()
    mode_lama = settings.isolir_mode
    settings.isolir_mode = "suspend"
    try:
        with TestClient(app) as c:
            rid = c.post("/api/routers", json={
                "id": None, "name": "E2E-SUSPEND", "host": "127.0.0.1", "port": srv.port,
                "username": "ispops", "password": "rahasia", "site": "", "enabled": True,
                "poll_interval": 10, "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            assert c.post(f"/api/customers/sync?router_id={rid}").status_code == 200
            db = SessionLocal()
            cust = db.query(Customer).filter(Customer.router_id == rid).first()
            cid = cust.id
            db.close()

            r = c.post("/api/isolir", json={"id": cid, "reason": "belum bayar"})
            assert r.status_code == 200, r.text
            assert r.json()["ok"], r.text

            # 1) pool + profil suspend dibuat otomatis
            pool = _perintah(srv, "/ip/pool/add")
            assert any("=name=ISP-OPS-SUSPEND" in w for w in pool), pool
            prof = _perintah(srv, "/ppp/profile/add")
            assert any("=name=SUSPENDED" in w and "=remote-address=ISP-OPS-SUSPEND" in w
                       for w in prof), prof

            # 2) rule firewall untuk subnet suspend (insert atau fallback add)
            rule = _perintah(srv, "/ip/firewall/filter/insert") + \
                _perintah(srv, "/ip/firewall/filter/add")
            assert any("=src-address=10.254.254.0/24" in w and "=action=drop" in w
                       for w in rule), rule

            # 3) secret DIPINDAH profil, TIDAK di-disable
            sets = _perintah(srv, "/ppp/secret/set")
            assert any("=profile=SUSPENDED" in w for w in sets), sets
            assert not any("=disabled=yes" in w for w in sets), \
                "mode suspend tidak boleh men-disable secret"

            # 4) sesi aktif tetap diputus
            assert _perintah(srv, "/ppp/active/remove")

            # 5) profil asli tercatat di database + status jadi ISOLIR
            db = SessionLocal()
            c2 = db.get(Customer, cid)
            assert c2.isolated and c2.ppp_profile == "pppoe-12m", \
                (c2.isolated, c2.ppp_profile)
            db.close()
            d = c.get(f"/api/status?router_id={rid}").json()["routers"][0]
            baris = [s for s in d["sessions"] if s["name"] == "budi-santoso"][0]
            assert baris["status"] == "isolated"

            # ---- tombol Buka memulihkan profil asli ----
            r2 = c.post("/api/buka", json={"id": cid})
            assert r2.status_code == 200, r2.text
            assert r2.json()["ok"], r2.text
            sets2 = _perintah(srv, "/ppp/secret/set")
            assert any("=profile=pppoe-12m" in w for w in sets2), sets2
            assert any("=disabled=no" in w for w in sets2), sets2
            db = SessionLocal()
            c3 = db.get(Customer, cid)
            assert not c3.isolated and c3.ppp_profile == "", \
                (c3.isolated, c3.ppp_profile)
            db.close()
            d = c.get(f"/api/status?router_id={rid}").json()["routers"][0]
            baris = [s for s in d["sessions"] if s["name"] == "budi-santoso"][0]
            assert baris["status"] != "isolated"

            c.post(f"/api/routers/{rid}/delete", json={})
    finally:
        settings.isolir_mode = mode_lama
        srv.stop()


def test_suspend_isolir_klien_offline():
    """Mode suspend harus bisa mengisolir klien yang sedang OFFLINE tanpa IP
    sama sekali, karena blok menempel di akun (profil), bukan di IP."""
    from app.config import settings

    srv = FakeMikrotik().start()
    srv.tanpa_aktif = True  # router tidak punya sesi aktif: semua pelanggan offline
    mode_lama = settings.isolir_mode
    settings.isolir_mode = "suspend"
    try:
        with TestClient(app) as c:
            rid = c.post("/api/routers", json={
                "id": None, "name": "E2E-SUSPEND-OFF", "host": "127.0.0.1", "port": srv.port,
                "username": "ispops", "password": "rahasia", "site": "", "enabled": True,
                "poll_interval": 10, "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            # pelanggan manual: tanpa IP, tanpa ONU, tanpa sesi aktif
            cid = c.post("/api/customers", json={
                "id": None, "router_id": rid, "name": "budi-santoso",
            }).json()["id"]

            r = c.post("/api/isolir", json={"id": cid, "reason": "belum bayar"})
            assert r.status_code == 200, r.text
            assert r.json()["ok"], r.text
            assert "SUSPENDED" in r.json().get("detail", ""), r.text

            # tidak menyentuh address-list sama sekali
            assert not _perintah(srv, "/ip/firewall/address-list/add")
            # secret tetap dipindah ke profil suspend
            assert any("=profile=SUSPENDED" in w for w in _perintah(srv, "/ppp/secret/set"))

            db = SessionLocal()
            c2 = db.get(Customer, cid)
            assert c2.isolated and c2.ppp_profile == "pppoe-12m" and c2.ip == "", \
                (c2.isolated, c2.ppp_profile, c2.ip)
            db.close()

            # ---- Buka saat masih offline juga harus jalan ----
            r2 = c.post("/api/buka", json={"id": cid})
            assert r2.status_code == 200, r2.text
            assert r2.json()["ok"], r2.text
            db = SessionLocal()
            c3 = db.get(Customer, cid)
            assert not c3.isolated and c3.ppp_profile == "", (c3.isolated, c3.ppp_profile)
            db.close()

            c.post(f"/api/routers/{rid}/delete", json={})
    finally:
        settings.isolir_mode = mode_lama
        srv.stop()


def test_buka_suspend_setelah_isolir_disable():
    """Isolir dilakukan saat masih mode disable (secret di-disable), lalu admin
    pindah ke mode suspend: tombol Buka tetap harus mengaktifkan secret lagi."""
    from app.config import settings

    srv = FakeMikrotik().start()
    mode_lama = settings.isolir_mode
    settings.isolir_mode = "disable"
    try:
        with TestClient(app) as c:
            rid = c.post("/api/routers", json={
                "id": None, "name": "E2E-PINDAH-MODE", "host": "127.0.0.1", "port": srv.port,
                "username": "ispops", "password": "rahasia", "site": "", "enabled": True,
                "poll_interval": 10, "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            assert c.post(f"/api/customers/sync?router_id={rid}").status_code == 200
            db = SessionLocal()
            cust = db.query(Customer).filter(Customer.router_id == rid).first()
            cid = cust.id
            db.close()

            r = c.post("/api/isolir", json={"id": cid, "reason": "belum bayar"})
            assert r.status_code == 200, r.text
            assert r.json()["ok"], r.text
            assert any("=disabled=yes" in w for w in _perintah(srv, "/ppp/secret/set"))

            # admin mengganti mode ke suspend, lalu menekan Buka
            settings.isolir_mode = "suspend"
            r2 = c.post("/api/buka", json={"id": cid})
            assert r2.status_code == 200, r2.text
            assert r2.json()["ok"], r2.text
            sets = _perintah(srv, "/ppp/secret/set")
            assert any("=disabled=no" in w for w in sets), sets

            c.post(f"/api/routers/{rid}/delete", json={})
    finally:
        settings.isolir_mode = mode_lama
        srv.stop()
