"""Inti aplikasi: pool koneksi mikrotik, poller pantauan, dan aksi teknisi."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .config import settings
from .kompat import to_thread
from .database import SessionLocal
from .mikrotik import RouterClient, make_client
from .models import ActivityLog, Customer, Router
from .util import format_uptime, parse_uptime, pretty_mac

STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_ISOLATED = "isolated"
STATUS_DISABLED = "disabled"
STATUS_NO_SECRET = "no-secret"


_CACHE_MAC = {}      # (host, port) -> {"t": waktu, "map": {nama: mac}}
_MAC_TTL = 600.0     # detik; 1500 baris pppoe-server tidak perlu diambil tiap putaran


async def _peta_mac(client, router, sesi, aktif) -> dict:
    """Peta nama-pelanggan -> MAC dari /interface pppoe-server print.

    Sebagian RouterOS tidak mengirim properti mac-address di /ppp active, padahal
    kolom MAC itu ada di pppoe-server. Diambil sekali lalu dipakai selama 10 menit,
    dan hanya kalau memang ada sesi aktif yang MAC-nya kosong.
    """
    if aktif and all((r.get("mac-address") or "").strip() for r in aktif):
        return {}
    kunci = (router.host, router.port or 8728)
    sekarang = time.time()
    isi = _CACHE_MAC.get(kunci)
    if isi is None:
        isi = {"t": 0.0, "map": {}}
    if sekarang - isi["t"] < _MAC_TTL:
        return isi["map"]
    hilang = {r.get("name") for r in aktif if r.get("name") and not (r.get("mac-address") or "").strip()}
    if isi["map"] and hilang and not (hilang - set(isi["map"])):
        return isi["map"]  # yang belum diketahui hanya pelanggan yang memang tak punya entri
    try:
        baris = await to_thread(client.pppoe_servers)
    except Exception:  # noqa: BLE001
        return isi["map"]
    peta = {
        str(r.get("user") or "").strip(): str(r.get("mac-address") or "").strip()
        for r in baris
        if r.get("user") and r.get("mac-address")
    }
    # sumber kedua: tabel ARP mikrotik (kunci = IP). RouterOS versi mana pun
    # punya tabel ini, jadi MAC tetap terbaca meski /ppp active maupun
    # pppoe-server tidak mengirim kolom MAC.
    try:
        arp = await to_thread(client.arp_print)
    except Exception:  # noqa: BLE001
        arp = []
    for r in arp:
        ip = str(r.get("address") or "").strip()
        macv = str(r.get("mac-address") or "").strip()
        if ip and macv:
            peta.setdefault(ip, macv)
    isi["map"] = peta
    isi["t"] = sekarang
    _CACHE_MAC[kunci] = isi
    return isi["map"]


def _pastikan_utc(nilai):
    """SQLite mengembalikan datetime tanpa timezone; samakan agar bisa dibandingkan."""
    if nilai is None or nilai.tzinfo is not None:
        return nilai
    return nilai.replace(tzinfo=timezone.utc)


def norm_name(name: str | None) -> str:
    """Kunci pencocokan nama PPPoE <-> data pelanggan.

    Di lapangan nama sering beda tipis: 'budi-santoso' vs 'budi_santoso'
    vs 'Budi.Santoso'. Semua itu orang yang sama.
    """
    text = (name or "").strip().lower()
    for ch in ("-", "_", ".", " "):
        text = text.replace(ch, "")
    return text


# ============================================================================ sesi

@dataclass
class Session:
    """Satu baris di tabel monitoring."""

    customer_id: int | None = None
    name: str = ""
    full_name: str = ""
    phone: str = ""
    address: str = ""
    notes: str = ""
    ip: str = ""
    mac: str = ""
    uptime: str = "-"
    uptime_seconds: int = 0
    status: str = STATUS_DOWN
    caller_id: str = ""
    service: str = "pppoe"
    disabled: bool = False
    is_paid: bool | None = None
    onu_ip: str = ""
    onu_port: int = 80
    router_ip: str = ""
    router_port: int = 80
    onu_info: str = ""
    auto_isolir: bool = False
    auto_isolir_date: str = ""


@dataclass
class Snapshot:
    """Hasil pantauan satu router pada satu waktu."""

    router_id: int
    router_name: str
    host: str
    manage_ip: str = ""
    manage_port: int = 80
    online: bool = False
    message: str = ""
    ms: int = 0
    updated_at: float = 0.0
    sessions: list[Session] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.sessions is None:
            self.sessions = []

    @property
    def stats(self) -> dict:
        up = sum(1 for s in self.sessions if s.status == STATUS_UP)
        iso = sum(1 for s in self.sessions if s.status == STATUS_ISOLATED)
        down = len(self.sessions) - up - iso
        return {
            "total": len(self.sessions),
            "up": up,
            "isolated": iso,
            "down": down,
        }

    def to_dict(self) -> dict:
        return {
            "router_id": self.router_id,
            "router_name": self.router_name,
            "host": self.host,
            "manage_ip": self.manage_ip,
            "manage_port": self.manage_port,
            "online": self.online,
            "message": self.message,
            "ms": self.ms,
            "updated_at": self.updated_at,
            "stats": self.stats,
            "sessions": [s.__dict__ for s in self.sessions],
        }


def _iso_format(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""


# ============================================================================ pool

class RouterPool:
    """Menyimpan satu koneksi mikrotik per router + hasil pantauan terakhir."""

    def __init__(self) -> None:
        self._clients: dict[int, RouterClient] = {}
        self._snapshots: dict[int, Snapshot] = {}
        self._demo: RouterClient | None = None
        self._seq: dict[int, float] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}

    def lock(self, router_id: int) -> asyncio.Lock:
        """Satu router hanya boleh dipantau oleh satu proses pada satu waktu.

        Lock asyncio terikat ke event loop yang pertama memakainya, jadi cache
        diberi kunci per-loop: produksi (satu loop) tetap satu lock per router,
        dan lingkungan tes dengan banyak loop tidak saling menabrak.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - selalu dipanggil dari async
            loop = None
        kunci = (loop, router_id)
        lock = self._locks.get(kunci)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[kunci] = lock
        return lock

    def set_demo(self, client: RouterClient | None) -> None:
        self._demo = client

    def client_for(self, router: Router) -> RouterClient:
        if router.host == "demo" and self._demo is not None:
            return self._demo
        client = self._clients.get(router.id)
        if client is None:
            backend = "ssh" if settings.ssh_fallback else "api"
            client = make_client(router, backend=backend)
            self._clients[router.id] = client
        return client

    def drop_client(self, router_id: int) -> None:
        client = self._clients.pop(router_id, None)
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self._snapshots.pop(router_id, None)
        self._seq.pop(router_id, None)

    def snapshot(self, router_id: int) -> Snapshot | None:
        return self._snapshots.get(router_id)

    def snapshots(self) -> list[Snapshot]:
        return list(self._snapshots.values())

    def put(self, snap: Snapshot) -> None:
        """Simpan hasil pantauan. Snapshot yang lebih basi tidak boleh menimpa
        data yang lebih baru (poller latar bisa selesai belakangan)."""
        if snap.router_id in self._seq and snap.updated_at < self._seq[snap.router_id]:
            return
        self._seq[snap.router_id] = snap.updated_at
        self._snapshots[snap.router_id] = snap

    async def close_all(self) -> None:
        for client in list(self._clients.values()):
            try:
                await to_thread(client.close)
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()


