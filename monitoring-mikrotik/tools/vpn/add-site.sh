#!/usr/bin/env bash
# ============================================================================
# Tambah satu situs luar ke VPN hub (sisi SERVER kantor).
#
# Pakai:  sudo ./add-site.sh NAMA IP-PUBLIK-KANTOR SUBNET-LAN [SUBNET-LAN-2 ...]
# Contoh: sudo ./add-site.sh situs-b 202.10.3.4 10.11.11.0/24
#
# Hasil:
#   - sertifikat klien + $DIR/clients/NAMA.ovpn (untuk perangkat non-Mikrotik)
#   - $DIR/clients/NAMA.{crt,key} + ca.crt (untuk diimpor ke Mikrotik)
#   - ccd iroute: subnet LAN situs dirutekan lewat tunnel klien ini
#   - perintah RouterOS siap tempel (dicetak di akhir)
# ============================================================================
set -euo pipefail

[ "${VPN_FORCE:-}" = "1" ] || [ "$(id -u)" -eq 0 ] || { echo "jalankan sebagai root/sudo"; exit 1; }
[ $# -ge 3 ] || { echo "pakai: $0 NAMA IP-PUBLIK-KANTOR SUBNET-LAN [SUBNET2..]"; exit 1; }

NAMA="$1"; OFFICE="$2"; shift 2
SUBNETS=("$@")
DIR="${VPN_DIR:-/etc/openvpn/ispops}"
ERSA="${VPN_ERSA:-/etc/openvpn/easy-rsa}"
CONF="${VPN_CONF:-/etc/openvpn/ispops.conf}"
PORT="$(grep -E '^port ' "$CONF" | awk '{print $2}')"
NET="$(grep -E '^server ' "$CONF" | awk '{print $2}')"
OFFICE_LAN="$(grep -oP 'push "route \K[0-9.]+' "$CONF")"
[ -f "$ERSA/pki/ca.crt" ] || { echo "jalankan setup-server.sh dulu"; exit 1; }

# ---------------------------------------------------------------- sertifikat
if [ ! -f "$ERSA/pki/issued/$NAMA.crt" ]; then
  EASYRSA="$ERSA" "$ERSA/easyrsa" --batch build-client-full "$NAMA" nopass
fi
EASYRSA="$ERSA" "$ERSA/easyrsa" gen-crl   # segarkan CRL biar revoke langsung berlaku

# ---------------------------------------------------------------- ccd iroute
CCD="$DIR/ccd/$NAMA"
: > "$CCD"
for s in "${SUBNETS[@]}"; do
  n="${s%/*}"; p="${s#*/}"
  [ "$p" = "$s" ] && p=24
  mask=$(python3 -c "import ipaddress;print(ipaddress.ip_network('0.0.0.0/$p').netmask)")
  echo "iroute $n $mask" >> "$CCD"
done

# ---------------------------------------------------------------- ekspor file
cp "$ERSA/pki/ca.crt"               "$DIR/clients/ca.crt"
cp "$ERSA/pki/issued/$NAMA.crt"     "$DIR/clients/$NAMA.crt"
cp "$ERSA/pki/private/$NAMA.key"    "$DIR/clients/$NAMA.key"

cat > "$DIR/clients/$NAMA.ovpn" <<OVPN
client
dev tun
proto tcp
remote $OFFICE $PORT
cipher AES-256-CBC
auth SHA256
nobind
persist-key
persist-tun
remote-cert-tls server
verb 3
<ca>
$(cat "$ERSA/pki/ca.crt")
</ca>
<cert>
$(cat "$ERSA/pki/issued/$NAMA.crt")
</cert>
<key>
$(cat "$ERSA/pki/private/$NAMA.key")
</key>
OVPN
chmod 600 "$DIR/clients/$NAMA.key" "$DIR/clients/$NAMA.ovpn"

if [ "${VPN_FORCE:-}" != "1" ] && command -v systemctl >/dev/null; then
  systemctl reload-or-restart openvpn@ispops
fi

cat <<EOF

SELESAI untuk situs '$NAMA' (subnet: ${SUBNETS[*]}).
File di $DIR/clients/:  $NAMA.ovpn  $NAMA.crt  $NAMA.key  ca.crt

==================== TEMPEL DI MIKROTIK SITUS (Winbox terminal) ====================
# 1) Salin ca.crt, $NAMA.crt, $NAMA.key ke router (Winbox Files / scp).
# 2) Jalankan:
/certificate import file-name=ca.crt name=ca-ispops
/certificate import file-name=$NAMA.crt name=vpn-$NAMA
/certificate import file-name=$NAMA.key name=vpn-$NAMA
/interface ovpn-client add name=ovpn-ispops connect-to=$OFFICE port=$PORT \\
    certificate=vpn-$NAMA cipher=aes256 auth=sha256 disabled=no
/ip route add dst-address=$NET/24 gateway=ovpn-ispops comment=vpn-ispops
# route pulang ke subnet kantor (opsional, biar situs bisa akses kantor):
/ip route add dst-address=$OFFICE_LAN/24 gateway=ovpn-ispops comment=vpn-ispops

# 3) Uji dari SERVER kantor:
#      ip route get ${SUBNETS[0]%.*}.1       # harus dev tun0
#      curl -v --max-time 8 http://${SUBNETS[0]%.*}.1/
#    lalu coba Remote ONU situs ini di aplikasi - harus terbuka langsung.
# Catatan router tua: 'auth' & 'cipher' harus SAMA di server.conf dan klien
# Mikrotik. Bila RouterOS lama menolak sha256, ganti keduanya ke sha1:
#   server.conf: auth SHA1   |   mikrotik: auth=sha1
EOF
