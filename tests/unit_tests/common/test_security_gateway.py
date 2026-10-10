# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Security gateway: SSH loopback default, net_guard config plumbing, sandbox network copy."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import (
    normalize_net_guard_patch,
)
from jiuwenswarm.common import config_split
from jiuwenswarm.extensions.agentos.agentos_router.config import load_ssh_channel_endpoint
from jiuwenswarm.gateway.channel_manager.protocol.ssh.config import proxy_config_from_dict


# --------------------------------------------------------------------------- #
# SSH channel
# --------------------------------------------------------------------------- #
def test_ssh_defaults_to_loopback(tmp_path):
    host_key = str(tmp_path / "host_key")
    assert proxy_config_from_dict({"host_key_path": host_key}).listen_host == "127.0.0.1"
    assert proxy_config_from_dict({"host_key_path": host_key, "listen_host": ""}).listen_host == "127.0.0.1"


def test_router_reports_advertise_host():
    base = {"enabled": True, "listen_host": "127.0.0.1", "listen_port": 2222}
    ep = load_ssh_channel_endpoint({"channels": {"ssh": {**base, "advertise_host": "10.1.2.3"}}})
    assert (ep.ip, ep.port) == ("10.1.2.3", 2222)
    ep = load_ssh_channel_endpoint({"channels": {"ssh": base}})
    assert ep.ip == "127.0.0.1"


# --------------------------------------------------------------------------- #
# net_guard config plumbing
# --------------------------------------------------------------------------- #
def test_config_split_keeps_user_net_guard_urls():
    package = {"permissions": {"net_guard": {"urls": {"*.pkg.example": "deny"}}}}
    user = {
        "permissions": {
            "net_guard": {"urls": {"*.pkg.example": "deny", "*.corp.example": "allow"}}
        }
    }
    keep = config_split.extract_user_keep_from_legacy(user, package)
    assert keep["permissions"]["net_guard"]["urls"] == {"*.corp.example": "allow"}

    merged = config_split.upsert_map({"a": "deny", "b": "allow"}, {"b": "deny", "c": "allow"})
    assert merged == {"a": "deny", "b": "deny", "c": "allow"}


def test_restore_user_keep_merges_net_guard_urls(tmp_path):
    user_yaml = tmp_path / "config.yaml"
    user_yaml.write_text(
        yaml.safe_dump({"permissions": {"net_guard": {"urls": {"*.pkg.example": "deny"}}}}),
        encoding="utf-8",
    )
    config_split.restore_user_keep_set(
        user_yaml, {"permissions": {"net_guard": {"urls": {"*.corp.example": "allow"}}}}
    )
    urls = yaml.safe_load(user_yaml.read_text(encoding="utf-8"))["permissions"]["net_guard"]["urls"]
    assert urls == {"*.pkg.example": "deny", "*.corp.example": "allow"}


def test_normalize_net_guard_patch_validates():
    out = normalize_net_guard_patch(
        {"enabled": True, "defaults": "DENY", "urls": {" *.corp.example ": "Allow"}, "tools": ["t1"]}
    )
    assert out == {"enabled": True, "defaults": "deny", "urls": {"*.corp.example": "allow"}}
    for bad in (
        {"urls": {"169.254.169.254": "allow"}},
        {"enabled": "yes"},
        {"defaults": "invalid"},
        {"urls": {"x": "invalid"}},
        {"urls": ["x"]},
    ):
        with pytest.raises(ValueError):
            normalize_net_guard_patch(bad)
    assert normalize_net_guard_patch({"defaults": "ask", "urls": {"example.com": "ask"}}) == {
        "defaults": "ask", "urls": {"example.com": "ask"},
    }
    with pytest.raises(ValueError):
        normalize_net_guard_patch({"urls": {"169.254.169.254": "ask"}})


