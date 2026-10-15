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
    """Mode suspend (alur baru): profil SUSPEND dianggap SUDAH dibuat
    manual oleh operator di MikroTik (router tiruan memang sudah
    memilikinya). Isolir harus: simpan profil asal, pindah secret ke
    profil suspend (TIDAK di-disable), lalu HAPUS sesi PPPoE aktif supaya
    klien login ulang ke profil suspend. Aplikasi tidak boleh membuat
    pool/profil/rule firewall/address-list apa pun. Buka adalah
    kebalikannya: profil asal dikembalikan lalu sesi aktif dihapus lagi
    supaya klien lepas dari profil suspend."""
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
            det = r.json().get("detail", "")
            assert "profil suspend 'SUSPEND'" in det, det
            assert "sesi PPPoE aktif dihapus" in det, det

            # 1) aplikasi TIDAK membuat apa pun di router
            assert not _perintah(srv, "/ip/pool/add"), _perintah(srv, "/ip/pool/add")
            assert not _perintah(srv, "/ppp/profile/add"), _perintah(srv, "/ppp/profile/add")
            assert not _perintah(srv, "/ip/firewall/filter/insert")
            assert not _perintah(srv, "/ip/firewall/filter/add")
            assert not _perintah(srv, "/ip/firewall/address-list/add")

            # 2) secret DIPINDAH ke profil suspend, TIDAK di-disable
            sets = _perintah(srv, "/ppp/secret/set")
            assert any("=profile=SUSPEND" in w for w in sets), sets
            assert not any("=disabled=yes" in w for w in sets), \
                "mode suspend tidak boleh men-disable secret"

            # 3) sesi aktif dihapus supaya klien berpindah profil
            assert _perintah(srv, "/ppp/active/remove")

            # 4) profil asli tercatat di database + status jadi ISOLIR
            db = SessionLocal()
            c2 = db.get(Customer, cid)
            assert c2.isolated and c2.ppp_profile == "pppoe-12m", \
                (c2.isolated, c2.ppp_profile)
            db.close()
            d = c.get(f"/api/status?router_id={rid}").json()["routers"][0]
            baris = [s for s in d["sessions"] if s["name"] == "budi-santoso"][0]
            assert baris["status"] == "isolated"

            # ---- tombol Buka: profil asal kembali + sesi dihapus lagi ----
            kick_sebelum = len(_perintah(srv, "/ppp/active/remove"))
            r2 = c.post("/api/buka", json={"id": cid})
            assert r2.status_code == 200, r2.text
            assert r2.json()["ok"], r2.text
            det2 = r2.json().get("detail", "")
            assert "profil asal 'pppoe-12m'" in det2, det2
            assert "lepas dari profil suspend" in det2, det2
            sets2 = _perintah(srv, "/ppp/secret/set")
            assert any("=profile=pppoe-12m" in w for w in sets2), sets2
            assert any("=disabled=no" in w for w in sets2), sets2
            assert len(_perintah(srv, "/ppp/active/remove")) > kick_sebelum, \
                "Buka isolir wajib menghapus sesi PPPoE aktif lagi"
            assert srv.secret_profiles["budi-santoso"] == "pppoe-12m"
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


