"""Panel operator: autentikasi, alur tenant (buat/perpanjang/suspend/hapus),
sweep kedaluwarsa, dan teks provisioning (compose/tunnel/onboarding)."""
from __future__ import annotations

import os
from base64 import b64encode
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

os.environ["OPERATOR_DATABASE_URL"] = "sqlite:///./test_operator.db"
os.environ["OPERATOR_PASSWORD"] = "opw"

from panel import provision                      # noqa: E402
from panel.main import app as panel_app         # noqa: E402
from panel.models import SessionOp, Tenant, init_op_db  # noqa: E402

AUTH = {"Authorization": "Basic " + b64encode(b"operator:opw").decode()}


@pytest.fixture()
def pclient():
    init_op_db()
    db = SessionOp()
    db.query(Tenant).delete()
    db.commit()
    db.close()
    with TestClient(panel_app) as c:
        yield c


def test_panel_butuh_kredensial(pclient):
    assert pclient.get("/").status_code in (401, 403)
    assert pclient.get("/", headers=AUTH).status_code == 200
    assert pclient.get("/health").status_code == 200


def _buat(c, nama, sub, limit="1", days="365"):
    return c.post("/buat", data={"name": nama, "subdomain": sub,
                                 "router_limit": limit, "days": days,
                                 "notes": ""}, headers=AUTH,
                  follow_redirects=False)


def test_alur_tenant_dan_provisioning(pclient):
    r = _buat(pclient, "Munir", "munir", limit="2")
    assert r.status_code == 303
    db = SessionOp()
    t = db.query(Tenant).first()
    assert t.port == 8101 and t.router_limit == 2 and t.status == "active"
    assert 364 <= (t.expires_at - t.started_at).days <= 366
    db.close()

    r = pclient.get(f"/snippet/{t.id}", headers=AUTH)
    assert "ispops-munir" in r.text
    assert "PORT: 8101" in r.text and "ROUTER_LIMIT: 2" in r.text
    assert "munir.marinagih.my.id" in r.text
    assert "*.munir.marinagih.my.id" in r.text   # wildcard onu-* tenant

    # subdomain duplikat ditolak
    assert _buat(pclient, "Munir2", "munir").status_code == 409

    # port naik otomatis
    _buat(pclient, "Budi", "budi", days="30")
    db = SessionOp()
    assert 8102 in [x.port for x in db.query(Tenant).all()]
    db.query(Tenant).filter_by(subdomain="budi").update(
        {"expires_at": t.started_at - timedelta(days=1)})
    db.commit()
    db.close()

    # sweep menandai expired; perpanjang mengaktifkan lagi
    assert provision.sweep_expired() == ["budi"]
    db = SessionOp()
    tb = db.query(Tenant).filter_by(subdomain="budi").first()
    assert tb.status == "expired"
    tid = tb.id
    db.close()
    provision.extend_tenant(tid, 30)
    db = SessionOp()
    tb = db.query(Tenant).get(tid)
    assert tb.status == "active"
    assert provision.days_left(tb) >= 29

    # suspend / aktifkan lewat HTTP
    pclient.post(f"/status/{tid}", headers=AUTH, follow_redirects=False)
    db = SessionOp()
    assert db.get(Tenant, tid).status == "suspended"
    db.close()

    # hapus
    pclient.post(f"/hapus/{tid}", headers=AUTH, follow_redirects=False)
    db = SessionOp()
    assert db.get(Tenant, tid) is None
    db.close()
