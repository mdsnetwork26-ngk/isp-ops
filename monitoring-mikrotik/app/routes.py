"""Semua endpoint HTTP (halaman + API JSON + proxy halaman ONU)."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, billing, onu, services
from .config import settings
from .kompat import to_thread
from .database import get_db
from .mikrotik import make_client
from .models import ActivityLog, Customer, Router, User, UserRouter
from .services import norm_name, pool
from .util import pretty_mac

router = APIRouter()


# ============================================================================ skema

class RouterIn(BaseModel):
    id: Optional[int] = None
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=120)
    port: int = 8728
    username: str = Field(min_length=1, max_length=80)
    password: str = ""
    site: str = ""
    enabled: bool = True
    poll_interval: int = 5
    manage_ip: str = ""
    api_ssl: str = "auto"


class CustomerIn(BaseModel):
    id: Optional[int] = None
    router_id: int
    name: str = Field(min_length=1, max_length=120)  # nama PPPoE
    full_name: str = ""
    phone: str = ""
    address: str = ""
    ip: str = ""
    mac: str = ""
    onu_ip: str = ""
    onu_port: int = 80
    onu_info: str = ""
    router_ip: str = ""
    router_port: int = 80
    billing_code: str = ""
    notes: str = ""


class ActionIn(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    router_id: Optional[int] = None
    reason: str = "belum bayar"


# ============================================================================ bantu

def _need_customer(db: Session, payload) -> Customer:
    cust = None
    if getattr(payload, "id", None):
        cust = db.get(Customer, payload.id)
    elif getattr(payload, "name", None):
        q = db.query(Customer).filter(Customer.name == payload.name)
        if getattr(payload, "router_id", None):
            q = q.filter(Customer.router_id == payload.router_id)
        cust = q.first()
    if cust is None:
        raise HTTPException(404, "pelanggan tidak ditemukan di database")
    return cust


def _actor(request: Request) -> str:
    return getattr(request.state, "user", None) or "teknisi"


def _router_terlihat(request: Request, db: Session):
    """None = semua router (admin/teknisi, atau pemantau belum dibatasi);
    selain itu set id router yang boleh dilihat pemantau."""
    if getattr(request.state, "role", "admin") != "pemantau":
        return None
    u = db.query(User).filter(
        User.username == getattr(request.state, "user", "")).first()
    if u is None:
        return set()
    rows = db.query(UserRouter.router_id).filter(UserRouter.user_id == u.id).all()
    if not rows:
        return None
    return {r[0] for r in rows}


def _boleh(request: Request, *peran: str) -> None:
    role = getattr(request.state, "role", "admin")
    if role not in peran:
        raise HTTPException(403, "khusus " + "/".join(peran))


def _require_admin(request: Request) -> None:
    """Teknisi hanya boleh memantau dan menambah klien; sisanya khusus admin."""
    if getattr(request.state, "role", "admin") != "admin":
        raise HTTPException(403, "khusus admin")


# ============================================================================ router mikrotik

@router.get("/api/me")
def api_me(request: Request):
    return {
        "user": getattr(request.state, "user", "teknisi"),
        "role": getattr(request.state, "role", "admin"),
    }


@router.get("/api/routers")
def api_routers(request: Request, db: Session = Depends(get_db)):
    lihat = _router_terlihat(request, db)
    q = db.query(Router).order_by(Router.name)
    if lihat is not None:
        q = q.filter(Router.id.in_(lihat or [0]))
    rows = q.all()
    out = []
    for r in rows:
        snap = pool.snapshot(r.id)
        out.append(
            {
                "id": r.id,
                "name": r.name,
                "host": r.host,
                "port": r.port,
                "username": r.username,
                "site": r.site,
                "enabled": r.enabled,
                "poll_interval": r.poll_interval,
                "manage_ip": r.manage_ip,
                "api_ssl": getattr(r, "api_ssl", "auto") or "auto",
                "online": snap.online if snap else None,
                "message": snap.message if snap else "belum dipantau",
                "ms": snap.ms if snap else 0,
                "stats": snap.stats if snap else {},
            }
        )
    return {"routers": out}


@router.post("/api/routers")
def api_save_router(payload: RouterIn, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    obj = db.get(Router, payload.id) if payload.id else None
    if obj is None:
        # kuota router berlangganan (0 = tanpa batas): menambah router baru
        # dibatasi, mengubah router lama tetap bebas
        if settings.router_limit > 0 and db.query(Router).count() >= settings.router_limit:
            raise HTTPException(
                403,
                f"Kuota router berlangganan sudah tercapai (maksimal "
                f"{settings.router_limit}). Hubungi penyedia layanan untuk "
                "menambah kuota.",
            )
        obj = Router(name=payload.name)
        db.add(obj)
    for field in (
        "name", "host", "port", "username", "password", "site",
        "enabled", "poll_interval", "manage_ip", "api_ssl",
    ):
        value = getattr(payload, field)
        if field == "password" and value == "":
            continue  # kosong = jangan ubah password
        setattr(obj, field, value)
    db.commit()
    pool.drop_client(obj.id)
    services.log_action(
        db, actor=_actor(request), action="router-save",
        customer_name="", router_name=obj.name, detail=f"{obj.host}:{obj.port}",
    )
    db.commit()
    return {"ok": True, "id": obj.id}


@router.post("/api/routers/{rid}/delete")
def api_delete_router(rid: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    obj = db.get(Router, rid)
    if obj is None:
        raise HTTPException(404, "router tidak ditemukan")
    nama, host = obj.name, obj.host
    db.delete(obj)
    db.commit()
    pool.drop_client(rid)
    try:
        services.log_action(
            db, actor=_actor(request), action="router-delete",
            customer_name="", router_name=nama, detail=host,
        )
        db.commit()
    except Exception:  # noqa: BLE001 - mencatat log tidak boleh menggagalkan hapus
        db.rollback()
    return {"ok": True}


@router.post("/api/routers/{rid}/test")
async def api_test_router(rid: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    obj = db.get(Router, rid)
    if obj is None:
        raise HTTPException(404, "router tidak ditemukan")
    import asyncio

    if obj.host == "demo" and pool._demo is not None:  # noqa: SLF001
        ok, msg = await to_thread(pool._demo.check)  # noqa: SLF001
        return {"ok": ok, "message": msg, "backend": "demo"}
    client = make_client(obj, backend="ssh" if settings.ssh_fallback else "api")
    try:
        ok, msg = await to_thread(client.check)
        info = {}
        if ok:
            try:
                info["secrets"] = len(await to_thread(client.ppp_secrets))
                info["active"] = len(await to_thread(client.ppp_active))
            except Exception as exc:  # noqa: BLE001
                info["warning"] = str(exc)
            # sekalian pastikan rule firewall isolir ada, supaya tombol Isolir
            # tidak perlu membuat rule di saat genting
            try:
                dibuat = await to_thread(
                    client.ensure_isolir_rule,
                    settings.isolir_list, settings.isolir_action,
                    settings.isolir_rule_comment,
                )
                info["rule"] = "dibuat" if dibuat else "sudah ada"
            except Exception as exc:  # noqa: BLE001
                info["rule"] = f"gagal: {exc}"
        return {"ok": ok, "message": msg, "info": info}
    finally:
        await to_thread(client.close)


# ============================================================================ data pantauan

@router.get("/api/status")
async def api_status(
    request: Request,
    router_id: Optional[int] = None,
    q: str = "",
    status: str = "",
    limit: int = 0,
    db: Session = Depends(get_db),
):
    lihat = _router_terlihat(request, db)
    existing_rids = {r[0] for r in db.query(Router.id).all()}
    if router_id:
        snaps = [pool.snapshot(router_id)] if router_id in existing_rids else []
    else:
        snaps = [s for s in pool.snapshots() if s and s.router_id in existing_rids]
    snaps = [s for s in snaps if s]
    if lihat is not None:
        snaps = [s for s in snaps if s.router_id in (lihat or {0})]
    text = (q or "").strip().lower()
    out = []
    for snap in snaps:
        data = snap.to_dict()
        rows = data["sessions"]
        if text:
            rows = [
                s for s in rows
                if text in s["name"].lower()
                or text in (s["full_name"] or "").lower()
                or text in (s["ip"] or "").lower()
                or text in (s["mac"] or "").lower().replace(":", "")
                or text in (s["phone"] or "").lower()
                or text in (s["address"] or "").lower()
            ]
        if status and status != "all":
            rows = [s for s in rows if s["status"] == status]
        # 1500 pelanggan sekaligus memberatkan browser; potong bila diminta.
        # Statistik tetap dihitung dari SEMUA pelanggan, bukan dari potongan.
        data["matched"] = len(rows)
        data["sessions"] = rows[:limit] if limit and limit > 0 else rows
        out.append(data)
    return {"routers": out, "billing": billing.status(), "demo": settings.demo_mode,
            "auto_isolir_grace_days": settings.auto_isolir_grace_days}


@router.post("/api/refresh")
async def api_refresh(router_id: Optional[int] = None):
    if router_id:
        snap = await services.refresh_router(router_id)
        if snap is None:
            raise HTTPException(404, "router tidak ditemukan")
        return {"ok": True, "router": snap.to_dict()}
    snaps = await services.refresh_all()
    return {"ok": True, "routers": [s.to_dict() for s in snaps]}


# ============================================================================ pelanggan

@router.get("/api/customers")
def api_customers(
    router_id: Optional[int] = None,
    source: Optional[str] = None,
    q: str = "",
    db: Session = Depends(get_db),
):
    query = db.query(Customer).order_by(Customer.name)
    if router_id:
        query = query.filter(Customer.router_id == router_id)
    if source:
        query = query.filter(Customer.source == source)
    text = (q or "").strip().lower()
    if text:
        query = query.filter(Customer.name.like(f"%{text}%"))
    return {
        "auto_isolir_grace_days": settings.auto_isolir_grace_days,
        "customers": [
            {
                "id": c.id,
                "router_id": c.router_id,
                "name": c.name,
                "full_name": c.full_name,
                "phone": c.phone,
                "address": c.address,
                "ip": c.ip,
                "mac": pretty_mac(c.mac),
                "onu_ip": c.onu_ip,
                "onu_port": c.onu_port,
                "onu_info": c.onu_info,
                "router_ip": c.router_ip,
                "router_port": c.router_port,
                "isolated": c.isolated,
                "billing_code": c.billing_code,
                "is_paid": c.is_paid,
                "source": getattr(c, "source", "sync") or "sync",
                "auto_isolir": bool(getattr(c, "auto_isolir", False)),
                "auto_isolir_date": c.auto_isolir_date.isoformat() if getattr(c, "auto_isolir_date", None) else "",
                "notes": c.notes,
                "last_seen": c.last_seen.isoformat() if c.last_seen else "",
            }
            for c in query.all()
        ]
    }


@router.post("/api/customers")
def api_save_customer(payload: CustomerIn, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    obj = db.get(Customer, payload.id) if payload.id else None
    if obj is None:
        obj = Customer(router_id=payload.router_id, name=payload.name, source="manual")
        db.add(obj)
    for field in (
        "router_id", "name", "full_name", "phone", "address", "ip", "mac",
        "onu_ip", "onu_port", "onu_info", "router_ip", "router_port",
        "billing_code", "notes",
    ):
        setattr(obj, field, getattr(payload, field))
    obj.mac = payload.mac.replace(":", "").replace("-", "").lower()
    db.commit()
    return {"ok": True, "id": obj.id}


@router.post("/api/customers/{cid}/delete")
def api_delete_customer(cid: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    obj = db.get(Customer, cid)
    if obj is None:
        raise HTTPException(404, "pelanggan tidak ditemukan")
    db.delete(obj)
    db.commit()
    return {"ok": True}


@router.post("/api/customers/sync")
async def api_sync_customers(router_id: int, request: Request, db: Session = Depends(get_db)):
    """Tarik daftar PPP secret dari mikrotik ke database (sekali klik, rapi).

    Cocokkan nama pakai kunci longgar (budi-santoso = budi_santoso = Budi.Santoso)
    supaya tidak membuat data pelanggan kembar.
    """
    import asyncio

    _require_admin(request)
    obj = db.get(Router, router_id)
    if obj is None:
        raise HTTPException(404, "router tidak ditemukan")
    client = pool.client_for(obj)
    try:
        secrets = await to_thread(client.ppp_secrets)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"gagal ambil PPP secret: {exc}")
    existing = {
        norm_name(c.name): c
        for c in db.query(Customer).filter(Customer.router_id == router_id).all()
    }
    added = updated = 0
    for sec in secrets:
        key = norm_name(sec.name)
        cust = existing.get(key)
        if cust is None:
            cust = Customer(router_id=router_id, name=sec.name, full_name=sec.comment)
            db.add(cust)
            added += 1
        else:
            if sec.comment and not cust.full_name:
                cust.full_name = sec.comment
            updated += 1
    try:
        db.commit()
    except Exception:  # nama kembar / bentrok constraint
        db.rollback()
        raise HTTPException(
            409,
            "gagal menyimpan: ada nama PPPoE kembar di database. "
            "Rapikan lewat menu Pengaturan lalu ulangi.",
        )
    # langsung pantau ulang supaya data baru langsung terlihat di layar monitoring
    snap = await services.refresh_router(router_id)
    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "total_secret": len(secrets),
        "stats": snap.stats if snap else {},
    }


# ============================================================================ pengguna (admin)

class UserIn(BaseModel):
    username: str
    password: str = ""
    role: str = "teknisi"


@router.get("/api/users")
def api_users(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    rows = db.query(User).order_by(User.username).all()
    return {"users": [{"id": r.id, "username": r.username, "role": r.role} for r in rows]}


@router.post("/api/users")
def api_save_user(payload: UserIn, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    nama = payload.username.strip()
    if not nama or len(payload.password) < 4:
        return JSONResponse({"ok": False, "error": "username wajib; password minimal 4 karakter"}, 400)
    if payload.role not in ("admin", "teknisi", "pemantau"):
        return JSONResponse({"ok": False, "error": "peran tidak dikenal"}, 400)
    if db.query(User).filter(User.username == nama).first():
        return JSONResponse({"ok": False, "error": f"username '{nama}' sudah ada"}, 400)
    h, salt = auth.hash_password(payload.password)
    db.add(User(username=nama, password_hash=h, salt=salt, role=payload.role))
    db.commit()
    services.log_action(db, actor=_actor(request), action="user-add",
                        customer_name="", router_name="", detail=f"{nama} ({payload.role})")
    db.commit()
    return {"ok": True}


@router.post("/api/users/{uid}/delete")
def api_delete_user(uid: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(404, "user tidak ditemukan")
    if u.username == getattr(request.state, "user", None):
        return JSONResponse({"ok": False, "error": "tidak bisa menghapus akun sendiri"}, 400)
    if u.role == "admin" and db.query(User).filter(User.role == "admin").count() <= 1:
        return JSONResponse({"ok": False, "error": "tidak bisa menghapus admin terakhir"}, 400)
    nama = u.username
    db.delete(u)
    db.commit()
    services.log_action(db, actor=_actor(request), action="user-delete",
                        customer_name="", router_name="", detail=nama)
    db.commit()
    return {"ok": True}


class AutoIsolirIn(BaseModel):
    enabled: bool
    date: str = ""  # YYYY-MM-DD; kosong = auto-isolir langsung aktif


@router.post("/api/customers/{cid}/auto-isolir")
def api_auto_isolir(cid: int, payload: AutoIsolirIn, request: Request,
                    db: Session = Depends(get_db)):
    """Tandai pelanggan agar diisolir otomatis setiap kali terlihat online.
    Hanya admin. Default semua pelanggan: isolir manual."""
    _require_admin(request)
    obj = db.get(Customer, cid)
    if obj is None:
        raise HTTPException(404, "pelanggan tidak ditemukan")
    from datetime import date as _date

    tanggal = (payload.date or "").strip()
    if tanggal:
        try:
            obj.auto_isolir_date = _date.fromisoformat(tanggal)
        except ValueError:
            return JSONResponse({"ok": False, "error": "format tanggal harus YYYY-MM-DD"}, 400)
    else:
        obj.auto_isolir_date = None
    obj.auto_isolir = bool(payload.enabled)
    db.commit()
    services.log_action(
        db, actor=_actor(request),
        action="auto-isolir-aktif" if obj.auto_isolir else "auto-isolir-nonaktif",
        customer_name=obj.name, router_name="",
        detail=(f"tanggal {obj.auto_isolir_date.isoformat()}, aktif +{settings.auto_isolir_grace_days} hari"
                if obj.auto_isolir and obj.auto_isolir_date else ""),
    )
    db.commit()
    return {"ok": True, "auto_isolir": obj.auto_isolir,
            "auto_isolir_date": obj.auto_isolir_date.isoformat() if obj.auto_isolir_date else ""}


class UserRoutersIn(BaseModel):
    router_ids: List[int]


@router.get("/api/users/{uid}/routers")
def api_user_routers(uid: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    ids = [r.router_id for r in
           db.query(UserRouter).filter(UserRouter.user_id == uid).all()]
    return {"router_ids": ids}


@router.post("/api/users/{uid}/routers")
def api_set_user_routers(uid: int, payload: UserRoutersIn, request: Request,
                         db: Session = Depends(get_db)):
    _require_admin(request)
    if db.get(User, uid) is None:
        raise HTTPException(404, "user tidak ditemukan")
    db.query(UserRouter).filter(UserRouter.user_id == uid).delete()
    for rid in payload.router_ids:
        if db.get(Router, rid) is not None:
            db.add(UserRouter(user_id=uid, router_id=rid))
    db.commit()
    return {"ok": True}


# ============================================================================ pppoe (tambah klien)

class PppoeIn(BaseModel):
    router_id: int
    name: str
    password: str = ""
    profile: str = "default"


@router.get("/api/ppp/profiles")
async def api_ppp_profiles(router_id: int, db: Session = Depends(get_db)):
    obj = db.get(Router, router_id)
    if obj is None:
        raise HTTPException(404, "router tidak ditemukan")
    client = pool.client_for(obj)
    try:
        return {"profiles": await to_thread(client.ppp_profiles)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"gagal ambil profile: {exc}")


@router.post("/api/ppp/secret/add")
async def api_ppp_add(payload: PppoeIn, request: Request):
    _boleh(request, "admin", "teknisi")
    res = await services.add_ppp_secret(
        actor=_actor(request),
        router_id=payload.router_id,
        name=payload.name,
        password=payload.password,
        profile=payload.profile,
    )
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return res


# ============================================================================ isolir / buka

@router.post("/api/isolir")
async def api_isolir(payload: ActionIn, request: Request):
    _boleh(request, "admin", "teknisi")
    res = await services.isolate_customer(
        actor=_actor(request),
        reason=payload.reason,
        customer_id=payload.id,
        name=payload.name,
        router_id=payload.router_id,
    )
    if not res.get("ok"):
        status = 404 if "tidak ditemukan" in res.get("error", "") else 400
        return JSONResponse(status_code=status, content=res)
    return res


@router.post("/api/buka")
async def api_buka(payload: ActionIn, request: Request):
    _boleh(request, "admin", "teknisi")
    res = await services.open_customer(
        actor=_actor(request),
        customer_id=payload.id,
        name=payload.name,
        router_id=payload.router_id,
    )
    if not res.get("ok"):
        status = 404 if "tidak ditemukan" in res.get("error", "") else 400
        return JSONResponse(status_code=status, content=res)
    return res


@router.post("/api/ping")
async def api_ping(payload: ActionIn, db: Session = Depends(get_db)):
    """Ping IP pelanggan lewat mikrotik. Tidak wajib ada baris pelanggan:
    cukup nama + router, IP diambil dari sesi yang terlihat di mikrotik."""
    import asyncio

    cust = services.find_customer(db, payload.id, payload.name, payload.router_id)
    rid = cust.router_id if cust else payload.router_id
    obj = db.get(Router, rid) if rid else None
    if obj is None:
        raise HTTPException(404, "router tidak ditemukan")
    nama = (cust.name if cust else payload.name) or ""
    snap = pool.snapshot(rid)
    ip = cust.ip if cust and cust.ip else ""
    if snap and nama:
        for s in snap.sessions:
            if norm_name(s.name) == norm_name(nama) and s.ip:
                ip = s.ip
                break
    if not ip:
        return {"ok": False, "error": "IP pelanggan belum diketahui (pelanggan offline?)"}
    client = pool.client_for(obj)
    try:
        received, ms = await to_thread(client.ping, ip)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": received > 0, "ip": ip, "received": received, "ms": ms}


@router.get("/api/logs")
def api_logs(limit: int = 60, request: Request = None, db: Session = Depends(get_db)):
    _require_admin(request)
    rows = (
        db.query(ActivityLog).order_by(ActivityLog.id.desc()).limit(min(limit, 500)).all()
    )
    return {
        "logs": [
            {
                "id": r.id,
                "ts": r.ts.isoformat() if r.ts else "",
                "actor": r.actor,
                "action": r.action,
                "customer_name": r.customer_name,
                "router_name": r.router_name,
                "detail": r.detail,
                "success": r.success,
            }
            for r in rows
        ]
    }


@router.post("/api/logs/clear")
def api_logs_clear(db: Session = Depends(get_db)):
    n = db.query(ActivityLog).delete()
    db.commit()
    return {"ok": True, "deleted": n}


@router.get("/api/onu/cek")
async def api_onu_cek(ip: str, port: int = 80, request: Request = None):
    """Alat diagnosis (admin): dari SERVER ini, cek TCP + HTTP ke ONU.
    Bila ini sukses tapi browser gagal, masalahnya di tunnel/DNS/cookie,
    bukan di keterjangkauan ONU."""
    _require_admin(request)
    import socket as _socket

    from . import onu as onu_mod

    out = {"ip": ip, "port": port, "tcp": False}
    try:
        sok = _socket.create_connection((ip, port), 3)
        sok.close()
        out["tcp"] = True
    except Exception as exc:  # noqa: BLE001
        out["error_tcp"] = str(exc)
        return out
    res = await onu_mod.proxy_fetch("", ip, port, "/", "")
    out["http_ok"] = bool(res.get("ok"))
    out["http_status"] = res.get("status")
    out["content_type"] = res.get("content_type", "")
    teks = res.get("text", "") or ""
    out["sampel"] = teks[:200]
    return out


# ============================================================================ proxy halaman ONU / router

@router.api_route("/proxy/{rid}/http/{target}/{path:path}", methods=["GET", "POST"], response_class=HTMLResponse)
@router.api_route("/proxy/{rid}/http/{target}", methods=["GET", "POST"], response_class=HTMLResponse)
async def proxy_router_page(rid: int, target: str, path: str = "", request: Request = None):
    """Buka http://TARGET/path lewat server ini.

    Dipakai kalau web ONU hanya bisa diakses dari sisi router, atau kalau PC
    teknisi tidak satu jaringan dengan IP manajemen ONU.
    """
    ip, _, port_s = target.partition(":")
    try:
        port = int(port_s) if port_s else 80
    except ValueError:
        port = 80
    prefix = f"/proxy/{rid}/http/{target}"
    query = str(request.query_params) if request is not None else ""
    body = await request.body() if request is not None and request.method == "POST" else b""
    ctype_in = request.headers.get("content-type", "") if request is not None else ""
    kuki = ""
    if request is not None:
        # cookie ONU yang disimpan browser diteruskan kembali ke ONU
        # (cookie sesi aplikasi sendiri tidak ikut)
        kuki = "; ".join(f"{k}={v}" for k, v in request.cookies.items()
                         if k != "ispops_sid")
    res = await onu.proxy_fetch(prefix, ip, port, path, query,
                                method=request.method if request is not None else "GET",
                                body=body, content_type=ctype_in, cookies=kuki,
                                self_host=request.headers.get("host", ""))

    def _balik(resp):
        for sc in res.get("set_cookies") or []:
            resp.headers.append("Set-Cookie", sc)
        return resp
    if not res.get("ok"):
        return _balik(HTMLResponse(
            f"<pre style='font:14px/1.6 system-ui;padding:24px'>{res.get('text', 'gagal')}</pre>",
            status_code=res.get("status", 502),
        ))
    if res.get("binary"):
        return _balik(Response(
            content=res["content"],
            media_type=res.get("content_type", "application/octet-stream"),
        ))
    if res.get("redirect"):
        return _balik(RedirectResponse(res["redirect"],
                                       status_code=res.get("status", 302)))
    if res.get("location"):
        loc = res["location"]
        if loc.startswith("http"):
            return _balik(HTMLResponse(
                f"<meta http-equiv='refresh' content='0;url={loc}'>"
                f"<p>Redirect ke <a href='{loc}'>{loc}</a></p>",
                status_code=res.get("status", 200),
            ))
    ct = res.get("content_type", "") or "text/html; charset=utf-8"
    if "html" not in ct:
        # CSS/JS: media type harus apa adanya, HTMLResponse memaksa text/html
        return _balik(Response(
            content=res["text"].encode("utf-8", "replace"),
            media_type=ct,
            status_code=res.get("status", 200),
        ))
    return _balik(HTMLResponse(res["text"], status_code=res.get("status", 200)))
