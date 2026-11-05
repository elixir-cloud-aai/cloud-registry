"""Tests for fetching URLs that users supply.

The tests talk to real servers on the loopback interface, over plain HTTP and
TLS, with the addresses that names resolve to replaced.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
import socket
import ssl
import threading
import time
from typing import Any, ClassVar

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from cloud_registry import safe_fetch
from cloud_registry.safe_fetch import (
    AddressNotAllowed,
    Unreachable,
    is_allowed_address,
    safe_get,
)

LOOPBACK = [ipaddress.ip_network("127.0.0.0/8")]
HOST = "service.example.org"
BODY = b'{"id": "org.example.service"}'


class Handler(BaseHTTPRequestHandler):
    """Handler of the test servers, recording requests."""

    status: ClassVar[int] = 200
    extra_headers: ClassVar[dict[str, str]] = {}
    body: ClassVar[bytes] = BODY
    requests: ClassVar[list[dict[str, str]]] = []
    paths: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        """Answer with the configured response, recording the request."""
        Handler.requests.append(dict(self.headers.items()))
        Handler.paths.append(self.path)
        self.send_response(Handler.status)
        for name, value in Handler.extra_headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(Handler.body)))
        self.end_headers()
        self.wfile.write(Handler.body)

    def log_message(self, format: str, *args: Any) -> None:
        """Stay silent.

        Args:
            format: Format of the message.
            *args: Values of the message.
        """


@pytest.fixture(autouse=True)
def _reset_handler() -> None:
    """Reset what the handler answers and recorded."""
    Handler.status = 200
    Handler.extra_headers = {}
    Handler.body = BODY
    Handler.requests = []
    Handler.paths = []


def _serve(server: ThreadingHTTPServer) -> Iterator[int]:
    """Run a server while a fixture is active.

    Args:
        server: Server.

    Yields:
        Port of the server.
    """
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def http_port() -> Iterator[int]:
    """Port of a plain HTTP server."""
    yield from _serve(ThreadingHTTPServer(("127.0.0.1", 0), Handler))


def _certificate(tmp_path: Path, name: str) -> Path:
    """Create a self-signed certificate.

    Args:
        tmp_path: Directory for the file.
        name: Host name of the certificate.

    Returns:
        Path of a file with the certificate and its key.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / f"{name}.pem"
    path.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


@pytest.fixture
def certificate(tmp_path: Path) -> Path:
    """File with the certificate and key of `HOST`."""
    return _certificate(tmp_path, HOST)


