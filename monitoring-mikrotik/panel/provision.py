"""Logika bisnis panel operator: masa aktif, kuota, provisioning tenant.

Model bisnis: pelanggan berlangganan = akses PENUH semua fitur; biaya
tambahan hanya per router (router_limit). Masa aktif per tanggal; lewat
tenggat container dianggap expired (operator tinggal stop / perpanjang).
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta

from .models import SessionOp, Tenant

PORT_AWAL = 8101  # 8000 dipakai isp-ops operator/host


def _password(n: int = 12) -> str:
    abc = string.ascii_letters + string.digits
    return "".join(secrets.choice(abc) for _ in range(n))


def next_port(db) -> int:
    top = db.query(Tenant.port).order_by(Tenant.port.desc()).first()
    return max(PORT_AWAL, (top[0] if top else PORT_AWAL - 1) + 1)


def create_tenant(name: str, subdomain: str, router_limit: int = 1,
                  days: int = 365, notes: str = "") -> Tenant:
    db = SessionOp()
    try:
        t = Tenant(
            name=name.strip(),
            subdomain=subdomain.strip().lower(),
            port=next_port(db),
            admin_user="admin",
            admin_pass=_password(),
            started_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=days),
            router_limit=int(router_limit),
            notes=notes.strip(),
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return t
    finally:
        db.close()


def extend_tenant(tenant_id: int, days: int = 365) -> Tenant:
    """Perpanjang masa aktif; tenant expired yang diperpanjang aktif lagi."""
    db = SessionOp()
    try:
        t = db.get(Tenant, tenant_id)
        if t is None:
            raise ValueError("tenant tidak ditemukan")
        dasar = max(datetime.utcnow(), t.expires_at)
        t.expires_at = dasar + timedelta(days=days)
        if t.status == "expired":
            t.status = "active"
        db.commit()
        db.refresh(t)
        return t
    finally:
        db.close()


def set_status(tenant_id: int, status: str) -> Tenant:
    if status not in ("active", "suspended"):
        raise ValueError("status hanya active/suspended")
    db = SessionOp()
    try:
        t = db.get(Tenant, tenant_id)
        if t is None:
            raise ValueError("tenant tidak ditemukan")
        t.status = status
        db.commit()
        db.refresh(t)
        return t
    finally:
        db.close()


def remove_tenant(tenant_id: int) -> None:
    db = SessionOp()
    try:
        t = db.get(Tenant, tenant_id)
        if t is not None:
            db.delete(t)
            db.commit()
    finally:
        db.close()


def sweep_expired() -> list:
    """Tandai tenant lewat tenggat sebagai expired. Dipanggil tiap render panel;
    di produksi, systemd timer / loop juga boleh memanggilnya."""
    db = SessionOp()
    try:
        kini = datetime.utcnow()
        lewat = (db.query(Tenant)
                 .filter(Tenant.status == "active", Tenant.expires_at < kini)
                 .all())
        nama = [t.subdomain for t in lewat]
        for t in lewat:
            t.status = "expired"
        db.commit()
        return nama
    finally:
        db.close()


def days_left(t: Tenant) -> int:
    return (t.expires_at - datetime.utcnow()).days


# ---------------------------------------------------------------- teks siap pakai

def tenant_fqdn(subdomain: str, default_domain: str) -> str:
    """Jika tenant pakai domain sendiri (ada titik, misal 'munir.my.id'), pakai itu.
    Jika hanya label (misal 'munir'), gabungkan dengan default DOMAIN operator."""
    sub = subdomain.strip().lower()
    return sub if "." in sub else f"{sub}.{default_domain}"


def compose_snippet(t: Tenant, domain: str) -> str:
    """Potongan docker-compose untuk container isp-ops tenant."""
    slug = t.subdomain.replace(".", "-")
    return (
        f"  ispops-{slug}:\n"
        f"    image: isp-ops:latest\n"
        f"    container_name: ispops-{slug}\n"
        f"    restart: unless-stopped\n"
        f"    environment:\n"
        f"      HOST: 0.0.0.0\n"
        f"      PORT: {t.port}\n"
        f"      AUTH_USER: {t.admin_user}\n"
        f"      AUTH_PASSWORD: {t.admin_pass}\n"
        f"      ROUTER_LIMIT: {t.router_limit}\n"
        f"      DATABASE_URL: sqlite:////data/ispops.db\n"
        f"    ports:\n"
        f"      - \"{t.port}:{t.port}\"\n"
        f"    volumes:\n"
        f"      - ./data:/data\n"
        f"      - /opt/isp-ops/app:/opt/isp-ops/app:ro\n"
        f"      - /opt/isp-ops/static:/opt/isp-ops/static:ro\n"
        f"      - /opt/isp-ops/templates:/opt/isp-ops/templates:ro\n"
    )


def tunnel_snippet(t: Tenant, domain: str) -> str:
    """Entri public hostname Cloudflare: domain tenant + wildcard onu-* nya."""
    fqdn = tenant_fqdn(t.subdomain, domain)
    return (
        f"  - hostname: {fqdn}\n"
        f"    service: http://127.0.0.1:{t.port}\n"
        f"  - hostname: '*.{fqdn}'\n"
        f"    service: http://127.0.0.1:{t.port}\n"
    )


def onboarding_text(t: Tenant, domain: str, office_public: str,
                    subnet_lan: str) -> str:
    """Langkah menghidupkan tenant: container + tunnel + VPN router pelanggan."""
    fqdn = tenant_fqdn(t.subdomain, domain)
    slug = t.subdomain.replace(".", "-")
    return f"""# Tenant {t.name} ({fqdn})
# 1) Data & container:
mkdir -p tenants/{slug}/data
#    tempel compose_snippet ke docker-compose.yml, lalu: docker compose up -d
# 2) Tunnel: tempel tunnel_snippet ke ingress cloudflared (urutan di atas '*').
# 3) VPN router pelanggan (full-tunnel):
#    sudo ./tools/vpn/add-site.sh {slug} {office_public} {subnet_lan}
#    lalu tempel blok Mikrotik keluarannya ke router pelanggan.
# 4) Kredensial admin tenant: {t.admin_user} / {t.admin_pass}
#    (alamat: https://{fqdn}  -  kuota router: {t.router_limit})
"""
