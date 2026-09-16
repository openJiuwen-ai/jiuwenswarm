# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-scoped, single-attempt streaming uploads to the configured Hub."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import BinaryIO, Literal
from urllib.parse import urlparse

import httpx

from jiuwenswarm.server.runtime.marketplace.asset_publish_models import (
    PublishIdentity,
    PublishProtocolError,
    PublishResult,
)
from jiuwenswarm.server.runtime.marketplace.hub_client import HttpHubTransport
from jiuwenswarm.server.runtime.marketplace.hub_publish_port import parse_publish_result

_CHUNK_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_KNOWN_ERRORS = frozenset(
    {
        "checksum_required",
        "checksum_mismatch",
        "invalid_file_format",
        "invalid_version",
        "invalid_plugin_structure",
        "invalid_plugin_config",
        "invalid_agent_plugin_manifest",
        "invalid_agent_plugin_capability",
        "invalid_agent_mcp",
        "invalid_manifest_json",
        "missing_persona",
        "dangerous_content",
        "invalid_visibility",
        "unauthorized",
        "permission_denied",
        "plugin_not_found",
        "version_conflict",
        "version_exists",
        "plugin_name_exists",
        "plugin_type_immutable",
        "file_too_large",
        "rate_limited",
    }
)


@dataclass(frozen=True, slots=True)
class PublishAuth:
    """Ephemeral credentials; never include this object in persistent records."""

    access_token: str = field(repr=False)
    oauth_provider: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.access_token, str) or not re.fullmatch(
            r"[!-~]+", self.access_token
        ):
            raise ValueError("Invalid OAuth token")
        if self.oauth_provider is not None and (
            not isinstance(self.oauth_provider, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", self.oauth_provider)
        ):
            raise ValueError("Invalid OAuth provider")


@dataclass(frozen=True, slots=True)
class PublishRequest:
    identity: PublishIdentity
    artifact_path: Path
    artifact_sha256: str
    display_name: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    version_desc: str = ""
    visibility: Literal["public", "private"] = "public"
    force: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.identity, PublishIdentity) or not isinstance(
            self.artifact_path, Path
        ):
            raise ValueError("Invalid prepared artifact")
        if not isinstance(self.artifact_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.artifact_sha256
        ):
            raise ValueError("Invalid artifact checksum")
        if self.visibility not in ("public", "private") or not isinstance(
            self.force, bool
        ):
            raise ValueError("Invalid publish options")
        if not isinstance(self.tags, tuple) or any(
            not isinstance(tag, str) for tag in self.tags
        ):
            raise ValueError("Invalid tags")
        for value in (
            self.display_name,
            self.description,
            self.version_desc,
            ",".join(self.tags),
            self.identity.package_name,
            self.identity.version,
            self.identity.target_asset_id or "",
        ):
            if not isinstance(value, str) or len(value.encode("utf-8")) > 65536:
                raise ValueError("Invalid or oversized metadata")


class PublishUploadError(RuntimeError):
    """Safe local error; raw response bodies and credentials are never retained."""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        outcome_unknown: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.outcome_unknown = outcome_unknown
        self.retry_after = retry_after


class HubPublishClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: httpx.Timeout | None = None,
        max_zip_bytes: int = 50 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = HttpHubTransport(base_url=base_url).base_url
        parsed = urlparse(self.base_url)
        if any((parsed.username is not None, parsed.password is not None,
                parsed.query, parsed.fragment)):
            raise ValueError("Hub URL must not contain credentials, query or fragment")
        if (
            not isinstance(max_zip_bytes, int)
            or isinstance(max_zip_bytes, bool)
            or max_zip_bytes <= 0
        ):
            raise ValueError("Invalid ZIP size limit")
        self.max_zip_bytes = max_zip_bytes
        self.timeout = (
            timeout
            if timeout is not None
            else httpx.Timeout(connect=15, write=120, read=120, pool=15)
        )
        self._transport = transport

    async def publish(
        self, request: PublishRequest, *, auth: PublishAuth
    ) -> PublishResult:
        """Upload a prepared archive once, using only the explicitly supplied user."""
        try:
            before_open = request.artifact_path.lstat()
            if not stat.S_ISREG(before_open.st_mode):
                raise PublishUploadError("artifact_unavailable")
            flags = os.O_RDONLY
            for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"):
                flags |= getattr(os, name, 0)
            descriptor = os.open(request.artifact_path, flags, 0o600)
            try:
                archive = os.fdopen(descriptor, "rb")
            except BaseException:
                os.close(descriptor)
                raise
        except OSError:
            raise PublishUploadError("artifact_unavailable") from None
        with archive:
            file_stat = os.fstat(archive.fileno())
            if (file_stat.st_dev, file_stat.st_ino) != (
                before_open.st_dev,
                before_open.st_ino,
            ):
                raise PublishUploadError("artifact_changed")
            if not stat.S_ISREG(file_stat.st_mode):
                raise PublishUploadError("invalid_artifact")
            if file_stat.st_size > self.max_zip_bytes:
                raise PublishUploadError("file_too_large")
            digest = hashlib.sha256()
            first = True
            try:
                while True:
                    chunk = await asyncio.to_thread(archive.read, _CHUNK_BYTES)
                    if not chunk:
                        break
                    if first and chunk[:4] not in (b"PK\x03\x04", b"PK\x05\x06"):
                        raise PublishUploadError("invalid_file_format")
                    first = False
                    digest.update(chunk)
                if first or digest.hexdigest() != request.artifact_sha256:
                    raise PublishUploadError("artifact_changed")
                archive.seek(0)
            except OSError:
                raise PublishUploadError("artifact_read_failed") from None
            try:
                return await self._upload(request, auth, archive, file_stat.st_size)
            except OSError:
                raise PublishUploadError(
                    "artifact_read_failed", outcome_unknown=True
                ) from None

    async def _upload(
        self, request: PublishRequest, auth: PublishAuth, archive: BinaryIO, size: int
    ) -> PublishResult:
        fields = {
            "asset_name": request.identity.package_name,
            "plugin_version": request.identity.version,
            "display_name": request.display_name,
            "description": request.description,
            "tags": ",".join(request.tags),
            "version_desc": request.version_desc,
            "visibility": request.visibility,
            "force": str(request.force).lower(),
        }
        if request.identity.target_asset_id is not None:
            fields["plugin_id"] = request.identity.target_asset_id
        boundary = secrets.token_hex(24)
        prefix = (
            b"".join(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
                for name, value in fields.items()
            )
            + (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="asset.zip"\r\n'
                "Content-Type: application/zip\r\n\r\n"
            ).encode()
        )
        suffix = f"\r\n--{boundary}--\r\n".encode()

        async def content():
            yield prefix
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = await asyncio.to_thread(
                    archive.read, min(_CHUNK_BYTES, remaining)
                )
                if not chunk:
                    raise PublishUploadError("artifact_changed", outcome_unknown=True)
                remaining -= len(chunk)
                digest.update(chunk)
                yield chunk
            if digest.hexdigest() != request.artifact_sha256:
                raise PublishUploadError("artifact_changed", outcome_unknown=True)
            yield suffix

        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Accept-Encoding": "identity",
            "X-Checksum-SHA256": request.artifact_sha256,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(prefix) + size + len(suffix)),
        }
        if auth.oauth_provider:
            headers["X-OAuth-Provider"] = auth.oauth_provider
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False, transport=self._transport
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/v1/plugins",
                    headers=headers,
                    content=content(),
                ) as response:
                    # Limit before decoding: even a tiny gzip body can expand enormously.
                    encoding = (
                        response.headers.get("Content-Encoding", "identity")
                        .strip()
                        .lower()
                    )
                    if encoding != "identity":
                        raise PublishUploadError(
                            "unsupported_response_encoding",
                            http_status=response.status_code,
                            outcome_unknown=True,
                        ) from None
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise PublishUploadError(
                                "invalid_response",
                                http_status=response.status_code,
                                outcome_unknown=True,
                            )
                        raw.extend(chunk)
                    return self._parse_response(response, raw, request)
        except httpx.RequestError:
            raise PublishUploadError(
                "upload_outcome_unknown", outcome_unknown=True
            ) from None

    @staticmethod
    def _parse_response(
        response: httpx.Response, raw: bytearray, request: PublishRequest
    ) -> PublishResult:
        status = response.status_code
        if status != 200:
            code = "hub_request_failed"
            try:
                detail = json.loads(raw).get("detail", {})
                candidate = detail.get("error")
                if isinstance(candidate, str) and candidate in _KNOWN_ERRORS:
                    code = candidate
            except (ValueError, AttributeError, TypeError, RecursionError):
                pass
            retry = response.headers.get("Retry-After", "")
            retry_after = (
                int(retry)
                if retry.isascii() and retry.isdigit() and len(retry) < 10
                else None
            )
            raise PublishUploadError(
                code,
                http_status=status,
                outcome_unknown=status == 408 or not 400 <= status < 500,
                retry_after=retry_after,
            ) from None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("code") != 200:
                raise PublishProtocolError("Invalid Hub envelope")
            return parse_publish_result(
                payload.get("data"),
                request.identity,
                expected_visibility=request.visibility,
            )
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise PublishUploadError(
                "invalid_response", http_status=status, outcome_unknown=True
            ) from None
