"""Self-signed TLS certificate generator for ATLAS.

Why this exists
---------------
Modern browsers refuse the microphone (and a handful of other powerful
features) on insecure-origin pages. Chrome treats ``http://localhost``,
``http://127.0.0.1``, and ``http://[::1]`` as secure contexts, but a raw
LAN IP like ``http://192.168.50.245:5000`` is NOT. The voice input on
the ATLAS chat just dies with ``not-allowed`` from any device that
isn't the observatory PC itself.

The fix is to serve over HTTPS. ATLAS is single-operator software on a
LAN — a public CA is overkill. We mint our own self-signed cert,
auto-discover the LAN IPs of the observatory PC, list them all in the
certificate's Subject Alternative Name (SAN), and hand the cert + key
to uvicorn. The browser warns once per device (``Advanced → Proceed``)
and from then on the warm-room laptop / phone reach the dashboard at
``https://<lan-ip>:5000`` with a working microphone.

The cert is regenerated automatically if it expires, if the IPs the
observatory PC reports change, or if the operator clicks "Regenerate"
in the Setup tab.
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from atlas.config import get_settings
from atlas.logging_setup import get_logger

log = get_logger("tls")

# Certificate validity. 825 days is the Apple-imposed maximum for
# browser-trusted certs; we don't need that because the browser will
# never trust us anyway, but keeping it sane means the user re-accepts
# the cert about once every two years.
_CERT_LIFETIME_DAYS = 825
# How close to expiry before we silently regenerate.
_RENEW_WITHIN_DAYS = 30


@dataclass
class CertInfo:
    """Summary of the TLS material on disk. Returned by ``load_cert_info``
    and surfaced in the Setup tab so the operator can verify which
    certificate they accepted on the warm-room device."""
    cert_path: Path
    key_path: Path
    fingerprint_sha256: str       # colon-separated uppercase hex
    subject_alt_names: list[str]  # ["localhost", "127.0.0.1", "192.168.50.245", ...]
    not_before: str               # ISO timestamp
    not_after: str                # ISO timestamp
    days_until_expiry: int
    self_signed: bool = True

    def to_jsonable(self) -> dict:
        return {
            "cert_path": str(self.cert_path),
            "key_path": str(self.key_path),
            "fingerprint_sha256": self.fingerprint_sha256,
            "subject_alt_names": list(self.subject_alt_names),
            "not_before": self.not_before,
            "not_after": self.not_after,
            "days_until_expiry": self.days_until_expiry,
            "self_signed": self.self_signed,
        }


def tls_dir() -> Path:
    """Directory holding the cert + key. Created on first call."""
    p = get_settings().data_dir / "tls"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cert_path() -> Path:
    return tls_dir() / "atlas.crt"


def key_path() -> Path:
    return tls_dir() / "atlas.key"


# ---- LAN IP discovery -------------------------------------------------------

def discover_local_ips() -> list[str]:
    """Best-effort enumeration of every IPv4 address the host advertises.

    We list every IP we can find so the operator can reach the dashboard
    from any subnet the observatory PC is multi-homed onto (wired LAN +
    WiFi, say). Includes localhost loopback for completeness."""
    ips: set[str] = {"127.0.0.1"}
    try:
        hostname = socket.gethostname()
        # getaddrinfo handles multi-NIC machines better than gethostbyname.
        for info in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            ips.add(info[4][0])
    except Exception as e:
        log.warning("Local IP discovery via getaddrinfo failed: %s", e)
    # Fallback: open a UDP socket and ask the OS which IP would route to
    # the internet. Doesn't actually send anything.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception as e:
        log.debug("Routing-IP discovery failed: %s", e)
    # Drop the link-local fallback if we got a real one.
    if len(ips) > 1:
        ips.discard("0.0.0.0")
    return sorted(ips)


def _san_list() -> list[x509.GeneralName]:
    """Build the SubjectAltName entries: DNS:localhost + every detected
    IPv4. Browsers consult this list, not the CN, when matching the URL
    bar to the certificate."""
    sans: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
    ]
    for ip in discover_local_ips():
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            log.debug("Skipping malformed IP %r in SAN list", ip)
    return sans


# ---- Generation -------------------------------------------------------------

def _build_cert(key: rsa.RSAPrivateKey) -> x509.Certificate:
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ATLAS Observatory (self-signed)"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ATLAS"),
    ])
    sans = _san_list()
    return (
        x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=_CERT_LIFETIME_DAYS))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_encipherment=True,
                    content_commitment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False,
                    crl_sign=False, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(private_key=key, algorithm=hashes.SHA256())
    )


def generate_cert(force: bool = False) -> CertInfo:
    """Generate or refresh the self-signed cert.

    The cert is rewritten if any of:
      * the files don't exist
      * ``force=True`` is passed (operator-initiated regenerate)
      * the existing cert expires within _RENEW_WITHIN_DAYS
      * the set of host IPs has changed (e.g. WiFi reconnected on a
        different subnet) — otherwise the warm-room device's URL stops
        matching what's in SAN

    Returns the CertInfo summary describing what's now on disk."""
    cp, kp = cert_path(), key_path()
    if not force and cp.exists() and kp.exists():
        try:
            existing = _load_existing(cp, kp)
            expiring_soon = existing.days_until_expiry < _RENEW_WITHIN_DAYS
            current_ips = set(discover_local_ips())
            cert_ips = {s for s in existing.subject_alt_names
                          if _looks_like_ip(s)}
            ips_drifted = bool(current_ips - cert_ips)  # new IPs not yet covered
            if not (expiring_soon or ips_drifted):
                log.debug("TLS cert still valid (%d days, IPs match) — reusing",
                            existing.days_until_expiry)
                return existing
            log.info("Regenerating TLS cert (expiring_soon=%s, ips_drifted=%s)",
                       expiring_soon, ips_drifted)
        except Exception as e:
            log.warning("Existing TLS cert unreadable (%s) — regenerating", e)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = _build_cert(key)

    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    # Restrict the key file's permissions where the OS supports it.
    # On Windows, NTFS ACLs would be the right answer but chmod is at
    # least a no-op rather than an error.
    try:
        kp.chmod(0o600)
    except Exception:
        pass
    log.info("Wrote new self-signed cert to %s (SAN: %s)",
              cp, [_san_str(s) for s in cert.extensions.get_extension_for_class(
                       x509.SubjectAlternativeName).value])
    return _load_existing(cp, kp)


