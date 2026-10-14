"""Penjaga kompatibilitas Python 3.8.

Latar belakang: aplikasi pernah gagal start di Ubuntu 20.04 dengan

    sqlalchemy.orm.exc.MappedAnnotationError:
    Could not interpret annotation Mapped[list['Customer']]

Penyebabnya: anotasi `Mapped[...]` dievaluasi SQLAlchemy saat runtime, dan di
Python 3.8 sintaks `list[...]` (PEP 585) serta `X | None` (PEP 604) belum ada.
`from __future__ import annotations` TIDAK menolong karena yang membaca anotasi
itu SQLAlchemy/Pydantic, bukan interpreter.

Tes ini memeriksa sumber secara statis (AST + regex) sehingga tetap jalan di
Python versi berapa pun - termasuk 3.13 yang dipakai pengembang, di mana
sintaks itu kebetulan sah.
"""
from __future__ import annotations

import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# tipe bawaan yang baru boleh ditulis list[...] mulai Python 3.9
BUILTINS_GENERIC = ("list", "dict", "tuple", "set", "frozenset", "type")

# pola anotasi yang dievaluasi saat runtime
RUNTIME_ANNOTATED = (
    re.compile(r"\bMapped\["),      # SQLAlchemy
)


def _baca(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _anotasi_runtime(sumber: str) -> list[tuple[int, str]]:
    """Kumpulkan anotasi variabel kelas yang dibaca saat runtime."""
    tree = ast.parse(sumber)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and node.annotation is not None:
            teks = ast.unparse(node.annotation)
            if any(p.search(teks) for p in RUNTIME_ANNOTATED):
                out.append((node.lineno, teks))
    return out


def _pelanggaran(teks: str) -> list[str]:
    masalah = []
    for nama in BUILTINS_GENERIC:
        if re.search(rf"\b{nama}\[", teks):
            masalah.append(f"{nama}[...] (PEP 585, butuh Python 3.9)")
    if re.search(r"\|\s*None|None\s*\|", teks):
        masalah.append("X | None (PEP 604, butuh Python 3.10)")
    return masalah


@pytest.mark.parametrize("rel", ["app/models.py"])
def test_anotasi_sqlalchemy_aman_untuk_py38(rel):
    """Anatomi bug: Mapped[list[...]] / Mapped[X | None] di models.py."""
    sumber = _baca(rel)
    semua = _anotasi_runtime(sumber)
    assert semua, f"tidak menemukan anotasi Mapped[...] di {rel} - pemeriksa ini perlu diperbarui"

    salah = []
    for lineno, teks in semua:
        for m in _pelanggaran(teks):
            salah.append(f"{rel}:{lineno}  {teks}  -> {m}")
    assert not salah, (
        "anotasi ini dievaluasi saat runtime dan akan gagal di Python 3.8:\n  "
        + "\n  ".join(salah)
        + "\nGanti ke typing.List[...] / typing.Optional[...]."
    )


@pytest.mark.parametrize("rel", ["app/routes.py"])
def test_model_pydantic_aman_untuk_py38(rel):
    """Model Pydantic juga dievaluasi saat runtime - tidak boleh pakai X | None."""
    sumber = _baca(rel)
    tree = ast.parse(sumber)
    salah = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # hanya kelas yang menurun dari BaseModel
        bases = [ast.unparse(b) for b in node.bases]
        if not any("BaseModel" in b for b in bases):
            continue
        for sub in node.body:
            if isinstance(sub, ast.AnnAssign) and sub.annotation is not None:
                teks = ast.unparse(sub.annotation)
                for m in _pelanggaran(teks):
                    salah.append(f"{rel}:{sub.lineno}  {teks}  -> {m}")
    assert not salah, (
        "model Pydantic ini akan gagal di Python 3.8:\n  " + "\n  ".join(salah)
    )


def test_tidak_ada_asyncio_to_thread_langsung():
    """asyncio.to_thread butuh 3.9; harus lewat app/kompat.py."""
    salah = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "app")):
        for nama in files:
            if not nama.endswith(".py") or nama == "kompat.py":
                continue
            rel = os.path.relpath(os.path.join(dirpath, nama), ROOT)
            with open(os.path.join(dirpath, nama), encoding="utf-8") as f:
                for i, baris in enumerate(f, 1):
                    kode = baris.split("#", 1)[0]
                    if "asyncio.to_thread" in kode:
                        salah.append(f"{rel}:{i}  {baris.strip()}")
    assert not salah, (
        "pakai app.kompat.to_thread() agar jalan di Python 3.8:\n  " + "\n  ".join(salah)
    )


def test_requirements_tidak_memakai_librouteros():
    """librouteros 4.x tidak tersedia untuk Python 3.8 - jangan dipakai lagi."""
    isi = _baca("requirements.txt")
    aktif = [
        baris for baris in isi.splitlines()
        if baris.strip() and not baris.strip().startswith("#")
    ]
    assert not any("librouteros" in b for b in aktif), aktif


def test_protokol_routeros_tidak_butuh_pustaka_luar():
    """app/routeros.py harus murni pustaka standar agar tak ada jebakan versi."""
    sumber = _baca("app/routeros.py")
    tree = ast.parse(sumber)
    modul = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modul.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modul.add(node.module.split(".")[0])
    eksternal = {m for m in modul if m not in sys.stdlib_module_names}
    assert not eksternal, f"app/routeros.py mengimpor pustaka luar: {eksternal}"
