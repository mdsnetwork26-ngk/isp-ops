"""Uji protokol RouterOS API buatan sendiri (app/routeros.py).

Caranya: jalankan server mikrotik tiruan di localhost yang benar-benar
berbicara protokol API lewat socket (length-prefixed words), lalu minta
klien kita login dan menjalankan perintah. Jadi yang diuji bukan tiruan
objek, tapi byte yang lewat di kabel.
"""
from __future__ import annotations

import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.routeros import (
    Api,
    ConnectionClosed,
    FatalError,
    TrapError,
    compose_word,
    connect,
    encode_length,
    encode_word,
    parse_word,
)


# ------------------------------------------------------------------ encoding

@pytest.mark.parametrize(
    "n,hexa",
    [
        (0, "00"), (9, "09"), (0x7F, "7f"),
        (0x80, "8080"), (0x1F4, "81f4"), (0x2000, "a000"), (0x3FFF, "bfff"),
        (0x4000, "c04000"), (0x1FFFFF, "dfffff"),
        (0x200000, "e0200000"), (0xFFFFFFF, "efffffff"),
        (0x10000000, "f010000000"),
    ],
)
def test_encode_length(n, hexa):
    assert encode_length(n).hex() == hexa


def test_word():
    assert encode_word("abc").hex() == "03" + b"abc".hex()
    assert compose_word("list", "isolir") == "=list=isolir"
    assert compose_word("0") == "=0"
    assert parse_word("=address=10.0.0.5") == ("address", "10.0.0.5")
    assert parse_word("=.id=*7") == (".id", "*7")
    assert parse_word("!done") == ("!done", "")


# ------------------------------------------------------------------ server tiruan

def _recv_sentence(conn: socket.socket) -> list[str]:
    """Baca satu kalimat (daftar kata) dari klien."""
    words: list[str] = []
    while True:
        head = conn.recv(1)
        if not head:
            raise ConnectionError("klien menutup koneksi")
        first = head[0]
        if first < 0x80:
            n = first
        elif first < 0xC0:
            n = ((first & 0x3F) << 8) + conn.recv(1)[0]
        elif first < 0xE0:
            n = ((first & 0x1F) << 16) + int.from_bytes(conn.recv(2), "big")
        elif first < 0xF0:
            n = ((first & 0x0F) << 24) + int.from_bytes(conn.recv(3), "big")
        else:
            n = int.from_bytes(conn.recv(4), "big")
        if n == 0:
            return words
        buf = b""
        while len(buf) < n:
            buf += conn.recv(n - len(buf))
        words.append(buf.decode("utf-8", "replace"))


def _send_sentence(conn: socket.socket, *words: str) -> None:
    payload = b"".join(encode_word(w) for w in words) + encode_word("")
    try:
        conn.sendall(payload)
    except (BrokenPipeError, ConnectionResetError, OSError):
        # klien sudah menutup koneksi (mis. cek port SYN/RST) - abaikan saja
        pass


