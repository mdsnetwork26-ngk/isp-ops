"""Koneksi database (SQLAlchemy 2.x). Default SQLite -> ringan, tanpa service tambahan."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401  (pastikan model terdaftar)
    from .auth import seed_users

    Base.metadata.create_all(bind=engine)
    _migrate()
    seed_users()


def _migrate() -> None:
    """Migrasi ringan untuk database lama (SQLite) tanpa tool migrasi."""
    if not settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())

    # kolom api_ssl di routers harus ditambahkan DULU, sebelum return apa pun:
    # database lama yang unique index-nya sudah ada duluan pernah melewatkan
    # langkah ini, sehingga INSERT router baru gagal (HTTP 500).
    if "routers" in tables:
        kolom_router = {c["name"] for c in insp.get_columns("routers")}
        if "api_ssl" not in kolom_router:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE routers ADD COLUMN api_ssl VARCHAR(8) DEFAULT 'auto'")
                )

    if "customers" in tables:
        # tombol "Cari ONU" sudah dihapus dari aplikasi; bersihkan sisanya
        kolom_cust = {c["name"] for c in insp.get_columns("customers")}
        if {"onu_ip", "onu_info"} <= kolom_cust:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE customers SET onu_ip='', onu_info='' "
                         "WHERE onu_ip <> '' OR onu_info <> ''")
                )
        if "source" not in kolom_cust:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE customers ADD COLUMN source VARCHAR(10) DEFAULT 'sync'")
                )
        if "auto_isolir" not in kolom_cust:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE customers ADD COLUMN auto_isolir BOOLEAN DEFAULT 0")
                )
        if "ppp_profile" not in kolom_cust:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE customers ADD COLUMN ppp_profile VARCHAR(80) DEFAULT ''")
                )
        if "auto_isolir_date" not in kolom_cust:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE customers ADD COLUMN auto_isolir_date DATE")
                )

    if "customers" not in tables:
        return
    sudah_ada = False
    for u in insp.get_unique_constraints("customers"):
        if tuple(sorted(u["column_names"])) == ("name", "router_id"):
            sudah_ada = True
    for i in insp.get_indexes("customers"):
        if i.get("unique") and tuple(sorted(i.get("column_names") or [])) == ("name", "router_id"):
            sudah_ada = True
    if sudah_ada:
        return
    with engine.begin() as conn:
        # hapus data kembar dulu (simpan yang paling baru) supaya unique index bisa dibuat
        conn.execute(
            text(
                """
                DELETE FROM customers
                WHERE id NOT IN (
                    SELECT MAX(id) FROM customers GROUP BY router_id, LOWER(name)
                )
                """
            )
        )
        try:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_customer_router_name "
                    "ON customers (router_id, LOWER(name))"
                )
            )
        except Exception:  # sudah ada
            pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
