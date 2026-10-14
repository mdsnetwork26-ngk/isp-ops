#!/usr/bin/env python3
"""Cari sebab aplikasi tidak bisa konek ke mikrotik, lalu sebut perintah perbaikannya.

Pakai:
    cd /opt/isp-ops
    sudo -u ispops ./venv/bin/python cek-mikrotik.py IP-MIKROTIK username password
    sudo -u ispops ./venv/bin/python cek-mikrotik.py 192.168.38.1 ispops 'rahasia' --port 8728
"""
from __future__ import annotations

import argparse
import ipaddress
import socket
import sys

OK = "\033[32m[OK]\033[0m"
GAGAL = "\033[31m[GAGAL]\033[0m"
TANYA = "\033[33m[?]\033[0m"


def langkah(n: int, judul: str) -> None:
    print(f"\n\033[36m{n}. {judul}\033[0m")


def ip_sendiri() -> str:
    """IP server ini yang paling mungkin dipakai mikrotik melihat kita."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "?"
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("username")
    ap.add_argument("password")
    ap.add_argument("--port", type=int, default=8728)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument(
        "--tanpa-tls", action="store_true",
        help="pakai API polos di port 8729 (kalau service 'api' dipindah ke 8729, "
             "bukan 'api-ssl'). Lihat /ip service print di mikrotik.",
    )
    a = ap.parse_args()
    use_ssl = False if a.tanpa_tls else None  # None = otomatis (TLS kalau 8729)

    print(f"Tujuan : {a.host}:{a.port}")
    print(f"Server : {ip_sendiri()}  <- IP inilah yang harus diizinkan di mikrotik")

    # --- 1. host bisa dijangkau? ------------------------------------
    langkah(1, "Host mikrotik bisa dijangkau (ICMP/TCP)")
    try:
        ip = socket.gethostbyname(a.host)
        print(f"  {OK} {a.host} -> {ip}")
    except socket.gaierror as exc:
        print(f"  {GAGAL} nama tidak dikenal: {exc}")
        print("     -> pakai IP angka, bukan nama. Contoh: 192.168.38.1")
        return 1

    # --- 2. port API terbuka? ---------------------------------------
    langkah(2, f"Port {a.port} terbuka")
    # Sengaja tidak memakai create_connection() lalu ditutup: mikrotik menghitung
    # itu sebagai satu sesi API, dan jumlah sesinya terbatas. SYN + RST saja.
    uji = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    uji.setblocking(False)
    try:
        uji.connect((ip, a.port))
        print(f"  {OK} TCP {ip}:{a.port} terbuka")
    except ConnectionRefusedError:
        print(f"  {GAGAL} koneksi DITOLAK -> service api mati di mikrotik")
        print("     Jalankan di mikrotik:")
        print("       /ip service enable api")
        print("       /ip service print")
        return 2
    except socket.timeout:
        print(f"  {GAGAL} TIMEOUT -> diblokir firewall, atau tidak ada rute")
        print("     Cek di server ini:")
        print(f"       ip route get {ip}")
        print(f"       ping -c 3 {ip}")
        print("     Cek di mikrotik (bolehkah server ini masuk?):")
        print(f"       /ip firewall filter print chain=input")
        print(f"       /ip firewall filter add chain=input src-address={ip_sendiri()}/32 "
              f"protocol=tcp dst-port={a.port} action=accept place-before=0 comment=ISP-OPS")
        print("     Cek juga service api dibatasi alamatnya:")
        print("       /ip service print")
        print("       -> kolom ADDRESS harus kosong atau memuat IP server ini")
        return 3
    except OSError as exc:
        if isinstance(exc, BlockingIOError):
            # SYN sudah dijawab -> port terbuka; RST menyusul, tidak apa-apa
            print(f"  {OK} TCP {ip}:{a.port} terbuka")
        else:
            print(f"  {GAGAL} {type(exc).__name__}: {exc}")
            uji.close()
            return 4
    finally:
        try:
            uji.close()
        except OSError:
            pass

    # --- 3. login ----------------------------------------------------
    langkah(3, "Login lewat API")
    from app.routeros import ConnectionClosed, LibRouterosError, TrapError, connect

    try:
        api = connect(ip, a.username, a.password, port=a.port, timeout=a.timeout,
                      use_ssl=use_ssl)
        print(f"  {OK} login berhasil sebagai '{a.username}'")
        info = api.call("/system/resource/print")
        if info:
            print(f"     RouterOS {info[0].get('version', '?')} / {info[0].get('board-name', '?')}")
    except TrapError as exc:
        print(f"  {GAGAL} login ditolak: {exc}")
        print("     Sebab tersering: password salah, atau user dibatasi kolom ADDRESS.")
        print("     Perbaiki di mikrotik:")
        print(f"       /user set [find name={a.username}] address=\"\"")
        print(f"       /user set [find name={a.username}] password=PasswordBaru")
        return 5
    except ConnectionClosed as exc:
        print(f"  {GAGAL} {exc}")
        if "SSL" in str(exc) or "TLS" in str(exc):
            print("     Jabat tangan TLS ditolak. Penyebab paling sering:")
            print("     di mikrotik, service 'api' (polos) dipindah ke port 8729,")
            print("     sedangkan 'api-ssl' invalid/tanpa sertifikat. Cek:")
            print("       /ip service print")
            print("     Kalau di baris 'api' tertulis PORT 8729 -> jalankan ulang dengan:")
            print(f"       python cek-mikrotik.py {a.host} {a.username} '...' --port {a.port} --tanpa-tls")
        else:
            print("     Port terbuka tapi API menolak. Cek: /ip service print")
            print("     Pastikan kolom ADDRESS kosong atau memuat IP server ini.")
        return 6
    except LibRouterosError as exc:
        print(f"  {GAGAL} {type(exc).__name__}: {exc}")
        return 7

    # --- 4. hak baca -------------------------------------------------
    langkah(4, "Hak membaca data PPP")
    try:
        secrets = api.call("/ppp/secret/print")
        active = api.call("/ppp/active/print")
        print(f"  {OK} {len(secrets)} PPP secret, {len(active)} sesi aktif")
    except LibRouterosError as exc:
        print(f"  {GAGAL} {exc}")
        print("     User butuh hak baca. Cek grupnya:")
        print(f"       /user print where name={a.username}")
        print("       -> group harus 'api' atau 'full'")
        api.close()
        return 8

    # --- 5. hak tulis (untuk isolir) ---------------------------------
    langkah(5, "Hak menulis (dipakai tombol Isolir)")
    from app.config import settings
    from app.mikrotik.api import RouterOSAPI

    # pakai adapter yang sama dengan aplikasi, biar yang diuji = yang dipakai
    klien = RouterOSAPI(ip, a.username, a.password, port=a.port, timeout=a.timeout,
                        use_ssl=use_ssl)
    try:
        klien.add_address_list(settings.isolir_list, "127.0.0.99", "ISP-OPS uji - aman dihapus")
        dihapus = klien.remove_address_list(settings.isolir_list, "127.0.0.99")
        print(f"  {OK} menambah & menghapus entri address-list '{settings.isolir_list}' ({dihapus} dihapus)")
    except LibRouterosError as exc:
        print(f"  {TANYA} {exc}")
        if "write" in str(exc):
            print("     Tombol Isolir akan gagal sampai user diberi hak write:")
            print(f"       /user print where name={a.username}   (lihat kolom GROUP)")
            print("       /user group set api policy=read,write,api,test")
        else:
            print("     Tombol Isolir mungkin tetap jalan; yang ditolak cuma atribut tambahan.")
    except Exception as exc:  # noqa: BLE001
        print(f"  {TANYA} {type(exc).__name__}: {exc}")
    print("     (uji ini menambah lalu menghapus IP 127.0.0.99 - tidak berdampak)")

    # --- 6. siapkan rule isolir --------------------------------------
    langkah(6, f"Rule firewall untuk isolir ('{settings.isolir_rule_comment}')")
    try:
        dibuat = klien.ensure_isolir_rule(
            settings.isolir_list, settings.isolir_action, settings.isolir_rule_comment
        )
        print(f"  {OK} rule " + ("dibuat di posisi teratas" if dibuat else "sudah ada")
              + f" (chain=forward, action={settings.isolir_action})")
    except LibRouterosError as exc:
        print(f"  {TANYA} {exc}")
        print("     Bisa dibuat manual di mikrotik (sekali saja, lalu tombol Isolir jalan):")
        print(f"       /ip firewall filter add chain=forward src-address-list={settings.isolir_list} "
              f"action={settings.isolir_action} place-before=0 comment=\"{settings.isolir_rule_comment}\"")

    # --- 7. ringkasan ------------------------------------------------
    langkah(7, "Contoh data yang akan tampil di aplikasi")
    peta_mac = {}
    if active and not any((r.get("mac-address") or "").strip() for r in active):
        pppoe = api.call("/interface/pppoe-server/print")
        peta_mac = {r.get("user", ""): r.get("mac-address", "") for r in pppoe if r.get("user")}
        if peta_mac:
            print(f"  {OK} /ppp active tidak mengirim MAC; diambil dari "
                  f"/interface pppoe-server print ({len(peta_mac)} entri)")
        else:
            print(f"  {TANYA} /ppp active tidak mengirim MAC dan tidak ada entri "
                  f"pppoe-server; kolom MAC akan kosong")
        print(f"     properti yang dikirim /ppp active: {sorted(active[0].keys())}")
    for row in active[:5]:
        nama = row.get("name", "?")
        print(
            f"  {nama:28s} {row.get('address', '?'):16s} "
            f"{(row.get('mac-address') or peta_mac.get(nama) or '?'):18s} "
            f"uptime {row.get('uptime', '?')}"
        )
    if len(active) > 5:
        print(f"  ... dan {len(active) - 5} sesi lainnya")
    if not active:
        print(f"  {TANYA} tidak ada sesi PPP aktif saat ini (semua pelanggan offline)")

    klien.close()
    print(f"\n\033[32mSemua beres.\033[0m Simpan kredensial ini di menu Pengaturan aplikasi:")
    print(f"  host {ip} | port {a.port} | user {a.username}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
