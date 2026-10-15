# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session files preserve full authorization across processes without a cache."""

import json
import multiprocessing
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
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
from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
    FileSkillGrantStore,
    configure_shared_skill_authorization,
)


@pytest.fixture
def stores(tmp_path):
    sessions = tmp_path / "sessions"
    (sessions / "session").mkdir(parents=True)
    a, b = [
        FileSkillGrantStore(sessions_root=lambda: sessions, data_root=lambda: tmp_path)
        for _ in range(2)
    ]
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
        permissions_hash="permissions",
        skill_md_hash="body",
        overlay={"tools": {"esb_system_tool": "allow"}},
    )


def activate(store, manifest, scope="main", decision=GrantDecision.LOCAL):
    store.create_pending_grant(
        "session",
        scope,
        manifest,
        decision=decision,
        approval_tool_call_id="load-1",
        authorization_generation=store.get_authorization_generation("session"),
    )
    assert store.activate_pending(
        "session", scope, manifest.skill_name, manifest, approval_tool_call_id="load-1"
    )


def test_full_grant_active_state_and_adapter_eviction(stores, manifest):
    a, b = stores
    activate(a, manifest)
    a.update_active_skill_state(
        "session", lambda _: {"active_skill": manifest.skill_name}
    )
    assert b.get_active("session", "main").identity_tuple() == manifest.identity_tuple()
    assert b.get_active("session", "main").overlay_snapshot == manifest.overlay
    set_skill_grant_store(b)
    skill_active_state.clear_session_skill_state("session", local_only=True)
    assert skill_active_state.get_session_active_skill("session") == manifest.skill_name
    assert set(vars(a)) == {"sessions_root", "data_root"}
    assert not hasattr(skill_active_state, "_sessions")


def test_session_scope_and_tenant_isolation(stores, manifest, tmp_path):
    a, b = stores
    activate(a, manifest)
    assert b.get_active("other-session", "main") is None
    assert b.get_active("session", "child") is None
    other = FileSkillGrantStore(
        sessions_root=lambda: tmp_path / "other", data_root=lambda: tmp_path
    )
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
    assert (
        b.activate_pending(
            "session",
            "main",
            manifest.skill_name,
            checked,
            approval_tool_call_id="wrong" if mismatch == "approval" else "load-1",
        )
        is None
    )
    assert a.get_active("session", "main") is None


def test_revocation_is_seen_by_original_process(stores, manifest):
    a, b = stores
    activate(a, manifest)
    a.update_active_skill_state(
        "session", lambda _: {"active_skill": manifest.skill_name}
    )
    b.revoke_grants("session", "main")
    assert a.get_active("session", "main") is None
    assert a.get_active_skill_state("session") is None
    a.update_active_skill_state(
        "session", lambda _: {"active_skill": manifest.skill_name}
    )
    assert (
        b.get_active_skill_state("session") is None
    )  # late callback cannot restore it


def test_session_approval_survives_completion_as_inactive(stores, manifest):
    a, b = stores
    activate(a, manifest, decision=GrantDecision.SESSION)
    b.revoke_grants("session", "main")
    assert (
        a.get_grant("session", "main", manifest.skill_name).status
        == GrantStatus.APPROVED_INACTIVE
    )
    assert a.get_active("session", "main") is None


@pytest.mark.parametrize("operation", ["cancel", "delete"])
def test_cleanup_uses_explicit_tenant_root_without_request_context(
    stores, manifest, tmp_path, monkeypatch, operation
):
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization import (
        runtime,
    )

    a, _ = stores
    activate(a, manifest)
    a.update_active_skill_state(
        "session", lambda _: {"active_skill": manifest.skill_name}
    )
    # The service singleton cannot rely on request ContextVars in cancel callbacks.
    singleton = FileSkillGrantStore(
        sessions_root=lambda: tmp_path / "wrong-tenant", data_root=lambda: tmp_path
    )
    set_skill_grant_store(singleton)
    monkeypatch.setattr(runtime, "_enabled", lambda: False)
    if operation == "cancel":
        runtime.revoke_main_scope(
            "session", reason="cancel", sessions_root=a.sessions_root()
        )
    else:
        runtime.clear_deleted_session("session", sessions_root=a.sessions_root())
    assert a.get_active("session", "main") is None
    assert a.get_active_skill_state("session") is None
    assert not (tmp_path / "wrong-tenant").exists()


