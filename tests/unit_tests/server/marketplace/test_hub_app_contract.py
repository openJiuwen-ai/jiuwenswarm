# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import io
import zipfile
from typing import Any

import pytest

from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetQuery,
    HubDownloadRequest,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_type_adapter import (
    get_hub_asset_type_contract,
    resolve_hub_asset_kind,
)
from jiuwenswarm.server.runtime.marketplace.hub_client import HubClient
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import (
    HubDownloadError,
    HubPackageDownloader,
)


class RecordingTransport:
    base_url = "https://hub.example.test"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, Any] | None]] = []

    async def get_data(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append((path, params))
        return self.responses.pop(0)


def _catalog(public_latest: str = "2.0.0") -> dict[str, Any]:
    return {
        "items": [
            {
                "asset_id": "app-1",
                "asset_type": "app",
                "plugin_type": "app",
                "name": "gomoku",
                "display_name": "Gomoku",
                "short_desc": "play",
                "latest_version": public_latest,
                "public_latest_version": public_latest,
                "icon_uri": "",
                "tags": ["game"],
                "install_count": 1,
            }
        ],
        "total": 1,
        "page": 1,
        "page_size": 20,
    }


def _detail(version: str) -> dict[str, Any]:
    return {
        "asset_id": "app-1",
        "asset_type": "app",
        "plugin_type": "app",
        "version": version,
        "name": "gomoku",
        "display_name": "Gomoku",
        "short_desc": "play",
        "file_size": 1234,
        "source_size": 1000,
        "requires_project": True,
        "permissions": ["ai"],
        "has_server_script": True,
        "checksum_sha256": "b" * 64,
    }


def _artifact() -> dict[str, Any]:
    return {
        "asset_id": "app-1",
        "asset_type": "app",
        "plugin_type": "app",
        "version": "1.0.0",
        "download_url": "https://downloads.example.test/gomoku.zip",
        "checksum_sha256": "a" * 64,
        "name": "gomoku",
        "file_size": 1234,
    }


def test_app_kind_contract() -> None:
    assert resolve_hub_asset_kind("app", "app") == "app"
    contract = get_hub_asset_type_contract("app")
    assert contract.hub_plugin_type == "app"
    assert contract.local_package_type == "app"


def test_query_asset_honours_explicit_version() -> None:
    transport = RecordingTransport([_catalog("2.0.0"), _detail("1.0.0")])
    client = HubClient(transport=transport)

    detail = asyncio.run(
        client.query_asset(HubAssetQuery(kind="app", asset_id="app-1", version="1.0.0"))
    )

    assert detail.version == "1.0.0"
    assert detail.file_size == 1234
    assert detail.source_size == 1000
    assert detail.requires_project is True
    assert detail.permissions == ("ai",)
    assert detail.has_server_script is True
    assert detail.checksum_sha256 == "b" * 64
    assert transport.requests[-1][0].endswith("/versions/1.0.0")


def test_resolve_download_for_app_requests_original_zip() -> None:
    transport = RecordingTransport([_artifact()])
    client = HubClient(transport=transport)

    resolved = asyncio.run(
        client.resolve_download(
            HubDownloadRequest(kind="app", asset_id="app-1", version="1.0.0")
        )
    )

    assert resolved.file_size == 1234
    assert resolved.package_name == "gomoku"
    assert transport.requests[0][1]["is_cli_download"] is True


def test_resolve_download_for_app_requires_file_size() -> None:
    payload = _artifact()
    payload.pop("file_size")
    client = HubClient(transport=RecordingTransport([payload]))
    with pytest.raises(Exception):
        asyncio.run(
            client.resolve_download(
                HubDownloadRequest(kind="app", asset_id="app-1", version="1.0.0")
            )
        )


def _zip_bytes(size: int = 4) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("a.txt", b"x" * size)
    return stream.getvalue()


def test_downloader_custom_uncompressed_limit() -> None:
    body = _zip_bytes(64)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        with pytest.raises(HubDownloadError):
            HubPackageDownloader.validated_members(
                archive, max_total_uncompressed_bytes=10
            )


def test_downloader_custom_download_limit() -> None:
    body = _zip_bytes()

    async def fetch(_url: str) -> bytes:
        return body

    downloader = HubPackageDownloader(
        fetch_bytes=fetch,
        allowed_download_hosts=("downloads.example.test",),
        max_download_bytes=2,
    )
    with pytest.raises(HubDownloadError):
        asyncio.run(
            downloader.download_bytes(
                type(
                    "Artifact",
                    (),
                    {
                        "download_url": "https://downloads.example.test/a.zip",
                        "checksum_sha256": "",
                    },
                )()
            )
        )
