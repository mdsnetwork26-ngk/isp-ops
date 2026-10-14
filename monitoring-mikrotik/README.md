# ISP-OPS — Monitoring ONU & Kontrol Mikrotik untuk Teknisi

Aplikasi web ringan untuk teknisi ISP/RT-RW Net. Satu layar untuk melihat kondisi
pelanggan yang dilayani sebuah mikrotik, membuka web ONU/router pelanggan, dan
mengisolir pelanggan yang belum bayar.

**Dipilih karena stabil & ringan:** Python + FastAPI + RouterOS API + SQLite.
Satu proses, pemakaian memori ±80 MB, tidak butuh Node/Redis/queue. Cocok untuk
VPS Ubuntu 1 GB.

**Butuh Python berapa?** 3.8 ke atas — jadi Ubuntu 20.04 pun bisa. Protokol
RouterOS API ditulis sendiri di `app/routeros.py` (murni pustaka standar),
**bukan** memakai `librouteros`, karena librouteros 4.x tidak tersedia untuk
Python 3.8. Cek versi Python server: `python3 -V`.

---

## 1. Apa yang ditampilkan

Satu tabel, per pelanggan:

| Kolom | Sumber di mikrotik |
|---|---|
| Nama PPPoE | `/ppp secret` + `/ppp active` |
| IP (address) | `/ppp active` → `address` |
| MAC | `/ppp active` → `mac-address` |
| Uptime | `/ppp active` → `uptime` (ditampilkan "6j 16m", "1 hari 3 jam", …) |
| Status | **UP** (ada sesi aktif), **DOWN** (secret ada, tidak konek), **ISOLIR**, **DISABLED** |
| Bayar | manual, atau otomatis dari aplikasi penagihan (lihat §7) |
| Aksi | Isolir / Buka isolir · Remote ONU (`http://IP-ONU:80`) · Router (`http://IP-router:80`) · Ping · Detail |

Data disegarkan otomatis tiap 5 detik (bisa diubah), dan langsung disegarkan lagi
setiap kali teknisi menekan tombol Isolir/Buka.

---

## 2. Persiapan di Mikrotik (wajib, sekali saja)

Jalankan di terminal mikrotik (ganti `1.2.3.4` dengan IP server Ubuntu Anda):

```routeros
# a) nyalakan service API. Pilih SALAH SATU:
/ip service enable api         # port 8728, tanpa enkripsi (umumnya dipakai)
/ip service enable api-ssl     # port 8729, pakai TLS

# pastikan kolom ADDRESS kosong atau memuat IP server Ubuntu, kalau tidak
# mikrotik akan menolak/timeout walau password benar
/ip service print

# b) buat user khusus aplikasi (jangan pakai user admin!)
/user add name=ispops group=api password=GantiPasswordIni address="" comment="ISP-OPS"

# c) batasi: hanya server itu yang boleh masuk port API (contoh pakai 8728;
#    ganti ke 8729 kalau Bapak memakai api-ssl)
/ip firewall filter add chain=input protocol=tcp dst-port=8728 src-address=1.2.3.4 action=accept place-before=0 comment="ISP-OPS API"
/ip firewall filter add chain=input protocol=tcp dst-port=8728 action=drop place-before=1 comment="blok API lain"
```

> **Soal TLS di port 8729 — baca dulu `/ip service print`.** Banyak mikrotik
> ternyata **memindah service `api` (polos) ke 8729**, sementara `api-ssl`
> dibiarkan `invalid` tanpa sertifikat:
>
> ```
>  5   api       8729                 <- API POLOS di 8729
>  7 I api-ssl   8729        none     <- 'I' = invalid, tidak pernah aktif
> ```
>
> Kalau bentuknya begitu, koneksinya **tanpa TLS**. Di aplikasi, kolom
> **Koneksi API** → pilih *API polos / tanpa TLS*. Di skrip pemeriksa, tambahkan
> `--tanpa-tls`. Kalau dipaksa TLS, muncul
> `[SSL: WRONG_VERSION_NUMBER] wrong version number`.
>
> Sebaliknya, kalau yang aktif benar-benar `api-ssl` (ada sertifikat), pilih
> *Paksa TLS*. Sertifikat mandiri mikrotik diterima apa adanya.
>
> Merapikan mikrotiknya juga boleh, lalu pakai pengaturan bawaan:
>
> ```routeros
> /ip service set api port=8728
> /ip service disable api-ssl
> ```

