"""Adapter Mikrotik lewat RouterOS API (protokol di app/routeros.py).

Cara pakainya sinkron (blocking), jadi selalu dijalankan lewat
padanan `to_thread(...)` (lihat app/kompat.py) oleh app/services.py
supaya tidak memblokir event loop - kompatibel sampai Python 3.8.
Koneksi disimpan (persistent) dan otomatis nyambung ulang kalau putus.
"""
from __future__ import annotations

import threading
import time

from ..routeros import (  # protokol RouterOS API buatan sendiri (tanpa dependensi)
    ConnectionClosed,
    FatalError,
    LibRouterosError,
    TrapError,
    connect,
)

# kesalahan yang artinya "koneksi ke mikrotik bermasalah" -> layak dicoba sambung ulang
KONEKSI_RUSAK = (ConnectionClosed, FatalError, OSError)

from ..config import settings
from .base import RouterClient, Secret


def _w(key: str, value: str = "") -> str:
    """Susun satu 'word' protokol API.

    Atribut biasa: '=key=value'. Query (syarat print): '?key=value' - TANPA
    '=' di depan; salah format membuat mikrotik menjawab 'unknown parameter'.
    """
    if key.startswith("?"):
        return f"{key}={value}"
    return f"={key}" if value == "" else f"={key}={value}"


_PENOLAKAN = ("permission", "no such item", "not allowed", "cannot ", "forbidden")


def _jelaskan(exc: Exception, cmd: str, words=()) -> None:
    """Naikkan galat dengan keterangan yang bisa ditindaklanjuti teknisi.

    RouterOS menjawab 'failure: no permissions...' untuk perintah tulis yang
    ditolak hak, dan 'unknown parameter' untuk atribut yang tidak dikenal.
    Keduanya sering tampil sama saja di log, jadi dibedakan di sini.
    """
    teks = str(exc)
    rendah = teks.lower()
    if "unknown parameter" in rendah:
        attrs = [w[1:].split("=", 1)[0] for w in words if w.startswith("=") and not w.startswith("=?")]
        raise LibRouterosError(
            f"{teks} - perintah {cmd} ditolak mikrotik pada atribut "
            f"{attrs or '(tanpa atribut)'}; coba RouterOS lebih baru atau buat rule manual"
        )
    if any(k in rendah for k in _PENOLAKAN):
        raise LibRouterosError(
            f"{teks} - user mikrotik tidak punya hak write. Jalankan di mikrotik: "
            f"/user group set api policy=read,write,api,test"
        )
    raise LibRouterosError(f"{teks} - perintah {cmd} gagal")


