"""Smart native paths use real SDK bindings, policy and execution targets."""

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import textwrap
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent
from openjiuwen.core.single_agent.interrupt.state import RESUME_INTERRUPT_ANSWERS_KEY, InterruptAnswer
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.cwd import _cwd_state, init_cwd, set_cwd
from openjiuwen.core.sys_operation.sys_operation import SysOperation, SysOperationCard
from openjiuwen.harness.tools.filesystem import (
    EditFileTool, GlobTool, GrepTool, ListDirTool, ReadFileTool, WriteFileTool,
)
from openjiuwen.harness.rails.interrupt.interrupt_base import InterruptResult

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import invocation_context as inv
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import PermissionHandlingResult
from jiuwenswarm.agents.harness.common.rails.permissions.native_path_context import NATIVE_PATH_ACCESS
from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import OpenJiuwenPolicyEvaluator
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import classify_permission_result
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import build_tool_decision_facts
from jiuwenswarm.agents.harness.common.tools import pdf_tools


@pytest.fixture
def native(tmp_path):
    root = tmp_path / "task"
    work = root / "work"
    work.mkdir(parents=True)
    (root / "outputs").mkdir()
    (work / "inside.txt").write_text("native fixture\n")
    (root / "outside.txt").write_text("outside search base\n")
    old_cwd = _cwd_state.set(None)
    old_access = NATIVE_PATH_ACCESS.set(None)
    init_cwd(str(work), workspace=str(root), project_root=str(root))
    operation = SysOperation(SysOperationCard(
        id=f"native-{tmp_path.name}", work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)]),
    ))
    manager = AbilityManager(owner_id=f"native-{tmp_path.name}")
    tools = [kind(operation) for kind in (
        ReadFileTool, WriteFileTool, EditFileTool, GlobTool, GrepTool, ListDirTool,
    )]
    for tool in tools:
        manager.add_ability(tool.card, tool)
    permissions = {
        "enabled": True, "mode": "auto", "defaults": {"*": "allow"},
        "file_guard": {
            "enabled": True, "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
            "workspace": {"read": "allow", "write": "allow", "exec": "ask"},
        },
    }
    rail = build_permission_rail(
        {"permissions": permissions}, session_id="native-session", workspace_root=root,
        enable_auto_permission=True, installed_permissions=permissions,
    )
    h = SimpleNamespace(root=root, work=work, operation=operation, manager=manager,
                        tools={tool.card.name: tool for tool in tools}, rail=rail, permissions=permissions)
    try:
        yield h
    finally:
        manager.teardown_tools()
        NATIVE_PATH_ACCESS.reset(old_access)
        _cwd_state.reset(old_cwd)


def _invocation(h, name, arguments, *, wire="json"):
    value = json.dumps(arguments) if wire == "json" else dict(arguments)
    call = SimpleNamespace(name=name, arguments=value, id="native-call")
    ctx = SimpleNamespace(
        agent=SimpleNamespace(ability_manager=h.manager),
        inputs=SimpleNamespace(tool_name=name, tool_args=value, tool_call=call),
        extra={}, session=None,
    )
    return inv._extract_invocation((ctx,), {})


def _freeze(h, name, args, *, wire="json"):
    invocation = _invocation(h, name, args, wire=wire)
    return inv._normalize_native_path_invocation_for_execution(invocation, {}, workspace_root=h.root)


@pytest.mark.parametrize("wire", ["json", "object"])
@pytest.mark.parametrize("name,field,raw,action", [
    ("read_file", "file_path", "inside.txt", "read"),
    ("write_file", "file_path", "../outputs/new.txt", "write"),
    ("edit_file", "file_path", "inside.txt", "write"),
    ("list_files", "path", ".", "read"),
    ("grep", "path", "", "read"),
    ("glob", "path", None, "read"),
])
async def test_native_policy_facts_and_live_arguments_agree(native, name, field, raw, action, wire):
    args = {"pattern": "*.txt"} if name in {"glob", "grep"} else {}
    if raw is not None:
        args[field] = raw
    frozen, error = _freeze(native, name, args, wire=wire)
    assert error == ""
    expected = (native.work / (raw or ".")).resolve()
    assert frozen.tool_args[field] == str(expected)
    live = inv._extract_invocation((frozen.ctx,), {})
    assert inv.normalize_invocation_tool_args(name, live.tool_args) == frozen.tool_args
    policy = OpenJiuwenPolicyEvaluator(
        native.rail.base_rail, permission_config_getter=lambda: native.permissions,
    )
    result = await policy.evaluate(frozen)
    assert result.level == "allow"
    facts = build_tool_decision_facts(name, frozen.tool_args, workspace_root=native.root,
                                    original_args_were_valid_object=True)
    assert facts.write_paths if action == "write" else facts.read_paths
    assert (facts.write_paths if action == "write" else facts.read_paths) == (str(expected),)
    # A later cwd change must not move a pending call or change its identity.
    set_cwd(str(native.root / "outputs"))
    again, error = inv._normalize_native_path_invocation_for_execution(frozen, {}, workspace_root=native.root)
    assert not error
    assert again.tool_args == frozen.tool_args


@pytest.mark.parametrize("pattern", ["../*", "**/../*", "{*.txt,../*}", "{*.txt,{ok,../*}}", "/tmp/*", "C:*.txt", r"\\server\share\*", "*" * 2049])
def test_invalid_glob_scope_is_rejected_without_rewriting_pattern(native, pattern):
    original = {"pattern": pattern}
    frozen, error = _freeze(native, "glob", original)
    assert error.startswith("native_glob_")
    assert inv.normalize_invocation_tool_args("glob", frozen.tool_args) == original
    assert NATIVE_PATH_ACCESS.get() is None