> Grup `api` sudah boleh membaca dan menulis. Kalau mau lebih ketat, buat grup
> sendiri dengan policy `read,write,api,test` (tanpa `policy`/`reboot`).

Cek dari server Ubuntu (pakai `--port 8729` kalau memakai api-ssl):

```bash
cd /opt/isp-ops
sudo -u ispops ./venv/bin/python cek-mikrotik.py 192.168.12.1 ispops 'GantiPasswordIni' --port 8729
```

`cek-mikrotik.py` memeriksa 7 titik (host, port, login, hak baca, hak tulis,
rule isolir, contoh data) dan **mencetak perintah perbaikannya** kalau ada yang
gagal.

Skrip ini mengetes 4 hal: port terbuka → login → baca PPP → menyiapkan rule isolir.
Kalau gagal, pesannya menyebut apa yang harus diperbaiki.

---

## 3. Pasang di Ubuntu

### Cara tercepat: satu perintah

Taruh `isp-ops-v1.0.0.zip` di server, lalu:

```bash
unzip -j isp-ops-v1.0.0.zip isp-ops/pasang.sh -d /tmp     # ambil skripnya saja
sudo bash /tmp/pasang.sh /path/ke/isp-ops-v1.0.0.zip
```

Skrip `pasang.sh` mengerjakan 7 langkah di bawah secara berurutan dan
**memeriksa tiap langkah**: bikin user, pasang `python3-venv`, ekstrak zip,
salin ke `/opt/isp-ops`, bikin venv + dependensi, siapkan `.env`, pasang
service systemd. Kalau ada yang kurang, skrip berhenti sambil menyebut
penyebabnya — tidak berhenti di tengah tanpa penjelasan.

Mau pakai user lain? `APP_USER=ubuntu sudo -E bash pasang.sh zip-nya.zip`
Mau folder lain? tambahkan di akhir: `... zip-nya.zip /srv/isp-ops`

