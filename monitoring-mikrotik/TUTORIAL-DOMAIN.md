# Petunjuk: Mengonlinekan ISP-OPS di `monitoring.marinagih.my.id`

Kondisi Anda (sudah dikonfirmasi):

- Domain `marinagih.my.id` dari **Rumahweb**, DNS-nya sudah dikelola di **Cloudflare**
  (aplikasi taginet sudah online di `marinagih.my.id` lewat Cloudflare Tunnel).
- ISP-OPS berjalan di server `192.168.38.2`, port `8000` (hanya mendengarkan localhost).

Karena taginet dan ISP-OPS berada di **satu server yang sama**, cara paling
praktis adalah **menumpang tunnel taginet**: satu tunnel, dua hostname.

Tunnel Cloudflare ada dua jenis, dan tempat mengedit route-nya berbeda:

| Jenis | Ciri | Route diedit di |
|---|---|---|
| **A. Dikelola dashboard** (token) | `systemctl cat cloudflared` menampilkan `tunnel run --token eyJ...` | Dashboard Cloudflare (Zero Trust → Tunnels → Public Hostname) |
| **B. Dikelola CLI** (cert.pem) | `cloudflared tunnel list` berhasil; ada `/etc/cloudflared/config.yml` | File `config.yml` di server |

### Kenali dulu jenis tunnel taginet

```bash
sudo cloudflared tunnel list              # berhasil  -> jenis B
systemctl cat cloudflared | grep ExecStart  # ada --token -> jenis A
```

> Bila `cloudflared tunnel list` (tanpa sudo) error *"Cannot determine default
> origin certificate path / No file cert.pem"*, coba dulu dengan `sudo`
> (cert-nya mungkin di `/root/.cloudflared/`). Kalau dengan sudo tetap error,
> hampir pasti tunnel Anda jenis A.

---

## Jenis A: tunnel token dari dashboard

> **Kasus nyata di server Anda (14 Sep 2026):** `cloudflared` berjalan sebagai
> **dua proses token di dalam Docker** (user `65532`, tidak ada
> `cloudflared.service`). Untuk setup seperti ini ada jebakan: `localhost` di
> dalam container BUKAN localhost server, sementara ISP-OPS hanya mendengarkan
> `127.0.0.1`. Menambah hostname `localhost:8000` ke tunnel container tidak akan
> tersambung. Karena itu pakai **A1 (tunnel baru native)** di bawah.

### A1 (disarankan untuk server ini): tunnel baru, cloudflared native di host

1. Dashboard → **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel**
   → **Cloudflared** → nama `ispops` → salin token.
2. Pasang cloudflared di host (bukan di container):

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb
sudo cloudflared service install <TOKEN>
sudo systemctl status cloudflared      # active (running)
```

3. Tunnel `ispops` → **Public Hostname** → **Add a public hostname**:
   subdomain `monitoring`, domain `marinagih.my.id`, Type `HTTP`,
   URL `localhost:8000` → **Save**.
4. Uji: `curl -I https://monitoring.marinagih.my.id/login` → `HTTP/2 200`.

Tunnel taginet yang di Docker tidak tersentuh sama sekali.

### A2: menumpang tunnel container yang sudah ada (perlu ubah bind)

Hanya bila tidak ingin memasang cloudflared baru:

1. Ubah `--host 127.0.0.1` menjadi `--host 192.168.38.2` di
   `/etc/systemd/system/ispops.service`, lalu
   `sudo systemctl daemon-reload && sudo systemctl restart ispops`.
2. Di dashboard, tambahkan public hostname ke tunnel taginet dengan URL
   `192.168.38.2:8000` (bukan `localhost:8000`).

Konsekuensi: aplikasi bisa diakses dari seluruh jaringan lokal.

### A3: tambah hostname lewat dashboard (tunnel native dashboard-managed)

