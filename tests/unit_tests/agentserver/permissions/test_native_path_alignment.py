"""Smart native paths use real SDK bindings, policy and execution targets."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
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
async def test_sdk_snapshot_fallback_retains_projected_search_base(native, monkeypatch, failure):
    frozen, error = _freeze(native, "glob", {"pattern": "*.txt", "path": str(native.root.parent)})
    assert not error
    def broken_snapshot():
        if failure == "raises":
            raise RuntimeError("snapshot unavailable")
        return None
    base = native.rail.base_rail
    monkeypatch.setattr(base._host, "get_permissions_snapshot", broken_snapshot)
    result = await base.resolve_interrupt(frozen.ctx, frozen.tool_call, None)
    assert isinstance(result, InterruptResult)
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
    with pytest.raises(AbortError):
        await native.rail.before_tool_call(invocation.ctx)
    assert NATIVE_PATH_ACCESS.get() is None
    pending = inv._extract_invocation((invocation.ctx,), {})
    frozen_args = inv.normalize_invocation_tool_args(name, pending.tool_args)
    assert frozen_args["path"] == str(native.root.parent)
    set_cwd(str(native.root / "outputs"))
    invocation.ctx.extra[RESUME_USER_INPUT_KEY] = {
        "approved": True, "auto_confirm": True, "persist_allow": permanent,
    }
    result = await native.rail.before_tool_call(invocation.ctx)
    assert result is None
    assert collected == [[(str(native.root.parent), "read")]]
    assert bool(persisted) is permanent
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
    assert result.external_paths == (str(native.root.parent),)
    assert base._collect_file_guard_persist_accesses("glob", frozen.tool_args, base._engine.config) == [
        (str(native.root.parent), "read"),
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


async def test_pdf_keeps_its_workspace_not_cwd_resolution(native, monkeypatch):
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
    finally:
        native.manager.remove(tool.card.name)
        if registered is None:
            Runner.resource_mgr.remove_tool(tool.card.id)
