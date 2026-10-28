"""Process CLI alone must require approval for unresolved command file effects."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.sys_operation.cwd import _cwd_state, init_cwd
from openjiuwen.harness.security.permission_engine.core import PermissionEngine
from openjiuwen.harness.security.permission_engine.host import ToolPermissionHost
from openjiuwen.harness.security.permission_engine.models import PermissionConfirmResponse

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import JiuwenSwarmPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import OpenJiuwenPolicyEvaluator
from jiuwenswarm.channels.process_cli.permissions import (
    ProcessCliPermissionEngine, ProcessCliPermissionInterruptRail,
)
from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT, RunContext


@pytest.fixture
def boundary(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "shared space"
    project.mkdir()
    outside.mkdir()
    token = _cwd_state.set(None)
    init_cwd(str(project), project_root=str(project), workspace=str(project))
    config = {
        "enabled": True, "schema": "tiered_policy", "mode": "manual",
        "package_builtin_rules": False, "defaults": {"*": "allow"},
        "tools": {"bash": "allow", "write_file": "allow"},
        "shell_guard": {"unknown_structure": False, "interpreter_sink": False},
        "file_guard": {"enabled": True,
                       "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
                       "workspace": {"read": "allow", "write": "allow", "exec": "ask"}},
    }
    try:
        yield SimpleNamespace(project=project, outside=outside, config=config)
    finally:
        _cwd_state.reset(token)


def _engine(h, cls=ProcessCliPermissionEngine):
    return cls(config=deepcopy(h.config), workspace_root=h.project, trusted_dirs=[h.project])


@pytest.fixture
def noninteractive():
    token = CURRENT_RUN_CONTEXT.set(RunContext(supports_permission_approval=False))
    try:
        yield
    finally:
        CURRENT_RUN_CONTEXT.reset(token)


@pytest.mark.parametrize("tool_name,arguments", [
    ("write_file", {"file_path": "{outside}/out.txt", "content": "BLOCKED"}),
    ("bash", {"command": "Set-Content -LiteralPath '{outside}/out.txt' -Value BLOCKED"}),
    ("bash", {"command": "$d='{outside}'; Set-Content (Join-Path $d 'out.txt') BLOCKED"}),
    ("bash", {"command": "echo BLOCKED>'{outside}/out.txt'"}),
    ("bash", {"command": "python writer.py"}),
    ("code", {"message": "write outside project"}),
])
async def test_noninteractive_requests_deny_instead_of_asking(boundary, noninteractive, tool_name, arguments):
    args = {key: value.format(outside=boundary.outside.as_posix()) for key, value in arguments.items()}
    result = await _engine(boundary).check_permission(tool_name, args)
    assert result.permission.value == "deny"
    assert "process_cli:noninteractive_approval_denied" in result.matched_rule
    assert not getattr(result, "manual_approval_required", False)
    assert not (boundary.outside / "out.txt").exists()


async def test_noninteractive_requests_keep_trusted_writes_and_disabled_guards(boundary, noninteractive):
    engine = _engine(boundary)
    for tool_name, args in [
        ("write_file", {"file_path": str(boundary.project / "ok.txt"), "content": "OK"}),
        ("bash", {"command": f"Set-Content -LiteralPath '{boundary.project / 'ok.txt'}' -Value OK"}),
    ]:
        assert (await engine.check_permission(tool_name, args)).permission.value == "allow"
    for disabled in ["permissions", "file_guard"]:
        config = deepcopy(boundary.config)
        if disabled == "permissions":
            config["enabled"] = False
        else:
            config["file_guard"]["enabled"] = False
        engine = ProcessCliPermissionEngine(config=config, workspace_root=boundary.project)
        assert (await engine.check_permission("bash", {"command": "python writer.py"})).permission.value == "allow"


@pytest.mark.parametrize("tool_name", ["write_file", "bash"])
async def test_noninteractive_rejection_cannot_be_replaced_by_scene_or_cached_approval(
    boundary, noninteractive, tool_name,
):
    async def scene(_inp):
        return ("approve",)

    async def confirm(_request):
        pytest.fail("Non-interactive execution must not request approval")

    rail = ProcessCliPermissionInterruptRail(
        config=boundary.config, tool_names=[], llm=None, model_name=None,
        host=ToolPermissionHost(resolve_workspace_dir=lambda: boundary.project,
                                permission_scene_hook=scene, request_permission_confirmation=confirm),
    )
    args = ({"file_path": str(boundary.outside / "out.txt"), "content": "BLOCKED"}
            if tool_name == "write_file" else {"command": "python writer.py"})
    call = ToolCall(id="call", type="function", name=tool_name, arguments=json.dumps(args))
    ctx = SimpleNamespace(session=None, extra={})
    decision = await rail.resolve_interrupt(
        ctx, call, {"approved": True}, {"": True, "bash_python": True},
    )
    assert type(decision).__name__ == "RejectResult"


async def test_noninteractive_smart_policy_returns_deny_before_review(boundary, noninteractive):
    rail = ProcessCliPermissionInterruptRail(
        config={**boundary.config, "mode": "auto"}, tool_names=[], llm=None, model_name=None,
        host=ToolPermissionHost(resolve_workspace_dir=lambda: boundary.project),
    )
    args = {"command": "python writer.py"}
    invocation = SimpleNamespace(tool_name="bash", tool_args=args,
                                 tool_call=ToolCall(id="call", type="function", name="bash", arguments=json.dumps(args)), ctx=None)
    result = await OpenJiuwenPolicyEvaluator(rail).evaluate(invocation)
    assert result.level == "deny"


@pytest.mark.parametrize("command,old_level", [
    ("$d='{outside}'; Set-Content -LiteralPath (Join-Path $d 'out.txt') -Value OK", "allow"),
    ('echo OK>"{outside}/out.txt"', "allow"),
    ("python -c \"from pathlib import Path; Path('{outside}/out.txt').write_text('OK')\"", "allow"),
    ("[System.IO.File]::WriteAllText('{outside}/out.txt', 'OK')", "allow"),
    ('echo OK > "$env:TARGET"', "allow"),
    ("./writer-script", "allow"),
    ("less -o '{outside}/out.txt' README.md", "ask"),
    ("rm --files0-from list.txt", "allow"),
])
async def test_unknown_shell_effects_ask_only_in_process_cli(boundary, command, old_level):
    args = {"command": command.format(outside=boundary.outside.as_posix()), "shell_type": "powershell"}
    old = await _engine(boundary, PermissionEngine).check_permission("bash", args)
    result = await _engine(boundary).check_permission("bash", args)
    assert old.permission.value == old_level
    assert result.permission.value == "ask"
    assert result.manual_approval_required
    assert "process_cli:unresolved_file_access" in result.matched_rule
    assert not (boundary.outside / "out.txt").exists()


async def test_literal_paths_still_use_existing_policy(boundary):
    engine = _engine(boundary)
    for target, expected in [(boundary.project / "ok.txt", "allow"), (boundary.outside / "out.txt", "ask")]:
        result = await engine.check_permission("bash", {
            "command": f"Set-Content -LiteralPath '{target}' -Value OK",
        })
        assert result.permission.value == expected
        assert not getattr(result, "manual_approval_required", False)
    result = await engine.check_permission("bash", {"command": "echo OK"})
    assert result.permission.value == "allow"


async def test_explicit_deny_and_disabled_guards_keep_their_meaning(boundary):
    config = deepcopy(boundary.config)
    config["tools"]["bash"] = "deny"
    engine = ProcessCliPermissionEngine(config=config, workspace_root=boundary.project)
    assert (await engine.check_permission("bash", {"command": "python script.py"})).permission.value == "deny"
    for disabled in ["permissions", "file_guard"]:
        config = deepcopy(boundary.config)
        if disabled == "permissions":
            config["enabled"] = False
        else:
            config["file_guard"]["enabled"] = False
        engine = ProcessCliPermissionEngine(config=config, workspace_root=boundary.project)
        assert (await engine.check_permission("bash", {"command": "python script.py"})).permission.value == "allow"


@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("channel_id", ["process_cli", "web", "tui", "acp", "a2a", "feishu", ""])
def test_builder_selects_cli_guard_without_changing_other_channels(boundary, monkeypatch, channel_id, smart):
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
    monkeypatch.setattr(permissions_layers, "load_user_permissions", lambda: {})
    monkeypatch.setattr(permissions_layers, "load_session_permissions", lambda _sid: {})
    original = deepcopy(boundary.config)
    config = {**boundary.config, "mode": "auto" if smart else "manual"}
    rail = build_permission_rail(
        {"permissions": config}, session_id="boundary", channel_id=channel_id,
        enable_auto_permission=smart, installed_permissions=config if smart else None,
        workspace_root=boundary.project,
    )
    assert rail is not None
    if smart:
        rail = rail.base_rail
    assert isinstance(rail, ProcessCliPermissionInterruptRail) is (channel_id == "process_cli")
    if channel_id != "process_cli":
        assert type(rail) is JiuwenSwarmPermissionInterruptRail
        assert type(rail._engine) is PermissionEngine
    assert boundary.config == original


@pytest.mark.parametrize("tool_name", ["code", "acp_chat", "subagent_spawn", "subagent_resume", "subagent_send_input"])
async def test_delegated_execution_cannot_silently_replace_a_blocked_shell(boundary, tool_name):
    result = await _engine(boundary).check_permission(tool_name, {"message": "write outside project"})
    assert result.permission.value == "ask"
    assert result.manual_approval_required


async def test_user_approval_is_once_and_cannot_be_replaced_by_scene_allow(boundary):
    asked = []
    async def scene(_inp):
        return ("approve",)
    async def confirm(request):
        asked.append(request)
        return PermissionConfirmResponse(approved=True, auto_confirm=True, persist_allow=True)
    rail = ProcessCliPermissionInterruptRail(
        config=boundary.config, tool_names=[], llm=None, model_name=None,
        host=ToolPermissionHost(resolve_workspace_dir=lambda: boundary.project,
                                permission_scene_hook=scene, request_permission_confirmation=confirm),
    )
    call = ToolCall(id="call", type="function", name="bash", arguments=json.dumps({"command": "python script.py"}))
    ctx = SimpleNamespace(session=None, extra={})
    for _ in range(2):
        decision = await rail.resolve_interrupt(ctx, call, None, {"bash_python": True, "": True})
        assert type(decision).__name__ == "ApproveResult"
    assert len(asked) == 2
    assert all(req.auto_confirm_key == "" for req in asked)
    assert rail.installed_permission_config() == boundary.config


async def test_unknown_command_prompt_shows_arguments_and_only_once_choices(boundary):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import _question_options_from_ui_options
    rail = ProcessCliPermissionInterruptRail(
        config=boundary.config, tool_names=[], llm=None, model_name=None,
        host=ToolPermissionHost(resolve_workspace_dir=lambda: boundary.project),
    )
    args = {"command": "python script.py"}
    call = ToolCall(id="call", type="function", name="bash", arguments=json.dumps(args))
    result = await rail._engine.check_permission("bash", args)
    message = rail._build_message(call, result)
    metadata = rail._build_interrupt_metadata(call, result)
    options = _question_options_from_ui_options({"metadata": metadata}, "permission_interrupt", "bash", message)
    assert "python script.py" in message
    assert [option["value"] for option in options] == ["allow_once", "reject"]
    original_options = _question_options_from_ui_options({}, "permission_interrupt", "bash", "old prompt")
    assert len(original_options) == 4


async def test_smart_policy_preserves_manual_marker(boundary):
    rail = ProcessCliPermissionInterruptRail(
        config=boundary.config, tool_names=[], llm=None, model_name=None,
        host=ToolPermissionHost(resolve_workspace_dir=lambda: boundary.project),
    )
    args = {"command": "python script.py"}
    invocation = SimpleNamespace(tool_name="bash", tool_args=args,
                                 tool_call=ToolCall(id="call", type="function", name="bash", arguments=json.dumps(args)), ctx=None)
    result = await OpenJiuwenPolicyEvaluator(rail).evaluate(invocation)
    assert result.level == "ask"
    assert result.manual_approval_required


async def test_smart_unknown_command_never_reaches_automatic_reviewer(boundary):
    from tests.unit_tests.agentserver.permissions.auto_permission_test_support import (
        AutoPermissionInterruptRail, AutoReviewer, FakeBaseRail, PolicyEvaluation,
        ReviewerOutcome, StaticPolicyEvaluator, StaticReviewerClient,
    )
    class ManualBase(FakeBaseRail):
        async def before_tool_call(self, ctx, tool_call):
            self.calls.append((tool_call.name, tool_call.arguments))
            return {"interrupt": True, "value": {"message": "human approval required"}}

    base = ManualBase()
    reviewer = StaticReviewerClient(outcome=ReviewerOutcome.ALLOW_ONCE)
    rail = AutoPermissionInterruptRail(
        base_rail=base, permission_config={"enabled": True, "mode": "auto"},
        workspace_root=boundary.project,
        policy_evaluator=StaticPolicyEvaluator(PolicyEvaluation(
            level="ask", reason="unresolved", manual_approval_required=True,
        )),
        auto_reviewer=AutoReviewer(client=reviewer),
    )
    result = await rail.before_tool_call(
        tool_name="bash", tool_args={"command": "python script.py"}, session_id="cli-session",
    )
    assert result["interrupt"] is True
    assert len(base.calls) == 1
    assert reviewer.requests == []
