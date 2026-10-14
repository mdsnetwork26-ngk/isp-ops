#!/usr/bin/env python3
"""Tes koneksi ke mikrotik dari server ini.

Pakai:
    python tes-koneksi.py 10.10.10.1 ispops 'password'
    python tes-koneksi.py 10.10.10.1 ispops 'password' --port 8729   # API-SSL
    python tes-koneksi.py 10.10.10.1 ispops 'password' --ssh        # pakai SSH

Keluarannya menjelaskan apa yang perlu diperbaiki kalau gagal.
"""
from __future__ import annotations

import argparse
import socket
import sys


def cek_port(host: str, port: int, timeout: float = 4) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "terbuka"
    except socket.timeout:
        return False, "timeout (kemungkinan diblokir firewall / IP tidak ada)"
    except ConnectionRefusedError:
        return False, "ditolak (service-nya mati di mikrotik)"
    except socket.gaierror as exc:
        return False, f"nama host tidak dikenal: {exc}"
    except OSError as exc:
        return False, str(exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("username")
    ap.add_argument("password")
    ap.add_argument("--port", type=int, default=8728)
    ap.add_argument("--ssh", action="store_true", help="pakai SSH (port 22), bukan API")
    args = ap.parse_args()

    port = 22 if args.ssh else args.port
    print(f"1) Cek TCP {args.host}:{port} ...")
    ok, pesan = cek_port(args.host, port)
    print(f"   {'OK' if ok else 'GAGAL'} - {pesan}")
    if not ok:
        print("\nYang perlu dilakukan di mikrotik:")
        if args.ssh:
            print("   /ip service enable ssh")
        else:
            print("   /ip service enable api        (port 8728)")
        print("   pastikan ada route dari server ini ke IP mikrotik")
        print("   cek /ip firewall filter tidak memblokir input dari IP server ini")
        return 1

    if args.ssh:
        try:
            from app.mikrotik.ssh import RouterOSSSH
        except Exception as exc:  # noqa: BLE001
            print(f"\nGagal memuat modul SSH: {exc}\nPasang dulu: pip install paramiko")
            return 1
        client = RouterOSSSH(args.host, args.username, args.password)
    else:
        from app.mikrotik.api import RouterOSAPI

        client = RouterOSAPI(args.host, args.username, args.password, port=args.port)

    print("2) Login & ambil info sistem ...")
    ok, pesan = client.check()
    print(f"   {'OK' if ok else 'GAGAL'} - {pesan}")
    if not ok:
        print("\nKemungkinan penyebab: username/password salah, atau user belum punya hak 'api'.")
        print("Buat user khusus:  /user add name=ispops group=api password=... address=IP-SERVER-INI")
        client.close()
        return 1

    print("3) Baca data PPP ...")
    try:
        secrets = client.ppp_secrets()
        active = client.ppp_active()
        print(f"   OK - {len(secrets)} PPP secret, {len(active)} sesi aktif")
        for row in active[:5]:
            print(
                f"     {row.get('name', '?'):28s} {row.get('address', '?'):16s} "
                f"{row.get('mac-address', '?'):18s} uptime {row.get('uptime', '?')}"
            )
        if len(active) > 5:
            print(f"     ... dan {len(active) - 5} sesi lainnya")
    except Exception as exc:  # noqa: BLE001
        print(f"   GAGAL - {type(exc).__name__}: {exc}")
        print("\nUser butuh hak baca. Cek group user di mikrotik (group 'api' atau 'full').")
        client.close()
        return 1

    print("4) Cek address-list isolir & rule firewall ...")
    from app.config import settings

    ips = client.address_list_addresses(settings.isolir_list)
    print(f"   address-list '{settings.isolir_list}' berisi {len(ips)} IP")
    dibuat = client.ensure_isolir_rule(
        settings.isolir_list, settings.isolir_action, settings.isolir_rule_comment
    )
    print(
        f"   rule '{settings.isolir_rule_comment}' "
        f"{'BARU dibuat di posisi teratas' if dibuat else 'sudah ada'}"
    )

    client.close()
    print("\nSemua beres. Aplikasi siap dipakai.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
