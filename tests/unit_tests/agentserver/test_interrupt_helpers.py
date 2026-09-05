import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    apply_permission_trusted_dirs,
    build_permission_rail,
    convert_interactions_to_ask_user_question,
    merge_permission_trusted_dirs,
)
from jiuwenswarm.common.utils import (
    get_agent_workspace_dir,
    get_default_project_session_workspace_dir,
    get_default_project_workspace_dir,
    get_workspace_dir,
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
    from openjiuwen.harness.security import PermissionSceneHookInput

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


def test_scene_hook_leaves_other_tools_to_engine():
    """Non-interactive tools must still fall through to the tiered engine
    (returns ``None``) when no owner-scope context is set."""
    hook = _permission_scene_hook()

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


def test_build_multi_questions_survives_a_question_without_text():
    """A missing question key must not take the whole conversion down.

    Same class of input as the #2331 guard above: a question that did not come
    through the ask_user rail's validation. There a malformed ``options`` value
    built a question out of single characters; here a missing ``question`` key
    raised ``KeyError``, losing every question in the call rather than the one
    that was malformed.
    """
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    questions = _build_multi_questions([{"header": "Follow-up"}])

    assert questions[0]["question"] == "Follow-up"
    assert questions[0]["header"] == "Follow-up"


def test_build_multi_questions_falls_back_to_the_calls_query():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    questions = _build_multi_questions(
        [{"options": [{"label": "A"}, {"label": "B"}]}],
        "When should the follow-up happen?",
    )

    assert questions[0]["question"] == "When should the follow-up happen?"
    # No header was written, so the existing placeholder still applies.
    assert questions[0]["header"] == "Question"


def test_build_multi_questions_prefers_question_over_the_fallbacks():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    questions = _build_multi_questions(
        [{"question": "Own text", "header": "Follow-up"}],
        "Top level query",
    )

    assert questions[0]["question"] == "Own text"


def test_build_multi_questions_derives_nothing_without_any_source():
    """No text anywhere yields an empty string, never a KeyError."""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    questions = _build_multi_questions([{"multi_select": True}])

    assert questions[0]["question"] == ""
    assert questions[0]["multi_select"] is True


def test_build_multi_questions_carries_an_inputs_declaration_through():
    """A declaration the caller supplied must reach the connector intact.

    ``_build_multi_questions`` rebuilt every question as a closed four-key
    literal, so ``inputs`` was discarded one layer below whatever would have
    rendered it. It is carried as opaque data: this is the normalisation point
    every channel's question passes through, so it cannot interpret a key one
    channel's renderer owns.
    """
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    declaration = [{"type": "date", "label": "First run"}]
    questions = _build_multi_questions(
        [{"question": "When?", "header": "Schedule", "inputs": declaration}]
    )

    assert questions[0]["inputs"] == declaration
    # Opaque means copied, not shared: one question can reach several channels,
    # and neither may edit what the next receives or the recorded arguments.
    assert questions[0]["inputs"] is not declaration
    questions[0]["inputs"][0]["label"] = "edited"
    assert declaration[0]["label"] == "First run"


@pytest.mark.parametrize(
    "declared",
    [None, [], "date", {"type": "date"}, 0],
    ids=["absent", "empty", "string", "object", "zero"],
)
def test_build_multi_questions_omits_anything_but_a_non_empty_array(declared):
    """A question declaring nothing usable keeps exactly the payload it had."""
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        _build_multi_questions,
    )

    question = {"question": "Which option?", "header": "Choice"}
    if declared is not None:
        question["inputs"] = declared

    questions = _build_multi_questions([question])

    assert questions == [
        {
            "question": "Which option?",
            "header": "Choice",
            "options": [],
            "multi_select": False,
        }
    ]


def test_convert_interactions_derives_prompt_from_the_calls_query():
    """End to end over the extraction point: query reaches the built question.

    The top-level ``query`` lives in ``tool_args`` beside the questions, which
    is the only place the fallback is still readable once the tool call has been
    consumed.
    """
    from openjiuwen.core.single_agent.interrupt import ToolCallInterruptRequest

    value = ToolCallInterruptRequest(
        message="Please provide the details for your follow-up:",
        payload_schema={},
        tool_name="ask_user",
        tool_call_id="tc_001",
        tool_args={
            "query": "Please provide the details for your follow-up:",
            "questions": [{"inputs": [{"type": "date", "name": "d"}]}],
        },
    )

    payload = convert_interactions_to_ask_user_question(
        [{"id": "req_1", "value": value}]
    )

    assert payload is not None
    assert payload["source"] == "ask_user_interrupt"
    question = payload["questions"][0]
    assert question["question"] == "Please provide the details for your follow-up:"
    assert question["inputs"] == [{"type": "date", "name": "d"}]

def test_permission_interrupt_uses_ask_title_from_metadata():
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        extract_question_from_interaction,
    )

    question = extract_question_from_interaction(
        SimpleNamespace(
            id="call_perm",
            value={
                "message": "write C:\\Users\\test\\test.txt\n",
                "tool_name": "write_file",
                "tool_args": {"file_path": r"C:\Users\test\test.txt"},
                "metadata": {
                    "ask_category": "path",
                    "ask_title": "检测到受保护的文件路径访问",
                    "ask_summary": r"write C:\Users\test\test.txt",
                },
            },
        )
    )

    assert question is not None
    assert question["header"] == "检测到受保护的文件路径访问"
    assert question["question"].startswith("write C:\\Users\\test\\test.txt")