pool = RouterPool()


# ============================================================================ data pantauan

async def build_snapshot(router: Router, db: Session) -> Snapshot:
    """Ambil /ppp active + /ppp secret dari mikrotik, gabungkan dengan data DB."""
    from .billing import unpaid_map

    snap = Snapshot(
        router_id=router.id,
        router_name=router.name,
        host=router.host,
        manage_ip=router.manage_ip or router.host,
        manage_port=settings.manage_port,
    )
    client = pool.client_for(router)
    start = time.perf_counter()
    try:
        active, secrets, isolir_ips = await asyncio.gather(
            to_thread(client.ppp_active),
            to_thread(client.ppp_secrets),
            to_thread(client.address_list_addresses, settings.isolir_list),
        )
        snap.online = True
        snap.message = "terhubung"
    except Exception as exc:  # noqa: BLE001
        snap.online = False
        snap.message = f"gagal terhubung: {type(exc).__name__}: {exc}"
        snap.updated_at = time.time()
        return snap

    snap.ms = int((time.perf_counter() - start) * 1000)
    isolir_set = {ip.strip() for ip in isolir_ips}
    mac_pppoe = await _peta_mac(client, router, active, active)

    customers = (
        db.query(Customer).filter(Customer.router_id == router.id).all()
    )
    by_name = {norm_name(c.name): c for c in customers}
    paid_status = await to_thread(unpaid_map)

    active_by_name: dict[str, dict] = {}
    for row in active:
        nama = (row.get("name") or "").strip()
        if nama:
            active_by_name[norm_name(nama)] = row

    now = datetime.now(timezone.utc)
    berubah = False
    rows: list[Session] = []
    for secret in secrets:
        key = norm_name(secret.name)
        row = active_by_name.get(key)
        cust = by_name.get(key)
        ip = (row or {}).get("address", "") or (cust.ip if cust else "")
        mac = ((row or {}).get("mac-address") or (row or {}).get("remote-mac-address") or "") or (cust.mac if cust else "")
        if not mac and row:
            mac = (mac_pppoe.get((row.get("name") or "").strip(), "")
                   or mac_pppoe.get((row.get("address") or "").strip(), ""))
        uptime_raw = (row or {}).get("uptime", "")
        secs = parse_uptime(uptime_raw)

        if cust and cust.isolated or ip in isolir_set:
            status = STATUS_ISOLATED
        elif secret.disabled:
            status = STATUS_DISABLED
        elif row:
            status = STATUS_UP
        else:
            status = STATUS_DOWN

        billing_code = cust.billing_code if cust else ""
        is_paid: bool | None = None
        if billing_code and billing_code in paid_status:
            is_paid = False
        elif cust and cust.is_paid is not None:
            is_paid = cust.is_paid

        sess = Session(
            customer_id=cust.id if cust else None,
            name=secret.name,
            full_name=cust.full_name if cust else "",
            phone=cust.phone if cust else "",
            address=cust.address if cust else "",
            notes=cust.notes if cust else "",
            ip=ip,
            mac=pretty_mac(mac),
            uptime=format_uptime(secs),
            uptime_seconds=secs,
            status=status,
            caller_id=(row or {}).get("caller-id", ""),
            service=secret.service or "pppoe",
            disabled=secret.disabled,
            is_paid=is_paid,
            onu_ip=cust.onu_ip if cust else "",
            onu_port=(cust.onu_port or settings.onu_default_port) if cust else settings.onu_default_port,
            router_ip=(cust.router_ip or router.manage_ip or router.host) if cust else (router.manage_ip or router.host),
            router_port=(cust.router_port or settings.manage_port) if cust else settings.manage_port,
            onu_info=cust.onu_info if cust else "",
            auto_isolir=bool(cust.auto_isolir) if cust else False,
            auto_isolir_date=_tanggal_iso(cust.auto_isolir_date) if cust else "",
        )
        rows.append(sess)

        # perbarui cache di DB (dipakai saat pelanggan offline & saat isolir)
        if cust is not None:
            changed = False
            if ip and cust.ip != ip:
                cust.ip, changed = ip, True
            if mac and cust.mac != mac:
                cust.mac, changed = mac, True
            if status == STATUS_UP:
                # last_seen cukup diperbarui kalau beda lebih dari 1 menit,
                # supaya 1500 baris tidak ditulis ulang tiap putaran pantau
                lama = _pastikan_utc(cust.last_seen)
                if lama is None or (now - lama).total_seconds() > 60:
                    cust.last_seen = now.replace(tzinfo=None)  # simpan UTC tanpa tz
                    changed = True
            if changed:
                db.add(cust)
                berubah = True

    # sesi aktif yang tidak ada di /ppp secret (mis. PPP lain)
    known = {norm_name(s.name) for s in rows}
    for nama, row in active_by_name.items():
        if nama in known:
            continue
        ip = row.get("address", "")
        mac = row.get("mac-address") or row.get("remote-mac-address") or ""
        if not mac:
            mac = mac_pppoe.get(nama, "") or mac_pppoe.get(str(ip).strip(), "")
        secs = parse_uptime(row.get("uptime", ""))
        cust = by_name.get(nama)
        rows.append(
            Session(
                customer_id=cust.id if cust else None,
                name=row.get("name", nama),
                full_name=cust.full_name if cust else "",
                phone=cust.phone if cust else "",
                address=cust.address if cust else "",
                notes=cust.notes if cust else "",
                ip=ip,
                mac=pretty_mac(mac),
                uptime=format_uptime(secs),
                uptime_seconds=secs,
                status=STATUS_ISOLATED if ip in isolir_set else STATUS_UP,
                caller_id=row.get("caller-id", ""),
                service=row.get("service", "pppoe"),
                disabled=False,
                is_paid=None,
                onu_ip=cust.onu_ip if cust else "",
                onu_port=(cust.onu_port or settings.onu_default_port) if cust else settings.onu_default_port,
                router_ip=(cust.router_ip or router.manage_ip or router.host) if cust else (router.manage_ip or router.host),
                router_port=(cust.router_port or settings.manage_port) if cust else settings.manage_port,
                onu_info=cust.onu_info if cust else "",
                auto_isolir=bool(cust.auto_isolir) if cust else False,
                auto_isolir_date=_tanggal_iso(cust.auto_isolir_date) if cust else "",
            )
        )

    rows.sort(key=lambda s: (s.status != STATUS_UP, s.name.lower()))
    snap.sessions = rows
    snap.updated_at = time.time()
    if berubah:  # hemat I/O: hanya tulis kalau benar-benar ada yang berubah
        db.commit()
    else:
        db.rollback()
    return snap


