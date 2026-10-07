"""HTTPS for phones, with a certificate the server makes itself.

Browsers only let a web page read the phone's GPS, and keep an offline copy of itself
(a service worker), on a secure origin: HTTPS or localhost. A boat has no domain name and
often no internet, so a certificate from a public authority is out of reach. Instead the
server makes, on first start, in ``<data_dir>/tls``:

- its own small certificate authority (CA), valid for 10 years and restricted by name
  constraints to private addresses and local names (.local, .lan, .home.arpa, .internal).
  Someone who copied its key could pose as devices on private networks to a phone that
  trusts it, but never as a website on the internet;
- a server certificate signed by that CA for this machine's local addresses and names. It
  is re-signed whenever those change (a new DHCP lease) and well before it expires; Apple
  devices accept at most 825 days.

A phone either accepts the browser's warning once, or installs the CA certificate (the
app's Settings link to /api/tls/ca.crt) and then gets no warning and the offline copy.
Users with a real certificate (``tailscale cert``, Let's Encrypt) set ``tls_cert`` and
``tls_key`` instead.

Only the standard library is used: RSA keys come from Python's own big integers and the
certificates are DER-encoded here, so neither the ``cryptography`` package (a native
dependency) nor an ``openssl`` command (missing on Windows) is needed. Python's ``ssl``
module, i.e. OpenSSL, does the actual TLS.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

KEY_BITS = 2048
PUBLIC_EXPONENT = 65537
CA_DAYS = 3650
SERVER_DAYS = 800               # Apple: at most 825 days for certificates from a private CA
RENEW_BEFORE_DAYS = 60
BACKDATE = dt.timedelta(days=2)  # tolerate a phone or server whose clock is a little behind
CA_BACKDATE = dt.timedelta(days=365)   # a CA made while the server's clock ran ahead still works
CHECK_EVERY_S = 600             # how often the running server looks for new addresses

# What the CA may sign for. Names: these suffixes (and the name itself). Addresses: private
# ranges, Tailscale/carrier-grade NAT, link-local (a direct cable with no DHCP), loopback.
PERMITTED_SUFFIXES = ("local", "lan", "home.arpa", "internal", "localhost")
PERMITTED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
    "127.0.0.0/8", "fc00::/7", "::1/128"))

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_warned: set[str] = set()


# ---------------------------------------------------------------------------- names

def is_permitted(name: str) -> bool:
    """Whether the server-made CA may vouch for this name or address."""
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        labels = name.lower().rstrip(".").split(".")
        if not all(_LABEL.match(x) for x in labels):
            return False
        host = ".".join(labels)
        return any(host == s or host.endswith("." + s) for s in PERMITTED_SUFFIXES)
    return any(ip in net for net in PERMITTED_NETS)


def _linux_addresses() -> set[str]:
    """Every local IPv4 address, from the kernel's routing table (no extra tools needed)."""
    out: set[str] = set()
    try:
        lines = Path("/proc/net/fib_trie").read_text().splitlines()
    except OSError:
        return out
    prev = ""
    for line in lines:
        if "/32 host LOCAL" in line and prev:
            out.add(prev)
        m = re.search(r"\|--\s+(\d+\.\d+\.\d+\.\d+)", line)
        prev = m.group(1) if m else ""
    return out


def local_addresses() -> set[str]:
    """This machine's IP addresses, as well as the standard library can tell."""
    found: set[str] = set()
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(str(info[4][0]).split("%")[0])
    # The address of the interface a packet would leave from. A UDP "connect" sends nothing.
    for target in ("10.255.255.255", "192.168.255.255", "172.31.255.255", "8.8.8.8"):
        with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target, 9))
            found.add(s.getsockname()[0])
    if sys.platform.startswith("linux"):
        found |= _linux_addresses()
    return found


