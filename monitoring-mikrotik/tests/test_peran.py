"""Login per user (sesi cookie), peran admin vs teknisi, tambah PPPoE, kelola pengguna."""
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import User
from tests.test_protocol import FakeMikrotik


@pytest.fixture()
def akun():
    """Aktifkan login: admin/adminpw (seed) + andi/andi123 (seed teknisi)."""
    asal_user = settings.auth_user
    settings.auth_user = "admin"
    settings.auth_password = "adminpw"
    settings.tech_users = "andi:andi123"
    from app.database import Base, SessionLocal, engine
    from app.models import ActivityLog, Customer, Router, User
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for m in (User, ActivityLog, Customer, Router):
            db.query(m).delete()  # pastikan seed deterministik
        db.commit()
    finally:
        db.close()
    auth.seed_users()
    yield
    settings.auth_user = asal_user
    settings.auth_password = ""
    settings.tech_users = ""
    auth._sessions.clear()


def _login(client, user, pwd):
    r = client.post("/api/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text
    return r.json()


def test_login_salah_password(akun):
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/api/login", json={"username": "admin", "password": "x"}).status_code == 401
        # belum login: halaman dialihkan, API menolak
        r = c.get("/"); assert r.status_code == 302 and r.headers["location"] == "/login"
        assert c.get("/api/routers").status_code == 401
        # halaman login tetap bisa dibuka
        assert c.get("/login").status_code == 200


def test_peran_teknisi(akun):
    # admin
    with TestClient(app, follow_redirects=False) as c:
        _login(c, "admin", "adminpw")
        me = c.get("/api/me").json()
        assert me["role"] == "admin" and me["user"] == "admin"
        assert c.get("/pengaturan").status_code == 200
        assert c.get("/log").status_code == 200
        assert c.get("/api/users").status_code == 200
    # teknisi
    with TestClient(app, follow_redirects=False) as c:
        _login(c, "andi", "andi123")
        me = c.get("/api/me").json()
        assert me["role"] == "teknisi" and me["user"] == "andi"
        assert c.get("/pengaturan").status_code == 302
        assert c.get("/log").status_code == 302
        assert c.get("/api/users").status_code == 403
        assert c.get("/api/routers").status_code == 200


def test_admin_kelola_pengguna(akun):
    with TestClient(app, follow_redirects=False) as c:
        _login(c, "admin", "adminpw")
        users = c.get("/api/users").json()["users"]
        nama = sorted(u["username"] for u in users)
        assert nama == ["admin", "andi"]
        # tambah teknisi baru
        r = c.post("/api/users", json={"username": "budi", "password": "budi123", "role": "teknisi"})
        assert r.status_code == 200, r.text
        # budi bisa login
        with TestClient(app, follow_redirects=False) as cb:
            _login(cb, "budi", "budi123")
            assert cb.get("/api/me").json()["role"] == "teknisi"
        # duplikat ditolak
        assert c.post("/api/users", json={"username": "budi", "password": "xxxx"}).status_code == 400
        # password pendek ditolak
        assert c.post("/api/users", json={"username": "cici", "password": "12"}).status_code == 400
        # hapus akun sendiri ditolak
        uid_admin = [u["id"] for u in c.get("/api/users").json()["users"] if u["username"] == "admin"][0]
        r = c.post(f"/api/users/{uid_admin}/delete")
        assert r.status_code == 400 and "akun sendiri" in r.json()["error"]
        # hapus budi
        uid_budi = [u["id"] for u in c.get("/api/users").json()["users"] if u["username"] == "budi"][0]
        assert c.post(f"/api/users/{uid_budi}/delete").status_code == 200
    # teknisi tidak boleh kelola pengguna
    with TestClient(app, follow_redirects=False) as ct:
        _login(ct, "andi", "andi123")
        assert ct.post("/api/users", json={"username": "x", "password": "xxxx"}).status_code == 403


def test_teknisi_tambah_pppoe(akun):
    srv = FakeMikrotik().start()
    try:
        # router disiapkan admin (endpoint router memang khusus admin)
        with TestClient(app, follow_redirects=False) as ca:
            _login(ca, "admin", "adminpw")
            r = ca.post("/api/routers", json={
                "id": None, "name": "MKT-PERAN", "host": "127.0.0.1", "port": srv.port,
                "username": "ispops", "password": "rahasia", "site": "", "enabled": True,
                "poll_interval": 0, "manage_ip": "10.1.4.39", "api_ssl": "no",
            })
            rid = r.json()["id"]
        with TestClient(app, follow_redirects=False) as c:
            _login(c, "andi", "andi123")
            # teknisi tidak boleh membuat router
            assert c.post("/api/routers", json={
                "id": None, "name": "X", "host": "h", "port": 8728,
                "username": "u", "password": "p", "site": "", "enabled": True,
                "poll_interval": 0, "manage_ip": "", "api_ssl": "no",
            }).status_code == 403
            # profil dibaca langsung dari mikrotik
            prof = c.get(f"/api/ppp/profiles?router_id={rid}").json()
            assert "default" in prof["profiles"]
            # teknisi boleh menambah pppoe
            r = c.post("/api/ppp/secret/add", json={"router_id": rid, "name": "budi-net",
                                                    "password": "rahasia", "profile": "default"})
            assert r.status_code == 200 and r.json()["ok"], r.text
            # duplikat ditolak
            r = c.post("/api/ppp/secret/add", json={"router_id": rid, "name": "budi-net",
                                                    "password": "rahasia", "profile": "default"})
            assert r.status_code == 400
            # tercatat sebagai pelanggan baru (source manual)
            cust = c.get("/api/customers?source=manual").json()["customers"]
            assert any(x["name"] == "budi-net" and x["source"] == "manual" for x in cust)
        # sync oleh admin tidak mengubah pelanggan manual jadi auto
        with TestClient(app, follow_redirects=False) as ca:
            _login(ca, "admin", "adminpw")
            ca.post(f"/api/customers/sync?router_id={rid}")
            cust = ca.get("/api/customers?source=manual").json()["customers"]
            assert any(x["name"] == "budi-net" for x in cust)
            auto = ca.get("/api/customers?source=auto").json()["customers"]
            assert not any(x["name"] == "budi-net" for x in auto)
        # teknisi boleh isolir dan buka isolir (pakai sesi yang online di mikrotik tiruan)
        with TestClient(app, follow_redirects=False) as ct:
            _login(ct, "andi", "andi123")
            r = ct.post("/api/isolir", json={"name": "budi-santoso", "router_id": rid,
                                             "reason": "uji"})
            assert r.status_code == 200 and r.json()["ok"], r.text
            r = ct.post("/api/buka", json={"name": "budi-santoso", "router_id": rid})
            assert r.status_code == 200 and r.json()["ok"], r.text
    finally:
        srv.stop()


def test_pemantau_hanya_melihat(akun):
    with TestClient(app, follow_redirects=False) as ca:
        _login(ca, "admin", "adminpw")
        r = ca.post("/api/users", json={"username": "siti", "password": "siti123",
                                        "role": "pemantau"})
        assert r.status_code == 200, r.text
    with TestClient(app, follow_redirects=False) as c:
        _login(c, "siti", "siti123")
        assert c.get("/api/me").json()["role"] == "pemantau"
        # monitoring boleh
        assert c.get("/api/status").status_code == 200
        assert c.get("/").status_code == 200
        # halaman & aksi lain ditolak
        assert c.get("/pengaturan").status_code == 302
        assert c.post("/api/ppp/secret/add",
                      json={"router_id": 1, "name": "x", "password": "y"}).status_code == 403
        assert c.post("/api/isolir",
                      json={"name": "x", "router_id": 1}).status_code == 403
        assert c.get("/api/users").status_code == 403


def test_auto_isolir_khusus_admin(akun):
    import asyncio

    from app import services
    srv = FakeMikrotik().start()
    try:
        with TestClient(app, follow_redirects=False) as ca:
            _login(ca, "admin", "adminpw")
            r = ca.post("/api/routers", json={
                "id": None, "name": "MKT-AUTO", "host": "127.0.0.1", "port": srv.port,
                "username": "ispops", "password": "rahasia", "site": "", "enabled": True,
                "poll_interval": 0, "manage_ip": "", "api_ssl": "no",
            })
            rid = r.json()["id"]
            ca.post(f"/api/customers/sync?router_id={rid}")
            custs = ca.get(f"/api/customers?router_id={rid}").json()["customers"]
            budi = [c for c in custs if c["name"] == "budi-santoso"][0]
            assert budi["auto_isolir"] is False  # default: isolir manual
            # teknisi tidak boleh menandai
            with TestClient(app, follow_redirects=False) as ct:
                _login(ct, "andi", "andi123")
                assert ct.post(f"/api/customers/{budi['id']}/auto-isolir",
                               json={"enabled": True}).status_code == 403
            # mulai dari kondisi terbuka (mikrotik tiruan menandai budi terisolir)
            ca.post("/api/buka", json={"name": "budi-santoso", "router_id": rid})
            asyncio.run(services.refresh_router(rid))
            st = ca.get(f"/api/status?router_id={rid}").json()["routers"][0]["sessions"]
            s = [x for x in st if x["name"] == "budi-santoso"][0]
            assert s["status"] == "up" and s["auto_isolir"] is False
            # admin menandai auto-isolir
            r = ca.post(f"/api/customers/{budi['id']}/auto-isolir", json={"enabled": True})
            assert r.status_code == 200 and r.json()["auto_isolir"] is True
            # putaran pantau berikutnya langsung mengisolir
            asyncio.run(services.refresh_router(rid))
            st = ca.get(f"/api/status?router_id={rid}").json()["routers"][0]["sessions"]
            s = [x for x in st if x["name"] == "budi-santoso"][0]
            assert s["status"] == "isolated" and s["auto_isolir"] is True
            # nonaktifkan: status isolir tetap (buka tetap manual)
            r = ca.post(f"/api/customers/{budi['id']}/auto-isolir", json={"enabled": False})
            assert r.status_code == 200 and r.json()["auto_isolir"] is False
    finally:
        srv.stop()


def test_auto_isolir_dengan_tanggal(akun):
    import asyncio
    from datetime import date, timedelta

    from app import services
    srv = FakeMikrotik().start()
    try:
        with TestClient(app, follow_redirects=False) as ca:
            _login(ca, "admin", "adminpw")
            rid = ca.post("/api/routers", json={
                "id": None, "name": "MKT-TGL", "host": "127.0.0.1", "port": srv.port,
                "username": "ispops", "password": "rahasia", "site": "", "enabled": True,
                "poll_interval": 0, "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            ca.post(f"/api/customers/sync?router_id={rid}")
            budi = [c for c in ca.get(f"/api/customers?router_id={rid}").json()["customers"]
                    if c["name"] == "budi-santoso"][0]
            ca.post("/api/buka", json={"name": "budi-santoso", "router_id": rid})

            # AUTO dengan tanggal MASA DEPAN -> belum jatuh tempo, tetap online
            besok = (date.today() + timedelta(days=1)).isoformat()
            r = ca.post(f"/api/customers/{budi['id']}/auto-isolir",
                        json={"enabled": True, "date": besok})
            assert r.status_code == 200 and r.json()["auto_isolir_date"] == besok
            asyncio.run(services.refresh_router(rid))
            s = [x for x in ca.get(f"/api/status?router_id={rid}").json()["routers"][0]["sessions"]
                 if x["name"] == "budi-santoso"][0]
            assert s["status"] == "up", "tanggal besok (+2 hari tenggang) belum boleh isolir"
            assert s["auto_isolir_date"] == besok

            # tanggal 3 hari lalu -> tenggang 2 hari sudah lewat -> isolir
            lama = (date.today() - timedelta(days=3)).isoformat()
            r = ca.post(f"/api/customers/{budi['id']}/auto-isolir",
                        json={"enabled": True, "date": lama})
            assert r.status_code == 200
            asyncio.run(services.refresh_router(rid))
            s = [x for x in ca.get(f"/api/status?router_id={rid}").json()["routers"][0]["sessions"]
                 if x["name"] == "budi-santoso"][0]
            assert s["status"] == "isolated"

            # format tanggal salah ditolak
            r = ca.post(f"/api/customers/{budi['id']}/auto-isolir",
                        json={"enabled": True, "date": "32-13-2026"})
            assert r.status_code == 400
    finally:
        srv.stop()


def test_proxy_onu_meneruskan_post_dan_css(akun):
    """Lewat domain HTTPS, login ONU (POST) dan CSS absolut harus lewat proxy."""
    import http.server
    import threading

    class OnuTiruan(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith(".css"):
                body = f"body{{background:url(http://{self.headers['Host']}/bg.png)}}".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/css")
                self.end_headers()
                self.wfile.write(body)
                return
            body = (b"<html><head></head><body><form method=post action=login.cgi>"
                    b"<input name=u></form></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            panjang = int(self.headers.get("Content-Length", 0))
            isi = self.rfile.read(panjang).decode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><body>TERIMA:{isi}</body></html>".encode())

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OnuTiruan)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        with TestClient(app, follow_redirects=False) as ca:
            _login(ca, "admin", "adminpw")
            rid = ca.post("/api/routers", json={
                "id": None, "name": "MKT-PROXY", "host": "127.0.0.1", "port": 8728,
                "username": "u", "password": "p", "site": "", "enabled": True,
                "poll_interval": 0, "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            # GET halaman + base href
            r = ca.get(f"/proxy/{rid}/http/127.0.0.1:{port}/")
            assert r.status_code == 200 and f'<base href="/proxy/{rid}/http/127.0.0.1:{port}/">' in r.text
            # POST form login ONU diteruskan
            r = ca.post(f"/proxy/{rid}/http/127.0.0.1:{port}/login.cgi",
                        content=b"u=teknisi&p=123",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
            assert r.status_code == 200 and "TERIMA:u=teknisi&p=123" in r.text
            # CSS absolut ditulis ulang (anti mixed-content)
            r = ca.get(f"/proxy/{rid}/http/127.0.0.1:{port}/style.css")
            assert f"http://127.0.0.1:{port}/" not in r.text
    finally:
        srv.shutdown()


def test_proxy_onu_cookie_js_dan_runtime_fix(akun):
    """ONU ZTE/China Mobile: cookie sesi harus bolak-balik, JS tanpa MIME
    dipaksa benar, dan skrip tambal URL runtime disuntikkan."""
    import http.server
    import threading

    class OnuZte(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith(".js"):
                # ONU murah sering mengirim JS tanpa content-type
                body = b"var u='http://127.0.0.1:PORT/login';"
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body.replace(b"PORT", str(self.server.server_address[1]).encode()))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "ztesid=abc123; Path=/; Domain=127.0.0.1; Secure")
            self.end_headers()
            self.wfile.write(b"<html><head></head><body>ok</body></html>")

        def do_POST(self):
            panjang = int(self.headers.get("Content-Length", 0))
            self.rfile.read(panjang)
            kuki = self.headers.get("Cookie", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><body>KOOKIE:{kuki}</body></html>".encode())

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OnuZte)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        with TestClient(app, follow_redirects=False) as ca:
            _login(ca, "admin", "adminpw")
            rid = ca.post("/api/routers", json={
                "id": None, "name": "MKT-ZTE", "host": "127.0.0.1", "port": 8728,
                "username": "u", "password": "p", "site": "", "enabled": True,
                "poll_interval": 0, "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            pref = f"/proxy/{rid}/http/127.0.0.1:{port}"
            r = ca.get(f"{pref}/")
            # Set-Cookie ONU diteruskan ke browser tanpa Domain/Secure
            sc = r.headers.get_list("set-cookie")
            assert any("ztesid=abc123" in x and "Domain" not in x and "Secure" not in x for x in sc)
            # skrip tambal runtime tersuntik
            assert "XMLHttpRequest.prototype.open" in r.text
            # cookie browser diteruskan balik ke ONU saat POST login
            r = ca.post(f"{pref}/login.cgi", content=b"a=1",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
            assert "KOOKIE:ztesid=abc123" in r.text
            # JS tanpa MIME dipaksa application/javascript + URL absolut direwrite
            r = ca.get(f"{pref}/app.js")
            assert r.headers["content-type"].startswith("application/javascript")
            assert f"http://127.0.0.1:{port}/login" not in r.text
    finally:
        srv.shutdown()


def test_proxy_onu_redirect_setelah_login(akun):
    """Login ONU sukses biasanya 302 ke http://IP-ONU/... — harus tetap di proxy,
    bukan halaman kosong/mentah."""
    import http.server
    import threading

    class OnuRedir(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>BERANDA ONU</body></html>")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><head></head><body>login</body></html>")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location",
                             f"http://{self.headers['Host']}/index.html")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OnuRedir)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        with TestClient(app, follow_redirects=False) as ca:
            _login(ca, "admin", "adminpw")
            rid = ca.post("/api/routers", json={
                "id": None, "name": "MKT-REDIR", "host": "127.0.0.1", "port": 8728,
                "username": "u", "password": "p", "site": "", "enabled": True,
                "poll_interval": 0, "manage_ip": "", "api_ssl": "no",
            }).json()["id"]
            pref = f"/proxy/{rid}/http/127.0.0.1:{port}"
            r = ca.post(f"{pref}/login.cgi", content=b"u=a&p=b",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
            # 302 ditulis ulang ke dalam proxy, bukan ke http://IP-ONU
            assert r.status_code == 302
            dari_urlparse = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(r.headers["location"])
            assert dari_urlparse.path == f"{pref}/index.html"
            r2 = ca.get(r.headers["location"])
            assert r2.status_code == 200 and "BERANDA ONU" in r2.text
    finally:
        srv.shutdown()


def test_remote_onu_subdomain_wildcard(akun):
    """Pola ala mdsgetas: 10-20-20-3.onu.DOMAIN -> proxy ke 10.20.20.3 tanpa
    rewrite prefix; tanpa sesi dialihkan ke login host utama."""
    import http.server
    import threading
    from urllib.parse import urlsplit

    class OnuW(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"<html><head></head><body>HSGQ-LOGIN</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "onuid=1; Path=/")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", f"http://{self.headers['Host']}/admin/login.asp")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OnuW)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    host = f"onu-127-0-0-1-{port}.contoh.id"
    try:
        # tanpa sesi: dilempar ke login host utama
        with TestClient(app, follow_redirects=False) as cx:
            r = cx.get("/", headers={"Host": host})
            assert r.status_code == 302
            assert r.headers["location"] == "https://monitoring.contoh.id/login"
        with TestClient(app, follow_redirects=False) as ca:
            _login(ca, "admin", "adminpw")
            r = ca.get("/", headers={"Host": host})
            assert r.status_code == 200 and "HSGQ-LOGIN" in r.text
            assert any("onuid=1" in x for x in r.headers.get_list("set-cookie"))
            r = ca.post("/login.cgi", content=b"a=b",
                        headers={"Host": host,
                                 "Content-Type": "application/x-www-form-urlencoded"})
            assert r.status_code == 302
            assert urlsplit(r.headers["location"]).path == "/admin/login.asp"
            # cabang Response (CSS/biner) lewat host wildcard: dulu NameError
            r = ca.get("/style.css", headers={"Host": host})
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/css")
    finally:
        srv.shutdown()


def test_pemantau_dibatasi_router_tertentu(akun):
    srv = FakeMikrotik().start()
    try:
        with TestClient(app, follow_redirects=False) as ca:
            _login(ca, "admin", "adminpw")
            dasar = {"host": "127.0.0.1", "port": srv.port, "username": "ispops",
                     "password": "rahasia", "site": "", "enabled": True,
                     "poll_interval": 0, "manage_ip": "", "api_ssl": "no"}
            rid_a = ca.post("/api/routers", json={**dasar, "name": "SITE-A"}).json()["id"]
            rid_b = ca.post("/api/routers", json={**dasar, "name": "SITE-B"}).json()["id"]
            ca.post("/api/users", json={"username": "siti", "password": "siti123",
                                        "role": "pemantau"})
            uid = [u["id"] for u in ca.get("/api/users").json()["users"]
                   if u["username"] == "siti"][0]
            r = ca.post(f"/api/users/{uid}/routers", json={"router_ids": [rid_a]})
            assert r.status_code == 200
            assert ca.get(f"/api/users/{uid}/routers").json()["router_ids"] == [rid_a]
            # teknisi tidak boleh mengubah batasan
            with TestClient(app, follow_redirects=False) as ct:
                _login(ct, "andi", "andi123")
                assert ct.post(f"/api/users/{uid}/routers",
                               json={"router_ids": []}).status_code == 403
        with TestClient(app, follow_redirects=False) as c:
            _login(c, "siti", "siti123")
            nama = [r["name"] for r in c.get("/api/routers").json()["routers"]]
            assert nama == ["SITE-A"]
            st = c.get("/api/status").json()["routers"]
            assert [r["router_id"] for r in st] == [rid_a]
    finally:
        srv.stop()