class FakeMikrotik:
    """Server API mikrotik tiruan untuk pengujian."""

    CHALLENGE = "5d7a14f6ee2c4a30b2c90da86b3a19f2"
    USER = "ispops"
    PASSWORD = "rahasia"

    def __init__(self, *, drop_after: int | None = None, bad_password: bool = False,
                 certfile: str = "", quirk: bool = False):
        self.drop_after = drop_after
        self.bad_password = bad_password
        self.certfile = certfile
        # quirk=True meniru mikrotik lapangan: menolak atribut 'comment' di
        # address-list add, menolak argumen posisi di filter insert, dan tidak
        # mengirim mac-address di /ppp active.
        self.quirk = quirk
        self.rule_isolir_ada = False
        # komentar rule firewall yang "sudah ada" (utk query ?comment=)
        self.rule_comments: set[str] = set()
        # state mode suspend: pool IP (usang) + profil PPP + tanpa sesi aktif.
        # "SUSPEND" sengaja SUDAH ada sejak awal: meniru profil suspend
        # yang dibuat manual oleh operator di MikroTik -- alur isolir baru
        # wajib memakainya, bukan membuatnya sendiri.
        self.pools: dict[str, str] = {}
        self.profiles: list[str] = ["default", "pppoe-6m", "pppoe-12m", "SUSPEND"]
        # profil secret yang berkeadaan: /ppp secret set profile=... harus
        # terbaca lagi oleh print berikutnya (untuk verifikasi isolir/buka)
        self.secret_profiles: dict[str, str] = {"budi-santoso": "pppoe-12m"}
        self.secret_disabled: dict[str, str] = {"budi-santoso": "false"}
        # tanpa_aktif=True meniru router yang TIDAK punya sesi PPP aktif sama
        # sekali (semua pelanggan offline) -- dipakai uji isolir mode suspend.
        self.tanpa_aktif = False
        self._challenge = ""
        # address-list berkeadaan, seperti mikrotik sungguhan
        self.address_list = {
            "*AA": {"address": "10.20.3.4", "list": "isolir", "comment": ""},
            "*AB": {"address": "10.20.9.9", "list": "isolir", "comment": ""},
        }
        self._al_seq = 0
        self.secrets_tambahan = []
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.commands: list[tuple[str, tuple[str, ...]]] = []
        self.sentence_count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "FakeMikrotik":
        if self.certfile:
            import ssl as _ssl

            ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.certfile)
            self.srv = ctx.wrap_socket(self.srv, server_side=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self.srv.close()
        except OSError:
            pass

    def _serve(self) -> None:
        """Layani setiap koneksi di thread sendiri.

        Mikrotik sungguhan bisa memegang beberapa sesi API sekaligus, dan skrip
        pemeriksa memang memakai dua koneksi berbarengan (satu untuk baca, satu
        lewat adapter). Kalau dilayani berurutan, sesi kedua menggantung.
        """
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> bool:
        """False = berhenti melayani (drop_after tercapai)."""
        with conn:
            conn.settimeout(10)
            try:
                return self._session(conn)
            except (BrokenPipeError, ConnectionResetError, OSError):
                import os, traceback
                if os.environ.get("FAKE_MIKROTIK_DEBUG"):
                    traceback.print_exc()
                return True

    def _session(self, conn: socket.socket) -> bool:
            logged_in = False
            while not self._stop.is_set():
                try:
                    words = _recv_sentence(conn)
                except (ConnectionError, OSError):
                    return True   # klien menutup koneksi; siap melayani sesi lain
                if not words:
                    continue
                self.sentence_count += 1
                if self.drop_after is not None and self.sentence_count > self.drop_after:
                    return False  # putuskan koneksi seenaknya, seperti jaringan jelek
                cmd = words[0]
                # mikrotik sungguhan menolak kata query yang ditulis '=?...=';
                # tiruan ini harus sama ketatnya supaya cacat ketahuan di tes
                if any(w.startswith("=?") for w in words[1:]):
                    _send_sentence(conn, "!trap", "=message=failure: unknown parameter")
                    _send_sentence(conn, "!done")
                    continue
                args = {}
                for w in words[1:]:
                    if w.startswith("?"):  # query word: '?nama=nilai'
                        k, _, v = w[1:].partition("=")
                        args["?" + k] = v
                    else:
                        k, v = parse_word(w)
                        args[k] = v
                self.commands.append((cmd, tuple(words[1:])))

                if cmd == "/login":
                    if "response" in args:
                        # cara challenge-response (librouteros): verifikasi MD5 sungguhan
                        import hashlib, binascii
                        sandi = "" if self.bad_password else self.PASSWORD
                        # harus persis sama dengan app.routeros._md5_hex (utf-8)
                        benar = "00" + hashlib.md5(
                            (chr(0) + sandi
                             + binascii.unhexlify(self._challenge).decode("latin-1")
                             ).encode("utf-8")
                        ).hexdigest()
                        if args.get("name") == self.USER and args.get("response") == benar:
                            logged_in = True
                            _send_sentence(conn, "!done")
                        else:
                            _send_sentence(conn, "!trap", "=message=cannot log in")
                            _send_sentence(conn, "!done")
                        continue
                    if "password" in args:
                        if args.get("name") == self.USER and args.get("password") == self.PASSWORD and not self.bad_password:
                            logged_in = True
                            _send_sentence(conn, "!done")
                        else:
                            _send_sentence(conn, "!trap", "=message=cannot log in")
                            _send_sentence(conn, "!done")
                        continue
                    self._challenge = self.CHALLENGE
                    _send_sentence(conn, "!re", f"=ret={self.CHALLENGE}")
                    _send_sentence(conn, "!done")
                    continue

                if not logged_in:
                    _send_sentence(conn, "!trap", "=message=not logged in")
                    _send_sentence(conn, "!done")
                    continue

                if cmd == "/system/resource/print":
                    _send_sentence(conn, "!re", "=version=7.15.3", "=board-name=CCR2004", "=uptime=12d3h")
                elif cmd == "/ppp/profile/print":
                    for i, p in enumerate(self.profiles):
                        if args.get("?name") and p != args["?name"]:
                            continue
                        _send_sentence(conn, "!re", f"=.id=*P{i}", f"=name={p}")
                elif cmd == "/ppp/profile/add":
                    nm = args.get("name", "")
                    if nm in self.profiles:
                        _send_sentence(conn, "!trap", "=message=failure: profile with such name already exists")
                        _send_sentence(conn, "!done")
                        continue
                    self.profiles.append(nm)
                elif cmd == "/ppp/secret/add":
                    if any(e.get("name") == args.get("name") for e in self.secrets_tambahan):
                        _send_sentence(conn, "!trap", "=message=failure: already have such entry")
                        _send_sentence(conn, "!done")
                        continue
                    self.secrets_tambahan.append({"name": args.get("name", ""),
                                                  "password": args.get("password", ""),
                                                  "profile": args.get("profile", "")})
                elif cmd == "/ppp/secret/set":
                    # berkeadaan: set profile/disabled harus terbaca print lagi
                    if args.get("profile"):
                        self.secret_profiles["budi-santoso"] = args["profile"]
                    if args.get("disabled"):
                        self.secret_disabled["budi-santoso"] = (
                            "true" if args["disabled"] == "yes" else "false"
                        )
                elif cmd == "/ppp/secret/print":
                    rows = [["=.id=*7", "=name=budi-santoso",
                             f"=disabled={self.secret_disabled.get('budi-santoso', 'false')}",
                             "=service=pppoe",
                             f"=profile={self.secret_profiles.get('budi-santoso', 'pppoe-12m')}"]]
                    rows += [[f"=.id=*T{i}", f"=name={e['name']}", "=disabled=false",
                              "=service=pppoe", f"=profile={e.get('profile', '')}"]
                             for i, e in enumerate(self.secrets_tambahan)]
                    for r in rows:
                        if args.get("?name") and f"=name={args['?name']}" not in r:
                            continue
                        _send_sentence(conn, "!re", *r)
                elif cmd == "/ppp/active/print":
                    if self.tanpa_aktif:
                        pass  # semua pelanggan offline: tidak ada baris sama sekali
                    else:
                        kolom = ["=.id=*B1", "=name=budi-santoso", "=address=10.20.3.4",
                                 "=uptime=1d2h3m4s", "=caller-id=ether3"]
                        if not self.quirk:
                            # RouterOS sungguhan mengirim remote-mac-address di /ppp active
                            kolom.append("=remote-mac-address=AA:BB:CC:11:22:33")
                        _send_sentence(conn, "!re", *kolom)
                elif cmd == "/interface/pppoe-server/print":
                    _send_sentence(conn, "!re", "=.id=*P1", "=name=pppoe-budi",
                                   "=user=budi-santoso", "=mac-address=AA:BB:CC:11:22:33")
                elif cmd == "/ip/arp/print":
                    _send_sentence(conn, "!re", "=.id=*AR1", "=interface=pppoe-budi",
                                   "=address=10.20.3.4", "=mac-address=AA:BB:CC:11:22:33")
                elif cmd == "/ip/firewall/filter/print":
                    # mikrotik sungguhan menyaring nilai ?comment=, tiruan juga harus
                    cm = args.get("?comment", "")
                    if cm and (cm in self.rule_comments
                               or (cm == "ISP-OPS ISOLIR" and self.rule_isolir_ada)):
                        _send_sentence(conn, "!re", "=.id=*F1", f"=comment={cm}")
                elif cmd == "/ip/firewall/address-list/print":
                    for rid in sorted(self.address_list):
                        e = self.address_list[rid]
                        if args.get("?list") and e["list"] != args["?list"]:
                            continue
                        if args.get("?address") and e["address"] != args["?address"]:
                            continue
                        _send_sentence(conn, "!re", f"=.id={rid}",
                                       f"=address={e['address']}", f"=list={e['list']}")
                elif cmd == "/ip/firewall/address-list/add":
                    if self.quirk and any(w.startswith("=comment") for w in words):
                        _send_sentence(conn, "!trap", "=message=failure: unknown parameter")
                        _send_sentence(conn, "!done")
                        continue          # tanpa ini !done terkirim dua kali -> protokol tidak sinkron
                    if any(e["address"] == args.get("address") and e["list"] == args.get("list")
                           for e in self.address_list.values()):
                        _send_sentence(conn, "!trap", "=message=failure: already have such entry (6)")
                        _send_sentence(conn, "!done")
                        continue
                    self._al_seq += 1
                    self.address_list[f"*AL{self._al_seq}"] = {
                        "address": args.get("address", ""), "list": args.get("list", ""),
                        "comment": args.get("comment", "")}
                elif cmd == "/ip/firewall/address-list/remove":
                    self.address_list.pop(args.get(".id", ""), None)
                elif cmd == "/ip/firewall/filter/insert":
                    if self.quirk:
                        _send_sentence(conn, "!trap", "=message=failure: unknown parameter")
                        _send_sentence(conn, "!done")
                        continue
                    self.rule_isolir_ada = True
                    self.rule_comments.add(args.get("comment", ""))
                elif cmd == "/ip/firewall/filter/add":
                    self.rule_isolir_ada = True
                    self.rule_comments.add(args.get("comment", ""))
                elif cmd == "/ip/pool/print":
                    for rid, (nm, rng) in self.pools.items():
                        if args.get("?name") and nm != args["?name"]:
                            continue
                        _send_sentence(conn, "!re", f"=.id={rid}",
                                       f"=name={nm}", f"=ranges={rng}")
                elif cmd == "/ip/pool/add":
                    self.pools[f"*PL{len(self.pools) + 1}"] = (
                        args.get("name", ""), args.get("ranges", ""))
                elif cmd == "/ping":
                    _send_sentence(conn, "!re", "=sent=1", "=received=1", "=avg-rtt=2.4ms")
                elif cmd == "/perintah/tidak/ada":
                    _send_sentence(conn, "!trap", "=message=no such command")
                elif cmd == "/perintah/fatal":
                    _send_sentence(conn, "!fatal", "=message=connection terminated")
                _send_sentence(conn, "!done")


@pytest.fixture()
def mikrotik():
    srv = FakeMikrotik().start()
    yield srv
    srv.stop()


# ------------------------------------------------------------------ uji klien

def test_login_dan_baca_data(mikrotik):
    api = connect("127.0.0.1", "ispops", "rahasia", port=mikrotik.port, timeout=5)
    try:
        info = api.call("/system/resource/print")[0]
        assert info["version"] == "7.15.3"
        assert info["board-name"] == "CCR2004"

        active = api.call("/ppp/active/print")[0]
        assert active["name"] == "budi-santoso"
        assert active["address"] == "10.20.3.4"
        assert active["uptime"] == "1d2h3m4s"

        secrets = api.call("/ppp/secret/print")
        assert secrets[0]["name"] == "budi-santoso"
    finally:
        api.close()


def test_perintah_tulis_terkirim_benar(mikrotik):
    api = connect("127.0.0.1", "ispops", "rahasia", port=mikrotik.port, timeout=5)
    try:
        api.raw_cmd("/ip/firewall/address-list/add", "=list=isolir", "=address=10.20.7.7",
                    "=comment=ISP-OPS ISOLIR | budi")
        api.raw_cmd("/ip/firewall/filter/insert", "=0", "=chain=forward",
                    "=src-address-list=isolir", "=action=drop")
    finally:
        api.close()

    tambah = [c for c in mikrotik.commands if c[0] == "/ip/firewall/address-list/add"][0][1]
    assert "=list=isolir" in tambah
    assert "=address=10.20.7.7" in tambah
    sisip = [c for c in mikrotik.commands if c[0] == "/ip/firewall/filter/insert"][0][1]
    assert "=0" in sisip and "=action=drop" in sisip


def test_trap_jadi_exception(mikrotik):
    api = connect("127.0.0.1", "ispops", "rahasia", port=mikrotik.port, timeout=5)
    try:
        with pytest.raises(TrapError) as exc:
            api.call("/perintah/tidak/ada")
        assert "no such command" in str(exc.value)

        # entri yang sudah ada -> trap juga; adapter kita yang menganggapnya sukses
        with pytest.raises(TrapError):
            api.raw_cmd("/ip/firewall/address-list/add", "=list=isolir", "=address=10.20.9.9")
    finally:
        api.close()


def test_fatal_jadi_fatalerror(mikrotik):
    api = connect("127.0.0.1", "ispops", "rahasia", port=mikrotik.port, timeout=5)
    try:
        with pytest.raises(FatalError):
            api.call("/perintah/fatal")
    finally:
        api.close()


def test_password_salah_ditolak():
    srv = FakeMikrotik(bad_password=True).start()
    try:
        with pytest.raises(Exception) as exc:
            connect("127.0.0.1", "ispops", "salah-total", port=srv.port, timeout=5)
        assert "cannot log in" in str(exc.value) or isinstance(exc.value, TrapError)
    finally:
        srv.stop()


def test_koneksi_putus_di_tengah_jalan():
    """Server memutus di tengah sesi -> klien melempar ConnectionClosed, bukan hang."""
    srv = FakeMikrotik(drop_after=5).start()   # login (3 kalimat) selesai dulu
    try:
        api = connect("127.0.0.1", "ispops", "rahasia", port=srv.port, timeout=5)
        with pytest.raises(ConnectionClosed):
            for _ in range(10):
                api.call("/ppp/active/print")
        api.close()
    finally:
        srv.stop()


def test_server_mati_sebelum_login_selesai():
    """Koneksi ditutup saat login -> ConnectionClosed, jelas penyebabnya."""
    srv = FakeMikrotik(drop_after=1).start()
    try:
        with pytest.raises(ConnectionClosed):
            connect("127.0.0.1", "ispops", "rahasia", port=srv.port, timeout=5)
    finally:
        srv.stop()


def test_adapter_pakai_protokol_ini(mikrotik):
    """Adapter RouterOSAPI (yang dipakai aplikasi) jalan di atas protokol ini."""
    from app.mikrotik.api import RouterOSAPI

    c = RouterOSAPI("127.0.0.1", "ispops", "rahasia", port=mikrotik.port, timeout=5)
    try:
        ok, msg = c.check()
        assert ok and "7.15.3" in msg, msg
        assert [s.name for s in c.ppp_secrets()] == ["budi-santoso"]
        assert c.ppp_active()[0]["address"] == "10.20.3.4"
        assert set(c.address_list_addresses("isolir")) == {"10.20.3.4", "10.20.9.9"}
        assert c.ping("10.20.3.4") == (1, 2.4)
        # panggilan pertama membuat rule, kedua mengetahui rule sudah ada
        assert c.ensure_isolir_rule("isolir", "drop", "ISP-OPS ISOLIR") is True
        assert c.ensure_isolir_rule("isolir", "drop", "ISP-OPS ISOLIR") is False
    finally:
        c.close()


# ------------------------------------------------------------------ api-ssl (8729)

CERT = "/tmp/tls/gab.pem"


@pytest.mark.skipif(not os.path.exists(CERT), reason="butuh sertifikat uji di /tmp/tls/gab.pem")
def test_api_ssl_port_8729():
    """Port 8729 = api-ssl. Klien harus otomatis memakai TLS dengan sertifikat mandiri."""
    from app.routeros import DEFAULT_SSL_PORT, connect

    srv = FakeMikrotik(certfile=CERT).start()
    try:
        assert DEFAULT_SSL_PORT == 8729, "port api-ssl harus 8729"
        # port server tiruan dinamis, jadi TLS dinyalakan lewat use_ssl=True
        api = connect("127.0.0.1", "ispops", "rahasia", port=srv.port, timeout=5, use_ssl=True)
        try:
            info = api.call("/system/resource/print")[0]
            assert info["version"] == "7.15.3"
            assert api.call("/ppp/active/print")[0]["address"] == "10.20.3.4"
        finally:
            api.close()
    finally:
        srv.stop()


@pytest.mark.skipif(not os.path.exists(CERT), reason="butuh sertifikat uji di /tmp/tls/gab.pem")
def test_adapter_lewat_ssl():
    """RouterOSAPI (yang dipakai aplikasi) juga harus bisa lewat TLS."""
    from app.mikrotik.api import RouterOSAPI

    srv = FakeMikrotik(certfile=CERT).start()
    try:
        c = RouterOSAPI("127.0.0.1", "ispops", "rahasia", port=srv.port, timeout=5,
                        use_ssl=True)
        try:
            ok, msg = c.check()
            assert ok and "7.15.3" in msg, msg
            assert [s.name for s in c.ppp_secrets()] == ["budi-santoso"]
        finally:
            c.close()
    finally:
        srv.stop()


@pytest.mark.skipif(not os.path.exists(CERT), reason="butuh sertifikat uji di /tmp/tls/gab.pem")
def test_tls_tanpa_ssl_gagal():
    """Menyerbu port TLS tanpa TLS harus gagal jelas, bukan menggantung."""
    from app.routeros import LibRouterosError, connect

    srv = FakeMikrotik(certfile=CERT).start()
    try:
        with pytest.raises((LibRouterosError, OSError)):
            connect("127.0.0.1", "ispops", "rahasia", port=srv.port, timeout=4, use_ssl=False)
    finally:
        srv.stop()


# ------------------------------------------------- TLS per router (kasus port 8729 polos)

class _RouterStub:
    """Baris Router dari database, secukupnya untuk make_client()."""

    def __init__(self, host, port, api_ssl):
        self.host, self.port, self.api_ssl = host, port, api_ssl
        self.username, self.password = "ispops", "rahasia"


def test_pilihan_tls_diterjemahkan_make_client():
    from app.mikrotik import make_client

    assert make_client(_RouterStub("h", 8729, "no")).use_ssl is False
    assert make_client(_RouterStub("h", 8728, "no")).use_ssl is False
    assert make_client(_RouterStub("h", 8728, "yes")).use_ssl is True
    assert make_client(_RouterStub("h", 8729, "auto")).use_ssl is None  # otomatis
    assert make_client(_RouterStub("h", 8729, "")).use_ssl is None


def test_port_8729_polos_bisa_dipakai_dengan_api_ssl_no():
    """Kasus nyata: mikrotik memindah service 'api' ke 8729, api-ssl invalid."""
    from app.mikrotik import make_client

    srv = FakeMikrotik().start()  # server POLOS (tanpa TLS)
    try:
        klien = make_client(_RouterStub("127.0.0.1", srv.port, "no"))
        assert klien.use_ssl is False
        ok, msg = klien.check()
        assert ok and "7.15.3" in msg, msg
        klien.close()
    finally:
        srv.stop()


def test_auto_turun_ke_polos_bila_tls_ditolak():
    """Kasus lapangan: port 8729 dipakai api POLOS. Mode 'auto' harus
    mencoba TLS, gagal, lalu turun ke polos - tanpa pengguna memilih apa pun."""
    srv = FakeMikrotik()
    srv.srv.close()
    import socket as _s
    srv.srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    srv.srv.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    try:
        srv.srv.bind(("127.0.0.1", 8729)); srv.srv.listen(5); srv.port = 8729
    except OSError:
        import pytest
        pytest.skip("port 8729 dipakai proses lain")
    srv.start()
    try:
        api = connect("127.0.0.1", "ispops", "rahasia", port=8729)  # use_ssl=None
        assert getattr(api, "use_ssl") is False
        assert api.call("/system/resource/print")[0]["version"] == "7.15.3"
        api.close()
    finally:
        srv.stop()