@pytest.fixture
def tls_port(certificate: Path) -> Iterator[int]:
    """Port of a TLS server with the certificate of `HOST`."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    yield from _serve(server)


@pytest.fixture
def client_context(certificate: Path) -> ssl.SSLContext:
    """TLS settings of a client that trusts the certificate of `HOST`."""
    context = ssl.create_default_context(cafile=str(certificate))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def test_default_tls_settings() -> None:
    """Fetches need TLS 1.2 or newer, and check certificates and host names."""
    context = safe_fetch._default_tls_context()
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname


def resolving(
    monkeypatch: pytest.MonkeyPatch, *addresses: str
) -> list[tuple[Any, ...]]:
    """Make all names resolve to given addresses.

    Args:
        monkeypatch: Fixture for patching.
        *addresses: Addresses.

    Returns:
        List to which each lookup adds its arguments.
    """
    calls: list[tuple[Any, ...]] = []
    lookup = socket.getaddrinfo

    def getaddrinfo(
        host: str, port: int, *args: Any, **kwargs: Any
    ) -> list[tuple[Any, ...]]:
        """Replace `socket.getaddrinfo()`; IP addresses resolve to themselves.

        Args:
            host: Host.
            port: Port.
            *args: Other arguments.
            **kwargs: Other arguments.

        Returns:
            The addresses.
        """
        if host in addresses:
            return lookup(host, port, *args, **kwargs)
        calls.append((host, port))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return calls


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # shared address space
        "0.0.0.0",  # noqa: S104 (not bound to)
        "192.0.2.1",  # documentation
        "198.18.0.1",  # benchmarking
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",  # link-local
        "fe80::1%eth0",
        "fc00::1",  # unique local
        "2001:db8::1",  # documentation
        "ff02::1",  # multicast
        "ff0e::1",  # global multicast
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "::127.0.0.1",  # IPv4-compatible
        "::8.8.8.8",
        "2002:7f00:1::",  # 6to4 of 127.0.0.1
        "2002:a00:1::",  # 6to4 of 10.0.0.1
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "64:ff9b:1::1",  # local NAT64
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "fec0::1",  # site-local
        "::ffff:0:a00:1",  # IPv4-translated
        "1::1",
        "5f00::1",
    ],
)
def test_address_not_allowed(address: str) -> None:
    """Addresses that aren't public are refused."""
    assert not is_allowed_address(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "2606:4700:4700::1111",
        "::ffff:8.8.8.8",
        "2002:808:808::",
        "64:ff9b::808:808",
    ],
)
def test_address_allowed(address: str) -> None:
    """Public addresses are allowed, also embedded in IPv6 ones."""
    assert is_allowed_address(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("10.1.2.3", True),
        ("::ffff:10.1.2.3", True),
        ("64:ff9b::a01:203", True),
        ("10.2.0.1", False),
        ("169.254.169.254", False),
        ("fd00::1", True),
        ("fd01::1", False),
    ],
)
def test_permitted_networks(address: str, allowed: bool) -> None:
    """Networks can be permitted, without opening up others."""
    networks = [ipaddress.ip_network("10.1.0.0/16"), ipaddress.ip_network("fd00::/64")]
    assert is_allowed_address(ipaddress.ip_address(address), networks) is allowed


def test_get_plain(http_port: int) -> None:
    """A plain HTTP URL is fetched if its scheme and address are allowed."""
    url = f"http://127.0.0.1:{http_port}/api/service-info?a=b"
    assert safe_get(url, schemes={"http"}, networks=LOOPBACK) == BODY
    assert Handler.paths == ["/api/service-info?a=b"]


def test_no_credentials_sent(http_port: int) -> None:
    """Requests carry no credentials, and ask for uncompressed JSON."""
    safe_get(f"http://127.0.0.1:{http_port}/", schemes={"http"}, networks=LOOPBACK)
    (sent,) = Handler.requests
    assert not {"authorization", "cookie"} & {name.lower() for name in sent}
    assert sent["Accept"] == "application/json"
    assert sent["Accept-Encoding"] == "identity"


def test_https_only_by_default(http_port: int) -> None:
    """Only `https` is allowed by default."""
    with pytest.raises(AddressNotAllowed):
        safe_get(f"http://127.0.0.1:{http_port}/", networks=LOOPBACK)
    assert not Handler.requests


@pytest.mark.parametrize(
    "url",
    ["ftp://example.org/", "file:///etc/passwd", "gopher://example.org/"],
)
def test_other_schemes(url: str) -> None:
    """Other schemes are refused, also if the caller lists them."""
    with pytest.raises(AddressNotAllowed):
        safe_get(url, schemes={"http", "https", "ftp", "file", "gopher"})


@pytest.mark.parametrize("credentials", ["user:secret@", ":secret@", "@"])
def test_credentials_in_url(http_port: int, credentials: str) -> None:
    """URLs with credentials are refused."""
    with pytest.raises(AddressNotAllowed):
        safe_get(
            f"http://{credentials}127.0.0.1:{http_port}/",
            schemes={"http"},
            networks=LOOPBACK,
        )
    assert not Handler.requests


@pytest.mark.parametrize(
    "url", ["https://", "https://[::1/", "https://example.org:port/"]
)
def test_malformed_url(url: str) -> None:
    """Malformed URLs are unreachable."""
    with pytest.raises(Unreachable):
        safe_get(url)


def test_private_address_refused(http_port: int) -> None:
    """Addresses that aren't public are refused without connecting."""
    with pytest.raises(AddressNotAllowed):
        safe_get(f"http://127.0.0.1:{http_port}/", schemes={"http"})
    assert not Handler.requests


