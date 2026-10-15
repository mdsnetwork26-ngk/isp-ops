# ============================================================================
# ISP-OPS - pengalihan klien profil SUSPEND ke halaman isolir
#
# Prasyarat:
#   1. Profil PPPoE "SUSPEND" sudah ada (instalasi ini: remote-address=
#      POOL-SUSPEND, rate-limit 1k/1k, address-list=SUSPEND-LIST), dan
#      aplikasi sudah memakai ISOLIR_MODE=suspend + SUSPEND_PROFILE=SUSPEND.
#   2. Aplikasi ISP-OPS sudah menyediakan halaman publik /isolir.
#
# Cara kerja:
#   - DNS tetap diizinkan supaya nama situs tetap ter-resolve.
#   - HTTP (tcp 80) klien di address-list SUSPEND-LIST di-dst-nat ke
#     server ISP-OPS -> aplikasi mengenali IP klien terisolir dan
#     menampilkannya halaman "Internet Sedang Dalam Masa Isolir".
#     URL uji konektivitas Android/Apple/Windows ikut sampai ke halaman
#     itu, jadi HP biasanya langsung memunculkannya sebagai portal.
#   - Lalu lintas forward lain dari SUSPEND-LIST di-drop (termasuk HTTPS:
#     HTTPS memang TIDAK di-dst-nat karena akan memicu peringatan
#     sertifikat di browser; halaman isolir tampil lewat HTTP/portal).
#
# Jalankan: sesuaikan DUA nilai di bawah ini dulu, lalu tempel di
# New Terminal WinBox / SSH, atau upload file ini lalu /import.
# ============================================================================

# ---- Server ISP-OPS instalasi ini: 192.168.38.2 port 8000 (akses langsung)
# Halaman /isolir dilayani aplikasi di server ini -- file isolir.html
# tinggal di server (/opt/isp-ops/templates/isolir.html), BUKAN di MikroTik.
# MikroTik hanya menjalankan skrip ini untuk mengalihkan klien ke sana.
:local serverIp   "192.168.38.2"
:local serverPort 8000
:local daftar     "SUSPEND-LIST"

# ----------------------------------------------------------------------------
# 1) NAT: alihkan HTTP klien suspend ke server halaman isolir (idempoten)
# ----------------------------------------------------------------------------
:if ([/ip/firewall/nat find comment="ISP-OPS SUSPEND REDIRECT"] = "") do={
    /ip/firewall/nat add chain=dstnat protocol=tcp dst-port=80 \
        src-address-list=$daftar dst-address=!$serverIp \
        action=dst-nat to-addresses=$serverIp to-ports=$serverPort \
        comment="ISP-OPS SUSPEND REDIRECT"
    :log info "ISP-OPS: rule redirect halaman isolir dibuat"
} else={
    :log info "ISP-OPS: rule redirect halaman isolir sudah ada, dilewati"
}

# ----------------------------------------------------------------------------
# 1b) SRCNAT: jangan masquerade lalu lintas ke server isolir.
#     Rule masquerade umum (out-interface tidak dibatasi) menimpa IP sumber
#     klien 10.200.0.x menjadi IP router; server lalu tidak bisa mengenali
#     pelanggan mana yang terisolir dan halaman tampil tanpa nama.
#     action=accept di chain srcnat berarti "lewati tanpa NAT" khusus
#     tujuan ini, dan rule ini ditaruh PALING ATAS chain srcnat.
#     Syarat: server bisa mencapai kembali 10.200.0.0/24 lewat router ini
#     (umumnya benar bila gateway server adalah router ini).
# ----------------------------------------------------------------------------
:if ([/ip/firewall/nat find comment="ISP-OPS SUSPEND NO-NAT"] = "") do={
    /ip/firewall/nat add chain=srcnat protocol=tcp dst-address=$serverIp \
        dst-port=$serverPort action=accept place-before=0 \
        comment="ISP-OPS SUSPEND NO-NAT"
    :log info "ISP-OPS: pengecualian masquerade untuk server isolir dibuat"
}

# ----------------------------------------------------------------------------
# 2) FILTER: izinkan DNS + halaman isolir, drop sisanya (idempoten)
#    Rule accept ditaruh SEBELUM rule drop. Bila Anda sudah punya rule
#    drop untuk SUSPEND-LIST, pastikan dua accept ini berada di atasnya
#    (cek: /ip firewall filter print, geser dengan perintah move bila perlu).
# ----------------------------------------------------------------------------
:if ([/ip/firewall/filter find comment="ISP-OPS SUSPEND PAGE"] = "") do={
    /ip/firewall/filter add chain=forward protocol=udp dst-port=53 \
        src-address-list=$daftar action=accept comment="ISP-OPS SUSPEND DNS"
    /ip/firewall/filter add chain=forward protocol=tcp dst-port=53 \
        src-address-list=$daftar action=accept comment="ISP-OPS SUSPEND DNS"
    /ip/firewall/filter add chain=forward protocol=tcp \
        src-address-list=$daftar dst-address=$serverIp dst-port=$serverPort \
        action=accept comment="ISP-OPS SUSPEND PAGE"
    :log info "ISP-OPS: rule accept DNS + halaman isolir dibuat"
}

# HTTPS ditolak dengan tcp-reset (bukan drop diam-diam) supaya browser/
# aplikasi klien GAGAL CEPAT dan segera beralih ke deteksi portal HTTP,
# bukan menggantung sampai timeout berulang kali. Ini yang membuat
# pengalihan terasa lambat bila hanya di-drop.
:if ([/ip/firewall/filter find comment="ISP-OPS SUSPEND REJECT"] = "") do={
    /ip/firewall/filter add chain=forward protocol=tcp dst-port=443 \
        src-address-list=$daftar action=reject reject-with=tcp-reset \
        comment="ISP-OPS SUSPEND REJECT"
    :log info "ISP-OPS: rule reject HTTPS klien suspend dibuat"
}

:if ([/ip/firewall/filter find comment="ISP-OPS SUSPEND DROP"] = "") do={
    /ip/firewall/filter add chain=forward src-address-list=$daftar \
        action=drop comment="ISP-OPS SUSPEND DROP"
    :log info "ISP-OPS: rule drop klien suspend dibuat"
} else={
    :log info "ISP-OPS: rule drop klien suspend sudah ada, dilewati"
}

# ============================================================================
# Membatalkan (rollback) bila tidak jadi dipakai:
#   /ip firewall nat    remove [find comment~"ISP-OPS SUSPEND"]
#   /ip firewall filter remove [find comment~"ISP-OPS SUSPEND"]
# Profil SUSPEND dan aplikasi tidak terpengaruh; klien suspend kembali
# hanya kena rate-limit 1k/1k seperti sebelumnya.
# ============================================================================
