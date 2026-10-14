"""Tombol Tes ikut memastikan rule firewall isolir ada di mikrotik."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app
from tests.test_protocol import FakeMikrotik


def test_tes_router_membuat_lalu_mengingatkan_rule_isolir():
    srv = FakeMikrotik().start()
    try:
        with TestClient(app) as c:
            r = c.post("/api/routers", json={
                "id": None, "name": "RULE-TEST", "host": "127.0.0.1", "port": srv.port,
                "username": "ispops", "password": "rahasia", "site": "", "enabled": True,
                "poll_interval": 10, "manage_ip": "", "api_ssl": "no",
            })
            rid = r.json()["id"]

            t1 = c.post(f"/api/routers/{rid}/test", json={}).json()
            assert t1["ok"] is True, t1
            assert t1["info"]["rule"] == "dibuat"

            t2 = c.post(f"/api/routers/{rid}/test", json={}).json()
            assert t2["info"]["rule"] == "sudah ada"

            c.post(f"/api/routers/{rid}/delete", json={})
    finally:
        srv.stop()