def certificate_names(extra: list[str] | tuple[str, ...] = (), host: str | None = None,
                      addresses: set[str] | None = None, hostname: str | None = None) -> list[str]:
    """Names and addresses for the server certificate: localhost, <hostname>.local, every
    private address of this machine, and ``tls_names`` from the config."""
    names = {"localhost", "127.0.0.1", "::1"}
    hostname = (hostname if hostname is not None else socket.gethostname()).split(".")[0].lower()
    if _LABEL.match(hostname):
        names.add(f"{hostname}.local")
    for a in addresses if addresses is not None else local_addresses():
        names.add(a)
    if host and host not in ("0.0.0.0", "::", ""):
        names.add(host)
    for n in extra:
        n = str(n).strip().lower().rstrip(".")
        if n and not is_permitted(n) and n not in _warned:
            _warned.add(n)
            log.warning("tls_names: %s is not a private address or local name (.local, .lan, .home.arpa, "
                        ".internal); left out of the certificate. Use your own certificate (tls_cert) for it.", n)
        names.add(n)
    keep = [n for n in names if n and is_permitted(n)]

    def order(n: str):
        try:
            ip = ipaddress.ip_address(n)
            return (1, ip.version, ip.packed)
        except ValueError:
            return (0, 0, n.encode())
    return sorted(keep, key=order)


# ---------------------------------------------------------------------------- RSA

_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]
_SMALL_PRODUCT = math.prod(_SMALL_PRIMES)


def _probably_prime(n: int, rounds: int = 8) -> bool:
    """Miller-Rabin with random bases (after trial division), as in FIPS 186-4 C.3."""
    if math.gcd(n, _SMALL_PRODUCT) != 1:
        return n in _SMALL_PRIMES
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _random_prime(bits: int) -> int:
    while True:
        # Top two bits set, so the product of two such primes has exactly 2 * bits bits.
        c = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if c % PUBLIC_EXPONENT != 1 and _probably_prime(c):
            return c


