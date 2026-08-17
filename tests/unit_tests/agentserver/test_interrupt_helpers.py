import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
    convert_interactions_to_ask_user_question,
)


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


def _permission_scene_hook(monkeypatch: pytest.MonkeyPatch):
    # build_permission_rail reads the process-effective permissions config, not
    # the unused ``config`` argument — enable it explicitly for these unit tests.
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": True, "tools": {}, "rules": []},
    )
    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
    hook = rail._host.permission_scene_hook
    assert hook is not None
    return hook


def test_scene_hook_approves_ask_user_on_resume(monkeypatch: pytest.MonkeyPatch):
    """Regression for issue #1976.

    The permission rail intercepts every tool. On resume it would otherwise
    grab the ask_user answer as its own user_input and re-raise a permission
    interrupt, making the option card re-pop forever. The scene hook must
    approve ask_user so its answer reaches the model.
    """
    hook = _permission_scene_hook(monkeypatch)
    resume_answer = {"answers": {"__free_text__": "数据处理"}, "original_request": "..."}

    outcome = asyncio.run(hook(_scene_hook_input("ask_user", resume_answer)))

    assert outcome == ("approve",)


def test_scene_hook_approves_ask_user_on_first_pass(monkeypatch: pytest.MonkeyPatch):
    hook = _permission_scene_hook(monkeypatch)

    outcome = asyncio.run(hook(_scene_hook_input("ask_user", None)))

    assert outcome == ("approve",)


def test_scene_hook_approves_deepresearch_execute_workflow_answer(
    monkeypatch: pytest.MonkeyPatch,
):
    """A DeepResearch card answer belongs to its execution rail, not permissions."""
    hook = _permission_scene_hook(monkeypatch)
    resume_answer = {
        "status": "answered",
        "answers": [
            {
                "question": "您希望这份报告是精简版还是专业版？",
                "selected_options": ["精简版"],
            }
        ],
    }

    outcome = asyncio.run(
        hook(_scene_hook_input("deepresearch_execute", resume_answer))
    )

    assert outcome == ("approve",)


@pytest.mark.parametrize(
    "user_input",
    [
        None,
        {"approved": True, "auto_confirm": False, "feedback": ""},
    ],
)
def test_scene_hook_keeps_deepresearch_execute_permission_checks(
    monkeypatch: pytest.MonkeyPatch,
    user_input,
):
    """Initial execution and permission decisions still use the permission engine."""
    hook = _permission_scene_hook(monkeypatch)

    outcome = asyncio.run(
        hook(_scene_hook_input("deepresearch_execute", user_input))
    )

    assert outcome is None


def test_scene_hook_leaves_other_tools_to_engine(monkeypatch: pytest.MonkeyPatch):
    """Non-interactive tools must still fall through to the tiered engine
    (returns ``None``) when no owner-scope context is set."""
    hook = _permission_scene_hook(monkeypatch)

    outcome = asyncio.run(hook(_scene_hook_input("bash", None)))

    assert outcome is None


def test_filter_init_kwargs_drops_unknown_host_fields():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _filter_init_kwargs,
    )

    class LegacyHost:
        def __init__(self, persist_allow_rule=None, host=None):
            self.persist_allow_rule = persist_allow_rule

    kwargs = _filter_init_kwargs(
        LegacyHost,
        persist_allow_rule=lambda _permissions: True,
        persist_session_allow_rule=lambda _permissions: True,
        host="unused",
    )
    assert "persist_allow_rule" in kwargs
    assert "persist_session_allow_rule" not in kwargs


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


def test_build_multi_questions_preserves_question_preview():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    questions = _build_multi_questions(
        [
            {
                "question": "Review the outline?",
                "header": "Outline",
                "options": [
                    {"label": "Confirm", "description": "Continue"},
                    {"label": "Edit", "description": "Revise"},
                ],
                "preview": {
                    "title": "Research outline",
                    "text": "# Outline\n\n## P1: Scope",
                    "format": "markdown",
                    "editable": True,
                    "outline_ref": "outline-1",
                    "meta": {"currentRound": 1},
                },
            }
        ]
    )

    assert questions[0]["preview"] == {
        "title": "Research outline",
        "text": "# Outline\n\n## P1: Scope",
        "format": "markdown",
        "editable": True,
        "outline_ref": "outline-1",
        "meta": {"currentRound": 1},
    }