@pytest.mark.parametrize("remove_directory", [False, True])
def test_deletion_blocks_late_writes(stores, manifest, remove_directory):
    a, b = stores
    activate(a, manifest)
    b.clear_session("session")
    if remove_directory:
        shutil.rmtree(b.session_directory("session"))
    assert a.get_active("session", "main") is None
    for operation in [
        lambda: a.create_pending_grant(
            "session", "main", manifest, decision=GrantDecision.LOCAL
        ),
        lambda: a.update_active_skill_state(
            "session", lambda _: {"active_skill": manifest.skill_name}
        ),
    ]:
        with pytest.raises(RuntimeError, match="deleted"):
            operation()
    a.clear_all()
    with pytest.raises(RuntimeError, match="deleted"):
        b.enter_skill_execution("session", "child", manifest.skill_name)


def test_feature_disable_invalidates_old_approval(stores, manifest):
    a, b = stores
    activate(a, manifest)
    generation = a.get_authorization_generation("session")
    b.clear_all()
    assert a.get_active("session", "main") is None
    with pytest.raises(RuntimeError, match="pending"):
        a.create_pending_grant(
            "session",
            "main",
            manifest,
            decision=GrantDecision.LOCAL,
            authorization_generation=generation,
        )


def test_recreated_session_rejects_previous_incarnation_approval(stores, manifest):
    a, _ = stores
    generation = a.get_authorization_generation("session")
    path = a.session_directory("session")
    shutil.rmtree(path)
    path.mkdir()
    with pytest.raises(RuntimeError, match="pending"):
        a.create_pending_grant(
            "session",
            "main",
            manifest,
            decision=GrantDecision.LOCAL,
            authorization_generation=generation,
        )


def test_scope_cleanup_and_read_only_operations(stores, manifest):
    a, b = stores
    activate(a, manifest)
    activate(a, manifest, scope="child")
    b.enter_skill_execution("session", "child", manifest.skill_name)
    b.clear_scope("session", "child")
    assert a.get_active("session", "main")
    assert a.get_active_skill_execution("session", "child") is None
    path = a.session_directory("session") / ".skill_authorization/state.json"
    before = path.stat().st_mtime_ns
    assert b.get_active("session", "main")
    assert b.list_grants("session")
    assert path.stat().st_mtime_ns == before
    b.clear_scope("session", "main")
    assert a.get_active("session", "main") is None


def _count_worker(root):
    from pathlib import Path

    path = Path(root)
    store = FileSkillGrantStore(
        sessions_root=lambda: path / "sessions", data_root=lambda: path
    )
    for _ in range(10):
        store.update_active_skill_state(
            "session", lambda s: {"count": s.get("count", 0) + 1}
        )


def test_cross_process_file_lock_prevents_lost_updates(stores):
    a, _ = stores
    ctx = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    processes = [
        ctx.Process(target=_count_worker, args=(str(a.data_root()),)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert a.get_active_skill_state("session")["count"] == 20


def test_concurrent_threads(stores):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda store: _count_worker(str(store.data_root())), stores))
    assert stores[0].get_active_skill_state("session")["count"] == 20


