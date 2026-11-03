"""Bounded, SSRF-safe download for user-supplied file URLs.

``file.import_url`` accepts URLs from chat clients. Before this module the
download path trusted the URL and the caller-supplied filename. That allowed
``file://`` reads, private-network requests and writes outside the workspace.
Keep every rule in one place so other import paths can reuse them.

Residual risk: DNS is validated before the connection, then urllib resolves
the host again. A hostile DNS server can rebind between the two lookups. The
IP allowlist still blocks private ranges at validation time; pinning the
connection would need a custom HTTP handler.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import ParseResult, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

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

DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0

_MAX_FILENAME_LENGTH: Final[int] = 200

_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f<>:\"|?*]")

#: Names that would resolve to a directory or the workspace root.
_RESERVED_FILENAMES: Final[frozenset[str]] = frozenset({"", ".", ".."})


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


def validate_import_url(url: str) -> ParseResult:
    """Return parsed URL facts, or raise when the target is unsafe.

    Checks the scheme, credentials, port, and every address the host resolves
    to. A host that resolves to any private, loopback, link-local or reserved
    address is rejected, because the request may pick any of them.
    """
    parsed = _parsed_url(url)
    host = str(parsed.hostname or "").strip()
    try:
        infos = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafeImportUrlError(f"url host does not resolve: {host}") from exc
    addresses = {str(info[4][0]) for info in infos if info[4]}
    if not addresses:
        raise UnsafeImportUrlError(f"url host does not resolve: {host}")
    for address in addresses:
        _reject_non_public_address(address)
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


def _open_target(target: Path, *, overwrite: bool) -> int:
    """Open ``target`` without following a symlink and without clobbering."""
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_TRUNC if overwrite else os.O_EXCL
    return os.open(str(target), flags, 0o600)


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


class _ValidatingRedirectHandler(HTTPRedirectHandler):
    """Validate every redirect hop before urllib follows it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        validate_import_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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

    Raises ``UnsafeImportUrlError`` when the URL or name is unsafe, and
    ``RuntimeError`` when the transfer is too large or fails.
    """
    parsed = validate_import_url(url)
    workspace_dir = Path(workspace)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    filename = sanitize_filename(preferred_name, fallback=fallback_name)
    target = _resolve_target(workspace_dir, filename, overwrite=overwrite)

    request = Request(
        str(url).strip(),
        headers={"User-Agent": "jiuwenswarm-agent"},
    )
    opener = build_opener(_ValidatingRedirectHandler())
    written = 0
    try:
        with opener.open(request, timeout=timeout) as upstream:
            declared = upstream.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise RuntimeError(
                    f"remote file is too large: {declared} bytes"
                )
            descriptor = _open_target(target, overwrite=overwrite)
            try:
                with os.fdopen(descriptor, "wb") as out:
                    while True:
                        chunk = upstream.read(65536)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise RuntimeError(
                                f"remote file exceeds the {max_bytes} byte limit"
                            )
                        out.write(chunk)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"download failed: {exc}") from exc
    if written == 0:
        logger.debug(
            "[remote_fetch] downloaded an empty file: scheme=%s host=%s",
            parsed.scheme,
            parsed.hostname,
        )
    return target
