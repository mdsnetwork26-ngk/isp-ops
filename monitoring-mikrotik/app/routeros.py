"""Protokol RouterOS API - ditulis sendiri, murni pustaka standar Python.

Kenapa tidak pakai librouteros?
    librouteros 4.x butuh Python baru (di Python 3.8 pip hanya menyediakan
    sampai 3.4.1 yang API-nya berbeda total). Modul ini hanya butuh `socket`,
    `hashlib`, dan `ssl` - jadi jalan di Python 3.8 ke atas tanpa dependensi
    apa pun, dan kita pegang kendali penuh atas perilakunya.

Protokolnya sendiri sederhana:
    permintaan  = satu "kalimat" berisi kata-kata, ditutup kata kosong
                  contoh: /ppp/active/print, =.tag=1, ""
    balasan     = beberapa kalimat !re (data), lalu !trap (error) atau !done
    tiap kata    = panjang (1-5 byte) + isi

Referensi: https://help.mikrotik.com/docs/spaces/ROS/pages/28573705/API
"""
from __future__ import annotations

import hashlib
import socket
import ssl
import threading
from binascii import unhexlify
from typing import Iterable, Iterator

DEFAULT_PORT = 8728
DEFAULT_SSL_PORT = 8729


# ------------------------------------------------------------------ exception

class LibRouterosError(Exception):
    """Induk semua error modul ini."""


class ProtocolError(LibRouterosError):
    """Balasan tidak sesuai protokol / decoding gagal."""


class FatalError(ProtocolError):
    """Mikrotik mengirim !fatal - koneksi harus ditutup."""


class TrapError(ProtocolError):
    """Mikrotik mengirim !trap - perintah ditolak (mis. 'no such item')."""

    def __init__(self, message: str = "", category: str | None = None) -> None:
        self.message = message
        self.category = category
        super().__init__(message)

    def __str__(self) -> str:
        return self.message.replace("\r\n", ",")


class MultiTrapError(ProtocolError):
    """Lebih dari satu !trap dalam satu balasan."""

    def __init__(self, *traps: TrapError) -> None:
        self.traps = traps
        super().__init__("; ".join(str(t) for t in traps))


class ConnectionClosed(LibRouterosError):
    """Koneksi tertutup oleh mikrotik / jaringan."""


# ------------------------------------------------------------------ encoding

def encode_length(n: int) -> bytes:
    """Panjang kata -> byte sesuai aturan protokol.

    >>> encode_length(0).hex()
    '00'
    >>> encode_length(9).hex()
    '09'
    >>> encode_length(0x7F).hex()
    '7f'
    >>> encode_length(0x80).hex()          # 2 byte: 0x80 | (n>>8), lalu n&0xFF
    '8080'
    >>> encode_length(0x1F4).hex()
    '81f4'
    >>> encode_length(0x2000).hex()        # 3 byte: 0xC0 | (n>>16)
    'c02000'
    >>> encode_length(0x1FFFFF).hex()
    'dfffff'
    >>> encode_length(0x200000).hex()      # 4 byte: 0xE0 | (n>>24)
    'e0200000'
    >>> encode_length(0x10000000).hex()    # 5 byte: 0xF0 + 4 byte besar-ke-kecil
    'f010000000'
    """
    if n < 0x80:
        return bytes((n,))
    if n < 0x4000:
        return bytes(((n >> 8) | 0x80, n & 0xFF))
    if n < 0x200000:
        return bytes(((n >> 16) | 0xC0, (n >> 8) & 0xFF, n & 0xFF))
    if n < 0x10000000:
        return bytes(((n >> 24) | 0xE0, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF))
    return bytes((0xF0,)) + n.to_bytes(4, "big")


def decode_length(sock: socket.socket, encoding: str = "utf-8") -> int:
    """Baca panjang kata dari socket."""
    first = _read_exactly(sock, 1)[0]
    if first < 0x80:
        return first
    if first < 0xC0:
        rest = _read_exactly(sock, 1)[0]
        return ((first & 0x7F) << 8) + rest
    if first < 0xE0:
        rest = _read_exactly(sock, 2)
        return ((first & 0x1F) << 16) + int.from_bytes(rest, "big")
    if first < 0xF0:
        rest = _read_exactly(sock, 3)
        return ((first & 0x0F) << 24) + int.from_bytes(rest, "big")
    return int.from_bytes(_read_exactly(sock, 4), "big")