def test_independent_sessions_read_concurrently(stores, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization import (
        file_store,
    )

    store, _ = stores
    store.clear_all()  # Exercise the shared epoch as well as session locks.
    session_ids = [f"parallel-{i}" for i in range(10)]
    for sid in session_ids:
        (store.sessions_root() / sid).mkdir()
        store.update_active_skill_state(sid, lambda _: {"active_skill": "probe"})
    rendezvous = threading.Barrier(len(session_ids))
    read_json = file_store._read_json

    def concurrent_read(fd, name):
        if name == "state.json":
            rendezvous.wait(timeout=3)
        return read_json(fd, name)

    monkeypatch.setattr(file_store, "_read_json", concurrent_read)
    with ThreadPoolExecutor(max_workers=len(session_ids)) as pool:
        results = list(pool.map(store.get_active_skill_state, session_ids))
    assert all(state == {"active_skill": "probe"} for state in results)
    assert not file_store._LOCAL_IO_LOCKS  # No growing per-session lock cache.


def test_epoch_reads_do_not_wait_for_writer_lock(stores):
    import portalocker

    store, _ = stores
    store.clear_all()
    with (store.epoch_directory() / "state.lock").open("a+b") as lock:
        portalocker.lock(lock, portalocker.LOCK_EX)
        assert store.read_epoch() == 1
        portalocker.unlock(lock)


def test_epoch_change_during_transaction_rejects_old_result(stores, manifest):
    a, b = stores
    activate(a, manifest)

    def invalidate_during_read(store, _doc):
        result = store.get_active("session", "main")
        b.clear_all()
        return result

    with pytest.raises(RuntimeError, match="changed during"):
        a.operate("session", invalidate_during_read, write=False)
    assert a.get_active("session", "main") is None


@pytest.mark.parametrize(
    "corruption", ["invalid-json", "bad-grant", "bad-map", "symlink"]
)
def test_corruption_never_serves_old_authorization(
    stores, manifest, tmp_path, corruption
):
    a, b = stores
    activate(a, manifest)
    path = a.session_directory("session") / ".skill_authorization/state.json"
    if corruption == "invalid-json":
        path.write_text("{")
    elif corruption == "bad-grant":
        doc = json.loads(path.read_text())
        doc["grants"]["main"] = [{"unexpected": True}]
        path.write_text(json.dumps(doc))
    elif corruption == "bad-map":
        doc = json.loads(path.read_text())
        doc["grants"] = []
        path.write_text(json.dumps(doc))
    else:
        replacement = tmp_path / "forged"
        replacement.write_text(path.read_text())
        path.unlink()
        path.symlink_to(replacement)
    assert b.get_active("session", "main") is None
    assert b.get_active_skill_state("session") is None


def test_startup_installs_files_without_redis_configuration(monkeypatch):
    original = get_skill_grant_store()
    try:
        set_skill_grant_store(SkillGrantStore())
        store = configure_shared_skill_authorization({})
        assert isinstance(store, FileSkillGrantStore)
        assert configure_shared_skill_authorization({}) is store
    finally:
        set_skill_grant_store(original)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits unlinking open files")
def test_deleted_during_transaction_cannot_recreate_directory(stores):
    a, b = stores
    a.update_active_skill_state("session", lambda _: {"active_skill": "before"})
    directory = a.session_directory("session")

    def delete_while_updating(_state):
        shutil.rmtree(directory)
        return {"active_skill": "late"}

    with pytest.raises(OSError):
        a.update_active_skill_state("session", delete_while_updating)
    assert not directory.exists()
    assert b.get_active_skill_state("session") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows protects open lock handles")
def test_windows_transaction_prevents_directory_deletion(stores):
    a, b = stores
    a.update_active_skill_state("session", lambda _: {"active_skill": "before"})
    directory = a.session_directory("session")

    def delete_while_updating(_state):
        with pytest.raises(PermissionError):
            shutil.rmtree(directory)
        return {"active_skill": "after"}

    a.update_active_skill_state("session", delete_while_updating)
    assert b.get_active_skill_state("session")["active_skill"] == "after"
    shutil.rmtree(directory)
    assert b.get_active_skill_state("session") is None


def test_bounded_lock_wait_never_serves_cached_state(stores, manifest):
    import time

    import portalocker

    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
        _LOCK_TIMEOUT,
    )

    a, b = stores
    activate(a, manifest)
    path = a.session_directory("session") / ".skill_authorization/state.lock"
    with path.open("a+b") as lock:
        portalocker.lock(lock, portalocker.LOCK_EX)
        started = time.monotonic()
        assert b.get_active("session", "main") is None
        elapsed = time.monotonic() - started
        assert _LOCK_TIMEOUT <= elapsed < 1
        portalocker.unlock(lock)
    assert a.get_active("session", "main")


