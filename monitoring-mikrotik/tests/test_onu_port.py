"""Deteksi otomatis port web Remote ONU/router.

Port pelanggan = 0 berarti "otomatis": server mencoba ONU_PORT_CANDIDATES
(80, 8080, 8000), lalu menyimpan hasilnya sebagai cache. Port yang diisi
teknisi (angka != 0) tetap menang dan tidak ditimpa hasil deteksi.
"""
from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app import onu as onu_mod
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import Customer, Router


# --------------------------------------------------------------------- bantu

def _server() -> HTTPServer:
    """Web tiruan di port acak localhost (pengganti ONU/router)."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"<html><title>ONU uji</title><body>ok</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _port_tertutup() -> int:
    """Port yang dijamin tidak ada pendengar (bind lalu tutup)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def klien():
    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        db.query(Customer).delete()
        db.query(Router).delete()
        db.commit()
        r = Router(name="R-PORT", host="127.0.0.1", port=8728, username="u",
                   password="p", enabled=False, poll_interval=0)
        db.add(r)
        db.commit()
        c = Customer(router_id=r.id, name="uji-port", onu_port=0, ip="127.0.0.1")
        db.add(c)
        db.commit()
        cid, rid = c.id, r.id
    finally:
        db.close()
    with TestClient(app) as tc:
        yield tc, cid, rid
    db = SessionLocal()
    try:
        db.query(Customer).delete()
        db.query(Router).delete()
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------- unit

def test_detect_port_pilih_yang_terbuka():
    srv = _server()
    port = srv.server_address[1]
    tertutup = _port_tertutup()
    try:
        hasil = asyncio.run(onu_mod.detect_port(
            "127.0.0.1", candidates=[tertutup, port], timeout=0.5))
        assert hasil["open"] is True
        assert hasil["port"] == port
    finally:
        srv.shutdown()


def test_detect_port_prioritas_port_tersimpan():
    a, b = _server(), _server()
    pa, pb = a.server_address[1], b.server_address[1]
    try:
        hasil = asyncio.run(onu_mod.detect_port(
            "127.0.0.1", configured=pa, candidates=[pa, pb], timeout=0.5))
        assert hasil["port"] == pa, "port tersimpan harus didahulukan"
    finally:
        a.shutdown()
        b.shutdown()


def test_detect_port_semua_tertutup():
    p1, p2 = _port_tertutup(), _port_tertutup()
    hasil = asyncio.run(onu_mod.detect_port(
        "127.0.0.1", candidates=[p1, p2], timeout=0.3))
    assert hasil["open"] is False and hasil["port"] == 0
    assert hasil["tried"] == [p1, p2]


# --------------------------------------------------------------------- endpoint

def test_endpoint_deteksi_lalu_cache(klien, monkeypatch):
    tc, cid, _rid = klien
    srv = _server()
    port = srv.server_address[1]
    monkeypatch.setattr(settings, "onu_port_candidates", f"80,{port}")
    try:
        r = tc.post("/api/onu/port", json={"id": cid, "ip": "127.0.0.1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "probe" and body["port"] == port

        # klik kedua memakai cache, bukan probe ulang
        srv.shutdown()
        r2 = tc.post("/api/onu/port", json={"id": cid, "ip": "127.0.0.1"})
        assert r2.json()["source"] == "cache"
        assert r2.json()["port"] == port
    finally:
        srv.shutdown()


def test_endpoint_port_tersimpan_menang(klien, monkeypatch):
    tc, cid, _rid = klien
    srv = _server()
    port = srv.server_address[1]
    db = SessionLocal()
    try:
        c = db.get(Customer, cid)
        c.onu_port = port
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(settings, "onu_port_candidates", "80,8080")
    try:
        body = tc.post("/api/onu/port",
                       json={"id": cid, "ip": "127.0.0.1"}).json()
        assert body["source"] == "config" and body["port"] == port
    finally:
        srv.shutdown()


def test_port_tersimpan_mati_deteksi_ulang_tanpa_menimpa(klien, monkeypatch):
    tc, cid, _rid = klien
    mati = _port_tertutup()
    db = SessionLocal()
    try:
        c = db.get(Customer, cid)
        c.onu_port = mati          # teknisi mengisi port yang ternyata salah
        db.commit()
    finally:
        db.close()

    srv = _server()
    port = srv.server_address[1]
    monkeypatch.setattr(settings, "onu_port_candidates", f"80,{port}")
    try:
        body = tc.post("/api/onu/port",
                       json={"id": cid, "ip": "127.0.0.1"}).json()
        assert body["source"] == "probe" and body["port"] == port
    finally:
        srv.shutdown()

    db = SessionLocal()
    try:
        c = db.get(Customer, cid)
        assert c.onu_port == mati, "port pilihan teknisi tidak boleh ditimpa"
        assert c.onu_port_detected == port
    finally:
        db.close()


def test_endpoint_semua_port_mati_pakai_cadangan(klien, monkeypatch):
    tc, cid, _rid = klien
    # pakai port yang dijamin tertutup, supaya tes tidak bergantung port host
    p1, p2 = _port_tertutup(), _port_tertutup()
    monkeypatch.setattr(settings, "onu_port_candidates", f"{p1},{p2}")
    body = tc.post("/api/onu/port",
                   json={"id": cid, "ip": "127.0.0.1"}).json()
    assert body["ok"] is False
    assert body["source"] == "default"
    assert body["port"] == settings.onu_default_port


def test_endpoint_refresh_paksa_deteksi_ulang(klien, monkeypatch):
    tc, cid, _rid = klien
    lama = _server()
    baru = _server()
    port_lama, port_baru = lama.server_address[1], baru.server_address[1]
    try:
        monkeypatch.setattr(settings, "onu_port_candidates", f"80,{port_lama}")
        body = tc.post("/api/onu/port",
                       json={"id": cid, "ip": "127.0.0.1"}).json()
        assert body["port"] == port_lama

        # perangkat diganti ke port lain: refresh harus mengabaikan cache
        lama.shutdown()
        monkeypatch.setattr(settings, "onu_port_candidates", f"80,{port_baru}")
        body = tc.post("/api/onu/port",
                       json={"id": cid, "ip": "127.0.0.1", "refresh": True}).json()
        assert body["source"] == "probe" and body["port"] == port_baru
    finally:
        lama.shutdown()
        baru.shutdown()


def test_endpoint_deteksi_port_8080(klien, monkeypatch):
    """Router tenant baru yang web-nya HANYA di port 8080 harus terdeteksi,
    bukan paksa port 80 yang kemudian mengakibatkan 502."""
    tc, cid, _rid = klien
    srv8080 = _server()  # hanya 8080 yang nyala
    port8080 = srv8080.server_address[1]
    monkeypatch.setattr(settings, "onu_port_candidates", f"80,{port8080}")
    try:
        body = tc.post("/api/onu/port",
                       json={"id": cid, "ip": "127.0.0.1"}).json()
        assert body["ok"] is True, body
        assert body["port"] == port8080, body
        assert body["source"] == "probe", body

        # deteksi disimpan ke cache sebagai onu_port_detected
        db = SessionLocal()
        try:
            c = db.get(Customer, cid)
            assert c.onu_port_detected == port8080
        finally:
            db.close()
    finally:
        srv8080.shutdown()
