"""Router tiruan untuk mode DEMO (DEMO_MODE=true).

Dipakai supaya aplikasi bisa dijalankan, diuji, dan dipelajari tanpa mikrotik
sungguhan. Perilakunya meniru RouterOS: ada sesi yang naik-turun, uptime
bertambah, dan isolir benar-benar memutus sesi sampai di-'buka' lagi.
"""
from __future__ import annotations

import random
import threading
import time

from .base import RouterClient, Secret

_NAMA = [
    "budi-santoso", "siti-aminah", "agus-wijaya", "dewi-lestari", "eko-prasetyo",
    "rina-marlina", "hendra-gunawan", "fitri-handayani", "yusuf-rahman", "lina-kusuma",
    "taufik-hidayat", "nur-halimah", "dwi-cahyono", "mayang-sari", "bagus-setiawan",
    "indah-permata", "reza-fahlevi", "wati-susanti", "arif-rahmat", "tuti-wulandari",
]


def _mac(seed: int) -> str:
    rnd = random.Random(seed)
    return ":".join(f"{rnd.randint(0, 255):02X}" for _ in range(6))


class FakeRouter(RouterClient):
    backend = "demo"

    def __init__(self, host: str = "127.0.0.1", username: str = "demo", password: str = "",
                 port: int = 8728, jumlah: int = 0) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self._lock = threading.Lock()
        self._t0 = time.time()
        self._isolir: dict[str, str] = {}
        self._rules: list[dict] = []
        self._pools: dict[str, str] = {}      # mode suspend: pool -> ranges
        self._profiles: list[str] = ["default", "10M", "20M", "50M"]
        self._sessions: dict[str, dict] = {}
        self._secrets: dict[str, Secret] = {}
        rnd = random.Random(7)
        nama_pelanggan = list(_NAMA)
        if jumlah > len(nama_pelanggan):
            # tambahan pelanggan untuk uji skala besar
            nama_pelanggan += [f"pelanggan-{i:05d}" for i in range(len(_NAMA), jumlah)]
        for i, nama in enumerate(nama_pelanggan):
            self._secrets[nama] = Secret(
                name=nama, service="pppoe", disabled=False,
                comment=f"pelanggan {i + 1:03d}", profile="10M", id=f"*{i + 1:02X}",
            )
            online = rnd.random() > 0.15
            self._sessions[nama] = {
                "ip": f"10.20.{rnd.randint(1, 4)}.{rnd.randint(2, 250)}",
                "mac": _mac(i + 1),
                "since": time.time() - rnd.randint(60, 260000) if online else 0.0,
                "service": "pppoe",
                "caller": f"ether{i % 8 + 1}",
                "radius": "no",
            }

    # ------------------------------------------------------------ internal
    def _elapsed(self, name: str) -> str:
        since = self._sessions[name]["since"]
        if not since:
            return ""
        s = int(time.time() - since)
        w, rem = divmod(s, 604800)
        d, rem = divmod(rem, 86400)
        h, rem = divmod(rem, 3600)
        m, sec = divmod(rem, 60)
        out = ""
        if w:
            out += f"{w}w"
        if d:
            out += f"{d}d"
        if h:
            out += f"{h}h"
        if m or out:
            out += f"{m}m"
        return out + f"{sec}s"

    def _tick(self) -> None:
        """Simulasi perubahan kondisi jaringan (jarang, supaya realistis).

        Pelanggan yang IP-nya ada di address-list isolir tidak akan pernah bisa
        konek lagi sampai entri itu dihapus - sama seperti di mikrotik sungguhan.
        """
        rnd = random.Random()
        for nama, s in self._sessions.items():
            if s["ip"] in self._isolir:
                s["since"] = 0.0  # terisolir -> tetap terputus
                continue
            sec = self._secrets.get(nama)
            if sec and sec.profile == "SUSPENDED":
                s["since"] = 0.0  # mode suspend -> tidak boleh konek normal
                continue
            if s["since"] and rnd.random() < 0.004:
                s["since"] = 0.0  # putus sesaat
            elif not s["since"] and rnd.random() < 0.35:
                s["since"] = time.time()  # nyambung lagi

    # ------------------------------------------------------------ RouterClient
    def check(self) -> tuple[bool, str]:
        up = int(time.time() - self._t0)
        return True, f"RouterOS 7.15 (demo) - CCR2004 - uptime {up // 60}m{up % 60}s"

    def close(self) -> None:
        return None

    def ppp_active(self) -> list[dict]:
        with self._lock:
            self._tick()
            rows = []
            for i, (nama, s) in enumerate(self._sessions.items()):
                if not s["since"]:
                    continue
                rows.append(
                    {
                        ".id": f"*A{i:02X}",
                        "name": nama,
                        "address": s["ip"],
                        "remote-mac-address": s["mac"],
                        "service": s["service"],
                        "caller-id": s["caller"],
                        "uptime": self._elapsed(nama),
                        "radius": s["radius"],
                        "encoding": "",
                    }
                )
            return rows

    def ppp_secrets(self) -> list[Secret]:
        with self._lock:
            return list(self._secrets.values())

    def ppp_profiles(self) -> list[str]:
        return list(self._profiles)

    def arp_print(self) -> list[dict]:
        with self._lock:
            return [{"address": s["ip"], "mac-address": s["mac"]}
                    for s in self._sessions.values() if s["since"]]

    def add_secret(self, name: str, password: str, profile: str) -> None:
        with self._lock:
            if name in self._secrets:
                raise RuntimeError(f"failure: already have entry for {name}")
            self._secrets[name] = Secret(
                name=name, service="pppoe", disabled=False,
                comment="ditambah dari aplikasi", profile=profile or "default",
                id=f"*N{len(self._secrets) + 1:02X}",
            )

    def address_list_addresses(self, list_name: str) -> list[str]:
        with self._lock:
            return [ip for (lst, ip) in self._isolir.values() if lst == list_name]

    def ping(self, address: str) -> tuple[int, float]:
        with self._lock:
            for s in self._sessions.values():
                if s["ip"] == address:
                    if not s["since"]:
                        return 0, 0.0
                    return 1, round(random.Random().uniform(0.6, 4.5), 2)
        return 0, 0.0

    def ensure_isolir_rule(self, list_name: str, action: str, comment: str) -> bool:
        with self._lock:
            if any(r["comment"] == comment for r in self._rules):
                return False
            self._rules.insert(0, {"chain": "forward", "src-address-list": list_name,
                                   "action": action, "comment": comment})
            return True

    def add_address_list(self, list_name: str, address: str, comment: str, timeout: str = "") -> None:
        with self._lock:
            self._isolir[address] = (list_name, address)

    def remove_address_list(self, list_name: str, address: str) -> int:
        with self._lock:
            return 1 if self._isolir.pop(address, None) else 0

    def set_secret_disabled(self, name: str, disabled: bool) -> bool:
        with self._lock:
            sec = self._secrets.get(name)
            if not sec:
                return False
            sec.disabled = disabled
            return True

    def kick_active(self, name: str) -> int:
        with self._lock:
            s = self._sessions.get(name)
            if not s:
                return 0
            if not s["since"] or name in self._isolir:
                return 0
            s["since"] = 0.0
            return 1

    # ---------------------- mode suspend (ISOLIR_MODE=suspend)
    def secret_profile(self, name: str) -> str | None:
        with self._lock:
            sec = self._secrets.get(name)
            return sec.profile if sec else None

    def set_secret_profile(self, name: str, profile: str) -> bool:
        with self._lock:
            sec = self._secrets.get(name)
            if not sec:
                return False
            sec.profile = profile
            return True

    def ensure_suspend_profile(self, profile: str, pool: str, subnet: str) -> bool:
        with self._lock:
            dibuat = False
            if pool not in self._pools:
                dasar = subnet.split("/")[0].rsplit(".", 1)[0]
                self._pools[pool] = f"{dasar}.2-{dasar}.254"
                dibuat = True
            if profile not in self._profiles:
                self._profiles.append(profile)
                dibuat = True
            return dibuat

    def ensure_suspend_rule(self, subnet: str, action: str, comment: str) -> bool:
        with self._lock:
            if any(r["comment"] == comment for r in self._rules):
                return False
            self._rules.insert(0, {"chain": "forward", "src-address": subnet,
                                   "action": action, "comment": comment})
            return True