def test_portable_path_based_atomic_io(tmp_path):
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.file_store import (
        _atomic_json,
        _read_json,
    )

    _atomic_json(tmp_path, "state.json", {"value": "会话"})
    assert _read_json(tmp_path, "state.json")[0] == {"value": "会话"}
    linked = tmp_path / "link.json"
    linked.symlink_to(tmp_path / "state.json")
    with pytest.raises(ValueError):
        _read_json(tmp_path, "link.json")


def test_cold_disabled_pod_retires_persisted_state_and_old_approval(stores, manifest):
    a, b = stores
    a.sync_feature_state("session", enabled=True)
    activate(a, manifest)
    old_generation = a.get_authorization_generation("session")
    a.update_active_skill_state(
        "session", lambda _: {"active_skill": manifest.skill_name}
    )
    b.sync_feature_state("session", enabled=False)
    assert a.get_active("session", "main") is None
    assert a.get_active_skill_state("session") is None
    b.sync_feature_state("session", enabled=True)
    assert a.get_active("session", "main") is None
    with pytest.raises(RuntimeError, match="pending"):
        a.create_pending_grant(
            "session",
            "main",
            manifest,
            decision=GrantDecision.LOCAL,
            authorization_generation=old_generation,
        )


@pytest.mark.parametrize("error", [OSError, ValueError, TypeError, RuntimeError, TimeoutError])
def test_generation_read_errors_abort_instead_of_returning_a_comparable_value(
    stores, monkeypatch, error,
):
    from openjiuwen.core.runner.callback.errors import AbortError

    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization import (
        file_store,
    )

    store, _ = stores
    token = file_store._session_file_context.set(("session", store.sessions_root()))

    def unavailable(_sid):
        raise error("unavailable")

    monkeypatch.setattr(store, "get_authorization_generation", unavailable)
    try:
        with pytest.raises(AbortError, match="version unavailable"):
            _ = store.authorization_generation
    finally:
        file_store._session_file_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["allow", "reject", "delete", "recreate", "corrupt", "lock"])
async def test_child_tool_approval_cannot_execute_after_session_storage_changes(
    stores, manifest, monkeypatch, change,
):
    from contextlib import ExitStack
    from types import SimpleNamespace

    import portalocker
    from openjiuwen.core.runner.callback.errors import AbortError
    from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails import subagent_permission_rail
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization import (
        file_store,
    )

    store, _ = stores
    set_skill_grant_store(store)
    store.enter_skill_execution("session", "child", manifest.skill_name)
    token = file_store._session_file_context.set(None)  # Child task lost parent ContextVars.
    directory = store.session_directory("session")
    policy = {"enabled": True, "defaults": {"*": "ask"},
              "skill_authorization": {"enabled": True}}
    monkeypatch.setattr(
        subagent_permission_rail, "merge_session_permissions_overlay", lambda base, **kw: base,
    )
    with ExitStack() as resources:
        async def answer(**_kwargs):
            if change in {"delete", "recreate"}:
                shutil.rmtree(directory)
            if change == "recreate":
                directory.mkdir()
                store.enter_skill_execution("session", "child", manifest.skill_name)
            elif change == "corrupt":
                (directory / ".skill_authorization/state.json").write_text("{")
            elif change == "lock":
                lock = resources.enter_context((directory / ".skill_authorization/state.lock").open("a+b"))
                portalocker.lock(lock, portalocker.LOCK_EX)
            return {"selected_options": ["不允许" if change == "reject" else "本次允许"]}

        async def sender(*_args, **_kwargs):
            pass

        rail = subagent_permission_rail.SubagentPermissionRail(
            agent_scope_id="child", session_id="session", grant_store=store,
            approval_registry=SimpleNamespace(request=answer), approval_sender=sender,
            config_provider=lambda: policy,
        )
        rail.set_sessions_root(store.sessions_root())
        tool = SimpleNamespace(id="approval-call", name="probe", arguments={})
        ctx = SimpleNamespace(inputs=ToolCallInputs(
            tool_call=tool, tool_name="probe", tool_args={}, tool_result=None, tool_msg=None,
        ), extra={})
        callbacks = AsyncCallbackFramework(enable_logging=False)
        await callbacks.register("before_tool", rail.before_tool_call)
        try:
            if change in {"delete", "corrupt", "lock"}:
                with pytest.raises(AbortError, match="version unavailable"):
                    await callbacks.trigger("before_tool", ctx)
            else:
                await callbacks.trigger("before_tool", ctx)
                assert bool(ctx.extra.get("_skip_tool")) == (change != "allow")
                assert "_interrupt_decision" in ctx.extra
        finally:
            file_store._session_file_context.reset(token)