def test_expansion_bound_is_checked_before_sdk_recursion(native, monkeypatch):
    def unexpected(_pattern):
        pytest.fail("unbounded input reached SDK expansion")
    monkeypatch.setattr(GlobTool, "_expand_brace_pattern", unexpected)
    for pattern in ("{a,b}" * 7, "{" * 7 + "a" + "}" * 7, "{a,b,c,d,e,f,g,h}"):
        _, error = _freeze(native, "glob", {"pattern": pattern})
        assert error == "native_glob_expansion_limit"


async def test_invalid_glob_stops_in_real_auto_rail_without_confirmation(native, monkeypatch):
    invocation = _invocation(native, "glob", {"pattern": "{*.txt,../*}"})
    async def unexpected(*_args, **_kwargs):
        pytest.fail("invalid native scope reached confirmation or reviewer")
    monkeypatch.setattr(native.rail.base_rail, "before_tool_call", unexpected)
    monkeypatch.setattr(native.rail, "auto_reviewer", SimpleNamespace(assess=unexpected))
    result = await native.rail.before_tool_call(invocation.ctx)
    assert classify_permission_result(result) == "denied"
    assert NATIVE_PATH_ACCESS.get() is None


async def test_projected_native_paths_keep_specific_file_guard_deny(native):
    frozen, error = _freeze(native, "glob", {"pattern": "*.txt"})
    assert not error
    denied = deepcopy(native.permissions)
    denied["file_guard"]["paths"] = [{
        "path": str(native.work), "read": "deny", "match": "prefix",
    }]
    policy = OpenJiuwenPolicyEvaluator(native.rail.base_rail, permission_config_getter=lambda: denied)
    assert (await policy.evaluate(frozen)).level == "deny"


def test_live_native_callback_requires_ability_manager(native):
    invocation = _invocation(native, "read_file", {"file_path": "inside.txt"})
    invocation.ctx.agent.ability_manager = None
    _, error = inv._normalize_native_path_invocation_for_execution(invocation, {}, workspace_root=native.root)
    assert error == "native_path_binding_unavailable"
    assert NATIVE_PATH_ACCESS.get() is None


@pytest.mark.parametrize("failure", ["raises", "non_dict"])
async def test_sdk_snapshot_failure_denies_and_retains_native_projection(native, monkeypatch, failure):
    frozen, error = _freeze(native, "glob", {"pattern": "*.txt", "path": str(native.root.parent)})
    assert not error
    def broken_snapshot():
        if failure == "raises":
            raise RuntimeError("snapshot unavailable")
        return None
    base = native.rail.base_rail
    monkeypatch.setattr(base._host, "get_permissions_snapshot", broken_snapshot)
    result = await base.resolve_interrupt(frozen.ctx, frozen.tool_call, None)
    assert "permission_snapshot_unavailable" in str(result)
    assert base._engine._file_guard.checker is not None


@pytest.mark.parametrize("name", ["glob", "grep", "list_files"])
@pytest.mark.parametrize("permanent", [False, True])
async def test_auto_answer_reenters_native_projection_after_cwd_changes(native, monkeypatch, name, permanent):
    arguments = {"path": str(native.root.parent)}
    if name in {"glob", "grep"}:
        arguments["pattern"] = "*.txt"
    invocation = _invocation(native, name, arguments)
    state, collected, persisted = {}, [], []
    invocation.ctx.session = SimpleNamespace(
        session_id="native-session", get_state=state.get, update_state=state.update,
    )
    base = native.rail.base_rail
    async def delegate_to_base(*_args, **_kwargs):
        # Isolate the existing SDK delegation branch; no model or disk writes.
        # Normalization, wrapper lifetime, base interrupt and persistence are real.
        return PermissionHandlingResult(False, None, "")
    monkeypatch.setattr(native.rail, "_maybe_run_reviewer", delegate_to_base)
    original_collect = base._collect_file_guard_persist_accesses
    def collect(*args):
        assert NATIVE_PATH_ACCESS.get() is not None
        accesses = original_collect(*args)
        collected.append(accesses)
        return accesses
    monkeypatch.setattr(base, "_collect_file_guard_persist_accesses", collect)
    monkeypatch.setattr(base, "_exact_persist_callback", lambda *_args: persisted.append(True) or True)
    monkeypatch.setattr(base._host, "persist_session_allow_rule", lambda *_args, **_kwargs: True)
    with pytest.raises(AbortError) as raised:
        await native.rail.before_tool_call(invocation.ctx)
    assert NATIVE_PATH_ACCESS.get() is None
    pending = inv._extract_invocation((invocation.ctx,), {})
    frozen_args = inv.normalize_invocation_tool_args(name, pending.tool_args)
    assert frozen_args["path"] == str(native.root.parent)
    set_cwd(str(native.root / "outputs"))
    request = raised.value.cause.request
    invocation.ctx.extra[RESUME_INTERRUPT_ANSWERS_KEY] = {"native-call": {request.interrupt_owner: InterruptAnswer(
        owner=request.interrupt_owner, kind=request.interrupt_kind, request=request,
        value={"approved": True, "auto_confirm": True, "persist_allow": permanent},
    )}}
    result = await native.rail.before_tool_call(invocation.ctx)
    assert result is None
    assert collected == [[(native.root.parent.as_posix(), "read")]]
    assert bool(persisted) is True
    assert inv.normalize_invocation_tool_args(name, pending.tool_args) == frozen_args
    assert NATIVE_PATH_ACCESS.get() is None


