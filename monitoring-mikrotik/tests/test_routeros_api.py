"""Uji perintah yang dikirim ke mikrotik lewat protokol API.

Penting: tes ini mengimpor app.mikrotik.api sungguhan (bukan tiruan),
sehingga kesalahan nama exception/impor langsung ketahuan di sini.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.mikrotik.api import RouterOSAPI, _w


class StubApi:
    """Transport tiruan: merekam perintah, menjawab seperti mikrotik."""

    def __init__(self, fail_once: bool = False, filter_rules: list[dict] | None = None,
                 trap_on_add: bool = False):
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail_once = fail_once
        self.filter_rules = filter_rules or []
        self.trap_on_add = trap_on_add
        self._failed = False

    def rawCmd(self, cmd, *words):  # noqa: N802 - mengikuti nama yang dipakai adapter
        self.calls.append((cmd, words))
        if self.fail_once and not self._failed:
            self._failed = True
            from app.routeros import ConnectionClosed

            raise ConnectionClosed("koneksi tertutup")
        if cmd == "/ip/firewall/filter/print":
            yield from self.filter_rules
        elif cmd == "/ip/firewall/address-list/add" and self.trap_on_add:
            from app.routeros import TrapError

            raise TrapError("failure: already have such entry (6)")
        elif cmd == "/ip/firewall/address-list/print":
            yield {".id": "*AA", "address": "10.20.3.4", "list": "isolir"}
        elif cmd == "/ppp/secret/print":
            yield {".id": "*7", "name": "budi", "disabled": "false", "service": "pppoe"}
        elif cmd == "/ppp/active/print":
            yield {
                ".id": "*B1", "name": "budi", "address": "10.20.3.4",
                "mac-address": "AA:BB:CC:11:22:33", "uptime": "1d2h3m4s",
                "caller-id": "ether3", "service": "pppoe",
            }
        elif cmd == "/system/resource/print":
            yield {"version": "7.15.3", "board-name": "CCR2004", "uptime": "12d3h"}
        elif cmd == "/ping":
            yield {"sent": 1, "received": 1, "avg-rtt": "2.4ms"}

    def close(self):
        pass


def _client(fail_once: bool = False) -> tuple[RouterOSAPI, StubApi]:
    c = RouterOSAPI("10.0.0.1", "ispops", "rahasia", port=8728)
    stub = StubApi(fail_once=fail_once)
    c._api = stub
    c._connect = lambda: stub  # kalau putus, "sambung ulang" ke stub yang sama
    return c, stub


def test_word_disusun_benar():
    assert _w("list", "isolir") == "=list=isolir"
    assert _w("0") == "=0"
    assert _w(".id", "*7") == "=.id=*7"


def test_baca_data_mikrotik():
    c, _ = _client()
    ok, msg = c.check()
    assert ok and "7.15.3" in msg and "CCR2004" in msg
    assert c.ppp_active()[0]["address"] == "10.20.3.4"
    assert [s.name for s in c.ppp_secrets()] == ["budi"]
    assert c.address_list_addresses("isolir") == ["10.20.3.4"]
    assert c.ping("10.20.3.4") == (1, 2.4)


def test_urutan_perintah_isolir():
    c, stub = _client()
    assert c.ensure_isolir_rule("isolir", "drop", "ISP-OPS ISOLIR") is True
    c.add_address_list("isolir", "10.20.3.4", "ISP-OPS ISOLIR | budi")
    assert c.set_secret_disabled("budi", True) is True
    assert c.kick_active("budi") == 1

    cmds = [x[0] for x in stub.calls]
    assert cmds == [
        "/ip/firewall/filter/print",      # cek rule sudah ada belum
        "/ip/firewall/filter/insert",     # buat rule di posisi paling atas
        "/ip/firewall/address-list/add",  # masukkan IP pelanggan
        "/ppp/secret/print",
        "/ppp/secret/set",                # matikan secret
        "/ppp/active/print",
        "/ppp/active/remove",             # putus sesi sekarang juga
    ]
    rule = dict(w.lstrip("=").split("=", 1) for w in stub.calls[1][1] if "=" in w[1:])
    assert rule["chain"] == "forward"
    assert rule["src-address-list"] == "isolir"
    assert rule["action"] == "drop"
    assert rule["comment"] == "ISP-OPS ISOLIR"
    assert "=0" in stub.calls[1][1], "rule harus dipasang di posisi teratas"

    add = " ".join(stub.calls[2][1])
    assert "=list=isolir" in add and "=address=10.20.3.4" in add
    assert "=timeout=" not in add, "tanpa timeout berarti isolir permanen"

    set_secret = " ".join(stub.calls[4][1])
    assert "=disabled=yes" in set_secret


def test_rule_tidak_dibuat_dua_kali():
    """Rule firewall cukup dibuat sekali; isolir berikutnya tidak menambah rule baru."""
    c = RouterOSAPI("10.0.0.1", "u", "p")
    stub = StubApi(filter_rules=[{".id": "*1", "comment": "ISP-OPS ISOLIR"}])
    c._api = stub
    assert c.ensure_isolir_rule("isolir", "drop", "ISP-OPS ISOLIR") is False
    assert [x[0] for x in stub.calls] == ["/ip/firewall/filter/print"]


def test_address_list_sudah_ada_dianggap_berhasil():
    """Mikrotik menjawab trap kalau IP sudah ada di list - jangan jadikan error."""
    c = RouterOSAPI("10.0.0.1", "u", "p")
    c._api = StubApi(trap_on_add=True)
    c.add_address_list("isolir", "10.20.3.4", "x")  # tidak boleh melempar exception


def test_urutan_perintah_buka_isolir():
    c, stub = _client()
    assert c.remove_address_list("isolir", "10.20.3.4") == 1
    assert c.set_secret_disabled("budi", False) is True
    cmds = [x[0] for x in stub.calls]
    assert cmds == [
        "/ip/firewall/address-list/print",
        "/ip/firewall/address-list/remove",
        "/ppp/secret/print",
        "/ppp/secret/set",
    ]
    assert "=disabled=no" in " ".join(stub.calls[3][1])


def test_isolir_pakai_timeout_bila_diisi():
    from app.config import settings

    c, stub = _client()
    c.add_address_list("isolir", "10.20.3.4", "x", timeout="1h")
    assert "=timeout=1h" in " ".join(stub.calls[0][1])
    assert settings.isolir_timeout == ""  # default: permanen


def test_sambung_ulang_otomatis_saat_koneksi_putus():
    c, stub = _client(fail_once=True)
    assert [s.name for s in c.ppp_secrets()] == ["budi"]
    assert stub._failed is True
    assert len(stub.calls) >= 2, "perintah harus diulang setelah sambung ulang"


def test_tidak_deadlock_saat_koneksi_putus():
    """Regresi: dulu _run() memanggil close() sambil memegang lock -> menggantung.

    Tes ini akan hang (dan dibunuh watchdog) bila bug itu kembali muncul.
    """
    import threading

    hasil: dict = {}

    def kerja():
        c, _ = _client(fail_once=True)
        try:
            hasil["secrets"] = [s.name for s in c.ppp_secrets()]
            hasil["check"] = c.check()[0]
            c.close()  # memastikan close() tetap aman dipanggil dari luar
            hasil["selesai"] = True
        except BaseException as exc:  # noqa: BLE001
            hasil["error"] = repr(exc)

    t = threading.Thread(target=kerja, daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "terjadi deadlock pada penanganan koneksi putus"
    assert hasil.get("selesai") is True, hasil
    assert hasil.get("secrets") == ["budi"]
    assert hasil.get("check") is True