@pytest.mark.asyncio
async def test_child_wiring_binds_parent_files_without_inherited_context(stores):
    from types import SimpleNamespace

    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization import (
        file_store,
    )
    from jiuwenswarm.agents.harness.common.tools.subagent_executor.authorization import (
        build_subagent_authorization_rails,
    )

    store, _ = stores
    set_skill_grant_store(store)
    token = file_store._session_file_context.set(None)
    try:
        rails = build_subagent_authorization_rails(
            "child", "session", config_provider=lambda: {
                "enabled": True, "skill_authorization": {"enabled": True},
            },
        )
        assert len(rails) == 2
        ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name="probe"), extra={})
        for rail in rails:
            file_store.clear_session_file_context()
            await rail.before_tool_call(ctx)
            assert file_store._session_file_context.get() == ("session", store.sessions_root())
        first = store.authorization_generation
        assert first == store.authorization_generation != 0
        assert (store.session_directory("session") / ".skill_authorization/state.json").is_file()
    finally:
        file_store._session_file_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["allow", "delete", "corrupt", "recreate"])
async def test_child_skill_approval_rechecks_persisted_session_version(
    stores, manifest, monkeypatch, change,
):
    from types import SimpleNamespace

    from openjiuwen.core.runner.callback.errors import AbortError
    from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs
    from openjiuwen.harness.rails.skills.skill_authorization_rail import (
        _SkillApprovalCall,
    )
    from openjiuwen.harness.security.skill_authorization import SkillPermissionDiff

    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization import (
        file_store,
    )
    from jiuwenswarm.agents.harness.common.rails.subagent_skill_authorization_rail import (
        SubagentSkillAuthorizationRail,
    )

    store, _ = stores
    set_skill_grant_store(store)
    token = file_store._session_file_context.set(("session", store.sessions_root()))
    directory = store.session_directory("session")

    async def answer(**_kwargs):
        if change in {"delete", "recreate"}:
            shutil.rmtree(directory)
        if change == "recreate":
            directory.mkdir()
        elif change == "corrupt":
            (directory / ".skill_authorization/state.json").write_text("{")
        return {"action": "approve_once"}

    async def sender(*_args, **_kwargs):
        pass

    rail = SubagentSkillAuthorizationRail(
        agent_scope_id="child", session_id="session", grant_store=store,
        approval_registry=SimpleNamespace(request=answer), approval_sender=sender,
        config_provider=lambda: {"enabled": True, "skill_authorization": {"enabled": True}},
    )
    monkeypatch.setattr(rail, "_resolve_manifest_for_call", lambda _name: manifest)
    tool = SimpleNamespace(id="skill-call", name="skill_tool", arguments={})
    ctx = SimpleNamespace(inputs=ToolCallInputs(
        tool_call=tool, tool_name="skill_tool", tool_args={}, tool_result=None, tool_msg=None,
    ), extra={})
    try:
        call = _SkillApprovalCall(
            ctx=ctx, tool_call=tool, tool_name="skill_tool", tool_call_id="skill-call",
            session_id="session", agent_scope_id="child", manifest=manifest,
            diff=SkillPermissionDiff(), authorization_generation=store.authorization_generation,
        )

        async def approve(_ctx):
            await rail._run_approval_flow(call)

        callbacks = AsyncCallbackFramework(enable_logging=False)
        await callbacks.register("before_tool", approve)
        if change in {"delete", "corrupt"}:
            with pytest.raises(AbortError, match="version unavailable"):
                await callbacks.trigger("before_tool", ctx)
        else:
            await callbacks.trigger("before_tool", ctx)
            grant = store.get_grant("session", "child", manifest.skill_name)
            assert (grant is not None) == (change == "allow")
    finally:
        file_store._session_file_context.reset(token)