async def test_sdk_glob_executes_original_bounded_branches(native, monkeypatch):
    calls = []
    fs = native.operation.fs()
    original = fs.search_files
    async def capture(path, pattern, *args, **kwargs):
        calls.append((path, pattern))
        return await original(path, pattern, *args, **kwargs)
    monkeypatch.setattr(fs, "search_files", capture)
    frozen, error = _freeze(native, "glob", {"pattern": "*.{txt,{py,md}}", "path": ""})
    assert not error
    result = await native.tools["glob"].invoke(frozen.tool_args)
    assert result.success
    assert [(Path(path), pattern) for path, pattern in calls] == [
        # Preserve the SDK's duplicate branch for nested braces as well.
        (native.work, "*.txt"), (native.work, "*.py"),
        (native.work, "*.txt"), (native.work, "*.md"),
    ]
    assert result.data["filenames"] == ["inside.txt"]
    assert frozen.tool_args["pattern"] == "*.{txt,{py,md}}"


async def test_native_list_and_write_execute_through_real_fs(native):
    frozen, error = _freeze(native, "write_file", {"file_path": "../outputs/new.txt", "content": "saved"})
    assert not error
    result = await native.tools["write_file"].invoke(frozen.tool_args)
    assert result.success
    assert (native.root / "outputs" / "new.txt").read_text() == "saved"
    frozen, error = _freeze(native, "list_files", {})
    assert not error
    result = await native.tools["list_files"].invoke(frozen.tool_args)
    assert result.success
    assert "inside.txt" in str(result.data)


async def test_tool_deny_and_exact_external_path_survive_projection(native):
    frozen, error = _freeze(native, "glob", {"pattern": "*.txt", "path": str(native.root.parent)})
    assert not error
    base = native.rail.base_rail
    policy = OpenJiuwenPolicyEvaluator(base, permission_config_getter=lambda: native.permissions)
    result = await policy.evaluate(frozen)
    assert result.level == "ask"
    assert result.external_paths == (native.root.parent.as_posix(),)
    assert base._collect_file_guard_persist_accesses("glob", frozen.tool_args, base._engine.config) == [
        (native.root.parent.as_posix(), "read"),
    ]
    denied = deepcopy(native.permissions)
    denied["tools"] = {"glob": "deny"}
    result = await OpenJiuwenPolicyEvaluator(base, permission_config_getter=lambda: denied).evaluate(frozen)
    assert result.level == "deny"


async def test_native_context_does_not_change_concurrent_manual_guard(native):
    manual = build_permission_rail({"permissions": {**native.permissions, "mode": "manual"}})
    original_guard = manual._engine._file_guard
    original_args = {"file_path": "inside.txt"}
    before = await manual._engine.check_permission("read_file", original_args)
    async def smart():
        token = NATIVE_PATH_ACCESS.set(None)
        try:
            _freeze(native, "read_file", original_args)
            await asyncio.sleep(0)
        finally:
            NATIVE_PATH_ACCESS.reset(token)
    async def ordinary():
        await asyncio.sleep(0)
        assert NATIVE_PATH_ACCESS.get() is None
        return await manual._engine.check_permission("read_file", original_args)
    _, after = await asyncio.gather(smart(), ordinary())
    assert after == before
    assert original_args == {"file_path": "inside.txt"}
    assert manual._engine._file_guard is original_guard


@pytest.mark.parametrize("missing", ["cwd", "workspace", "writeback"])
def test_missing_runtime_or_failed_live_writeback_never_issues_access(native, monkeypatch, missing):
    if missing == "cwd":
        _cwd_state.set(None)
    elif missing == "workspace":
        monkeypatch.setattr(inv, "get_workspace", lambda: None)
    else:
        monkeypatch.setattr(inv, "_write_invocation_tool_args", lambda *_args: None)
    _, error = _freeze(native, "read_file", {"file_path": "inside.txt"})
    assert error.startswith("native_path_")
    assert NATIVE_PATH_ACCESS.get() is None


@pytest.mark.parametrize("name,args", [
    ("read_file", {"file_path": "inside.txt"}),
    ("glob", {"pattern": "*.txt"}),
    ("list_files", {}),
])
async def test_real_ability_manager_runs_frozen_native_call(native, monkeypatch, name, args):
    assessed, executed = [], []
    original = native.tools[name].invoke
    async def record_execute(inputs, **kwargs):
        executed.append(dict(inputs))
        return await original(inputs, **kwargs)
    monkeypatch.setattr(native.tools[name], "invoke", record_execute)
    policy = OpenJiuwenPolicyEvaluator(native.rail.base_rail,
                                      permission_config_getter=lambda: native.permissions)
    class Callbacks:
        async def execute(self, event, ctx):
            if event is not AgentCallbackEvent.BEFORE_TOOL_CALL:
                return
            token = NATIVE_PATH_ACCESS.set(None)
            try:
                invocation = inv._extract_invocation((ctx,), {})
                frozen, error = inv._normalize_native_path_invocation_for_execution(
                    invocation, {}, workspace_root=native.root,
                )
                assert not error
                assert (await policy.evaluate(frozen)).level == "allow"
                facts = build_tool_decision_facts(name, frozen.tool_args,
                    workspace_root=native.root, original_args_were_valid_object=True)
                assessed.append((dict(facts.untrusted_args), facts.read_paths))
            finally:
                NATIVE_PATH_ACCESS.reset(token)
    parent = AgentCallbackContext(agent=SimpleNamespace(
        ability_manager=native.manager, agent_callback_manager=Callbacks(),
    ))
    call = ToolCall(id="native-execute", type="function", name=name, arguments=json.dumps(args))
    await native.manager.execute(parent, call, session=SimpleNamespace(session_id="native-execute"))
    assert len(assessed) == len(executed) == 1
    assert assessed[0][0] == executed[0]
    expected = native.work / "inside.txt" if name == "read_file" else native.work
    assert assessed[0][1] == (str(expected),)
    assert NATIVE_PATH_ACCESS.get() is None


