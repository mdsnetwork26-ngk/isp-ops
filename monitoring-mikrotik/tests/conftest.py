"""Konfigurasi bersama untuk semua tes.

Penting: app.config membaca environment saat diimpor, jadi DATABASE_URL harus
ditetapkan SEKALI di sini sebelum modul aplikasi apa pun diimpor. Kalau tiap
berkas tes menetapkan sendiri, berkas yang diimpor lebih dulu yang menang dan
tes lain diam-diam memakai database yang salah.
"""
from __future__ import annotations

import os
import sys

os.environ["DEMO_MODE"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///./test_ispops.db"
os.environ.setdefault("ISOLIR_DISABLE_SECRET", "true")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
