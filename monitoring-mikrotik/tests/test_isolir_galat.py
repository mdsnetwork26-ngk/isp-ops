"""Uji perilaku Isolir pada mikrotik yang menolak perintah tulis.

Kasus nyata di lapangan: /ip firewall address-list add dibalas
'failure: unknown parameter ...' (atribut ditolak) atau 'failure: no
permissions...' (user tidak punya hak write). Keduanya dulu tampil sebagai
'unknown parameter' saja di layar, jadi dibedakan di sini.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.routeros import LibRouterosError, TrapError
from app.mikrotik.api import RouterOSAPI


class StubRouter(RouterOSAPI):
    """RouterOSAPI dengan lapisan transport tiruan.

    Hanya `_run` yang dipalsukan; logika add_address_list / ensure_isolir_rule
    yang diuji adalah kode sungguhan yang dipakai aplikasi.
    """

    def __init__(self, tolak):
        # sengaja tidak memanggil __init__: tidak membuka soket
        self.tolak = tolak          # callable(cmd, words) -> None | raise
        self.dipanggil = []         # [(cmd, words)]

    def _run(self, cmd, *words):
        self.dipanggil.append((cmd, tuple(words)))
        hasil = self.tolak(cmd, words)
        if isinstance(hasil, Exception):
            raise hasil
        return hasil if isinstance(hasil, list) else []


def test_atribut_ditolak_lalu_dicoba_tanpa_comment_dan_timeout():
    def tolak(cmd, words):
        if cmd != "/ip/firewall/address-list/add":
            return []
        if len(words) > 2:  # versi lengkap dengan comment
            return TrapError("failure: unknown parameter")
        return [{"!done": ""}]

    r = StubRouter(tolak)
    r.add_address_list("isolir", "10.1.1.99", "ISP-OPS isolir: budi")

    tambah = [w for c, w in r.dipanggil if c.endswith("address-list/add")]
    assert len(tambah) == 2
    assert len(tambah[0]) == 3 and len(tambah[1]) == 2  # mundur ke list+address saja


def test_tanpa_hak_write_pesan_menyebut_perintah_perbaikan():
    def tolak(cmd, words):
        if cmd.endswith("address-list/add"):
            return TrapError("failure: no permissions to call this command")
        return []

    r = StubRouter(tolak)
    with pytest.raises(LibRouterosError) as info:
        r.add_address_list("isolir", "10.1.1.99", "coba")
    assert "/user group set api policy=read,write,api,test" in str(info.value)


def test_ensure_rule_mundur_ke_add_bila_insert_ditolak():
    def tolak(cmd, words):
        if cmd == "/ip/firewall/filter/insert":
            return TrapError("failure: unknown parameter")
        if cmd == "/ip/firewall/filter/print":
            return []
        return [{"!done": ""}]

    r = StubRouter(tolak)
    assert r.ensure_isolir_rule("isolir", "drop", "ISP-OPS ISOLIR") is True
    cmds = [c for c, _ in r.dipanggil]
    assert "/ip/firewall/filter/add" in cmds
    rule = dict(w[1:].split("=", 1) for w in r.dipanggil[-1][1])
    assert rule == {"chain": "forward", "src-address-list": "isolir",
                    "action": "drop", "comment": "ISP-OPS ISOLIR"}
