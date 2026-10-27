import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
    convert_interactions_to_ask_user_question,
    convert_interactions_to_ask_user_questions,
)


@pytest.fixture
def channel_permission_case(monkeypatch, tmp_path):
    from contextvars import ContextVar
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from jiuwenswarm.common import utils
    from jiuwenswarm.common.invocation_context import InvocationContext, runtime
    from jiuwenswarm.agents.harness.common.rails.permissions import (
        permissions_persist, tool_permission_context,
    )

    monkeypatch.setattr(utils, "get_config_file", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(utils, "get_workspace_dir", lambda: tmp_path)

    def create(channel, behavior_enabled=False, use_invocation=True, permission="ask"):
        monkeypatch.setenv("CLAW_BEHAVIOR_SECURITY", "1" if behavior_enabled else "0")
        monkeypatch.setenv("CLAW_SKILL_TOKEN", "permission-regression-token")
        invocation = InvocationContext(
            version=1, invocation_id="approval-test", request_id="approval-test",
            session_id=None, channel_id=channel, chat_id=None,
        ) if use_invocation else None
        monkeypatch.setattr(runtime, "get_current_invocation_context", lambda: invocation)
        # The actual invocation must take precedence over the legacy web fallback.
        monkeypatch.setattr(tool_permission_context, "TOOL_PERMISSION_CHANNEL_ID", ContextVar(
            "test_permission_channel", default="web" if use_invocation else channel,
        ))
        permissions = {"enabled": True, "package_builtin_rules": False, "tools": {"bash": permission}}
        monkeypatch.setattr(permissions_persist, "get_permissions_with_session_overlay", lambda **kw: permissions)
        rail = build_permission_rail({"permissions": permissions})
        assert rail is not None
        call = ToolCall(id="approval-call", type="function", name="bash",
                        arguments='{"command":"echo permission-regression"}')
        return rail, SimpleNamespace(session=None, extra={}), call

    return create


@pytest.mark.parametrize("behavior_enabled", [False, True])
@pytest.mark.parametrize("use_invocation", [False, True])
def test_desktop_ask_interrupts_until_explicit_confirmation(
    channel_permission_case, behavior_enabled, use_invocation,
):
    from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult, InterruptResult, RejectResult

    rail, ctx, call = channel_permission_case("desktop", behavior_enabled, use_invocation)
    decision = asyncio.run(rail.resolve_interrupt(ctx, call, None))
    assert isinstance(decision, InterruptResult), "desktop ASK must not silently approve"
    assert decision.request.metadata["matched_rule"] == "tools.bash"
    assert isinstance(asyncio.run(rail.resolve_interrupt(ctx, call, {"approved": False})), RejectResult)
    assert isinstance(asyncio.run(rail.resolve_interrupt(ctx, call, {"approved": True})), ApproveResult)


@pytest.mark.parametrize("channel", ["unknown-client", "websocket", ""])
@pytest.mark.parametrize("behavior_enabled", [False, True])
def test_unknown_channel_ask_fails_closed(channel_permission_case, channel, behavior_enabled):
    from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult

    rail, ctx, call = channel_permission_case(channel, behavior_enabled)
    decision = asyncio.run(rail.resolve_interrupt(ctx, call, None))
    assert isinstance(decision, RejectResult), "ASK without an approval channel must fail closed"
    assert "[PERMISSION_DENIED]" in decision.tool_result


@pytest.mark.parametrize("channel", ["desktop", "unknown-client"])
@pytest.mark.parametrize("permission", ["allow", "deny"])
def test_channel_fix_preserves_explicit_allow_and_deny(channel_permission_case, channel, permission):
    from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult, RejectResult

    rail, ctx, call = channel_permission_case(channel, permission=permission)
    decision = asyncio.run(rail.resolve_interrupt(ctx, call, None))
    assert isinstance(decision, ApproveResult if permission == "allow" else RejectResult)


@pytest.mark.parametrize("scope", ["parent", "domain", "subcommands"])
def test_permission_scope_options_reach_frontend(scope):
    scopes = [{"value": "exact", "label": "当前对象"}, {"value": scope, "label": "范围授权"}]
    interaction = SimpleNamespace(id="approval", value={
        "tool_name": "read_file", "message": "需要授权", "tool_args": {"path": "/tmp/a"},
        "metadata": {"source": "permission_interrupt", "authorization_scopes": scopes},
    })
    result = convert_interactions_to_ask_user_question([interaction])
    assert result["questions"][0]["authorization_scopes"] == scopes


@pytest.mark.parametrize("scope", ["parent", "domain", "subcommands"])
def test_permission_scope_reply_reaches_confirmation_payload(scope):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    result = JiuWenSwarm._build_interactive_input_from_answers("approval", [{
        "selected_options": ["session_allow"], "authorization_mode": "allow_with_scope", "authorization_scope": scope,
    }], source="permission_interrupt")
    assert result.user_inputs["approval"] == {
        "approved": True, "auto_confirm": True, "persist_allow": False, "feedback": "",
        "authorization_mode": "allow_with_scope", "authorization_scope": scope,
    }


@pytest.mark.parametrize("fields", [{}, {"authorization_scope": "exact"}, {"authorization_mode": "allow_with_scope"}])
def test_permission_reply_keeps_omitted_scope_fields_for_server_defaults(fields):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    result = JiuWenSwarm._build_interactive_input_from_answers("approval", [{
        "selected_options": ["session_allow"], **fields,
    }], source="permission_interrupt")
    assert result.user_inputs["approval"] == {
        "approved": True, "auto_confirm": True, "persist_allow": False, "feedback": "", **fields,
    }


def test_shell_chain_is_listed_in_one_unchanged_approval_request():
    options = [
        {"label": "本次允许", "value": "allow_once"},
        {"label": "会话内记住", "value": "session_allow"},
        {"label": "永久记住", "value": "always_allow"},
        {"label": "拒绝", "value": "reject"},
    ]
    scopes = [{"value": "exact", "label": "仅当前对象"}]
    args = {"command": 'echo "a && b" && dir', "shell_type": "cmd"}
    interaction = {"id": "chain-request", "value": {
        "tool_name": "mcp_exec_command", "tool_call_id": "original-call", "tool_args": args,
        "message": "read C:/workspace\n\n> 永久记住写入配置。",
        "metadata": {"ask_title": "检测到受保护的文件路径访问，需要确认后才能执行", "authorization_scopes": scopes},
        "ui_options": options,
    }}

    payloads = convert_interactions_to_ask_user_questions([interaction])
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["request_id"] == "chain-request"
    assert payload["source"] == "permission_interrupt"
    assert len(payload["questions"]) == 1
    question = payload["questions"][0]
    assert question["header"] == "助手想要在你的设备上运行程序指令"
    assert 'echo "a && b" &&\ndir' in question["question"]
    assert "永久记住写入配置" in question["question"]
    assert "检测到受保护的文件路径访问" in question["question"]
    assert question["tool_call_id"] == "original-call"
    assert question["tool_args"] == args
    assert question["authorization_scopes"] == scopes
    assert [option["value"] for option in question["options"]] == [option["value"] for option in options]


@pytest.mark.parametrize("explicit_workdir", [False, True])
def test_acp_shell_prompt_keeps_one_request_and_existing_option_ids(monkeypatch, tmp_path, explicit_workdir):
    from openjiuwen.harness.security.host import PermissionConfirmationRequest
    from openjiuwen.harness.security.models import PermissionLevel, PermissionResult
    from openjiuwen.harness.security.permission_engine.approve import operation_grants

    from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
        TOOL_PERMISSION_CHANNEL_ID,
    )
    from jiuwenswarm.agents.harness.common.tools import acp_output_tools
    from jiuwenswarm.common import utils

    monkeypatch.setattr(utils, "get_config_file", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(utils, "get_workspace_dir", lambda: tmp_path)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(operation_grants, "get_cwd", lambda: str(runtime_dir))

    sent = []

    async def send_request(method, params, *, session_id):
        sent.append((method, params, session_id))
        return {"result": {"outcome": {"outcome": "selected", "optionId": "allow-once"}}}

    monkeypatch.setattr(acp_output_tools, "get_acp_output_manager", lambda: SimpleNamespace(
        send_jsonrpc_request=send_request,
    ))
    rail = build_permission_rail({"permissions": {"enabled": True}})
    args = {"command": "cd .. && dir", "shell_type": "cmd"}
    if explicit_workdir:
        args["workdir"] = str(tmp_path / "explicit")
    original = dict(args)
    request = PermissionConfirmationRequest(
        ctx=SimpleNamespace(session=SimpleNamespace(session_id="acp-session")),
        tool_call=SimpleNamespace(id="original-call", name="mcp_exec_command", arguments=args),
        result=PermissionResult(PermissionLevel.ASK, matched_rule="tools.mcp_exec_command", reason="需要确认目录访问"),
        auto_confirm_key="original-key",
    )
    token = TOOL_PERMISSION_CHANNEL_ID.set("acp")
    try:
        response = asyncio.run(rail._host.request_permission_confirmation(request))
    finally:
        TOOL_PERMISSION_CHANNEL_ID.reset(token)

    assert response.approved and not response.auto_confirm
    assert len(sent) == 1
    method, params, session_id = sent[0]
    assert method == "session/request_permission" and session_id == "acp-session"
    assert params["toolCall"]["title"] == "助手想要在你的设备上运行程序指令"
    assert params["toolCall"]["rawInput"] == args
    assert params["toolCall"]["toolCallId"] == "original-call"
    assert "cd .. &&\ndir" in params["toolCall"]["content"][0]["content"]["text"]
    assert "需要确认目录访问" in params["toolCall"]["content"][0]["content"]["text"]
    body = params["toolCall"]["content"][0]["content"]["text"]
    expected = tmp_path / "explicit" if explicit_workdir else runtime_dir
    assert body.count("工作目录：") == 1 and str(expected.resolve()) in body
    assert args == original
    assert [option["optionId"] for option in params["options"]] == [
        "allow-once", "allow-session", "allow-always", "reject-once",
    ]


def test_xiaoyi_receives_split_command_prompt_and_one_pending_approval():
    from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import XiaoyiChannel

    payload = convert_interactions_to_ask_user_question([{
        "id": "chain-request",
        "value": {
            "tool_name": "mcp_exec_command",
            "tool_args": {"command": "cd .. && dir", "shell_type": "cmd"},
            "message": "read C:/workspace",
            "metadata": {"ask_title": "检测到受保护的文件路径访问，需要确认后才能执行"},
        },
    }])
    sent = []

    async def send_text(session_id, task_id, text, url_key, **flags):
        sent.append((text, flags))

    channel = SimpleNamespace(
        _pending_approvals={}, _ws_connections={"mock": object()}, _send_text_response=send_text,
    )
    message = SimpleNamespace(
        payload=payload, metadata={"xiaoyi_conversation_id": "conversation"}, session_id="conversation",
    )
    asyncio.run(XiaoyiChannel._send_ask_user_question_prompt(channel, message, "session", "task"))

    assert len(channel._pending_approvals) == 1
    assert channel._pending_approvals["conversation"]["request_id"] == "chain-request"
    assert channel._pending_approvals["conversation"]["source"] == "permission_interrupt"
    assert len(sent) == 1
    prompt, flags = sent[0]
    # 结构化选项卡形态：text part 只放问题正文（与卡片 question 原文一致），
    # 标题/可选项/回复约定话术由 data part 的 ToolExecute/Invoke 卡承载。
    assert "cd .. &&\ndir" in prompt
    assert "本次授权适用于整组指令" in prompt
    assert "本次允许 / 会话内记住 / 永久记住 / 拒绝" not in prompt
    card = flags["extra_part"]["data"]["commands"][0]
    assert card["header"] == {"namespace": "ToolExecute", "name": "Invoke"}
    question = card["payload"]["questions"][0]
    assert prompt == question["question"]
    assert question["question_id"] == "chain-request"
    assert [option["label"] for option in question["options"]] == [
        "本次允许", "会话内记住", "永久记住", "拒绝",
    ]
    assert flags["is_final"] is False


def test_core_permission_message_reaches_ws_with_complete_file_list(tmp_path):
    from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
    from openjiuwen.harness.security.models import PermissionLevel, PermissionResult

    source, destination, notes = tmp_path / "报告.txt", tmp_path / "备份.txt", tmp_path / "说明.txt"
    args = {
        "command": f'copy "{source.as_posix()}" "{destination.as_posix()}" && type "{notes.as_posix()}"',
        "workdir": str(tmp_path), "shell_type": "cmd", "description": "备份报告并读取说明",
    }
    call = SimpleNamespace(id="original-copy", name="mcp_exec_command", arguments=args)
    result = PermissionResult(PermissionLevel.ASK, matched_rule="file_guard:defaults", external_paths=[str(destination)])
    rail = PermissionInterruptRail(config={"enabled": True})
    message = rail._build_message(call, result)
    metadata = rail._build_interrupt_metadata(call, result)
    assert len(metadata["file_operations"]) == 3
    assert metadata["ask_risk_level"] == "高"
    payloads = convert_interactions_to_ask_user_questions([{
        "id": "copy-approval", "value": {
            "tool_name": call.name, "tool_call_id": call.id,
            "tool_args": args, "message": message, "metadata": metadata,
        },
    }])
    assert len(payloads) == 1 and len(payloads[0]["questions"]) == 1
    question = payloads[0]["questions"][0]
    body = question["question"]
    for path, action in [(source, "读取"), (destination, "写入"), (notes, "读取")]:
        assert f"- {action} " in body and path.name in body
        assert any(op["path"] == str(path.resolve()) for op in metadata["file_operations"])
    assert body.count("涉及的文件操作") == 1 and body.count("风险等级") == 1
    assert body.index("助手想要") < body.index("工具 `") < body.index("涉及的文件操作") < body.index("风险等级")
    assert "**风险等级：高风险**" in body
    assert "默认按可用范围授权" in body
    assert "命令为可独立或组合复用的子命令" in body
    assert question["tool_args"] == args and question["tool_call_id"] == call.id
    assert payloads[0]["request_id"] == "copy-approval"


def test_ordinary_permission_keeps_captured_cwd_across_context_change(tmp_path, monkeypatch):
    from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
    from openjiuwen.harness.security.models import PermissionLevel, PermissionResult
    from openjiuwen.harness.security.permission_engine.approve import operation_grants

    initial_dir = tmp_path / "project"
    later_dir = tmp_path / "later"
    monkeypatch.setattr(operation_grants, "get_cwd", lambda: str(initial_dir))
    args = {"command": "chmod -R a-w rel_out_action_008.txt && echo APPROVAL_ACT008 > rel_out_action_008.txt"}
    call = SimpleNamespace(id="action-008", name="bash", arguments=args)
    result = PermissionResult(PermissionLevel.ASK, matched_rule="tools.bash")
    rail = PermissionInterruptRail(config={"enabled": True})
    message = rail._build_message(call, result)
    metadata = rail._build_interrupt_metadata(call, result)
    monkeypatch.setattr(operation_grants, "get_cwd", lambda: str(later_dir))
    payloads = convert_interactions_to_ask_user_questions([{
        "id": "cwd-approval", "value": {
            "tool_name": call.name, "tool_call_id": call.id,
            "tool_args": args, "message": message, "metadata": metadata,
        },
    }])
    question = payloads[0]["questions"][0]
    assert payloads[0]["source"] == "permission_interrupt"
    assert question["question"].count("工作目录：") == 1
    assert str(initial_dir.resolve()) in question["question"]
    assert str(later_dir.resolve()) not in question["question"]
    assert question["tool_args"] == args and "workdir" not in args
    assert question["tool_call_id"] == call.id
    assert question["authorization_scopes"] == metadata["authorization_scopes"]


def _evolution_interrupt(
    tool_name: str,
    operation: str,
    *,
    metadata: dict | None = None,
):
    if operation == "evolve":
        message = "是否批准 Skill 'demo-skill' 的 1 条演进经验？"
    else:
        message = "是否执行 Skill 'demo-skill' 的 1 项经验精简操作？"

    value = {
        "message": message,
        "tool_name": tool_name,
        "metadata": metadata
        or {
            "source": "evolution_interrupt",
            "interrupt_kind": "skill_evolution_approval",
        },
        "ui_options": [
            {"label": "本次允许", "value": "allow_once", "description": "允许本次技能演进变更执行"},
            {"label": "总是允许", "value": "allow_always", "description": "自动允许后续匹配的技能演进变更"},
            {"label": "拒绝", "value": "reject", "description": "跳过本次技能演进变更"},
        ],
    }
    return SimpleNamespace(
        id="call_123",
        value=value,
    )


@pytest.mark.parametrize(
    ("tool_name", "operation", "approval_kind", "question"),
    [
        (
            "simplify_skill_experiences",
            "simplify",
            "simplify",
            "是否执行 Skill 'demo-skill' 的 1 项经验精简操作？",
        ),
        (
            "evolve_skill_experiences",
            "evolve",
            "evolve",
            "是否批准 Skill 'demo-skill' 的 1 条演进经验？",
        ),
    ],
)
def test_structured_evolution_approval_interrupt_is_classified(
    tool_name,
    operation,
    approval_kind,
    question,
):
    interaction = _evolution_interrupt(tool_name, operation)

    result = convert_interactions_to_ask_user_question([interaction])

    assert result is not None
    assert result["source"] == "evolution_interrupt"
    assert result["approval_kind"] == approval_kind
    assert "approval_schema" not in result
    assert "evolution_meta" not in result
    assert "rail_kind" not in result
    assert "approval_detail" not in result["questions"][0]
    assert result["questions"][0]["question"] == question
    assert [option["value"] for option in result["questions"][0]["options"]] == [
        "allow_once",
        "allow_always",
        "reject",
    ]


def test_skill_evolution_tool_name_without_detail_is_classified():
    interaction = SimpleNamespace(
        id="call_123",
        value={
            "message": "Skill evolution approval required.",
            "tool_name": "simplify_skill_experiences",
        },
    )

    result = convert_interactions_to_ask_user_question([interaction])

    assert result is not None
    assert result["source"] == "evolution_interrupt"
    assert result["approval_kind"] == "simplify"
    assert result["questions"][0]["question"] == "Skill evolution approval required."


def test_legacy_skill_evolution_approval_metadata_is_classified():
    interaction = _evolution_interrupt(
        "evolve_skill_experiences",
        "evolve",
        metadata={"source": "skill_evolution_approval"},
    )

    result = convert_interactions_to_ask_user_question([interaction])

    assert result is not None
    assert result["source"] == "evolution_interrupt"
    assert result["approval_kind"] == "evolve"


def test_convert_interactions_to_ask_user_questions_keeps_all_valid_items_in_order():
    """A batched interrupt must not drop later approval requests."""
    interactions = [
        {
            "id": "permission-1",
            "value": {
                "message": "**工具 `write_file` 需要授权才能执行**",
                "tool_name": "write_file",
            },
        },
        {
            "id": "ask-user-2",
            "value": {
                "questions": [{"question": "继续吗？", "header": "确认"}],
            },
        },
        {"id": "", "value": {"message": "invalid without id"}},
    ]

    payloads = convert_interactions_to_ask_user_questions(interactions)

    assert [payload["request_id"] for payload in payloads] == [
        "permission-1",
        "ask-user-2",
    ]
    assert [payload["source"] for payload in payloads] == [
        "permission_interrupt",
        "ask_user_interrupt",
    ]


def test_convert_interactions_to_ask_user_questions_prefers_structured_duplicate_id():
    """The permission shell around ask_user must not become a second card."""
    interactions = [
        {
            "id": "ask-user-1",
            "value": {
                "message": "**工具 `ask_user` 需要授权才能执行**",
                "tool_name": "ask_user",
            },
        },
        {
            "id": "ask-user-1",
            "value": {
                "questions": [{"question": "请选择语言", "header": "语言"}],
                "tool_name": "ask_user",
            },
        },
    ]

    payloads = convert_interactions_to_ask_user_questions(interactions)

    assert len(payloads) == 1
    assert payloads[0]["request_id"] == "ask-user-1"
    assert payloads[0]["source"] == "ask_user_interrupt"
    assert payloads[0]["questions"][0]["question"] == "请选择语言"


def _scene_hook_input(normalized_tool_name: str, user_input):
    from openjiuwen.harness.security.host import PermissionSceneHookInput

    return PermissionSceneHookInput(
        ctx=SimpleNamespace(session=None),
        tool_call=SimpleNamespace(id="call_1", name=normalized_tool_name, arguments={}),
        user_input=user_input,
        normalized_tool_name=normalized_tool_name,
        tool_args={},
        engine=None,
    )


def _permission_scene_hook():
    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
    hook = rail._host.permission_scene_hook
    assert hook is not None
    return hook


def test_scene_hook_approves_ask_user_on_resume():
    """Regression for issue #1976.

    The permission rail intercepts every tool. On resume it would otherwise
    grab the ask_user answer as its own user_input and re-raise a permission
    interrupt, making the option card re-pop forever. The scene hook must
    approve ask_user so its answer reaches the model.
    """
    hook = _permission_scene_hook()
    resume_answer = {"answers": {"__free_text__": "数据处理"}, "original_request": "..."}

    outcome = asyncio.run(hook(_scene_hook_input("ask_user", resume_answer)))

    assert outcome == ("approve",)


def test_scene_hook_approves_ask_user_on_first_pass():
    hook = _permission_scene_hook()

    outcome = asyncio.run(hook(_scene_hook_input("ask_user", None)))

    assert outcome == ("approve",)


@pytest.mark.parametrize("tool_name", ["bash", "task_tool"])
def test_scene_hook_leaves_other_tools_to_engine(tool_name):
    """Non-interactive tools must still fall through to the tiered engine
    (returns ``None``) when no owner-scope context is set."""
    hook = _permission_scene_hook()

    outcome = asyncio.run(hook(_scene_hook_input(tool_name, None)))

    assert outcome is None


def test_expert_confirmation_exemption_preserves_digital_avatar_restriction():
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        PermissionContext, TOOL_PERMISSION_CONTEXT,
    )

    hook = _permission_scene_hook()
    token = TOOL_PERMISSION_CONTEXT.set(PermissionContext(group_digital_avatar=True))
    try:
        outcome = asyncio.run(hook(_scene_hook_input("symphony_plan_experts", {"approved": True})))
    finally:
        TOOL_PERMISSION_CONTEXT.reset(token)
    assert outcome[0] == "reject"


