# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise the Gmail source package through publishing and runtime discovery."""

import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import zipfile

import httpx
import pytest
from jiuwenswarm.common.mcp_package_manifest import load_mcp_package
from jiuwenswarm.server.runtime.marketplace.asset_package_builder import build_asset_package
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity
from jiuwenswarm.server.runtime.mcp import credential, gmail_oauth, oauth_flow, registry, skill_installer, state_store


SOURCE = Path(__file__).resolve().parents[4] / "connectors" / "gmail"


def test_gmail_publish_roundtrip_preserves_launch_config_and_user_guide(tmp_path):
    source = load_mcp_package(SOURCE)
    built = build_asset_package(
        source=SOURCE,
        identity=PublishIdentity("mcp", "gmail", source.raw["version"]),
        metadata={"description": source.description},
        output_dir=tmp_path / "artifacts",
    )
    installed = tmp_path / "installed" / "gmail"
    with zipfile.ZipFile(built.artifact_path) as archive:
        files = set(archive.namelist())
        assert {"manifest.json", "mcp.json", "token-schema.json", "icon.png"} <= files
        assert "skills/gmail-guide/SKILL.md" in files
        assert not any(Path(name).name in {"credentials.json", "gcp-oauth.keys.json"} for name in files)
        archive.extractall(installed)

    loaded = load_mcp_package(installed, expected_id="gmail")
    assert loaded.server_config == source.server_config
    assert loaded.integration_type == "stdio-mcp"
    assert loaded.credentials_type == "token"
    assert loaded.icon_file.read_bytes() == (SOURCE / "icon.png").read_bytes()
    assert (loaded.skill_dirs[0] / "SKILL.md").read_bytes() == (source.skill_dirs[0] / "SKILL.md").read_bytes()


def test_gmail_installed_package_prompts_for_local_client(tmp_path, monkeypatch):
    monkeypatch.delenv("GMAIL_OAUTH_PATH", raising=False)
    monkeypatch.delenv("GMAIL_CREDENTIALS_PATH", raising=False)
    installed = tmp_path / "mcp" / "mcp_hub" / "gmail"
    shutil.copytree(SOURCE, installed)
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(skill_installer, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(gmail_oauth, "default_client_path", lambda: tmp_path / "gcp-oauth.keys.json")

    card = next(item for item in registry.list_marketplace_mcps("builtin") if item["name"] == "gmail")
    entry = registry.build_config_entry("gmail")

    assert card["source"] == "hub"
    assert card["integration_type"] == "stdio-mcp"
    assert card["icon"].startswith("/file-api/download?token=")
    assert entry["transport"] == "stdio"
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "@artymclabin/gmail-mcp@1.2.4"]
    assert entry["timeout_s"] == 120
    assert credential.detect_credential_kind("gmail") == "token"
    prompt = registry.connect_mcp("gmail")
    assert prompt["credentials_required"] is True
    assert "placeholder" not in prompt["fields"]["GMAIL_OAUTH_PATH"]
    assert "placeholder" not in prompt["fields"]["GMAIL_PROXY_PORT"]
    assert prompt["required_tokens"] == ["GMAIL_OAUTH_PATH"]
    assert prompt["optional_values"] == {"GMAIL_PROXY_PORT": ""}
    assert "GMAIL_PROXY_PORT" in prompt["fields"]
    skills = registry.get_mcp_skills("gmail")
    assert len(skills) == 1
    assert "search_emails" in skills[0]["description"]
    detail = registry.get_mcp("gmail")
    assert detail["show_skills"] is False
    assert detail["skills"] == skills
    result = skill_installer.install_mcp_skills("gmail")
    assert len(result["installed"]) == 1
    guide = tmp_path / "mcp" / "skills" / "gmail" / result["installed"][0] / "SKILL.md"
    assert guide.read_bytes() == (SOURCE / "skills" / "gmail-guide" / "SKILL.md").read_bytes()


