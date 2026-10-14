"""Konfigurasi aplikasi. Semua nilai bisa dioverride lewat file .env / environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # python-dotenv bersifat opsional
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv tidak wajib terpasang
    pass


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "ya", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # --- dasar ---
    app_name: str = os.getenv("APP_NAME", "ISP-OPS")
    app_version: str = "1.0.0"
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000)
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./ispops.db")

    # --- keamanan ---
    auth_user: str = os.getenv("AUTH_USER", "teknisi")
    auth_password: str = os.getenv("AUTH_PASSWORD", "")  # kosong = login dimatikan
    # teknisi tambahan: "nama:password,nama:password" (peran 'teknisi')
    tech_users: str = os.getenv("TECH_USERS", "")

    def users(self) -> dict:
        """nama -> (password, peran). AUTH_USER berperan admin (penuh);
        setiap entri TECH_USERS berperan teknisi (monitoring + tambah klien)."""
        peta: dict = {}
        if self.auth_password:
            peta[self.auth_user] = (self.auth_password, "admin")
        for bagian in self.tech_users.split(","):
            bagian = bagian.strip()
            if not bagian:
                continue
            nama, _, sandi = bagian.partition(":")
            if nama.strip():
                peta[nama.strip()] = (sandi.strip(), "teknisi")
        return peta
    session_secret: str = os.getenv("SESSION_SECRET", "ganti-ini-di-env")

    # --- mikrotik ---
    poll_interval: float = float(os.getenv("POLL_INTERVAL", "5"))
    api_port: int = _int("API_PORT", 8728)
    api_timeout: float = float(os.getenv("API_TIMEOUT", "4"))
    ssh_fallback: bool = _bool("SSH_FALLBACK", False)
    isolir_list: str = os.getenv("ISOLIR_LIST", "isolir")
    auto_isolir_grace_days: int = int(os.getenv("AUTO_ISOLIR_GRACE_DAYS", "2"))
    isolir_rule_comment: str = os.getenv("ISOLIR_RULE_COMMENT", "ISP-OPS ISOLIR")
    isolir_action: str = os.getenv("ISOLIR_ACTION", "drop")  # drop | reject
    isolir_timeout: str = os.getenv("ISOLIR_TIMEOUT", "")  # "" = tanpa batas
    isolir_disable_secret: bool = _bool("ISOLIR_DISABLE_SECRET", True)
    isolir_kick_active: bool = _bool("ISOLIR_KICK_ACTIVE", True)
    # Mode isolir: "disable" (klasik: disable secret + address-list; ada banjir
    # log login PPPoE gagal karena CPE redial terus) atau "suspend" (secret
    # dipindah ke profil SUSPENDED: login tetap berhasil, address dari pool
    # khusus yang di-drop firewall -> blok menempel di akun, bukan IP).
    isolir_mode: str = os.getenv("ISOLIR_MODE", "disable").strip().lower()  # disable | suspend
    suspend_profile: str = os.getenv("SUSPEND_PROFILE", "SUSPENDED")
    suspend_pool: str = os.getenv("SUSPEND_POOL", "ISP-OPS-SUSPEND")
    suspend_subnet: str = os.getenv("SUSPEND_SUBNET", "10.254.254.0/24")
    suspend_rule_comment: str = os.getenv("SUSPEND_RULE_COMMENT", "ISP-OPS SUSPEND")
    manage_port: int = _int("MANAGE_PORT", 80)

    # --- ONU ---
    onu_default_port: int = _int("ONU_DEFAULT_PORT", 80)
    onu_candidates: str = os.getenv(
        "ONU_CANDIDATES", "192.168.100.1,192.168.1.1,192.168.10.1"
    )
    onu_scan_timeout: float = float(os.getenv("ONU_SCAN_TIMEOUT", "0.6"))
    onu_scan_concurrency: int = _int("ONU_SCAN_CONCURRENCY", 32)
    onu_proxy_timeout: float = float(os.getenv("ONU_PROXY_TIMEOUT", "45"))
    # Rentang port di router situs luar yang diforward provider untuk NAT
    # sementara Remote ONU otomatis (mis. "45600-45699"). Kosong = fitur mati.
    onu_nat_ports: str = os.getenv("ONU_NAT_PORTS", "")
    # Berapa detik NAT dibiarkan setelah aktivitas remote terakhir.
    onu_nat_ttl: int = _int("ONU_NAT_TTL", 600)
    # Kuota router untuk instalasi tenant (0 = tanpa batas). Diisi otomatis
    # oleh panel operator lewat compose; menahan tambah router di UI/API.
    router_limit: int = _int("ROUTER_LIMIT", 0)

    # --- integrasi aplikasi penagihan (opsional) ---
    billing_enabled: bool = _bool("BILLING_ENABLED", False)
    billing_host: str = os.getenv("BILLING_DB_HOST", "127.0.0.1")
    billing_port: int = _int("BILLING_DB_PORT", 3306)
    billing_user: str = os.getenv("BILLING_DB_USER", "billing_ro")
    billing_password: str = os.getenv("BILLING_DB_PASS", "")
    billing_name: str = os.getenv("BILLING_DB_NAME", "billing")
    billing_sql: str = os.getenv(
        "BILLING_SQL",
        # contoh: kembalikan daftar kode pelanggan yang masih punya tagihan jatuh tempo
        "SELECT customer_code AS code FROM invoices "
        "WHERE status='unpaid' AND due_date < CURDATE()",
    )
    billing_cache_ttl: int = _int("BILLING_CACHE_TTL", 300)

    # --- demo (tanpa mikrotik sungguhan) ---
    demo_mode: bool = _bool("DEMO_MODE", False)

    extra: dict = field(default_factory=dict)

    @property
    def onu_candidate_list(self) -> list[str]:
        return [c.strip() for c in self.onu_candidates.split(",") if c.strip()]


settings = Settings()