class RouterOSAPI(RouterClient):
    backend = "api"

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int | None = None,
        timeout: float | None = None,
        use_ssl: bool | None = None,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port or settings.api_port
        self.timeout = timeout or settings.api_timeout
        # None = otomatis: TLS kalau port 8729 (api-ssl)
        self.use_ssl = use_ssl
        self._api = None
        self._lock = threading.Lock()
        self._last_error = ""

    # ------------------------------------------------------------ koneksi
    def _connect(self):
        return connect(
            host=self.host,
            username=self.username,
            password=self.password,
            port=self.port,
            timeout=self.timeout,
            use_ssl=self.use_ssl,
        )

    def _ensure(self):
        if self._api is None:
            self._api = self._connect()
        return self._api

    def _close_unlocked(self) -> None:
        """Tutup koneksi. HANYA boleh dipanggil saat self._lock sudah dipegang."""
        if self._api is not None:
            try:
                self._api.close()
            except Exception:  # noqa: BLE001
                pass
            self._api = None

    def _run(self, cmd: str, *words: str) -> list[dict]:
        """Jalankan satu perintah API dengan retry 1x kalau koneksi putus."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    api = self._ensure()
                    return list(api.rawCmd(cmd, *words))
                except KONEKSI_RUSAK as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._close_unlocked()
                    if attempt == 2:
                        raise
                    time.sleep(0.4)
        return []  # pragma: no cover - tidak akan tercapai

    def check(self) -> tuple[bool, str]:
        try:
            rows = self._run("/system/resource/print")
            if rows:
                row = rows[0]
                return True, (
                    f"RouterOS {row.get('version', '?')} - "
                    f"{row.get('board-name', '?')} - "
                    f"uptime {row.get('uptime', '?')}"
                )
            return True, "terhubung"
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            return False, self._last_error

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    # ------------------------------------------------------------ baca
    def ppp_active(self) -> list[dict]:
        return self._run("/ppp/active/print")

    def pppoe_servers(self) -> list[dict]:
        """/interface pppoe-server print -> sumber MAC bila /ppp active tak mengirimnya.

        Beberapa RouterOS tidak menyertakan properti mac-address di /ppp active,
        sementara kolom itu ada di pppoe-server. Gagal di sini tidak fatal.
        """
        try:
            return self._run("/interface/pppoe-server/print")
        except Exception:  # noqa: BLE001
            return []

    def arp_print(self) -> list[dict]:
        """/ip arp print -> IP:MAC semua perangkat. Gagal di sini tidak fatal."""
        try:
            return self._run("/ip/arp/print")
        except Exception:  # noqa: BLE001
            return []

    def ppp_profiles(self) -> list[str]:
        rows = self._run("/ppp/profile/print")
        return [r.get("name", "") for r in rows if r.get("name")]

    def add_secret(self, name: str, password: str, profile: str) -> None:
        self._run(
            "/ppp/secret/add",
            _w("name", name),
            _w("password", password),
            _w("profile", profile),
        )

    def ppp_secrets(self) -> list[Secret]:
        rows = self._run("/ppp/secret/print")
        return [
            Secret(
                name=r.get("name", ""),
                service=r.get("service", "pppoe"),
                disabled=str(r.get("disabled", "false")).lower() == "true",
                comment=r.get("comment", ""),
                profile=r.get("profile", ""),
                id=r.get(".id", ""),
            )
            for r in rows
            if r.get("name")
        ]

    def address_list_addresses(self, list_name: str) -> list[str]:
        rows = self._run("/ip/firewall/address-list/print", _w("?list", list_name))
        return [r.get("address", "") for r in rows if r.get("address")]

    def ping(self, address: str) -> tuple[int, float]:
        rows = self._run("/ping", _w("address", address), _w("count", "1"))
        for r in rows:
            if "received" in r:
                avg = r.get("avg-rtt") or r.get("last-rtt") or "0ms"
                try:
                    ms = float(str(avg).rstrip("us").rstrip("ms") or 0)
                except ValueError:
                    ms = 0.0
                return int(r.get("received", 0) or 0), ms
        return 0, 0.0

    # ------------------------------------------------------------ tulis
    def ensure_isolir_rule(self, list_name: str, action: str, comment: str) -> bool:
        existing = self._run("/ip/firewall/filter/print", _w("?comment", comment))
        if existing:
            return False
        # 'insert ... 0' menaruh rule di posisi teratas. Sebagian RouterOS menolak
        # argumen posisi itu, jadi kalau ditolak kita mundur ke 'add' biasa.
        try:
            self._run(
                "/ip/firewall/filter/insert",
                _w("0"),
                _w("chain", "forward"),
                _w("src-address-list", list_name),
                _w("action", action),
                _w("comment", comment),
            )
        except TrapError:
            self._run(
                "/ip/firewall/filter/add",
                _w("chain", "forward"),
                _w("src-address-list", list_name),
                _w("action", action),
                _w("comment", comment),
            )
        return True

    def add_address_list(
        self, list_name: str, address: str, comment: str, timeout: str = ""
    ) -> None:
        words = [_w("list", list_name), _w("address", address)]
        if comment:
            words.append(_w("comment", comment))
        if timeout:
            words.append(_w("timeout", timeout))
        try:
            self._run("/ip/firewall/address-list/add", *words)
        except TrapError as exc:
            pesan = str(exc).lower()
            if "already" in pesan:
                return  # sudah ada -> idempoten
            if "unknown parameter" in pesan:
                # mikrotik menolak salah satu atribut; coba versi paling sederhana
                self._run(
                    "/ip/firewall/address-list/add",
                    _w("list", list_name), _w("address", address),
                )
                return
            _jelaskan(exc, "/ip/firewall/address-list/add", words)

    def remove_address_list(self, list_name: str, address: str) -> int:
        rows = self._run(
            "/ip/firewall/address-list/print",
            _w("?list", list_name),
            _w("?address", address),
        )
        removed = 0
        for r in rows:
            rid = r.get(".id")
            if not rid:
                continue
            self._run("/ip/firewall/address-list/remove", _w(".id", rid))
            removed += 1
        return removed

    def set_secret_disabled(self, name: str, disabled: bool) -> bool:
        rows = self._run("/ppp/secret/print", _w("?name", name))
        if not rows:
            return False
        self._run(
            "/ppp/secret/set",
            _w(".id", rows[0].get(".id", "")),
            _w("disabled", "yes" if disabled else "no"),
        )
        return True

    def kick_active(self, name: str) -> int:
        rows = self._run("/ppp/active/print", _w("?name", name))
        kicked = 0
        for r in rows:
            rid = r.get(".id")
            if not rid:
                continue
            self._run("/ppp/active/remove", _w(".id", rid))
            kicked += 1
        return kicked

    # ---------------------- mode suspend (ISOLIR_MODE=suspend)
    def secret_profile(self, name: str) -> str | None:
        rows = self._run("/ppp/secret/print", _w("?name", name))
        if not rows:
            return None
        return str(rows[0].get("profile", "") or "")

    def set_secret_profile(self, name: str, profile: str) -> bool:
        rows = self._run("/ppp/secret/print", _w("?name", name))
        if not rows:
            return False
        self._run(
            "/ppp/secret/set",
            _w(".id", rows[0].get(".id", "")),
            _w("profile", profile),
        )
        return True

    def profile_exists(self, name: str) -> bool:
        """Cek /ppp profile by-name (lebih ringan dari print semua profil)."""
        if not name:
            return False
        return bool(self._run("/ppp/profile/print", _w("?name", name)))

    def ensure_suspend_profile(self, profile: str, pool: str, subnet: str) -> bool:
        """Pastikan pool IP + profil PPP suspend ada di router (idempoten)."""
        dibuat = False
        if not self._run("/ip/pool/print", _w("?name", pool)):
            dasar = subnet.split("/")[0].rsplit(".", 1)[0]
            self._run(
                "/ip/pool/add",
                _w("name", pool),
                _w("ranges", f"{dasar}.2-{dasar}.254"),
            )
            dibuat = True
        if not self._run("/ppp/profile/print", _w("?name", profile)):
            self._run(
                "/ppp/profile/add",
                _w("name", profile),
                _w("remote-address", pool),
            )
            dibuat = True
        return dibuat

    def ensure_suspend_rule(self, subnet: str, action: str, comment: str) -> bool:
        existing = self._run("/ip/firewall/filter/print", _w("?comment", comment))
        if existing:
            return False
        try:
            self._run(
                "/ip/firewall/filter/insert",
                _w("0"),
                _w("chain", "forward"),
                _w("src-address", subnet),
                _w("action", action),
                _w("comment", comment),
            )
        except TrapError:
            # RouterOS lama menolak argumen posisi; fallback ke add biasa.
            self._run(
                "/ip/firewall/filter/add",
                _w("chain", "forward"),
                _w("src-address", subnet),
                _w("action", action),
                _w("comment", comment),
            )
        return True

    def __enter__(self) -> "RouterOSAPI":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
