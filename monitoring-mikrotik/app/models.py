"""Model data: router mikrotik, data pelanggan, dan log aktivitas teknisi."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Router(Base):
    __tablename__ = "routers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    host: Mapped[str] = mapped_column(String(120))
    port: Mapped[int] = mapped_column(Integer, default=8728)
    username: Mapped[str] = mapped_column(String(80))
    password: Mapped[str] = mapped_column(String(160))
    site: Mapped[str] = mapped_column(String(80), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval: Mapped[int] = mapped_column(Integer, default=5)
    # IP manajemen router (webfig). Kosong = pakai host koneksi API.
    manage_ip: Mapped[str] = mapped_column(String(60), default="")
    # TLS ke mikrotik: "auto" (TLS kalau port 8729), "yes" (paksa TLS),
    # "no" (API polos - untuk mikrotik yang memindah service 'api' ke 8729)
    api_ssl: Mapped[str] = mapped_column(String(8), default="auto")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Catatan: pakai typing.List, bukan list[...] - sintaks list[...] baru ada
    # di Python 3.9, sedangkan SQLAlchemy mengevaluasi anotasi ini saat runtime.
    customers: Mapped[List["Customer"]] = relationship(
        back_populates="router", cascade="all, delete-orphan"
    )


class UserRouter(Base):
    """Batas pandangan peran pemantau: router mana yang boleh ia lihat.
    Bila tidak ada baris untuk user pemantau, ia melihat semua router."""
    __tablename__ = "user_routers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    router_id: Mapped[int] = mapped_column(ForeignKey("routers.id"), index=True)


class OnuNat(Base):
    """NAT sementara di router situs luar untuk Remote ONU otomatis:
    port publik dari pool -> IP ONU. Baris dihapus sweep setelah idle."""
    __tablename__ = "onu_nat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    router_id: Mapped[int] = mapped_column(ForeignKey("routers.id"), index=True)
    onu_ip: Mapped[str] = mapped_column(String(60))
    onu_port: Mapped[int] = mapped_column(Integer, default=80)
    nat_port: Mapped[int] = mapped_column(Integer)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("router_id", "name", name="uq_customer_router_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    router_id: Mapped[int] = mapped_column(ForeignKey("routers.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)  # = nama PPPoE secret
    full_name: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(String(200), default="")

    # hasil pantauan terakhir
    ip: Mapped[str] = mapped_column(String(60), default="", index=True)
    mac: Mapped[str] = mapped_column(String(30), default="")
    last_seen: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ONU / router pelanggan
    onu_ip: Mapped[str] = mapped_column(String(60), default="")
    onu_port: Mapped[int] = mapped_column(Integer, default=80)
    onu_info: Mapped[str] = mapped_column(String(160), default="")
    router_ip: Mapped[str] = mapped_column(String(60), default="")
    router_port: Mapped[int] = mapped_column(Integer, default=80)

    # asal baris: sync (tarikan mikrotik), auto (dibuat tombol Isolir),
    # manual (ditambah lewat aplikasi) - tab Pelanggan hanya menampilkan manual
    source: Mapped[str] = mapped_column(String(10), default="sync")
    auto_isolir: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_isolir_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)

    # status isolir & tagihan
    isolated: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    isolated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    isolated_by: Mapped[str] = mapped_column(String(80), default="")
    isolated_reason: Mapped[str] = mapped_column(String(200), default="")
    # profil PPP asli pelanggan; terisi saat isolir mode "suspend" supaya tombol
    # Buka bisa memulihkannya (kolom baru, dimigrasi otomatis oleh _migrate()).
    ppp_profile: Mapped[str] = mapped_column(String(80), default="")
    is_paid: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True
    )  # None = tidak tahu
    billing_code: Mapped[str] = mapped_column(String(80), default="", index=True)

    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    router: Mapped[Router] = relationship(back_populates="customers")


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(80), default="")
    action: Mapped[str] = mapped_column(String(40))  # isolate | open | scan_onu | ...
    customer_name: Mapped[str] = mapped_column(String(120), default="")
    router_name: Mapped[str] = mapped_column(String(80), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)


class User(Base):
    """Akun login teknisi/admin (dikelola dari menu Pengaturan)."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(160))
    salt: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(10), default="teknisi")  # admin|teknisi
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
