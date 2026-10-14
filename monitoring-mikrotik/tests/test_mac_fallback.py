"""MAC pelanggan pada RouterOS yang tidak mengirim mac-address di /ppp active.

Di lapangan kolom MAC itu tetap ada di /interface pppoe-server print, jadi
diambil dari sana (sekali per 10 menit, bukan tiap putaran pantau).
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import services


class KlienTiruan:
    def __init__(self, baris):
        self.baris = baris
        self.dipanggil = 0

    def pppoe_servers(self):
        self.dipanggil += 1
        return self.baris


class RouterTiruan:
    host, port = "10.0.0.9", 8728


def _peta(klien, aktif):
    services._CACHE_MAC.clear()
    return asyncio.run(services._peta_mac(klien, RouterTiruan(), aktif, aktif))


def test_mac_diambil_dari_pppoe_server_bila_ppp_active_kosong():
    klien = KlienTiruan([{"user": "budi", "mac-address": "AA:BB:CC:11:22:33"}])
    peta = _peta(klien, [{"name": "budi", "address": "10.1.1.2"}])
    assert peta == {"budi": "AA:BB:CC:11:22:33"}
    assert klien.dipanggil == 1


def test_tidak_ambil_pppoe_server_kalau_ppp_active_sudah_ada_mac():
    klien = KlienTiruan([{"user": "budi", "mac-address": "AA:BB:CC:11:22:33"}])
    peta = _peta(klien, [{"name": "budi", "mac-address": "11:22:33:44:55:66"}])
    assert peta == {}
    assert klien.dipanggil == 0


def test_peta_dipakai_ulang_selama_ttl():
    klien = KlienTiruan([{"user": "budi", "mac-address": "AA:BB:CC:11:22:33"}])
    aktif = [{"name": "budi", "address": "10.1.1.2"}]
    services._CACHE_MAC.clear()
    asyncio.run(services._peta_mac(klien, RouterTiruan(), aktif, aktif))
    asyncio.run(services._peta_mac(klien, RouterTiruan(), aktif, aktif))
    asyncio.run(services._peta_mac(klien, RouterTiruan(), aktif, aktif))
    assert klien.dipanggil == 1  # 1500 baris tidak diambil ulang tiap 15 detik


class KlienDenganArp:
    """pppoe-server tanpa kolom user (kasus RouterOS tertentu) + tabel ARP."""

    def pppoe_servers(self):
        return [{"name": "pppoe-budi", "mac-address": "AA:BB:CC:11:22:33"}]  # tanpa user

    def arp_print(self):
        return [{"interface": "pppoe-budi", "address": "10.1.1.2",
                 "mac-address": "AA:BB:CC:11:22:33"}]


def test_mac_diambil_dari_tabel_arp_berdasarkan_ip():
    klien = KlienDenganArp()
    peta = _peta(klien, [{"name": "budi", "address": "10.1.1.2"}])
    # kunci IP tersedia walau pppoe-server tidak mengirim kolom user
    assert peta.get("10.1.1.2") == "AA:BB:CC:11:22:33"
