# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.marketplace.hub_models import HubArtifact
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import (
    HubDownloadError,
    HubPackageDownloader,
)


class FakeStreamingResponse:
    def __init__(
        self, chunks: list[bytes], *, content_length: int | None = None
    ) -> None:
        self._chunks = chunks
        self.headers = (
            {"content-length": str(content_length)}
            if content_length is not None
            else {}
        )

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


def _zip_bytes(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")
    return stream.getvalue()


def _artifact(checksum: str) -> HubArtifact:
    return HubArtifact(
        asset_id="sales-expert",
        plugin_type="agent_template",
        version="1.2.3",
        download_url="https://downloads.example.test/sales-expert.zip",
        checksum_sha256=checksum,
    )


@pytest.mark.asyncio
async def test_download_and_extract_verifies_checksum_and_writes_files(
    tmp_path: Path,
) -> None:
    body = _zip_bytes({"sales-expert/manifest.json": b"{}"})

    async def fetch_bytes(_url: str) -> bytes:
        return body

    downloader = HubPackageDownloader(
        fetch_bytes=fetch_bytes,
        allowed_download_hosts=("downloads.example.test",),
    )
    await downloader.download_and_extract(
        _artifact(hashlib.sha256(body).hexdigest()), tmp_path
    )

    assert (tmp_path / "sales-expert" / "manifest.json").read_bytes() == b"{}"


@pytest.mark.asyncio
async def test_download_and_extract_rejects_checksum_mismatch(tmp_path: Path) -> None:
    body = _zip_bytes({"manifest.json": b"{}"})

    async def fetch_bytes(_url: str) -> bytes:
        return body

    downloader = HubPackageDownloader(
        fetch_bytes=fetch_bytes,
        allowed_download_hosts=("downloads.example.test",),
    )
    with pytest.raises(HubDownloadError, match="SHA256"):
        await downloader.download_and_extract(_artifact("0" * 64), tmp_path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("checksum", ["", "a" * 63, "a" * 65, "g" * 64])
def test_checksum_must_be_exactly_64_hexadecimal_characters(checksum: str) -> None:
    with pytest.raises(HubDownloadError, match="64-character hexadecimal"):
        HubPackageDownloader._verify_checksum(b"archive", checksum)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,error",
    [
        (_zip_bytes({"../escape.txt": b"bad"}), "非法路径"),
        (_zip_bytes({}, symlink="link"), "链接文件"),
    ],
)
async def test_download_and_extract_rejects_unsafe_members(
    tmp_path: Path, body: bytes, error: str
) -> None:
    async def fetch_bytes(_url: str) -> bytes:
        return body

    downloader = HubPackageDownloader(
        fetch_bytes=fetch_bytes,
        allowed_download_hosts=("downloads.example.test",),
    )
    with pytest.raises(HubDownloadError, match=error):
        await downloader.download_and_extract(
            _artifact(hashlib.sha256(body).hexdigest()), tmp_path
        )


@pytest.mark.asyncio
async def test_download_and_extract_rejects_download_host_before_fetch(
    tmp_path: Path,
) -> None:
    fetched = False

    async def fetch_bytes(_url: str) -> bytes:
        nonlocal fetched
        fetched = True
        return b""

    downloader = HubPackageDownloader(
        fetch_bytes=fetch_bytes,
        allowed_download_hosts=("allowed.example.test",),
    )
    with pytest.raises(HubDownloadError, match="白名单"):
        await downloader.download_and_extract(_artifact(""), tmp_path)

    assert fetched is False


def test_plain_http_is_allowed_only_for_loopback_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_SKILLS_HUB_ALLOW_INSECURE_HTTP", raising=False)
    production = HubPackageDownloader(
        allowed_download_hosts=("downloads.example.test",)
    )
    with pytest.raises(HubDownloadError, match="HTTPS"):
        production.assert_download_url_allowed(
            "http://downloads.example.test/package.zip"
        )

    local = HubPackageDownloader(allowed_download_hosts=("localhost",))
    local.assert_download_url_allowed("http://localhost:8080/package.zip")


def test_plain_http_download_allows_explicitly_opted_in_whitelisted_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_SKILLS_HUB_ALLOW_INSECURE_HTTP", "true")
    downloader = HubPackageDownloader(allowed_download_hosts=("119.8.233.112",))

    downloader.assert_download_url_allowed("http://119.8.233.112:8080/package.zip")


@pytest.mark.asyncio
async def test_streaming_download_aborts_when_size_limit_is_exceeded() -> None:
    response = FakeStreamingResponse(
        [b"a" * (30 * 1024 * 1024), b"b" * (21 * 1024 * 1024)]
    )

    with pytest.raises(HubDownloadError, match="大小超过限制"):
        await HubPackageDownloader._read_bounded_response(response)
