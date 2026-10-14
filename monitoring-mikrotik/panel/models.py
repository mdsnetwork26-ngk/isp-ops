"""Panel operator: model + database TERPISAH dari data tenant.

Satu baris Tenant = satu pelanggan layanan monitoring (satu container
isp-ops + satu subdomain). Tidak ada tabel ini di database tenant, dan
sebaliknya: isolasi penuh antar pelanggan.
"""
from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class OpBase(DeclarativeBase):
    pass


class Tenant(OpBase):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    # label subdomain: munir -> https://munir.DOMAIN
    subdomain: Mapped[str] = mapped_column(String(80), unique=True)
    # port container isp-ops tenant ini di host
    port: Mapped[int] = mapped_column(Integer, unique=True)
    admin_user: Mapped[str] = mapped_column(String(80), default="admin")
    admin_pass: Mapped[str] = mapped_column(String(160), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # kuota router yang dibayar pelanggan (0 = tanpa batas)
    router_limit: Mapped[int] = mapped_column(Integer, default=1)
    # active | suspended | expired
    status: Mapped[str] = mapped_column(String(20), default="active")
    notes: Mapped[str] = mapped_column(String(300), default="")


_engine = None
SessionOp = sessionmaker()


def get_engine():
    global _engine
    if _engine is None:
        url = os.getenv("OPERATOR_DATABASE_URL", "sqlite:///./operator.db")
        args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=args)
        SessionOp.configure(bind=_engine)
    return _engine


def init_op_db() -> None:
    OpBase.metadata.create_all(get_engine())
