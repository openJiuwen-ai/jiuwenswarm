# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verified and bounded artifact download/extraction shared by Hub consumers."""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlparse

import httpx


class HubDownloadArtifact(Protocol):
    download_url: str
    checksum_sha256: str

_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_SINGLE_FILE_BYTES = 64 * 1024 * 1024
_MAX_FILE_COUNT = 10_000
_MAX_PATH_DEPTH = 32
_DEFAULT_ALLOWED_HOSTS = (
    "openjiuwen-market.obs.*.myhuaweicloud.com",
    "*.obs.*.myhuaweicloud.com",
    "127.0.0.1",
    "localhost",
)
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def _allow_insecure_http() -> bool:
    return (
        os.getenv("TEAM_SKILLS_HUB_ALLOW_INSECURE_HTTP", "").strip().lower()
        in _TRUE_VALUES
    )


class HubDownloadError(RuntimeError):
    """Raised when a Hub artifact fails transport or archive validation."""


class HubPackageDownloader:
    def __init__(
        self,
        *,
        fetch_bytes: Callable[[str], Awaitable[bytes]] | None = None,
        allowed_download_hosts: tuple[str, ...] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._fetch_bytes = fetch_bytes or self._http_fetch_bytes
        self.timeout = max(1.0, float(timeout))
        if allowed_download_hosts is None:
            raw = os.getenv("TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS", "").strip()
            allowed_download_hosts = (
                tuple(part.strip().lower() for part in raw.split(",") if part.strip())
                if raw
                else _DEFAULT_ALLOWED_HOSTS
            )
        self.allowed_download_hosts = allowed_download_hosts

    async def _http_fetch_bytes(self, url: str) -> bytes:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    return await self._read_bounded_response(response)
        except Exception as exc:
            if isinstance(exc, HubDownloadError):
                raise
            raise HubDownloadError(f"下载 Hub 包失败: {exc}") from exc

    @staticmethod
    async def _read_bounded_response(response: object) -> bytes:
        headers = getattr(response, "headers", {})
        raw_length = headers.get("content-length") if hasattr(headers, "get") else None
        if raw_length:
            try:
                if int(raw_length) > _MAX_DOWNLOAD_BYTES:
                    raise HubDownloadError("下载 ZIP 大小超过限制")
            except ValueError:
                pass
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_DOWNLOAD_BYTES:
                raise HubDownloadError("下载 ZIP 大小超过限制")
        return bytes(body)

    @staticmethod
    def _host_matches(host: str, rule: str) -> bool:
        if rule.startswith("."):
            return host.endswith(rule)
        host_parts = host.split(".")
        rule_parts = rule.split(".")
        if len(host_parts) != len(rule_parts):
            return False
        return all(
            rule == "*" or actual == rule
            for actual, rule in zip(host_parts, rule_parts)
        )

    def assert_download_url_allowed(self, url: str) -> None:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"}:
            raise HubDownloadError("Hub download_url 必须使用 HTTP 或 HTTPS")
        host = (parsed.hostname or "").strip().lower()
        if not host:
            raise HubDownloadError("Hub download_url 缺少主机名")
        if (
            parsed.scheme == "http"
            and host not in {"localhost", "127.0.0.1", "::1"}
            and not _allow_insecure_http()
        ):
            raise HubDownloadError("Hub 生产下载地址必须使用 HTTPS")
        if not any(
            self._host_matches(host, rule) for rule in self.allowed_download_hosts
        ):
            raise HubDownloadError(f"Hub download_url host 不在白名单: {host}")

    @staticmethod
    def _verify_checksum(body: bytes, expected: str) -> None:
        normalized = str(expected or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
            raise HubDownloadError(
                "Hub SHA-256 checksum must be a 64-character hexadecimal value"
            )
        if hashlib.sha256(body).hexdigest().lower() != normalized:
            raise HubDownloadError("下载文件校验失败（SHA256 不匹配）")

    @staticmethod
    def _validated_members(
        archive: zipfile.ZipFile,
    ) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
        infos = archive.infolist()
        if len(infos) > _MAX_FILE_COUNT:
            raise HubDownloadError("ZIP 文件数量超过限制")
        total_size = 0
        validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for info in infos:
            raw = (info.filename or "").replace("\\", "/")
            if not raw or "\0" in raw:
                raise HubDownloadError("ZIP 包含非法路径")
            relative = PurePosixPath(raw.rstrip("/"))
            if relative.is_absolute() or PureWindowsPath(raw).is_absolute():
                raise HubDownloadError("ZIP 包含非法路径")
            if ".." in relative.parts or len(relative.parts) > _MAX_PATH_DEPTH:
                raise HubDownloadError("ZIP 包含非法路径")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise HubDownloadError("ZIP 包含链接文件")
            if info.file_size > _MAX_SINGLE_FILE_BYTES:
                raise HubDownloadError("ZIP 单文件大小超过限制")
            total_size += info.file_size
            if total_size > _MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise HubDownloadError("ZIP 解包总大小超过限制")
            validated.append((info, relative))
        return validated

    @classmethod
    def extract_zip(cls, body: bytes, destination: Path) -> None:
        if not body.startswith(b"PK"):
            raise HubDownloadError("下载内容不是 ZIP 文件")
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        try:
            with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
                members = cls._validated_members(archive)
                if archive.testzip() is not None:
                    raise HubDownloadError("下载 ZIP 文件已损坏")
                for info, relative in members:
                    target = root.joinpath(*relative.parts)
                    try:
                        target.resolve().relative_to(root)
                    except ValueError as exc:
                        raise HubDownloadError("ZIP 包含非法路径") from exc
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as source, target.open("wb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
        except zipfile.BadZipFile as exc:
            raise HubDownloadError("下载内容不是有效 ZIP 文件") from exc

    async def download_and_extract(
        self, artifact: HubDownloadArtifact, destination: Path
    ) -> None:
        body = await self.download_bytes(artifact)
        self.extract_zip(body, destination)

    async def download_bytes(self, artifact: HubDownloadArtifact) -> bytes:
        """Download and validate an artifact without extracting it."""
        self.assert_download_url_allowed(artifact.download_url)
        body = await self._fetch_bytes(artifact.download_url)
        if not body:
            raise HubDownloadError("下载内容为空")
        if len(body) > _MAX_DOWNLOAD_BYTES:
            raise HubDownloadError("下载 ZIP 大小超过限制")
        self._verify_checksum(body, artifact.checksum_sha256)
        if not body.startswith(b"PK"):
            raise HubDownloadError("下载内容不是 ZIP 文件")
        try:
            with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
                if archive.testzip() is not None:
                    raise HubDownloadError("下载 ZIP 文件已损坏")
        except zipfile.BadZipFile as exc:
            raise HubDownloadError("下载内容不是有效 ZIP 文件") from exc
        return body
