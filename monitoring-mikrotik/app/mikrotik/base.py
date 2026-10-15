"""Antarmuka (interface) yang harus dipenuhi setiap adapter Mikrotik.

Adapter nyata: app.mikrotik.api.RouterOSAPI
Adapter demo  : app.mikrotik.fake.FakeRouter
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Secret:
    name: str
    service: str = "pppoe"
    disabled: bool = False
    comment: str = ""
    profile: str = ""
    id: str = ""


class RouterClient(ABC):
    """Operasi yang dibutuhkan aplikasi terhadap satu router Mikrotik."""

    #: nama yang dipakai untuk fallback SSH
    backend = "abstract"

    @abstractmethod
    def check(self) -> tuple[bool, str]:
        """Cek koneksi. Kembalikan (sukses, pesan)."""

    @abstractmethod
    def close(self) -> None:
        ...

    # ------------------------------ baca
    @abstractmethod
    def ppp_active(self) -> list[dict]:
        """/ppp active print -> daftar sesi aktif."""

    @abstractmethod
    def ppp_secrets(self) -> list[Secret]:
        """/ppp secret print -> daftar pelanggan terdaftar."""

    def pppoe_servers(self) -> list[dict]:
        """/interface pppoe-server print. Opsional; default kosong."""
        return []

    def arp_print(self) -> list[dict]:
        """/ip arp print. Sumber MAC paling andal (kunci: IP). Default kosong."""
        return []

    def ppp_profiles(self) -> list[str]:
        """/ppp profile print -> nama profile untuk pilihan form. Default kosong."""
        return []

    def add_secret(self, name: str, password: str, profile: str) -> None:
        """/ppp secret add. Default: tidak didukung."""
        raise NotImplementedError("adapter ini tidak mendukung tambah secret")

    @abstractmethod
    def address_list_addresses(self, list_name: str) -> list[str]:
        """IP yang ada di sebuah address-list (dipakai menandai status isolir)."""

    @abstractmethod
    def ping(self, address: str) -> tuple[int, float]:
        """Ping 1x. Kembalikan (paket diterima, rata-rata ms)."""

    # ------------------------------ tulis
    @abstractmethod
    def ensure_isolir_rule(self, list_name: str, action: str, comment: str) -> bool:
        """Pastikan rule firewall untuk isolir sudah ada. True bila dibuat baru."""

    @abstractmethod
    def add_address_list(self, list_name: str, address: str, comment: str, timeout: str = "") -> None:
        ...

    @abstractmethod
    def remove_address_list(self, list_name: str, address: str) -> int:
        """Hapus entri address-list. Kembalikan jumlah yang dihapus."""

    @abstractmethod
    def set_secret_disabled(self, name: str, disabled: bool) -> bool:
        ...

    @abstractmethod
    def kick_active(self, name: str) -> int:
        """Putus sesi /ppp active milik nama tsb. Kembalikan jumlah sesi diputus."""

    # ---------------------- mode suspend (ISOLIR_MODE=suspend)
    @abstractmethod
    def secret_profile(self, name: str) -> str | None:
        """Profil PPP sebuah secret saat ini. None bila secret tak ada di router."""

    @abstractmethod
    def set_secret_profile(self, name: str, profile: str) -> bool:
        """Pindahkan secret ke profil lain. False bila secret tak ada."""

    def profile_exists(self, name: str) -> bool:
        """True bila profil PPP bernama `name` ada di router.

        Mode suspend memakai profil yang SUDAH dibuat operator, jadi
        keberadaannya wajib dicek sebelum secret dipindah -- aplikasi tidak
        boleh membuat profil sendiri. Implementasi bawaan memakai
        ppp_profiles(); adapter boleh menimpanya dengan query yang lebih
        ringan. Nama dibandingkan persis (peka huruf besar/kecil).
        """
        if not name:
            return False
        try:
            return name in self.ppp_profiles()
        except Exception:  # noqa: BLE001 - adapter tak bisa membaca profil
            return False

    @abstractmethod
    def ensure_suspend_profile(self, profile: str, pool: str, subnet: str) -> bool:
        """USANG: dulu mode suspend membuat pool + profil sendiri.

        Tidak lagi dipanggil oleh layanan isolir (profil suspend dibuat
        manual oleh operator). Dipertahankan hanya untuk kompatibilitas.
        """

    @abstractmethod
    def ensure_suspend_rule(self, subnet: str, action: str, comment: str) -> bool:
        """USANG: dulu mode suspend membuat rule firewall subnet sendiri.

        Tidak lagi dipanggil oleh layanan isolir. Lihat ensure_suspend_profile.
        """