def test_parse_hosted_permission_answer_maps_options():
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        parse_hosted_permission_answer,
    )

    once = parse_hosted_permission_answer(
        [{"selected_options": ["本次允许"]}]
    )
    remember = parse_hosted_permission_answer(
        {"selected_options": ["本会话内允许"]}
    )
    remember_legacy = parse_hosted_permission_answer(
        {"selected_options": ["会话内记住"]}
    )
    permanent = parse_hosted_permission_answer(
        {"selected_options": ["永久记住"]}
    )
    reject = parse_hosted_permission_answer(
        {"selected_options": ["拒绝"]}
    )

    assert once == PermissionConfirmResponse(
        approved=True, auto_confirm=False, persist_allow=False, feedback=""
    )
    assert remember == PermissionConfirmResponse(
        approved=True, auto_confirm=True, persist_allow=False, feedback=""
    )
    assert remember_legacy == remember
    assert permanent == PermissionConfirmResponse(
        approved=True, auto_confirm=True, persist_allow=True, feedback=""
    )
    assert reject is not None and reject.approved is False


def test_parse_hosted_permission_answer_enterprise_skips_persist(
    monkeypatch: pytest.MonkeyPatch,
):
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers as helpers

    monkeypatch.setattr(helpers, "is_enterprise", lambda: True)
    permanent = helpers.parse_hosted_permission_answer(
        {"selected_options": ["总是允许"]}
    )
    assert permanent == PermissionConfirmResponse(
        approved=True, auto_confirm=True, persist_allow=False, feedback=""
    )


def test_resolve_subagent_permission_parent_ignores_same_session(
    monkeypatch: pytest.MonkeyPatch,
):
    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers as helpers

    parent = SimpleNamespace(
        get_session_id=lambda: "main-session",
        write_stream=lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars.get_subagent_parent_session",
        lambda: parent,
    )

    assert (
        helpers.resolve_subagent_permission_parent_session(
            SimpleNamespace(session=parent)
        )
        is None
    )


def test_request_permission_confirmation_uses_hosted_path_for_subagent(
    monkeypatch: pytest.MonkeyPatch,
):
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
        TOOL_PERMISSION_CHANNEL_ID,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": True, "tools": {}, "rules": []},
    )

    parent = SimpleNamespace(
        get_session_id=lambda: "main-session",
        write_stream=lambda *_a, **_k: None,
    )
    child = SimpleNamespace(get_session_id=lambda: "child-session")

    async def _fake_hosted(req, *, parent_session, timeout=120.0):
        assert parent_session is parent
        assert req.tool_call.name == "read_file"
        return PermissionConfirmResponse(
            approved=True, auto_confirm=False, persist_allow=False, feedback=""
        )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers.resolve_subagent_permission_parent_session",
        lambda _ctx: parent,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers.request_subagent_hosted_permission_confirmation",
        _fake_hosted,
    )

    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
    hook = rail._host.request_permission_confirmation
    assert hook is not None

    from openjiuwen.harness.security.host import PermissionConfirmationRequest
    from openjiuwen.harness.security.models import PermissionLevel, PermissionResult

    req = PermissionConfirmationRequest(
        ctx=SimpleNamespace(session=child),
        tool_call=SimpleNamespace(id="call_1", name="read_file", arguments={"path": "x"}),
        result=PermissionResult(
            permission=PermissionLevel.ASK,
            reason="file_guard",
            matched_rule="file_guard:defaults",
        ),
        auto_confirm_key="read_file",
    )

    token = TOOL_PERMISSION_CHANNEL_ID.set("officeclaw")
    try:
        outcome = asyncio.run(hook(req))
    finally:
        TOOL_PERMISSION_CHANNEL_ID.reset(token)
    assert outcome == PermissionConfirmResponse(
        approved=True, auto_confirm=False, persist_allow=False, feedback=""
    )