def _tanggal_iso(nilai) -> str:
    return nilai.isoformat() if nilai else ""


def _auto_isolir_jatuh_tempo(sesi) -> bool:
    """AUTO tanpa tanggal = langsung aktif. AUTO dengan tanggal = aktif
    setelah tanggal + masa tenggang (default 2 hari, AUTO_ISOLIR_GRACE_DAYS)."""
    if not sesi.auto_isolir:
        return False
    if not sesi.auto_isolir_date:
        return True
    from datetime import date as _date, timedelta

    try:
        d = _date.fromisoformat(sesi.auto_isolir_date)
    except ValueError:
        return True
    return _date.today() >= d + timedelta(days=settings.auto_isolir_grace_days)


async def _tegakkan_auto_isolir(snap: Snapshot | None, router_id: int) -> None:
    """Isolir pelanggan bertanda auto-isolir (diatur admin) begitu terlihat online.

    WAJIB dipanggil di LUAR pool.lock: isolate_customer memanggil refresh_router
    yang mengambil lock yang sama (lock asyncio tidak reentrant -> deadlock).
    Default semua pelanggan: isolir manual (tanda off)."""
    if snap is None:
        return
    for sess in snap.sessions:
        if (sess.status != STATUS_UP or not sess.customer_id
                or not sess.ip or not _auto_isolir_jatuh_tempo(sess)):
            continue
        try:
            await isolate_customer(
                actor="auto-isolir",
                reason="auto-isolir (ditandai admin)",
                customer_id=sess.customer_id,
                router_id=router_id,
            )
        except Exception:  # noqa: BLE001
            pass  # percobaan berikut akan mencoba lagi


