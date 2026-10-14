"""Panel operator layanan komunitas ISP-OPS.

Jalankan terpisah dari aplikasi tenant:
    OPERATOR_PASSWORD=rahasia python3 -m uvicorn panel.main:app --port 9000

Mengelola tenant (masa aktif, kuota router, suspend) dan menghasilkan teks
provisioning (compose/tunnel/onboarding). Data panel di operator.db,
terpisah dari data tenant mana pun.
"""
from __future__ import annotations

import os
import secrets
from html import escape

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import provision
from .models import SessionOp, Tenant, init_op_db

OP_USER = os.getenv("OPERATOR_USER", "operator")
OP_PASS = os.getenv("OPERATOR_PASSWORD", "")
DOMAIN = os.getenv("OPERATOR_DOMAIN", "marinagih.my.id")
OFFICE_PUBLIC = os.getenv("OFFICE_PUBLIC", "IP-PUBLIK-KANTOR")

app = FastAPI(title="ISP-OPS Operator")
_basic = HTTPBasic()
init_op_db()


def _auth(cred: HTTPBasicCredentials = Depends(_basic)) -> str:
    if not OP_PASS:
        raise HTTPException(503, "Set OPERATOR_PASSWORD di environment dulu.")
    ok = (secrets.compare_digest(cred.username, OP_USER)
          and secrets.compare_digest(cred.password, OP_PASS))
    if not ok:
        raise HTTPException(401, "kredensial salah",
                            headers={"WWW-Authenticate": "Basic"})
    return cred.username


_CSS = ("<style>body{font:14px/1.5 system-ui;margin:24px;color:#123}"
        "table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #ccd;padding:6px 10px;text-align:left}"
        "th{background:#eef}.ok{color:#070}.bad{color:#b00}"
        "form{display:inline}input,select{padding:4px}"
        ".kotak{border:1px solid #ccd;padding:12px;margin:16px 0}"
        "pre{background:#f6f6f6;padding:10px;overflow:auto}</style>")


def _baris(t: Tenant) -> str:
    sisa = provision.days_left(t)
    warna = "ok" if (t.status == "active" and sisa > 14) else "bad"
    fqdn = provision.tenant_fqdn(t.subdomain, DOMAIN)
    return (
        f"<tr><td>{escape(t.name)}</td>"
        f"<td><a href='https://{fqdn}' target='_blank'>{fqdn}</a></td>"
        f"<td>{t.router_limit}</td>"
        f"<td>{t.expires_at:%d-%m-%Y}</td>"
        f"<td class='{warna}'>{t.status} ({sisa} hari)</td>"
        f"<td>"
        f"<form method='post' action='/perpanjang/{t.id}'>"
        f"<input type='number' name='days' value='365' min='1' style='width:70px'>"
        f"<button>perpanjang</button></form> "
        f"<form method='post' action='/status/{t.id}'>"
        f"<button>{'suspensi' if t.status == 'active' else 'aktifkan'}</button></form> "
        f"<form method='post' action='/hapus/{t.id}' "
        f"onsubmit=\"return confirm('hapus {escape(t.name)}?')\">"
        f"<button>hapus</button></form> "
        f"<a href='/snippet/{t.id}'>provisioning</a>"
        f"</td></tr>"
    )


@app.get("/", response_class=HTMLResponse)
def dasbor(_u: str = Depends(_auth)):
    provision.sweep_expired()
    db = SessionOp()
    try:
        tenants = db.query(Tenant).order_by(Tenant.name).all()
        isi = "".join(_baris(t) for t in tenants) or "<tr><td colspan=6>belum ada tenant</td></tr>"
    finally:
        db.close()
    return f"""<!doctype html><html lang=id><head><meta charset=utf-8>
<title>Operator ISP-OPS</title>{_CSS}</head><body>
<h1>Panel Operator ISP-OPS</h1>
<div class=kotak><b>Tenant baru</b>
<form method=post action=/buat>
 nama <input name=name required placeholder="Munir">
 subdomain / domain <input name=subdomain required placeholder="munir atau munir.my.id" style="width:170px">
 kuota router <input name=router_limit type=number value=1 min=1 style="width:60px">
 masa aktif (hari) <input name=days type=number value=365 min=1 style="width:80px">
 catatan <input name=notes placeholder="opsional">
 <button>buat</button>
</form></div>
<table><tr><th>nama</th><th>domain</th><th>kuota router</th><th>tenggat</th>
<th>status</th><th>aksi</th></tr>{isi}</table>
<p>Model bisnis: berlangganan = semua fitur; biaya tambahan hanya per router
(naikkan kuota lewat tombol provisioning/edit). Container tenant yang
<em>expired</em> wajib di-stop di host (lihat teks provisioning).</p>
</body></html>"""


@app.post("/buat")
def buat(_u: str = Depends(_auth), name: str = Form(...),
         subdomain: str = Form(...), router_limit: int = Form(1),
         days: int = Form(365), notes: str = Form("")):
    db = SessionOp()
    try:
        ada = (db.query(Tenant).filter((Tenant.name == name.strip())
                                       | (Tenant.subdomain == subdomain.strip().lower()))
               .first())
    finally:
        db.close()
    if ada:
        raise HTTPException(409, "nama/subdomain sudah dipakai")
    provision.create_tenant(name, subdomain, router_limit, days, notes)
    return RedirectResponse("/", status_code=303)


@app.post("/perpanjang/{tid}")
def perpanjang(tid: int, _u: str = Depends(_auth), days: int = Form(365)):
    provision.extend_tenant(tid, max(1, days))
    return RedirectResponse("/", status_code=303)


@app.post("/status/{tid}")
def status(tid: int, _u: str = Depends(_auth)):
    db = SessionOp()
    try:
        t = db.get(Tenant, tid)
        kini = t.status if t else "active"
    finally:
        db.close()
    provision.set_status(tid, "suspended" if kini == "active" else "active")
    return RedirectResponse("/", status_code=303)


@app.post("/hapus/{tid}")
def hapus(tid: int, _u: str = Depends(_auth)):
    provision.remove_tenant(tid)
    return RedirectResponse("/", status_code=303)


@app.get("/snippet/{tid}", response_class=HTMLResponse)
def snippet(tid: int, _u: str = Depends(_auth)):
    db = SessionOp()
    try:
        t = db.get(Tenant, tid)
        if t is None:
            raise HTTPException(404, "tenant tidak ditemukan")
        teks = ("# docker-compose.yml (tambahkan ke services:)\n"
                + provision.compose_snippet(t, DOMAIN)
                + "\n# ingress cloudflared (taruh di atas entri '*'):\n"
                + provision.tunnel_snippet(t, DOMAIN)
                + "\n# onboarding:\n"
                + provision.onboarding_text(t, DOMAIN, OFFICE_PUBLIC,
                                            "10.0.0.0/24"))
    finally:
        db.close()
    return f"""<!doctype html><html lang=id><head><meta charset=utf-8>
<title>Provisioning {escape(t.subdomain)}</title>{_CSS}</head><body>
<h1>Provisioning {escape(t.name)}</h1>
<p>Status: {t.status} &middot; tenggat {t.expires_at:%d-%m-%Y} &middot;
kuota router {t.router_limit}</p>
<pre>{escape(teks)}</pre>
<p><a href='/'>kembali</a></p></body></html>"""


@app.get("/health")
def health():
    return {"ok": True}
