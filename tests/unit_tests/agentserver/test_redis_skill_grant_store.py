# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared-state lifecycle, isolation and fail-closed regression coverage."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock

import pytest

fakeredis = pytest.importorskip("fakeredis")
from openjiuwen.harness.security.skill_authorization import (
    GrantDecision,
    GrantStatus,
    SkillGrantStore,
    SkillManifest,
    SkillTrustLevel,
    get_skill_grant_store,
    set_skill_grant_store,
)

from jiuwenswarm.agents.harness.common.rails import skill_active_state
from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.redis_store import (
    RedisSkillGrantStore,
    configure_shared_skill_authorization,
)


@pytest.fixture
def stores():
    server = fakeredis.FakeServer()
    clients = [
        fakeredis.FakeRedis(server=server, decode_responses=True) for _ in range(2)
    ]
    a, b = [RedisSkillGrantStore(c, namespace="deployment-a") for c in clients]
    original = get_skill_grant_store()
    yield a, b
    set_skill_grant_store(original)


@pytest.fixture
def manifest():
    return SkillManifest(
        skill_name="会议预定",
        source="test",
        version="1",
        trust=SkillTrustLevel.OTHER,
        permissions_hash="permission-hash",
        skill_md_hash="body-hash",
        overlay={"tools": {"esb_system_tool": "allow"}},
    )


def activate(store, manifest, scope="main", decision=GrantDecision.LOCAL):
    store.create_pending_grant(
        "session",
        scope,
        manifest,
        decision=decision,
        approval_tool_call_id="load-1",
        authorization_generation=store.authorization_generation,
    )
    assert store.activate_pending(
        "session", scope, manifest.skill_name, manifest, approval_tool_call_id="load-1"
    )


def test_cross_pod_full_grant_and_active_skill(stores, manifest):
    a, b = stores
    activate(a, manifest)
    a.update_active_skill_state(
        "session",
        lambda _s: {
            "active_skill": manifest.skill_name,
            "source_session": None,
            "invokes_since_skill_tool": 0,
        },
    )
    assert b.get_active("session", "main").identity_tuple() == manifest.identity_tuple()
    assert b.get_active("session", "main").overlay_snapshot == manifest.overlay
    set_skill_grant_store(b)
    assert skill_active_state.get_session_active_skill("session") == manifest.skill_name
    assert b._client.ttl(b._session_key("session")) > 60 * 60
    # Disposing an adapter is not deleting the user's logical session.
    skill_active_state.clear_session_skill_state("session", local_only=True)
    assert skill_active_state.get_session_active_skill("session") == manifest.skill_name


def test_session_scope_and_deployment_isolation(stores, manifest):
    a, b = stores
    activate(a, manifest)
    assert b.get_active("other-session", "main") is None
    assert b.get_active("session", "child") is None
    other = RedisSkillGrantStore(a._client, namespace="deployment-b")
    assert other.get_active("session", "main") is None


@pytest.mark.parametrize("mismatch", ["body", "approval"])
def test_identity_and_approval_binding(stores, manifest, mismatch):
    a, b = stores
    a.create_pending_grant(
        "session",
        "main",
        manifest,
        decision=GrantDecision.LOCAL,
        approval_tool_call_id="load-1",
    )
    checked = (
        replace(manifest, skill_md_hash="changed") if mismatch == "body" else manifest
    )
    call_id = "wrong" if mismatch == "approval" else "load-1"
    assert (
        b.activate_pending(
            "session",
            "main",
            manifest.skill_name,
            checked,
            approval_tool_call_id=call_id,
        )
        is None
    )
    assert a.get_active("session", "main") is None


def test_revoke_reaches_old_pod(stores, manifest):
    a, b = stores
    activate(a, manifest)
    a.update_active_skill_state(
        "session", lambda _s: {"active_skill": manifest.skill_name}
    )
    b.revoke_grants("session", "main")
    assert a.get_active("session", "main") is None
    assert a.get_active_skill_state("session") is None


def test_session_approval_becomes_inactive_on_completion(stores, manifest):
    a, b = stores
    activate(a, manifest, decision=GrantDecision.SESSION)
    b.revoke_grants("session", "main")
    assert a.get_active("session", "main") is None
    assert (
        a.get_grant("session", "main", manifest.skill_name).status
        == GrantStatus.APPROVED_INACTIVE
    )


def test_deleted_session_cannot_be_recreated(stores, manifest):
    a, b = stores
    activate(a, manifest)
    b.clear_session("session")
    assert a.get_active("session", "main") is None
    with pytest.raises(RuntimeError, match="deleted"):
        a.create_pending_grant(
            "session", "main", manifest, decision=GrantDecision.LOCAL
        )
    with pytest.raises(RuntimeError, match="deleted"):
        a.update_active_skill_state(
            "session", lambda _s: {"active_skill": manifest.skill_name}
        )
    a.clear_all()  # Config reload must not undo a deletion tombstone.
    with pytest.raises(RuntimeError, match="deleted"):
        b.enter_skill_execution("session", "child", manifest.skill_name)


def test_disable_epoch_invalidates_grants_active_skill_and_late_approval(
    stores, manifest
):
    a, b = stores
    activate(a, manifest)
    old_epoch = a.authorization_generation
    a.update_active_skill_state(
        "session", lambda _s: {"active_skill": manifest.skill_name}
    )
    b.clear_all()
    assert a.get_active("session", "main") is None
    assert a.get_active_skill_state("session") is None
    with pytest.raises(RuntimeError, match="pending"):
        a.create_pending_grant(
            "session",
            "main",
            manifest,
            decision=GrantDecision.LOCAL,
            authorization_generation=old_epoch,
        )


