"""Fungsi bantu: format uptime, normalisasi MAC, deteksi ONU dari isi halaman web."""
from __future__ import annotations

import ipaddress
import re

# --------------------------------------------------------------------------- uptime

_UNIT_ID = {
    "w": "minggu",
    "d": "hari",
    "h": "jam",
    "m": "menit",
    "s": "detik",
    "ms": "ms",
    "us": "us",
}


def parse_uptime(raw: str | None) -> int:
    """Ubah uptime RouterOS ('1w2d3h4m5s' / '3m12s' / '45s') jadi detik.

    >>> parse_uptime('1w2d3h4m5s')
    788645
    >>> parse_uptime('3m12s')
    192
    >>> parse_uptime('45s')
    45
    >>> parse_uptime('')
    0
    """
    if not raw:
        return 0
    text = str(raw).strip().lower().replace(" ", "")
    if text.isdigit():
        return int(text)
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+)(ms|us|w|d|h|m|s)", text):
        found = True
        n = int(value)
        if unit == "w":
            total += n * 604800
        elif unit == "d":
            total += n * 86400
        elif unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
        elif unit == "s":
            total += n
        # ms/us diabaikan (tidak relevan untuk uptime sesi)
    return total if found else 0


def format_uptime(seconds: int) -> str:
    """Detik -> teks Indonesia yang enak dibaca.

    >>> format_uptime(788645)
    '1 minggu 2 hari'
    >>> format_uptime(192)
    '3m 12d'
    >>> format_uptime(0)
    '-'
    """
    s = int(seconds or 0)
    if s <= 0:
        return "-"
    weeks, rem = divmod(s, 604800)
    days, rem = divmod(rem, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if weeks:
        return f"{weeks} minggu {days} hari"
    if days:
        return f"{days} hari {hours} jam"
    if hours:
        return f"{hours}j {minutes}m"
    if minutes:
        return f"{minutes}m {secs}d"
    return f"{secs}d"


# --------------------------------------------------------------------------- MAC

_MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_VENDOR_HINTS = (
    "zte", "huawei", "fiberhome", "nokia", "dasan", "vsol", "hsgq", "cdata",
    "gpon", "onu", "ont", "epon", "xpon", "routeros", "mikrotik",
)


def norm_mac(mac: str | None) -> str:
    """'AA:BB:CC:DD:EE:FF' / 'aa-bb-cc-dd-ee-ff' -> 'aabbccddeeff'."""
    if not mac:
        return ""
    return re.sub(r"[^0-9a-fA-F]", "", mac).lower()


def pretty_mac(mac: str | None) -> str:
    """'aabbccddeeff' -> 'AA:BB:CC:DD:EE:FF' (huruf besar, format umum Mikrotik)."""
    raw = norm_mac(mac)
    if len(raw) != 12:
        return (mac or "").upper()
    return ":".join(raw[i : i + 2].upper() for i in range(0, 12, 2))


def extract_macs(text: str, limit: int = 10) -> list[str]:
    """Ambil semua alamat MAC dari teks (halaman web ONU/router)."""
    seen: list[str] = []
    for m in _MAC_RE.finditer(text or ""):
        mac = norm_mac(m.group(0))
        if mac not in seen and mac not in {"ffffffffffff", "000000000000"}:
            seen.append(mac)
        if len(seen) >= limit:
            break
    return seen


def onu_score(body: str, headers: dict[str, str], target_mac: str = "") -> tuple[int, str]:
    """Nilai keyakinan bahwa halaman web itu adalah ONU pelanggan.

    Mengembalikan (skor 0-100, alasan). MAC cocok = bukti terkuat.
    """
    text = (body or "")[:6000].lower()
    head_text = " ".join(f"{k}: {v}" for k, v in (headers or {}).items()).lower()
    macs = extract_macs(body or "")
    reason: list[str] = []
    score = 0

    if target_mac and norm_mac(target_mac) in macs:
        score += 60
        reason.append("MAC cocok dengan MAC PPPoE pelanggan")
    if _MAC_RE.search(body or ""):
        score += 10
        reason.append("halaman menampilkan MAC")
    for hint in _VENDOR_HINTS:
        if hint in text or hint in head_text:
            score += 10
            reason.append(f"keyword '{hint}'")
            if score >= 95:
                break
    return min(score, 100), ", ".join(reason[:4])


# --------------------------------------------------------------------------- IP

_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # rentang VPN operator (CGNAT)


def is_private_ip(ip: str) -> bool:
    """True untuk IP privat/loopback/link-local/CGNAT (aman untuk diakses proxy).

    100.64/10 (dipakai banyak VPN operator untuk alamat tunnel) disertakan
    eksplisit supaya perilakunya sama di semua versi Python (sejak 3.12.4
    is_private tidak lagi mencakupnya)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr in _CGNAT)


def same_subnet_ips(ip: str, prefix: int = 24, limit: int = 254) -> list[str]:
    """Daftar IP host dalam satu subnet (untuk fitur scan ONU manual)."""
    try:
        net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    except ValueError:
        return []
    return [str(h) for h in list(net.hosts())[:limit]]


def last_octet_variants(ip: str) -> list[str]:
    """Varian IP dengan oktet terakhir umum dipakai ONU (.1/.2/.254/.100)."""
    if not ip or ip.count(".") != 3:
        return []
    base = ip.rsplit(".", 1)[0]
    return [f"{base}.{o}" for o in (1, 2, 254, 100)]
