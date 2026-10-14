#!/usr/bin/env bash
# ============================================================================
# Hapus satu situs dari VPN hub: revokasi sertifikat + buang route iroute.
# Pakai:  sudo ./remove-site.sh NAMA
# ============================================================================
set -euo pipefail

[ "${VPN_FORCE:-}" = "1" ] || [ "$(id -u)" -eq 0 ] || { echo "jalankan sebagai root/sudo"; exit 1; }
[ $# -eq 1 ] || { echo "pakai: $0 NAMA"; exit 1; }
NAMA="$1"
DIR="${VPN_DIR:-/etc/openvpn/ispops}"
ERSA="${VPN_ERSA:-/etc/openvpn/easy-rsa}"

if [ -f "$ERSA/pki/issued/$NAMA.crt" ]; then
  EASYRSA="$ERSA" "$ERSA/easyrsa" --batch revoke "$NAMA" || true
  EASYRSA="$ERSA" "$ERSA/easyrsa" gen-crl
fi
rm -f "$DIR/ccd/$NAMA" \
      "$DIR/clients/$NAMA.ovpn" "$DIR/clients/$NAMA.crt" "$DIR/clients/$NAMA.key"

if [ "${VPN_FORCE:-}" != "1" ] && command -v systemctl >/dev/null; then
  systemctl reload-or-restart openvpn@ispops
fi
echo "Situs '$NAMA' dihapus: sertifikat direvokasi, route iroute dibuang."
echo "Bersihkan sisi router: remove interface ovpn-ispops + route comment vpn-ispops + sertifikatnya."
