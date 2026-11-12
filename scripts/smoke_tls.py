"""Smoke test of verified TLS fetches, run in the app's image.

Imports and syncs fetch service info over TLS with the settings of
`cloud_registry.safe_fetch`, which check certificates against the CA
certificates of the system. Without them, every fetch would fail, with uniform
`502` responses and `unreachable` reasons that don't tell why. The script
checks, with the Python, OpenSSL and CA certificates of wherever it runs, that

- the TLS settings of fetches have CA certificates: loaded from a file, or
  in the directory where OpenSSL looks them up when it needs them (file and
  directory as OpenSSL is configured, or `SSL_CERT_FILE` and `SSL_CERT_DIR`);
- a fetch with these settings, trusting a test CA too, gets the response of a
  local TLS server (on a free port of the loopback interface) whose
  certificate the test CA issued, over a verified connection;
- TLS handshakes with that server fail on its certificate with these
  settings alone, which don't trust it, and for another host name with the
  test CA, so that certificates are checked, also for the host.

CI's smoke test runs it in the app's container (`.github/workflows/ci.yml`,
job `smoke`), from standard input, as the image doesn't contain `scripts/`:

    docker compose exec -T cloud-registry python - < scripts/smoke_tls.py

The test server and certificates are made here, not shared with the tests
(cf. `tests/test_safe_fetch.py`), as the script must run on its own. It needs
nothing but the app and its dependencies (the test certificates are
made with `cryptography`, a dependency of FOCA), and no network. It exits with
status 1 if a check fails.
"""

from collections.abc import Iterator
import contextlib
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from cloud_registry.safe_fetch import (
    Unreachable,
    _default_tls_context,  # the settings that fetches use
    safe_get,
)

HOST = "localhost"
# Host name that the certificate of the test server isn't for
OTHER_HOST = "other.example.org"
# Fetches may only reach the loopback interface, where the test server is
NETWORKS = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
BODY = b'{"smoke": "tls"}'
TIMEOUT = 10.0
# Names of CA certificates in OpenSSL's lookup directory: hash of the subject
# and a sequence number (`openssl rehash`); CRLs have an `r` before the number
_HASHED_NAME = re.compile(r"[0-9a-f]{8}\.[0-9]+")
# OpenSSL's verification error of a certificate whose issuer isn't trusted
# (`X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY`)
_UNTRUSTED_ISSUER = 20
# ... and of a certificate of another host (`X509_V_ERR_HOSTNAME_MISMATCH`)
_HOSTNAME_MISMATCH = 62


class SmokeTestError(Exception):
    """A check failed."""


