# ============================================================================
# ISP-OPS - skrip isolir untuk RouterOS (v6 & v7)
#
# Bagian A: jalankan SEKALI untuk menyiapkan rule firewall.
# Bagian B: isolir satu pelanggan (ubah :local nama / ipnya).
# Bagian C: buka kembali isolir pelanggan itu.
#
# Ditempel langsung di terminal mikrotik (New Terminal di WinBox, atau SSH),
# atau diunggah:  /file upload lalu  /import mikrotik-isolir.rsc
# ============================================================================

# ----------------------------------------------------------------------------
# A. RULE FIREWALL (sekali saja)
# ----------------------------------------------------------------------------
:if ([/ip/firewall/filter find comment="ISP-OPS ISOLIR"] = "") do={
    /ip/firewall/filter add chain=forward action=drop src-address-list=isolir comment="ISP-OPS ISOLIR"
    /ip/firewall/filter move [find comment="ISP-OPS ISOLIR"] 0
    :log info "ISP-OPS: rule isolir dibuat di posisi teratas"
} else={
    :log info "ISP-OPS: rule isolir sudah ada, dilewati"
}

# ----------------------------------------------------------------------------
# B. ISOLIR satu pelanggan - ubah dua nilai ini
# ----------------------------------------------------------------------------
:local nama  "budi"
:local ipnya "10.1.1.100"

# 1) masuk address-list 'isolir' -> langsung di-drop oleh rule bagian A
/ip/firewall/address-list add list=isolir address=$ipnya comment="ISP-OPS ISOLIR | $nama"

# 2) disable PPP secret supaya tidak bisa login lagi
/ppp/secret set [find name=$nama] disabled=yes

# 3) putus sesi yang sedang berjalan
/ppp/active remove [find name=$nama]

# ----------------------------------------------------------------------------
# C. BUKA isolir (setelah bayar) - isi nama/ipnya yang sama
# ----------------------------------------------------------------------------
# :local nama  "budi"
# :local ipnya "10.1.1.100"
# /ip/firewall/address-list remove [find list=isolir address=$ipnya]
# /ppp/secret set [find name=$nama] disabled=no
