import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
    convert_interactions_to_ask_user_question,
    convert_interactions_to_ask_user_questions,
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


def _scene_hook_input(
    normalized_tool_name: str,
    user_input,
    *,
    extra=None,
    engine=None,
    tool_call_id: str = "call_1",
):
    from openjiuwen.harness.security.host import PermissionSceneHookInput

    return PermissionSceneHookInput(
        ctx=SimpleNamespace(session=None, extra=extra or {}),
        tool_call=SimpleNamespace(
            id=tool_call_id, name=normalized_tool_name, arguments={}
        ),
        user_input=user_input,
        normalized_tool_name=normalized_tool_name,
        tool_args={},
        engine=engine,
    )


class _FakeEngineLevel:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeSceneEngine:
    """只实现 scene hook 用到的直接裁决查询。"""

    def __init__(self, level) -> None:
        self._level = level

    def check_tool_permission_directly(self, tool_name, tool_args):
        if isinstance(self._level, Exception):
            raise self._level
        return self._level, "engine_rule"


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


def test_scene_hook_leaves_other_tools_to_engine():
    """Non-interactive tools must still fall through to the tiered engine
    (returns ``None``) when no owner-scope context is set."""
    hook = _permission_scene_hook()

    outcome = asyncio.run(hook(_scene_hook_input("bash", None)))

    assert outcome is None


def test_scene_hook_rejects_when_security_list_denied():
    """统一名单 Rail（95）deny 后，权限引擎层直接 reject，不二次弹窗。"""
    from jiuwenswarm.agents.harness.common.rails.security_lists.rail import (
        SECURITY_LIST_DENY_KEY,
    )

    hook = _permission_scene_hook()
    deny_message = "[SECURITY_LIST_DENIED] 命中安全名单规则（命令: curl*），已拒绝执行 bash。"
    extra = {SECURITY_LIST_DENY_KEY: deny_message}

    outcome = asyncio.run(hook(_scene_hook_input("bash", None, extra=extra)))

    assert outcome == ("reject", deny_message)


def test_scene_hook_reuses_security_list_approval_when_engine_not_deny():
    """归一化：名单 Rail 的 ask 已获用户批准 → 引擎层不再二次弹窗。"""
    from jiuwenswarm.agents.harness.common.rails.security_lists.rail import (
        SECURITY_LIST_APPROVED_KEY,
    )

    hook = _permission_scene_hook()
    engine = _FakeSceneEngine(_FakeEngineLevel("ask"))
    extra = {SECURITY_LIST_APPROVED_KEY: "call_1"}

    outcome = asyncio.run(
        hook(_scene_hook_input("bash", None, extra=extra, engine=engine))
    )

    assert outcome == ("approve",)


def test_scene_hook_ignores_security_list_approval_when_engine_denies():
    """引擎 deny 优先于用户批准：不因名单批准标记而放行（取严语义）。"""
    from jiuwenswarm.agents.harness.common.rails.security_lists.rail import (
        SECURITY_LIST_APPROVED_KEY,
    )

    hook = _permission_scene_hook()
    engine = _FakeSceneEngine(_FakeEngineLevel("deny"))
    extra = {SECURITY_LIST_APPROVED_KEY: "call_1"}

    outcome = asyncio.run(
        hook(_scene_hook_input("bash", None, extra=extra, engine=engine))
    )

    assert outcome is None


def test_scene_hook_ignores_approval_marker_of_other_tool_call():
    """标记按 tool_call_id 匹配：其他调用的残留标记不生效（防跨调用误放行）。"""
    from jiuwenswarm.agents.harness.common.rails.security_lists.rail import (
        SECURITY_LIST_APPROVED_KEY,
    )

    hook = _permission_scene_hook()
    engine = _FakeSceneEngine(_FakeEngineLevel("ask"))
    extra = {SECURITY_LIST_APPROVED_KEY: "other_call"}

    outcome = asyncio.run(
        hook(_scene_hook_input("bash", None, extra=extra, engine=engine))
    )

    assert outcome is None


def test_scene_hook_does_not_approve_when_engine_query_fails():
    """引擎查询失败时不放行，退回引擎自身判定流程（fail-safe）。"""
    from jiuwenswarm.agents.harness.common.rails.security_lists.rail import (
        SECURITY_LIST_APPROVED_KEY,
    )

    hook = _permission_scene_hook()
    engine = _FakeSceneEngine(RuntimeError("engine unavailable"))
    extra = {SECURITY_LIST_APPROVED_KEY: "call_1"}

    outcome = asyncio.run(
        hook(_scene_hook_input("bash", None, extra=extra, engine=engine))
    )

    assert outcome is None


def test_build_permission_rail_wires_session_persist_hook():
    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
    assert rail._host.persist_session_allow_rule is not None
    assert rail._host.get_permissions_snapshot is not None


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