async def refresh_router(router_id: int) -> Snapshot | None:
    """Pantau ulang satu router sekarang sekali (dipakai setelah aksi isolir/buka)."""
    db = SessionLocal()
    try:
        router = db.get(Router, router_id)
        if router is None:
            return None
        async with pool.lock(router_id):
            snap = await build_snapshot(router, db)
            pool.put(snap)
        await _tegakkan_auto_isolir(snap, router_id)
        return pool.snapshot(router_id) or snap
    finally:
        db.close()


async def refresh_all() -> list[Snapshot]:
    db = SessionLocal()
    try:
        routers = db.query(Router).filter(Router.enabled == True).all()  # noqa: E712
        out: list[Snapshot] = []
        for router in routers:
            async with pool.lock(router.id):
                snap = await build_snapshot(router, db)
                pool.put(snap)
            await _tegakkan_auto_isolir(snap, router.id)
            out.append(pool.snapshot(router.id) or snap)
        return out
    finally:
        db.close()


async def poller_loop() -> None:
    """Loop pantauan latar: refresh tiap router sesuai interval masing-masing."""
    last: dict[int, float] = {}
    while True:
        try:
            db = SessionLocal()
            routers = db.query(Router).filter(Router.enabled == True).all()  # noqa: E712
            db.close()
            now = time.time()
            for router in routers:
                interval = max(2.0, float(router.poll_interval or settings.poll_interval))
                if now - last.get(router.id, 0) < interval:
                    continue
                snap_db = SessionLocal()
                snap = None
                try:
                    async with pool.lock(router.id):
                        snap = await build_snapshot(router, snap_db)
                        pool.put(snap)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    snap_db.close()
                await _tegakkan_auto_isolir(snap, router.id)
                last[router.id] = time.time()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1.0)


# ============================================================================ aksi teknisi

def find_customer(db: Session, customer_id: int | None, name: str | None, router_id: int | None):
    q = db.query(Customer)
    if customer_id:
        return q.filter(Customer.id == customer_id).first()
    if name:
        q = q.filter(Customer.name == name)
        if router_id:
            q = q.filter(Customer.router_id == router_id)
        return q.first()
    return None


_find_customer = find_customer  # alias lama


async def add_ppp_secret(
    *, actor: str, router_id: int, name: str, password: str, profile: str,
) -> dict:
    """Tambah user PPPoE (/ppp secret add) di mikrotik + catat sebagai pelanggan baru."""
    db = SessionLocal()
    try:
        router = db.get(Router, router_id)
        if router is None:
            return {"ok": False, "error": "router tidak ditemukan"}
        if not name.strip() or not password:
            return {"ok": False, "error": "nama dan password wajib diisi"}
        client = pool.client_for(router)
        try:
            await to_thread(client.add_secret, name.strip(), password, profile or "default")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "already" in msg.lower():
                return {"ok": False, "error": f"nama '{name}' sudah ada di mikrotik"}
            return {"ok": False, "error": f"gagal perintah mikrotik: {msg}"}
        cust = (
            db.query(Customer)
            .filter(Customer.router_id == router_id, Customer.name == name.strip())
            .first()
        )
        if cust is None:
            cust = Customer(name=name.strip(), router_id=router_id, source="manual")
            db.add(cust)
        elif cust.source != "manual":
            cust.source = "manual"
        db.commit()
        log_action(
            db, actor=actor, action="pppoe-add",
            customer_name=cust.name, router_name=router.name,
            detail=f"profile {profile or 'default'}",
        )
        db.commit()
        return {"ok": True, "id": cust.id, "name": cust.name}
    finally:
        db.close()


