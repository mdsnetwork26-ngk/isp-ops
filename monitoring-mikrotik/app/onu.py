"""Fitur ONU: menemukan IP ONU/router pelanggan + membuka halaman webnya.

Kenyataan di lapangan: IP ONU (mis. 192.168.100.1) tidak selalu sama polanya
tiap merk. Karena itu aplikasi menyediakan 3 cara:
  1. IP tersimpan di database (paling andal, sekali isi dipakai selamanya)
  2. Kandidat otomatis: IP tersimpan, IP ONU umum, varian oktet terakhir IP PPPoE
  3. Scan satu subnet -> teknisi pilih dari hasil (ditandai yang paling meyakinkan)
"""
from __future__ import annotations

import asyncio
import re
import socket

import httpx

from .config import settings
from .util import (
    extract_macs,
    is_private_ip,
    last_octet_variants,
    norm_mac,
    onu_score,
    same_subnet_ips,
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


# --------------------------------------------------------------------------- kandidat

def candidate_ips(
    onu_ip: str = "",
    router_ip: str = "",
    pppoe_ip: str = "",
    include_common: bool = True,
) -> list[str]:
    """Urutan IP yang dicoba: tersimpan dulu, lalu tebakan umum."""
    out: list[str] = []
    for ip in (onu_ip, router_ip):
        ip = (ip or "").strip()
        if ip and ip not in out:
            out.append(ip)
    if include_common:
        for ip in settings.onu_candidate_list + last_octet_variants(pppoe_ip):
            if ip not in out:
                out.append(ip)
    return out


def target_url(ip: str, port: int) -> str:
    port = int(port or settings.onu_default_port)
    if port in (80, 0):
        return f"http://{ip}/"
    return f"http://{ip}:{port}/"


# --------------------------------------------------------------------------- probe

async def _probe(
    ip: str,
    port: int,
    timeout: float,
    target_mac: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Coba buka http://ip:port/ lalu nilai apakah itu ONU pelanggan."""
    result: dict = {
        "ip": ip,
        "port": port,
        "reachable": False,
        "http": 0,
        "title": "",
        "server": "",
        "macs": [],
        "score": 0,
        "reason": "",
        "ms": 0,
    }
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        # cek port dulu (lebih cepat daripada tunggu timeout HTTP penuh)
        await asyncio.wait_for(
            loop.run_in_executor(None, _tcp_open, ip, port, min(timeout, 1.5)),
            timeout=timeout + 0.5,
        )
    except Exception:  # noqa: BLE001
        result["ms"] = int((loop.time() - started) * 1000)
        return result
    result["reachable"] = True

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        resp = await client.get(target_url(ip, port))
        body = resp.text
        result["http"] = resp.status_code
        result["server"] = resp.headers.get("server", "")[:60]
        m = _TITLE_RE.search(body)
        if m:
            result["title"] = " ".join(m.group(1).split())[:60]
        result["macs"] = extract_macs(body, 6)
        score, reason = onu_score(body, dict(resp.headers), target_mac)
        result["score"] = score
        result["reason"] = reason
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"port terbuka, HTTP error: {type(exc).__name__}"
        result["score"] = 20
    finally:
        if own_client:
            await client.aclose()
    result["ms"] = int((loop.time() - started) * 1000)
    return result


def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


async def scan(
    ips: list[str],
    port: int,
    target_mac: str = "",
    concurrency: int | None = None,
    timeout: float | None = None,
) -> list[dict]:
    """Probe banyak IP paralel. Hasil diurutkan: skor tertinggi dulu."""
    timeout = timeout or settings.onu_scan_timeout
    sem = asyncio.Semaphore(concurrency or settings.onu_scan_concurrency)
    mac = norm_mac(target_mac)

    async def worker(ip: str) -> dict:
        async with sem:
            return await _probe(ip, port, timeout, mac)

    results = await asyncio.gather(*(worker(ip) for ip in ips), return_exceptions=True)
    out = [r for r in results if isinstance(r, dict)]
    out.sort(key=lambda r: (r["reachable"], r["score"], -r["ms"]), reverse=True)
    return out


async def scan_subnet(ip_in_subnet: str, port: int, target_mac: str = "", prefix: int = 24) -> list[dict]:
    return await scan(same_subnet_ips(ip_in_subnet, prefix), port, target_mac)


# --------------------------------------------------------------------------- proxy halaman

def boleh_proxy(ip: str, allowed_hosts=None) -> bool:
    """IP privat selalu boleh. IP publik HANYA bila masuk allow-list -- yaitu
    host router yang terdaftar di aplikasi (kasus situs luar via VPN L2TP yang
    ONU-nya dijangkau lewat port-mapping publik, mis. onu-165-99-95-106-45580).
    Tanpa allow-list ini aplikasi bisa disalahgunakan sebagai open proxy."""
    return is_private_ip(ip) or bool(allowed_hosts and ip in allowed_hosts)


def _rewrite_base(body: str, prefix: str) -> str:
    """Buat link relatif di halaman ONU ikut lewat proxy."""
    if "<head" in body.lower():
        return re.sub(
            r"(<head[^>]*>)",
            rf'\1<base href="{prefix}/">',
            body,
            count=1,
            flags=re.I,
        )
    return f'<base href="{prefix}/">' + body


def _rewrite_location(loc: str, ip: str, port: int, prefix: str,
                      self_host: str = "", extra_hosts=()) -> str:
    """Belokkan redirect ONU (302 setelah login) supaya tetap di dalam proxy."""
    from urllib.parse import urlsplit

    if not loc:
        return ""
    if loc.startswith("//"):
        loc = "http:" + loc
    if loc.startswith("http://") or loc.startswith("https://"):
        u = urlsplit(loc)
        host = u.netloc.split("@")[-1]
        if host in (ip, f"{ip}:{port}", self_host) or host in extra_hosts:
            return prefix + (u.path or "/") + (("?" + u.query) if u.query else "")
        return loc
    if loc.startswith("/"):
        return prefix + loc
    if not prefix:
        # mode host (prefix kosong): redirect RELATIF dibiarkan apa adanya agar
        # browser meresolusinya terhadap halaman saat ini. Halaman ONU di
        # subdirektori (mis. /html/menu/go.asp -> 302 ../bbsp/wan.asp) akan
        # rusak kalau dipaksa ke root ("/../bbsp/wan.asp").
        return loc
    return prefix + "/" + loc


def _bersihkan_set_cookie(daftar) -> list:
    """Teruskan cookie ONU ke browser tanpa Domain/Secure (biar jalan di proxy).

    Penting untuk firmware multi-halaman (HSGQ/ZimLink): "SameSite=None" TANPA
    "Secure" ditolak mentah-mentah oleh Chrome/Firefox. Karena Secure sudah kita
    buang (proxy bisa diakses lewat HTTP LAN), SameSite=None wajib ikut dibuang
    supaya cookie sesi tetap tersimpan - kalau tidak, tiap pindah halaman dianggap
    belum login dan firmware melempar balik ke halaman awal ("looping").

    Juga paksa Path=/ agar cookie sesi berlaku untuk SELURUH direktori proxy
    (/cgi-bin, /boaform, /html, dsb). Jika firmware memberi Path=/cgi-bin/ atau
    tanpa Path, halaman frame lain tidak akan menerima cookie sesi.
    """
    out = []
    for sc in daftar or []:
        sc = re.sub(r";\s*[Dd]omain=[^;]*", "", sc, flags=re.I)
        sc = re.sub(r";\s*[Ss]ecure", "", sc, flags=re.I)
        sc = re.sub(r";\s*[Ss]ame[Ss]ite\s*=\s*[Nn]one", "", sc, flags=re.I)
        if re.search(r";\s*path=[^;]*", sc, flags=re.I):
            sc = re.sub(r";\s*path=[^;]*", "; Path=/", sc, flags=re.I)
        else:
            sc += "; Path=/"
        out.append(sc)
    return out


def _inject_runtime_fix(body: str, prefix: str, ip: str = "", is_https: bool = False) -> str:
    """Tambal perilaku runtime halaman ONU: XHR/fetch/form/link yang memakai URL
    absolut http://IP-ONU/... dialihkan lewat proxy. Banyak ONU (ZTE/China Mobile)
    mengirim login lewat XHR dengan URL absolut yang tidak bisa direwrite statis.

    Hanya URL milik host proxy atau IP ONU yang dibelokkan; URL ke host lain
    (CDN, dsb.) dibiarkan utuh supaya tidak ikut terseret ke proxy."""
    csp = '<meta http-equiv="Content-Security-Policy" content="upgrade-insecure-requests">' if is_https else ''
    script = (
        csp +
        "<script>(function(){var P=" + '"' + prefix + '"' + ";"
        "var IP=" + '"' + (ip or "") + '"' + ";"
        # URL absolut ke IP privat (IP LAN ONU itu sendiri - terjadi pada ONU
        # yang dijangkau lewat port-mapping publik) juga dibelokkan ke proxy.
        "function priv(h){return /^(10\\.|192\\.168\\.|172\\.(1[6-9]|2\\d|3[01])\\.|127\\.)/.test(h);}"
        "function fix(u){if(!u||typeof u!=='string')return u;"
        "if(P&&u.indexOf(P)===0)return u;"
        "if(u.indexOf('http://')===0||u.indexOf('https://')===0||u.indexOf('//')===0){"
        "try{var a=document.createElement('a');a.href=u;var hn=a.hostname;"
        "if(hn===location.hostname||(IP&&hn===IP)||priv(hn))return P+a.pathname+a.search+(a.hash||'');"
        "return u;}catch(e){return u;}}"
        "return u;}"
        "var XO=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){try{arguments[1]=fix(u);}catch(e){}"
        "return XO.apply(this,arguments);};"
        "if(window.fetch){var F=window.fetch;"
        "window.fetch=function(i,o){if(typeof i==='string')i=fix(i);"
        "else if(i&&i.url){try{i=new Request(fix(i.url),i);}catch(e){}}"
        "return F.call(this,i,o);};}"
        "if(window.open){var WO=window.open;window.open=function(u,n,f){try{u=fix(u);}catch(e){}return WO.call(this,u,n,f);};}"
        "document.addEventListener('submit',function(e){"
        "var f=e.target;if(f&&f.action)f.action=fix(f.action);},true);"
        "document.addEventListener('click',function(e){"
        "var t=e.target,a=t&&t.closest?t.closest('a'):null;"
        "if(a&&a.getAttribute&&a.getAttribute('href'))"
        "a.setAttribute('href',fix(a.getAttribute('href')));},true);"
        "})();</script>"
    )
    m = re.search(r"<head[^>]*>", body, flags=re.I)
    if m:
        return body[: m.end()] + script + body[m.end() :]
    return script + body


def _rewrite_absolute(body: str, ip: str, port: int, prefix: str) -> str:
    """Ubah URL absolut http://IP:PORT/... jadi lewat proxy tanpa merusak subnet/CIDR."""
    hosts = [f"{ip}:{port}", ip]
    for host in hosts:
        body = body.replace(f"http://{host}/", f"{prefix}/")
        body = body.replace(f"https://{host}/", f"{prefix}/")
        body = body.replace(f"//{host}/", f"{prefix}/")
        # Ganti kutip host/ hanya bila diikuti huruf path (bukan angka/CIDR seperti /24)
        body = re.sub(rf"""(['"]){re.escape(host)}/(?=[a-zA-Z_])""", rf"""\1{prefix}/""", body)
    return body


async def proxy_fetch(prefix: str, ip: str, port: int, path: str = "/", query: str = "",
                    method: str = "GET", body: bytes = b"", content_type: str = "",
                    cookies: str = "", self_host: str = "",
                    allowed_hosts=None, rewrite_ip: str = "",
                    rewrite_port: int = 0,
                    incoming_headers: dict | None = None,
                    is_https: bool = False) -> dict:
    """Ambil satu halaman dari ONU/router dan tulis ulang linknya agar tetap di proxy.

    Hanya IP privat yang diizinkan (mencegah aplikasi dipakai menembus ke
    internet), KECUALI IP publik yang masuk allow-list host router terdaftar
    (lihat boleh_proxy).

    rewrite_ip/rewrite_port: bila koneksi ditempuh lewat NAT router situs luar
    (ip:port = host-router:port-pool), rewrite halaman tetap memakai alamat
    ONU asli supaya link absolutnya ikut dibelokkan.
    """
    if not boleh_proxy(ip, allowed_hosts):
        return {"ok": False, "status": 403,
                "text": "target bukan IP privat (dan bukan host router terdaftar)"}
    rip = rewrite_ip or ip
    rport = int(rewrite_port or port)
    extra = () if rip == ip else (ip, f"{ip}:{port}")
    url = target_url(ip, port).rstrip("/") + "/" + (path or "").lstrip("/")
    if query:
        url += "?" + query

    hdr = {}
    if incoming_headers:
        ua = incoming_headers.get("user-agent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        hdr["User-Agent"] = ua
        if "accept" in incoming_headers:
            hdr["Accept"] = incoming_headers["accept"]
        if "accept-language" in incoming_headers:
            hdr["Accept-Language"] = incoming_headers["accept-language"]
        if "x-requested-with" in incoming_headers:
            hdr["X-Requested-With"] = incoming_headers["x-requested-with"]
        if "authorization" in incoming_headers:
            hdr["Authorization"] = incoming_headers["authorization"]

        # Belokkan Referer agar ONU melihat host IP target sendiri
        ref = incoming_headers.get("referer", "")
        if ref:
            if self_host:
                ref = re.sub(rf"https?://{re.escape(self_host)}/", f"http://{ip}:{port}/", ref)
            hdr["Referer"] = ref
        else:
            hdr["Referer"] = url

        # Belokkan Origin agar lolos proteksi CORS/CSRF internal ONU
        orig = incoming_headers.get("origin", "")
        if orig:
            hdr["Origin"] = f"http://{ip}:{port}"
    else:
        hdr["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        hdr["Referer"] = url

    if body and content_type:
        hdr["Content-Type"] = content_type
    if cookies:
        hdr["Cookie"] = cookies

    timeout_sec = float(getattr(settings, "onu_proxy_timeout", 45.0) or 45.0)
    client_timeout = httpx.Timeout(timeout_sec, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=client_timeout, follow_redirects=False) as client:
            resp = await client.request(method, url, content=body or None, headers=hdr)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": 502,
            "text": (
                f"Tidak bisa membuka {url}\n{type(exc).__name__}: {exc}\n\n"
                "Periksa: (1) IP ONU benar, (2) server ini punya rute ke IP tersebut, "
                "(3) port 80 ONU terbuka."
            ),
        }
    loc = resp.headers.get("location", "")
    www_auth = resp.headers.get("www-authenticate", "")
    set_cookies = _bersihkan_set_cookie(resp.headers.get_list("set-cookie"))

    if loc and resp.status_code in (301, 302, 303, 307, 308):
        return {
            "ok": True,
            "status": resp.status_code,
            "binary": False,
            "text": "",
            "redirect": _rewrite_location(loc, rip, rport, prefix, self_host, extra),
            "set_cookies": set_cookies,
            "www_authenticate": www_auth,
        }
    ctype = resp.headers.get("content-type", "")
    lowpath = (path or "").lower()
    is_css = "css" in ctype.lower() or lowpath.endswith(".css")
    is_js = "javascript" in ctype.lower() or lowpath.endswith(".js")
    if is_css or is_js:
        try:
            teks = resp.text
        except Exception:
            teks = resp.content.decode("utf-8", errors="replace")
        return {
            "ok": True,
            "status": resp.status_code,
            "binary": False,
            "text": _rewrite_absolute(teks, rip, rport, prefix),
            "content_type": "text/css" if is_css else "application/javascript",
            "location": "",
            "set_cookies": set_cookies,
            "www_authenticate": www_auth,
        }

    is_json = "json" in ctype.lower() or lowpath.endswith(".json")
    if is_json:
        try:
            teks = resp.text
        except Exception:
            teks = resp.content.decode("utf-8", errors="replace")
        return {
            "ok": True,
            "status": resp.status_code,
            "binary": False,
            "text": _rewrite_absolute(teks, rip, rport, prefix),
            "content_type": ctype or "application/json",
            "location": "",
            "set_cookies": set_cookies,
            "www_authenticate": www_auth,
        }

    is_html = ("html" in ctype.lower() or
               lowpath.endswith((".asp", ".htm", ".html", ".php", ".cgi")) or
               resp.content.lstrip().startswith(b"<"))
    if not is_html:
        return {
            "ok": True,
            "status": resp.status_code,
            "binary": True,
            "content": resp.content,
            "content_type": ctype or "application/octet-stream",
            "set_cookies": set_cookies,
            "www_authenticate": www_auth,
        }
    try:
        body_text = resp.text
    except Exception:
        body_text = resp.content.decode("utf-8", errors="replace")

    if not body_text.strip():
        # sebagian ONU menjawab login sukses dengan badan kosong + tanpa redirect
        return {
            "ok": True,
            "status": resp.status_code,
            "binary": False,
            "text": (f'<meta http-equiv="refresh" content="0;url={prefix}/">'
                     f'<p>Membuka halaman ONU...</p>'),
            "content_type": "text/html; charset=utf-8",
            "location": "",
            "set_cookies": set_cookies,
            "www_authenticate": www_auth,
        }
    body = body_text
    if prefix:
        body = _rewrite_base(body, prefix)
    body = _inject_runtime_fix(body, prefix, rip, is_https=is_https)
    body = _rewrite_absolute(body, rip, rport, prefix)
    return {
        "ok": True,
        "status": resp.status_code,
        "binary": False,
        "text": body,
        "content_type": "text/html; charset=utf-8",
        "location": resp.headers.get("location", ""),
        "set_cookies": set_cookies,
        "www_authenticate": www_auth,
    }
