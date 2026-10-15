"""Halaman isolir publik untuk klien profil suspend.

Yang dibuktikan:
- /isolir bisa dibuka TANPA login dan memuat pesan masa isolir,
- URL uji konektivitas OS juga menjawab halaman yang sama (portal HP),
- pengunjung dari IP pelanggan terisolir diarahkan ke /isolir dari URL
  apa pun dan namanya tampil di halaman,
- tombol WhatsApp muncul bila ISOLIR_WA diisi.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import Customer, Router


def test_halaman_isolir_publik():
    with TestClient(app) as c:
        r = c.get("/isolir")
        assert r.status_code == 200, r.text
        assert "Masa Isolir" in r.text
        assert "no-store" in r.headers.get("cache-control", "")
        # alias + URL uji konektivitas OS menjawab halaman isolir juga
        for jalur in ("/terisolir", "/generate_204", "/hotspot-detect.html",
                      "/connecttest.txt"):
            r2 = c.get(jalur)
            assert r2.status_code == 200, (jalur, r2.status_code)
            assert "Masa Isolir" in r2.text, jalur


def test_klien_terisolir_diarahkan_dan_dikenali():
    """IP TestClient adalah 'testclient': jadikan itu IP pelanggan
    terisolir, maka URL apa pun harus berbelok ke /isolir dan halaman
    menampilkan nama + keterangan pelanggan itu."""
    db = SessionLocal()
    try:
        rtr = db.query(Router).first()
        if rtr is None:
            rtr = Router(name="UJI-ISOLIR", host="127.0.0.1", port=8728,
                         username="u", password="p")
            db.add(rtr)
            db.commit()
            db.refresh(rtr)
        cust = Customer(name="uji-isolir", full_name="Budi Uji Isolir",
                        router_id=rtr.id, source="manual",
                        ip="testclient", isolated=True,
                        isolated_reason="belum bayar")
        db.add(cust)
        db.commit()
        cid = cust.id
    finally:
        db.close()
    wa_lama, kontak_lama = settings.isolir_wa, settings.isolir_kontak
    settings.isolir_wa, settings.isolir_kontak = "0812-3456-7890", "Admin Uji"
    try:
        with TestClient(app, follow_redirects=False) as c:
            # URL asal bebas -> dibelokkan ke halaman isolir
            r = c.get("/apa-saja-yang-dibuka-klien")
            assert r.status_code == 302, r.text
            assert r.headers["location"] == "/isolir"

            r2 = c.get("/isolir")
            assert r2.status_code == 200, r2.text
            assert "Budi Uji Isolir" in r2.text
            assert "belum bayar" in r2.text
            # 0812-... harus diubah jadi tautan wa.me/62812...
            assert "wa.me/6281234567890" in r2.text
            assert "Admin Uji" in r2.text
    finally:
        settings.isolir_wa, settings.isolir_kontak = wa_lama, kontak_lama
        db = SessionLocal()
        try:
            obj = db.get(Customer, cid)
            if obj is not None:
                db.delete(obj)
                db.commit()
        finally:
            db.close()


def test_lalu_lintas_asing_tidak_diarahkan_ke_login():
    """Lalu lintas HTTP klien suspend yang ter-masquerade router (IP
    sumbernya IP router, tidak bisa dikenali per-pelanggan) membuka URL
    situs asal yang acak. Itu TIDAK boleh dibelokkan ke halaman login
    operator -- harus ke halaman isolir. Halaman aplikasi yang sah
    tetap ke login."""
    sandi_lama = settings.auth_password
    settings.auth_password = "sandi-uji"  # nyalakan login
    try:
        with TestClient(app, follow_redirects=False) as c:
            r = c.get("/situs-acak-yang-dibuka-klien")
            assert r.status_code == 302, r.text
            assert r.headers["location"] == "/isolir", r.headers

            r2 = c.get("/generate_204")
            assert r2.status_code == 200 and "Masa Isolir" in r2.text

            r3 = c.get("/")
            assert r3.status_code == 302
            assert r3.headers["location"] == "/login", r3.headers
    finally:
        settings.auth_password = sandi_lama


def test_host_portal_brave_tidak_ke_login_operator():
    """Brave memeriksa portal lewat detectportal.brave-http-only.com/login
    -- jalurnya bertabrakan dengan /login operator. Dari Host-nya harus
    dikenali sebagai lalu lintas portal klien suspend -> /isolir, sedang
    /login dengan host aplikasi sendiri tetap halaman login."""
    sandi_lama = settings.auth_password
    settings.auth_password = "sandi-uji"
    try:
        with TestClient(app, follow_redirects=False) as c:
            r = c.get("/login", headers={
                "Host": "detectportal.brave-http-only.com"})
            assert r.status_code == 302, r.text
            assert r.headers["location"] == "/isolir", r.headers

            r2 = c.get("/generate_204", headers={
                "Host": "connectivitycheck.gstatic.com"})
            assert r2.status_code == 200 and "Masa Isolir" in r2.text

            r3 = c.get("/login", headers={"Host": "192.168.38.2:8000"})
            assert r3.status_code == 200, r3.text
            assert "Masuk" in r3.text
    finally:
        settings.auth_password = sandi_lama


def test_host_asing_diarahkan_otomatis_ke_isolir():
    """APP_HOSTS diisi: klien suspend membuka situs apa saja (Host asing,
    jalur "/" sekalipun) harus langsung teralihkan ke /isolir, sedang
    Host resmi aplikasi tetap melayani login/dashboard seperti biasa."""
    sandi_lama, hosts_lama = settings.auth_password, settings.app_hosts
    settings.auth_password = "sandi-uji"
    settings.app_hosts = "192.168.38.2"
    try:
        with TestClient(app, follow_redirects=False) as c:
            for host_asing in ("neverssl.com", "situs-berita.example"):
                r = c.get("/", headers={"Host": host_asing})
                assert r.status_code == 302, (host_asing, r.text)
                assert r.headers["location"] == "/isolir", r.headers
                # /login sengaja tetap bisa dijangkau (jalan darurat
                # admin); yang dibelokkan dari host asing adalah
                # halaman selain itu
                r = c.get("/login", headers={"Host": host_asing})
                assert r.status_code == 200, r.headers

            r = c.get("/", headers={"Host": "192.168.38.2:8000"})
            assert r.status_code == 302
            assert r.headers["location"] == "/login", r.headers
            r = c.get("/login", headers={"Host": "192.168.38.2:8000"})
            assert r.status_code == 200 and "Masuk" in r.text
    finally:
        settings.auth_password, settings.app_hosts = sandi_lama, hosts_lama


def test_sesi_admin_tidak_dibelokkan_ke_isolir():
    """Admin yang sudah login dan membuka panel lewat host yang tidak
    tercantum di APP_HOSTS (domain/alamat lain) TIDAK boleh terbelok ke
    halaman isolir -- sesi operator yang sah selalu menang."""
    from app import auth

    sandi_lama, hosts_lama = settings.auth_password, settings.app_hosts
    settings.auth_password = "sandi-uji"
    settings.app_hosts = "192.168.38.2"
    token = auth.create_session("admin-uji", "admin")
    try:
        with TestClient(app, follow_redirects=False) as c:
            c.cookies.set("ispops_sid", token)
            r = c.get("/", headers={"Host": "panel.alamat-lain.example"})
            assert r.status_code == 200, (r.status_code, r.headers)
            assert "Masa Isolir" not in r.text
    finally:
        auth.destroy_session(token)
        settings.auth_password, settings.app_hosts = sandi_lama, hosts_lama


def test_pengunjung_data_seluler_tidak_dibelokkan_ke_isolir():
    """HP dengan data seluler adalah pengunjung internet publik: membuka
    server dengan Host apa pun TIDAK boleh dibelokkan ke halaman isolir.
    Yang terbelok hanya lalu lintas dari jaringan sendiri (IP privat /
    klien suspend yang ter-dst-nat MikroTik)."""
    sandi_lama, hosts_lama = settings.auth_password, settings.app_hosts
    settings.auth_password = "sandi-uji"
    settings.app_hosts = "192.168.38.2,monitoring.marinagih.my.id"
    try:
        with TestClient(app, follow_redirects=False) as c:
            # IP publik operator seluler (lewat header proxy tepercaya)
            r = c.get("/", headers={"Host": "neverssl.com",
                                    "X-Real-IP": "8.8.8.8"})
            assert r.status_code == 302, r.text
            assert r.headers["location"] == "/login", r.headers

            # IP privat jaringan sendiri -> tetap terbelok ke isolir
            r2 = c.get("/", headers={"Host": "neverssl.com",
                                     "X-Real-IP": "10.200.0.23"})
            assert r2.status_code == 302, r2.text
            assert r2.headers["location"] == "/isolir", r2.headers
    finally:
        settings.auth_password, settings.app_hosts = sandi_lama, hosts_lama
