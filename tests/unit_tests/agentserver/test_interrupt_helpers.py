import asyncio
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
    assert permanent == PermissionConfirmResponse(
        approved=True, auto_confirm=True, persist_allow=True, feedback=""
    )
    assert reject is not None and reject.approved is False


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

    outcome = asyncio.run(hook(req))
    assert outcome == PermissionConfirmResponse(
        approved=True, auto_confirm=False, persist_allow=False, feedback=""
    )


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