def _looks_like_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _san_str(name: x509.GeneralName) -> str:
    if isinstance(name, x509.DNSName):
        return name.value
    if isinstance(name, x509.IPAddress):
        return str(name.value)
    return str(name)


def _load_existing(cp: Path, kp: Path) -> CertInfo:
    pem = cp.read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    fp = cert.fingerprint(hashes.SHA256()).hex().upper()
    fp_pretty = ":".join(fp[i:i+2] for i in range(0, len(fp), 2))
    try:
        san_ext = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        sans = [_san_str(g) for g in san_ext]
    except x509.ExtensionNotFound:
        sans = []
    nb = cert.not_valid_before_utc
    na = cert.not_valid_after_utc
    days_left = (na - datetime.datetime.now(datetime.timezone.utc)).days
    return CertInfo(
        cert_path=cp, key_path=kp,
        fingerprint_sha256=fp_pretty,
        subject_alt_names=sans,
        not_before=nb.isoformat(),
        not_after=na.isoformat(),
        days_until_expiry=days_left,
    )


def load_cert_info() -> CertInfo | None:
    """Read the cert from disk and summarise it. Returns None if the
    cert isn't present — callers use this to decide whether HTTPS is
    available without forcing a generation pass."""
    cp, kp = cert_path(), key_path()
    if not (cp.exists() and kp.exists()):
        return None
    try:
        return _load_existing(cp, kp)
    except Exception as e:
        log.warning("Failed to read TLS cert: %s", e)
        return None


def ensure_cert() -> CertInfo:
    """Guarantee a usable cert is on disk and return its info. This is
    the function ``atlas serve --https`` calls during boot."""
    return generate_cert(force=False)