def test_net_guard_rpc_get_and_set(monkeypatch):
    from openjiuwen.harness.security.outbound import policy as host_exit

    from jiuwenswarm.agents.harness.common.rails.permissions import (
        permissions_config_rpc as rpc,
        permissions_persist,
    )
    from jiuwenswarm.common import config as config_mod
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod

    store = {"permissions": {"net_guard": {"enabled": True, "defaults": "allow", "urls": {}}}}

    def _persist(patch):
        normalized = normalize_net_guard_patch(patch)
        store["permissions"]["net_guard"].update(normalized)
        return dict(store["permissions"]["net_guard"])

    monkeypatch.setattr(config_mod, "get_config", lambda: store)
    monkeypatch.setattr(permissions_persist, "persist_net_guard_section", _persist)
    host_exit.reset_host_exit_policy()
    try:
        assert ReqMethod.PERMISSIONS_NET_GUARD_GET in rpc.get_permissions_read_only_req_methods()
        assert ReqMethod.PERMISSIONS_NET_GUARD_SET not in rpc.get_permissions_read_only_req_methods()

        resp = rpc.dispatch_permissions_config_request(
            AgentRequest(request_id="r1", req_method=ReqMethod.PERMISSIONS_NET_GUARD_GET)
        )
        assert resp.ok and resp.payload["apply_mode"] == "hot_reload"
        assert resp.payload["net_guard"]["enforce_host_exit"] is True

        resp = rpc.dispatch_permissions_config_request(
            AgentRequest(
                request_id="r2",
                req_method=ReqMethod.PERMISSIONS_NET_GUARD_SET,
                params={"net_guard": {"urls": {"*.evil.example": "deny"}}},
            )
        )
        assert resp.ok, resp.payload
        assert resp.payload["net_guard"]["urls"] == {"*.evil.example": "deny"}
        assert resp.payload["host_exit"]["mode"] == host_exit.STATE_ACTIVE
        with pytest.raises(host_exit.OutboundBlockedError):
            host_exit.check_outbound_url("https://a.evil.example/")

        resp = rpc.dispatch_permissions_config_request(
            AgentRequest(
                request_id="r3",
                req_method=ReqMethod.PERMISSIONS_NET_GUARD_SET,
                params={"defaults": "ask"},
            )
        )
        assert resp.ok and resp.payload["net_guard"]["defaults"] == "ask"
        # ASK belongs to pre-tool approval; the host exit continues enforcing DENY.
        host_exit.check_outbound_url("https://example.com/")
        with pytest.raises(host_exit.OutboundBlockedError):
            host_exit.check_outbound_url("https://a.evil.example/")
    finally:
        host_exit.reset_host_exit_policy()


def test_publish_host_exit_policy_from_config():
    from openjiuwen.harness.security.outbound import policy as host_exit

    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
        publish_host_exit_policy_from_config,
    )

    host_exit.reset_host_exit_policy()
    try:
        publish_host_exit_policy_from_config(
            {"permissions": {"net_guard": {"enabled": True, "urls": {"*.evil.example": "deny"}}}}
        )
        with pytest.raises(host_exit.OutboundBlockedError):
            host_exit.check_outbound_url("https://a.evil.example/")
        publish_host_exit_policy_from_config({"permissions": {"tools": {}}})
        assert host_exit.get_host_exit_state().mode == host_exit.STATE_ACTIVE
    finally:
        host_exit.reset_host_exit_policy()