1. Buka [dash.cloudflare.com](https://dash.cloudflare.com) → **Zero Trust** →
   **Networks** → **Tunnels**.
2. Klik tunnel taginet → tab **Public Hostname** → **Add a public hostname**.
3. Isi:
   - Subdomain: `monitoring`
   - Domain: `marinagih.my.id`
   - Service → Type: `HTTP`
   - URL: `localhost:8000`
4. **Save** — langsung aktif, tanpa restart apa pun di server.

---

## Jenis B: tambah hostname lewat config.yml di server

### 1. Cari nama tunnel dan file konfigurasi yang dipakai

```bash
cloudflared tunnel list                 # catat NAMA tunnel taginet
systemctl cat cloudflared | grep ExecStart   # lihat path config.yml yang dipakai
sudo cat /etc/cloudflared/config.yml    # biasanya di sini
```

### 2. Cadangkan, lalu tambah satu hostname

```bash
sudo cp /etc/cloudflared/config.yml /etc/cloudflared/config.yml.bak
sudo nano /etc/cloudflared/config.yml
```

Tambahkan dua baris `monitoring` **di atas** baris `service: http_status:404`
(baris 404 wajib tetap paling bawah):

```yaml
ingress:
  - hostname: marinagih.my.id            # <-- punya taginet, JANGAN diubah
    service: http://localhost:PORT-TAGINET
  - hostname: monitoring.marinagih.my.id # <-- TAMBAHAN untuk ISP-OPS
    service: http://localhost:8000
  - service: http_status:404
```

### 3. Cek aturan valid

```bash
cloudflared tunnel ingress validate --config /etc/cloudflared/config.yml
```

### 4. Daftarkan DNS (otomatis, tanpa buka dashboard)

```bash
cloudflared tunnel route dns <NAMA-TUNNEL-TAGINET> monitoring.marinagih.my.id
```

Ini membuat CNAME `monitoring` di Cloudflare secara otomatis.

### 5. Restart tunnel

```bash
sudo systemctl restart cloudflared
```

### 6. Uji

```bash
curl -I https://monitoring.marinagih.my.id/login   # harus HTTP/2 200
curl -I https://marinagih.my.id                    # pastikan taginet tetap hidup
```

Lalu buka `https://monitoring.marinagih.my.id` lewat internet (bukan WiFi
kantor) → muncul halaman login ISP-OPS.

> Catatan: kedua aplikasi satu server, jadi `http://localhost:8000` langsung
> tersambung — tidak perlu mengubah bind ISP-OPS.

---

## Alternatif: tunnel terpisah untuk ISP-OPS

Hanya perlu bila Anda ingin benar-benar memisahkan keduanya (misal nanti
ISP-OPS pindah server). Di server yang sama, service bawaan cloudflared hanya
membaca satu `/etc/cloudflared/config.yml`, jadi tunnel kedua butuh unit
systemd sendiri dengan `--config /etc/cloudflared/ispops.yml` — komponen
bertambah tanpa keuntungan nyata selama keduanya serumah.

---

## Remote ONU dari luar kantor (subdomain wildcard)

Supaya tombol **Remote ONU** bekerja dari internet (pola yang sama dengan
`10-20-20-3.mdsgetas.my.id`), tambahkan **satu public hostname wildcard** di
tunnel yang sama:

1. Dashboard → Zero Trust → Networks → Tunnels → tunnel Anda →
   **Public Hostname** → **Add a public hostname**.
2. Isi:
   - Subdomain: `*`   (hanya bintang — Cloudflare tidak menerima `*.onu`)
   - Domain: `marinagih.my.id`
   - Service Type: `HTTP` · URL: `localhost:8000`
3. **Save**. Pastikan entri `monitoring` tetap berada DI ATAS entri `*`
   (aturan dibaca urut; yang spesifik harus dulu).

Setelah itu tombol Remote ONU otomatis membuka:

```
https://onu-10-20-20-3.marinagih.my.id/          (port 80)
https://onu-10-20-20-3-8080.marinagih.my.id/     (port 8080)
```

Tanpa wildcard ini, Remote ONU tetap memakai jalur cadangan `/proxy/...`
(tombol "ONU via server"), yang juga sudah diperbaiki. Cookie sesi berlaku
untuk semua subdomain, jadi halaman ONU tetap terlindungi login aplikasi.

## Remote ONU situs luar via VPN L2TP (port-mapping publik)

Bila VPN provider hanya mem-forward port tertentu (bukan *full-tunnel*), LAN
situs luar tidak terute ke server kantor -- `curl http://IP-ONU/` dari server
akan *timeout*, padahal monitoring API hidup lewat mapping `…:45536`.

### Otomatis (disarankan): NAT sementara dibuat aplikasi

1. Pesan ke provider **sekali saja**: forward satu RENTANG port ke router
   situs luar, mis. `45600-45699`.
2. Di server, isi `.env`:
   ```
   ONU_NAT_PORTS=45600-45699
   ONU_NAT_TTL=600
   ```
   lalu `sudo systemctl restart ispops`.
3. Selesai. Begitu tombol **Remote ONU** pelanggan situs luar diklik,
   aplikasi lewat API router membuat rule dst-nat (port pool -> IP ONU),
   proxy menyambung lewat `host-router:port`, dan rule **dihapus otomatis**
   bila 10 menit (ONU_NAT_TTL) tidak ada aktivitas. Sisa rule setelah restart
   aplikasi dibersihkan saat startup. Rule berpenanda comment `ispops-onu:`
   sehingga mudah dikenali/diaudit di Winbox.

Catatan keamanan: IP publik hanya boleh menjadi target proxy bila terdaftar
sebagai host router (Pengaturan → Router); IP publik lain tetap 403.

### Manual: satu port per ONU

Bila tidak memakai fitur otomatis: pesan satu port publik per ONU (mis.
45580) dan buat NAT permanen di router situs luar:

```
/ip firewall nat add chain=dstnat protocol=tcp port=45580 \
    action=dst-nat to-addresses=10.11.11.35 to-ports=80
/ip firewall filter add chain=forward protocol=tcp \
    dst-address-list=onu-remote action=accept place-before=0
/ip firewall address-list add list=onu-remote address=10.11.11.35
```

lalu buka `https://onu-165-99-95-106-45580.marinagih.my.id/`.

Jangka panjang tetap lebih nyaman *full-tunnel* (jika provider sanggup
merutekan subnet): semua tombol Remote ONU bekerja otomatis tanpa NAT sama
sekali.

## Sebelum dibuka ke internet (wajib)

1. **Ganti password admin** yang kuat di `.env` (`AUTH_PASSWORD=...`), lalu
   `sudo systemctl restart ispops`. Halaman login Anda kini bisa diakses siapa
   pun dari internet.
2. Password teknisi (`TECH_USERS` awal / dibuat dari menu Pengaturan) juga
   diganti yang tidak mudah ditebak.
3. HTTPS sudah otomatis dari Cloudflare — password login aman di jalan.
4. Optional: batasi negara/IP yang boleh membuka lewat dashboard Cloudflare →
   Security → WAF → custom rule (misal hanya Indonesia).

## Bila cloudflared berjalan di Docker (kasus 502 pada wildcard)

`localhost` di dalam container BUKAN server Anda. Ada dua jalan:

1. **Pindahkan wildcard ke tunnel yang sama dengan `monitoring`** (tunnel yang
   terbukti bisa mengakses aplikasi). Hapus entri `*` dari tunnel lain, lalu
   tambah lagi di tunnel `monitoring` dengan URL `localhost:8000`.
2. **Atau buat keduanya memakai IP LAN**: ubah bind aplikasi di
   `/etc/systemd/system/ispops.service` dari `--host 127.0.0.1` menjadi
   `--host 192.168.38.2`, lalu `sudo systemctl daemon-reload && sudo systemctl
   restart ispops`; kemudian isi URL service entri `*` (dan bila perlu
   `monitoring`) dengan `http://192.168.38.2:8000`. Aman: port 8000 tetap
   hanya reachable dari jaringan lokal, internet tetap lewat tunnel.

Cek cepat dari dalam container:
`docker exec <nama-container-cloudflared> wget -qO- http://192.168.38.2:8000/health`

## Masalah umum

| Gejala | Solusi |
|---|---|
| `502 Bad Gateway` | Service ispops mati: `sudo systemctl status ispops` |
| `onu-*` 502 (Cloudflare "Host Error") padahal `monitoring` normal | Entri wildcard `*` berada di tunnel/container yang `localhost`-nya bukan server ini (atau URL-nya salah port). Pindahkan `*` ke tunnel yang sama dengan `monitoring` (URL `localhost:8000`), atau ganti URL-nya ke `http://192.168.38.2:8000`. Urutan entri: yang spesifik (`monitoring`) di atas `*`. |
| `error 1033` | Tunnel belum jalan: `sudo systemctl status cloudflared` |
| Halaman lama/berantakan | Cache browser: Ctrl+F5 |
| `curl` gagal, browser bisa | DNS belum propagasi: tunggu beberapa menit |
| Wildcard `*` kena 502 padahal `monitoring` hidup | Wildcard tertambah di tunnel/container yang tidak bisa menjangkau `localhost:8000`. Pindahkan entri `*` ke tunnel yang sama dengan `monitoring`; bila tetap 502, ubah bind aplikasi ke IP LAN dan isi URL service `*` dengan `http://192.168.38.2:8000` (lihat bagian "Bila cloudflared berjalan di Docker") |