def test_build_permission_rail_wires_session_persist_hook():
    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
    assert rail._host.persist_session_allow_rule is not None
    assert rail._host.get_permissions_snapshot is not None


def test_permission_host_forwards_one_batch_for_each_lifetime(monkeypatch):
    from unittest.mock import Mock
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_persist

    session = Mock(return_value=True)
    permanent = Mock(return_value=True)
    monkeypatch.setattr(permissions_persist, "persist_session_allow_rule", session)
    monkeypatch.setattr(permissions_persist, "persist_merged_allow_rule_snapshot", permanent)
    rail = build_permission_rail({"permissions": {"enabled": True}})
    grants = [{"id": "first"}, {"id": "second"}]
    snapshot = {"approval_overrides": [*grants, {"id": "unrelated"}]}
    assert rail._host.persist_session_allow_rule(
        snapshot, session_id="batch-session", approval_grants=grants,
    )
    assert rail._host.persist_allow_rule(snapshot, approval_grants=grants)
    session.assert_called_once_with(snapshot, session_id="batch-session", approval_grants=grants)
    permanent.assert_called_once_with(snapshot, approval_grants=grants)


def test_build_multi_questions_ignores_string_options():
    """Regression for #2331: options='a,b' must not become character options + Other."""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    questions = _build_multi_questions(
        [
            {
                "question": "Which option?",
                "header": "Choice",
                "options": "a,b",
            }
        ]
    )

    assert len(questions) == 1
    assert questions[0]["options"] == []


