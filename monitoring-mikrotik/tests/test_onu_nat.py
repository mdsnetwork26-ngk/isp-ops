"""NAT sementara otomatis untuk Remote ONU situs luar (VPN L2TP).

Klien API RouterOS diganti stub berkeadaan; logika app/onu_nat (alokasi port,
idempotensi, sweep, cleanup) dan wiring middleware diuji sungguhan.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import onu_nat
from app.config import settings
from app.database import SessionLocal
from app.models import Customer, OnuNat, Router
from app.routeros import TrapError
from tests.test_remote_onu_multipage import client_onu  # noqa: F401  (fixture)


class StubAPI:
    """Tiruan RouterOSAPI secukupnya untuk perintah firewall."""

    instances = []

    def __init__(self, router):
        self.router = router
        self.nat_rules = []
        self.addr = []
        self.filt = []
        self._n = 0
        StubAPI.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _run(self, cmd, *words):
        kv = {}
        for wd in words:
            if wd.startswith("="):      # word atribut: '=key=value'
                wd = wd[1:]
            k, _, v = wd.partition("=")
            kv[k] = v
        if cmd == "/ip/firewall/nat/print":
            return [dict(r) for r in self.nat_rules
                    if "?comment" not in kv or r.get("comment") == kv["?comment"]]
        if cmd == "/ip/firewall/nat/add":
            self._n += 1
            r = {".id": f"*N{self._n}"}
            r.update(kv)
            self.nat_rules.append(r)
            return [r]
        if cmd == "/ip/firewall/nat/remove":
            self.nat_rules = [r for r in self.nat_rules
                              if r.get(".id") != kv.get(".id")]
            return []
        if cmd == "/ip/firewall/filter/print":
            return [dict(f) for f in self.filt
                    if f.get("comment") == kv.get("?comment")]
        if cmd in ("/ip/firewall/filter/insert", "/ip/firewall/filter/add"):
            self._n += 1
            f = {".id": f"*F{self._n}"}
            f.update(kv)
            self.filt.append(f)
            return []
        if cmd == "/ip/firewall/address-list/add":
            if any(a.get("address") == kv.get("address") for a in self.addr):
                raise TrapError("failure: already have such entry")
            self._n += 1
            a = {".id": f"*A{self._n}"}
            a.update(kv)
            self.addr.append(a)
            return []
        if cmd == "/ip/firewall/address-list/print":
            return [dict(a) for a in self.addr
                    if ("?list" not in kv or a.get("list") == kv["?list"])
                    and ("?address" not in kv or a.get("address") == kv["?address"])]
        if cmd == "/ip/firewall/address-list/remove":
            self.addr = [a for a in self.addr if a.get(".id") != kv.get(".id")]
            return []
        return []


@pytest.fixture()
def stub_router(monkeypatch):
    from app.database import Base, engine
    Base.metadata.create_all(bind=engine)
    db0 = SessionLocal()
    db0.query(OnuNat).delete()
    db0.query(Customer).delete()
    db0.query(Router).delete()   # sisa tes lain: mulai bersih
    db0.commit()
    db0.close()
    StubAPI.instances = []
    monkeypatch.setattr(onu_nat, "make_client", lambda router: StubAPI(router))
    asal = settings.onu_nat_ports
    settings.onu_nat_ports = "45600-45602"
    db = SessionLocal()
    r = Router(name="SITE-L2TP", host="93.184.216.34", port=45536,
               username="u", password="p", site="luar")
    db.add(r)
    db.commit()
    rid = r.id
    db.close()
    yield rid
    settings.onu_nat_ports = asal
    db = SessionLocal()
    db.query(OnuNat).delete()
    db.query(Customer).delete()
    db.query(Router).filter_by(id=rid).delete()
    db.commit()
    db.close()


def _router(rid):
    db = SessionLocal()
    try:
        return db.query(Router).get(rid)
    finally:
        db.close()


def test_pool_ports():
    asal = settings.onu_nat_ports
    try:
        settings.onu_nat_ports = ""
        assert onu_nat.pool_ports() == []
        settings.onu_nat_ports = "45600-45602"
        assert onu_nat.pool_ports() == [45600, 45601, 45602]
        settings.onu_nat_ports = "45600, 45610"
        assert onu_nat.pool_ports() == [45600, 45610]
        settings.onu_nat_ports = "abc"
        assert onu_nat.pool_ports() == []
    finally:
        settings.onu_nat_ports = asal


def test_ensure_nat_membuat_rule_dan_idempoten(stub_router):
    r = _router(stub_router)
    p1 = onu_nat.ensure_nat(r, "10.11.11.35", 80)
    assert p1 == 45600
    api = StubAPI.instances[-1]
    assert len(api.nat_rules) == 1
    rule = api.nat_rules[0]
    assert rule["dst-port"] == "45600"
    assert rule["to-addresses"] == "10.11.11.35"
    assert rule["comment"] == "ispops-onu:10.11.11.35:80"
    assert any(f["comment"] == "ispops-onu-allow" for f in api.filt)
    assert any(a["address"] == "10.11.11.35" for a in api.addr)

    # ONU kedua mendapat port berikutnya
    p2 = onu_nat.ensure_nat(r, "10.11.11.40", 80)
    assert p2 == 45601

    # panggilan ulang dalam TTL: port sama, rule tidak bertambah
    p1b = onu_nat.ensure_nat(r, "10.11.11.35", 80)
    assert p1b == 45600
    total = sum(len(i.nat_rules) for i in StubAPI.instances)
    assert total == 2, total


def test_sweep_menghapus_nat_idle(stub_router):
    r = _router(stub_router)
    assert onu_nat.ensure_nat(r, "10.11.11.35", 80) == 45600
    db = SessionLocal()
    db.query(OnuNat).update({"last_seen": datetime.utcnow()
                             - timedelta(seconds=3600)})
    db.commit()
    db.close()
    n = onu_nat.sweep_once()
    assert n == 1
    api = StubAPI.instances[-1]
    assert api.nat_rules == []          # rule di router ikut dibuang
    assert api.addr == []               # address-list bersih
    db = SessionLocal()
    assert db.query(OnuNat).count() == 0
    db.close()


def test_startup_cleanup_buang_sisa_rule(stub_router):
    api = StubAPI(None)
    api._run("/ip/firewall/nat/add", *["chain=dstnat", "protocol=tcp",
             "dst-port=45600", "action=dst-nat", "to-addresses=10.11.11.35",
             "to-ports=80", "comment=ispops-onu:10.11.11.35:80"])
    r = _router(stub_router)

    class _Klien:
        def __enter__(self):
            return api

        def __exit__(self, *a):
            return False

        def _run(self, cmd, *words):
            return api._run(cmd, *words)

    onu_nat.make_client = lambda router: _Klien()
    onu_nat.startup_cleanup()
    assert api.nat_rules == [], api.nat_rules


class _FakeClient:
    """httpx.AsyncClient palsu: selalu gagal konek, biar 502 deterministik."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, content=None, headers=None):
        import httpx
        raise httpx.ConnectError("stub-tak-ada-rute")


def test_middleware_memakai_nat_untuk_onu_situs_luar(client_onu, stub_router,
                                                    monkeypatch):  # noqa: F811
    from app import onu as onu_mod
    monkeypatch.setattr(onu_mod.httpx, "AsyncClient", _FakeClient)
    db = SessionLocal()
    db.add(Customer(router_id=stub_router, name="vpn-1", onu_ip="10.11.11.35"))
    db.commit()
    db.close()

    # jalur NAT aktif: proxy menyambung ke host-router:port-pool
    r = client_onu.get("/", headers={"host": "onu-10-11-11-35.onu.test"})
    assert r.status_code == 502, r.text  # sandbox tak bisa konek; yang penting:
    assert "http://93.184.216.34:45600/" in r.text

    # fitur dimatikan: jatuh ke koneksi langsung ke IP ONU
    settings.onu_nat_ports = ""
    r = client_onu.get("/", headers={"host": "onu-10-11-11-35.onu.test"})
    assert "http://10.11.11.35/" in r.text
