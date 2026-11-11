"""Fetching URLs that users supply, safe from server-side request forgery.

The app fetches addresses that users choose, which could otherwise point it at
internal services (e.g., cloud metadata endpoints). `safe_get()` therefore

- only allows the schemes given by the caller;
- resolves the host once, and refuses it unless all of its addresses are
  allowed (cf. `is_allowed_address()`);
- connects to a checked address itself rather than resolving the host again,
  so that a name can't point elsewhere after the check (DNS rebinding), and
  verifies the certificate for the host name, not for the address;
- tries the checked addresses in turn, IPv6 and IPv4 alternating, each with
  its share of the time left for connecting, so that an address that doesn't
  answer leaves time for the others;
- doesn't follow redirects, which could lead anywhere: only `200 OK` passes;
- limits the size of the response and the total time, also for servers that
  send very slowly;
- sends no credentials, cookies or settings of the environment (e.g., proxies).

Failures are `AddressNotAllowed` and `Unreachable`, without details of the
network, which callers shouldn't pass on to users anyway: anything they can
tell apart lets them map networks. The module knows nothing of the app, so that
it can move to a library.
"""

from collections.abc import Collection, Iterable, Mapping
import contextlib
import http.client
import ipaddress
import itertools
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Default limits of the response size, in bytes, and the total time, in seconds
MAX_BYTES = 256 * 1024
# Lookups at a time per process: they can't be interrupted, and outlive their
# fetches if the resolver hangs, so that they must not pile up
MAX_LOOKUPS = 8
_LOOKUPS = threading.BoundedSemaphore(MAX_LOOKUPS)
TIMEOUT = 10.0
# Ports of schemes, for URLs without a port
_DEFAULT_PORTS = {"http": 80, "https": 443}
# NAT64 prefix with an IPv4 address in its last 32 bits (RFC 6052), which
# reaches the IPv4 address
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# All public IPv6 addresses are global unicast addresses; `is_global` also
# accepts others, e.g., deprecated site-local and IPv4-translated ones
_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")
_LAST_32_BITS = 0xFFFFFFFF


class FetchError(Exception):
    """An address could not be fetched."""


class AddressNotAllowed(FetchError):
    """The URL or its host resolves to an address that is not allowed."""


class Unreachable(FetchError):
    """The server can't be reached, or its response is unusable."""


def is_allowed_address(address: IPAddress, networks: Iterable[IPNetwork] = ()) -> bool:
    """Check whether the app may connect to an IP address.

    Args:
        address: IP address.
        networks: Networks to allow besides public addresses.

    Returns:
        Whether the address is in `networks` or public. IPv4 addresses embedded
        in IPv6 addresses (IPv4-mapped, 6to4, NAT64) are checked themselves, as
        they would be reached; Teredo addresses and other IPv6 addresses outside
        of global unicast are refused.
    """
    networks = list(networks)
    if any(address in network for network in networks):
        return True
    if isinstance(address, ipaddress.IPv6Address):
        embedded: ipaddress.IPv4Address | None = (
            address.ipv4_mapped or address.sixtofour
        )
        if embedded is None and address in _NAT64:
            embedded = ipaddress.IPv4Address(int(address) & _LAST_32_BITS)
        if embedded is not None:
            return is_allowed_address(embedded, networks)
        if address.teredo is not None or address not in _GLOBAL_UNICAST:
            return False
    return address.is_global and not address.is_multicast


class _Deadline:
    """Time left for a fetch."""

    def __init__(self, seconds: float) -> None:
        """Start the time.

        Args:
            seconds: Total time.
        """
        self._end = time.monotonic() + seconds

    def left(self) -> float:
        """Get the time left.

        Returns:
            Seconds left.

        Raises:
            Unreachable: No time is left.
        """
        left = self._end - time.monotonic()
        if left <= 0:
            raise Unreachable
        return left


def _resolve(host: str, port: int, deadline: _Deadline) -> list[str]:
    """Resolve a host name, within the time left.

    `socket.getaddrinfo()` takes no timeout, so it runs in a thread of its own,
    which ends by itself when the resolver gives up.

    Args:
        host: Host name or IP address.
        port: Port.
        deadline: Time left.

    Returns:
        Unique IP addresses, in the order of the resolver.

    Raises:
        Unreachable: The name can't be resolved in time, or too many lookups
            are still running (cf. `MAX_LOOKUPS`).
    """
    addresses: list[str] = []
    if not _LOOKUPS.acquire(blocking=False):
        raise Unreachable

    def lookup() -> None:
        """Store the addresses of the host, if any."""
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):  # e.g., labels too long to encode
            return
        else:
            addresses.extend(dict.fromkeys(str(info[4][0]) for info in infos))
        finally:
            _LOOKUPS.release()

    thread = threading.Thread(target=lookup, daemon=True, name="safe-fetch-resolve")
    thread.start()
    thread.join(deadline.left())
    if thread.is_alive() or not addresses:
        raise Unreachable
    return addresses


def _interleave(addresses: Iterable[str]) -> list[str]:
    """Order addresses so that IPv6 and IPv4 take turns (cf. RFC 8305).

    Args:
        addresses: IP addresses, in the order of the resolver.

    Returns:
        The addresses, starting with the family of the first one, and otherwise
        in the order of the resolver.
    """
    families: dict[int, list[str]] = {}
    for address in addresses:
        families.setdefault(ipaddress.ip_address(address).version, []).append(address)
    rounds = itertools.zip_longest(*families.values())
    return [address for turn in rounds for address in turn if address is not None]