def test_same_name_non_native_binding_keeps_its_original_arguments(native):
    class OtherRead(ReadFileTool):
        pass
    other = OtherRead(native.operation)
    native.manager.remove_ability("read_file")
    native.manager.add_ability(other.card, other)
    frozen, error = _freeze(native, "read_file", {"file_path": "inside.txt"})
    assert not error
    assert inv.normalize_invocation_tool_args("read_file", frozen.tool_args) == {"file_path": "inside.txt"}
    assert NATIVE_PATH_ACCESS.get() is None


async def test_read_edit_and_grep_use_actual_workspace_files(native):
    frozen, error = _freeze(native, "read_file", {"file_path": "inside.txt"})
    assert not error
    assert (await native.tools["read_file"].invoke(frozen.tool_args)).success
    frozen, error = _freeze(native, "edit_file", {
        "file_path": "inside.txt", "old_string": "native fixture", "new_string": "updated fixture",
    })
    assert not error
    assert (await native.tools["edit_file"].invoke(frozen.tool_args)).success
    assert (native.work / "inside.txt").read_text() == "updated fixture\n"
    frozen, error = _freeze(native, "grep", {"pattern": "updated", "path": ""})
    assert not error
    result = await native.tools["grep"].invoke(frozen.tool_args)
    assert result.success
    assert "inside.txt" in str(result.data)
    assert frozen.tool_args["pattern"] == "updated"


@pytest.mark.parametrize("level", ["allow", "ask", "deny"])
async def test_pdf_keeps_its_workspace_not_cwd_resolution(native, monkeypatch, level):
    tool = pdf_tools.read_pdf
    registered = Runner.resource_mgr.get_tool(tool.card.id)
    monkeypatch.setattr(tool.card, "stateless", True)
    native.manager.add_ability(tool.card, tool)
    target = native.root / "report.pdf"
    target.write_bytes(b"synthetic PDF path fixture")
    observed = []
    def capture_reader(request):
        # Keep the actual PDF path owner; text extraction is unrelated here.
        observed.append(pdf_tools._resolve_pdf_path(request.pdf_path))
        return "synthetic text"
    monkeypatch.setattr(pdf_tools, "_read_pdf_sync", capture_reader)
    try:
        frozen, error = _freeze(native, "read_pdf", {"pdf_path": "report.pdf"})
        assert not error
        assert frozen.tool_args["pdf_path"] == str(target)
        assert await tool.invoke(frozen.tool_args) == "synthetic text"
        assert observed == [target]
        facts = build_tool_decision_facts("read_pdf", frozen.tool_args,
            workspace_root=native.root, original_args_were_valid_object=True)
        assert facts.read_paths == (str(target),)
        configured = deepcopy(native.permissions)
        configured["file_guard"]["paths"] = [{"path": str(target), "read": level}]
        base = native.rail.base_rail
        policy = OpenJiuwenPolicyEvaluator(base, permission_config_getter=lambda: configured)
        assert (await policy.evaluate(frozen)).level == level
        expected = [(target.as_posix(), "read")] if level == "ask" else []
        assert base._collect_file_guard_persist_accesses("read_pdf", frozen.tool_args, configured) == expected
    finally:
        native.manager.remove(tool.card.name)
        if registered is None:
            Runner.resource_mgr.remove_tool(tool.card.id)