> Kalau langkah service gagal dengan `status=226/NAMESPACE`, server Bapak
> berjalan di dalam container/LXC yang tidak mengizinkan sandboxing systemd.
> Hapus 5 baris pengaman di `/etc/systemd/system/ispops.service`
> (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`, `ProtectHome`,
> `ReadWritePaths`), lalu `sudo systemctl daemon-reload && sudo systemctl restart ispops`.

### Cara manual (kalau ingin paham tiap langkah)

### Langkah 0 — buat user khusus `ispops` (WAJIB, sekali saja)

`ispops` **bukan** user bawaan Ubuntu. Itu user baru yang kita buat sendiri,
khusus untuk menjalankan aplikasi ini. Kalau belum dibuat, perintah `chown`
di bawah akan menolak dengan `chown: invalid user: 'ispops:ispops'`.

```bash
sudo useradd -r -m -s /bin/bash ispops
id ispops            # harus keluar: uid=... (ispops) gid=... (ispops) groups=... (ispops)
```

> `useradd -r` otomatis membuat grup dengan nama sama (`ispops`), jadi penulisan
> `ispops:ispops` berarti *user ispops, grup ispops*.

**Tidak mau bikin user baru?** Boleh saja — pakai user Ubuntu yang Bapak pakai
sekarang (cek namanya dengan `whoami`, misal `ubuntu`). Ganti semua kata
`ispops` di langkah berikutnya dengan nama itu, **dan** ubah dua baris di
`/etc/systemd/system/ispops.service`:

```ini
User=ubuntu
Group=ubuntu
```

Lebih aman tetap pakai user khusus: kalau aplikasi sampai bobol, penyerang tidak
langsung dapat hak user utama Bapak.

### Langkah 1 — salin berkas & pasang dependensi

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx

sudo mkdir -p /opt/isp-ops
sudo unzip isp-ops-v1.0.0.zip -d /tmp          # atau: sudo cp -r isp-ops/* /opt/isp-ops/
sudo cp -r /tmp/isp-ops/* /opt/isp-ops/
sudo chown -R ispops:ispops /opt/isp-ops       # <- baru jalan setelah user ispops ada

cd /opt/isp-ops
sudo -u ispops python3 -m venv venv
sudo -u ispops ./venv/bin/pip install -r requirements.txt
```

### Langkah 2 — isi konfigurasi

```bash
sudo -u ispops cp .env.example .env
sudo -u ispops nano .env            # minimal isi AUTH_PASSWORD
sudo -u ispops chmod 600 .env       # .env berisi password mikrotik
```

### Langkah 3 — jalankan sebagai service

```bash
sudo cp deploy/ispops.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ispops
sudo systemctl status ispops --no-pager
```

### Langkah 4 — (disarankan) nginx sebagai pintu depan

```bash
sudo cp deploy/nginx-ispops.conf /etc/nginx/sites-available/ispops
sudo nano /etc/nginx/sites-available/ispops     # ganti server_name
sudo ln -s /etc/nginx/sites-available/ispops /etc/nginx/sites-enabled/

# PENTING: matikan situs bawaan, kalau tidak dia yang menyambar semua permintaan
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t && sudo systemctl restart nginx
sudo systemctl is-active nginx                  # harus: active
curl -s http://127.0.0.1/health                 # harus menjawab {"status":"ok",...}
```

> Tanpa langkah `rm sites-enabled/default`, browser akan menampilkan
> **"Welcome to nginx!"** — bukan aplikasi. Itu karena situs bawaan menangkap
> semua nama host.

Buka `http://IP-SERVER/` → login → menu **Pengaturan** → *Tambah router* →
isi IP API, user, password → **Tes** → lalu **Sinkron dari mikrotik** untuk
menarik semua PPP secret jadi data pelanggan. Selesai.

Perintah harian:

```bash
sudo systemctl restart ispops     # setelah mengubah .env
sudo journalctl -u ispops -f      # lihat log
```

---

## 4. Coba dulu tanpa mikrotik (mode demo)

Untuk belajar atau demo ke tim:

```bash
cd /opt/isp-ops
DEMO_MODE=true ./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Muncul router tiruan berisi 20 pelanggan yang sesekali naik-turun sendiri.
Tombol Isolir/Buka benar-benar memutus dan menyambungkan sesi di router tiruan itu.
Jangan pakai `DEMO_MODE=true` di produksi.

---

## 5. Cara kerja tombol Isolir

Satu klik = 4 perintah ke mikrotik (semuanya tercatat di menu **Log Aksi**):

```routeros
# 1. sekali saja: buat rule drop di posisi paling atas
/ip firewall filter add chain=forward src-address-list=isolir action=drop \
    place-before=0 comment="ISP-OPS ISOLIR"

# 2. masukkan IP pelanggan ke address-list
/ip firewall address-list add list=isolir address=10.20.3.4 comment="ISP-OPS ISOLIR | budi-santoso"

# 3. matikan PPP secret supaya tidak bisa konek lagi
/ppp secret set [find name=budi-santoso] disabled=yes

# 4. putuskan sesi yang sedang jalan
/ppp active remove [find name=budi-santoso]
```

Tombol **Buka isolir** melakukan kebalikannya (hapus dari address-list + aktifkan
secret), dan status bayar pelanggan ditandai lunas.

Semua perilakunya bisa diatur di `.env`:

| Variabel | Arti | Default |
|---|---|---|
| `ISOLIR_LIST` | nama address-list | `isolir` |
| `ISOLIR_RULE_COMMENT` | penanda rule (jangan diubah kalau rule sudah ada) | `ISP-OPS ISOLIR` |
| `ISOLIR_ACTION` | `drop` (putus total) atau `reject` (lebih cepat terasa error) | `drop` |
| `ISOLIR_TIMEOUT` | mis. `1h` = isolir lepas sendiri setelah 1 jam | kosong = permanen |
| `ISOLIR_DISABLE_SECRET` | ikut matikan PPP secret | `true` |
| `ISOLIR_KICK_ACTIVE` | ikut putuskan sesi berjalan | `true` |
| `ISOLIR_MODE` | `disable` (klasik) atau `suspend` (lihat di bawah) | `disable` |
| `SUSPEND_PROFILE` | nama profil PPP untuk pelanggan suspend | `SUSPENDED` |
| `SUSPEND_POOL` | nama pool IP suspend | `ISP-OPS-SUSPEND` |
| `SUSPEND_SUBNET` | subnet pool suspend | `10.254.254.0/24` |
| `SUSPEND_RULE_COMMENT` | penanda rule firewall subnet suspend | `ISP-OPS SUSPEND` |

> Mau metode lebih lembut? Set `ISOLIR_ACTION=drop` tetap, tapi hapus langkah
> disable secret (`ISOLIR_DISABLE_SECRET=false`) — pelanggan masih bisa login,
> hanya tidak dapat internet. Bisa juga diarahkan ke halaman info bayar dengan
> menambahkan rule `dst-nat` ke IP `isolir` sebelum rule drop.

### Mode suspend (`ISOLIR_MODE=suspend`)

Metode klasik (`disable`) mematikan PPP secret, sehingga CPE pelanggan terus
mencoba login PPPoE dan **membanjiri log MikroTik** dengan percobaan login
gagal. Mode `suspend` mengatasinya dengan cara yang berbeda:

- Secret **tidak** di-disable. Ia dipindah ke profil `SUSPENDED` yang
  mengarah ke pool IP khusus (`10.254.254.0/24`), dan subnet itu di-drop di
  chain forward. Login PPPoE tetap **berhasil** → tidak ada banjir log.
- Blok menempel di **akun**, bukan IP → tidak bocor saat IP pelanggan berubah
  saat reconnect (kelemahan address-list).
- Pool, profil, dan rule firewall dibuat **otomatis** saat isolir pertama kali.
- Pelanggan yang sedang **offline tetap bisa diisolir** (tidak butuh IP).
- Profil asli disimpan di database (kolom `ppp_profile`, dimigrasi otomatis);
  tombol **Buka** mengembalikan profil asli, mengaktifkan secret, lalu
  memutus sesi sekali supaya profil langsung berlaku.

```bash
# aktifkan di .env lalu restart:
ISOLIR_MODE=suspend
```

---

## 6. Remote ONU & router pelanggan

IP ONU tiap merk beda-beda (ZTE/Huawei sering `192.168.100.1`, Fiberhome
`192.168.1.1`, ada juga yang satu subnet dengan IP PPPoE). Karena itu ada 3 cara:

1. **Diisi manual sekali** — menu Pengaturan → kolom *IP ONU*. Setelah tersimpan,
   tombol **Remote ONU** muncul di tabel dan langsung membuka `http://IP-ONU:80`.
2. **Dicarikan otomatis** — tombol **Cari ONU** mencoba IP kandidat (IP tersimpan →
   IP ONU umum → varian oktet terakhir IP PPPoE). Yang port 80-nya terbuka akan
   dinilai: kalau halaman webnya memuat **MAC yang sama dengan MAC PPPoE
   pelanggan**, nilainya tertinggi dan ditandai *Paling cocok*. Centang
   *Scan 1 subnet* untuk menyisir 254 IP (agak lama).
3. **Dibuka lewat server** — tombol **ONU via server**. Berguna kalau web ONU hanya
   bisa dijangkau dari sisi router/jaringan manajemen, atau PC teknisi tidak satu
   jaringan. Semua link di halaman itu otomatis dialihkan agar tetap lewat server.
   Demi keamanan, hanya IP privat yang boleh diakses lewat fitur ini.

> Server Ubuntu harus punya rute ke IP manajemen ONU. Kalau ONU di belakang
> mikrotik pelanggan, tambahkan route di mikrotik pusat ke subnet manajemen ONU,
> atau akses lewat interface ONU (ZTE: `192.168.100.1` biasanya reachable dari
> interface GPON saat ONU menyala).

---

## 7. Nyambung ke aplikasi penagihan yang sudah berjalan

Standarnya status bayar diisi manual. Kalau mau otomatis, aplikasi ini cukup
membaca database penagihan Anda (read-only). Set di `.env`:

```ini
BILLING_ENABLED=true
BILLING_DB_HOST=127.0.0.1
BILLING_DB_USER=billing_ro        # buat user MySQL khusus, hak SELECT saja
BILLING_DB_PASS=xxxx
BILLING_DB_NAME=billing
BILLING_SQL=SELECT customer_code AS code FROM invoices WHERE status='unpaid' AND due_date < CURDATE()
BILLING_CACHE_TTL=300
```

Aturannya sederhana: query harus mengembalikan satu kolom bernama `code` berisi
kode pelanggan yang **masih menunggak**. Cocokkan kode itu dengan kolom
*Kode tagihan* di data pelanggan ISP-OPS. Hasilnya di-cache 5 menit supaya
database penagihan tidak terbebani.

```bash
sudo -u ispops ./venv/bin/pip install pymysql
```

Kalau query gagal (server mati, password salah), aplikasi tetap jalan normal —
status bayar kembali manual, dan kesalahannya terlihat di `/health`.

---

## 8. Keamanan

- Set `AUTH_PASSWORD` di `.env` → aplikasi meminta login lewat halaman `/login` (sesi cookie 12 jam).
  Pasang HTTPS lewat `certbot --nginx -d ops.domainanda.com` agar password tidak
  lewat jaringan dalam bentuk terbuka.
- `ispops.service` mengikat aplikasi ke `127.0.0.1`; akses luar hanya lewat nginx.
- Password mikrotik disimpan di database aplikasi. Kalau mau lebih aman, beri hak
  tulis file hanya ke user `ispops` (`chmod 600 .env`, `chmod 600 ispops.db`) dan
  buat user mikrotik khusus seperti di §2 — jangan pakai user admin.
- Fitur proxy ONU hanya menerima IP privat (10/8, 172.16/12, 192.168/16, 127/8),
  jadi tidak bisa dipakai menembus ke internet.
- Setiap aksi teknisi (siapa, kapan, pelanggan mana, perintah apa) tercatat di
  menu **Log Aksi**.

---

## 9. Kalau ada masalah

| Gejala | Penyebab & solusi |
|---|---|
| `ERROR: Could not find a version that satisfies the requirement librouteros>=4.0` | Versi lama ISP-OPS. Paket baru sudah tidak memakai librouteros sama sekali. Ambil `isp-ops-v1.0.0.zip` terbaru (MD5 `4ea310b9...` atau lebih baru) lalu ulangi pemasangan. |
| `Could not open requirements file: ... 'requirements.txt'` | Folder aplikasi belum berisi berkas (baru `venv` yang terbuat). Isi zip belum tersalin ke `/opt/isp-ops`. Pakai `bash pasang.sh /path/isp-ops-v1.0.0.zip` yang memeriksa tiap langkah, atau periksa dengan `ls /opt/isp-ops` — harus ada `requirements.txt`, `app/`, `templates/`, `static/`. |
| `./venv/bin/pip: No such file or directory` | Bapak tidak sedang berada di folder aplikasi. Jalankan `cd /opt/isp-ops` dulu, atau pakai path penuh: `/opt/isp-ops/venv/bin/pip ...` |
| `chown: invalid user: 'ispops:ispops'` | User `ispops` belum dibuat. Jalankan dulu `sudo useradd -r -m -s /bin/bash ispops`, cek dengan `id ispops`. Atau ganti semua `ispops` dengan user Ubuntu Bapak (`whoami`) + ubah `User=`/`Group=` di `ispops.service`. |
| `systemctl status ispops` → `Failed at step USER` | Sama: user di `User=` belum ada. Buat usernya, atau ganti dengan user yang sudah ada. |
| `sudo: unknown user: ispops` | Sama seperti di atas — `useradd` belum dijalankan. |
| Service `failed`, log menyebut `MappedAnnotationError: Could not interpret annotation Mapped[list['Customer']]` | Bug versi lama di Python 3.8: anotasi `Mapped[list[...]]` dan `Mapped[X \| None]` baru sah di Python 3.9+. Sudah diperbaiki (pakai `typing.List` / `typing.Optional`). Ambil zip terbaru. Ada penjaga otomatisnya: `tests/test_py38_compat.py`. |
| **502 Bad Gateway** dari nginx | nginx hidup tapi tidak bisa menjangkau aplikasi di `127.0.0.1:8000` — artinya aplikasi yang mati. Jalankan `sudo bash cek-status.sh`; skrip itu menelusuri 7 titik dan menyebut mana yang putus. Penyebab tersering: service `ispops` gagal start (lihat `journalctl -u ispops -n 30 --no-pager`). |
| `systemctl is-active nginx` → `failed` | Lihat sebabnya: `sudo nginx -t` (paling jelas), lalu `sudo systemctl status nginx --no-pager`. |
| `nginx: [emerg] "location" directive is not allowed here in /etc/nginx/sites-enabled/default:NN` | Berkas situs bawaan rusak — ada `location` di luar blok `server`. Ini bukan dari ISP-OPS. Buang saja: `sudo rm -f /etc/nginx/sites-enabled/default`, lalu `sudo nginx -t && sudo systemctl enable --now nginx`. Kalau situs itu masih dipakai aplikasi lain, rapikan dulu: `location` wajib berada di dalam `server { ... }`. |
| `nginx: [emerg] bind() to 0.0.0.0:80 failed (98: Address already in use)` | Port 80 dipakai proses lain (biasanya Apache). `sudo ss -tlnp \| grep ':80 '` untuk melihat pelakunya, lalu `sudo systemctl disable --now apache2`. |
| nginx jalan tapi folder `sites-available` tidak ada | nginx bukan dari paket Ubuntu (SNAP/dari sumber). Salin ke `conf.d`: `sudo cp /opt/isp-ops/deploy/nginx-ispops.conf /etc/nginx/conf.d/ispops.conf` lalu `sudo nginx -t && sudo systemctl restart nginx`. |
| Browser menampilkan "Welcome to nginx!" | Situs `default` masih aktif. `sudo rm -f /etc/nginx/sites-enabled/default && sudo systemctl restart nginx` |
| `[SSL: WRONG_VERSION_NUMBER] wrong version number` saat konek ke 8729 | Di mikrotik, service `api` (polos) dipindah ke 8729 dan `api-ssl` invalid. Buka router di menu Pengaturan → **Koneksi API** → *API polos / tanpa TLS*. Cek dengan `cek-mikrotik.py ... --port 8729 --tanpa-tls`. |
| `unknown parameter` saat menekan tombol Isolir | Mikrotik menolak salah satu atribut (`comment`/`timeout`). Aplikasi otomatis mengulang perintah tanpa atribut itu; kalau masih ditolak, buat rule-nya manual: `/ip firewall filter add chain=forward src-address-list=isolir action=drop place-before=0 comment="ISP-OPS ISOLIR"` |
| `failure: no permissions...` saat Isolir | User mikrotik tidak punya hak tulis: `/user group set api policy=read,write,api,test` |
| Kolom MAC kosong padahal pelanggan online | RouterOS itu tidak mengirim `mac-address` di `/ppp active`. Aplikasi mengambilnya dari `/interface pppoe-server print` (diambil sekali per 10 menit, tidak tiap putaran pantau). Kalau tetap kosong, sesi pelanggan bukan PPPoE (PPTP/L2TP tidak punya MAC). |
| `[SSL: WRONG_VERSION_NUMBER] wrong version number` saat konek ke 8729 | Di mikrotik, service `api` (polos) dipindah ke 8729 dan `api-ssl` invalid. Buka router di menu Pengaturan → **Koneksi API** → *API polos / tanpa TLS*. Cek dengan `cek-mikrotik.py ... --port 8729 --tanpa-tls`. |
| Service gagal: `status=226/NAMESPACE` | Server di dalam container/LXC. Hapus baris `PrivateTmp`, `ProtectSystem`, `ProtectHome`, `ReadWritePaths`, `NoNewPrivileges` dari `ispops.service`, lalu `daemon-reload` + `restart`. |
| Kartu router merah "gagal terhubung: timeout" | Port 8728 tidak terbuka / diblokir firewall mikrotik. Cek §2 dan jalankan `tes-koneksi.py`. |
| "login failed" | Username/password salah, atau user belum masuk grup `api`. |
| Tabel kosong padahal mikrotik online | Belum klik **Sinkron dari mikrotik**, atau pelanggan tidak punya PPP secret (mis. pakai PPPoE dari RADIUS). |
| Isolir gagal: "IP pelanggan belum diketahui" | Pelanggan sedang offline dan belum pernah konek sejak data disimpan. Tunggu online sekali, atau isi IP ONU-nya. |
| Status tidak berubah setelah isolir | Rule firewall lain menerima trafik lebih dulu. Cek posisi rule `ISP-OPS ISOLIR` harus di paling atas: `/ip firewall filter print`. |
| Remote ONU tidak bisa dibuka | Server tidak punya rute ke IP ONU, atau port 80 ONU ditutup. Coba tombol **ONU via server**, atau jalankan **Cari ONU**. |
| Mau ganti port/interval | `.env` → `PORT`, `POLL_INTERVAL`; per-router bisa diatur di menu Pengaturan. |

Log aplikasi: `sudo journalctl -u ispops -n 100 --no-pager`

---

## 10. Uji otomatis

46 tes menutup bagian paling rawan: encoding panjang kata protokol API,
login (challenge & plain), urutan perintah isolir/buka terhadap **server
mikrotik tiruan yang berbicara protokol API sungguhan lewat socket**,
penanganan koneksi putus, pencarian ONU, dan seluruh endpoint HTTP.

```bash
cd /opt/isp-ops
./venv/bin/python -m pytest -q
# 46 passed
```

---

## 11. Isi direktori

```
isp-ops/
├── app/
│   ├── main.py            # aplikasi FastAPI, login, halaman
│   ├── routes.py          # semua endpoint API + proxy halaman ONU
│   ├── services.py        # poller pantauan, aksi isolir/buka, log
│   ├── onu.py             # pencarian IP ONU + proxy halaman web
│   ├── billing.py         # integrasi status tagihan (opsional)
│   ├── database.py        # SQLite/MySQL + migrasi ringan
│   ├── models.py          # tabel routers, customers, activity_logs
│   ├── config.py          # semua pengaturan dari .env
│   ├── routeros.py        # protokol RouterOS API (ditulis sendiri, tanpa dependensi)
│   ├── kompat.py          # padanan asyncio.to_thread agar jalan di Python 3.8
│   ├── util.py            # format uptime, MAC, penilaian halaman ONU
│   └── mikrotik/
│       ├── api.py         # ← utama: adapter Mikrotik (pakai app/routeros.py)
│       ├── ssh.py         # ← cadangan: SSH (aktifkan SSH_FALLBACK=true)
│       ├── fake.py        # router tiruan untuk mode demo
│       └── base.py        # antarmuka bersama
├── templates/             # tampilan (index, pengaturan, log)
├── static/                # style.css, app.js (tanpa library luar)
├── tests/                 # 25 tes otomatis
├── deploy/                # ispops.service, nginx-ispops.conf
├── tes-koneksi.py         # periksa koneksi ke mikrotik dari server
├── cek-status.sh          # periksa rantai browser -> nginx -> aplikasi (diagnosis 502)
├── requirements.txt
└── .env.example
```

---

## 12. Beban server

| Jumlah pelanggan | Interval saran | Beban terukur |
|---|---|---|
| < 500 | 5 detik | 3 perintah API per putaran. RAM ±80 MB, CPU < 2%. |
| 1500 | 10 detik | **terukur**: satu putaran pantau penuh 47 ms (puncak 80 ms); `/api/status` dengan 100 baris 6 ms / 39 KB; pencarian 3 ms. RAM ±120 MB. |
| 3000+ | 15–30 detik | Naikkan interval; aplikasi hanya menulis ke SQLite kalau ada perubahan. |

Angka di atas diukur dengan router tiruan berisi 1500 pelanggan
(`tests/test_skala.py`). Yang membebani bukan aplikasinya melainkan browser:
karena itu tabel dipotong (pemilih **Baris** di kanan atas, bawaan 100) dan
statistik tetap dihitung dari semua pelanggan.

Catatan untuk jaringan besar:
- Kolom **Uptime** dan **Status** diambil dari `/ppp active` — sekali perintah
  untuk semua pelanggan, bukan per pelanggan.
- `last_seen` hanya ditulis ulang kalau beda lebih dari 1 menit, jadi 1500 baris
  tidak ditulis ke disk tiap 10 detik.
- Interval bisa diatur **per router** di menu Pengaturan, jadi mikrotik besar
  bisa 15 detik sementara mikrotik kecil tetap 5 detik.

Satu hal yang perlu diingat: jangan memasang lebih dari satu proses
(`--workers 1` sudah benar). Kalau perlu beberapa server, pisahkan per mikrotik.

## Beberapa teknisi (login per nama)

Isi `.env`:

```
AUTH_USER=heru            # pemilik / admin
AUTH_PASSWORD=...
TECH_USERS=andi:pass123, budi:pass456, catur:pass789, dodi:pass012
```

> `AUTH_USER`/`TECH_USERS` hanya dipakai sebagai **seed awal** (saat tabel
> pengguna masih kosong). Setelah itu semua akun dikelola dari
> **Pengaturan → Pengguna aplikasi** (tambah/hapus, peran admin/teknisi).
> Tombol **Keluar** ada di pojok kanan atas.

- Akun `AUTH_USER` berperan **admin**: semua menu (Monitoring, Pengaturan, Log Aksi, Isolir/Buka, sinkron).
- **Auto-isolir** (khusus admin): Pengaturan → panel "Auto-isolir pelanggan" →
  cari nama PPPoE → isi tanggal bila perlu → Aktifkan.
  * Tanpa tanggal: langsung diisolir otomatis setiap kali terlihat online.
  * Dengan tanggal: isolir baru aktif **2 hari setelah tanggal** yang ditentukan
    (masa tenggang; bisa diubah lewat `AUTO_ISOLIR_GRACE_DAYS` di `.env`).
  * Lainnya tetap isolir manual lewat tombol (default).
- Peran ketiga **pemantau**: hanya melihat Monitoring (tidak bisa menambah klien, tidak bisa isolir); dibuat lewat menu Pengaturan.
- **Teknisi** boleh: melihat Monitoring, menambah klien (PPPoE), dan isolir/buka isolir.
- Akun `TECH_USERS` berperan **teknisi**: hanya melihat Monitoring dan menambah
  klien (tombol **+ Tambah PPPoE**). Menu Pengaturan/Log Aksi dan tombol
  Isolir tidak tampil; endpointnya juga dijaga server (403).
- Log Aksi mencatat nama akun yang menekan tombol.
