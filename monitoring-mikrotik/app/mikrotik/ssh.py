"""Adapter SSH (opsional). Dipakai hanya kalau SSH_FALLBACK=true di .env,
misalnya saat port API 8728 diblokir di jaringan Bapak/Ibu.

Butuh: pip install paramiko
"""
from __future__ import annotations

import re
import shlex

from .base import RouterClient, Secret

try:
    import paramiko
except ImportError:  # paramiko opsional - hanya perlu kalau SSH_FALLBACK=true
    paramiko = None  # type: ignore[assignment]


def _rows(output: str) -> list[dict]:
    """Parse output 'Flags: ...' + header + baris Mikrotik jadi list of dict."""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("Flags:"):
            header_idx = i + 1
            break
    if header_idx is None:
        header_idx = 0
    if header_idx >= len(lines):
        return []
    headers = lines[header_idx].split()
    out: list[dict] = []
    for ln in lines[header_idx + 1 :]:
        parts = ln.split()
        if not parts:
            continue
        # kolom pertama bisa berisi flag huruf (mis. 'D', 'X') yang menempel
        row: dict[str, str] = {}
        for h, p in zip(headers, parts):
            row[h] = p
        out.append(row)
    return out


class RouterOSSSH(RouterClient):
    backend = "ssh"

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        timeout: float = 6.0,
    ) -> None:
        if paramiko is None:  # pragma: no cover
            raise RuntimeError("paramiko belum terpasang: pip install paramiko")
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self._client = None

    def _run(self, command: str) -> str:
        if self._client is None:
            self._client = paramiko.SSHClient()
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._client.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
        _, stdout, stderr = self._client.exec_command(
            f"/{command} once", timeout=self.timeout
        )
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if err and "failure" in err.lower():
            raise RuntimeError(err.strip())
        return out

    def check(self) -> tuple[bool, str]:
        try:
            out = self._run("system resource print")
            return True, " ".join(out.split())[:160]
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def ppp_active(self) -> list[dict]:
        return _rows(self._run("ppp active print"))

    def ppp_secrets(self) -> list[Secret]:
        out: list[Secret] = []
        for r in _rows(self._run("ppp secret print")):
            out.append(
                Secret(
                    name=r.get("name", ""),
                    service=r.get("service", "pppoe"),
                    disabled="D" in r.get("flags", "") or r.get("disabled") == "true",
                    comment=r.get("comment", ""),
                    profile=r.get("profile", ""),
                    id=r.get("#", ""),
                )
            )
        return out

    def address_list_addresses(self, list_name: str) -> list[str]:
        rows = _rows(self._run(f"ip firewall address-list print where list={shlex.quote(list_name)}"))
        return [r.get("address", "") for r in rows if r.get("address")]

    def ping(self, address: str) -> tuple[int, float]:
        out = self._run(f"ping {shlex.quote(address)} count=1")
        received = 1 if "received" in out and " 0 received" not in out else 0
        m = re.search(r"avg-rtt=([\d.]+)", out)
        return received, float(m.group(1)) if m else 0.0

    def ensure_isolir_rule(self, list_name: str, action: str, comment: str) -> bool:
        rows = _rows(self._run(f"ip firewall filter print where comment={shlex.quote(comment)}"))
        if rows:
            return False
        self._run(
            "ip firewall filter add chain=forward "
            f"src-address-list={shlex.quote(list_name)} action={action} "
            f"comment={shlex.quote(comment)} place-before=0"
        )
        return True

    def add_address_list(
        self, list_name: str, address: str, comment: str, timeout: str = ""
    ) -> None:
        extra = f" timeout={timeout}" if timeout else ""
        self._run(
            f"ip firewall address-list add list={shlex.quote(list_name)} "
            f"address={shlex.quote(address)} comment={shlex.quote(comment)}{extra}"
        )

    def remove_address_list(self, list_name: str, address: str) -> int:
        rows = _rows(
            self._run(
                f"ip firewall address-list print where list={shlex.quote(list_name)} "
                f"and address={shlex.quote(address)}"
            )
        )
        for r in rows:
            num = r.get("#", "").strip()
            if num:
                self._run(f"ip firewall address-list remove numbers={shlex.quote(num)}")
        return len(rows)

    def set_secret_disabled(self, name: str, disabled: bool) -> bool:
        self._run(f"ppp secret set [find name={shlex.quote(name)}] disabled={'yes' if disabled else 'no'}")
        return True

    def kick_active(self, name: str) -> int:
        rows = _rows(self._run(f"ppp active print where name={shlex.quote(name)}"))
        for r in rows:
            num = r.get("#", "").strip()
            if num:
                self._run(f"ppp active remove numbers={shlex.quote(num)}")
        return len(rows)

    def ppp_profiles(self) -> list[str]:
        rows = _rows(self._run("ppp profile print"))
        return [r.get("name", "") for r in rows if r.get("name")]

    def profile_exists(self, name: str) -> bool:
        if not name:
            return False
        rows = _rows(self._run(f"ppp profile print where name={shlex.quote(name)}"))
        return any(r.get("name") == name for r in rows)

    # ---------------------- mode suspend (ISOLIR_MODE=suspend)
    def secret_profile(self, name: str) -> str | None:
        rows = _rows(self._run(f"ppp secret print where name={shlex.quote(name)}"))
        if not rows:
            return None
        return str(rows[0].get("profile", "") or "")

    def set_secret_profile(self, name: str, profile: str) -> bool:
        self._run(
            f"ppp secret set [find name={shlex.quote(name)}] "
            f"profile={shlex.quote(profile)}"
        )
        return True

    def ensure_suspend_profile(self, profile: str, pool: str, subnet: str) -> bool:
        dibuat = False
        if not _rows(self._run(f"ip pool print where name={shlex.quote(pool)}")):
            dasar = subnet.split("/")[0].rsplit(".", 1)[0]
            self._run(
                f"ip pool add name={shlex.quote(pool)} "
                f"ranges={dasar}.2-{dasar}.254"
            )
            dibuat = True
        if not _rows(self._run(f"ppp profile print where name={shlex.quote(profile)}")):
            self._run(
                f"ppp profile add name={shlex.quote(profile)} "
                f"remote-address={shlex.quote(pool)}"
            )
            dibuat = True
        return dibuat

    def ensure_suspend_rule(self, subnet: str, action: str, comment: str) -> bool:
        rows = _rows(self._run(f"ip firewall filter print where comment={shlex.quote(comment)}"))
        if rows:
            return False
        self._run(
            "ip firewall filter add chain=forward "
            f"src-address={shlex.quote(subnet)} action={action} "
            f"comment={shlex.quote(comment)} place-before=0"
        )
        return True