def encode_word(word: str, encoding: str = "utf-8") -> bytes:
    data = word.encode(encoding)
    return encode_length(len(data)) + data


def compose_word(key: str, value: str = "") -> str:
    """Susun kata permintaan: '=nama=nilai', '=nama', atau query '?nama=nilai'.

    >>> compose_word('list', 'isolir')
    '=list=isolir'
    >>> compose_word('.tag', '1')
    '=.tag=1'
    >>> compose_word('0')
    '=0'
    >>> compose_word('?list', 'isolir')
    '?list=isolir'
    """
    if key.startswith("?"):
        return f"{key}={value}"
    return f"={key}" if value == "" else f"={key}={value}"


def parse_word(word: str) -> tuple[str, str]:
    """Pecah kata balasan '=nama=nilai' atau '.id' jadi (nama, nilai).

    >>> parse_word('=address=10.0.0.5')
    ('address', '10.0.0.5')
    >>> parse_word('=.id=*7')
    ('.id', '*7')
    >>> parse_word('!done')
    ('!done', '')
    """
    if word.startswith("="):
        key, _, value = word[1:].partition("=")
        return key, value
    return word, ""


def _read_exactly(sock: socket.socket, n: int) -> bytes:
    """Baca tepat n byte; ConnectionClosed kalau koneksi putus di tengah."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError as exc:
            raise ConnectionClosed(f"koneksi bermasalah: {exc}") from exc
        if not chunk:
            raise ConnectionClosed("koneksi ditutup mikrotik")
        buf.extend(chunk)
    return bytes(buf)


# ------------------------------------------------------------------ client

class Api:
    """Klien API mikrotik. Satu instance = satu koneksi TCP."""

    def __init__(self, sock: socket.socket, encoding: str = "utf-8") -> None:
        self.sock = sock
        self.encoding = encoding
        self._lock = threading.Lock()
        self._closed = False

    # --- tingkat rendah -------------------------------------------------
    def write_sentence(self, *words: str) -> None:
        if self._closed:
            raise ConnectionClosed("koneksi sudah ditutup")
        payload = b"".join(encode_word(w, self.encoding) for w in words)
        payload += encode_word("", self.encoding)  # kata kosong = akhir kalimat
        try:
            self.sock.sendall(payload)
        except OSError as exc:
            raise ConnectionClosed(f"gagal mengirim: {exc}") from exc

    def read_sentence(self) -> tuple[str, dict]:
        """Baca satu kalimat. Kembalikan (kata-balasan, {atribut: nilai})."""
        words = []
        while True:
            n = decode_length(self.sock, self.encoding)
            if n == 0:
                break
            raw = _read_exactly(self.sock, n)
            try:
                words.append(raw.decode(self.encoding, "replace"))
            except Exception as exc:  # noqa: BLE001
                raise ProtocolError(f"gagal membaca kata: {exc}") from exc
        if not words:
            raise ProtocolError("kalimat kosong dari mikrotik")
        reply = words[0]
        attrs = dict(parse_word(w) for w in words[1:])
        return reply, attrs

    def read_response(self) -> list[dict]:
        """Baca sampai !done. Kumpulkan baris !re; lempar trap kalau ada."""
        traps: list[TrapError] = []
        rows: list[dict] = []
        while True:
            reply, attrs = self.read_sentence()
            if reply == "!re":
                if attrs:
                    rows.append(attrs)
            elif reply == "!trap":
                traps.append(TrapError(attrs.get("message", ""), attrs.get("category")))
            elif reply == "!fatal":
                traps.append(FatalError(attrs.get("message", "fatal")))
                break
            elif reply == "!done":
                if attrs:
                    rows.append(attrs)
                break
        if len(traps) > 1:
            raise MultiTrapError(*traps)
        if traps:
            raise traps[0]
        return rows

    # --- tingkat tinggi -------------------------------------------------
    def raw_cmd(self, cmd: str, *words: str) -> list[dict]:
        """Jalankan satu perintah, kembalikan baris !re-nya.

        raw_cmd('/ppp/active/print')
        raw_cmd('/ip/firewall/address-list/add', '=list=isolir', '=address=10.0.0.1')
        """
        with self._lock:
            self.write_sentence(cmd, *words)
            return self.read_response()

    # nama alias, biar mirip librouteros (memudahkan yang sudah terbiasa)
    def rawCmd(self, cmd: str, *words: str) -> list[dict]:  # noqa: N802
        return self.raw_cmd(cmd, *words)

    def call(self, cmd: str, **kwargs: str) -> list[dict]:
        """Sama seperti raw_cmd tapi argumennya keyword.

        call('/ppp/secret/print', name='budi')  ->  '=name=budi'
        """
        return self.raw_cmd(cmd, *(compose_word(k, v) for k, v in kwargs.items()))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Api":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ------------------------------------------------------------------ connect

def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 - diminta protokol mikrotik


def login(api: Api, username: str, password: str) -> None:
    """Login. Coba cara challenge-response dulu, lalu cara plain.

    RouterOS 6.43+ menerima keduanya. Cara plain lebih sederhana dan dipakai
    sebagai cadangan kalau mikrotik menolak cara challenge.
    """
    try:
        rows = api.call("/login", name=username, command="")
        challenge = ""
        for row in rows:
            challenge = row.get("ret") or challenge
        if challenge:
            digest = "00" + _md5_hex(chr(0) + password + unhexlify(challenge).decode("latin-1"))
            api.call("/login", name=username, response=digest)
            return
    except (TrapError, FatalError, MultiTrapError):
        pass
    # cara plain (RouterOS 6.43 ke atas)
    api.call("/login", name=username, password=password)


def connect(
    host: str,
    username: str,
    password: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
    encoding: str = "utf-8",
    use_ssl: bool | None = None,
) -> Api:
    """Buka koneksi ke mikrotik dan login.

    use_ssl=True memaksa TLS (port 8729); False memaksa polos;
    None = otomatis: coba TLS kalau port 8729, dan bila jabat tangan TLS
    ditolak (mikrotik yang memakai api polos di 8729) turun ke polos.
    """
    if use_ssl is None and port == DEFAULT_SSL_PORT:
        try:
            return _connect_once(host, username, password, port, timeout, encoding, True)
        except ConnectionClosed as exc:
            if any(k in str(exc) for k in ("SSL", "TLS", "EOF")):
                return _connect_once(host, username, password, port, timeout, encoding, False)
            raise
    return _connect_once(
        host, username, password, port, timeout, encoding,
        port == DEFAULT_SSL_PORT if use_ssl is None else use_ssl,
    )


def _connect_once(host, username, password, port, timeout, encoding, use_ssl):
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise ConnectionClosed(f"tidak bisa terhubung ke {host}:{port} - {exc}") from exc
    sock.settimeout(timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if use_ssl:
        ctx = ssl.create_default_context()
        # mikrotik umumnya pakai sertifikat sendiri; jangan tolak mentah-mentah
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            sock = ctx.wrap_socket(sock, server_hostname=host)
        except (ssl.SSLError, OSError) as exc:
            sock.close()
            raise ConnectionClosed(f"gagal TLS ke {host}:{port} - {exc}") from exc
    api = Api(sock, encoding=encoding)
    api.use_ssl = use_ssl
    try:
        login(api, username, password)
    except LibRouterosError:
        api.close()
        raise
    except OSError as exc:
        api.close()
        raise ConnectionClosed(f"login gagal: {exc}") from exc
    return api


def ping_rows(api: Api, address: str) -> Iterable[dict]:
    """Bantuan kecil untuk /ping (dipakai tes & fitur ping)."""
    return api.call("/ping", address=address, count="1")


def iter_rows(rows: list[dict]) -> Iterator[dict]:
    return iter(rows)