def test_gmail_first_google_callback_and_direct_reconnect(tmp_path, monkeypatch):
    installed = tmp_path / "mcp" / "mcp_hub" / "gmail"
    shutil.copytree(SOURCE, installed)
    monkeypatch.setattr(registry, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(credential, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(skill_installer, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(state_store, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(gmail_oauth, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(gmail_oauth, "default_client_path", lambda: tmp_path / "default.json")
    client_path = tmp_path / "downloaded.json"
    client_path.write_text(json.dumps({"installed": {
        "client_id": "test.apps.googleusercontent.com", "client_secret": "mock-secret",
    }}), encoding="utf-8")
    registry.save_mcp_credentials("gmail", {"GMAIL_OAUTH_PATH": str(client_path)})
    registry.save_mcp_credentials("gmail", {"GMAIL_PROXY_PORT": "7897"})
    monkeypatch.setattr(gmail_oauth, "direct_reachable", lambda _url: False)
    assert registry.connect_mcp("gmail")["host_oauth"] is True

    def token_response(request):
        assert str(request.url) == gmail_oauth.TOKEN_URL
        data = parse_qs(request.content.decode())
        assert data["code_verifier"] and data["client_secret"] == ["mock-secret"]
        return httpx.Response(200, json={
            "access_token": "test-access", "refresh_token": "test-refresh",
            "expires_in": 3600, "scope": " ".join(gmail_oauth.SCOPES),
        })

    def google_client():
        return httpx.Client(transport=httpx.MockTransport(token_response))

    agent = oauth_flow.OAuthFlowManager(workspace_dir=tmp_path, http_client=google_client)
    gateway = oauth_flow.OAuthFlowManager(workspace_dir=tmp_path, http_client=google_client)
    def proxied_client(*, proxy, trust_env):
        assert proxy == "http://127.0.0.1:7897"
        assert trust_env is True
        return google_client()

    monkeypatch.setattr(gateway, "_client", proxied_client)
    url = urlsplit(agent.start("gmail", "http://127.0.0.1:19000")["auth_url"])
    query = parse_qs(url.query)
    assert url.hostname == "accounts.google.com"
    assert query["code_challenge_method"] == ["S256"]
    assert not agent.gmail_authorization_ready("gmail")
    assert gateway.complete({"state": query["state"][0], "code": "mock-code"})
    assert agent.gmail_authorization_ready("gmail")
    client, tokens = gmail_oauth.paths("gmail")
    assert client == client_path
    assert gmail_oauth.read_tokens(tokens)["refresh_token"] == "test-refresh"

    def unreachable(*args, **kwargs):
        raise httpx.ConnectTimeout("test timeout")

    monkeypatch.setattr(gmail_oauth.httpx, "post", unreachable)
    retry = registry.connect_mcp("gmail")
    assert retry["credentials_required"] and retry["gmail_network_retry"]
    assert retry["required_tokens"] == []
    assert retry["optional_values"] == {"GMAIL_PROXY_PORT": "7897"}
    assert state_store.get_mcp_record("gmail") is None
    assert gmail_oauth.read_tokens(tokens)["refresh_token"] == "test-refresh"

    monkeypatch.setattr(gmail_oauth, "direct_reachable", lambda url: url == gmail_oauth.GMAIL_URL)
    completed = registry.complete_host_oauth_auth("gmail")
    assert completed["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert "gmail.googleapis.com" in completed["env"]["NO_PROXY"].split(",")
    assert "oauth2.googleapis.com" not in completed["env"]["NO_PROXY"].split(",")
    monkeypatch.setattr(gmail_oauth, "direct_reachable", lambda _url: False)

    def refresh(url, *, timeout, data, proxy):
        assert url == gmail_oauth.TOKEN_URL
        assert proxy == "http://127.0.0.1:7897"
        assert data["refresh_token"] == "test-refresh"
        return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 3600})

    monkeypatch.setattr(gmail_oauth.httpx, "post", refresh)
    entry = registry.connect_mcp("gmail")
    assert not entry.get("auth_required")
    assert entry["env"]["GMAIL_CREDENTIALS_PATH"] == str(tokens)
    assert entry["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert entry["env"]["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert entry["env"]["NO_PROXY"] == "localhost,127.0.0.1"
    assert gmail_oauth.read_tokens(tokens)["access_token"] == "refreshed"
    from jiuwenswarm.common.mcp_config import build_mcp_server_config

    fallback_env = build_mcp_server_config(entry).params["env"]
    assert "gmail.googleapis.com" not in fallback_env["NO_PROXY"]
    assert fallback_env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    def unexpected_probe(_url):
        pytest.fail("Building an MCP configuration must not probe the network")

    with monkeypatch.context() as context:
        context.setattr(gmail_oauth, "direct_reachable", unexpected_probe)
        assert build_mcp_server_config(entry).params["env"] == fallback_env

    monkeypatch.setattr(gmail_oauth, "direct_reachable", lambda _url: True)
    def refresh_direct(url, *, timeout, data, trust_env):
        assert url == gmail_oauth.TOKEN_URL and trust_env is False
        return httpx.Response(200, json={"access_token": "direct-access", "expires_in": 3600})

    monkeypatch.setattr(gmail_oauth.httpx, "post", refresh_direct)
    entry = registry.connect_mcp("gmail")
    assert not entry.get("auth_required")
    with monkeypatch.context() as context:
        context.setattr(gmail_oauth, "direct_reachable", unexpected_probe)
        direct_env = build_mcp_server_config(entry).params["env"]
    assert {"gmail.googleapis.com", "oauth2.googleapis.com"} <= set(direct_env["NO_PROXY"].split(","))
    assert "HTTPS_PROXY" not in direct_env
    assert gmail_oauth.read_tokens(tokens)["access_token"] == "direct-access"
    state_store.set_mcp_state("gmail", state="connected")
    with pytest.raises(ValueError, match="between 1 and 65535"):
        registry.save_mcp_credentials("gmail", {"GMAIL_PROXY_PORT": "65536"})
    assert gmail_oauth.configured_proxy_port("gmail") == "7897"
    monkeypatch.setattr(gmail_oauth, "direct_reachable", lambda _url: False)
    updated = registry.save_mcp_credentials("gmail", {"GMAIL_PROXY_PORT": "7897", "unused": None})
    assert updated["saved_keys"] == ["GMAIL_PROXY_PORT"]
    assert state_store.get_mcp_record("gmail")["env"]["HTTPS_PROXY"].endswith(":7897")
    registry.save_mcp_credentials("gmail", {"GMAIL_PROXY_PORT": ""})
    assert "HTTPS_PROXY" not in state_store.get_mcp_record("gmail")["env"]
    assert gmail_oauth.oauth_proxy("") == (None, False)
    assert gmail_oauth.paths("gmail") == (client, tokens)
    registry.disconnect_mcp("gmail")
    assert state_store.get_mcp_record("gmail") is None
    assert gmail_oauth.configured_proxy_port("gmail") == ""
    assert gmail_oauth.read_tokens(tokens)["access_token"] == "direct-access"


def test_gmail_tool_translations_are_display_metadata_only(monkeypatch):
    from openjiuwen.core.runner import Runner

    names = ["search_emails", "send_email", "read_email", "future_tool"]
    cards = {name: SimpleNamespace(name=name, description="Original server description") for name in names}
    manager = SimpleNamespace(
        get_mcp_server_ids=lambda name: ["server"],
        get_mcp_tool_ids=lambda server: names,
        _tools={name: SimpleNamespace(card=card) for name, card in cards.items()},
    )
    monkeypatch.setattr(Runner.resource_mgr, "_resource_registry", SimpleNamespace(tool=lambda: manager))
    package = load_mcp_package(SOURCE)
    monkeypatch.setattr(registry, "_resolve_package", lambda name: package)

    tools = {tool["name"]: tool for tool in registry.get_mcp_tools("gmail")}

    assert "搜索邮件" in tools["search_emails"]["description_i18n"]["zh"]
    assert "发送邮件" in tools["send_email"]["description_i18n"]["zh"]
    assert "读取指定邮件" in tools["read_email"]["description_i18n"]["zh"]
    assert tools["future_tool"] == {"name": "future_tool", "description": "Original server description"}
    assert all(card.description == "Original server description" for card in cards.values())

    monkeypatch.setattr(registry, "_resolve_package", lambda name: SimpleNamespace(raw={"presentation": "invalid"}))
    assert all("description_i18n" not in tool for tool in registry.get_mcp_tools("another-package"))
