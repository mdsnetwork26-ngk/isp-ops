#!/usr/bin/env bash
# ============================================================ ISP-OPS
# Pemasang sekali jalan. Skrip ini memeriksa tiap langkah dan berhenti
# dengan pesan jelas kalau ada yang kurang - jadi tidak mungkin "berhenti
# di tengah" tanpa tahu sebabnya.
#
#   1) taruh isp-ops-v1.0.0.zip di server (mis. di /home/$USER)
#   2) jalankan:  bash pasang.sh /home/andi/isp-ops-v1.0.0.zip
#
# Setelah selesai, buka  http://IP-SERVER/
# ============================================================
set -euo pipefail

ZI="${1:-}"
DIR="${2:-/opt/isp-ops}"
APP_USER="${APP_USER:-ispops}"

merah()  { printf '\033[31m%s\033[0m\n' "$*"; }
hijau()  { printf '\033[32m%s\033[0m\n' "$*"; }
kuning() { printf '\033[33m%s\033[0m\n' "$*"; }
langkah(){ printf '\n\033[36m==> %s\033[0m\n' "$*"; }

# --- 0. berkas zip -------------------------------------------------
if [[ -z "$ZI" || ! -f "$ZI" ]]; then
  merah "Berkas zip tidak ditemukan."
  echo "Pakai:  bash pasang.sh /path/ke/isp-ops-v1.0.0.zip"
  echo "Contoh: bash pasang.sh /home/$USER/isp-ops-v1.0.0.zip"
  [[ -n "$ZI" ]] && echo "Yang Bapak tulis: $ZI"
  exit 1
fi
echo "Zip      : $ZI"
echo "Tujuan   : $DIR"
echo "User app : $APP_USER"

# --- 1. hak root ---------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  merah "Jalankan dengan sudo:  sudo bash pasang.sh $ZI"
  exit 1
fi

# --- 2. user khusus ------------------------------------------------
langkah "Langkah 1/7  User $APP_USER"
if id "$APP_USER" &>/dev/null; then
  hijau "user $APP_USER sudah ada: $(id "$APP_USER")"
else
  useradd -r -m -s /bin/bash "$APP_USER"
  hijau "user $APP_USER dibuat: $(id "$APP_USER")"
fi

# --- 3. paket sistem -----------------------------------------------
langkah "Langkah 2/7  Paket python3-venv"
if ! dpkg -s python3-venv &>/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip
fi
hijau "python3-venv siap ($(python3 -V))"

# --- 4. ekstrak ----------------------------------------------------
langkah "Langkah 3/7  Ekstrak zip"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if command -v unzip &>/dev/null; then
  unzip -q "$ZI" -d "$TMP"
else
  kuning "unzip tidak ada, pakai python3 -m zipfile"
  python3 -m zipfile -e "$ZI" "$TMP"
fi
SRC="$(find "$TMP" -maxdepth 3 -name requirements.txt -printf '%h\n' | head -1)"
if [[ -z "$SRC" ]]; then
  merah "requirements.txt tidak ada di dalam zip. Pastikan zip-nya benar."
  exit 1
fi
hijau "isi zip ditemukan di: $SRC"

# --- 5. salin ------------------------------------------------------
langkah "Langkah 4/7  Salin ke $DIR"
mkdir -p "$DIR"
cp -a "$SRC/." "$DIR/"
chown -R "$APP_USER:$APP_USER" "$DIR"
hijau "berkas tersalin: $(find "$DIR" -maxdepth 1 -type f | wc -l) berkas di tingkat atas"

# --- 6. venv + dependensi ------------------------------------------
langkah "Langkah 5/7  Virtualenv & dependensi"
if [[ ! -x "$DIR/venv/bin/python" ]]; then
  sudo -u "$APP_USER" python3 -m venv "$DIR/venv"
fi
if [[ ! -f "$DIR/requirements.txt" ]]; then
  merah "requirements.txt tidak ada di $DIR - penyalinan gagal."
  exit 1
fi
sudo -u "$APP_USER" "$DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"
hijau "dependensi terpasang:"
sudo -u "$APP_USER" "$DIR/venv/bin/python" - <<'PY'
import importlib.metadata as m
for p in ("fastapi", "uvicorn", "sqlalchemy", "librouteros", "jinja2", "httpx"):
    print(f"  {p:12s} {m.version(p)}")
PY

# --- 7. .env -------------------------------------------------------
langkah "Langkah 6/7  Berkas .env"
if [[ ! -f "$DIR/.env" ]]; then
  sudo -u "$APP_USER" cp "$DIR/.env.example" "$DIR/.env"
  chmod 600 "$DIR/.env"
  kuning ".env dibuat dari contoh - WAJIB diisi passwordnya:"
  kuning "    sudo -u $APP_USER nano $DIR/.env     (ubah AUTH_PASSWORD)"
else
  hijau ".env sudah ada, tidak ditimpa"
fi

# --- 8. service ----------------------------------------------------
langkah "Langkah 7/7  Service systemd"
sed -e "s|^User=.*|User=$APP_USER|" \
    -e "s|^Group=.*|Group=$APP_USER|" \
    -e "s|/opt/isp-ops|$DIR|g" \
    "$DIR/deploy/ispops.service" > /etc/systemd/system/ispops.service
systemctl daemon-reload
systemctl enable --now ispops
sleep 2
if systemctl is-active --quiet ispops; then
  hijau "service ispops AKTIF"
else
  merah "service belum aktif. Lihat log:  journalctl -u ispops -n 40 --no-pager"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
# --- 9. nginx (opsional, kalau terpasang) ---------------------------
if command -v nginx &>/dev/null; then
  langkah "Bonus  nginx sebagai pintu depan"
  cp "$DIR/deploy/nginx-ispops.conf" /etc/nginx/sites-available/ispops 2>/dev/null \
    || cp "$DIR/deploy/nginx-ispops.conf" /etc/nginx/conf.d/ispops.conf
  ln -sf /etc/nginx/sites-available/ispops /etc/nginx/sites-enabled/ispops 2>/dev/null || true
  rm -f /etc/nginx/sites-enabled/default      # situs bawaan menyambar semua host
  if nginx -t &>/dev/null; then
    systemctl restart nginx
    hijau "nginx aktif -> buka http://${IP:-IP-SERVER}/"
  else
    merah "konfigurasi nginx ditolak. Perbaiki lalu: sudo nginx -t"
    nginx -t 2>&1 | tail -3
  fi
fi

cat <<EOF

============================================================
Selesai.

  Uji koneksi mikrotik :
      cd $DIR && sudo -u $APP_USER ./venv/bin/python tes-koneksi.py IP-MIKROTIK user password

  Buka aplikasi        : http://${IP:-IP-SERVER}/
  Login                : user 'teknisi', password dari AUTH_PASSWORD di .env
  Status / log         : systemctl status ispops --no-pager
                         journalctl -u ispops -f

Langkah berikutnya di dalam aplikasi:
  menu Pengaturan -> Tambah router -> Tes -> lalu 'Sinkron dari mikrotik'
============================================================
EOF