class _Handler(BaseHTTPRequestHandler):
    """Answers every `GET` request with `BODY`."""

    def do_GET(self) -> None:
        """Send `BODY`."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, format: str, *args: object) -> None:
        """Log nothing (cf. `BaseHTTPRequestHandler.log_message()`)."""


class _Server(ThreadingHTTPServer):
    """TLS server on a free port of the loopback interface.

    Each connection makes its TLS handshake in its own thread, with a
    timeout, so that a client that doesn't complete it can't block the
    server or its shutdown.
    """

    def __init__(self, context: ssl.SSLContext) -> None:
        """Create the server, listening.

        Args:
            context: TLS settings of the server.
        """
        super().__init__(("127.0.0.1", 0), _Handler)
        self.context = context

    def get_request(self) -> tuple[ssl.SSLSocket, object]:
        """Accept a connection, without its TLS handshake yet.

        Returns:
            The TLS socket of the connection and the client's address.
        """
        sock, address = self.socket.accept()
        sock.settimeout(TIMEOUT)
        tls = self.context.wrap_socket(
            sock, server_side=True, do_handshake_on_connect=False
        )
        return tls, address

    def handle_error(self, request: object, client_address: object) -> None:
        """Ignore failed connections, e.g., handshakes that the client refused.

        Args:
            request: Socket of the connection.
            client_address: Address of the client.
        """


def _certificates() -> tuple[bytes, bytes]:
    """Make a test CA and a certificate of `HOST` that it issued.

    Returns:
        The CA certificate, and the certificate of `HOST` with its key, all
        PEM-encoded.
    """
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Smoke test CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, HOST)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    chain = certificate.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return ca.public_bytes(serialization.Encoding.PEM), chain


@contextlib.contextmanager
def _server(chain: bytes) -> Iterator[int]:
    """Serve `BODY` over TLS on a free port of the loopback interface.

    Args:
        chain: Certificate of the server with its key, PEM-encoded.

    Yields:
        Port of the server.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "server.pem"
        path.write_bytes(chain)
        context.load_cert_chain(path)
    server = _Server(context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _ca_certificates(context: ssl.SSLContext) -> int:
    """Count the CA certificates of TLS settings with the system's defaults.

    Args:
        context: TLS settings that loaded the default CA certificates.

    Returns:
        Number of CA certificates loaded, plus that of CA certificates that
        load from the directory where OpenSSL looks them up when it needs them.
    """
    count = context.cert_store_stats()["x509_ca"]
    capath = ssl.get_default_verify_paths().capath
    if capath:
        found = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        for path in Path(capath).iterdir():
            if _HASHED_NAME.fullmatch(path.name):
                # not certificates (e.g., empty, or links to missing files)
                with contextlib.suppress(OSError):
                    found.load_verify_locations(cafile=path)
        count += found.cert_store_stats()["x509_ca"]
    return count


def _refused(
    port: int, context: ssl.SSLContext, host: str, code: int, what: str
) -> None:
    """Check that a TLS handshake with the test server fails on its certificate.

    The handshake is made directly, as fetches don't tell why they fail;
    errors other than the certificate's (e.g., of the connection) end the
    script with their traceback.

    Args:
        port: Port of the test server.
        context: TLS settings of the client.
        host: Host name that the certificate is checked for.
        code: OpenSSL's verification error that the handshake must fail with.
        what: Description of the certificate, for messages.

    Raises:
        SmokeTestError: The handshake succeeded, or failed with another error.
    """
    with socket.create_connection(("127.0.0.1", port), TIMEOUT) as sock:
        try:
            context.wrap_socket(sock, server_hostname=host).close()
        except ssl.SSLCertVerificationError as exc:
            if exc.verify_code == code:
                return
            raise SmokeTestError(
                f"TLS handshake with {what} failed for another reason: "
                f"{exc.verify_message}"
            ) from exc
    raise SmokeTestError(f"{what} accepted")


def check() -> None:
    """Run the checks (cf. the module docstring).

    Raises:
        SmokeTestError: A check failed.
    """
    if not _ca_certificates(_default_tls_context()):
        raise SmokeTestError(
            "no CA certificates in the TLS settings of fetches: install the "
            "system's CA certificates in the image"
        )
    ca, chain = _certificates()
    with _server(chain) as port:
        url = f"https://{HOST}:{port}/service-info"
        trusting = _default_tls_context()
        trusting.load_verify_locations(cadata=ca.decode())
        try:
            body = safe_get(url, networks=NETWORKS, timeout=TIMEOUT, context=trusting)
        except Unreachable as exc:
            raise SmokeTestError("verified TLS fetch failed") from exc
        if body != BODY:
            raise SmokeTestError(f"unexpected response of verified TLS fetch: {body!r}")
        _refused(
            port,
            _default_tls_context(),
            HOST,
            _UNTRUSTED_ISSUER,
            "untrusted certificate",
        )
        _refused(
            port, trusting, OTHER_HOST, _HOSTNAME_MISMATCH, "certificate of other host"
        )


def main() -> int:
    """Run the checks and report the result.

    Returns:
        Exit status: 0 if all checks passed, 1 otherwise.
    """
    try:
        check()
    except SmokeTestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        "CA certificates found; verified TLS fetch passed; untrusted certificate "
        "and certificate of other host refused"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