def log_action(
    db: Session, *, actor: str, action: str, customer_name: str,
    router_name: str, detail: str, success: bool = True,
) -> ActivityLog:
    entry = ActivityLog(
        actor=actor or "-",
        action=action,
        customer_name=customer_name,
        router_name=router_name,
        detail=detail,
        success=success,
    )
    db.add(entry)
    return entry


async def _wajib_profil_ada(client, profil: str, peran: str) -> None:
    """Gagalkan aksi bila profil tujuan tidak ada di router.

    Profil suspend (dan profil asal saat Buka) adalah buatan operator di
    MikroTik -- aplikasi tidak boleh membuat profil sendiri diam-diam,
    karena isi pool / rate-limit / redirect halaman isolirnya hanya operator
    yang tahu. Pesan galatnya menyebut profil yang tersedia supaya teknisi
    langsung tahu harus mengoreksi SUSPEND_PROFILE atau membuat profilnya.
    """
    if not profil:
        raise RuntimeError(f"nama profil {peran} kosong -- periksa konfigurasi .env")
    if await to_thread(client.profile_exists, profil):
        return
    try:
        tersedia = [p for p in await to_thread(client.ppp_profiles) if p]
    except Exception:  # noqa: BLE001 - daftar hanya pelengkap pesan galat
        tersedia = []
    daftar = ", ".join(tersedia) if tersedia else "(tidak terbaca)"
    raise RuntimeError(
        f"profil {peran} '{profil}' tidak ditemukan di router "
        f"(profil tersedia: {daftar}). Buat dulu profil '{profil}' di "
        f"MikroTik lewat /ppp profile, atau sesuaikan namanya di .env "
        f"(SUSPEND_PROFILE=... untuk profil suspend). Tidak ada yang diubah."
    )


async def _set_dan_verifikasi_profil(client, nama: str, profil: str, peran: str) -> None:
    """Pindah profil secret lalu baca ulang untuk memastikan benar-benar pindah.

    RouterOS menerima /ppp secret set tanpa memastikan sesi aktif ikut
    berubah -- profil baru hanya berlaku saat login berikutnya (itulah
    sebabnya pemanggil WAJIB menghapus sesi aktif sesudah langkah ini).
    """
    if not await to_thread(client.set_secret_profile, nama, profil):
        raise RuntimeError(
            f"gagal memindah secret '{nama}' ke profil {peran} '{profil}': "
            f"secret PPP tidak ditemukan di router"
        )
    terpasang = await to_thread(client.secret_profile, nama)
    # Adapter tertentu (SSH) bisa gagal membaca ulang kolom profil; hanya
    # nilai terbaca yang jelas-jelas berbeda yang dianggap gagal.
    if terpasang and terpasang.strip() != profil:
        raise RuntimeError(
            f"verifikasi gagal: profil secret '{nama}' masih "
            f"'{terpasang.strip()}', bukan '{profil}' -- tidak ada sesi "
            f"yang diputus, periksa hak tulis user API di MikroTik"
        )


async def _isolir_suspend(client, cust) -> list[str]:
    """Isolir mode suspend: masukkan klien ke profil suspend buatan operator.

    Alur (persis permintaan operator):
      1. Pastikan profil suspend (SUSPEND_PROFILE) SUDAH ada di router.
         Aplikasi tidak membuat pool/profil/rule apa pun.
      2. Baca profil secret saat ini; simpan sebagai profil asal di
         customers.ppp_profile (tidak ditimpa bila sudah tercatat, supaya
         isolir berulang tidak menyimpan 'profil suspend' sebagai asal).
      3. Pastikan secret aktif (tidak di-disable) agar login PPPoE ke
         profil suspend berhasil dan tidak membanjiri log auth gagal.
      4. Pindah secret ke profil suspend + verifikasi baca-ulang.
         Sesi aktif dihapus oleh pemanggil tepat sesudah ini, supaya klien
         login ulang dan berpindah ke profil suspend.
    Profil asal hanya ditulis ke `cust` SETELAH pemindahan terverifikasi,
    jadi isolir yang gagal tidak meninggalkan catatan asal yang keliru.
    """
    detail: list[str] = []
    suspend = (settings.suspend_profile or "").strip()
    await _wajib_profil_ada(client, suspend, "suspend")

    profil_kini = await to_thread(client.secret_profile, cust.name)
    if profil_kini is None:
        raise RuntimeError("secret PPP tidak ditemukan di router")
    profil_kini = profil_kini.strip()

    tersimpan = (cust.ppp_profile or "").strip()
    # Profil asal = yang tercatat sebelumnya (isolir berulang) atau profil
    # saat ini. Profil kosong di RouterOS berarti memakai 'default'.
    profil_asal = tersimpan or (profil_kini if profil_kini != suspend else "")
    if not profil_asal and not profil_kini:
        profil_asal = "default"

    if await to_thread(client.set_secret_disabled, cust.name, False):
        detail.append("PPP secret dipastikan aktif (tidak di-disable)")

    if profil_kini == suspend:
        detail.append(f"secret sudah berada di profil suspend '{suspend}'")
        if not tersimpan:
            detail.append(
                "PERINGATAN: profil asal tidak tercatat di database -- "
                "saat Buka nanti klien dikembalikan ke profil "
                f"'{settings.suspend_fallback_profile}'"
            )
    else:
        await _set_dan_verifikasi_profil(client, cust.name, suspend, "suspend")
        detail.append(
            f"profil asal '{profil_asal}' tersimpan; secret dipindah ke "
            f"profil suspend '{suspend}'"
        )

    if profil_asal and profil_asal != suspend:
        cust.ppp_profile = profil_asal  # aman ditulis: pemindahan terverifikasi
    return detail