@pytest.mark.parametrize("spelling", ["127.1", "2130706433", "0x7f.0.0.1", "localhost"])
def test_spellings_of_loopback(http_port: int, spelling: str) -> None:
    """Other spellings of loopback are checked after they are resolved."""
    with pytest.raises(AddressNotAllowed):
        safe_get(f"http://{spelling}:{http_port}/", schemes={"http"})
    assert not Handler.requests


def test_name_of_private_address(
    monkeypatch: pytest.MonkeyPatch, http_port: int
) -> None:
    """A name that resolves to a private address is refused."""
    resolving(monkeypatch, "127.0.0.1")
    with pytest.raises(AddressNotAllowed):
        safe_get(f"http://{HOST}:{http_port}/", schemes={"http"})
    assert not Handler.requests


def test_any_address_not_allowed(
    monkeypatch: pytest.MonkeyPatch, http_port: int
) -> None:
    """A name with a public and a private address is refused."""
    resolving(monkeypatch, "8.8.8.8", "127.0.0.1")
    with pytest.raises(AddressNotAllowed):
        safe_get(f"http://{HOST}:{http_port}/", schemes={"http"})
    assert not Handler.requests


def test_pinned(monkeypatch: pytest.MonkeyPatch, http_port: int) -> None:
    """The name is resolved once, and the checked address connected to."""
    calls = resolving(monkeypatch, "127.0.0.1")
    url = f"http://{HOST}:{http_port}/"
    assert safe_get(url, schemes={"http"}, networks=LOOPBACK) == BODY
    assert calls == [(HOST, http_port)]
    assert Handler.requests[0]["Host"] == f"{HOST}:{http_port}"


def test_next_address(monkeypatch: pytest.MonkeyPatch, http_port: int) -> None:
    """The next address is tried if a connection fails."""
    resolving(monkeypatch, "127.0.0.2", "127.0.0.1")
    connect = socket.socket.connect
    tried: list[str] = []

    def failing(sock: socket.socket, address: Any) -> None:
        """Refuse connections to the first address.

        Args:
            sock: Socket.
            address: Address.

        Raises:
            ConnectionRefusedError: The connection is to the first address.
        """
        tried.append(address[0])
        if address[0] == "127.0.0.2":
            raise ConnectionRefusedError
        connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", failing)
    url = f"http://{HOST}:{http_port}/"
    assert safe_get(url, schemes={"http"}, networks=LOOPBACK) == BODY
    assert tried == ["127.0.0.2", "127.0.0.1"]


def test_unknown_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names that can't be resolved are unreachable."""

    def failing(*args: Any, **kwargs: Any) -> None:
        """Fail like a failed lookup.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Raises:
            socket.gaierror: Always.
        """
        raise socket.gaierror

    monkeypatch.setattr(socket, "getaddrinfo", failing)
    with pytest.raises(Unreachable):
        safe_get(f"https://{HOST}/")


def test_slow_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lookups that take too long are given up."""
    release = threading.Event()

    def slow(*args: Any, **kwargs: Any) -> list[Any]:
        """Wait until the test ends.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            No addresses.
        """
        release.wait(10)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    start = time.monotonic()
    with pytest.raises(Unreachable):
        safe_get(f"https://{HOST}/", timeout=0.1)
    release.set()
    assert time.monotonic() - start < 5


def test_connection_refused(http_port: int) -> None:
    """Failed connections are unreachable."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(Unreachable):
        safe_get(f"http://127.0.0.1:{port}/", schemes={"http"}, networks=LOOPBACK)


@pytest.mark.parametrize("status", [204, 301, 302, 307, 308, 401, 403, 404, 500])
def test_other_status(http_port: int, status: int) -> None:
    """Only `200` passes, so redirects aren't followed."""
    Handler.status = status
    Handler.extra_headers = {"Location": f"http://127.0.0.1:{http_port}/other"}
    with pytest.raises(Unreachable):
        safe_get(f"http://127.0.0.1:{http_port}/", schemes={"http"}, networks=LOOPBACK)
    assert Handler.paths == ["/"]


