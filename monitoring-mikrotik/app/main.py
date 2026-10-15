"""ISP-OPS - Monitoring ONU & Kontrol Mikrotik untuk teknisi.

Jalankan:  python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Demo    :  DEMO_MODE=true python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, billing, services
from .config import settings
from .database import SessionLocal, init_db
from .mikrotik.fake import FakeRouter
from .models import Customer, Router, User
from .routes import router as api_router
from .services import pool


# ============================================================================ nyala/mati

async def _seed_demo() -> None:
    """Mode demo: bikin 1 router tiruan + pelanggan contoh supaya bisa langsung dicoba."""
    db = SessionLocal()
    try:
        existing = db.query(Router).filter(Router.name == "DEMO-Mikrotik").first()
        if existing is None:
            db.add(
                Router(
                    name="DEMO-Mikrotik",
                    host="demo",
                    port=8728,
                    username="demo",
                    password="demo",
                    site="Kantor (demo)",
                    enabled=True,
                    poll_interval=5,
                    manage_ip="10.10.10.1",
                )
            )
            db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.demo_mode:
        # jangan timpa instance demo yang sudah dipasang (mis. oleh pengujian)
        if pool._demo is None:  # noqa: SLF001
            pool.set_demo(FakeRouter())
        await _seed_demo()
    task = asyncio.create_task(services.poller_loop())
    from . import onu_nat
    task_nat = asyncio.create_task(onu_nat.sweep_loop())
    try:  # bersihkan sisa NAT sementara dari sesi sebelum restart
        await asyncio.get_event_loop().run_in_executor(
            None, onu_nat.startup_cleanup)
    except Exception:  # noqa: BLE001
        pass
    try:
        await services.refresh_all()
    except Exception:  # noqa: BLE001
        pass
    try:
        yield
    finally:
        task.cancel()
        task_nat.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await task_nat
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await pool.close_all()


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ============================================================================ login sederhana

import logging
import re as _re

logger = logging.getLogger("ispops.onu")

_ONU_HOST = _re.compile(r"^onu-((?:\d{1,3}-){3}\d{1,3})(?:-(\d{2,5}))?\.(.+)$")


def _onu_dari_host(host: str):
    """Subdomain wildcard satu label: onu-10-20-20-3.DOMAIN (akhiran -8080 bila
    port bukan 80) — cocok dengan wildcard Cloudflare "*" satu tingkat.
    Mengembalikan (ip, port, domain)."""
    m = _ONU_HOST.match(host.split(":")[0])
    if not m:
        return None
    ipd, ports, domain = m.groups()
    okt = [int(x) for x in ipd.split("-")]
    if any(o > 255 for o in okt):
        return None
    return ".".join(str(o) for o in okt), int(ports or 80), domain


# Jalur yang selalu publik karena menjadi tujuan pengalihan klien suspend:
# halaman isolir + URL uji konektivitas OS (Android/Apple/Windows memakai
# URL ini; menjawabnya dengan halaman isolir membuat HP menampilkan portal).
_JALUR_ISOLIR = ("/isolir", "/terisolir", "/generate_204", "/gen_204",
                 "/hotspot-detect.html", "/library/test/success.html",
                 "/connecttest.txt", "/ncsi.txt", "/success.txt")

# Host uji portal milik OS/browser. Request dengan Host ini hanya bisa
# tiba di server lewat dst-nat MikroTik (klien suspend membuka situs itu,
# router membelokkannya ke sini) -- jadi apa pun jalurnya, termasuk
# "/login" milik Brave (detectportal.brave-http-only.com/login) yang
# bertabrakan dengan jalur login operator, jawabannya harus halaman
# isolir, bukan halaman aplikasi.
_HOST_PORTAL = {
    "detectportal.brave-http-only.com",
    "connectivitycheck.gstatic.com", "connectivitycheck.android.com",
    "clients3.google.com", "captive.apple.com",
    "www.msftconnecttest.com", "www.msftncsi.com",
    "detectportal.firefox.com", "connectivity-check.ubuntu.com",
}


def _ip_pengunjung(request: Request) -> str:
    """IP asal pengunjung. Header proxy hanya dipercaya bila pengirimnya
    proxy lokal/privat (nginx di server yang sama, docker, dst.) supaya
    pengunjung internet tidak bisa memalsukan IP pelanggan lain."""
    peer = request.client.host if request.client else ""
    from .util import is_private_ip

    if peer in ("127.0.0.1", "::1", "testclient") or (peer and is_private_ip(peer)):
        for header in ("x-real-ip", "x-forwarded-for"):
            nilai = (request.headers.get(header) or "").split(",")[0].strip()
            if nilai:
                return nilai
    return peer or ""


def _pelanggan_terisolir(ip: str) -> dict | None:
    """Data pelanggan terisolir pemilik IP ini (untuk halaman isolir).

    Klien suspend menerima IP baru dari pool profil suspend; poller
    memperbarui customers.ip dari sesi PPPoE aktif, jadi IP barunya
    tetap bisa dicocokkan ke pelanggan yang sedang diisolir.
    """
    if not ip:
        return None
    db = SessionLocal()
    try:
        c = (db.query(Customer)
             .filter(Customer.ip == ip, Customer.isolated.is_(True))
             .order_by(Customer.id.desc()).first())
        if c is None:
            return None
        return {
            "nama": c.full_name or c.name,
            "username": c.name,
            "alasan": c.isolated_reason or "",
            "sejak": (c.isolated_at.strftime("%d-%m-%Y %H:%M")
                      if c.isolated_at else ""),
            "ip": ip,
        }
    except Exception:  # noqa: BLE001 - halaman tetap tampil tanpa data
        return None
    finally:
        db.close()


def _tautan_wa(pelanggan: dict | None) -> str:
    """Tautan wa.me dari ISOLIR_WA (boleh ditulis 08xx atau 628xx)."""
    import re as _re_wa
    from urllib.parse import quote

    digit = _re_wa.sub(r"\D", "", settings.isolir_wa or "")
    if not digit:
        return ""
    if digit.startswith("0"):
        digit = "62" + digit[1:]
    sapaan = "Halo admin"
    if settings.isolir_kontak:
        sapaan = f"Halo {settings.isolir_kontak}"
    siapa = f" ({pelanggan['nama']})" if pelanggan else ""
    teks = quote(f"{sapaan}, saya{siapa} mohon info tagihan / buka isolir internet saya.")
    return f"https://wa.me/{digit}?text={teks}"


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.user = settings.auth_user
    request.state.role = "admin"
    path = request.url.path
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    # Sesi operator yang sah MENANG atas semua pengalihan isolir di bawah:
    # admin yang membuka panel lewat host/alamat mana pun (domain, IP lain,
    # nama host server) tidak boleh ikut terbelok ke halaman isolir hanya
    # karena host-nya tidak tercantum di APP_HOSTS. Klien suspend tidak
    # membawa cookie sesi operator, jadi mereka tetap teralihkan.
    sesi_operator = (auth.get_session(request.cookies.get("ispops_sid", ""))
                     if settings.auth_password else None)
    # Pengalihan isolir berbasis Host hanya berlaku untuk lalu lintas dari
    # jaringan sendiri (IP privat: klien PPPoE 10.200.0.x, router yang
    # me-masquerade, LAN operator). Pengunjung dari internet publik --
    # misalnya HP memakai DATA SELULER -- tidak boleh ikut terbelok ke
    # halaman isolir hanya karena Host-nya bukan host resmi aplikasi.
    from .util import is_private_ip as _is_private

    ip_pengunjung = _ip_pengunjung(request)
    dari_jaringan_sendiri = bool(
        ip_pengunjung and (ip_pengunjung == "testclient"
                           or _is_private(ip_pengunjung)))
    # Host BUKAN host resmi aplikasi (APP_HOSTS terisi): ini lalu lintas
    # asing klien suspend yang ter-dst-nat -- situs apa pun yang ia buka,
    # termasuk halaman depannya ("/") yang bertabrakan dengan dashboard.
    # Host subdomain ONU dikecualikan (itu memang pola host aplikasi),
    # dan /login selalu dikecualikan di sini (jalan darurat admin masuk
    # dari alamat mana pun; khusus host portal seperti Brave tetap
    # dibelokkan oleh blok host portal di bawahnya).
    host_resmi = settings.app_host_list
    if (host_resmi and request.method in ("GET", "HEAD")
            and sesi_operator is None and dari_jaringan_sendiri
            and host and host not in host_resmi
            and not _onu_dari_host(request.headers.get("host", ""))
            and not path.startswith(("/api/", "/static/"))
            and path not in _JALUR_ISOLIR and path not in ("/health", "/login")):
        return RedirectResponse("/isolir", status_code=302)
    # Host portal captive (Brave/Chrome/Apple/dll.): selalu halaman isolir,
    # apa pun jalurnya -- termasuk "/login" Brave yang bertabrakan nama.
    if (request.method in ("GET", "HEAD") and host in _HOST_PORTAL
            and sesi_operator is None
            and path not in _JALUR_ISOLIR):
        return RedirectResponse("/isolir", status_code=302)
    # Klien yang sedang diisolir dan sampai ke server ini (pengalihan
    # MikroTik meneruskan HTTP-nya ke sini) selalu diarahkan ke halaman
    # isolir, apa pun URL asal yang ia buka. API & jalur publik dikecualikan
    # supaya teknisi dan halaman isolirnya sendiri tidak ikut teralihkan.
    if (request.method in ("GET", "HEAD")
            and sesi_operator is None
            and not path.startswith("/api/")
            and not any(path == q or path.startswith("/static/") for q in _JALUR_ISOLIR)
            and path != "/health"):
        if _pelanggan_terisolir(ip_pengunjung):
            return RedirectResponse("/isolir", status_code=302)
    if not settings.auth_password:  # login dimatikan (mode demo/development)
        return await call_next(request)
    public = ("/static/", "/health", "/login", "/api/login") + _JALUR_ISOLIR
    if any(path.startswith(q) for q in public):
        return await call_next(request)
    sesi = auth.get_session(request.cookies.get("ispops_sid", ""))
    if sesi is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "belum login"}, status_code=401)
        onu = _onu_dari_host(request.headers.get("host", ""))
        if onu:
            return RedirectResponse(f"https://{onu[2]}/login", status_code=302)
        # Jalur asing (bukan halaman aplikasi) yang sampai ke server ini
        # hampir pasti lalu lintas HTTP klien suspend yang di-dst-nat oleh
        # MikroTik: IP sumbernya sudah ter-masquerade oleh router sehingga
        # tidak bisa dikenali per-pelanggan. Tampilkan halaman isolir
        # (umum), BUKAN halaman login operator -- klien tidak boleh melihat
        # apalagi mencoba login ke panel. Halaman aplikasi yang sah tetap
        # diarahkan ke login seperti biasa.
        if (request.method in ("GET", "HEAD")
                and path not in ("/", "/pengaturan", "/log")):
            return RedirectResponse("/isolir", status_code=302)
        return RedirectResponse("/login", status_code=302)
    request.state.user = sesi["user"]
    request.state.role = sesi["role"]
    if _onu_dari_host(request.headers.get("host", "")):
        return await _layani_onu(request, request.url.path)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    if not settings.auth_password:
        return RedirectResponse("/")
    if auth.get_session(request.cookies.get("ispops_sid", "")):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "login.html", {"settings": settings})


def _cookie_domain(host: str) -> str | None:
    """Tentukan domain wildcard untuk cookie ispops_sid.
    Supaya cookie sesi berlaku baik di domain utama maupun subdomain wildcard
    onu-X-X-X-X.DOMAIN, tapi TIDAK boleh diset ke public suffix (misal .my.id atau .co.id).
    """
    raw_host = host.split(":")[0].strip().lower()
    if not raw_host or _re.match(r"^[\d.]+$", raw_host):
        return None
    parts = raw_host.split(".")
    if len(parts) < 2:
        return None
    m = _re.search(r"\.onu\.(.+)$", raw_host)
    if m:
        return "." + m.group(1)
    two_level_tlds = {"my", "co", "web", "net", "ac", "sch", "go", "or", "biz", "mil"}
    if len(parts) >= 3 and parts[-2] in two_level_tlds and len(parts[-1]) == 2:
        return "." + ".".join(parts[-3:])
    if len(parts) >= 2:
        return "." + ".".join(parts[-2:])
    return None


@app.post("/api/login")
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    username = str(body.get("username") or "").strip()
    pwd = str(body.get("password") or "")
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == username).first()
        ok = u is not None and auth.verify_password(pwd, u.salt, u.password_hash)
        role = u.role if u else "teknisi"
    finally:
        db.close()
    if not ok:
        return JSONResponse({"ok": False, "error": "nama atau password salah"},
                            status_code=401)
    token = auth.create_session(username, role)
    resp = JSONResponse({"ok": True, "user": username, "role": role})
    domain = _cookie_domain(request.headers.get("host", ""))
    resp.set_cookie("ispops_sid", token, httponly=True, samesite="lax",
                    max_age=auth.SESSION_TTL, domain=domain)
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    auth.destroy_session(request.cookies.get("ispops_sid", ""))
    domain = _cookie_domain(request.headers.get("host", ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("ispops_sid", domain=domain)
    return resp


app.include_router(api_router)


# ============================================================================ halaman

@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "settings": settings,
            "user": getattr(request.state, "user", "teknisi"),
            "role": getattr(request.state, "role", "admin"),
            "page": "monitor",
        },
    )


@app.get("/pengaturan", response_class=HTMLResponse)
async def page_settings(request: Request):
    if getattr(request.state, "role", "admin") != "admin":
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "pengaturan.html",
        {
            "settings": settings,
            "user": getattr(request.state, "user", "teknisi"),
            "role": getattr(request.state, "role", "admin"),
            "page": "pengaturan",
        },
    )


@app.get("/log", response_class=HTMLResponse)
async def page_logs(request: Request):
    if getattr(request.state, "role", "admin") != "admin":
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "log.html",
        {
            "settings": settings,
            "user": getattr(request.state, "user", "teknisi"),
            "role": getattr(request.state, "role", "admin"),
            "page": "log",
        },
    )


@app.get("/isolir", response_class=HTMLResponse)
@app.get("/terisolir", response_class=HTMLResponse)
@app.get("/generate_204", response_class=HTMLResponse)
@app.get("/gen_204", response_class=HTMLResponse)
@app.get("/hotspot-detect.html", response_class=HTMLResponse)
@app.get("/library/test/success.html", response_class=HTMLResponse)
@app.get("/connecttest.txt", response_class=HTMLResponse)
@app.get("/ncsi.txt", response_class=HTMLResponse)
@app.get("/success.txt", response_class=HTMLResponse)
async def page_isolir(request: Request):
    """Halaman isolir publik (tanpa login) untuk klien profil suspend.

    MikroTik mengalihkan HTTP klien SUSPEND-LIST ke server ini; URL uji
    konektivitas OS ikut diarahkan ke sini supaya HP/laptop klien langsung
    memunculkan halaman ini sebagai portal. Bila IP pengunjung dikenal
    sebagai pelanggan terisolir, namanya ditampilkan di halaman.
    """
    pelanggan = _pelanggan_terisolir(_ip_pengunjung(request))
    resp = templates.TemplateResponse(
        request, "isolir.html",
        {"settings": settings, "pelanggan": pelanggan,
         "tautan_wa": _tautan_wa(pelanggan)},
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "demo": settings.demo_mode,
        "routers": len(pool.snapshots()),
        "billing": billing.status(),
    }


# ============================================================================ remote ONU subdomain wildcard
async def _layani_onu(request: Request, path: str = ""):
    """Remote ONU ala 10-20-20-3.onu.DOMAIN: tiap ONU terasa punya host sendiri,
    jadi URL relatif/absolut-root, redirect, dan cookie bekerja alami tanpa
    rewrite prefix. Hanya IP privat yang boleh."""
    dari = _onu_dari_host(request.headers.get("host", ""))
    if dari is None:
        raise HTTPException(404, "tidak ditemukan")
    ip, port, _domain = dari
    from . import onu as onu_mod

    # Allow-list: IP publik hanya boleh diproxy bila ia host router terdaftar
    # (situs luar via VPN L2TP dengan port-mapping publik). IP privat tetap
    # selalu boleh; publik lain tetap ditolak (anti open-proxy).
    allowed = set()
    db = SessionLocal()
    try:
        for (h,) in db.query(Router.host).all():
            if h and _re.match(r"^\d{1,3}(\.\d{1,3}){3}$", h):
                allowed.add(h)
    finally:
        db.close()

    # ONU situs luar (router-nya ber-host publik/VPN): sambung lewat NAT
    # sementara yang dibuat otomatis di router itu (lihat app/onu_nat.py).
    # ONU kantor (router host privat) tetap koneksi langsung seperti biasa.
    from . import onu_nat
    from .util import is_private_ip as _priv

    target_ip, target_port, nat_tag = ip, port, None
    if _priv(ip):
        db = SessionLocal()
        try:
            rc = (db.query(Customer.router_id)
                  .filter(Customer.onu_ip == ip).order_by(Customer.id).first())
            rtr = db.query(Router).get(rc[0]) if rc else None
        finally:
            db.close()
        if rtr is not None and not _priv(rtr.host) and onu_nat.pool_ports():
            loop = asyncio.get_event_loop()
            np = await loop.run_in_executor(None, onu_nat.ensure_nat, rtr, ip, port)
            if np:
                target_ip, target_port = rtr.host, np
                nat_tag = (rtr.id, ip, port)

    prefix = ""  # host sudah "milik" ONU itu sendiri
    raw_cookie = request.headers.get("cookie", "")
    kuki = _re.sub(r"(?:^|;\s*)ispops_sid=[^;]*", "", raw_cookie).strip("; ")
    body = await request.body() if request.method == "POST" else b""
    is_https = (request.url.scheme == "https" or
                request.headers.get("x-forwarded-proto") == "https")
    res = await onu_mod.proxy_fetch(
        prefix, target_ip, target_port, path, str(request.query_params),
        method=request.method, body=body,
        content_type=request.headers.get("content-type", ""), cookies=kuki,
        self_host=request.headers.get("host", ""), allowed_hosts=allowed,
        rewrite_ip=ip if nat_tag else "", rewrite_port=port if nat_tag else 0,
        incoming_headers=dict(request.headers), is_https=is_https,
    )
    logger.info("ONU proxy [%s:%s] %s %s -> HTTP %s",
                target_ip, target_port, request.method, path or "/", res.get("status", 0))
    if nat_tag and res.get("ok"):
        onu_nat.touch(*nat_tag)  # perpanjang umur NAT selama masih dipakai

    def _balik(resp):
        for sc in res.get("set_cookies") or []:
            resp.headers.append("Set-Cookie", sc)
        if res.get("www_authenticate"):
            resp.headers["WWW-Authenticate"] = res["www_authenticate"]
        return resp

    if not res.get("ok"):
        return _balik(HTMLResponse(
            f"<pre style='font:14px/1.6 system-ui;padding:24px'>{res.get('text', 'gagal')}</pre>",
            status_code=res.get("status", 502),
        ))
    if res.get("redirect"):
        return _balik(RedirectResponse(res["redirect"],
                                       status_code=res.get("status", 302)))
    if res.get("binary"):
        return _balik(Response(
            content=res["content"],
            media_type=res.get("content_type", "application/octet-stream"),
        ))
    ct = res.get("content_type", "") or "text/html; charset=utf-8"
    if "html" not in ct:
        return _balik(Response(
            content=res["text"].encode("utf-8", "replace"),
            media_type=ct, status_code=res.get("status", 200),
        ))
    return _balik(HTMLResponse(res["text"], status_code=res.get("status", 200)))


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def onu_wildcard(request: Request, path: str = ""):
    return await _layani_onu(request, path)
