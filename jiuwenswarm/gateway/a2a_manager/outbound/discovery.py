"""Secure Agent Card discovery for the personal-edition outbound manager."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
import httpx
from a2a.client import ClientConfig
from a2a.client.card_resolver import parse_agent_card
from a2a.client.client_factory import ClientFactory
from google.protobuf.json_format import MessageToDict
from httpcore._backends.auto import AutoBackend

from .errors import A2AOutboundError, A2AOutboundErrorCode
from .models import A2ACompatibleInterface, A2ADiscoveredAgent

DEFAULT_CARD_PATH = "/.well-known/agent-card.json"
MAX_DISCOVERY_URL_LENGTH = 2048
MAX_REDIRECTS = 3
MAX_CARD_BYTES = 1_048_576

AddressResolver = Callable[[str, int], Awaitable[list[str]]]
TransportFactory = Callable[[Mapping[str, str]], httpx.AsyncBaseTransport]


@dataclass(frozen=True)
class DiscoveredCard:
    source_url: str
    card_path: str
    card_url: str
    card_fingerprint: str
    agent: A2ADiscoveredAgent
    agent_card: dict[str, Any]
    security_requirements: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _ValidatedTarget:
    host: str
    port: int
    pinned_address: str


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect hostnames to addresses selected by the SSRF validator.

    httpcore still receives the original URL host, so TLS uses the original
    hostname for SNI and certificate verification. Only the TCP destination is
    replaced, closing the validate-then-resolve DNS rebinding window.
    """

    def __init__(
        self,
        pinned_addresses: Mapping[str, str],
        *,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._pinned_addresses = {
            str(host).lower().rstrip("."): str(address)
            for host, address in pinned_addresses.items()
        }
        self._backend = backend or AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        normalized_host = str(host).lower().rstrip(".")
        pinned = self._pinned_addresses.get(normalized_host)
        if pinned is None:
            raise httpcore.ConnectError("connection target was not validated")
        return await self._backend.connect_tcp(
            pinned,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _HttpcoreResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _PinnedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, pinned_addresses: Mapping[str, str]) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=4,
            max_keepalive_connections=0,
            network_backend=_PinnedNetworkBackend(pinned_addresses),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_HttpcoreResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def _pinned_transport_factory(
    pinned_addresses: Mapping[str, str],
) -> httpx.AsyncBaseTransport:
    return _PinnedAsyncHTTPTransport(pinned_addresses)


def create_pinned_transport(
    pinned_addresses: Mapping[str, str],
) -> httpx.AsyncBaseTransport:
    """Build the shared DNS-pinned transport used by discovery and dispatch."""
    return _pinned_transport_factory(pinned_addresses)


async def _resolve_addresses(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    rows = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(row[4][0]).split("%", 1)[0] for row in rows})


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(ip.is_global) and not any(
        (
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_rfc1918_ipv4_only(addresses: list[str]) -> bool:
    # Match Manager's RFC1918-only policy; IPv6 ULA/mapped addresses
    # are not included in the private-network exception.
    for item in addresses:
        address = ipaddress.ip_address(item)
        if address.version != 4:
            return False
        in_private = False
        for network in _RFC1918_NETWORKS:
            if address in network:
                in_private = True
                break
        if not in_private:
            return False
    return True


def _http_blocked_by_policy(
    network_policy: dict[str, bool], *, public_only: bool
) -> bool:
    if network_policy.get("allow_http") is not True:
        return True
    if public_only and network_policy.get("allow_public_http") is not True:
        return True
    return False


class A2AOutboundDiscoveryService:
    """Resolve a caller-supplied Card URL without granting invocation rights."""

    def __init__(
        self,
        *,
        allow_loopback_http: bool = False,
        address_resolver: AddressResolver = _resolve_addresses,
        transport_factory: TransportFactory = _pinned_transport_factory,
    ) -> None:
        self._allow_loopback_http = allow_loopback_http
        self._address_resolver = address_resolver
        self._transport_factory = transport_factory

    @property
    def allow_loopback_http(self) -> bool:
        return self._allow_loopback_http

    def set_allow_loopback_http(self, enabled: bool) -> None:
        self._allow_loopback_http = bool(enabled)

    async def discover(self, url: str, card_path: str | None = None) -> DiscoveredCard:
        source_url, normalized_path, card_url = self._normalize(url, card_path)
        current = card_url
        parsed = None
        canonical: dict[str, Any] | None = None
        for redirect_count in range(MAX_REDIRECTS + 1):
            target = await self._validate_network_target(current)
            transport = self._transport_factory({target.host: target.pinned_address})
            try:
                async with httpx.AsyncClient(
                    transport=transport,
                    follow_redirects=False,
                    timeout=httpx.Timeout(10.0),
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "GET", current, headers={"Accept": "application/json"}
                    ) as response:
                        if response.is_redirect:
                            if redirect_count >= MAX_REDIRECTS:
                                raise A2AOutboundError(
                                    A2AOutboundErrorCode.CARD_FETCH_FAILED
                                )
                            location = response.headers.get("location", "")
                            next_url = urljoin(current, location)
                            next_host = (urlsplit(next_url).hostname or "").lower()
                            current_host = (urlsplit(current).hostname or "").lower()
                            if next_host != current_host:
                                raise A2AOutboundError(
                                    A2AOutboundErrorCode.DISCOVERY_BLOCKED
                                )
                            current = next_url
                            continue
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_CARD_BYTES:
                                raise A2AOutboundError(
                                    A2AOutboundErrorCode.CARD_INVALID
                                )
                        try:
                            raw_card = json.loads(body)
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            raise A2AOutboundError(
                                A2AOutboundErrorCode.CARD_INVALID
                            ) from exc
                        if not isinstance(raw_card, dict):
                            raise A2AOutboundError(A2AOutboundErrorCode.CARD_INVALID)
                        parsed = parse_agent_card(copy.deepcopy(raw_card))
                        canonical = MessageToDict(
                            parsed, preserving_proto_field_name=False
                        )
                        # ClientFactory is the SDK source of truth for transport
                        # compatibility. The client remains open for this check.
                        factory = ClientFactory(ClientConfig(httpx_client=client))
                        factory.create(parsed)
                        break
            except A2AOutboundError:
                raise
            except httpx.HTTPStatusError as exc:
                raise A2AOutboundError(A2AOutboundErrorCode.CARD_FETCH_FAILED) from exc
            except (httpx.RequestError, httpcore.NetworkError, OSError) as exc:
                raise A2AOutboundError(A2AOutboundErrorCode.CARD_FETCH_FAILED) from exc
            except Exception as exc:
                raise A2AOutboundError(A2AOutboundErrorCode.CARD_INVALID) from exc
        else:  # pragma: no cover - loop always exits or raises
            raise A2AOutboundError(A2AOutboundErrorCode.CARD_FETCH_FAILED)

        if parsed is None or canonical is None:
            raise A2AOutboundError(A2AOutboundErrorCode.CARD_INVALID)

        interfaces = tuple(
            A2ACompatibleInterface(
                protocol_binding=item.protocol_binding,
                protocol_version=item.protocol_version,
                url=item.url,
            ).validate()
            for item in parsed.supported_interfaces
            if item.protocol_binding.upper() == "JSONRPC"
        )
        if not interfaces:
            raise A2AOutboundError(A2AOutboundErrorCode.CARD_INVALID)
        for interface in interfaces:
            await self._validate_network_target(interface.url)

        skills = tuple(
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "tags": list(item.tags),
            }
            for item in parsed.skills
        )
        security = tuple(canonical.get("securityRequirements") or [])
        fingerprint_source = json.dumps(
            canonical, sort_keys=True, separators=(",", ":")
        )
        fingerprint = (
            f"sha256:{hashlib.sha256(fingerprint_source.encode()).hexdigest()}"
        )
        final_parts = urlsplit(current)
        return DiscoveredCard(
            source_url=source_url,
            card_path=normalized_path,
            card_url=urlunsplit(
                (final_parts.scheme, final_parts.netloc, final_parts.path, "", "")
            ),
            card_fingerprint=fingerprint,
            agent=A2ADiscoveredAgent(
                name=parsed.name,
                description=parsed.description,
                version=parsed.version,
                skills=skills,
                compatible_interfaces=interfaces,
            ),
            agent_card=canonical,
            security_requirements=security,
            warnings=(),
        )

    @staticmethod
    def _normalize(url: str, card_path: str | None) -> tuple[str, str, str]:
        text = str(url or "").strip()
        if not text or len(text) > MAX_DISCOVERY_URL_LENGTH:
            raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_URL_INVALID)
        try:
            parts = urlsplit(text)
            _ = parts.port
        except ValueError as exc:
            raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_URL_INVALID) from exc
        if parts.scheme.lower() not in {"https", "http"} or not parts.hostname:
            raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_URL_INVALID)
        if parts.username or parts.password or parts.fragment:
            raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_URL_INVALID)
        if parts.query:
            raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_URL_INVALID)
        scheme = parts.scheme.lower()
        host = parts.hostname.lower().rstrip(".")
        port = parts.port
        default_port = 443 if scheme == "https" else 80
        netloc = f"[{host}]" if ":" in host else host
        if port and port != default_port:
            netloc = f"{netloc}:{port}"
        path = parts.path or "/"
        direct_card = card_path is None and path != "/"
        normalized_path = (
            path if direct_card else str(card_path or DEFAULT_CARD_PATH).strip()
        )
        if (
            not normalized_path.startswith("/")
            or "?" in normalized_path
            or "#" in normalized_path
        ):
            raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_URL_INVALID)
        source_path = "/" if direct_card else path.rstrip("/") or "/"
        source_url = urlunsplit((scheme, netloc, source_path, "", ""))
        card_url = (
            urlunsplit((scheme, netloc, normalized_path, "", ""))
            if direct_card
            else urljoin(f"{source_url.rstrip('/')}/", normalized_path.lstrip("/"))
        )
        return source_url, normalized_path, card_url

    async def _validate_network_target(
        self, url: str, *, network_policy: dict[str, bool] | None = None
    ) -> _ValidatedTarget:
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_URL_INVALID) from exc
        has_credentials = bool(parts.username or parts.password)
        if scheme not in {"https", "http"} or not parts.hostname or has_credentials:
            raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_URL_INVALID)
        try:
            addresses = await self._address_resolver(parts.hostname, port)
        except OSError as exc:
            raise A2AOutboundError(A2AOutboundErrorCode.CARD_FETCH_FAILED) from exc
        if not addresses:
            raise A2AOutboundError(A2AOutboundErrorCode.CARD_FETCH_FAILED)
        loopback_only = all(
            ipaddress.ip_address(item).is_loopback for item in addresses
        )
        public_only = all(_is_public_address(item) for item in addresses)
        if network_policy is None:
            if scheme == "http" and not (self._allow_loopback_http and loopback_only):
                raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_BLOCKED)
            if not public_only and not (self._allow_loopback_http and loopback_only):
                raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_BLOCKED)
        else:
            private_only = _is_rfc1918_ipv4_only(addresses)
            allowed_address = public_only
            if network_policy.get("allow_loopback") is True and loopback_only:
                allowed_address = True
            if network_policy.get("allow_private_network") is True and private_only:
                allowed_address = True
            if not allowed_address:
                raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_BLOCKED)
            if scheme == "http" and _http_blocked_by_policy(
                network_policy, public_only=public_only
            ):
                raise A2AOutboundError(A2AOutboundErrorCode.DISCOVERY_BLOCKED)
        return _ValidatedTarget(
            host=parts.hostname.lower().rstrip("."),
            port=port,
            pinned_address=addresses[0],
        )

    async def validate_network_target(
        self, url: str, *, network_policy: dict[str, bool] | None = None
    ) -> _ValidatedTarget:
        """Revalidate and resolve a target immediately before a connection."""
        return await self._validate_network_target(url, network_policy=network_policy)


__all__ = [
    "A2AOutboundDiscoveryService",
    "DiscoveredCard",
    "DEFAULT_CARD_PATH",
    "create_pinned_transport",
]
