# ISP-OPS — Mode Suspend Isolir (profil buatan operator)

Alur isolir mode `suspend` — **tanpa dependency baru**:

**Isolir:** profil asal klien disimpan di database → secret PPPoE dipindah
ke **profil suspend yang sudah dibuat manual oleh operator di MikroTik** →
**sesi PPPoE aktif dihapus** (`/ppp active remove`) supaya klien login ulang
dan berpindah ke profil suspend.

**Buka isolir:** secret dikembalikan ke **profil asal** → **sesi PPPoE aktif
dihapus lagi** supaya klien login ulang dan lepas dari profil suspend.

Perbedaan penting dari versi sebelumnya:

- Aplikasi **tidak lagi membuat** pool `ISP-OPS-SUSPEND`, profil, atau
  rule firewall sendiri. Pool / rate-limit / halaman isolir sepenuhnya
  milik profil buatan operator.
- Kalau profil suspend tidak ditemukan di router, isolir **dibatalkan**
  dengan pesan yang menyebut profil-profil yang tersedia. Tidak ada yang
  diubah di MikroTik maupun database.
- Penghapusan sesi PPPoE aktif di mode suspend **selalu** dilakukan
  (Isolir maupun Buka), tidak lagi tergantung `ISOLIR_KICK_ACTIVE`.
- Profil hasil pemindahan **diverifikasi** dengan dibaca ulang dari router.

## 1. Prasyarat di MikroTik (sekali)

Profil suspend harus sudah ada di MikroTik. Profil yang dipakai
instalasi ini **sudah dibuat dan terverifikasi ada**:

```routeros
/ppp profile print where name=SUSPEND
# 0 ;;; Profile for Isolated Clients (ROS v6)
#   name="SUSPEND" local-address=10.200.0.1 remote-address=POOL-SUSPEND
#   rate-limit="1k/1k" address-list=SUSPEND-LIST
```

Artinya: klien yang secret-nya dipindah ke profil ini akan dapat IP dari
`POOL-SUSPEND`, kena rate-limit `1k/1k`, dan IP-nya otomatis masuk
address-list `SUSPEND-LIST` oleh RouterOS. Aplikasi tidak membuat pool,
profil, maupun rule apa pun — ia hanya memindah profil secret ke sini.

Namanya **persis** `SUSPEND` (peka huruf besar/kecil), dan itu sudah
menjadi bawaan aplikasi (`SUSPEND_PROFILE=SUSPEND`).

## 2. Langkah deploy

```bash
# 1. backup kode lama
tar -czf /opt/isp-ops-BACKUP-$(date +%F-%H%M).tar.gz -C /opt/isp-ops app

# 2. timpa dengan kode baru (atau git pull bila dari repo ini)

# 3. nyalakan mode suspend pada env yang dipakai instance
#    (cek file env yang benar-benar dipakai container:)
docker inspect ispops-munir --format '{{json .Config.Env}}'
cat >> /opt/isp-ops/.env <<'EOF'
ISOLIR_MODE=suspend
SUSPEND_PROFILE=SUSPEND
EOF
#    ^^^ sesuaikan dengan nama profil buatan Anda (saat ini: SUSPEND).
#    Baris lama SUSPEND_POOL / SUSPEND_SUBNET / SUSPEND_RULE_COMMENT
#    boleh dihapus: sudah tidak dipakai.

# 4. restart kedua instance (container ispops-munir + native 8101)
docker restart ispops-munir
pgrep -af 'uvicorn|8101'    # instance native: restart unit/prosesnya

# 5. buka UI dan tekan Ctrl+F5
```

## 3. Verifikasi pasca-deploy

Isolir satu pelanggan uji, lalu di MikroTik:

```routeros
/ppp secret print detail where name=<user>
#   -> profile=<profil-suspend-anda>, disabled=false
/ppp active print where name=<user>
#   -> sesi lama hilang; sesi baru (bila CPE sudah redial) memakai
#      address dari pool profil suspend Anda
```

- Log ISP-OPS (menu Log) memuat: `profil asal '<x>' tersimpan; secret
  dipindah ke profil suspend '<nama>'` dan `... sesi PPPoE aktif dihapus
  -- klien login ulang ke profil suspend '<nama>'`.
- Tombol **Buka** memuat: `profil dikembalikan ke profil asal '<x>'` dan
  `... sesi PPPoE aktif dihapus -- klien login ulang dengan profil asal,
  lepas dari profil suspend '<nama>'`, lalu di MikroTik
  `profile=<x>, disabled=false`.
- Log MikroTik tidak lagi berisi banjir login PPPoE gagal untuk user itu.
- Uji negatif: ganti nama profil di WinBox sementara, tekan Isolir —
  harus gagal dengan pesan `profil suspend '...' tidak ditemukan di
  router (profil tersedia: ...)` dan status pelanggan tidak berubah.

