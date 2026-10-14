"""Paket adapter Mikrotik."""
from .base import RouterClient, Secret

__all__ = ["RouterClient", "Secret", "make_client"]


def make_client(router, *, backend: str = "api") -> RouterClient:
    """Bikin client untuk satu baris Router dari database.

    backend: "api" (RouterOS API, default) | "ssh" (fallback, butuh paramiko)
    """
    if backend == "ssh":
        try:
            import paramiko  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "SSH_FALLBACK=true tapi paramiko belum terpasang. "
                "Jalankan: pip install paramiko"
            ) from exc
        from .ssh import RouterOSSSH

        return RouterOSSSH(
            host=router.host,
            username=router.username,
            password=router.password,
            port=22,
        )
    from .api import RouterOSAPI

    pilihan = (getattr(router, "api_ssl", "auto") or "auto").strip().lower()
    use_ssl = {"yes": True, "true": True, "no": False, "false": False}.get(pilihan)
    return RouterOSAPI(
        host=router.host,
        username=router.username,
        password=router.password,
        port=router.port or 8728,
        use_ssl=use_ssl,  # None = otomatis (TLS kalau port 8729)
    )
