from __future__ import annotations

import json

import pytest

from jiuwenswarm.common.mcp_config import build_mcp_server_config
from jiuwenswarm.server.runtime.mcp import credential, oauth_flow, registry, state_store
from jiuwenswarm.server.runtime.mcp.credential import extract_placeholders
from jiuwenswarm.server.runtime.mcp.package_manifest import load_mcp_package
from jiuwenswarm.server.runtime.mcp.registry import _normalize_transport
from tests.unit_tests.agentserver.mcp.manifest_helpers import write_canva_oauth_package


@pytest.fixture
def package_dir(tmp_path):
    return write_canva_oauth_package(tmp_path / "package" / "canva")


def test_package_contract(package_dir) -> None:
    package = load_mcp_package(package_dir)

    assert package.package_id == "canva"
    assert package.version == "1.0.0"
    assert package.integration_type == "remote-mcp"
    assert package.credentials_type == "oauth"
    assert package.server_name == "canva"
    assert package.icon_file is not None and package.icon_file.name == "icon.png"
    assert [path.name for path in package.skill_dirs] == ["canva-guide"]


def test_mcp_json_uses_direct_remote_mcp(package_dir) -> None:
    package = load_mcp_package(package_dir)
    assert package.server_config is not None

    config = package.server_config
    assert _normalize_transport(config.get("type", ""), config) == "streamable-http"
    assert config["url"] == "https://mcp.canva.cn/mcp"
    assert "headers" not in config
    assert extract_placeholders(config) == set()


@pytest.mark.parametrize("legacy_header", [False, True])
def test_host_supplies_oauth_header_without_persisting_a_token(tmp_path, monkeypatch, legacy_header):
    installed = write_canva_oauth_package(
        tmp_path / "mcp" / "mcp_hub" / "canva", legacy_header=legacy_header
    )
    original = (installed / "mcp.json").read_bytes()
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(oauth_flow, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(state_store, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(registry, "_install_bundled_skills_safe", lambda name: [])

    assert registry.connect_mcp("canva")["host_oauth"] is True
    store = credential.CredentialStore(workspace_dir=tmp_path)
    store.save_token("canva", "OAUTH_ACCESS_TOKEN", "synthetic-canva-token")
    store.save_token("canva", "OAUTH_CLIENT_ID", "synthetic-client")
    entry = registry.connect_mcp("canva")
    assert entry["headers"] == {"Authorization": "Bearer ${OAUTH_ACCESS_TOKEN}"}
    assert registry.complete_host_oauth_auth("canva")["headers"] == entry["headers"]

    runtime = build_mcp_server_config(
        entry, credential_resolver=lambda key: store.get_token("canva", key)
    )
    assert runtime.auth_headers == {"Authorization": "Bearer synthetic-canva-token"}
    record = state_store.get_mcp_record("canva")
    assert record["headers"] == entry["headers"]
    assert "synthetic-canva-token" not in json.dumps(record)
    assert (installed / "mcp.json").read_bytes() == original


@pytest.mark.parametrize(
    "credentials_type,headers,expected",
    [
        ("oauth", {"X-Canva-Client": "desktop"},
         {"X-Canva-Client": "desktop", "Authorization": "Bearer ${OAUTH_ACCESS_TOKEN}"}),
        ("oauth", {"authorization": "Bearer ${OAUTH_ACCESS_TOKEN}"},
         {"authorization": "Bearer ${OAUTH_ACCESS_TOKEN}"}),
        (None, {"X-Canva-Client": "desktop"}, {"X-Canva-Client": "desktop"}),
    ],
)
def test_oauth_header_is_scoped_to_declared_oauth_packages(
    tmp_path, monkeypatch, credentials_type, headers, expected
):
    installed = write_canva_oauth_package(tmp_path / "mcp" / "mcp_hub" / "canva")
    manifest_path = installed / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if credentials_type is None:
        manifest.pop("credentials")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    config_path = installed / "mcp.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mcpServers"]["canva"]["headers"] = headers
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)

    assert registry.build_config_entry("canva")["headers"] == expected
    assert registry.resolve_package("canva").server_config["headers"] == headers