def test_smart_construction_leaves_process_file_registry_unchanged(tmp_path):
    # A fresh interpreter prevents earlier permission tests from hiding a registration leak.
    result = subprocess.run([sys.executable, "-c", textwrap.dedent('''
        import sys
        from pathlib import Path
        from openjiuwen.harness.security.permission_engine.fileguard.file_tool_specs import lookup_file_tool_specs
        from openjiuwen.harness.security.permission_engine.fileguard.file_guard import (
            FileGuardChecker, EffectiveFileGuardConfig, FileGuardAxisDefaults,
        )
        from openjiuwen.harness.security.models import PermissionLevel
        from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
        root = Path(sys.argv[1])
        defaults = FileGuardAxisDefaults(*(PermissionLevel.ASK,) * 3)
        checker = FileGuardChecker(EffectiveFileGuardConfig(True, "native", defaults, (), root))
        args = {"pdf_path": str(root.parent / "outside.pdf")}
        assert lookup_file_tool_specs("read_pdf") is None
        before = checker.evaluate("read_pdf", args)
        config = {"enabled": True, "mode": "auto", "file_guard": {"enabled": True}}
        for _ in range(2):
            rail = build_permission_rail(
                {"permissions": config}, enable_auto_permission=True,
                installed_permissions=config, workspace_root=root,
            )
            assert rail is not None
            assert lookup_file_tool_specs("read_pdf") is None
            assert checker.evaluate("read_pdf", args) == before
            del rail
        assert lookup_file_tool_specs("read_pdf") is None
    '''), str(tmp_path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def native_history(native):
    from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
    from jiuwenswarm.agents.harness.common.rails.permissions.artifact_path_provenance import SessionArtifactPaths
    from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import AutoReviewer
    from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueue
    from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
        RootPermissionQueueRail, bind_root_permission_request, reset_root_permission_request,
    )
    from tests.unit_tests.agentserver.permissions.auto_permission_test_support import PolicyEvaluation, StaticPolicyEvaluator
    from tests.unit_tests.agentserver.permissions.test_auto_permission_pipeline import _Session
    from tests.unit_tests.agentserver.permissions.auto_permission_test_support import StaticReviewerClient

    owner = SessionArtifactPaths("session-a")
    owner.configure(native.root, host_visible=True)
    client = StaticReviewerClient(outcome="allow_once")
    native.rail.session_artifact_paths = owner
    native.rail.policy_evaluator = StaticPolicyEvaluator(PolicyEvaluation(level="ask", reason="ask"))
    native.rail.auto_reviewer = AutoReviewer(client=client)
    queue = RootPermissionQueue()
    queue_rail = RootPermissionQueueRail(queue)
    callbacks = AgentCallbackManager(f"native-history-{id(owner)}")
    agent = SimpleNamespace(ability_manager=native.manager, agent_callback_manager=callbacks)
    session = _Session()
    call_count = 0

    async def execute(name, args):
        nonlocal call_count
        async def before(ctx):
            await queue_rail.before_tool_call(ctx)
            await native.rail.before_tool_call(ctx)
        async def after(ctx):
            await native.rail.after_tool_call(ctx)
        if call_count == 0:
            await callbacks.register_callback(AgentCallbackEvent.BEFORE_TOOL_CALL, before)
            await callbacks.register_callback(AgentCallbackEvent.AFTER_TOOL_CALL, after)
        call_count += 1
        call = ToolCall(id=f"native-history-{call_count}", type="function",
                        name=name, arguments=json.dumps(args))
        parent = AgentCallbackContext(agent=agent, session=session, extra={})
        token = bind_root_permission_request(root_session_id="session-a", request_id="native-history",
                                             enabled=True, queue=queue)
        try:
            return await native.manager.execute(parent, call, session=session)
        finally:
            reset_root_permission_request(token)

    yield SimpleNamespace(**vars(native), owner=owner, client=client, execute=execute)
    owner.dispose()


async def test_successful_native_write_two_edits_and_read_keep_exact_history(native_history):
    h = native_history
    await h.execute("write_file", {"file_path": "../outputs/draft.txt", "content": "one"})
    await h.execute("edit_file", {"file_path": "../outputs/draft.txt", "old_string": "one", "new_string": "two"})
    await h.execute("edit_file", {"file_path": "../outputs/draft.txt", "old_string": "two", "new_string": "three"})
    await h.execute("read_file", {"file_path": "../outputs/draft.txt"})
    assert (h.root / "outputs/draft.txt").read_text() == "three"
    assert len(h.client.requests) == 4
    assert "session_write_evidence" not in h.client.requests[0].review_evidence
    for request in h.client.requests[1:]:
        assert request.to_json_dict()["review_evidence"]["session_write_evidence"] == [{"path": "outputs/draft.txt", "native_write_completed": True}]
        assert "trusted_session_artifact_paths" not in request.review_evidence
    # Only the actual native target enters history.
    assert tuple(h.owner._accepted) == ("outputs/draft.txt",)
    assert not h.owner._pending
    assert NATIVE_PATH_ACCESS.get() is None


async def test_editing_existing_file_does_not_require_history_or_claim_creation(native_history):
    h = native_history
    await h.execute("read_file", {"file_path": "inside.txt"})
    await h.execute("edit_file", {"file_path": "inside.txt", "old_string": "native fixture", "new_string": "existing edit"})
    assert (h.work / "inside.txt").read_text() == "existing edit\n"
    assert len(h.client.requests) == 2
    assert "session_write_evidence" not in h.client.requests[1].review_evidence
    await h.execute("read_file", {"file_path": "inside.txt"})
    assert h.client.requests[2].review_evidence["session_write_evidence"] == [{"path": "work/inside.txt", "native_write_completed": True}]


async def test_history_cannot_bypass_current_file_guard_deny(native_history):
    from tests.unit_tests.agentserver.permissions.auto_permission_test_support import PolicyEvaluation
    h = native_history
    await h.execute("write_file", {"file_path": "../outputs/draft.txt", "content": "one"})
    h.rail.policy_evaluator.result = PolicyEvaluation(level="deny", reason="current_readonly_policy")
    await h.execute("edit_file", {"file_path": "../outputs/draft.txt", "old_string": "one", "new_string": "blocked"})
    assert len(h.client.requests) == 1
    assert (h.root / "outputs/draft.txt").read_text() == "one"
    assert not h.owner._pending


async def test_native_bash_uses_same_frozen_workdir_for_review_and_execution(native_history):
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    h = native_history
    tool = BashTool(h.operation)
    h.manager.add_ability(tool.card, tool)
    (h.work / "sub").mkdir()
    (h.work / "output.txt").write_text("ROOT_INPUT")
    await h.execute("bash", {"command": "cd sub && printf '%s' SUB_OUTPUT > output.txt"})
    request = h.client.requests[0].to_json_dict()
    assert request["review_evidence"]["reviewable_payload"]["workdir"] == str(h.work)
    assert (h.work / "output.txt").read_text() == "ROOT_INPUT"
    assert (h.work / "sub" / "output.txt").read_text() == "SUB_OUTPUT"
    assert not h.owner._accepted


def test_bash_name_alone_does_not_freeze_workdir(native):
    args = {"command": "pwd"}
    invocation = _invocation(native, "bash", args)
    result, error = inv._normalize_command_invocation_for_execution(invocation, {})
    assert not error
    assert result is invocation


@pytest.mark.parametrize("arguments", ["{}", "{invalid"])
def test_native_bash_leaves_empty_or_invalid_arguments_to_existing_validation(native, arguments):
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    tool = BashTool(native.operation)
    native.manager.add_ability(tool.card, tool)
    invocation = _invocation(native, "bash", {})
    invocation.ctx.inputs.tool_args = arguments
    invocation.tool_call.arguments = arguments
    invocation = inv._extract_invocation((invocation.ctx,), {})
    result, error = inv._normalize_command_invocation_for_execution(invocation, {})
    assert not error
    assert result is invocation
    assert invocation.tool_call.arguments == arguments


def test_native_bash_failed_partial_workdir_writeback_still_blocks(native):
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    tool = BashTool(native.operation)
    native.manager.add_ability(tool.card, tool)
    invocation = _invocation(native, "bash", {"command": "pwd"})
    inputs = invocation.ctx.inputs

    class ReadOnlyInputs:
        tool_name = inputs.tool_name
        tool_call = inputs.tool_call

        @property
        def tool_args(self):
            return inputs.tool_args

    invocation.ctx.inputs = ReadOnlyInputs()
    scope = Mock()
    result, error = inv._normalize_command_invocation_for_execution(invocation, {}, shell_path_scope=scope)
    assert error == "command_workdir_writeback_failed"
    scope.bind.assert_not_called()
    assert result is invocation
    assert json.loads(invocation.tool_call.arguments)["workdir"] == str(native.work)


def test_native_bash_freezes_workdir_with_or_without_scope(native):
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    tool = BashTool(native.operation)
    native.manager.add_ability(tool.card, tool)
    for scope in (None, Mock()):
        invocation = _invocation(native, "bash", {"command": "pwd"})

        def check_writeback(bound_tool, args):
            assert bound_tool is tool
            assert args == json.loads(invocation.tool_call.arguments)
            assert args["workdir"] == str(native.work)

        if scope is not None:
            scope.bind.side_effect = check_writeback
        forwarded = {}
        result, error = inv._normalize_command_invocation_for_execution(
            invocation, forwarded, shell_path_scope=scope,
        )
        assert not error and result.tool_args["workdir"] == str(native.work)
        assert "shell_path_scope" not in forwarded
        if scope is not None:
            scope.bind.assert_called_once()


def test_native_bash_preserves_explicit_relative_workdir(native):
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    tool = BashTool(native.operation)
    native.manager.add_ability(tool.card, tool)
    invocation = _invocation(native, "bash", {"command": "pwd", "workdir": "sub"})
    result, error = inv._normalize_command_invocation_for_execution(invocation, {})
    assert not error
    assert result is invocation


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Verified local POSIX executor only")
@pytest.mark.parametrize("sub_exists", [True, False])
async def test_a44_native_bash_reviewer_paths_match_execution(native_history, monkeypatch, sub_exists):
    from openjiuwen.harness.tools.shell.bash._tool import BashTool
    from jiuwenswarm.agents.harness.common.rails.permissions.shell_path_context import current_shell_paths

    pytest.importorskip("openjiuwen.harness.security.permission_engine.fileguard.shell_paths")
    for name in ("CDPATH", "ENV", "BASH_ENV", "SHELLOPTS", "BASHOPTS"):
        monkeypatch.delenv(name, raising=False)
    h = native_history
    tool = BashTool(h.operation)
    h.manager.add_ability(tool.card, tool)
    configured = deepcopy(h.permissions)
    configured["tools"] = {"bash": "ask"}
    configured["file_guard"]["paths"] = [
        {"path": (h.work / "output.txt").as_posix(), "read": "allow", "write": "deny"},
    ]
    h.rail.policy_evaluator = OpenJiuwenPolicyEvaluator(
        h.rail.base_rail, permission_config_getter=lambda: configured,
    )
    if sub_exists:
        (h.work / "sub").mkdir()
    (h.work / "output.txt").write_bytes(b"ROOT_INPUT")
    (h.root / "output.txt").write_bytes(b"PARENT_INPUT")
    command = (
        "cd sub && printf '%s' 'SUB_OUTPUT' > output.txt; echo \"exit=$?\"; "
        "echo \"--- sub/output.txt ---\"; ls -l output.txt; echo \"[content]\"; cat output.txt; echo; "
        "echo \"--- root output.txt (after) ---\"; cd .. && ls -l output.txt && md5sum output.txt "
        "&& echo \"[content]\"; cat output.txt; echo"
    )
    await h.execute("bash", {"command": command})
    wire = h.client.requests[0].to_json_dict()
    evidence = wire["review_evidence"]
    assert evidence["reviewable_payload"]["command"] == command
    assert evidence["path_targets_incomplete"] is False
    targets = evidence["path_targets"]
    writes = [target for target in targets if target["operation"] == "write"]
    assert len(writes) == 1
    assert writes[0]["location"]["relative_path"] == "work/sub/output.txt"
    parent_reads = [
        target for target in targets if target["location"]["relative_path"] == "output.txt"
    ]
    assert len(parent_reads) == 1
    assert parent_reads[0]["requires_prior_directory_change_failure"] is True
    assert wire["descriptor_summary"]["path_counts"]["write"] == 1
    assert (h.work / "output.txt").read_bytes() == b"ROOT_INPUT"
    assert (h.root / "output.txt").read_bytes() == b"PARENT_INPUT"
    child = h.work / "sub" / "output.txt"
    if sub_exists:
        assert child.read_bytes() == b"SUB_OUTPUT"
    else:
        assert not child.exists()
    assert current_shell_paths("bash", evidence["reviewable_payload"]) is None


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Verified local POSIX executor only")
async def test_posix_reviewer_marks_partial_targets_incomplete(native_history, monkeypatch):
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    pytest.importorskip("openjiuwen.harness.security.permission_engine.fileguard.shell_paths")
    for name in ("CDPATH", "ENV", "BASH_ENV", "SHELLOPTS", "BASHOPTS", "A44_VALUE"):
        monkeypatch.delenv(name, raising=False)
    h = native_history
    tool = BashTool(h.operation)
    h.manager.add_ability(tool.card, tool)
    (h.work / "local file").write_text("CONFIRMED")
    (h.work / "legacy.txt").write_text("AFTER_DYNAMIC_WORD")

    await h.execute("bash", {"command": 'cat "local file"; echo "$A44_VALUE"; cat ./legacy.txt'})

    evidence = h.client.requests[0].to_json_dict()["review_evidence"]
    assert evidence["path_targets_incomplete"] is True
    targets = evidence["path_targets"]
    assert len(targets) == 1
    assert targets[0]["location"]["relative_path"] == "work/local file"
    assert targets[0]["operation"] == "read"
    assert targets[0]["requires_prior_directory_change_failure"] is False


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Verified local POSIX executor only")
@pytest.mark.parametrize("layout", ["normal", "outer_logs", "deny_parent", "deny_log"])
async def test_compound_build_reviewer_tracks_second_workdir(native_history, monkeypatch, layout):
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    pytest.importorskip("openjiuwen.harness.security.permission_engine.shell_path_context")
    for name in ("CDPATH", "ENV", "BASH_ENV", "SHELLOPTS", "BASHOPTS"):
        monkeypatch.delenv(name, raising=False)
    h = native_history
    tool = BashTool(h.operation)
    h.manager.add_ability(tool.card, tool)
    configured = deepcopy(h.permissions)
    configured["tools"] = {"bash": "ask"}
    configured["file_guard"]["paths"] = [
        {"path": str(h.work / misleading), "read": "allow", "write": "deny"}
        for misleading in ("app.c", "build.log")
    ]
    if layout in {"deny_parent", "deny_log"}:
        protected = "project" if layout == "deny_parent" else "project/logs/build.log"
        configured["file_guard"]["paths"].append(
            {"path": str(h.work / protected), "read": "allow", "write": "deny"},
        )
    h.rail.policy_evaluator = OpenJiuwenPolicyEvaluator(
        h.rail.base_rail, permission_config_getter=lambda: configured,
    )
    if layout == "outer_logs":
        (h.work / "project").mkdir()
        (h.work / "project/src").write_text("blocker")
        (h.root / "logs").mkdir()
        (h.root / "logs/build.log").write_text("BEFORE\n")
    command = (
        'mkdir -p ./project/src ./project/logs && cd ./project/src && echo "main() {}" > ./app.c '
        '&& ls -la ../non_existent_dir >> ../logs/build.log 2>&1 ; cd ../logs '
        '&& echo "--- Build Check ---" >> ./build.log && cat ./build.log && cd - && pwd'
    )
    results = await h.execute("bash", {"command": command})
    if layout.startswith("deny_"):
        assert not h.client.requests
        assert not (h.work / "project").exists()
        return
    assert results[0][0].success
    evidence = h.client.requests[0].to_json_dict()["review_evidence"]
    assert evidence["path_targets_incomplete"] is False
    paths = {(entry["location"]["relative_path"], entry["operation"], entry["access_kind"])
             for entry in evidence["path_targets"]}
    expected_writes = {
        "work/project/src", "work/project/logs", "work/project/src/app.c",
        "work/project/logs/build.log", "logs/build.log",
    }
    if layout == "normal":
        expected_writes.add("work/project")
    assert {path for path, action, _ in paths if action == "write"} == expected_writes
    assert ("work/project/logs", "read", "directory_change") in paths
    if layout == "outer_logs":
        assert (h.root / "logs/build.log").read_text() == "BEFORE\n--- Build Check ---\n"
        assert not (h.work / "project/logs/build.log").exists()
    else:
        assert (h.work / "project/src/app.c").read_text() == "main() {}\n"
        log = (h.work / "project/logs/build.log").read_text()
        assert "non_existent_dir" in log and log.endswith("--- Build Check ---\n")


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Verified local POSIX executor only")
async def test_complete_shell_paths_still_obey_reviewer_projection_limit(native_history, monkeypatch):
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    pytest.importorskip("openjiuwen.harness.security.permission_engine.shell_path_context")
    for name in ("CDPATH", "ENV", "BASH_ENV", "SHELLOPTS", "BASHOPTS"):
        monkeypatch.delenv(name, raising=False)
    h = native_history
    tool = BashTool(h.operation)
    h.manager.add_ability(tool.card, tool)
    files = [f"file-{index}.txt" for index in range(17)]
    for name in files:
        (h.work / name).touch()
    results = await h.execute("bash", {"command": "cat " + " ".join(files)})
    assert results[0][0].success
    evidence = h.client.requests[0].to_json_dict()["review_evidence"]
    assert evidence["path_targets_incomplete"] is True
    assert [entry["location"]["relative_path"] for entry in evidence["path_targets"]] == [
        f"work/{name}" for name in files[:16]
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows PowerShell")
@pytest.mark.parametrize(("executable", "expected"), [("powershell.exe", "rejected"), ("pwsh.exe", "parsed")])
def test_compound_powershell_is_parsed_by_the_engine_version(executable, expected, tmp_path):
    path = shutil.which(executable)
    if path is None:
        pytest.skip(f"{executable} is unavailable")
    command = (
        r'md -Force ".\Proj Folder\Src", "..\Logs" | Out-Null && sl .\Proj\` Folder/Src '
        r'&& New-Item -Path .\app.cs -Value "test" -Force > ..\Logs\build.log 2>&1 '
        r'&& pushd $env:TEMP && gci .\\\*.tmp >> $HOME\Desktop\temp_list.txt && popd && cd - '
        r'&& gc .\app.cs | Out-File -FilePath ..\Logs/final.txt -Append'
    )
    payload = base64.b64encode(command.encode()).decode()
    probe = f"""
        $src = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))
        $tokens = $null
        $errors = $null
        $null = [System.Management.Automation.Language.Parser]::ParseInput($src,[ref]$tokens,[ref]$errors)
        if ($errors) {{
            'rejected'
            $errors | ForEach-Object {{ $_.Extent.Text }}
        }} else {{ 'parsed' }}
    """
    encoded = base64.b64encode(probe.encode("utf-16-le")).decode()
    result = subprocess.run(
        [path, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        cwd=tmp_path, capture_output=True, text=True, timeout=15, check=True,
    )
    assert result.stdout.splitlines()[0] == expected
    if expected == "rejected":
        assert "&&" in result.stdout.splitlines()
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Requires a local POSIX shell")
@pytest.mark.parametrize(
    "source", ["none", "code", "unmanaged_team", "managed_team", "custom", "parse_failure", "base_changes"],
)
async def test_posix_path_evidence_uses_available_environment(native_history, monkeypatch, source):
    from openjiuwen.agent_teams.rails import team_context
    from openjiuwen.core.sys_operation.execution_environment import ExecutionEnvironment, execution_environment_scope
    from openjiuwen.harness.tools.shell.bash._tool import BashTool
    from jiuwenswarm.agents.harness.code.spec import CodeBuildContext
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext

    pytest.importorskip("openjiuwen.harness.security.permission_engine.fileguard.shell_paths")
    for name in ("CDPATH", "ENV", "BASH_ENV", "SHELLOPTS", "BASHOPTS"):
        monkeypatch.delenv(name, raising=False)
    h = native_history
    tool = BashTool(h.operation)
    h.manager.add_ability(tool.card, tool)
    local = h.work / "sub"
    alternate = h.root / "alternate" / "sub"
    local.mkdir()
    alternate.mkdir(parents=True)
    environment = {"CDPATH": str(alternate.parent)}

    async def env_at_dispatch():
        return ExecutionEnvironment(values=environment)

    async def stored_settings(_):
        return {"settings": {"environment": environment}}

    if source == "code":
        h.manager.execution_environment_resolver = CodeBuildContext().resolve_execution_environment
    elif source in {"unmanaged_team", "managed_team"}:
        backend = SimpleNamespace(db=SimpleNamespace(session_settings=SimpleNamespace(get=stored_settings)))
        monkeypatch.setattr(team_context, "get_team_backend", lambda _: backend)
        context = SwarmBuildContext(mode="team", execution_settings_managed=source == "managed_team")
        h.manager.execution_environment_resolver = context.resolve_execution_environment
    elif source == "custom":
        h.manager.execution_environment_resolver = env_at_dispatch
    elif source == "base_changes":
        assess = h.client.assess

        async def assess_and_change_base(request):
            monkeypatch.setenv("CDPATH", str(alternate.parent))
            return await assess(request)

        monkeypatch.setattr(h.client, "assess", assess_and_change_base)

    analyze = Mock(wraps=tool.analyze_local_paths)
    if source == "parse_failure":
        analyze.side_effect = RuntimeError("parser unavailable")
    monkeypatch.setattr(tool, "analyze_local_paths", analyze)

    with execution_environment_scope(ExecutionEnvironment(values=environment)):
        await h.execute("bash", {"command": "cd sub && printf '%s' DATA > output.txt"})

    late_overlay = source in {"managed_team", "custom"}
    assert analyze.call_count == (0 if late_overlay else 1)
    evidence = h.client.requests[0].review_evidence
    assert evidence["path_targets_incomplete"] is (late_overlay or source == "parse_failure")
    if not evidence["path_targets_incomplete"]:
        assert any(
            entry["operation"] == "write" and entry["location"]["relative_path"] == "work/sub/output.txt"
            for entry in evidence["path_targets"]
        )
    if late_overlay or source == "base_changes":
        assert (alternate / "output.txt").read_bytes() == b"DATA"
        assert not (local / "output.txt").exists()
    else:
        assert (local / "output.txt").read_bytes() == b"DATA"
        assert not (alternate / "output.txt").exists()


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Requires a local POSIX shell")
async def test_posix_late_environment_keeps_legacy_file_guard_deny(native_history):
    from openjiuwen.core.sys_operation.execution_environment import ExecutionEnvironment
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    h = native_history
    tool = BashTool(h.operation)
    h.manager.add_ability(tool.card, tool)
    async def env_at_dispatch():
        return ExecutionEnvironment()

    h.manager.execution_environment_resolver = env_at_dispatch
    blocked = h.work / "blocked.txt"
    config = deepcopy(h.permissions)
    config["file_guard"]["paths"] = [{"path": str(blocked), "write": "deny"}]
    h.rail.policy_evaluator = OpenJiuwenPolicyEvaluator(h.rail.base_rail, permission_config_getter=lambda: config)

    result = await h.execute("bash", {"command": "echo DATA > ./blocked.txt"})

    assert "PERMISSION_DENIED" in str(result)
    assert not h.client.requests
    assert not blocked.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Requires a native Windows shell")
@pytest.mark.parametrize("shell_type", ["auto", "cmd", "powershell"])
async def test_windows_bash_uses_native_compatibility_path(native_history, shell_type):
    from openjiuwen.harness.security.permission_engine.fileguard.path_extract import extract_accesses_native
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    h = native_history
    tool = BashTool(h.operation)
    h.manager.add_ability(tool.card, tool)
    (h.root / "output.txt").write_bytes(b"ROOT_INPUT")
    command = "echo WIN_OUTPUT > output.txt"
    await h.execute("bash", {"command": command, "shell_type": shell_type})
    evidence = h.client.requests[0].to_json_dict()["review_evidence"]
    args = evidence["reviewable_payload"]
    assert Path(args["workdir"]) == h.work
    assert args["shell_type"] == shell_type
    assert evidence["path_targets_incomplete"] is True
    legacy = extract_accesses_native("bash", args, h.root)
    output = (h.work / "output.txt").read_bytes()
    encoding = "utf-16" if output.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    assert output.decode(encoding).strip() == "WIN_OUTPUT"
    assert (h.root / "output.txt").read_bytes() == b"ROOT_INPUT"
    assert (h.work / "output.txt", "write", "shlex") in legacy
    writes = [entry for entry in evidence["path_targets"] if entry["operation"] == "write"]
    assert [entry["location"]["relative_path"] for entry in writes] == ["work/output.txt"]
    assert all("access_kind" not in entry for entry in writes)
    assert all("requires_prior_directory_change_failure" not in entry for entry in writes)
