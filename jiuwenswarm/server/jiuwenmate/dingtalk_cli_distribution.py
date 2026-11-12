"""Install the pinned native DWS release without a Node.js runtime."""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.sqlite_store import private_dir

logger = logging.getLogger(__name__)

DWS_VERSION = "1.0.65"
# Gitee first: a slow but live GitHub transfer could otherwise consume most of the prepare budget.
_RELEASE_SOURCES = (
    "https://gitee.com/dingtalk-real-ai/dingtalk-workspace-cli",
    "https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli",
)
# Checksums from the official immutable v1.0.65 release, shared by both mirrors.
_RELEASE_SHA256 = {
    "darwin-amd64": "6afdd8dcb093ea0c7aa9d0e76371f44308aa6ee9f8a44bf0d2b010bb2e3c8244",
    "darwin-arm64": "8550c9c42a2018204761db923911321a56df9bedcacf3d42baffae00ccd7bc44",
    "linux-amd64": "e712e01924935070ef0d313ef898bf90e5124c6c4ad9ef197c3647180b4be9cb",
    "linux-arm64": "8858dbaaa9552c4028aaa23c621d4ab960e1eda8d5078e238e7ec216ac2a00d7",
    "windows-amd64": "db7cd21655ede7375676a349f44f5d9116fe28a8836276244341e109314407b7",
    "windows-arm64": "c275d60eaab2acc55dd0b24e09885d481cb5b2a26fec0d7ab4740e9d0722a582",
}
_ARCH_ALIASES = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_BINARY_BYTES = 128 * 1024 * 1024
_SOURCE_TIMEOUT_SECONDS = 45
_IO_TIMEOUT_SECONDS = 8


def native_dws_platform() -> str | None:
    """Select a published archive for the current OS and architecture."""
    machine = _ARCH_ALIASES.get(platform.machine().lower())
    tag = f"{platform.system().lower()}-{machine}"
    return tag if tag in _RELEASE_SHA256 else None


def managed_dws_path(cache_root: Path, platform_tag: str) -> Path:
    binary = "dws.exe" if platform_tag.startswith("windows-") else "dws"
    return cache_root / "managed" / DWS_VERSION / platform_tag / binary


def _download_archive(url: str, archive: Path, expected_sha256: str, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("DWS release download deadline expired")
    digest = hashlib.sha256()
    size = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=min(remaining, _IO_TIMEOUT_SECONDS)) as response:
        response.raise_for_status()
        with archive.open("wb") as output:
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if time.monotonic() >= deadline:
                    raise TimeoutError("DWS release download deadline expired")
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    raise ValueError("DWS release archive is too large")
                digest.update(chunk)
                output.write(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("DWS release archive checksum mismatch")


def _extract_binary(archive: Path, binary: Path) -> None:
    if binary.suffix == ".exe":
        with zipfile.ZipFile(archive) as package:
            name = binary.name if binary.name in package.namelist() else f"./{binary.name}"
            member = package.getinfo(name)
            if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("DWS release binary is not a regular file")
            if not 0 < member.file_size <= _MAX_BINARY_BYTES:
                raise ValueError("Invalid DWS release binary size")
            with package.open(member) as source, binary.open("wb") as output:
                shutil.copyfileobj(source, output)
        return

    with tarfile.open(archive, "r:gz") as package:
        names = {binary.name, f"./{binary.name}"}
        member = next((entry for entry in package if entry.name in names), None)
        if member is None or not member.isfile() or not 0 < member.size <= _MAX_BINARY_BYTES:
            raise ValueError("Invalid DWS release binary")
        source = package.extractfile(member)
        if source is None:
            raise ValueError("Missing DWS release binary")
        with source, binary.open("wb") as output:
            shutil.copyfileobj(source, output)


def install_managed_dws(cache_root: Path, platform_tag: str, *, timeout: float) -> Path:
    """Verify a release archive and atomically publish only its executable."""
    target = managed_dws_path(cache_root, platform_tag)
    suffix = ".zip" if target.suffix == ".exe" else ".tar.gz"
    archive_name = f"dws-{platform_tag}{suffix}"
    deadline = time.monotonic() + timeout
    try:
        if not target.parent.resolve().is_relative_to(cache_root):
            raise ValueError("DWS installation directory escapes the program cache")
        private_dir(target.parent)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=target.parent) as staging:
            archive = Path(staging) / archive_name
            for source in _RELEASE_SOURCES:
                source_deadline = min(deadline, time.monotonic() + _SOURCE_TIMEOUT_SECONDS)
                try:
                    url = f"{source}/releases/download/v{DWS_VERSION}/{archive_name}"
                    _download_archive(url, archive, _RELEASE_SHA256[platform_tag], source_deadline)
                    break
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    logger.warning("DWS release download or checksum verification failed: %s", type(exc).__name__)
            else:
                raise OSError("No verified DWS release archive could be downloaded")
            binary = Path(staging) / target.name
            _extract_binary(archive, binary)
            binary.chmod(0o700)
            os.replace(binary, target)
    except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise PersistentAgentError(
            "无法下载、校验或安装钉钉 CLI，请检查网络和本地缓存目录后重试。",
            code="DINGTALK_CLI_PREPARE_FAILED",
        ) from exc
    return target
