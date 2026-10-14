"""NAT sementara OTOMATIS di router situs luar untuk Remote ONU.

Alur: pengguna klik Remote ONU (subdomain onu-...) -> middleware melihat ONU
itu milik router ber-host publik (situs VPN) -> modul ini membuat rule
dst-nat di router tersebut lewat API RouterOS (port publik dari pool ->
IP ONU) -> proxy menyambung ke host-router:port. Begitu tidak ada aktivitas
lewat TTL (default 10 menit), sweep latar belakang menghapus rule-nya --
jadi ONU tidak terekspos lebih lama dari kebutuhan.

Prasyarat: provider VPN memforward RENTANG port (ONU_NAT_PORTS, mis.
45600-45699) ke router situs luar -- dipesan SEKALI, setelah itu penambahan
ONU tidak butuh kontak ke provider lagi.

Semua rule diberi comment berpenanda "ispops-onu:" supaya gampang dikenali
dan dibersihkan (termasuk sisa sebelum restart aplikasi).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .mikrotik import make_client
from .mikrotik.api import _w
from .models import OnuNat, Router
from .util import is_private_ip

KOMENTAR = "ispops-onu:"              # penanda rule NAT per ONU
KOMENTAR_ALLOW = "ispops-onu-allow"   # penanda satu rule forward per router
LIST_ONU = "onu-remote"               # address-list berisi IP ONU yang aktif


def pool_ports() -> list:
    """Parse ONU_NAT_PORTS: '45600-45699' atau '45600,45601'. [] = fitur mati."""
    raw = (settings.onu_nat_ports or "").strip()
    if not raw:
        return []
    if "-" in raw and "," not in raw:
        a, _, b = raw.partition("-")
        try:
            return list(range(int(a.strip()), int(b.strip()) + 1))
        except ValueError:
            return []
    out = []
    for t in raw.split(","):
        try:
            out.append(int(t.strip()))
        except ValueError:
            continue
    return out


def _kom_onu(onu_ip: str, onu_port: int) -> str:
    return f"{KOMENTAR}{onu_ip}:{onu_port}"


def ensure_nat(router: Router, onu_ip: str, onu_port: int):
    """Pastikan rule NAT untuk ONU ini ada di router; kembalikan port publiknya.
    None bila fitur mati / pool habis / router tak terjangkau (middleware lalu
    jatuh ke koneksi langsung seperti biasa)."""
    ports = pool_ports()
    if not ports or is_private_ip(router.host):
        return None
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        row = (db.query(OnuNat)
               .filter_by(router_id=router.id, onu_ip=onu_ip, onu_port=onu_port)
               .first())
        if row and (now - row.last_seen) < timedelta(seconds=settings.onu_nat_ttl):
            row.last_seen = now          # masih hangat: pakai lagi, tanpa ke router
            db.commit()
            return row.nat_port
        dipakai = {r.nat_port for r in db.query(OnuNat).filter_by(router_id=router.id)}
        nat_port = row.nat_port if (row and row.nat_port in ports) else None
        if nat_port is None:
            for p in ports:
                if p not in dipakai:
                    nat_port = p
                    break
        if nat_port is None:             # pool penuh
            return None
    finally:
        db.close()

    kom = _kom_onu(onu_ip, onu_port)
    try:
        api = make_client(router)
        with api:
            # buang rule lama dengan penanda sama (port boleh berubah)
            for r in api._run("/ip/firewall/nat/print", _w("?comment", kom)):
                if r.get(".id"):
                    api._run("/ip/firewall/nat/remove", _w(".id", r[".id"]))
            api._run(
                "/ip/firewall/nat/add",
                _w("chain", "dstnat"), _w("protocol", "tcp"),
                _w("dst-port", str(nat_port)), _w("action", "dst-nat"),
                _w("to-addresses", onu_ip), _w("to-ports", str(onu_port)),
                _w("comment", kom),
            )
            # satu rule forward per router (idempoten, berpenanda)
            if not api._run("/ip/firewall/filter/print",
                            _w("?comment", KOMENTAR_ALLOW)):
                kata = [_w("chain", "forward"), _w("protocol", "tcp"),
                        _w("dst-address-list", LIST_ONU), _w("action", "accept"),
                        _w("comment", KOMENTAR_ALLOW)]
                try:
                    api._run("/ip/firewall/filter/insert", _w("0"), *kata)
                except Exception:  # noqa: BLE001 - RouterOS lama menolak insert
                    api._run("/ip/firewall/filter/add", *kata)
            # daftarkan IP ONU ke address-list (idempoten; ulang tiap ensure
            # supaya sembuh sendiri bila pernah gagal)
            try:
                api._run("/ip/firewall/address-list/add",
                         _w("list", LIST_ONU), _w("address", onu_ip),
                         _w("comment", KOMENTAR))
            except Exception:  # noqa: BLE001 - sudah ada = biasa
                pass
    except Exception:  # noqa: BLE001 - router tak terjangkau: jatuh ke direct
        return None

    db = SessionLocal()
    try:
        row = (db.query(OnuNat)
               .filter_by(router_id=router.id, onu_ip=onu_ip, onu_port=onu_port)
               .first())
        if row is None:
            row = OnuNat(router_id=router.id, onu_ip=onu_ip, onu_port=onu_port,
                         nat_port=nat_port)
            db.add(row)
        else:
            row.nat_port = nat_port
        row.last_seen = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    return nat_port


def touch(router_id: int, onu_ip: str, onu_port: int) -> None:
    """Perpanjang napas NAT: dipanggil tiap ada request proxy yang lewat."""
    db = SessionLocal()
    try:
        (db.query(OnuNat)
         .filter_by(router_id=router_id, onu_ip=onu_ip, onu_port=onu_port)
         .update({"last_seen": datetime.utcnow()}))
        db.commit()
    finally:
        db.close()


def sweep_once() -> int:
    """Hapus NAT yang idle lewat TTL: rule di router + barisnya. Router yang
    tak terjangkau dibiarkan untuk putaran berikutnya."""
    batas = datetime.utcnow() - timedelta(seconds=settings.onu_nat_ttl)
    db = SessionLocal()
    try:
        kedaluwarsa = db.query(OnuNat).filter(OnuNat.last_seen < batas).all()
        if not kedaluwarsa:
            return 0
        per_router: dict = {}
        for r in kedaluwarsa:
            per_router.setdefault(r.router_id, []).append(r)
        hapus_db = []
        for rid, rows in per_router.items():
            router = db.query(Router).get(rid)
            if router is None:
                hapus_db.extend(rows)
                continue
            try:
                api = make_client(router)
                with api:
                    for r in rows:
                        kom = _kom_onu(r.onu_ip, r.onu_port)
                        for n in api._run("/ip/firewall/nat/print",
                                          _w("?comment", kom)):
                            if n.get(".id"):
                                api._run("/ip/firewall/nat/remove",
                                         _w(".id", n[".id"]))
                        sisa = (db.query(OnuNat)
                                .filter_by(router_id=rid, onu_ip=r.onu_ip)
                                .filter(OnuNat.id != r.id).count())
                        if sisa == 0:
                            for a in api._run(
                                    "/ip/firewall/address-list/print",
                                    _w("?list", LIST_ONU),
                                    _w("?address", r.onu_ip)):
                                if a.get(".id"):
                                    api._run("/ip/firewall/address-list/remove",
                                             _w(".id", a[".id"]))
                hapus_db.extend(rows)
            except Exception:  # noqa: BLE001 - coba lagi putaran depan
                continue
        for r in hapus_db:
            db.delete(r)
        db.commit()
        return len(hapus_db)
    finally:
        db.close()


def startup_cleanup() -> None:
    """Buang sisa rule NAT dari sesi sebelumnya (restart aplikasi mematikan
    semua sesi remote, jadi semua rule berpenanda tanpa baris DB = sampah)."""
    db = SessionLocal()
    try:
        routers = db.query(Router).all()
        hidup = {(r.router_id, _kom_onu(r.onu_ip, r.onu_port))
                 for r in db.query(OnuNat).all()}
    finally:
        db.close()
    for router in routers:
        if is_private_ip(router.host):
            continue
        try:
            api = make_client(router)
            with api:
                for n in api._run("/ip/firewall/nat/print"):
                    kom = n.get("comment", "") or ""
                    if kom.startswith(KOMENTAR) and (router.id, kom) not in hidup:
                        if n.get(".id"):
                            api._run("/ip/firewall/nat/remove",
                                     _w(".id", n[".id"]))
        except Exception:  # noqa: BLE001
            continue


async def sweep_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, sweep_once)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception:  # noqa: BLE001 - sweep jangan sampai mematikan loop
            pass
