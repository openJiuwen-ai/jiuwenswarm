"""Bounded, SSRF-safe download for user-supplied file URLs.

``file.import_url`` accepts URLs from chat clients. Before this module the
download path trusted the URL and the caller-supplied filename. That allowed
``file://`` reads, private-network requests and writes outside the workspace.
Keep every rule in one place so other import paths can reuse them.

The connection is pinned to an address that passed the public-address check.
``urllib`` re-resolves the hostname, so a hostile DNS server could answer with
a public address during validation and a private address during connect. This
module builds the ``http.client`` connection itself and keeps the original
hostname for the ``Host`` header and TLS SNI.
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import os
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import ParseResult, urljoin, urlparse, urlunsplit

from jiuwenswarm.server.runtime.attachments.upload_storage import (
    unique_upload_path,
)

logger = logging.getLogger(__name__)

#: ``https`` is preferred. Plain ``http`` stays allowed for intranet links,
#: but every target must still resolve to a public address.
ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Hard cap for one imported file. Downloads stream to disk, so this bounds
#: disk use, not memory.
DEFAULT_MAX_BYTES: Final[int] = 256 * 1024 * 1024

#: Total wall-clock budget for one import, including redirects and reads.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0

#: Upper bound for a single connect or read wait. The total deadline above
#: still applies.
CONNECT_TIMEOUT_SECONDS: Final[float] = 15.0

#: DNS has no timeout argument, so run it on a small bounded pool and wait
#: with the remaining budget.
DNS_TIMEOUT_SECONDS: Final[float] = 10.0

_DNS_EXECUTOR: Final[ThreadPoolExecutor] = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="jiuwenswarm-dns",
)

MAX_REDIRECTS: Final[int] = 5

_READ_CHUNK_BYTES: Final[int] = 65536

_MAX_FILENAME_LENGTH: Final[int] = 200

_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f<>:\"|?*]")

#: Names that would resolve to a directory or the workspace root.
_RESERVED_FILENAMES: Final[frozenset[str]] = frozenset({"", ".", ".."})

_REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

_OK_STATUSES: Final[frozenset[int]] = frozenset({200, 206})


class UnsafeImportUrlError(ValueError):
    """The URL or filename can escape the import boundary."""


def _parsed_url(url: str) -> ParseResult:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeImportUrlError(
            f"unsupported url scheme: {parsed.scheme or '<empty>'}"
        )
    if not parsed.hostname:
        raise UnsafeImportUrlError("url has no hostname")
    if parsed.username or parsed.password:
        raise UnsafeImportUrlError("url must not contain user info")
    try:
        parsed.port
    except ValueError as exc:
        raise UnsafeImportUrlError("url has an invalid port") from exc
    return parsed


def _reject_non_public_address(address: str) -> None:
    """Reject one resolved address unless it is globally routable."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise UnsafeImportUrlError(f"unresolvable address: {address}") from exc
    if not ip.is_global:
        raise UnsafeImportUrlError(
            f"url resolves to a non-public address: {ip}"
        )


def _resolve_public_addresses(
    parsed: ParseResult, *, deadline: float | None = None
) -> tuple[str, ...]:
    """Return every resolved address after checking all of them.

    A host that resolves to one public and one private address is rejected,
    because the connection may pick either result. DNS has no timeout
    argument, so the lookup runs on a bounded pool and the caller stops
    waiting when the import deadline expires.
    """
    host = str(parsed.hostname or "").strip()
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    wait = DNS_TIMEOUT_SECONDS
    if deadline is not None:
        wait = min(wait, _remaining(deadline))
    future = _DNS_EXECUTOR.submit(
        socket.getaddrinfo,
        host,
        parsed.port or default_port,
        type=socket.SOCK_STREAM,
    )
    try:
        infos = future.result(timeout=wait)
    except FuturesTimeout as exc:
        future.cancel()
        raise RuntimeError("dns resolution timed out") from exc
    except socket.gaierror as exc:
        raise UnsafeImportUrlError(f"url host does not resolve: {host}") from exc
    addresses = tuple(
        dict.fromkeys(str(info[4][0]) for info in infos if info[4])
    )
    if not addresses:
        raise UnsafeImportUrlError(f"url host does not resolve: {host}")
    for address in addresses:
        _reject_non_public_address(address)
    return addresses


def validate_import_url(url: str) -> ParseResult:
    """Return parsed URL facts, or raise when the target is unsafe."""
    parsed = _parsed_url(url)
    _resolve_public_addresses(parsed)
    return parsed


def sanitize_filename(raw: object, *, fallback: str = "download") -> str:
    """Return one safe basename for a workspace file.

    Strips directory components, control characters and Windows-reserved
    characters. The result never contains ``/`` or ``\\`` and never names a
    directory.
    """
    text = str(raw or "").replace("\\", "/")
    name = PurePosixPath(text).name
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).strip().strip(".").strip()
    name = name.lstrip(".")
    if name in _RESERVED_FILENAMES:
        name = fallback
    if len(name) > _MAX_FILENAME_LENGTH:
        suffix = PurePosixPath(name).suffix
        keep = max(1, _MAX_FILENAME_LENGTH - len(suffix))
        name = name[:keep] + suffix
    if not name:
        name = fallback
    return name


