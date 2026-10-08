"""Built-in HTTPS: the server's own CA and certificate, and the second (HTTPS) listener."""

import datetime as dt
import hashlib
import json
import socket
import ssl
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from shweather import tls
from shweather.app import create_app
from shweather.config import Settings

NAMES = ["boat.lan", "localhost", "127.0.0.1", "::1"]


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    """One CA + server certificate for the whole module (key generation takes a moment)."""
    st = Settings(data_dir=tmp_path_factory.mktemp("tls"))
    return st, tls.ensure_certificates(st, names=NAMES)


def handshake(server: ssl.SSLContext, client: ssl.SSLContext, hostname: str) -> None:
    """A TLS handshake in memory; raises ssl.SSLCertVerificationError when the client refuses."""
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    c = client.wrap_bio(c_in, c_out, server_hostname=hostname)
    s = server.wrap_bio(s_in, s_out, server_side=True)
    done = set()
    for _ in range(20):
        for name, end in (("client", c), ("server", s)):
            if name in done:
                continue
            try:
                end.do_handshake()
                done.add(name)
            except ssl.SSLWantReadError:
                pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if len(done) == 2:
            return
    raise AssertionError("handshake did not finish")


def contexts(files, ca_path):
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(files.cert, files.key)
    client = ssl.create_default_context(cafile=str(ca_path))
    return server, client


def test_phones_that_trust_the_ca_accept_the_server(made):
    st, files = made
    assert files.own and files.renewed and files.names == NAMES
    server, client = contexts(files, tls.tls_dir(st) / "ca.crt")
    for name in ("boat.lan", "localhost", "127.0.0.1", "::1"):
        handshake(server, client, name)
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(server, client, "other.lan")          # not in the certificate


def test_the_ca_cannot_vouch_for_real_websites(made, tmp_path):
    """Name constraints: even with the CA's key, a certificate for a public name or address fails."""
    st, files = made
    d = tls.tls_dir(st)
    ca_key = tls.RsaKey.from_pem((d / "ca.key").read_text())
    ca_name = json.loads((d / "state.json").read_text())["ca_name"]
    key = tls.RsaKey.from_pem((d / "server.key").read_text())
    client = ssl.create_default_context(cafile=str(d / "ca.crt"))
    for name in ("www.example.com", "8.8.8.8", "mainrig"):
        cert = tmp_path / "evil.crt"
        cert.write_text(tls._pem("CERTIFICATE", tls.make_server_cert(key, [name], ca_key, ca_name)))
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert, d / "server.key")
        with pytest.raises(ssl.SSLCertVerificationError):
            handshake(server, client, name)


def test_certificates_are_reused_and_renewed_when_needed(made):
    st, files = made
    d = tls.tls_dir(st)
    ca_before, key_before = (d / "ca.crt").read_text(), (d / "server.key").read_text()
    again = tls.ensure_certificates(st, names=NAMES)
    assert not again.renewed and again.ca_fingerprint == files.ca_fingerprint
    # a new address (DHCP) -> re-signed with the same key and CA
    moved = tls.ensure_certificates(st, names=[*NAMES, "192.168.1.77"])
    assert moved.renewed and "192.168.1.77" in moved.names
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=tls.SERVER_DAYS - tls.RENEW_BEFORE_DAYS + 1)
    assert tls.ensure_certificates(st, names=[*NAMES, "192.168.1.77"], now=later).renewed   # before it expires
    assert (d / "ca.crt").read_text() == ca_before and (d / "server.key").read_text() == key_before
    # phones download the CA in DER form
    der = tls.pem_to_der(ca_before)
    assert der[0] == 0x30 and tls.fingerprint(der) == files.ca_fingerprint


