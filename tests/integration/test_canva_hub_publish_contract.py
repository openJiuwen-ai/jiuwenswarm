# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline contract check against an optional checkout of the real Hub validator."""

from email.parser import BytesParser
from email.policy import default
import io
import os
from pathlib import Path
import zipfile

import httpx
import pytest
import yaml

from jiuwenswarm.server.runtime.marketplace.asset_package_builder import build_asset_package
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity
from jiuwenswarm.server.runtime.marketplace.hub_publish_client import HubPublishClient, PublishAuth, PublishRequest
from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
from tests.unit_tests.agentserver.mcp.manifest_helpers import write_canva_oauth_package


@pytest.fixture
def hub_contract(monkeypatch):
    source = os.getenv("SKILLHUB_SOURCE_DIR")
    if not source:
        pytest.skip("Set SKILLHUB_SOURCE_DIR to run the real Hub validator offline")
    market = Path(source) / "marketplace"
    assert (market / "plugins_market/validation/types/agent_mcp.py").is_file()
    monkeypatch.syspath_prepend(str(market))
    from plugins_market.core.errors import PublishError
    from plugins_market.validation import extract_plugin_metadata

    return extract_plugin_metadata, PublishError


def _market_wrapper(content):
    """Supply the standard market envelope around the uploaded native payload."""
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(output, "w") as target:
        target.writestr("canva/plugin.yaml", yaml.safe_dump({
            "name": "canva",
            "version": "1.0.0",
            "display_name": "Canva",
            "description": "Canva host OAuth connector",
            "runtime": {"type": "agent-mcp"},
            "metadata": {"author": "mock-author", "tags": []},
        }))
        for entry in source.infolist():
            if not entry.is_dir():
                target.writestr("canva/canva/" + entry.filename, source.read(entry))
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_header", [False, True])
async def test_canva_upload_matches_real_hub_contract_offline(tmp_path, monkeypatch, hub_contract, legacy_header):
    validate, publish_error = hub_contract
    installed = write_canva_oauth_package(tmp_path / "installed" / "canva", legacy_header=legacy_header)
    originals = {p.relative_to(installed): p.read_bytes() for p in installed.rglob("*") if p.is_file()}

    if legacy_header:
        old_upload = io.BytesIO()
        with zipfile.ZipFile(old_upload, "w") as archive:
            for relative, content in originals.items():
                archive.writestr(relative.as_posix(), content)
        with pytest.raises(publish_error, match="OAUTH_ACCESS_TOKEN") as error:
            validate(_market_wrapper(old_upload.getvalue()))
        assert error.value.error == "invalid_agent_mcp"

    def reject_credential_lookup(*args, **kwargs):
        raise AssertionError("Publishing must not read user credentials")

    monkeypatch.setattr(CredentialStore, "get_all", reject_credential_lookup)
    identity = PublishIdentity("mcp", "canva", "1.0.0")
    package = build_asset_package(installed, identity, {}, tmp_path / "prepared")
    calls = []

    async def handler(request):
        body = await request.aread()
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + body
        )
        parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                 for part in message.iter_parts()}
        metadata = validate(_market_wrapper(parts["file"]))
        calls.append(metadata)
        assert metadata["plugin_type"] == "agent-mcp"
        assert metadata["integration_type"] == "remote-mcp"
        return httpx.Response(200, json={"code": 200, "data": {
            "asset_id": "mock-canva-asset",
            "plugin_type": metadata["plugin_type"],
            "name": metadata["name"],
            "version": metadata["version"],
            "publish_result": "pending_moderation",
            "visibility": "public",
        }})

    client = HubPublishClient(base_url="https://hub.example.invalid", transport=httpx.MockTransport(handler))
    result = await client.publish(
        PublishRequest(identity, package.artifact_path, package.artifact_sha256),
        auth=PublishAuth("mock-hub-token", "gitcode"),
    )
    assert len(calls) == 1
    assert result.publish_result == "pending_moderation"
    assert originals == {p.relative_to(installed): p.read_bytes() for p in installed.rglob("*") if p.is_file()}