def _default_tls_context() -> ssl.SSLContext:
    """Get the TLS settings of fetches.

    Returns:
        Settings of the standard library that check certificates, with TLS 1.2
        or newer, whatever the defaults of the Python version and its OpenSSL.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class _PinnedConnection(http.client.HTTPConnection):
    """Connection to a given address, for a host (plain, unless with TLS)."""

    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        timeout: float,
        context: ssl.SSLContext | None,
        connect_timeout: float | None = None,
    ) -> None:
        """Create the connection, without connecting.

        Args:
            host: Host name, for the `Host` header and certificate checks.
            port: Port.
            address: IP address to connect to.
            timeout: Timeout of each socket operation, in seconds.
            context: TLS settings; plain HTTP if `None`.
            connect_timeout: Timeout of the TCP connection, in seconds; that of
                other socket operations if `None`.
        """
        super().__init__(host, port, timeout=timeout)
        # so that the `Host` header only has ports other than the scheme's
        self.default_port = _DEFAULT_PORTS["http" if context is None else "https"]
        self._address = address
        self._tls = context
        self._connect_timeout = timeout if connect_timeout is None else connect_timeout
        # kept for `abort()`: `http.client` drops `sock` while the response of
        # a connection that closes is read
        self._socket: socket.socket | None = None

    def connect(self) -> None:
        """Connect to the address; the certificate is checked for the host."""
        sock = socket.create_connection(
            (self._address, self.port), self._connect_timeout
        )
        # the TLS handshake and the request get the timeout of other operations
        sock.settimeout(self.timeout)
        self.sock = self._socket = sock
        if self._tls is None:
            return
        # no handshake yet, so that `abort()` reaches the socket during it
        tls_sock = self._tls.wrap_socket(
            sock, server_hostname=self.host, do_handshake_on_connect=False
        )
        self.sock = self._socket = tls_sock
        tls_sock.do_handshake()

    def abort(self) -> None:
        """Interrupt whatever the connection is waiting for, from any thread."""
        if self._socket is not None:
            # also of TLS sockets, without their state, which another thread
            # may be using
            with contextlib.suppress(OSError):
                socket.socket.shutdown(self._socket, socket.SHUT_RDWR)


def _request(
    connection: _PinnedConnection,
    target: str,
    headers: Mapping[str, str],
    max_bytes: int,
    deadline: _Deadline,
) -> bytes:
    """Send a `GET` request over a connection that is open.

    Args:
        connection: Connection.
        target: Path and query.
        headers: Request headers.
        max_bytes: Maximum size of the response body.
        deadline: Time left.

    Returns:
        Response body.

    Raises:
        Unreachable: The response isn't a `200 OK` within the limits.
    """
    connection.request("GET", target, headers=dict(headers))
    response = connection.getresponse()
    if response.status != http.client.OK:
        raise Unreachable
    body = response.read(max_bytes + 1)
    # `length` is what is missing of the announced length: `read()` with a size
    # returns what it got if the server closes the connection early
    if len(body) > max_bytes or response.length:
        raise Unreachable
    deadline.left()
    return body


def safe_get(
    url: str,
    *,
    schemes: Collection[str] = frozenset({"https"}),
    networks: Iterable[IPNetwork] = (),
    max_bytes: int = MAX_BYTES,
    timeout: float = TIMEOUT,
    headers: Mapping[str, str] | None = None,
    context: ssl.SSLContext | None = None,
) -> bytes:
    """Get a URL that a user supplied (cf. the module docstring).

    Args:
        url: HTTP(S) URL, already checked by the caller (e.g., with FOCA's URL
            helpers): international host names must be encoded.
        schemes: Allowed schemes, lowercase.
        networks: Networks to allow besides public addresses.
        max_bytes: Maximum size of the response body.
        timeout: Maximum total time, in seconds.
        headers: Further request headers; never pass credentials.
        context: TLS settings, by default the system's certificates.

    Returns:
        Response body.

    Raises:
        AddressNotAllowed: The scheme is not allowed, the URL has credentials,
            or an address of the host is not allowed.
        Unreachable: The name can't be resolved, no connection is possible, the
            response is no `200 OK` within the limits, or the URL is malformed.
    """
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise Unreachable from exc
    scheme = parts.scheme.lower()
    if scheme not in schemes or scheme not in _DEFAULT_PORTS or "@" in parts.netloc:
        raise AddressNotAllowed
    if not host:
        raise Unreachable
    if port == 0:  # nothing can listen there
        raise Unreachable
    port = port or _DEFAULT_PORTS[scheme]
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    deadline = _Deadline(timeout)
    addresses = _resolve(host, port, deadline)
    networks = list(networks)
    if not all(
        is_allowed_address(ipaddress.ip_address(item), networks) for item in addresses
    ):
        raise AddressNotAllowed
    tls = None
    if scheme == "https":
        tls = context or _default_tls_context()
    request_headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Connection": "close",
        **(headers or {}),
    }
    # Each address gets an equal share of the time left for connecting, so that
    # one that doesn't answer (e.g., IPv6 dropped on the path) can't use it all
    ordered = _interleave(addresses)
    for index, address in enumerate(ordered):
        left = deadline.left()
        share = left / (len(ordered) - index)
        connection = _PinnedConnection(host, port, address, left, tls, share)
        watchdog = threading.Timer(left, connection.abort)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                connection.connect()
            except (OSError, http.client.HTTPException):
                continue
            return _request(connection, target, request_headers, max_bytes, deadline)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise Unreachable from exc
        finally:
            watchdog.cancel()
            connection.close()
    raise Unreachable