async def _buka_suspend(client, cust) -> list[str]:
    """Buka isolir mode suspend: kembalikan klien ke profil asalnya.

    Alur kebalikan dari _isolir_suspend:
      1. Tentukan profil asal dari customers.ppp_profile. Bila tidak
         tercatat: pakai profil saat ini bila ia sudah bukan suspend
         (mis. sudah dipulihkan manual di WinBox); jalan terakhirnya
         SUSPEND_FALLBACK_PROFILE, selalu sebagai peringatan di log.
      2. Pastikan profil asal itu masih ada di router -- bila sudah
         dihapus/berganti nama, Buka DIBATALKAN dan catatan profil asal
         TIDAK dihapus, supaya bisa dicoba lagi setelah profil dibuat.
      3. Aktifkan secret, pindah profil, verifikasi. Sesi aktif dihapus
         oleh pemanggil tepat sesudah ini, supaya klien login ulang dan
         lepas dari profil suspend.
    Fungsi ini tidak mengubah `cust`; penghapusan customers.ppp_profile
    dilakukan pemanggil hanya setelah seluruh langkah berhasil.
    """
    detail: list[str] = []
    suspend = (settings.suspend_profile or "").strip()

    profil_kini = await to_thread(client.secret_profile, cust.name)
    if profil_kini is None:
        raise RuntimeError("secret PPP tidak ditemukan di router")
    profil_kini = profil_kini.strip()

    tersimpan = (cust.ppp_profile or "").strip()
    if tersimpan:
        target = tersimpan
    elif profil_kini and profil_kini != suspend:
        target = profil_kini
        detail.append(
            f"profil asal tidak tercatat; secret sudah di profil '{target}'"
        )
    else:
        target = (settings.suspend_fallback_profile or "default").strip()
        detail.append(
            "PERINGATAN: profil asal tidak tercatat di database -- "
            f"klien dikembalikan ke profil '{target}'"
        )

    if await to_thread(client.set_secret_disabled, cust.name, False):
        detail.append("PPP secret diaktifkan kembali")

    if profil_kini == target:
        detail.append(f"profil secret sudah di profil asal '{target}'")
    else:
        await _wajib_profil_ada(client, target, "asal pelanggan")
        await _set_dan_verifikasi_profil(client, cust.name, target, "asal pelanggan")
        detail.append(f"profil dikembalikan ke profil asal '{target}'")
    return detail


