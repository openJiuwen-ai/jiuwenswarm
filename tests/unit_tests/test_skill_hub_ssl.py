"""Regression coverage for SwarmSkillHub API TLS configuration."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import zipfile
import ssl
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from jiuwenswarm.extensions.sdk.skill_source import (
    ArtifactDescriptor,
    ArtifactRef,
    DownloadPolicy,
    SkillRef,
    SourceConfig,
)
from jiuwenswarm.extensions.swarm_skill_hub.extension import SwarmSkillHubSourceExtension


_ROOT = Path(__file__).resolve().parents[2]
_REGISTRY = _ROOT / "jiuwenswarm/server/runtime/skill/source_registry.py"
_ERROR_NODE = next(
    node for node in ast.parse(_REGISTRY.read_text(encoding="utf-8")).body
    if isinstance(node, ast.ClassDef) and node.name == "SourceRegistryError"
)
_ERROR_NAMESPACE = {}
exec(compile(ast.Module(body=[_ERROR_NODE], type_ignores=[]), str(_REGISTRY), "exec"), _ERROR_NAMESPACE)
SourceRegistryError = _ERROR_NAMESPACE["SourceRegistryError"]


def _parse_source_config(raw):
    # Load the actual parser without importing the full AgentServer dependency graph.
    path = _ROOT / "jiuwenswarm/server/runtime/skill/skill_manager.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    manager = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "SkillManager")
    method = next(node for node in manager.body if isinstance(node, ast.FunctionDef) and node.name == "_parse_source_config")
    method.decorator_list = []
    namespace = {
        "Any": Any,
        "SourceConfig": SourceConfig,
        "DownloadPolicy": DownloadPolicy,
        "SourceRegistryError": SourceRegistryError,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_parse_source_config"](raw)


@pytest.mark.parametrize("setting", [None, True, False])
def test_source_tls_setting_reaches_hub_api_client(setting):
    raw = {
        "source_id": "swarmskillhub",
        "provider_type": "swarmskillhub",
        "endpoint_ref": "https://hub.example.com",
    }
    if setting is not None:
        raw["verify_ssl"] = setting
    provider = SwarmSkillHubSourceExtension().create_provider(_parse_source_config(raw))
    expected = True if setting is None else setting
    urls = []
    verify_values = []
    original_client = httpx.AsyncClient

    def respond(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"code": 200, "data": {"items": []}})

    def client_factory(**kwargs):
        verify_values.append(kwargs["verify"])
        # MockTransport performs no TLS; avoid loading the host OS trust store.
        kwargs["verify"] = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        kwargs["trust_env"] = False
        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    async def run():
        try:
            await provider._get_data("/api/v1/plugins")
        finally:
            await provider.close()

    with patch("httpx.AsyncClient", side_effect=client_factory):
        asyncio.run(run())
    assert verify_values == [expected]
    assert urls == ["https://hub.example.com/api/v1/plugins"]


@pytest.mark.parametrize("setting", ["false", "true", 0, 1, None])
def test_source_tls_setting_rejects_non_boolean_values(setting):
    with pytest.raises(SourceRegistryError, match="verify_ssl must be a boolean"):
        _parse_source_config({"verify_ssl": setting})


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        ("valid", None),
        ("redirect", "跳转"),
        ("checksum", "SHA256"),
        ("invalid_zip", "ZIP"),
        ("oversize", "最大大小"),
    ],
)
def test_ported_download_tls_and_integrity_behavior(scenario, error):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("SKILL.md", "# Port regression\n")
    payload = archive.getvalue()
    checksum = hashlib.sha256(payload).hexdigest()
    descriptor = ArtifactDescriptor(
        artifact_ref=ArtifactRef(SkillRef("swarmskillhub", "sample"), "v1"),
        download_url="https://download.example.com/sample.zip",
        checksum_sha256="0" * 64 if scenario == "checksum" else checksum,
    )
    policy = DownloadPolicy(max_bytes=1 if scenario == "oversize" else 1024)
    provider = SwarmSkillHubSourceExtension().create_provider(
        SourceConfig(source_id="swarmskillhub", provider_type="swarmskillhub")
    )
    requests = []
    client_settings = []
    original_client = httpx.AsyncClient

    def respond(request):
        requests.append(str(request.url))
        if scenario == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.example.com"})
        content = b"not a zip" if scenario == "invalid_zip" else payload
        return httpx.Response(200, content=content)

    def client_factory(**kwargs):
        client_settings.append((kwargs["verify"], kwargs["follow_redirects"]))
        kwargs["trust_env"] = False
        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    with patch("httpx.AsyncClient", side_effect=client_factory):
        if error is None:
            assert asyncio.run(provider.download_artifact(descriptor, policy)) == payload
        else:
            with pytest.raises(RuntimeError, match=error):
                asyncio.run(provider.download_artifact(descriptor, policy))
    assert client_settings == [(False, False)]
    assert requests == [descriptor.download_url]
