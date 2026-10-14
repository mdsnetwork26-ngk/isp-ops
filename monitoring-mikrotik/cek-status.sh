#!/usr/bin/env bash
# ============================================================ ISP-OPS
# Pemeriksa keadaan. Jalankan kalau ada yang tidak beres:
#
#     sudo bash cek-status.sh
#
# Perlu sudo: membaca journal, melihat proses pemilik port, dan nginx -t.
#
# Skrip ini memeriksa rantai permintaan dari browser sampai ke mikrotik,
# lalu menyebut bagian mana yang putus.
# ============================================================
set -uo pipefail

DIR="${DIR:-/opt/isp-ops}"
PORT="${PORT:-8000}"
APP_USER="${APP_USER:-ispops}"

hijau()  { printf '\033[32m  [OK]   %s\033[0m\n' "$*"; }
merah()  { printf '\033[31m  [GAGAL] %s\033[0m\n' "$*"; }
kuning() { printf '\033[33m  [?]    %s\033[0m\n' "$*"; }
judul()  { printf '\n\033[36m%s\033[0m\n' "$*"; }

gagal=0

if [[ $EUID -ne 0 ]]; then
  kuning "sebaiknya dijalankan dengan sudo agar journal & nginx -t terbaca"
fi

judul "1. Berkas aplikasi di $DIR"
for f in requirements.txt app/main.py static/app.js templates/index.html .env venv/bin/python; do
  if [[ -e "$DIR/$f" ]]; then hijau "$f"; else merah "$f tidak ada"; gagal=1; fi
done

judul "2. Service systemd"
if systemctl is-active --quiet ispops; then
  hijau "service ispops aktif"
else
  merah "service ispops TIDAK aktif -> ini penyebab 502 paling umum"
  gagal=1
  echo "        systemctl status ispops --no-pager | head -20"
  systemctl status ispops --no-pager 2>&1 | grep -E "Active:|Process:|Main PID:" | sed 's/^/        /'
  echo
  echo "        15 baris log terakhir:"
  journalctl -u ispops -n 15 --no-pager 2>&1 | sed 's/^/        /'
fi

judul "3. Port $PORT di 127.0.0.1"
listen="$(ss -tlnp 2>/dev/null | grep ":$PORT " || true)"
if [[ -n "$listen" ]]; then
  hijau "ada yang mendengar di port $PORT"
  echo "        $listen" | sed 's/^/        /'
else
  merah "tidak ada yang mendengar di port $PORT"
  gagal=1
fi

judul "4. Aplikasi menjawab dari dalam server"
body="$(curl -s -m 5 "http://127.0.0.1:$PORT/health" || true)"
if [[ "$body" == *'"status":"ok"'* ]]; then
  hijau "/health menjawab: $body"
elif [[ -n "$body" ]]; then
  kuning "/health menjawab tapi tidak biasa: ${body:0:160}"
else
  merah "/health tidak menjawab sama sekali"
  gagal=1
fi

judul "5. nginx"
# nginx umumnya di /usr/sbin yang tidak selalu ada di PATH user biasa
NGINX="$(command -v nginx || true)"
[[ -z "$NGINX" && -x /usr/sbin/nginx ]] && NGINX=/usr/sbin/nginx
[[ -z "$NGINX" && -x /sbin/nginx ]] && NGINX=/sbin/nginx
if [[ -n "$NGINX" ]]; then
  if "$NGINX" -t &>/dev/null; then hijau "konfigurasi nginx benar ($NGINX)"; else
    merah "konfigurasi nginx ditolak"; "$NGINX" -t 2>&1 | tail -3 | sed 's/^/        /'; gagal=1
  fi
  if systemctl is-active --quiet nginx; then hijau "nginx aktif"; else merah "nginx tidak aktif"; gagal=1; fi
  if [[ -e /etc/nginx/sites-enabled/default ]]; then
    kuning "situs 'default' masih aktif - bisa menyambar permintaan (tampil 'Welcome to nginx!')"
  fi
  upstream="$(grep -rh "proxy_pass" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/ 2>/dev/null | head -1)"
  echo "        upstream: ${upstream:-tidak ditemukan}"
  err="$(tail -3 /var/log/nginx/error.log 2>/dev/null)"
  [[ -n "$err" ]] && { echo "        error.log terakhir:"; echo "$err" | sed 's/^/        /'; }
else
  kuning "nginx tidak terpasang - buka http://IP-SERVER:$PORT/ langsung (ubah --host jadi 0.0.0.0)"
fi

judul "6. Lewat nginx (port 80)"
body="$(curl -s -m 5 -H "Host: $(hostname -I 2>/dev/null | awk '{print $1}')" http://127.0.0.1/health || true)"
if [[ "$body" == *'"status":"ok"'* ]]; then
  hijau "http://IP-SERVER/ melayani aplikasi"
else
  merah "http://IP-SERVER/ belum melayani aplikasi (${body:0:80})"
fi

judul "7. Semua modul bisa dimuat + dependensi lengkap"
if [[ -x "$DIR/venv/bin/python" ]]; then
  out="$(cd "$DIR" && sudo -u "$APP_USER" "$DIR/venv/bin/python" - <<'PYEOF' 2>&1
import importlib
mods = ["fastapi", "uvicorn", "jinja2", "sqlalchemy", "httpx", "dotenv",
        "app.main", "app.routes", "app.services", "app.onu", "app.billing",
        "app.database", "app.models", "app.routeros", "app.kompat",
        "app.mikrotik.api", "app.mikrotik.ssh"]
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as exc:
        bad.append(f"{m}: {type(exc).__name__}: {exc}")
print("\n".join(bad) if bad else "SEMUA-OK")
PYEOF
)"
  if [[ "$out" == *"SEMUA-OK"* ]]; then
    hijau "semua modul & dependensi dimuat tanpa error"
  else
    merah "ada modul yang gagal dimuat:"
    echo "$out" | sed 's/^/        /'
    gagal=1
  fi
else
  kuning "venv belum ada - jalankan: sudo -u $APP_USER python3 -m venv $DIR/venv"
fi

judul "8. Berkas .env"
if [[ -r "$DIR/.env" ]]; then
  hijau ".env bisa dibaca"
  grep -qE "^AUTH_PASSWORD=.+" "$DIR/.env" || kuning "AUTH_PASSWORD masih kosong"
else
  kuning ".env tidak bisa dibaca oleh user ini (normal kalau chmod 600)"
fi

echo
if [[ $gagal -eq 0 ]]; then
  hijau "Rantai utuh. Buka http://$(hostname -I 2>/dev/null | awk '{print $1}')/ di browser."
else
  merah "Ada bagian yang putus - lihat baris [GAGAL] di atas."
  echo
  echo "Perbaikan paling sering:"
  echo "  sudo systemctl restart ispops && sleep 2 && systemctl status ispops --no-pager | head -20"
  echo "  sudo -u $APP_USER $DIR/venv/bin/python -c 'import app.main'   # cek dependensi lengkap"
fi