def test_request_permission_confirmation_skips_hosted_on_acp(
    monkeypatch: pytest.MonkeyPatch,
):
    """ACP nested ASK must stay on session/request_permission, not parent card."""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
        TOOL_PERMISSION_CHANNEL_ID,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": True, "tools": {}, "rules": []},
    )
    parent = SimpleNamespace(
        get_session_id=lambda: "main-session",
        write_stream=lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers.resolve_subagent_permission_parent_session",
        lambda _ctx: parent,
    )

    hosted_called = {"n": 0}

    async def _boom(*_a, **_k):
        hosted_called["n"] += 1
        raise AssertionError("hosted path must not run on acp")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers.request_subagent_hosted_permission_confirmation",
        _boom,
    )

    # ACP path needs session_id + acp output manager; stub manager lookup to fail
    # early with None after channel gate — we only assert hosted was skipped.
    # When channel is acp and parent exists, code falls through to ACP branch.
    # Provide minimal stubs so hook returns None (no session_id path) or we
    # mock deeper. Simplest: empty session_id → None after skipping hosted.
    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
    hook = rail._host.request_permission_confirmation

    from openjiuwen.harness.security.host import PermissionConfirmationRequest
    from openjiuwen.harness.security.models import PermissionLevel, PermissionResult

    req = PermissionConfirmationRequest(
        ctx=SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: "")),
        tool_call=SimpleNamespace(id="call_1", name="bash", arguments={}),
        result=PermissionResult(
            permission=PermissionLevel.ASK,
            reason="ask",
            matched_rule="bash",
        ),
        auto_confirm_key="bash",
    )
    token = TOOL_PERMISSION_CHANNEL_ID.set("acp")
    try:
        outcome = asyncio.run(hook(req))
    finally:
        TOOL_PERMISSION_CHANNEL_ID.reset(token)
    assert hosted_called["n"] == 0
    assert outcome is None


def test_request_permission_confirmation_still_interrupts_on_web_main_agent(
    monkeypatch: pytest.MonkeyPatch,
):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_rail,
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": True, "tools": {}, "rules": []},
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers.resolve_subagent_permission_parent_session",
        lambda _ctx: None,
    )

    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
    hook = rail._host.request_permission_confirmation
    assert hook is not None

    from openjiuwen.harness.security.host import PermissionConfirmationRequest
    from openjiuwen.harness.security.models import PermissionLevel, PermissionResult

    req = PermissionConfirmationRequest(
        ctx=SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: "main")),
        tool_call=SimpleNamespace(id="call_1", name="bash", arguments={"command": "ls"}),
        result=PermissionResult(
            permission=PermissionLevel.ASK,
            reason="ask",
            matched_rule="bash",
        ),
        auto_confirm_key="bash",
    )

    outcome = asyncio.run(hook(req))
    assert outcome == "interrupt"