def test_task_tool_resume_interrupt_is_not_rendered_as_ask_user_question():
    """回归：被中断的工具不认识 query 时，不能凭 tool_args.query 造 ask_user 卡片。

    真实故障（2026-09-21）：并行 task_tool 里某个子代理永久挂起后，父会话把
    后续每条普通消息都当成 HITL 续跑输入；ToolInterruptHandler 会把用户原话写进
    被中断工具调用（task_tool）的 ``arguments["query"]``。旧实现把这个 query 当成
    ask_user 的 plain query，于是前端一直弹出题干＝用户原话的「询问你」卡片，
    用户输入全被引到这张卡上，形成死循环。
    """
    interaction = {
        "id": "call_9f6a09682c80434dbcc01500aeb7e7ff",
        "value": {
            "message": "工具 `task_tool` 需要授权才能执行",
            "tool_name": "task_tool",
            "tool_call_id": "call_9f6a09682c80434dbcc01500aeb7e7ff",
            "tool_args": {
                "kind": "task_tool",
                "keys": ["query", "subagent_type", "task_description"],
                "query": "还没好吗",
                "subagent_type": "general-purpose",
            },
        },
    }

    result = convert_interactions_to_ask_user_question([interaction])

    assert result is not None
    assert result["source"] != "ask_user_interrupt"
    assert result["questions"][0]["question"] != "还没好吗"


