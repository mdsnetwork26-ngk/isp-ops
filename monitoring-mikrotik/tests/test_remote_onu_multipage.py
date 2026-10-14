"""Regresi remote ONU untuk firmware MULTI-HALAMAN (HSGQ, ZimLink, VSOL, dsb).

China Mobile satu halaman: semua lewat XHR di halaman pertama -> lancar.
HSGQ/ZimLink: login -> cookie sesi -> frameset -> halaman .asp di SUBDIREKTORI
dengan link relatif dan redirect relatif. Tiga hal yang dulu merusak navigasi:

1. <base href="/"> disuntik padahal proxy memakai host sendiri (prefix kosong).
   Halaman /html/menu/menu.asp dengan link "../bbsp/wan.asp" jadi /bbsp/wan.asp
   (404 -> firmware melempar balik ke halaman awal = "looping").
2. Redirect relatif (302 Location: wan.asp) dipaksa jadi "/wan.asp".
3. Cookie sesi "Secure; SameSite=None" dibuang Secure-nya tapi SameSite=None
   dibiarkan -> browser menolak cookie -> tiap request tanpa sesi -> 302 ke
   halaman login/awal terus-menerus.

Mock di bawah meniru pola firmware tersebut; navigasi disimulasikan persis
seperti browser (urljoin dengan aturan <base>).
"""
from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient

from app.main import app

_SESI = {"token": ""}


class _HsgqHandler(BaseHTTPRequestHandler):
    """Mock ONU multi-halaman: sesi cookie + frameset + halaman subdirektori."""

    def _kirim(self, status, body=b"", headers=None, ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or []):
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _login(self):
        kuki = self.headers.get("Cookie", "")
        return bool(_SESI["token"]) and f"session={_SESI['token']}" in kuki

    def _tolak(self):
        # tanpa sesi -> firmware melempar balik ke halaman awal
        self._kirim(302, headers=[("Location", "/")])

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            self._kirim(200, (
                b"<html><head><title>HSGQ</title></head><body>"
                b'<form action="boaform/formLogin" method="post">'
                b'<input name="username"><input name="password" type="password">'
                b"</form></body></html>"
            ))
        elif p in ("/index.asp", "/html/menu/menu.asp",
                   "/html/bbsp/status.asp", "/html/bbsp/wan.asp",
                   "/html/menu/go.asp"):
            if not self._login():
                return self._tolak()
            if p == "/index.asp":
                self._kirim(200, (
                    b"<html><head></head><frameset cols='30%,70%'>"
                    b'<frame src="html/menu/menu.asp">'
                    b'<frame src="html/bbsp/status.asp">'
                    b"</frameset></html>"
                ))
            elif p == "/html/menu/menu.asp":
                self._kirim(200, (
                    b"<html><head></head><body>"
                    b'<a href="../bbsp/wan.asp">WAN</a>'
                    b'<a href="go.asp">Go</a>'
                    b"</body></html>"
                ))
            elif p == "/html/menu/go.asp":
                # redirect RELATIF dari dalam subdirektori
                self._kirim(302, headers=[("Location", "../bbsp/wan.asp")])
            elif p == "/html/bbsp/status.asp":
                self._kirim(200, b"<html><body>STATUS-OK</body></html>")
            else:
                self._kirim(200, b"<html><body>WAN-OK</body></html>")
        else:
            self._kirim(404, b"<html><body>404</body></html>")

    def do_POST(self):
        if self.path.split("?")[0] == "/boaform/formLogin":
            panjang = int(self.headers.get("Content-Length", 0))
            self.rfile.read(panjang)
            _SESI["token"] = "abc123"
            self._kirim(302, headers=[
                # Location ABSOLUT ke IP ONU sendiri (khas firmware)
                ("Location", f"http://{self.server.server_address[0]}:{self.server.server_address[1]}/index.asp"),
                ("Set-Cookie", "session=abc123; Path=/; HttpOnly"),
                # pola yang bikin sesi mati di browser:
                ("Set-Cookie", "auth2=x; Path=/; Secure; SameSite=None"),
            ])
        else:
            self._kirim(404, b"404")

    def log_message(self, *args):
        pass


