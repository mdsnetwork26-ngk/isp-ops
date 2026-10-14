# ISP-OPS — Mode Suspend Isolir

Isi paket: `app/`, `tests/`, `README.md`, `requirements.txt` — **tanpa dependency baru**.

Yang berubah: mode isolir baru `suspend` — secret PPP dipindah ke profil
`SUSPENDED` (bukan di-disable) supaya login PPPoE tetap berhasil dan **tidak
ada banjir log login gagal** dari CPE yang redial terus; blok menempel di akun,
bukan IP. Klien yang sedang offline tetap bisa diisolir. Kolom baru
`customers.ppp_profile` (dimigrasi otomatis saat startup). Default tetap
`disable` — perilaku lama tidak berubah selama env tidak diubah.

## Langkah deploy (dua instance: container `ispops-munir` + native 8101)

```bash
# 1. upload isp-ops-suspend.tar.gz ke server, lalu backup kode lama
tar -czf /opt/isp-ops-BACKUP-$(date +%F-%H%M).tar.gz -C /opt/isp-ops app

# 2. timpa dengan isi paket
tar -xzf isp-ops-suspend.tar.gz -C /opt/isp-ops

# 3. nyalakan mode suspend pada env yang dipakai instance
echo 'ISOLIR_MODE=suspend' >> /opt/isp-ops/.env
#    (cek file env yang benar-benar dipakai container:)
docker inspect ispops-munir --format '{{json .Config.Env}}'

# 4. restart kedua instance
docker restart ispops-munir
pgrep -af 'uvicorn|8101'    # instance native: restart unit/prosesnya

# 5. buka UI dan tekan Ctrl+F5
```

## Verifikasi pasca-deploy

Isolir satu pelanggan uji, lalu di MikroTik:

```
/ppp secret print detail where name=<user>
   -> profile=SUSPENDED, disabled=false
/ppp profile print where name=SUSPENDED
   -> remote-address=ISP-OPS-SUSPEND
/ip pool print where name=ISP-OPS-SUSPEND
   -> ranges=10.254.254.2-10.254.254.254
/ip firewall filter print where comment="ISP-OPS SUSPEND"
   -> chain=forward src-address=10.254.254.0/24 action=drop
```

- Log ISP-OPS memuat: `profil asli '<x>' tersimpan; secret dipindah ke profil
  'SUSPENDED'`.
- Tombol **Buka** mengembalikan `profile=<x>`, `disabled=false`, sesi diputus
  sekali agar profil langsung berlaku.
- Log MikroTik tidak lagi berisi banjir login PPPoE gagal untuk user tersebut.

## Rollback / catatan penting

- Kode baru tetap mendukung mode lama: cukup set `ISOLIR_MODE=disable` dan
  restart — tidak wajib mengembalikan kode backup.
- **Sebelum mengganti mode kembali ke `disable`, buka dulu semua pelanggan yang
  sedang di-suspend.** Tombol Buka pada mode `disable` tidak memulihkan profil;
  kalau terlanjur, pulihkan manual di MikroTik:
  `/ppp secret set [find name=<user>] profile=<profil-asli>`
  (profil asli tercatat di kolom `ppp_profile` tabel `customers`).
- Pelanggan yang diisolir dengan mode lama (disable) tetap bisa dibuka normal
  setelah ganti mode suspend (secret otomatis diaktifkan lagi).