@dataclass(frozen=True)
class RsaKey:
    n: int
    e: int
    d: int
    p: int
    q: int

    @classmethod
    def generate(cls, bits: int = KEY_BITS) -> RsaKey:
        while True:
            p, q = _random_prime(bits // 2), _random_prime(bits // 2)
            if p == q:
                continue
            n = p * q
            lam = math.lcm(p - 1, q - 1)
            if n.bit_length() == bits and math.gcd(PUBLIC_EXPONENT, lam) == 1:
                return cls(n, PUBLIC_EXPONENT, pow(PUBLIC_EXPONENT, -1, lam), max(p, q), min(p, q))

    @property
    def size(self) -> int:
        return (self.n.bit_length() + 7) // 8

    def public_der(self) -> bytes:
        """RSAPublicKey (PKCS #1)."""
        return _seq(_int(self.n), _int(self.e))

    def private_pem(self) -> str:
        """RSAPrivateKey (PKCS #1), which OpenSSL and Python's ssl module load directly."""
        p, q, d = self.p, self.q, self.d
        der = _seq(_int(0), _int(self.n), _int(self.e), _int(d), _int(p), _int(q),
                   _int(d % (p - 1)), _int(d % (q - 1)), _int(pow(q, -1, p)))
        return _pem("RSA PRIVATE KEY", der)

    @classmethod
    def from_pem(cls, text: str) -> RsaKey:
        """Raises ValueError for anything that isn't a sound PKCS #1 RSA key."""
        try:
            der = _pem_body(text, "RSA PRIVATE KEY")
        except Exception as exc:          # binascii.Error and friends
            raise ValueError(f"unreadable RSA private key: {exc}") from exc
        tag, body, _ = _read_tlv(der, 0)
        if tag != 0x30:
            raise ValueError("not an RSA private key")
        values, i = [], 0
        while i < len(body):
            tag, v, i = _read_tlv(body, i)
            values.append(int.from_bytes(v, "big"))
        if len(values) < 9:
            raise ValueError("incomplete RSA private key")
        _, n, e, d, p, q = values[:6]
        if p * q != n or pow(pow(2, e, n), d, n) != 2:
            raise ValueError("inconsistent RSA private key")
        return cls(n, e, d, p, q)

    def sign(self, data: bytes) -> bytes:
        """RSASSA-PKCS1-v1_5 with SHA-256 (the signature every browser accepts)."""
        t = _SHA256_DIGEST_INFO + hashlib.sha256(data).digest()
        em = b"\x00\x01" + b"\xff" * (self.size - len(t) - 3) + b"\x00" + t
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(self.size, "big")


_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


# ---------------------------------------------------------------------------- DER

def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _int(v: int) -> bytes:
    return _tlv(0x02, v.to_bytes(v.bit_length() // 8 + 1, "big"))   # room for a leading 0 bit


def _oid(dotted: str) -> bytes:
    a, b, *rest = (int(x) for x in dotted.split("."))
    body = bytearray([40 * a + b])
    for v in rest:
        chunk = [v & 0x7F]
        v >>= 7
        while v:
            chunk.append(0x80 | (v & 0x7F))
            v >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, bytes(body))


def _bits(b: bytes, unused: int = 0) -> bytes:
    return _tlv(0x03, bytes([unused]) + b)


def _octets(b: bytes) -> bytes:
    return _tlv(0x04, b)


def _utf8(s: str) -> bytes:
    return _tlv(0x0C, s.encode())


def _time(t: dt.datetime) -> bytes:
    t = t.astimezone(dt.UTC)
    if t.year < 2050:
        return _tlv(0x17, t.strftime("%y%m%d%H%M%SZ").encode())       # UTCTime
    return _tlv(0x18, t.strftime("%Y%m%d%H%M%SZ").encode())           # GeneralizedTime


def _read_tlv(buf: bytes, i: int) -> tuple[int, bytes, int]:
    if i + 2 > len(buf):
        raise ValueError("truncated DER")
    tag, n = buf[i], buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        if not 0 < k <= 4 or i + k > len(buf):
            raise ValueError("bad DER length")
        n = int.from_bytes(buf[i:i + k], "big")
        i += k
    if i + n > len(buf):
        raise ValueError("truncated DER")
    return tag, buf[i:i + n], i + n


def _pem(label: str, der: bytes) -> str:
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def _pem_body(text: str, label: str) -> bytes:
    m = re.search(rf"-----BEGIN {label}-----(.*?)-----END {label}-----", text, re.S)
    if not m:
        raise ValueError(f"no {label} found")
    return base64.b64decode("".join(m.group(1).split()))


def pem_to_der(text: str) -> bytes:
    """The first certificate in a PEM file, as DER (the form phones install)."""
    return _pem_body(text, "CERTIFICATE")


def fingerprint(der: bytes) -> str:
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


# ---------------------------------------------------------------------------- certificates

_SHA256_WITH_RSA = _seq(_oid("1.2.840.113549.1.1.11"), b"\x05\x00")
_RSA = _seq(_oid("1.2.840.113549.1.1.1"), b"\x05\x00")
_CRITICAL = b"\x01\x01\xff"


def _name(common_name: str) -> bytes:
    def rdn(oid: str, value: str) -> bytes:
        return _tlv(0x31, _seq(_oid(oid), _utf8(value)))
    return _seq(rdn("2.5.4.10", "SHWeather"), rdn("2.5.4.3", common_name))


def _ext(oid: str, value: bytes, critical: bool = False) -> bytes:
    return _seq(_oid(oid), _CRITICAL if critical else b"", _octets(value))


def _key_id(key: RsaKey) -> bytes:
    return hashlib.sha1(key.public_der()).digest()   # RFC 5280 4.2.1.2 method (1), not a security use


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _general_name(name: str) -> bytes:
    try:
        return _tlv(0x87, ipaddress.ip_address(name).packed)          # iPAddress
    except ValueError:
        return _tlv(0x82, name.encode("ascii"))                        # dNSName


def _name_constraints() -> bytes:
    subtrees = [_seq(_tlv(0x82, s.encode())) for s in PERMITTED_SUFFIXES]
    subtrees += [_seq(_tlv(0x87, net.network_address.packed + net.netmask.packed)) for net in PERMITTED_NETS]
    return _seq(_tlv(0xA0, b"".join(subtrees)))                         # permittedSubtrees only


def _certificate(subject: str, key: RsaKey, issuer: str, issuer_key: RsaKey, days: int, *,
                 extensions: list[bytes], now: dt.datetime, backdate: dt.timedelta = BACKDATE) -> bytes:
    tbs = _seq(
        _tlv(0xA0, _int(2)),                                            # v3
        _int(secrets.randbits(126) | 1 << 126),
        _SHA256_WITH_RSA,
        _name(issuer),
        _seq(_time(now - backdate), _time(now + dt.timedelta(days=days))),
        _name(subject),
        _seq(_RSA, _bits(key.public_der())),
        _tlv(0xA3, _seq(*extensions)),
    )
    return _seq(tbs, _SHA256_WITH_RSA, _bits(issuer_key.sign(tbs)))


def make_ca(key: RsaKey, name: str, now: dt.datetime | None = None, days: int = CA_DAYS) -> bytes:
    now = now or dt.datetime.now(dt.UTC)
    return _certificate(name, key, name, key, days, backdate=CA_BACKDATE, now=now, extensions=[
        _ext("2.5.29.19", _seq(b"\x01\x01\xff", _int(0)), critical=True),   # CA, no sub-CAs
        _ext("2.5.29.15", _bits(b"\x86", 1), critical=True),                # sign certs, CRLs, data
        _ext("2.5.29.14", _octets(_key_id(key))),
        _ext("2.5.29.30", _name_constraints(), critical=True),
    ])


def make_server_cert(key: RsaKey, names: list[str], ca_key: RsaKey, ca_name: str,
                     now: dt.datetime | None = None, days: int = SERVER_DAYS) -> bytes:
    now = now or dt.datetime.now(dt.UTC)
    # The subject names a host the CA may vouch for, should a verifier look at it.
    cn = next((n for n in names if not _is_ip(n)), "localhost")
    return _certificate(cn, key, ca_name, ca_key, days, now=now, extensions=[
        _ext("2.5.29.19", _seq(), critical=True),                           # not a CA
        _ext("2.5.29.15", _bits(b"\xa0", 5), critical=True),                # digitalSignature, keyEncipherment
        _ext("2.5.29.37", _seq(_oid("1.3.6.1.5.5.7.3.1"))),                 # TLS server
        _ext("2.5.29.14", _octets(_key_id(key))),
        _ext("2.5.29.35", _seq(_tlv(0x80, _key_id(ca_key)))),
        _ext("2.5.29.17", _seq(*(_general_name(n) for n in names))),
    ])


# ---------------------------------------------------------------------------- files

@dataclass
class TlsFiles:
    cert: Path
    key: Path
    own: bool                       # made by this server (False: the user's tls_cert)
    names: list[str] = field(default_factory=list)
    not_after: float | None = None
    ca_fingerprint: str | None = None
    renewed: bool = False

    def public_info(self) -> dict:
        return {"own_certificate": self.own, "names": self.names, "expires": self.not_after,
                "ca_fingerprint": self.ca_fingerprint}


def _write(path: Path, text: str, private: bool = False) -> None:
    """Write atomically; private keys are readable by the service's user only (POSIX).

    On Windows the files inherit the data folder's permissions (see docs/DEPLOYMENT.md).
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text)
    if private and os.name == "posix":
        os.chmod(tmp, 0o600)   # an older tmp file keeps its mode through O_TRUNC
    os.replace(tmp, path)


def tls_dir(settings) -> Path:
    return Path(settings.data_dir) / "tls"


def ensure_certificates(settings, now: dt.datetime | None = None, names: list[str] | None = None,
                        bits: int = KEY_BITS) -> TlsFiles:
    """The certificate and key to serve HTTPS with, making or renewing them as needed."""
    if settings.tls_cert and settings.tls_key:
        return TlsFiles(Path(settings.tls_cert), Path(settings.tls_key), own=False)
    now = now or dt.datetime.now(dt.UTC)
    d = tls_dir(settings)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    files = {k: d / f for k, f in (("ca_key", "ca.key"), ("ca_crt", "ca.crt"), ("key", "server.key"),
                                   ("crt", "server.crt"), ("state", "state.json"))}
    state: dict = {}
    with contextlib.suppress(OSError, ValueError):
        state = json.loads(files["state"].read_text())

    t = now.timestamp()
    renew_before = RENEW_BEFORE_DAYS * 86400
    ca_key = ca_der = None
    if files["ca_key"].is_file() and files["ca_crt"].is_file() and state.get("ca_name"):
        try:
            ca_key = RsaKey.from_pem(files["ca_key"].read_text())
            ca_der = pem_to_der(files["ca_crt"].read_text())
        except Exception:
            log.warning("the HTTPS certificate authority in %s is damaged; making a new one "
                        "(phones that installed the old one need the new one)", d)
            ca_key = None
        if ca_key is not None and state.get("ca_not_after", float("inf")) - t < renew_before:
            log.warning("the HTTPS certificate authority expires soon; making a new one "
                        "(phones that installed the old one need the new one)")
            ca_key = None
    if ca_key is None:
        log.info("making this server's HTTPS certificate authority (once; takes a few seconds on a Pi)")
        ca_key = RsaKey.generate(bits)
        host = socket.gethostname().split(".")[0] or "server"
        state = {"ca_name": f"SHWeather CA ({host}, {now:%Y-%m-%d})",
                 "ca_not_after": (now + dt.timedelta(days=CA_DAYS)).timestamp()}
        ca_der = make_ca(ca_key, state["ca_name"], now)
        _write(files["ca_key"], ca_key.private_pem(), private=True)
        _write(files["ca_crt"], _pem("CERTIFICATE", ca_der))
        _write(files["state"], json.dumps(state))
    ca_fp = fingerprint(ca_der)

    def readable(path: Path, load) -> bool:
        try:
            load(path.read_text())
            return True
        except Exception:
            return False

    names = names if names is not None else certificate_names(settings.tls_names, settings.host)
    renew = (not readable(files["key"], RsaKey.from_pem) or not readable(files["crt"], pem_to_der)
             or (state.get("not_after") or 0) - t < renew_before
             or t < (state.get("not_before") or 0)          # signed while the clock ran ahead
             or state.get("names") != names or state.get("ca_fingerprint") != ca_fp)
    if renew:
        key = None
        with contextlib.suppress(OSError, ValueError):
            key = RsaKey.from_pem(files["key"].read_text())
        if key is None:
            key = RsaKey.generate(bits)
            _write(files["key"], key.private_pem(), private=True)
        # Never outlive the CA.
        ca_left = (state["ca_not_after"] - t) // 86400 if "ca_not_after" in state else SERVER_DAYS
        days = int(max(1, min(SERVER_DAYS, ca_left)))
        leaf = make_server_cert(key, names, ca_key, state["ca_name"], now, days=days)
        # Leaf first, then the CA: phones that trust the CA find the whole chain.
        _write(files["crt"], _pem("CERTIFICATE", leaf) + _pem("CERTIFICATE", ca_der))
        state.update(names=names, ca_fingerprint=ca_fp, not_before=(now - BACKDATE).timestamp(),
                     not_after=(now + dt.timedelta(days=days)).timestamp())
        _write(files["state"], json.dumps(state))
        log.info("HTTPS certificate for %s", ", ".join(names))
    return TlsFiles(files["crt"], files["key"], own=True, names=names, not_after=state["not_after"],
                    ca_fingerprint=ca_fp, renewed=renew)


def ca_certificate(settings) -> str | None:
    """The server-made CA certificate (PEM), if there is one."""
    path = tls_dir(settings) / "ca.crt"
    try:
        return path.read_text()
    except OSError:
        return None


# ---------------------------------------------------------------------------- the HTTPS listener

def _secondary_server_class():
    import uvicorn

    class SecondaryServer(uvicorn.Server):
        """A uvicorn server that leaves signals to the main (HTTP) server and its lifespan."""

        on_started = None

        def install_signal_handlers(self) -> None:      # uvicorn < 0.29
            pass

        @contextlib.contextmanager
        def capture_signals(self):                      # uvicorn >= 0.29
            yield

        async def startup(self, sockets=None) -> None:
            await super().startup(sockets=sockets)
            if self.started and self.on_started:
                self.on_started()

    return SecondaryServer


class HttpsListener:
    """Serves the same app over HTTPS next to the plain-HTTP server.

    Started and stopped by the app's lifespan, so it runs exactly as long as the service it
    shares. Problems (a busy port, a broken certificate) are logged and shown in
    /api/status; they never stop the HTTP side.
    """

    def __init__(self, app, settings, service):
        self.app, self.settings, self.service = app, settings, service
        self.server = None
        self.task: asyncio.Task | None = None
        self._stopping = False
        self.info: dict = {"enabled": True, "port": settings.https_port, "running": False}

    def start(self) -> None:
        self.service.https = self.info
        self.task = asyncio.create_task(self._run(), name="https")

    async def stop(self) -> None:
        self._stopping = True         # also covers a listener still making its certificate
        if self.server is not None:
            self.server.should_exit = True
        if self.task:
            try:
                await asyncio.wait_for(self.task, 10)
            except (TimeoutError, asyncio.CancelledError):
                self.task.cancel()
            except Exception:
                log.exception("HTTPS listener failed while stopping")

    async def _run(self) -> None:
        import uvicorn

        st = self.settings
        try:
            files = await asyncio.to_thread(ensure_certificates, st)
        except Exception as exc:
            log.exception("HTTPS is off: could not prepare its certificate")
            self.info["error"] = f"certificate: {exc}"
            return
        if self._stopping:
            return
        self.info.update(files.public_info())
        config = uvicorn.Config(self.app, host=st.host, port=st.https_port, ssl_certfile=str(files.cert),
                                ssl_keyfile=str(files.key), lifespan="off", log_config=None,
                                log_level=st.log_level, access_log=st.access_log, proxy_headers=True)
        self.server = _secondary_server_class()(config)

        def started() -> None:
            self.info["running"] = True
            log.info("HTTPS on port %d (phones can use their GPS there)", st.https_port)
        self.server.on_started = started
        if self._stopping:
            return
        watcher = asyncio.create_task(self._watch(config, files))
        try:
            await self.server.serve()
        except SystemExit:
            # uvicorn exits when it cannot listen; keep the HTTP server running regardless.
            from .__main__ import port_problem
            hint = (port_problem(st.host, st.https_port, setting="https_port")
                    or f"could not listen on port {st.https_port}")
            log.error("HTTPS is off: %s", hint)
            self.info["error"] = hint
        except Exception as exc:
            log.exception("HTTPS is off")
            self.info["error"] = str(exc)
        finally:
            self.info["running"] = False
            if watcher:
                watcher.cancel()

    async def _watch(self, config, files: TlsFiles) -> None:
        """Re-sign our certificate when this machine's addresses change or it nears expiry;
        reload your own (tls_cert) when it is renewed on disk. New connections get the new one."""
        def mtimes(f: TlsFiles):
            try:
                return f.cert.stat().st_mtime, f.key.stat().st_mtime
            except OSError:
                return None
        seen = mtimes(files)
        while True:
            await asyncio.sleep(CHECK_EVERY_S)
            try:
                files = await asyncio.to_thread(ensure_certificates, self.settings)
                now_seen = mtimes(files)
                if (files.renewed or now_seen != seen) and config.ssl is not None and now_seen:
                    config.ssl.load_cert_chain(str(files.cert), str(files.key))
                    log.info("HTTPS certificate reloaded")
                seen = now_seen
                self.info.update(files.public_info())
            except Exception:
                log.exception("checking the HTTPS certificate failed")