def test_isolir_suspend_gagal_bila_profil_suspend_belum_dibuat():
    """Profil suspend adalah buatan operator: bila namanya tidak ada di
    router, isolir harus DIBATALKAN (bukan membuat profil sendiri) dan
    tidak boleh ada perubahan apa pun di secret maupun di database."""
    from app.config import settings

    srv = FakeMikrotik().start()
    mode_lama, profil_lama = settings.isolir_mode, settings.suspend_profile
    settings.isolir_mode = "suspend"
    settings.suspend_profile = "PROFIL-BELUM-ADA"
    try:
        with TestClient(app) as c:
            rid = c.post("/api/routers", json={
                "id": None, "name": "E2E-SUSPEND-TIDAK-ADA", "host": "127.0.0.1",
                "port": srv.port, "username": "ispops", "password": "rahasia",
                "site": "", "enabled": True, "poll_interval": 10,
                "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            assert c.post(f"/api/customers/sync?router_id={rid}").status_code == 200
            db = SessionLocal()
            cust = db.query(Customer).filter(Customer.router_id == rid).first()
            cid = cust.id
            db.close()

            r = c.post("/api/isolir", json={"id": cid, "reason": "belum bayar"})
            assert r.status_code == 400, r.text
            assert not r.json()["ok"], r.text
            assert "PROFIL-BELUM-ADA" in r.json().get("error", ""), r.text
            assert "SUSPEND" in r.json().get("error", ""), \
                "pesan galat harus menyebut profil yang tersedia"
            assert not _perintah(srv, "/ppp/profile/add")
            assert srv.secret_profiles["budi-santoso"] == "pppoe-12m"

            db = SessionLocal()
            c2 = db.get(Customer, cid)
            assert not c2.isolated and c2.ppp_profile == "", (c2.isolated, c2.ppp_profile)
            db.close()
            c.post(f"/api/routers/{rid}/delete", json={})
    finally:
        settings.isolir_mode, settings.suspend_profile = mode_lama, profil_lama
        srv.stop()


def test_isolir_suspend_berulang_dan_buka_bila_profil_asal_hilang():
    """Isolir dua kali tidak boleh menimpa profil asal dengan profil
    suspend. Buka saat profil asal sudah terhapus dari router harus gagal
    TANPA menghapus catatan profil asal -- dan berhasil lagi setelah
    profil asal dibuat kembali."""
    from app.config import settings

    srv = FakeMikrotik().start()
    mode_lama = settings.isolir_mode
    settings.isolir_mode = "suspend"
    try:
        with TestClient(app) as c:
            rid = c.post("/api/routers", json={
                "id": None, "name": "E2E-SUSPEND-ULANG", "host": "127.0.0.1",
                "port": srv.port, "username": "ispops", "password": "rahasia",
                "site": "", "enabled": True, "poll_interval": 10,
                "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            assert c.post(f"/api/customers/sync?router_id={rid}").status_code == 200
            db = SessionLocal()
            cust = db.query(Customer).filter(Customer.router_id == rid).first()
            cid = cust.id
            db.close()

            for _ in range(2):
                r = c.post("/api/isolir", json={"id": cid, "reason": "belum bayar"})
                assert r.json()["ok"], r.text
            db = SessionLocal()
            c2 = db.get(Customer, cid)
            assert c2.ppp_profile == "pppoe-12m", c2.ppp_profile
            db.close()

            srv.profiles.remove("pppoe-12m")  # profil asal terhapus di router
            r2 = c.post("/api/buka", json={"id": cid})
            assert not r2.json()["ok"], r2.text
            assert "pppoe-12m" in r2.json().get("error", ""), r2.text
            db = SessionLocal()
            c3 = db.get(Customer, cid)
            assert c3.isolated and c3.ppp_profile == "pppoe-12m", \
                (c3.isolated, c3.ppp_profile)
            db.close()

            srv.profiles.append("pppoe-12m")  # operator membuat lagi profilnya
            r3 = c.post("/api/buka", json={"id": cid})
            assert r3.json()["ok"], r3.text
            assert srv.secret_profiles["budi-santoso"] == "pppoe-12m"
            c.post(f"/api/routers/{rid}/delete", json={})
    finally:
        settings.isolir_mode = mode_lama
        srv.stop()


def test_suspend_tetap_menghapus_sesi_meski_kick_setting_mati():
    """ISOLIR_KICK_ACTIVE=false tidak boleh membatalkan penghapusan sesi
    di mode suspend -- tanpa itu klien tidak berpindah profil."""
    from app.config import settings

    srv = FakeMikrotik().start()
    mode_lama, kick_lama = settings.isolir_mode, settings.isolir_kick_active
    settings.isolir_mode = "suspend"
    settings.isolir_kick_active = False
    try:
        with TestClient(app) as c:
            rid = c.post("/api/routers", json={
                "id": None, "name": "E2E-SUSPEND-KICK", "host": "127.0.0.1",
                "port": srv.port, "username": "ispops", "password": "rahasia",
                "site": "", "enabled": True, "poll_interval": 10,
                "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            assert c.post(f"/api/customers/sync?router_id={rid}").status_code == 200
            db = SessionLocal()
            cust = db.query(Customer).filter(Customer.router_id == rid).first()
            cid = cust.id
            db.close()

            r = c.post("/api/isolir", json={"id": cid, "reason": "belum bayar"})
            assert r.json()["ok"], r.text
            assert _perintah(srv, "/ppp/active/remove")
            kick1 = len(_perintah(srv, "/ppp/active/remove"))
            r2 = c.post("/api/buka", json={"id": cid})
            assert r2.json()["ok"], r2.text
            assert len(_perintah(srv, "/ppp/active/remove")) > kick1
            c.post(f"/api/routers/{rid}/delete", json={})
    finally:
        settings.isolir_mode, settings.isolir_kick_active = mode_lama, kick_lama
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
            assert "SUSPEND" in r.json().get("detail", ""), r.text

            # tidak menyentuh address-list sama sekali
            assert not _perintah(srv, "/ip/firewall/address-list/add")
            # secret tetap dipindah ke profil suspend
            assert any("=profile=SUSPEND" in w for w in _perintah(srv, "/ppp/secret/set"))

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
