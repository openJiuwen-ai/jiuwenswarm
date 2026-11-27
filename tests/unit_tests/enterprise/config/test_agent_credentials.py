"""Agent registration credentials are independent of custom application JSON."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.mcp_dynamic_token_patch import (
    apply_dispatched_credentials,
    read_instance_credentials,
)
from jiuwenswarm.enterprise.config.effective.agent_credentials import select_agent_credentials


def _request(agent_id="agent-a", config_id="config", runtime_id="runtime-a"):
    return SimpleNamespace(metadata={"routing": {"bot_id": "resource"}, "agent_identity": {
        "agent_id": agent_id, "agent_template_id": config_id, "runtime_template_id": runtime_id,
    }})


def test_combinations_sharing_one_config_use_only_their_own_credentials():
    template = {"template_id": "config", "data": {
        "custom_config": [{"client_id": "ordinary", "client_secret": "ordinary-data", "timeout": 30}],
        "managed_agent_clients": {
            "agent-a": {"client_id": "agent-a", "client_secret": "secret-a", "runtime_template_id": "runtime-a"},
            "agent-b": {"client_id": "agent-b", "client_secret": "secret-b", "runtime_template_id": "runtime-b"},
        },
    }}
    before = deepcopy(template)
    for suffix in ("a", "b"):
        request = _request(f"agent-{suffix}", runtime_id=f"runtime-{suffix}")
        assert select_agent_credentials(template, request.metadata.get("agent_identity", {})) == {
            "client_id": f"agent-{suffix}", "client_secret": f"secret-{suffix}",
        }
    for request in (SimpleNamespace(metadata={}), _request(config_id="wrong"),
                    _request(runtime_id="runtime-b"), _request("unregistered")):
        assert select_agent_credentials(template, request.metadata.get("agent_identity", {})) is None
    assert template == before


def test_missing_template_ids_do_not_match_an_incomplete_registration():
    template = {"data": {"managed_agent_clients": {
        "agent-a": {"client_id": "agent-a", "client_secret": "test-secret"},
    }}}
    assert select_agent_credentials(template, {"agent_id": "agent-a"}) is None


@pytest.mark.parametrize("clients", [None, {}, [], "invalid", {
    "agent-a": {"client_id": "wrong", "client_secret": "s", "runtime_template_id": "runtime-a"},
}, {
    "agent-a": {"client_id": "agent-a", "client_secret": " ", "runtime_template_id": "runtime-a"},
}])
def test_custom_json_never_supplies_credentials(clients):
    template = {"template_id": "config", "data": {
        "custom_config": [{"client_id": "manual", "client_secret": "manual-secret"}],
        "managed_agent_clients": clients,
    }}
    before = deepcopy(template)
    assert select_agent_credentials(template, _request().metadata["agent_identity"]) is None
    assert template == before


@pytest.mark.asyncio
async def test_agent_id_credentials_refresh_without_process_restart(monkeypatch):
    from jiuwenswarm.enterprise.config.effective import loader

    custom = [{"client_id": "ordinary", "client_secret": "ordinary-data", "timeout": 30}]
    row = {"template_id": "config", "template_ref": {"mcp": ["mcp"]},
           "data": {"custom_config": custom}}

    async def list_records(table, *, filters):
        if table == "instance_agent_resource":
            return [{"resource_id": "resource", "ref_template_id": "config"}]
        assert table == "agent_template" and filters["template_id"] == "config"
        return [deepcopy(row)]

    async def entities(slot, template_ids):
        return [{"template_id": "mcp"}]

    monkeypatch.setattr(loader, "is_enterprise", lambda: True)
    monkeypatch.setattr(loader.db_queries, "list_records", list_records)
    monkeypatch.setattr(loader, "_fetch_slot_entities", entities)
    loader.invalidate_enterprise_config_caches()
    try:
        await loader._fetch_agent_template_row("config")
        for secret in ("secret-first", "secret-replaced", None, "secret-reregistered"):
            row["data"]["managed_agent_clients"] = {
                "agent-a": {"client_id": "agent-a", "client_secret": secret, "runtime_template_id": "runtime-a"},
            } if secret else {}
            before = deepcopy(row)
            loaded = await loader.load_effective_enterprise_config(_request(), [loader.TemplateRefSlot.MCP])
            assert loaded.agent_client_credentials == (
                {"client_id": "agent-a", "client_secret": secret} if secret else None
            )
            assert loaded.agent_template["data"] == {"custom_config": custom}
            assert row == before
            assert "agent_client_credentials" not in loaded.as_dict()
            assert secret is None or secret not in repr(loaded)
            apply_dispatched_credentials(loaded)
            credentials = read_instance_credentials()
            if secret:
                assert credentials.client_id == "agent-a" and credentials.client_secret == secret
            else:
                assert credentials is None
            # Unknown Agent IDs cannot reuse another registration or custom JSON credentials.
            unknown = await loader.load_effective_enterprise_config(
                _request("unregistered"), [loader.TemplateRefSlot.MCP],
            )
            assert unknown.agent_client_credentials is None
            assert unknown.agent_template["data"] == {"custom_config": custom}
            apply_dispatched_credentials(unknown)
            assert read_instance_credentials() is None
    finally:
        apply_dispatched_credentials(None)
        loader.invalidate_enterprise_config_caches()
