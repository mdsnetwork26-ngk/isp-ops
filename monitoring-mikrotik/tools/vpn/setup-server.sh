#!/usr/bin/env bash
# ============================================================================
# ISP-OPS VPN HUB (OpenVPN) - setup sisi SERVER kantor.
#
# Untuk situs luar yang routernya TUA (tanpa WireGuard/ZeroTier): router jadi
# klien OVPN yang dial-out ke kantor; server merutekan subnet LAN situs lewat
# tunnel (full-tunnel). Setelah ini, tambah situs cukup: ./add-site.sh
#
# Pakai:   sudo ./setup-server.sh [SUBNET_TUNNEL] [PORT]
# Contoh:  sudo ./setup-server.sh                 # 10.12.0.0/24, tcp 1194
#          sudo ./setup-server.sh 10.12.0.0 1194
#
# CATATAN: SUBNET_TUNNEL jangan sama dengan subnet WireGuard/LAN mana pun
# (WireGuard Anda memakai 10.10.10.0/24 -> default 10.12.0.0 aman).
# ============================================================================
set -euo pipefail

[ "${VPN_FORCE:-}" = "1" ] || [ "$(id -u)" -eq 0 ] || { echo "jalankan sebagai root/sudo"; exit 1; }

NET="${1:-10.12.0.0}"
PORT="${2:-1194}"
MASK="255.255.255.0"
DIR="${VPN_DIR:-/etc/openvpn/ispops}"
ERSA="${VPN_ERSA:-/etc/openvpn/easy-rsa}"
CONF="${VPN_CONF:-/etc/openvpn/ispops.conf}"
OFFICE_LAN="${OFFICE_LAN:-192.168.38.0}"   # subnet kantor (dipush ke klien)

if [ "${VPN_SKIP_APT:-}" != "1" ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq openvpn easy-rsa
fi

mkdir -p "$DIR/ccd" "$DIR/clients"

# ---------------------------------------------------------------- PKI (sekali)
if [ ! -f "$ERSA/pki/ca.crt" ]; then
  # jangan rm -rf: kalau PKI sudah ada tapi ca.crt hilang, lebih baik gagal
  # eksplisit daripada membuang sertifikat lama yang mungkin masih dipakai.
  mkdir -p "$ERSA"
  if [ ! -x "$ERSA/easyrsa" ] && [ -d /usr/share/easy-rsa ]; then
    cp -a /usr/share/easy-rsa/. "$ERSA/"
  fi
  [ -x "$ERSA/easyrsa" ] || { echo "easy-rsa tidak ditemukan (pasang: apt install easy-rsa)"; exit 1; }
  cat > "$ERSA/vars" <<'VARS'
set_var EASYRSA_ALGO           rsa
set_var EASYRSA_KEY_SIZE       2048
set_var EASYRSA_DIGEST         sha256
set_var EASYRSA_CA_EXPIRE      3650
set_var EASYRSA_CERT_EXPIRE    1825
set_var EASYRSA_CRL_DAYS       365
set_var EASYRSA_DN             cn_only
set_var EASYRSA_REQ_COUNTRY    "ID"
set_var EASYRSA_REQ_ORG        "ISP-OPS"
set_var EASYRSA_REQ_CN         "ISP-OPS-CA"
VARS
  EASYRSA="$ERSA" "$ERSA/easyrsa" --batch init-pki
  EASYRSA="$ERSA" "$ERSA/easyrsa" --batch build-ca nopass
  EASYRSA="$ERSA" "$ERSA/easyrsa" --batch build-server-full server nopass
  EASYRSA="$ERSA" "$ERSA/easyrsa" gen-crl
fi

# ---------------------------------------------------------------- server.conf
cat > "$CONF" <<CONF
# ISP-OPS VPN hub - dibuat oleh tools/vpn/setup-server.sh
port $PORT
proto tcp
dev tun
ca $ERSA/pki/ca.crt
cert $ERSA/pki/issued/server.crt
key $ERSA/pki/private/server.key
crl-verify $ERSA/pki/crl.pem
server $NET $MASK
client-config-dir $DIR/ccd
push "route $OFFICE_LAN $MASK"
keepalive 10 60
cipher AES-256-CBC
auth SHA256
user nobody
group nogroup
persist-key
persist-tun
verb 3
status $DIR/status.log
CONF

# ---------------------------------------------------------------- forwarding
if [ "${VPN_FORCE:-}" != "1" ]; then
  sysctl -w net.ipv4.ip_forward=1 >/dev/null
  grep -q "^net.ipv4.ip_forward=1" /etc/sysctl.conf \
    || echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
  systemctl enable --now openvpn@ispops
  systemctl restart openvpn@ispops
fi

cat <<EOF

SELESAI. Server OpenVPN aktif di tcp/$PORT (subnet $NET/24).

LANGKAH BERIKUTNYA (sekali per situs):
  sudo ./add-site.sh NAMA-SITUS IP-PUBLIK-KANTOR SUBNET-LAN-SITUS
  contoh: sudo ./add-site.sh situs-b 202.10.3.4 10.11.11.0/24

Pastikan port $PORT/tcp diforward di edge kantor ke $(hostname -I | awk '{print $1}').
EOF