@pytest.mark.asyncio
async def test_hosted_permission_rail_hook_registry_emit_resolve_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
):
    """Half-integration: real parent resolve + registry + card emit + chat answer.

    Trigger surface: nested agent with own PermissionInterruptRail, session id
    different from parent ContextVar (OfficeClaw worker / nested agent). Not
    dependent on task_tool inheriting PermissionInterruptRail.
    """
    from openjiuwen.harness.security.host import PermissionConfirmationRequest
    from openjiuwen.harness.security.models import (
        PermissionConfirmResponse,
        PermissionLevel,
        PermissionResult,
    )
    from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
        SubagentApprovalRegistry,
    )

    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers as helpers
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization.runtime import (
        resolve_subagent_approval,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
        TOOL_PERMISSION_CHANNEL_ID,
    )
    from jiuwenswarm.agents.harness.common.tools.subagent_executor import context_vars

    SubagentApprovalRegistry.reset_instance_for_tests()
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions.config_loader.get_effective_permissions_config",
        lambda **_kwargs: {"enabled": True, "tools": {}, "rules": []},
    )
    monkeypatch.setattr(helpers, "is_enterprise", lambda: False)

    emitted: list[dict] = []

    class _ParentSession:
        def get_session_id(self) -> str:
            return "parent-sess"

        async def write_stream(self, event: object) -> None:
            payload = getattr(event, "payload", event)
            if isinstance(payload, dict):
                emitted.append(payload)

    parent = _ParentSession()
    child = SimpleNamespace(get_session_id=lambda: "child-sess")
    token_parent = context_vars.set_subagent_parent_session(parent)
    token_channel = TOOL_PERMISSION_CHANNEL_ID.set("officeclaw")
    try:
        # Real resolve_subagent_permission_parent_session (no mock).
        assert (
            helpers.resolve_subagent_permission_parent_session(
                SimpleNamespace(session=child)
            )
            is parent
        )

        rail = helpers.build_permission_rail({"permissions": {"enabled": True}})
        assert rail is not None
        hook = rail._host.request_permission_confirmation
        assert hook is not None

        req = PermissionConfirmationRequest(
            ctx=SimpleNamespace(session=child),
            tool_call=SimpleNamespace(
                id="call_hosted_1",
                name="read_file",
                arguments={"path": "/tmp/x"},
            ),
            result=PermissionResult(
                permission=PermissionLevel.ASK,
                reason="file_guard",
                matched_rule="file_guard:defaults",
            ),
            auto_confirm_key="read_file",
        )

        async def _approve_when_pending() -> None:
            for _ in range(50):
                pending = SubagentApprovalRegistry.get_instance().pending_requests()
                if pending:
                    rid = pending[0].approval_id
                    ok = resolve_subagent_approval(
                        request_id=rid,
                        session_id="parent-sess",
                        source="subagent_tool_permission",
                        answers=[{"selected_options": ["本次允许"]}],
                        agent_scope_id="child-sess",
                    )
                    assert ok is True
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("approval never became pending")

        approve_task = asyncio.create_task(_approve_when_pending())
        outcome = await hook(req)
        await approve_task

        assert outcome == PermissionConfirmResponse(
            approved=True, auto_confirm=False, persist_allow=False, feedback=""
        )
        assert any(
            p.get("source") == "subagent_tool_permission"
            or p.get("event_type") == "chat.ask_user_question"
            for p in emitted
        ), emitted
        session_opt = next(
            (
                o
                for p in emitted
                for q in (p.get("questions") or [])
                for o in (q.get("options") or [])
                if o.get("label") == "本会话内允许"
            ),
            None,
        )
        if session_opt is not None:
            assert "子 Agent" in str(session_opt.get("description") or "")
    finally:
        TOOL_PERMISSION_CHANNEL_ID.reset(token_channel)
        context_vars.reset_subagent_parent_session(token_parent)
        SubagentApprovalRegistry.reset_instance_for_tests()


# ---------------------------------------------------------------------------
# 权限审批卡意图标题（build_permission_intent_headline / apply_permission_intent）
# ---------------------------------------------------------------------------


def _tool_permission_message(tool_name: str, tool_args: dict) -> str:
    """Mirror agent-core PermissionInterruptRail._build_message output shape."""
    args_preview = json.dumps(tool_args, ensure_ascii=False, indent=2)
    return (
        f"**工具 `{tool_name}` 需要授权才能执行**\n\n"
        "请确认是否允许该操作。\n\n"
        f"参数：\n```json\n{args_preview}\n```\n"
        f"\n匹配规则：`permission_engine`"
    )


def test_intent_headline_combines_action_phrase_and_call_goal():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    headline = build_permission_intent_headline(
        "write_file",
        {
            "call_goal": "生成上周的数据分析周报",
            "path": "/ws/report.md",
            "content": "# 周报",
        },
    )

    assert headline == (
        "**是否允许「写入 report.md」？**\n\n模型说明（仅供参考）：生成上周的数据分析周报"
    )