def test_ask_user_plain_query_without_tool_context_still_converts():
    """没有工具上下文的 ask_user shell 仍走 query 兜底，别把合法卡片一起改坏。"""
    result = convert_interactions_to_ask_user_question(
        [
            {
                "id": "call_ask_shell",
                "value": {
                    "message": "",
                    "tool_args": {"query": "请选择语言"},
                },
            }
        ]
    )

    assert result is not None
    assert result["source"] == "ask_user_interrupt"
    assert result["questions"][0]["question"] == "请选择语言"


def test_build_multi_questions_appends_other_for_valid_options():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    questions = _build_multi_questions(
        [
            {
                "question": "Which option?",
                "header": "Choice",
                "options": [
                    {"label": "A", "description": "opt a"},
                    {"label": "B", "description": "opt b"},
                ],
            }
        ]
    )

    assert [opt["label"] for opt in questions[0]["options"]] == ["A", "B", "Other"]


def test_build_multi_questions_preserves_tool_metadata_for_permission_cards():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    questions = _build_multi_questions(
        [
            {
                "question": "需要授权",
                "header": "权限审批: bash",
                "options": [{"label": "本次允许"}],
                "tool_call_id": "call-1",
                "tool_name": "bash",
                "tool_args": {"command": "python D:\\小艺claw\\test.py"},
            }
        ]
    )

    assert questions[0]["tool_call_id"] == "call-1"
    assert questions[0]["tool_name"] == "bash"
    assert questions[0]["tool_args"] == {"command": "python D:\\小艺claw\\test.py"}
