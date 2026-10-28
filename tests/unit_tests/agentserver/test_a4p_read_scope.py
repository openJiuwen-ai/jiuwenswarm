from __future__ import annotations

from types import SimpleNamespace

import pytest
from a4p.user_authorizer import approve_user_mandate
from openjiuwen.harness.security.host import PermissionConfirmationRequest

from jiuwenswarm.agents.harness.common.a4p import runtime as a4p_runtime
from jiuwenswarm.agents.harness.common.a4p.execution_context import (
    AUTHORIZATION_EXECUTION_CONTEXTS,
    AuthorizationExecutionContext,
    AuthorizerRoute,
)
from jiuwenswarm.agents.harness.common.a4p.runtime import (
    A4PIdentity,
    A4PRuntime,
    INTERNAL_A4P_USER_ID,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.agents.harness.common.tools.a4p_tools import _normalize_actions


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(a4p_runtime, "get_config_dir", lambda: tmp_path)
    return A4PRuntime({"a4p": {"enabled": True}})


async def issue(runtime, actions):
    prepared = await runtime.server.prepare_intent_authorization({
        "agentId": "agent-1", "userId": INTERNAL_A4P_USER_ID,
        "intent": {"actions": _normalize_actions(actions)}, "validitySeconds": 60,
    })
    result = await runtime.server.complete_intent_authorization({
        "signedMandate": approve_user_mandate(prepared.mandate),
    })
    assert result.intentToken is not None
    runtime.store_intent_token("session", result.intentToken)
    runtime.store_cron_intent_token("job", result.intentToken)
    return result.intentToken


async def matches(runtime, action, params, cron=False):
    kwargs = {"identity": A4PIdentity(agent_id="agent-1"), "action": action, "params": params}
    if cron:
        return await runtime.find_valid_cron_intent_token(cron_job_id="job", **kwargs)
    return await runtime.find_valid_intent_token(session_id="session", **kwargs)


@pytest.mark.parametrize("action,field", [
    ("read_file", "file_path"), ("list_files", "path"), ("glob", "path"), ("grep", "path"),
])
@pytest.mark.parametrize("path", [None, "", ".", "relative/file.md", "*", "/tmp/../secret", "/tmp/*/file"])
def test_invalid_read_scope(action, field, path):
    with pytest.raises(ValueError):
        _normalize_actions([{"name": action, "params": {field: path}}])


@pytest.mark.asyncio
@pytest.mark.parametrize("cron", [False, True])
@pytest.mark.parametrize("action,field,extras", [
    ("read_file", "file_path", {"offset": 1, "limit": 5}),
    ("list_files", "path", {"show_hidden": True}),
    ("glob", "path", {"pattern": "**/*.md"}),
    ("grep", "path", {"pattern": "changed keyword", "head_limit": 10}),
])
async def test_real_token_read_tools(runtime, tmp_path, cron, action, field, extras):
    root = tmp_path / "reports"
    root.mkdir()
    target = root / "notes.md" if action == "read_file" else root
    if action == "read_file":
        target.write_text("notes")
    await issue(runtime, [{"name": action, "params": {field: str(target)}}])
    assert await matches(runtime, action, {field: str(target), **extras}, cron)
    assert await matches(runtime, action, {field: str(tmp_path / "other"), **extras}, cron) is None
    assert await matches(runtime, action, extras, cron) is None
    assert await matches(runtime, "write_file", {"file_path": str(target), "content": "x"}, cron) is None


@pytest.mark.asyncio
async def test_filename_glob_does_not_cover_descendants_or_outward_links(runtime, tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    child = root / "nested"
    child.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("secret")
    (root / "link.md").symlink_to(outside)
    await issue(runtime, [{"name": "read_file", "params": {"file_path": str(root / "*.md")}}])
    assert await matches(runtime, "read_file", {"file_path": str(root / "notes.md")})
    for path in [child / "notes.md", root / "link.md", root / ".." / "secret.md"]:
        assert await matches(runtime, "read_file", {"file_path": str(path)}) is None


@pytest.mark.asyncio
async def test_constraints_cannot_be_combined_across_different_actions(runtime, tmp_path):
    await issue(runtime, [
        {"name": "read_file", "params": {"file_path": str(tmp_path / "*.md"), "offset": 1}},
        {"name": "read_file", "params": {"file_path": str(tmp_path / "nested" / "*.md"), "offset": 10}},
    ])
    path = str(tmp_path / "nested" / "notes.md")
    assert await matches(runtime, "read_file", {"file_path": path, "offset": 1}) is None
    assert await matches(runtime, "read_file", {"file_path": path, "offset": 10})


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["list_files", "glob"])
async def test_discovery_is_not_read_permission(runtime, tmp_path, action):
    await issue(runtime, [{"name": action, "params": {"path": str(tmp_path)}}])
    assert await matches(runtime, "read_file", {"file_path": str(tmp_path / "notes.md")}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["glob", "grep"])
async def test_recursive_search_rechecks_new_outward_links(runtime, tmp_path, action):
    root = tmp_path / "reports"
    root.mkdir()
    await issue(runtime, [{"name": action, "params": {"path": str(root)}}])
    args = {"path": str(root), "pattern": "*.md" if action == "glob" else "secret"}
    assert await matches(runtime, action, args, cron=True)
    (root / "external").symlink_to(tmp_path, target_is_directory=True)
    assert await matches(runtime, action, args, cron=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["../*.md", "{*.md,../secret/*}", "/etc/*", "{*.md,/etc/*}", "..\\secret\\*"])
async def test_glob_cannot_escape_search_root(runtime, tmp_path, pattern):
    await issue(runtime, [{"name": "glob", "params": {"path": str(tmp_path)}}])
    assert await matches(runtime, "glob", {"path": str(tmp_path), "pattern": pattern}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cron", [False, True])
async def test_meeting_batch_passes_permission_host(runtime, tmp_path, monkeypatch, cron):
    config = {"a4p": {"enabled": True}, "permissions": {"enabled": True}}
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: config)
    monkeypatch.setattr(a4p_runtime, "get_a4p_runtime", lambda: runtime)
    actions = [
        {"name": "read_file", "params": {"file_path": str(tmp_path / name)}}
        for name in ["meeting.md", "progress.md", "todo.md"]
    ] + [
        {"name": "write_file", "params": {"file_path": str(tmp_path / "minutes.md")}},
        {"name": "edit_file", "params": {"file_path": str(tmp_path / "progress.md")}},
        {"name": "edit_file", "params": {"file_path": str(tmp_path / "todo.md")}},
    ]
    await issue(runtime, actions)
    AUTHORIZATION_EXECUTION_CONTEXTS.activate(AuthorizationExecutionContext(
        request_id="request", session_id="session", channel_id="__cron__" if cron else "web",
        agent_id="agent-1", metadata={"cron": {"job_id": "job"}} if cron else {},
        authorizer_route=None if cron else AuthorizerRoute(
            session_id="session", app_id="default", agent_ref_mode="agent", agent_ref_id="default",
        ),
    ))
    try:
        rail = build_permission_rail(config)
        for action in actions:
            response = await rail._host.request_permission_confirmation(PermissionConfirmationRequest(
                ctx=SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: "session")),
                tool_call=SimpleNamespace(name=action["name"], arguments=action["params"]),
                result=SimpleNamespace(), auto_confirm_key="",
            ))
            assert response.approved is True
            assert response.auto_confirm is False
    finally:
        AUTHORIZATION_EXECUTION_CONTEXTS.clear()