def test_permission_rail_workspace_uses_session_dir():
    session_id = "sess_permission_workspace"
    rail = build_permission_rail(
        {"permissions": {"enabled": True}},
        session_id=session_id,
    )
    assert rail is not None
    resolved = rail._host.resolve_workspace_dir().resolve()
    expected = get_default_project_session_workspace_dir(session_id).resolve()
    assert resolved == expected
    assert resolved != get_workspace_dir().resolve()
    assert resolved != get_agent_workspace_dir().resolve()


def test_permission_rail_workspace_without_session_uses_projects_root():
    rail = build_permission_rail({"permissions": {"enabled": True}})
    assert rail is not None
    resolved = rail._host.resolve_workspace_dir().resolve()
    assert resolved == get_default_project_workspace_dir().resolve()
    assert resolved != get_agent_workspace_dir().resolve()


def test_merge_permission_trusted_dirs_adds_project_when_empty(tmp_path: Path):
    project = tmp_path / "my-project"
    project.mkdir()
    merged = merge_permission_trusted_dirs(None, str(project))
    assert [Path(item).resolve() for item in merged] == [project.resolve()]


def test_merge_permission_trusted_dirs_keeps_session_trusted_and_project(tmp_path: Path):
    extra = tmp_path / "extra"
    extra.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    merged = merge_permission_trusted_dirs([str(extra)], str(project))
    resolved = {Path(item).resolve() for item in merged}
    assert extra.resolve() in resolved
    assert project.resolve() in resolved


def test_merge_permission_trusted_dirs_dedupes_project_already_trusted(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    merged = merge_permission_trusted_dirs([str(project)], str(project))
    assert len(merged) == 1
    assert Path(merged[0]).resolve() == project.resolve()


def test_apply_permission_trusted_dirs_injects_project_into_file_guard(tmp_path: Path):
    project = tmp_path / "web-project"
    project.mkdir()
    rail = build_permission_rail(
        {"permissions": {"enabled": True}},
        session_id="sess_web_project",
    )
    assert rail is not None
    apply_permission_trusted_dirs(rail, trusted_dirs=None, project_dir=str(project))
    trusted = [path.resolve() for path in rail._engine.trusted_dirs]
    assert project.resolve() in trusted


def _patch_permission_layer_paths(tmp_path: Path, monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers

    monkeypatch.setattr(layers, "user_permissions_path", lambda: tmp_path / "user_permissions.yaml")
    monkeypatch.setattr(
        layers,
        "session_permissions_path",
        lambda session_id: tmp_path / session_id / "session_permissions.yaml",
    )
    monkeypatch.setattr(layers, "load_global_permissions", lambda: {})


def test_persist_session_allow_uses_explicit_session_id_when_unbound(
    tmp_path: Path, monkeypatch
) -> None:
    """Rail built without session_id must still persist when the hook gets ctx id."""
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers

    _patch_permission_layer_paths(tmp_path, monkeypatch)
    rail = build_permission_rail({"permissions": {"enabled": True}}, session_id=None)
    assert rail is not None
    hook = rail._host.persist_session_allow_rule
    assert hook is not None
    ok = hook(
        {
            "allow_tools": ["bash"],
            "approval_overrides": [{"id": "x", "action": "allow"}],
        },
        session_id="sess-explicit",
    )
    assert ok is True
    session = layers.load_session_permissions("sess-explicit")
    assert session["allow_tools"] == ["bash"]
    assert session["approval_overrides"][0]["id"] == "x"


def test_persist_session_allow_skips_when_unbound_and_no_explicit_id() -> None:
    rail = build_permission_rail({"permissions": {"enabled": True}}, session_id=None)
    assert rail is not None
    hook = rail._host.persist_session_allow_rule
    assert hook is not None
    assert hook({"allow_tools": ["bash"]}) is False


def test_snapshot_loads_overlay_for_explicit_session_id(
    tmp_path: Path, monkeypatch
) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers

    _patch_permission_layer_paths(tmp_path, monkeypatch)
    rail = build_permission_rail({"permissions": {"enabled": True}}, session_id=None)
    assert rail is not None
    persist = rail._host.persist_session_allow_rule
    snapshot = rail._host.get_permissions_snapshot
    assert persist is not None and snapshot is not None
    assert persist(
        {"allow_tools": ["bash"]},
        session_id="sess-explicit",
    ) is True
    assert layers.load_session_permissions("sess-explicit")["allow_tools"] == ["bash"]

    empty = snapshot()
    tools_empty = empty.get("tools") or {}
    assert tools_empty.get("bash") != "allow"

    overlay = snapshot(session_id="sess-explicit")
    tools = overlay.get("tools") or {}
    assert tools.get("bash") == "allow" or "bash" in (overlay.get("allow_tools") or [])


def test_persist_session_allow_still_uses_bound_session_id(
    tmp_path: Path, monkeypatch
) -> None:
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers

    _patch_permission_layer_paths(tmp_path, monkeypatch)
    rail = build_permission_rail(
        {"permissions": {"enabled": True}},
        session_id="sess-bound",
    )
    assert rail is not None
    hook = rail._host.persist_session_allow_rule
    assert hook is not None
    assert hook({"allow_tools": ["bash"]}) is True
    assert layers.load_session_permissions("sess-bound")["allow_tools"] == ["bash"]