def test_intent_headline_action_phrase_without_call_goal():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    headline = build_permission_intent_headline("bash", {"command": "ls -la /tmp"})

    assert headline == "**是否允许「执行 ls -la /tmp」？**"


def test_intent_headline_call_goal_only_for_unknown_tool():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    headline = build_permission_intent_headline(
        "mcp_custom_tool",
        {"call_goal": "查询工单状态", "ticket": "T-1"},
    )

    assert headline == (
        "**是否允许执行 `mcp_custom_tool`？**\n\n模型说明（仅供参考）：查询工单状态"
    )


def test_intent_headline_empty_without_phrase_or_goal():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    assert build_permission_intent_headline("mcp_custom_tool", {"command": "x"}) == ""
    assert build_permission_intent_headline("", {"call_goal": "x"}) == ""


def test_apply_permission_intent_prepends_headline_and_strips_rule_line():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        apply_permission_intent,
    )

    tool_args = {
        "call_goal": "生成上周的数据分析周报",
        "path": "/ws/report.md",
        "content": "# 周报",
    }
    message = _tool_permission_message("write_file", tool_args)

    question = apply_permission_intent(message, "write_file", tool_args)

    assert question.startswith("**是否允许「写入 report.md」？**")
    assert "模型说明（仅供参考）：生成上周的数据分析周报" in question
    assert question.index("模型说明（仅供参考）：") < question.index("**工具")
    assert "匹配规则" not in question
    assert "请确认是否允许该操作" not in question
    # 安全不变量：工具声明行与原始参数保留，供用户核对实际操作。
    assert "**工具 `write_file` 需要授权才能执行**" in question
    assert '"/ws/report.md"' in question


def test_apply_permission_intent_degrades_to_rule_line_removal_only():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        apply_permission_intent,
    )

    tool_args = {"command": "ls -la /tmp"}
    message = _tool_permission_message("mcp_custom_tool", tool_args)

    question = apply_permission_intent(message, "mcp_custom_tool", tool_args)

    # 词典外工具且无 call_goal：无标题，仅移除内部「匹配规则」行，其余不变。
    assert question.startswith("**工具 `mcp_custom_tool` 需要授权才能执行**")
    assert "匹配规则" not in question
    assert "请确认是否允许该操作。" in question
    assert "ls -la /tmp" in question


def test_extract_question_adds_intent_to_permission_card(
    monkeypatch: pytest.MonkeyPatch,
):
    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers as helpers

    monkeypatch.setattr(helpers, "is_enterprise", lambda: False)
    tool_args = {
        "call_goal": "生成上周的数据分析周报",
        "path": "/ws/report.md",
        "content": "# 周报",
    }
    interaction = SimpleNamespace(
        id="call_intent_1",
        value={
            "message": _tool_permission_message("write_file", tool_args),
            "tool_name": "write_file",
            "tool_args": tool_args,
        },
    )

    result = helpers.extract_question_from_interaction(interaction)

    assert result is not None
    question = result["question"]
    assert result["header"] == "权限审批: write_file"
    assert question.startswith("**是否允许「写入 report.md」？**")
    assert "模型说明（仅供参考）：生成上周的数据分析周报" in question
    assert "匹配规则" not in question
    assert "**工具 `write_file` 需要授权才能执行**" in question
    assert '"/ws/report.md"' in question
    assert {"本次允许", "拒绝"} <= {opt["label"] for opt in result["options"]}