def test_size_limit(http_port: int) -> None:
    """Responses over the limit are refused; those at the limit pass."""
    url = f"http://127.0.0.1:{http_port}/"
    assert (
        safe_get(url, schemes={"http"}, networks=LOOPBACK, max_bytes=len(BODY)) == BODY
    )
    with pytest.raises(Unreachable):
        safe_get(url, schemes={"http"}, networks=LOOPBACK, max_bytes=len(BODY) - 1)


def _dripping_server(respond: Callable[[socket.socket], None]) -> Iterator[int]:
    """Run a server that handles each connection with a function.

    Args:
        respond: Function handling a connection.

    Yields:
        Port of the server.
    """
    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve() -> None:
        """Accept connections until stopped."""
        server.settimeout(0.05)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            threading.Thread(target=respond, args=(connection,), daemon=True).start()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    stop.set()
    thread.join()
    server.close()


def _drip(connection: socket.socket, stop_after: float, prefix: bytes = b"") -> None:
    """Send a byte every now and then, each in time for a socket timeout.

    Args:
        connection: Connection.
        stop_after: Time after which to stop, in seconds.
        prefix: Bytes to send first.
    """
    end = time.monotonic() + stop_after
    try:
        connection.recv(4096)
        connection.sendall(prefix)
        while time.monotonic() < end:
            connection.sendall(b"a")
            time.sleep(0.05)
    except OSError:
        pass
    finally:
        connection.close()


@pytest.mark.parametrize(
    "prefix",
    [
        b"",  # the status line
        b"HTTP/1.1 200 OK\r\nX-Drip: ",  # the headers
        b"HTTP/1.1 200 OK\r\n\r\n",  # the body, without a length
        b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n",
    ],
)
def test_total_time_limit(prefix: bytes) -> None:
    """Servers that send slowly can't extend the total time."""
    for port in _dripping_server(lambda connection: _drip(connection, 8, prefix)):
        start = time.monotonic()
        with pytest.raises(Unreachable):
            safe_get(
                f"http://127.0.0.1:{port}/",
                schemes={"http"},
                networks=LOOPBACK,
                timeout=0.3,
            )
        assert time.monotonic() - start < 3


def test_tls_handshake_time_limit(client_context: ssl.SSLContext) -> None:
    """Servers that never finish the TLS handshake can't extend the time."""
    for port in _dripping_server(lambda connection: _drip(connection, 8)):
        resolving_start = time.monotonic()
        with pytest.raises(Unreachable):
            safe_get(
                f"https://127.0.0.1:{port}/",
                networks=LOOPBACK,
                timeout=0.3,
                context=client_context,
            )
        assert time.monotonic() - resolving_start < 3


def test_silent_after_body_cut(http_port: int) -> None:
    """Responses cut off before their length are unreachable."""

    def respond(connection: socket.socket) -> None:
        """Announce more than is sent.

        Args:
            connection: Connection.
        """
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")
        connection.close()

    for port in _dripping_server(respond):
        with pytest.raises(Unreachable):
            safe_get(f"http://127.0.0.1:{port}/", schemes={"http"}, networks=LOOPBACK)


def test_not_http() -> None:
    """Servers that don't speak HTTP are unreachable."""

    def respond(connection: socket.socket) -> None:
        """Send something else than HTTP.

        Args:
            connection: Connection.
        """
        connection.recv(4096)
        connection.sendall(b"SSH-2.0-OpenSSH\r\n")
        connection.close()

    for port in _dripping_server(respond):
        with pytest.raises(Unreachable):
            safe_get(f"http://127.0.0.1:{port}/", schemes={"http"}, networks=LOOPBACK)


def test_tls_with_name(
    monkeypatch: pytest.MonkeyPatch, tls_port: int, client_context: ssl.SSLContext
) -> None:
    """TLS connections go to the checked address, with the host's name."""
    calls = resolving(monkeypatch, "127.0.0.1")
    body = safe_get(
        f"https://{HOST}:{tls_port}/service-info",
        networks=LOOPBACK,
        context=client_context,
    )
    assert body == BODY
    assert calls == [(HOST, tls_port)]
    assert Handler.requests[0]["Host"] == f"{HOST}:{tls_port}"