## 4. Halaman isolir (opsional, sekali di MikroTik)

Supaya klien di profil `SUSPEND` **dialihkan ke halaman pesan isolir**
(bukan sekadar lemot 1k), aplikasi menyediakan halaman publik `/isolir`.
Isi `.env` bila ingin ada tombol kontak, lalu restart aplikasi:

```bash
ISOLIR_WA=6281234567890
ISOLIR_KONTAK=Admin MDS Network
APP_HOSTS=192.168.38.2,monitoring.marinagih.my.id
```

`APP_HOSTS` di atas memuat kedua alamat resmi instalasi ini — IP lokal
`192.168.38.2` dan domain admin `monitoring.marinagih.my.id` — dan
menyalakan pengalihan otomatis berdasarkan Host: klien
suspend yang membuka situs apa saja — halaman depannya sekalipun — akan
langsung dibelokkan server ke `/isolir`, karena Host situs itu jelas
bukan host ISP-OPS. Tanpa baris ini pengalihan hanya bekerja untuk URL
uji portal dan jalur yang tidak bertabrakan dengan halaman aplikasi.

Lalu jalankan sekali skrip `mikrotik-suspend-redirect.rsc` di MikroTik
— di dalamnya `serverIp` sudah diisi `192.168.38.2` (server ISP-OPS
instalasi ini); tinggal pastikan `serverPort`-nya: 80 bila lewat nginx,
atau port aplikasi Anda bila diakses langsung. Skrip itu, secara idempoten:

1. `dst-nat` tcp 80 dari `SUSPEND-LIST` ke server ISP-OPS — aplikasi
   mengenali IP pelanggan terisolir dan menampilkan namanya di halaman;
   URL uji konektivitas OS ikut diarahkan ke sana sehingga HP memunculkan
   halaman ini sebagai portal.
2. Mengizinkan DNS (udp/tcp 53) dan akses ke server halaman itu.
3. Men-drop forward lain dari `SUSPEND-LIST` (HTTPS tidak di-`dst-nat`
   supaya tidak memicu peringatan sertifikat di browser).
4. Pengecualian `srcnat` (`ISP-OPS SUSPEND NO-NAT`): lalu lintas ke
   server isolir TIDAK di-masquerade, supaya IP asli klien (`10.200.0.x`)
   terbaca server dan halaman bisa menampilkan nama pelanggannya. Tanpa
   ini IP sumber berubah jadi IP router, dan halaman tampil umum tanpa
   nama (aplikasi juga sudah berjaga: jalur asing apa pun kini diarahkan
   ke `/isolir`, bukan ke halaman login operator).

Verifikasi: isolir pelanggan uji, hubungkan HP ke WiFi-nya, buka browser
atau tunggu portalnya muncul — harus tampil *"Internet Sedang Dalam Masa
Isolir"*. Membatalkannya cukup hapus rule berkomentar `ISP-OPS SUSPEND`
(nat + filter); profil `SUSPEND` dan aplikasi tidak terpengaruh.

## 5. Rollback / catatan penting

- Kembali ke mode lama cukup dengan `ISOLIR_MODE=disable` dan restart —
  tidak wajib mengembalikan kode backup.
- **Sebelum mengganti mode kembali ke `disable`, buka dulu semua
  pelanggan yang sedang berada di profil suspend.** Tombol Buka pada
  mode `disable` tidak memulihkan profil; kalau terlanjur, pulihkan
  manual di MikroTik:
  `/ppp secret set [find name=<user>] profile=<profil-asli>`
  (profil asli tercatat di kolom `ppp_profile` tabel `customers`),
  lalu putus sesinya: `/ppp active remove [find name=<user>]`.
- Pelanggan yang diisolir dengan mode lama (disable) tetap bisa dibuka
  normal setelah ganti ke mode suspend: secret diaktifkan lagi dan sisa
  entri address-list dibersihkan otomatis.
- Bila profil asal seorang pelanggan sudah terhapus dari router, tombol
  Buka akan gagal dan **catatan profil asalnya tidak dihapus**. Buat
  lagi profil itu di MikroTik (atau perbaiki kolom `ppp_profile`), lalu
  tekan Buka lagi. Jalan terakhir bila profil asal memang tidak tercatat
  sama sekali: `SUSPEND_FALLBACK_PROFILE` (bawaan `default`), selalu
  ditandai `PERINGATAN` di log.
- Pool/profil/rule buatan versi lama (`ISP-OPS-SUSPEND`,
  `10.254.254.0/24`, rule berkomentar `ISP-OPS SUSPEND`) tidak lagi
  disentuh aplikasi. Bila tidak dipakai profil buatan Anda, boleh
  dihapus manual dari MikroTik setelah semua pelanggan dibuka isolirnya.
