# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Enterprise rule details use SDK evidence without changing approval behavior."""

from copy import deepcopy

import pytest
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)


def permission_interaction(metadata):
    return {
        "id": "call-rule-display",
        "value": {
            "tool_name": "write_file",
            "tool_args": {"path": "example.txt", "content": "example"},
            "message": "工具 `write_file` 需要授权才能执行",
            "metadata": metadata,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_rule,expected", [(True, "tools.write_file"), (False, "tiered_policy:defaults.*")])
async def test_sdk_rule_reaches_enterprise_question_without_changing_approval(
    monkeypatch: pytest.MonkeyPatch,
    tool_rule: bool,
    expected: str,
) -> None:
    """Preserve SDK approval evidence while applying enterprise UI restrictions."""
    config = {
        "enabled": True, "package_builtin_rules": False,
        "defaults": {"*": "ask"}, "file_guard": {"enabled": False},
        "tools": {"write_file": "ask"} if tool_rule else {},
    }
    rail = PermissionInterruptRail(config=config)
    args = {"path": "example.txt", "content": "example"}
    result = await rail._engine.check_permission("write_file", args)
    assert result.needs_approval and result.matched_rule == expected
    call = ToolCall(id="call-rule-display", name="write_file", type="function", arguments='{"path":"example.txt"}')
    interaction = permission_interaction(rail._build_interrupt_metadata(call, result))
    interaction["value"]["message"] = rail._build_message(call, result)
    original = deepcopy(interaction)

    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    personal = convert_interactions_to_ask_user_question([interaction])
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    enterprise = convert_interactions_to_ask_user_question([interaction])

    assert enterprise["source"] == "permission_interrupt"
    assert enterprise["questions"][0].pop("matched_rule") == expected
    personal_question = personal["questions"][0]
    enterprise_question = enterprise["questions"][0]
    assert [option["value"] for option in personal_question["options"]] == [
        "allow_once", "session_allow", "always_allow", "reject",
    ]
    assert [option["value"] for option in enterprise_question["options"]] == [
        "allow_once", "session_allow", "reject",
    ]
    assert "永久记住" in personal_question["question"]
    assert "永久记住" not in enterprise_question["question"]
    assert "会话内记住" in enterprise_question["question"]
    expected_enterprise = deepcopy(personal)
    expected_question = expected_enterprise["questions"][0]
    expected_question["options"].pop(2)
    expected_question["question"] = personal_question["question"].split("；选择「永久记住」", 1)[0]
    assert enterprise == expected_enterprise
    assert interaction == original
    assert result.needs_approval and result.matched_rule == expected


@pytest.mark.parametrize("edition", ["personal", ""])
def test_personal_payload_is_unchanged_even_when_sdk_provides_a_rule(monkeypatch, edition):
    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    without_rule = permission_interaction({"ask_title": "Permission"})
    with_rule = permission_interaction({"ask_title": "Permission", "matched_rule": "tools.write_file"})
    assert convert_interactions_to_ask_user_question([with_rule]) == convert_interactions_to_ask_user_question([without_rule])


@pytest.mark.parametrize("rule", [None, "", "  ", 123, {"rule": "tools.write_file"}])
def test_enterprise_does_not_invent_missing_or_malformed_rules(monkeypatch, rule):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    payload = convert_interactions_to_ask_user_question([permission_interaction({"matched_rule": rule})])
    assert "matched_rule" not in payload["questions"][0]


def test_enterprise_uses_request_rule_before_outer_metadata_and_redacts_secrets(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    interaction = permission_interaction({"matched_rule": "rules[write]: api_key=display-test-secret"})
    interaction["metadata"] = {"matched_rule": "outer-rule"}
    payload = convert_interactions_to_ask_user_question([interaction])
    rule = payload["questions"][0]["matched_rule"]
    assert "rules[write]" in rule and "display-test-secret" not in rule and "outer-rule" not in rule


def test_enterprise_reads_rule_from_outer_interaction_metadata(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    interaction = permission_interaction({})
    interaction["metadata"] = {"matched_rule": "defaults.*"}
    payload = convert_interactions_to_ask_user_question([interaction])
    assert payload["questions"][0]["matched_rule"] == "defaults.*"


def test_enterprise_does_not_change_control_confirmation(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    interaction = permission_interaction({"matched_rule": "tools.switch_mode"})
    interaction["value"].update(tool_name="switch_mode", message="Please approve or reject?")
    payload = convert_interactions_to_ask_user_question([interaction])
    assert payload["source"] == "confirm_interrupt"
    assert "matched_rule" not in payload["questions"][0]