@pytest.fixture()
def hsgq_server():
    _SESI["token"] = ""
    srv = HTTPServer(("127.0.0.1", 0), _HsgqHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address
    srv.shutdown()


@pytest.fixture()
def client_onu():
    """Login aktif (seperti produksi): middleware men-dispatch host onu-* ke
    proxy hanya bila sesi aplikasi sah."""
    from app import auth
    from app.config import settings
    from app.database import Base, SessionLocal, engine
    from app.models import ActivityLog, Customer, Router, User

    asal = (settings.auth_user, settings.auth_password, settings.tech_users)
    settings.auth_user = "admin"
    settings.auth_password = "adminpw"
    settings.tech_users = ""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for m in (User, ActivityLog, Customer, Router):
            db.query(m).delete()
        db.commit()
    finally:
        db.close()
    auth.seed_users()
    with TestClient(app, follow_redirects=False) as c:
        r = c.post("/api/login", json={"username": "admin", "password": "adminpw"})
        assert r.status_code == 200, r.text
        yield c
    settings.auth_user, settings.auth_password, settings.tech_users = asal
    auth._sessions.clear()


def _url_browser(html: str, url_halaman: str, rel: str) -> str:
    """Resolusi URL relatif persis seperti browser: hormati tag <base>."""
    m = re.search(r'<base[^>]+href="([^"]+)"', html or "", re.I)
    dasar = urljoin(url_halaman, m.group(1)) if m else url_halaman
    return urljoin(dasar, rel)


def test_onu_multipage_navigasi_tidak_looping(client_onu, hsgq_server):
    ip, port = hsgq_server
    host = f"onu-127-0-0-1-{port}.onu.test"
    asal = f"http://{host}"
    h = {"host": host}

    # 1) halaman login ONU
    r = client_onu.get("/", headers=h)
    assert r.status_code == 200
    assert "boaform/formLogin" in r.text

    # 2) login -> 302 ke /index.asp (Location absolut ikut dibelokkan);
    #    cookie sesi ONU otomatis disimpan cookie-jar TestClient
    r = client_onu.post("/boaform/formLogin",
                        data={"username": "admin", "password": "x"}, headers=h)
    assert r.status_code == 302
    assert r.headers["location"] == "/index.asp"
    set_cookies = r.headers.get_list("set-cookie")
    gabung = "; ".join(set_cookies).lower()
    assert "secure" not in gabung, set_cookies
    # SameSite=None tanpa Secure = cookie DIBUANG browser -> sesi mati -> looping
    assert "samesite=none" not in gabung, set_cookies

    # 3) frameset: TIDAK boleh ada <base href="/"> (merusak resolusi relatif
    #    untuk halaman di subdirektori)
    r = client_onu.get("/index.asp", headers=h)
    assert r.status_code == 200
    assert "<base" not in r.text.lower(), "base href merusak halaman subdirektori"
    src_menu = re.search(r'<frame src="([^"]+)"', r.text).group(1)
    url_menu = _url_browser(r.text, asal + "/index.asp", src_menu)
    assert url_menu == asal + "/html/menu/menu.asp", url_menu

    # 4) halaman menu di subdirektori, link relatif antar-subdirektori
    r = client_onu.get("/html/menu/menu.asp", headers=h)
    assert r.status_code == 200
    assert "<base" not in r.text.lower()
    href = re.search(r'href="([^"]+)"', r.text).group(1)
    url_wan = _url_browser(r.text, asal + "/html/menu/menu.asp", href)
    assert url_wan == asal + "/html/bbsp/wan.asp", url_wan

    # 5) navigasi sungguhan ke halaman berikutnya -> ISI halaman, bukan
    #    dilempar balik ke halaman awal
    r = client_onu.get("/html/bbsp/wan.asp", headers=h)
    assert r.status_code == 200
    assert "WAN-OK" in r.text

    # 6) redirect RELATIF dari dalam subdirektori tidak boleh dipaksa ke root
    r = client_onu.get("/html/menu/go.asp", headers=h)
    assert r.status_code == 302
    loc = r.headers["location"]
    url_go = _url_browser("", asal + "/html/menu/go.asp", loc)
    assert url_go == asal + "/html/bbsp/wan.asp", loc

    # 7) tanpa sesi memang dilempar ke awal (perilaku firmware, bukan proxy)
    client_onu.cookies.delete("session")
    r = client_onu.get("/html/bbsp/wan.asp", headers=h)
    assert r.status_code == 302 and r.headers["location"] == "/"


def test_script_runtime_tidak_membajak_host_lain(client_onu, hsgq_server):
    """Skrip tambalan hanya membelokkan URL absolut milik ONU/host proxy;
    URL ke host lain (mis. CDN) dibiarkan utuh."""
    ip, port = hsgq_server
    host = f"onu-127-0-0-1-{port}.onu.test"
    r = client_onu.get("/", headers={"host": host})
    assert r.status_code == 200
    assert "location.hostname" in r.text  # ada penjaga host
    assert 'var IP="127.0.0.1"' in r.text
    assert "function priv" in r.text  # URL absolut ke IP privat ikut dibelokkan


def test_cookie_path_dipaksa_root():
    from app.onu import _bersihkan_set_cookie
    sc1 = _bersihkan_set_cookie(["SESSIONID=12345678; Path=/cgi-bin/; HttpOnly"])
    assert "Path=/" in sc1[0]
    assert "Path=/cgi-bin/" not in sc1[0]

    sc2 = _bersihkan_set_cookie(["auth=token_val; HttpOnly"])
    assert "Path=/" in sc2[0]


def test_rewrite_absolute_tidak_merusak_cidr():
    from app.onu import _rewrite_absolute
    html = 'var ip = "10.20.20.17/24"; var link = "http://10.20.20.17/cgi-bin/status.asp";'
    out = _rewrite_absolute(html, "10.20.20.17", 80, "")
    assert '"10.20.20.17/24"' in out
    assert 'href="/cgi-bin/status.asp"' not in out
    assert '"/cgi-bin/status.asp"' in out
