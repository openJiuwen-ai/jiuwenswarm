# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""User/agent-level permission publication after session Manager keys were retired.

Session publish/acquire/retain are no-ops. Skill overlays are composed as
temporary ``config=`` at check time, not as derived Manager keys.
"""

import copy

import pytest
from agent_shield.tool_permission import get_permission_manager
from openjiuwen.harness.security.skill_authorization import (
    SkillGrantStore,
    SkillManifest,
    SkillTrustLevel,
    GrantDecision,
)

from jiuwenswarm.agents.harness.common.rails.permissions import config_loader
from jiuwenswarm.agents.harness.common.rails.permissions.published_scopes import (
    PublishedPermissionScopes,
)
from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
    SkillAuthorizationPermissionRail,
)


@pytest.fixture
def publisher():
    store = SkillGrantStore()
    config = {
        "enabled": True,
        "defaults": {"*": "ask"},
        "tools": {"get_current_user": "ask"},
        "skill_authorization": {"enabled": True},
        "net_guard": {"enabled": True, "defaults": "allow"},
    }
    publisher = PublishedPermissionScopes(
        "w3-published-test", base_config=config, store=store
    )
    yield publisher, store, config
    publisher.close()


def activate(store, session, scope="main"):
    manifest = SkillManifest(
        "fixture",
        "test",
        None,
        SkillTrustLevel.OTHER,
        "hash",
        skill_md_hash="markdown-hash",
        overlay={"tools": {"get_current_user": "allow"}},
    )
    store.create_pending_grant(session, scope, manifest, decision=GrantDecision.LOCAL)
    store.activate_pending(session, scope, manifest.skill_name, manifest)


def test_session_publish_apis_are_noop(publisher):
    scopes, _, config = publisher
    manager = get_permission_manager()
    before = set(manager.keys())
    scopes.publish_session("session-one", config)
    scopes.publish_session("session-two", config)
    assert scopes.session_ids() == []
    assert set(manager.keys()) == before
    with pytest.raises(KeyError):
        scopes.key_for("session-one")


@pytest.mark.asyncio
async def test_activation_uses_temp_config_not_manager_skill_key(publisher):
    """Skill activation no longer registers a derived Manager key."""
    from types import SimpleNamespace
    from openjiuwen.harness.security.skill_authorization import get_skill_grant_store

    _, _, config = publisher
    manager = get_permission_manager()
    store = get_skill_grant_store()
    before = set(manager.keys())
    rail = SkillAuthorizationPermissionRail(config=config)
    try:
        activate(store, "session-one")
        rail.set_skill_scope_resolver(lambda _: ("session-one", "main"))
        assert set(manager.keys()) == before | {rail.permission_key}
        ctx = SimpleNamespace(
            agent=object(), session=SimpleNamespace(session_id="session-one")
        )
        temp = rail._temp_permission_config_for_check(ctx)
        assert temp is not None
        assert temp["tools"]["get_current_user"] == "allow"
        assert (
            await manager.check_permission("get_current_user", {}, config=temp)
        ).permission.value == "allow"
        assert (
            await manager.check_permission(
                "get_current_user", {}, rail.permission_key
            )
        ).permission.value == "ask"
        store.invalidate_scope("session-one", "main", reason="test")
        assert set(manager.keys()) == before | {rail.permission_key}
    finally:
        store.clear_session("session-one")
        rail.uninit(None)


def test_session_and_agent_cleanup_do_not_require_session_keys(publisher):
    scopes, store, _ = publisher
    activate(store, "session-one", "child")
    store.clear_session("session-one")
    scopes.close()
    assert scopes.session_ids() == []


def test_unknown_scope_does_not_register_empty_config(publisher):
    scopes, _, _ = publisher
    with pytest.raises(KeyError):
        scopes.key_for("missing-session")
    assert not get_permission_manager().has_config("missing-session")


def test_publish_session_refresh_is_noop_without_manager_churn(publisher, monkeypatch):
    from unittest.mock import Mock

    scopes, store, config = publisher
    activate(store, "session-one")
    manager = get_permission_manager()
    remove = Mock(wraps=manager.remove_config)
    monkeypatch.setattr(manager, "remove_config", remove)
    before = set(manager.keys())
    scopes.publish_session("session-one", config)
    assert set(manager.keys()) == before
    remove.assert_not_called()


def test_netguard_session_delta_merges_and_reverts_without_mutating_base():
    base = {
        "net_guard": {
            "enabled": True,
            "defaults": "allow",
            "urls": {"global.test": "deny"},
            "connection": {"allow_http": False, "allow_private_network": False},
        }
    }
    original = copy.deepcopy(base)
    overlay = {
        "net_guard": {
            "urls": {"session.test": "allow"},
            "connection": {"allow_http": True},
        }
    }
    merged = config_loader._merge_permissions_config(base, overlay)
    assert merged["net_guard"]["urls"] == {
        "global.test": "deny",
        "session.test": "allow",
    }
    assert merged["net_guard"]["connection"] == {
        "allow_http": True,
        "allow_private_network": False,
    }
    assert config_loader._extract_session_overlay(base, merged) == overlay
    assert config_loader._extract_session_overlay(base, original) == {}
    assert base == original


def test_session_policy_write_updates_memory_overlay_only(publisher, monkeypatch):
    scopes, _, config = publisher
    monkeypatch.setattr(config_loader, "is_enterprise", lambda: True)
    monkeypatch.setattr(config_loader, "get_base_permissions_config", lambda: config)
    manager = get_permission_manager()
    before = set(manager.keys())
    try:
        config_loader.persist_permissions_mutate(
            lambda cfg: cfg["net_guard"].update({"urls": {"blocked.test": "deny"}}),
            session_id="session-one",
        )
        assert config_loader.has_session_permissions_overlay("session-one")
        overlay = config_loader._session_overlays["session-one"]
        assert overlay["net_guard"]["urls"] == {"blocked.test": "deny"}
        assert set(manager.keys()) == before
        assert scopes.session_ids() == []
    finally:
        config_loader.clear_session_permissions_overlay("session-one")


@pytest.mark.asyncio
async def test_native_tool_execution_carries_key_and_resets_after_exception_and_cancel():
    import asyncio
    from types import SimpleNamespace
    from flash_sdk import sdk_tools
    from openjiuwen.core.common.security.outbound import outbound_permission_key
    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.harness.rails.security.tool_security_rail import (
        PermissionInterruptRail,
    )

    key = "w3-native-execution"
    config = {
        "enabled": True,
        "defaults": {"*": "allow"},
        "tools": {"outbound_probe": "allow"},
        "net_guard": {"enabled": True},
    }
    rail = PermissionInterruptRail(config=config, permission_key=key)
    started, observed = asyncio.Event(), []

    async def probe(mode):
        observed.append(outbound_permission_key())
        if mode == "cancel":
            started.set()
            await asyncio.Event().wait()
        if mode == "error":
            raise RuntimeError("probe error")
        return "done"

    tool = LocalFunction(
        card=ToolCard(
            name="outbound_probe",
            id="outbound_probe",
            description="probe",
            input_params={"type": "object", "properties": {"mode": {"type": "string"}}},
        ),
        func=probe,
    )
    session = SimpleNamespace(
        session_id="native", get_state=lambda _: None, update_state=lambda _: None
    )
    async with sdk_tools(rail, session, [tool]) as (manager, ctx):
        for mode in ("ok", "error"):
            call = ToolCall(
                id=mode,
                type="function",
                name="outbound_probe",
                arguments='{"mode":"' + mode + '"}',
            )
            await manager.execute(ctx, call, session)
            assert outbound_permission_key() is None
        call = ToolCall(
            id="cancel",
            type="function",
            name="outbound_probe",
            arguments='{"mode":"cancel"}',
        )
        task = asyncio.create_task(manager.execute(ctx, call, session))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert outbound_permission_key() is None
    assert observed == [key, key, key]


def test_file_store_activation_does_not_publish_session_manager_keys(
    tmp_path, monkeypatch
):
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
        FileSkillGrantStore,
    )

    (tmp_path / "session").mkdir()
    store = FileSkillGrantStore(
        sessions_root=lambda: tmp_path, data_root=lambda: tmp_path
    )
    config = {
        "enabled": True,
        "tools": {"get_current_user": "ask"},
        "skill_authorization": {"enabled": True},
    }
    scopes = PublishedPermissionScopes(
        "w3-file-publisher", base_config=config, store=store
    )
    manager = get_permission_manager()
    before = set(manager.keys())
    try:
        scopes.publish_session("session", config)
        activate(store, "session")
        assert set(manager.keys()) == before
        monkeypatch.setattr(
            store, "session_directory", lambda _: tmp_path / "already-deleted"
        )
        store.clear_session("session")
        assert set(manager.keys()) == before
    finally:
        scopes.close()


@pytest.mark.asyncio
async def test_normalized_cache_target_cannot_bypass_netguard(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from openjiuwen.core.common.security.outbound import bind_outbound_permission_key
    from openjiuwen.core.common.security.outbound_http import OutboundPermissionError
    from jiuwenswarm.agents.harness.common.tools import web_fetch_tools

    manager = get_permission_manager()
    key = "w3-cache-policy"
    manager.update_config({"enabled": True, "net_guard": {"enabled": True}}, key)
    cache = SimpleNamespace(get=AsyncMock())
    network = Mock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(web_fetch_tools, "_fetch_webpage_sync", network)
    try:
        with bind_outbound_permission_key(key), pytest.raises(OutboundPermissionError):
            await web_fetch_tools._fetch_single_url(
                "127.0.0.1",
                max_chars=100,
                timeout_seconds=1,
                use_cache=True,
                cache=cache,
            )
        cache.get.assert_not_awaited()
        network.assert_not_called()
    finally:
        manager.remove_config(key)


@pytest.mark.asyncio
async def test_jina_secondary_request_uses_current_key_and_cannot_bypass_deny(monkeypatch):
    import asyncio
    import socket
    from types import SimpleNamespace
    import requests
    from openjiuwen.core.common.security.outbound import bind_outbound_permission_key
    from openjiuwen.core.common.security.outbound_http import OutboundPermissionError
    from jiuwenswarm.common import http_proxy_config
    from jiuwenswarm.agents.harness.common.tools import web_fetch_tools

    key, calls = "w3-jina-policy", []
    manager = get_permission_manager()
    manager.update_config({"enabled": True, "net_guard": {"enabled": True, "urls": {"r.jina.ai": "deny"}}}, key)
    monkeypatch.setattr(http_proxy_config, "prepare_requests_kwargs", lambda url, kwargs: dict(kwargs))
    monkeypatch.setattr(http_proxy_config, "resolve_requests_proxies", lambda _: None)
    monkeypatch.setattr(web_fetch_tools, "_apply_free_search_proxy", lambda *args: False)
    # Legacy default must not turn off TLS on the protected route.
    monkeypatch.setattr(web_fetch_tools, "_free_search_ssl_verify", lambda: False)
    monkeypatch.setenv("JIUWENSWARM_ENABLE_JINA_FETCH", "true")
    original_dns = socket.getaddrinfo

    def dns(host, port, *args, **kwargs):
        return original_dns("8.8.8.8" if host == "r.jina.ai" else host, port, *args, **kwargs)

    def primary_response(self, method, url, **kwargs):
        assert kwargs["verify"] is True
        calls.append(url)
        return SimpleNamespace(status_code=403, headers={}, content=b"", url=url)

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(requests.Session, "request", primary_response)
    try:
        with bind_outbound_permission_key(key), pytest.raises(OutboundPermissionError):
            await asyncio.to_thread(web_fetch_tools._fetch_webpage_sync, "https://8.8.8.8/", 1)
        assert calls == ["https://8.8.8.8/"]
    finally:
        manager.remove_config(key)