def test_rsa_keys_survive_a_round_trip():
    key = tls.RsaKey.generate(1024)
    assert key.n.bit_length() == 1024
    assert tls.RsaKey.from_pem(key.private_pem()) == key
    sig = key.sign(b"hello")
    em = pow(int.from_bytes(sig, "big"), key.e, key.n).to_bytes(key.size, "big")
    t = tls._SHA256_DIGEST_INFO + hashlib.sha256(b"hello").digest()
    assert em == b"\x00\x01" + b"\xff" * (key.size - len(t) - 3) + b"\x00" + t


def test_certificate_names_keep_to_the_local_network():
    names = tls.certificate_names(["boat.lan", "example.com", "10.0.0.5"], host="0.0.0.0",
                                  addresses={"192.168.1.20", "8.8.8.8", "fe80::1", "100.101.102.103"},
                                  hostname="MainRig")
    assert names[:3] == ["boat.lan", "localhost", "mainrig.local"]
    assert {"10.0.0.5", "192.168.1.20", "100.101.102.103", "127.0.0.1", "::1"} <= set(names)
    assert not {"example.com", "8.8.8.8", "fe80::1", "mainrig"} & set(names)
    assert tls.is_permitted("pi.home.arpa") and not tls.is_permitted("lan.example.com")


def test_your_own_certificate_is_used_as_it_is(tmp_path):
    st = Settings(data_dir=tmp_path, tls_cert="certs/boat.crt", tls_key="certs/boat.key")
    files = tls.ensure_certificates(st)
    assert not files.own and files.cert.name == "boat.crt"
    assert not (tmp_path / "tls").exists()


def test_https_settings_are_checked():
    with pytest.raises(ValidationError, match="https_port"):
        Settings(port=8443, https_port=8443)
    assert Settings(port=8443).https_port == 9443        # an older install on 8443 keeps starting
    with pytest.raises(ValidationError, match="tls_cert and tls_key"):
        Settings(tls_cert="x.crt")
    assert Settings().https_port == 8443 and Settings(https_port=0).https_port == 0
    assert Settings(tls_names=None).tls_names == []