@pytest.mark.asyncio
async def test_reload_continues_when_host_exit_publish_fails(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_config_rpc
    from jiuwenswarm.server.runtime import agent_manager as agent_manager_module

    class FakeAgent:
        def __init__(self) -> None:
            self.reload_calls = 0

        async def reload_agent_config(self, *args, **kwargs):
            self.reload_calls += 1

    def boom(_config=None):
        raise RuntimeError("publish broken")

    monkeypatch.setattr(permissions_config_rpc, "publish_host_exit_policy_from_config", boom)
    monkeypatch.setattr(agent_manager_module, "get_team_manager", lambda _cid: None)
    manager = agent_manager_module.AgentManager()
    agent = FakeAgent()
    manager.agents = {"web": {"agent": agent}}

    await manager.reload_agents_config({"permissions": {}}, {}, target_channel_id="web")
    assert agent.reload_calls == 1


def test_mcp_endpoint_check_error_skips_only_that_server():
    from openjiuwen.harness.security.outbound import policy as host_exit

    from jiuwenswarm.common.mcp_config import build_enabled_mcp_server_configs, mcp_endpoint_blocked_reason

    host_exit.publish_host_exit_policy({"net_guard": {"enabled": True, "enforce_host_exit": True}})
    try:
        assert mcp_endpoint_blocked_reason("http://[::1/mcp")
        configs = build_enabled_mcp_server_configs(
            {
                "mcp": {
                    "servers": [
                        {"name": "bad", "transport": "sse", "url": "http://[::1/mcp"},
                        {"name": "good", "transport": "sse", "url": "https://mcp.example.com/sse"},
                    ]
                }
            }
        )
        assert [c.server_name for c in configs] == ["good"]
    finally:
        host_exit.reset_host_exit_policy()


# --------------------------------------------------------------------------- #
# Sandbox network runtime copy (Linux)
# --------------------------------------------------------------------------- #
@pytest.fixture
def linux_render(tmp_path, monkeypatch):
    from jiuwenswarm.server import sandbox_policy_render as render
    from jiuwenswarm.common import config
    from jiuwenswarm.server import security_lists_rpc

    path = tmp_path / "config.yaml"
    path.write_text("sandbox:\n  type: jiuwenbox\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(security_lists_rpc, "_publish_enforcement", lambda: "published")
    monkeypatch.setattr(render, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(render, "_is_windows", lambda: False)
    return render


def test_linux_network_copy_roundtrip(linux_render, tmp_path):
    render = linux_render
    view = render.set_sandbox_network_config(False, ["example.com"], ["evil.example"])
    assert view == {"disable_all": False, "allow_domains": ["example.com"], "deny_domains": ["evil.example"]}
    assert render.get_sandbox_network_config() == {
        "disable_all": False, "allow_domains": ["example.com", "*.example.com"],
        "deny_domains": ["evil.example", "*.evil.example"],
    }

    stored = yaml.safe_load((tmp_path / "default-policy.runtime.yaml").read_text(encoding="utf-8"))
    assert stored == {
        "network": {"egress": {"allowed_domains": ["example.com", "*.example.com"],
                                "blocked_domains": ["evil.example", "*.evil.example"]}}
    }


def test_linux_rejects_disable_all(linux_render):
    with pytest.raises(ValueError):
        linux_render.set_sandbox_network_config(True, [], [])


# --------------------------------------------------------------------------- #
# Lint guard: host outbound tools must go through the host exit
# --------------------------------------------------------------------------- #
_TOOLS_DIR = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "agents" / "harness" / "common" / "tools"
_RAW_HTTP = re.compile(
    r"\b(requests|httpx)\.(get|post|put|patch|delete|head|request|Session|Client|AsyncClient)\b"
    r"|aiohttp\.ClientSession\("
    r"|\burlopen\("
)
# Pre-existing modules outside net_guard's tool scope (media / device upload helpers).
_RAW_HTTP_ALLOWLIST = frozenset({
    "audio_tools.py",
    "image_tools.py",
    "send_html_card.py",
    "ssl_config.py",
    "video_tools.py",
    "xiaoyi_phone_tools/file_tools.py",
    "xiaoyi_phone_tools/file_upload_helpers.py",
    "xiaoyi_phone_tools/image_reading_tool.py",
    "xiaoyi_phone_tools/save_tools.py",
    "xiaoyi_phone_tools/xiaoyi_collection_tool.py",
})


def test_host_tools_do_not_use_raw_http_clients():
    offenders = []
    for path in _TOOLS_DIR.rglob("*.py"):
        rel = path.relative_to(_TOOLS_DIR).as_posix()
        if rel.startswith("browser-move/") or rel in _RAW_HTTP_ALLOWLIST:
            continue
        if _RAW_HTTP.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert offenders == [], (
        "use openjiuwen.harness.security.outbound (sync_client / async_client) "
        f"instead of raw HTTP clients: {offenders}"
    )