async def isolate_customer(
    *,
    actor: str,
    reason: str = "belum bayar",
    customer_id: int | None = None,
    name: str | None = None,
    router_id: int | None = None,
) -> dict:
    """Isolir pelanggan.

    Mode "disable" (klasik): IP masuk address-list + drop forward, secret
    di-disable, sesi diputus. Mode "suspend" (ISOLIR_MODE=suspend): profil
    asal pelanggan disimpan, secret dipindah ke profil suspend buatan
    operator, lalu sesi PPPoE aktif DIHAPUS supaya klien login ulang dan
    berpindah ke profil suspend -- klien offline pun tetap bisa diisolir
    karena blok menempel di akun, bukan IP.
    """
    db = SessionLocal()
    try:
        cust = _find_customer(db, customer_id, name, router_id)
        if cust is None and name and router_id:
            # sesi terlihat di mikrotik tapi belum ada barisnya (mis. belum sinkron):
            # buat otomatis supaya Isolir/Buka/Ping selalu bisa bekerja
            cust = Customer(name=name, router_id=router_id, source="auto")
            db.add(cust)
            db.commit()
            db.refresh(cust)
        if cust is None:
            return {"ok": False, "error": "data pelanggan tidak ditemukan di database"}
        router = db.get(Router, cust.router_id)
        if router is None:
            return {"ok": False, "error": "router pelanggan tidak ditemukan"}

        snap = pool.snapshot(router.id)
        live = ""
        if snap:
            for s in snap.sessions:
                if norm_name(s.name) == norm_name(cust.name):
                    live = s.ip
                    break
        ip = live or cust.ip
        ip_asal = ""
        if not ip and cust.onu_ip:
            # pelanggan sedang offline: pakai IP terakhir ONU (satu jaringan manajemen)
            from .util import same_subnet_ips

            kandidat = same_subnet_ips(cust.onu_ip, 24)
            ip = kandidat[-1] if kandidat else ""
            if ip:
                ip_asal = f" (IP turunan dari IP ONU {cust.onu_ip})"
        suspend = settings.isolir_mode == "suspend"
        if not ip and not suspend:
            log_action(
                db, actor=actor, action="isolate", customer_name=cust.name,
                router_name=router.name, detail="gagal: IP pelanggan kosong (sedang offline)",
                success=False,
            )
            db.commit()
            return {"ok": False, "error": "IP pelanggan belum diketahui (pelanggan sedang offline)"}

        client = pool.client_for(router)
        detail: list[str] = []
        try:
            if suspend:
                # Blok menempel di akun: klien offline pun bisa diisolir.
                # Aplikasi tidak menyentuh address-list / firewall / pool /
                # profil sama sekali di mode ini -- profil suspend sepenuhnya
                # milik operator.
                detail += await _isolir_suspend(client, cust)
                # WAJIB di mode suspend (tidak tergantung ISOLIR_KICK_ACTIVE):
                # tanpa menghapus sesi aktif, klien tetap berjalan di profil
                # lama sampai putus sendiri dan isolir terasa "tidak bekerja".
                try:
                    kicked = await to_thread(client.kick_active, cust.name)
                except Exception as exc:  # noqa: BLE001
                    # Profil sudah terverifikasi pindah; gagal menghapus sesi
                    # jangan membatalkan isolir -- catat saja dengan jelas.
                    detail.append(
                        f"PERINGATAN: gagal menghapus sesi PPPoE aktif ({exc}) "
                        f"-- profil suspend berlaku saat klien login berikutnya"
                    )
                else:
                    if kicked:
                        detail.append(
                            f"{kicked} sesi PPPoE aktif dihapus -- klien login "
                            f"ulang ke profil suspend '{settings.suspend_profile}'"
                        )
                    else:
                        detail.append(
                            "tidak ada sesi PPPoE aktif yang dihapus (klien "
                            "offline -- profil suspend berlaku saat login "
                            "berikutnya)"
                        )
            else:
                dibuat = await to_thread(
                    client.ensure_isolir_rule,
                    settings.isolir_list, settings.isolir_action, settings.isolir_rule_comment,
                )
                if dibuat:
                    detail.append(f"rule firewall '{settings.isolir_rule_comment}' dibuat")
                await to_thread(
                    client.add_address_list, settings.isolir_list, ip,
                    f"{settings.isolir_rule_comment} | {cust.name}", settings.isolir_timeout,
                )
                detail.append(f"{ip}{ip_asal} -> address-list '{settings.isolir_list}'")
                if settings.isolir_disable_secret:
                    if await to_thread(client.set_secret_disabled, cust.name, True):
                        detail.append("PPP secret di-disable")
                if settings.isolir_kick_active:
                    kicked = await to_thread(client.kick_active, cust.name)
                    detail.append(f"{kicked} sesi diputus")
        except Exception as exc:  # noqa: BLE001
            log_action(
                db, actor=actor, action="isolate", customer_name=cust.name,
                router_name=router.name, detail=f"gagal: {exc}", success=False,
            )
            db.commit()
            return {"ok": False, "error": f"gagal perintah mikrotik: {exc}"}

        cust.isolated = True
        cust.isolated_at = datetime.now(timezone.utc)
        cust.isolated_by = actor
        cust.isolated_reason = reason
        if ip:
            cust.ip = ip  # saat suspend-offline ip bisa kosong: pertahankan yang lama
        if "bayar" in reason.lower() or "nunggak" in reason.lower():
            cust.is_paid = False  # biar kolom "Bayar" ikut merah di layar
        db.add(cust)
        log_action(
            db, actor=actor, action="isolate", customer_name=cust.name,
            router_name=router.name, detail="; ".join(detail) + f" | alasan: {reason}",
        )
        db.commit()
        await refresh_router(router.id)
        return {"ok": True, "customer": cust.name, "ip": ip, "detail": "; ".join(detail)}
    finally:
        db.close()