def test_tls_wrong_name(
    monkeypatch: pytest.MonkeyPatch, tls_port: int, client_context: ssl.SSLContext
) -> None:
    """Certificates are checked for the host's name, not for the address."""
    resolving(monkeypatch, "127.0.0.1")
    with pytest.raises(Unreachable):
        safe_get(
            f"https://other.example.org:{tls_port}/",
            networks=LOOPBACK,
            context=client_context,
        )
    assert not Handler.requests


def test_tls_untrusted(monkeypatch: pytest.MonkeyPatch, tls_port: int) -> None:
    """Certificates must be trusted, by default by the system's certificates."""
    resolving(monkeypatch, "127.0.0.1")
    with pytest.raises(Unreachable):
        safe_get(f"https://{HOST}:{tls_port}/", networks=LOOPBACK)
    assert not Handler.requests


def test_host_header_ipv6() -> None:
    """The `Host` header has IPv6 addresses in brackets."""
    connection = safe_fetch._PinnedConnection("2001:db8::1", 8443, "::1", 1.0, None)
    connection.putrequest("GET", "/")
    assert b"Host: [2001:db8::1]:8443" in connection._buffer  # type: ignore[attr-defined]


def test_port_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Port 0 is not taken for the scheme's default."""
    resolving(monkeypatch, "127.0.0.1")
    with pytest.raises(Unreachable):
        safe_get(f"https://{HOST}:0/", networks=LOOPBACK)


def test_lookups_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hanging lookups don't pile up, and free their places when they end."""
    release = threading.Event()
    started: list[str] = []

    def hanging(host: str, *args: object, **kwargs: object) -> list[tuple[object, ...]]:
        """Hang until the test is done.

        Args:
            host: Host name.
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            One address.
        """
        started.append(host)
        release.wait(10)
        return [(0, 0, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", hanging)
    try:
        for index in range(safe_fetch.MAX_LOOKUPS):
            with pytest.raises(Unreachable):
                safe_get(f"https://{HOST}{index}/", networks=LOOPBACK, timeout=0.01)
        assert len(started) == safe_fetch.MAX_LOOKUPS
        with pytest.raises(Unreachable):
            safe_get(f"https://{HOST}/", networks=LOOPBACK, timeout=0.01)
        assert len(started) == safe_fetch.MAX_LOOKUPS
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if safe_fetch._LOOKUPS._value == safe_fetch.MAX_LOOKUPS:
            break
        time.sleep(0.01)
    assert safe_fetch._LOOKUPS._value == safe_fetch.MAX_LOOKUPS


@pytest.mark.parametrize(
    ("tls", "port", "host"),
    [
        (True, 443, HOST),
        (True, 80, f"{HOST}:80"),
        (True, 8443, f"{HOST}:8443"),
        (False, 80, HOST),
        (False, 443, f"{HOST}:443"),
    ],
)
def test_host_header_port(tls: bool, port: int, host: str) -> None:
    """The `Host` header has the port unless it is the scheme's default."""
    context = safe_fetch._default_tls_context() if tls else None
    connection = safe_fetch._PinnedConnection(HOST, port, "127.0.0.1", 1.0, context)
    sent: list[bytes] = []
    connection.send = sent.append  # type: ignore[method-assign,assignment]
    connection.request("GET", "/")
    assert f"Host: {host}\r\n".encode() in b"".join(sent)


def test_abort_without_socket() -> None:
    """Aborting a connection that isn't connected does nothing."""
    connection = safe_fetch._PinnedConnection("host", 1, "127.0.0.1", 1.0, None)
    connection.abort()


def test_abort_closed_socket() -> None:
    """Aborting a connection whose socket failed does nothing."""
    connection = safe_fetch._PinnedConnection("host", 1, "127.0.0.1", 1.0, None)
    with socket.socket() as sock:
        connection._socket = sock
        connection.abort()


def test_deadline_over() -> None:
    """A deadline with no time left raises."""
    with pytest.raises(Unreachable):
        safe_fetch._Deadline(0).left()