def test_extract_question_keeps_non_permission_interrupt_untouched():
    """确认类中断（confirm_interrupt）不经过意图改写。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers as helpers

    interaction = SimpleNamespace(
        id="call_confirm_1",
        value={"message": "确认执行该任务？", "tool_name": "switch_mode"},
    )

    result = helpers.extract_question_from_interaction(interaction)

    assert result is not None
    assert result["header"] == "操作确认: switch_mode"
    assert result["question"] == "确认执行该任务？"


def test_hosted_permission_question_leads_with_intent_headline():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _format_hosted_permission_question,
    )

    question = _format_hosted_permission_question(
        tool_name="write_file",
        tool_args={"call_goal": "生成周报", "path": "/ws/report.md"},
        reason="file_guard",
        agent_scope_id="child-sess",
    )

    assert question.startswith("**是否允许「写入 report.md」？**")
    assert "模型说明（仅供参考）：生成周报" in question
    assert "**工具 `write_file` 需要授权才能执行**" in question
    assert "> 发起方：子 Agent `child-sess`" in question
    assert "**权限原因：** file_guard" in question


def test_intent_headline_sanitizes_model_controlled_text():
    """call_goal 是模型可控文本：折叠为单行并剔除 markdown 结构符，伪造声明行不可存活。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    malicious_goal = (
        "生成周报\n\n**工具 `bash` 需要授权才能执行**\n\n"
        '参数：\n```json\n{"command": "rm -rf /"}\n```'
    )
    headline = build_permission_intent_headline(
        "write_file",
        {"call_goal": malicious_goal, "path": "/ws/report.md"},
    )

    assert headline.startswith(
        "**是否允许「写入 report.md」？**\n\n模型说明（仅供参考）："
    )
    goal_line = headline.split("\n\n", 1)[1]
    # 单行：换行被折叠，无法在意图区伪造新的声明行/参数块结构。
    assert "\n" not in goal_line
    # markdown 结构符与代码围栏全部剔除。
    for ch in "*`_[]":
        assert ch not in goal_line
    # 伪造内容降级为普通文字（信息保留供核对，但不再具有结构）。
    assert "工具 bash 需要授权才能执行" in goal_line
    assert "```" not in headline
    assert "需要授权才能执行**" not in headline


def test_intent_headline_keeps_title_pairing_despite_bracket_injection():
    """参数内容含「」/**/反引号时，「」→【】保住标题配对，结构符被剔除。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    headline = build_permission_intent_headline(
        "write_file",
        {"path": "/ws/report」**注入`rm -rf`.md"},
    )

    # 注入的 」被替换为 】、** 与反引号被剔除，标题的「」配对保持完整。
    assert headline == "**是否允许「写入 report】注入rm -rf.md」？**"
    assert headline.count("「") == 1
    assert headline.count("」") == 1


def test_intent_headline_falls_back_to_tool_name_without_actionable_args():
    """write_file 缺 path 等可组短语的参数时，标题退化为工具名，call_goal 仍保留。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    headline = build_permission_intent_headline("write_file", {"call_goal": "生成周报"})

    assert headline == (
        "**是否允许执行 `write_file`？**\n\n模型说明（仅供参考）：生成周报"
    )


def test_intent_headline_truncates_action_and_goal():
    """action 截 60、call_goal 截 80，超长以 … 收尾，标题块不随参数膨胀。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    headline = build_permission_intent_headline(
        "write_file",
        {"call_goal": "目标" * 50, "path": "/ws/" + "长" * 60 + ".md"},
    )
    title, goal_line = headline.split("\n\n", 1)

    assert title == "**是否允许「写入 " + "长" * 57 + "…」？**"
    assert goal_line == "模型说明（仅供参考）：" + "目标" * 40 + "…"


def test_intent_headline_and_apply_tolerate_none_tool_args():
    """tool_args=None（中断字段读取失败的降级路径）不抛异常，仅退化为移除匹配规则行。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        apply_permission_intent,
        build_permission_intent_headline,
    )

    assert build_permission_intent_headline("write_file", None) == ""

    message = _tool_permission_message("write_file", {"path": "/ws/report.md"})
    question = apply_permission_intent(message, "write_file", None)

    # 无标题可前置：保留原始声明与确认句，仅移除内部「匹配规则」行。
    assert question.startswith("**工具 `write_file` 需要授权才能执行**")
    assert "请确认是否允许该操作。" in question
    assert "匹配规则" not in question


