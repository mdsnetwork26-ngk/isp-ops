# Menjalankan ISP-OPS sebagai layanan komunitas

Model bisnis: pelanggan berlangganan = **akses penuh semua fitur**; biaya
tambahan hanya **per router** (kuota). Masa aktif per tanggal (mis. 1 tahun);
lewat tenggat = expired.

## Arsitektur (satu VPS kecil cukup)

```
VPS ber-IP publik
├─ hub VPN (OpenVPN: tools/vpn, atau sstp-server)  <- router pelanggan dial-out
├─ cloudflared: wildcard *.DOMAIN + entri per tenant
├─ panel operator (port 9000, internal saja)       <- module panel/
└─ satu container isp-ops per pelanggan            <- isolasi total
```

Tidak perlu aplikasi manajemen terpisah: **panel operator** ada di repo ini
(module `panel/`), database sendiri (`operator.db`), tidak menyentuh data
tenant mana pun.

## Menjalankan panel

```bash
OPERATOR_PASSWORD='rahasia' OPERATOR_DOMAIN=marinagih.my.id \
OFFICE_PUBLIC=202.10.3.4 \
python3 -m uvicorn panel.main:app --host 127.0.0.1 --port 9000
```

Buka http://127.0.0.1:9000 (basic auth). Jangan diekspos ke internet.

## Alur pelanggan baru (contoh: Munir, mulai hari ini, 1 tahun, 1 router)

0. Sekali saja: bangun image tenant —
   `cd /opt/isp-ops && sudo docker build -t isp-ops:latest .`
1. Panel → buat tenant: nama `Munir`, subdomain `munir`, kuota router `1`,
   masa aktif `365`.
2. Buka tautan **provisioning** tenant → salin tiga blok:
   - `docker-compose` (simpan sebagai
     `/opt/tenants/<sub>/docker-compose.yml`, container isp-ops tenant,
     PORT otomatis 8101+, `ROUTER_LIMIT` terisi, password admin ter-generate),
   - ingress cloudflared (`munir.DOMAIN` + `*.munir.DOMAIN` untuk onu-*),
   - onboarding VPN router pelanggan (`tools/vpn/add-site.sh munir ...`
     lalu tempel blok Mikrotik ke router pelanggan).
3. Pelanggan membuka `https://munir.marinagih.my.id` dengan kredensial yang
   tercetak di panel — semua fitur langsung tersedia.

## Perpanjangan & kuota

- **Perpanjang**: tombol di panel (+N hari); tenant expired yang diperpanjang
  aktif lagi. Sweep otomatis menandai `expired` saat tenggat lewat — container
  tenant yang expired wajib Anda `docker stop` (langkah manual yang aman).
- **Tambah router berbayar**: naikkan `router_limit` di compose tenant lalu
  `docker compose up -d`; aplikasi tenant menolak menambah router melewati
  kuota dengan pesan yang ramah (`ROUTER_LIMIT`).
- **Suspend/aktifkan/hapus**: tombol di panel.

## Catatan operasional

- Satu VPS 2 vCPU/2 GB nyaman untuk 10-20 tenant; lebih dari itu tambah VPS.
- Backup: `operator.db` + `tenants/*/data/` (db tiap tenant).
- Hub VPN: OpenVPN (`tools/vpn`) kompatibel RouterOS lama; SSTP server
  (`sstp-server`) pilihan setara bila ingin onboarding satu baris
  `/interface sstp-client` seperti provider lain.