def test_cas_retries_do_not_resurrect_after_epoch_change(stores, manifest, monkeypatch):
    a, b = stores
    activate(a, manifest)
    original = a._client.eval

    def race(script, *args):
        if "return -1" in script:
            b.clear_all()
        return original(script, *args)

    monkeypatch.setattr(a._client, "eval", race)
    assert a.get_active("session", "main") is None
    monkeypatch.setattr(a._client, "eval", original)
    assert b.get_active("session", "main") is None


def test_redis_failure_does_not_serve_old_grants(stores, manifest, monkeypatch):
    a, _b = stores
    activate(a, manifest)
    monkeypatch.setattr(a._client, "eval", Mock(side_effect=ConnectionError("offline")))
    assert a.get_active("session", "main") is None
    assert a.get_active_skill_state("session") is None
    with pytest.raises(ConnectionError):
        a.revoke_grants("session", "main")


def test_parallel_active_state_updates_are_atomic(stores):
    def count(store):
        for _ in range(10):
            store.update_active_skill_state(
                "session", lambda s: {"count": s.get("count", 0) + 1}
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(count, stores))
    assert stores[0].get_active_skill_state("session")["count"] == 20


def test_child_scope_cleanup_does_not_clear_parent(stores, manifest):
    a, b = stores
    activate(a, manifest)
    activate(b, manifest, scope="child")
    b.enter_skill_execution("session", "child", manifest.skill_name)
    assert a.get_active_skill_execution("session", "child") == manifest.skill_name
    b.clear_scope("session", "child")
    assert a.get_active("session", "main")
    assert a.get_active("session", "child") is None
    assert a.get_active_skill_execution("session", "child") is None


def test_delete_between_read_and_cas_cannot_recreate_document(
    stores, manifest, monkeypatch
):
    a, b = stores
    original = a._client.eval

    def race(script, *args):
        if "return -1" in script:
            b.clear_session("session")
        return original(script, *args)

    monkeypatch.setattr(a._client, "eval", race)
    with pytest.raises(RuntimeError, match="deleted"):
        a.create_pending_grant(
            "session", "main", manifest, decision=GrantDecision.LOCAL
        )
    assert not a._client.exists(a._session_key("session"))


def test_late_active_skill_callback_cannot_undo_revocation(stores, manifest):
    a, b = stores
    activate(a, manifest)
    b.revoke_grants("session", "main")
    a.update_active_skill_state(
        "session", lambda _s: {"active_skill": manifest.skill_name}
    )
    assert b.get_active_skill_state("session") is None
    assert a.get_active("session", "main") is None


def test_expiry_never_falls_back_to_old_local_state(stores, manifest):
    a, b = stores
    activate(a, manifest)
    assert a.get_active("session", "main")
    b._client.delete(b._session_key("session"))
    assert a.get_active("session", "main") is None


def test_corrupt_document_never_provides_permissions(stores, manifest):
    a, _b = stores
    activate(a, manifest)
    a._client.set(a._session_key("session"), '{"schema":999,"generation":0}')
    assert a.get_active("session", "main") is None
    assert a.get_active_skill_state("session") is None


@pytest.mark.parametrize(
    "failure", ["ConnectionError", "TimeoutError", "AuthenticationError"]
)
def test_startup_redis_failure_keeps_original_memory_store(
    monkeypatch, caplog, failure
):
    import redis

    original = get_skill_grant_store()
    memory = SkillGrantStore()
    client = Mock()
    client.ping.side_effect = getattr(redis.exceptions, failure)("unavailable")
    monkeypatch.setattr(redis, "Redis", Mock(return_value=client))
    set_skill_grant_store(memory)
    try:
        result = configure_shared_skill_authorization(
            {"skill_authorization_store": {"backend": "redis"}}
        )
        assert result is None
        assert get_skill_grant_store() is memory
        client.close.assert_called_once_with()
        assert "using memory for this process" in caplog.text
    finally:
        set_skill_grant_store(original)


def test_startup_success_installs_redis_without_later_memory_fallback(monkeypatch):
    import redis

    original = get_skill_grant_store()
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis, "Redis", Mock(return_value=client))
    set_skill_grant_store(SkillGrantStore())
    config = {
        "skill_authorization_store": {"backend": "redis", "namespace": "startup-test"}
    }
    try:
        shared = configure_shared_skill_authorization(config)
        assert isinstance(shared, RedisSkillGrantStore)
        assert get_skill_grant_store() is shared
        monkeypatch.setattr(
            client, "ping", Mock(side_effect=redis.exceptions.ConnectionError())
        )
        assert configure_shared_skill_authorization(config) is shared
        assert get_skill_grant_store() is shared
    finally:
        set_skill_grant_store(original)


def test_invalid_startup_configuration_does_not_trigger_memory_fallback():
    original = get_skill_grant_store()
    with pytest.raises(ValueError, match="backend"):
        configure_shared_skill_authorization(
            {"skill_authorization_store": {"backend": "typo"}}
        )
    assert get_skill_grant_store() is original


def test_startup_does_not_replace_memory_grants(manifest):
    original = get_skill_grant_store()
    memory = SkillGrantStore()
    activate(memory, manifest)
    set_skill_grant_store(memory)
    try:
        with pytest.raises(RuntimeError, match="before accepting requests"):
            configure_shared_skill_authorization(
                {"skill_authorization_store": {"backend": "redis"}}
            )
        assert get_skill_grant_store() is memory
        assert memory.get_active("session", "main")
    finally:
        set_skill_grant_store(original)