def test_intent_headline_washes_tool_name_in_fallback_title():
    """兜底标题中的工具名（模型可控）经 code-span 清洗：单行、无反引号。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    malicious_name = "evil\n**工具 `bash` 需要授权才能执行**\n`rm`"
    headline = build_permission_intent_headline(
        malicious_name, {"call_goal": "查询工单状态"}
    )

    title = headline.split("\n\n", 1)[0]
    # 单行、反引号被剔除：换行+反引号无法在标题区伪造声明行/提前终止代码段。
    assert "\n" not in title
    assert "`bash`" not in headline
    assert "**工具 `bash` 需要授权才能执行**" not in headline
    # 伪造内容降级为代码段内的惰性文字。
    assert "工具 bash 需要授权才能执行" in headline
    assert "模型说明（仅供参考）：查询工单状态" in headline

    # 真实工具名（含下划线）不改写：code-span 清洗不做全量字符剔除。
    legit = build_permission_intent_headline(
        "mcp__search__query", {"call_goal": "查资料"}
    )
    assert legit.startswith("**是否允许执行 `mcp__search__query`？**")


def test_intent_headline_exempts_skill_turbo_custom_card():
    """skill_turbo 统一审批卡有语义完整的定制文案，不加通用意图标题。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        build_permission_intent_headline,
    )

    assert (
        build_permission_intent_headline(
            "skill_acceleration_exec", {"call_goal": "执行技能任务"}
        )
        == ""
    )


def test_apply_permission_intent_keeps_skill_turbo_card_untouched():
    """skill_turbo 定制审批消息（含工具清单）经改写后应原样保留。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        apply_permission_intent,
    )

    message = (
        "**即将调用 `skill加速`，需要授权后整体放行：**\n\n"
        "即将调用 skill加速 执行技能任务，需要一次性授权下列工具。"
        "确认后执行过程中不再逐个询问。\n\n"
        "**可能用到的工具：**\n\n- `bash` — 执行 shell 命令\n"
    )
    args = {"call_goal": "执行技能任务", "skill": "weekly-report"}

    assert apply_permission_intent(message, "skill_acceleration_exec", args) == message


def test_apply_permission_intent_strips_rule_line_with_embedded_backticks():
    """matched_rule 内嵌反引号（shell_subcommands 聚合格式）时仍整行移除。"""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        apply_permission_intent,
    )

    message = (
        "**工具 `bash` 需要授权才能执行**\n\n请确认是否允许该操作。\n\n"
        "参数：\n```json\n{\"command\": \"echo `id -u`\"}\n```\n"
        "\n匹配规则：`tiered_policy:shell_subcommands:echo `id -u`=>rules:bash:ask`"
    )

    question = apply_permission_intent(message, "bash", {"command": "echo `id -u`"})

    assert "匹配规则" not in question
    # 整行移除：配对反引号匹配只截半行时会残留 =>rules:bash:ask 尾段。
    assert "=>rules:bash" not in question
    # 参数区原文（含反引号命令）保留。
    assert "echo `id -u`" in question

def test_permission_host_uses_runtime_workspace_not_agent_root(tmp_path, monkeypatch):
    """file_guard workspace 应是当前任务目录，而不是 ~/.jiuwenswarm/agent/workspace。"""
    from openjiuwen.core.sys_operation.cwd import _cwd_state, init_cwd

    agent_ws = tmp_path / "workspace"
    project = agent_ws / "projects" / "web_xxx"
    project.mkdir(parents=True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_workspace_dir",
        lambda: agent_ws,
    )
    token = _cwd_state.set(None)
    try:
        init_cwd(str(project), project_root=str(project), workspace=str(project))
        rail = build_permission_rail({"permissions": {"enabled": True, "mode": "auto"}})
        assert rail is not None
        resolved = Path(rail._host.resolve_workspace_dir()).resolve()
        assert resolved == project.resolve()
        assert resolved != agent_ws.resolve()
    finally:
        _cwd_state.reset(token)