def test_ca_download_only_while_the_server_uses_its_own_certificate(made):
    st, files = made
    with TestClient(create_app(st, start_background=False)) as c:
        assert c.get("/api/tls/ca.crt").status_code == 404
        c.app.state.service.https = {"enabled": True, "running": True, **files.public_info()}
        r = c.get("/api/tls/ca.crt")
        assert r.status_code == 200 and r.headers["content-type"] == "application/x-x509-ca-cert"
        assert tls.fingerprint(r.content) == files.ca_fingerprint
        assert "BEGIN CERTIFICATE" in c.get("/api/tls/ca.crt?format=pem").text
        assert c.get("/api/status").json()["https"]["ca_fingerprint"] == files.ca_fingerprint


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(fn, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_https_listener_serves_the_app(tmp_path):
    port = free_port()
    st = Settings(data_dir=tmp_path, demo=True, host="127.0.0.1", https_port=port)
    with TestClient(create_app(st, start_background=False, serve_https=True)) as c:
        info = wait_for(lambda: (h := c.get("/api/status").json()["https"]).get("running") and h)
        assert info["port"] == port and info["own_certificate"] and "localhost" in info["names"]
        ctx = ssl.create_default_context(cafile=str(tmp_path / "tls" / "ca.crt"))
        with urllib.request.urlopen(f"https://localhost:{port}/api/status", context=ctx, timeout=10) as r:
            assert json.load(r)["https"]["running"] is True
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/", context=ctx, timeout=10) as r:
            assert b"SHWeather" in r.read()
    assert c.app.state.service.https["running"] is False       # stopped with the app


def test_busy_https_port_leaves_http_working(tmp_path):
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        st = Settings(data_dir=tmp_path, demo=True, host="127.0.0.1", https_port=port)
        with TestClient(create_app(st, start_background=False, serve_https=True)) as c:
            info = wait_for(lambda: (h := c.get("/api/status").json()["https"]).get("error") and h)
            assert f"Port {port} is already in use" in info["error"] and "https_port" in info["error"]
            assert "HTTPS cannot start" in info["error"]
            assert not info["running"]
            assert c.get("/api/now").status_code in (200, 409)


def test_damaged_or_expiring_files_are_replaced(tmp_path):
    st = Settings(data_dir=tmp_path)
    d = tls.tls_dir(st)
    first = tls.ensure_certificates(st, names=NAMES, bits=1024)
    (d / "server.key").write_text("x")                      # damaged: a new key and certificate
    again = tls.ensure_certificates(st, names=NAMES, bits=1024)
    assert again.ca_fingerprint == first.ca_fingerprint and again.renewed
    assert tls.RsaKey.from_pem((d / "server.key").read_text())
    (d / "ca.key").write_text("-----BEGIN RSA PRIVATE KEY-----\nMAA=\n-----END RSA PRIVATE KEY-----\n")
    fresh = tls.ensure_certificates(st, names=NAMES, bits=1024)
    assert fresh.ca_fingerprint != first.ca_fingerprint and fresh.renewed
    # the CA nears its end: a new one, before phones start refusing it
    late = dt.datetime.now(dt.UTC) + dt.timedelta(days=tls.CA_DAYS - 10)
    assert tls.ensure_certificates(st, names=NAMES, bits=1024, now=late).ca_fingerprint != fresh.ca_fingerprint


def test_certificate_signed_while_the_clock_ran_ahead_is_redone(tmp_path):
    st = Settings(data_dir=tmp_path)
    ahead = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
    made = tls.ensure_certificates(st, names=NAMES, bits=1024, now=ahead)
    fixed = tls.ensure_certificates(st, names=NAMES, bits=1024)          # the clock is right again
    assert fixed.renewed and fixed.ca_fingerprint == made.ca_fingerprint    # same CA: phones keep it
    assert not tls.ensure_certificates(st, names=NAMES, bits=1024).renewed


def test_server_certificate_never_outlives_its_ca(tmp_path):
    st = Settings(data_dir=tmp_path)
    tls.ensure_certificates(st, names=NAMES, bits=1024)
    late = dt.datetime.now(dt.UTC) + dt.timedelta(days=tls.CA_DAYS - 100)
    files = tls.ensure_certificates(st, names=NAMES, bits=1024, now=late)
    state = json.loads((tls.tls_dir(st) / "state.json").read_text())
    assert files.not_after <= state["ca_not_after"] + 86400


def test_bad_keys_are_value_errors():
    for text in ("", "-----BEGIN RSA PRIVATE KEY-----\n!!!\n-----END RSA PRIVATE KEY-----\n",
                 "-----BEGIN RSA PRIVATE KEY-----\nMAA=\n-----END RSA PRIVATE KEY-----\n",
                 "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----\n"):
        with pytest.raises(ValueError):
            tls.RsaKey.from_pem(text)


def test_server_certificate_subject_is_a_permitted_name(made):
    st, files = made
    pem = (tls.tls_dir(st) / "server.crt").read_text()
    der = tls.pem_to_der(pem)
    assert b"boat.lan" in der[:600]                                      # CN = the first name


def test_stopping_while_the_certificate_is_made_is_quick(tmp_path):
    st = Settings(data_dir=tmp_path, demo=True, host="127.0.0.1", https_port=free_port())
    started = time.monotonic()
    with TestClient(create_app(st, start_background=False, serve_https=True)):
        pass
    assert time.monotonic() - started < 8


def test_cli_port_override_moves_https_aside(tmp_path):
    from shweather.__main__ import main

    cfg = tmp_path / "config.yaml"
    cfg.write_text("data_dir: data\n")
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(["--config", str(cfg), "config"])
    assert json.loads(out.getvalue())["https_port"] == 8443
    from shweather import __main__ as cli
    args = type("A", (), {"config": str(cfg), "port": 8443, "host": None, "demo": False})()
    assert cli._settings(args).https_port == 9443