async def open_customer(
    *,
    actor: str,
    customer_id: int | None = None,
    name: str | None = None,
    router_id: int | None = None,
) -> dict:
    """Buka isolir pelanggan.

    Mode "disable": hapus IP dari address-list + aktifkan secret lagi.
    Mode "suspend": secret dikembalikan ke profil asal yang tercatat, lalu
    sesi PPPoE aktif DIHAPUS supaya klien login ulang dan lepas dari profil
    suspend. Catatan profil asal hanya dihapus setelah pemulihan berhasil.
    """
    db = SessionLocal()
    try:
        cust = _find_customer(db, customer_id, name, router_id)
        if cust is None and name and router_id:
            # sesi terlihat di mikrotik tapi belum ada barisnya (mis. belum sinkron):
            # buat otomatis supaya Isolir/Buka/Ping selalu bisa bekerja
            cust = Customer(name=name, router_id=router_id, source="auto")
            db.add(cust)
            db.commit()
            db.refresh(cust)
        if cust is None:
            return {"ok": False, "error": "data pelanggan tidak ditemukan di database"}
        router = db.get(Router, cust.router_id)
        if router is None:
            return {"ok": False, "error": "router pelanggan tidak ditemukan"}

        snap = pool.snapshot(router.id)
        live = ""
        if snap:
            for s in snap.sessions:
                if norm_name(s.name) == norm_name(cust.name):
                    live = s.ip
                    break
        ip = live or cust.ip
        client = pool.client_for(router)
        detail: list[str] = []
        try:
            if settings.isolir_mode == "suspend":
                # Bersihkan sisa isolir cara lama (mode disable) bila ada:
                # pelanggan lama yang dulu masuk address-list harus lepas
                # juga, tapi di alur suspend murni langkah ini tidak
                # menghasilkan apa-apa dan tidak perlu memenuhi log.
                removed = 0
                for candidate in {c for c in (ip, cust.ip) if c}:
                    removed += await to_thread(
                        client.remove_address_list, settings.isolir_list, candidate
                    )
                if removed:
                    detail.append(
                        f"sisa isolir lama: {removed} entri address-list dihapus"
                    )
                # _buka_suspend juga mengaktifkan secret tanpa syarat:
                # menutup kasus isolir lama yang sempat dibuat saat masih
                # mode disable (secret sempat di-disable).
                detail += await _buka_suspend(client, cust)
                # WAJIB di mode suspend (tidak tergantung ISOLIR_KICK_ACTIVE):
                # sesi yang masih nyangkut di profil suspend tidak berganti
                # profil sebelum reconnect -- hapus supaya klien login ulang
                # dengan profil asal.
                try:
                    kicked = await to_thread(client.kick_active, cust.name)
                except Exception as exc:  # noqa: BLE001
                    detail.append(
                        f"PERINGATAN: gagal menghapus sesi PPPoE aktif ({exc}) "
                        f"-- profil asal berlaku saat klien login berikutnya"
                    )
                else:
                    if kicked:
                        detail.append(
                            f"{kicked} sesi PPPoE aktif dihapus -- klien login "
                            f"ulang dengan profil asal, lepas dari profil "
                            f"suspend '{settings.suspend_profile}'"
                        )
                    else:
                        detail.append(
                            "tidak ada sesi PPPoE aktif yang dihapus (klien "
                            "offline -- profil asal berlaku saat login "
                            "berikutnya)"
                        )
                # Hanya dihapus setelah profil terverifikasi pulih DAN sesi
                # sudah ditangani; bila langkah di atas gagal, catatan ini
                # tetap ada supaya tombol Buka bisa dicoba lagi.
                cust.ppp_profile = ""
            else:
                removed = 0
                for candidate in {c for c in (ip, cust.ip) if c}:
                    removed += await to_thread(
                        client.remove_address_list, settings.isolir_list, candidate
                    )
                detail.append(f"{removed} entri address-list dihapus")
                if settings.isolir_disable_secret:
                    if await to_thread(client.set_secret_disabled, cust.name, False):
                        detail.append("PPP secret diaktifkan kembali")
        except Exception as exc:  # noqa: BLE001
            log_action(
                db, actor=actor, action="open", customer_name=cust.name,
                router_name=router.name, detail=f"gagal: {exc}", success=False,
            )
            db.commit()
            return {"ok": False, "error": f"gagal perintah mikrotik: {exc}"}

        cust.isolated = False
        cust.isolated_at = None
        cust.isolated_by = ""
        cust.isolated_reason = ""
        cust.is_paid = True
        db.add(cust)
        log_action(
            db, actor=actor, action="open", customer_name=cust.name,
            router_name=router.name, detail="; ".join(detail),
        )
        db.commit()
        await refresh_router(router.id)
        return {"ok": True, "customer": cust.name, "ip": ip, "detail": "; ".join(detail)}
    finally:
        db.close()


def recent_logs(limit: int = 60) -> list[dict]:
    db = SessionLocal()
    try:
        rows = (
            db.query(ActivityLog)
            .order_by(ActivityLog.id.desc())
            .limit(limit)
            .all()
        )
        return [
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
    finally:
        db.close()


def iso_now() -> str:
    return _iso_format(time.time())