def _resolve_target(workspace: Path, filename: str, *, overwrite: bool) -> Path:
    root = workspace.resolve(strict=False)
    candidate = (root / filename).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeImportUrlError("filename escapes the workspace") from exc
    if candidate == root:
        raise UnsafeImportUrlError("filename resolves to the workspace root")
    if overwrite:
        return candidate
    if candidate.exists():
        return unique_upload_path(candidate)
    return candidate


class _SocketHolder:
    """Latest live socket for one request, shared with the deadline guard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None

    def set(self, sock: socket.socket | None) -> None:
        with self._lock:
            self._socket = sock

    def get(self) -> socket.socket | None:
        with self._lock:
            return self._socket


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that dials a validated address, not a fresh DNS result."""

    def __init__(
        self,
        host: str,
        *,
        pinned_address: str,
        socket_holder: _SocketHolder,
        **kwargs,
    ) -> None:
        super().__init__(host, **kwargs)
        self._pinned_address = pinned_address
        self._socket_holder = socket_holder

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        self._socket_holder.set(self.sock)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an address while keeping SNI and cert checks."""

    def __init__(
        self,
        host: str,
        *,
        pinned_address: str,
        socket_holder: _SocketHolder,
        **kwargs,
    ) -> None:
        super().__init__(host, **kwargs)
        self._pinned_address = pinned_address
        self._socket_holder = socket_holder

    def connect(self) -> None:
        raw = self._create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        self._socket_holder.set(raw)
        # Split wrap and handshake so the guard can reach the SSLSocket while
        # the TLS handshake is still running. A slow handshake must not outlive
        # the request deadline.
        try:
            ssl_sock = self._context.wrap_socket(
                raw,
                server_hostname=self.host,
                do_handshake_on_connect=False,
            )
        except BaseException:
            raw.close()
            raise
        self._socket_holder.set(ssl_sock)
        try:
            ssl_sock.do_handshake()
        except BaseException:
            ssl_sock.close()
            raise
        self.sock = ssl_sock


def _build_connection(
    parsed: ParseResult,
    address: str,
    timeout: float,
    socket_holder: _SocketHolder,
) -> http.client.HTTPConnection:
    host = str(parsed.hostname or "")
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    port = parsed.port or default_port
    if parsed.scheme.lower() == "https":
        return _PinnedHTTPSConnection(
            host,
            pinned_address=address,
            socket_holder=socket_holder,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return _PinnedHTTPConnection(
        host,
        pinned_address=address,
        socket_holder=socket_holder,
        port=port,
        timeout=timeout,
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("download timed out")
    return remaining


def _set_read_timeout(sock: socket.socket | None, timeout: float) -> None:
    if sock is None:
        return
    try:
        sock.settimeout(max(0.001, timeout))
    except OSError:
        return


def _shutdown_socket(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _response_socket(
    response: http.client.HTTPResponse | None,
) -> socket.socket | None:
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    return getattr(raw, "_sock", None)


def _response_fileno(response: http.client.HTTPResponse | None) -> int | None:
    fp = getattr(response, "fp", None)
    if fp is None:
        return None
    try:
        return fp.fileno()
    except (OSError, ValueError):
        return None


def _temp_file(target: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".part",
        dir=str(target.parent),
    )
    os.close(descriptor)
    return Path(name)


def _commit_temp(temp_path: Path, target: Path, *, overwrite: bool) -> Path:
    """Move the finished temp file into place without losing an older file."""
    if overwrite:
        os.replace(temp_path, target)
        return target

    candidate = target
    for _ in range(1000):
        try:
            os.link(temp_path, candidate)
        except FileExistsError:
            candidate = unique_upload_path(candidate)
            continue
        except OSError:
            # Filesystems without hard links: create the target exclusively,
            # then copy. The old file is never touched.
            try:
                descriptor = os.open(
                    str(candidate),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                candidate = unique_upload_path(candidate)
                continue
            try:
                out = os.fdopen(descriptor, "wb")
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                candidate.unlink(missing_ok=True)
                raise
            try:
                with out, temp_path.open("rb") as src:
                    shutil.copyfileobj(src, out)
            except BaseException:
                candidate.unlink(missing_ok=True)
                raise
        temp_path.unlink(missing_ok=True)
        return candidate
    raise RuntimeError("could not allocate a unique filename")


class _DeadlineGuard:
    """Shut down the live socket when the total deadline expires.

    The guard starts before ``connection.request()``, so it covers the TCP
    connect and TLS handshake. ``_SocketHolder`` receives the socket as soon
    as it exists. ``HTTPConnection.getresponse()`` can close
    ``connection.sock`` for ``Connection: close``; the response fd is still
    live, so the guard also shuts it through a duplicate.
    """

    def __init__(
        self,
        connection: http.client.HTTPConnection,
        socket_holder: _SocketHolder,
        deadline: float,
    ) -> None:
        self._connection = connection
        self._holder = socket_holder
        self._socket = socket_holder.get()
        self._response: http.client.HTTPResponse | None = None
        self.expired = threading.Event()
        self._timer = threading.Timer(
            max(0.0, deadline - time.monotonic()), self._expire
        )
        self._timer.daemon = True
        self._timer.start()

    @property
    def read_socket(self) -> socket.socket | None:
        sock = getattr(self._connection, "sock", None)
        if sock is not None:
            return sock
        return self._holder.get()

    def attach_response(self, response: http.client.HTTPResponse) -> None:
        self._response = response
        response_socket = _response_socket(response)
        if response_socket is not None:
            self._socket = response_socket

    def cancel(self) -> None:
        self._timer.cancel()

    def _expire(self) -> None:
        self.expired.set()
        _shutdown_socket(self._socket)
        _shutdown_socket(self._holder.get())
        _shutdown_socket(getattr(self._connection, "sock", None))
        fd = _response_fileno(self._response)
        if fd is None or fd < 0:
            return
        try:
            duplicate = os.dup(fd)
        except OSError:
            return
        try:
            sock = socket.socket(fileno=duplicate)
        except OSError:
            os.close(duplicate)
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass


def _request_once(
    parsed: ParseResult,
    addresses: tuple[str, ...],
    *,
    deadline: float,
) -> tuple[
    http.client.HTTPConnection,
    http.client.HTTPResponse,
    _DeadlineGuard,
]:
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    headers = {
        "User-Agent": "jiuwenswarm-agent",
        "Host": parsed.netloc,
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    last_error: BaseException | None = None
    for address in addresses:
        remaining = _remaining(deadline)
        connect_timeout = min(remaining, CONNECT_TIMEOUT_SECONDS)
        holder = _SocketHolder()
        connection = _build_connection(
            parsed, address, connect_timeout, holder
        )
        # Start the guard before request(): HTTPS performs the TCP connect and
        # TLS handshake inside request(), and both must respect the deadline.
        guard = _DeadlineGuard(connection, holder, deadline)
        success = False
        try:
            connection.request("GET", path, headers=headers)
            if guard.expired.is_set():
                connection.close()
                raise RuntimeError("download timed out")
            response = connection.getresponse()
            guard.attach_response(response)
            success = True
            return connection, response, guard
        except (OSError, http.client.HTTPException) as exc:
            if guard.expired.is_set():
                connection.close()
                raise RuntimeError("download timed out") from exc
            last_error = exc
        finally:
            if not success:
                guard.cancel()
        connection.close()
    raise RuntimeError(f"download failed: {last_error}")


def download_url_to_workspace(
    url: str,
    workspace: Path | str,
    *,
    preferred_name: object = "",
    fallback_name: str = "download",
    overwrite: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Path:
    """Download ``url`` to ``workspace`` and return the written path.

    The total wall-clock budget is ``timeout``. A slow peer cannot extend it by
    sending one byte now and then.

    Raises ``UnsafeImportUrlError`` when the URL or name is unsafe, and
    ``RuntimeError`` when the transfer is too large, times out, or fails.
    """
    deadline = time.monotonic() + max(0.001, float(timeout))
    workspace_dir = Path(workspace)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    filename = sanitize_filename(preferred_name, fallback=fallback_name)
    target = _resolve_target(workspace_dir, filename, overwrite=overwrite)

    current_url = str(url).strip()
    redirects = 0
    temp_path: Path | None = None
    while True:
        parsed = _parsed_url(current_url)
        addresses = _resolve_public_addresses(parsed, deadline=deadline)
        connection, response, guard = _request_once(
            parsed, addresses, deadline=deadline
        )

        try:
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location:
                    raise RuntimeError("redirect response has no Location header")
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise RuntimeError("too many redirects")
                current_url = urljoin(current_url, location)
                continue

            if response.status not in _OK_STATUSES:
                raise RuntimeError(f"download failed: HTTP {response.status}")

            declared = response.getheader("Content-Length")
            declared_length = int(declared) if declared and declared.isdigit() else None
            if declared_length is not None and declared_length > max_bytes:
                raise RuntimeError(f"remote file is too large: {declared} bytes")

            temp_path = _temp_file(target)
            written = 0
            with temp_path.open("wb") as out:
                while True:
                    _set_read_timeout(guard.read_socket, _remaining(deadline))
                    # ``read`` loops until the requested size or EOF, so one
                    # call can outlive the deadline when the peer sends a
                    # trickle. ``read1`` performs at most one socket read.
                    chunk = response.read1(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError(
                            f"remote file exceeds the {max_bytes} byte limit"
                        )
                    out.write(chunk)
            if guard.expired.is_set():
                raise RuntimeError("download timed out")
            if declared_length is not None and written != declared_length:
                raise RuntimeError("download failed: incomplete body")
            committed = _commit_temp(temp_path, target, overwrite=overwrite)
            temp_path = None
            return committed
        except (OSError, http.client.HTTPException) as exc:
            if guard.expired.is_set():
                raise RuntimeError("download timed out") from exc
            raise RuntimeError(f"download failed: {exc}") from exc
        finally:
            guard.cancel()
            connection.close()
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
