"""Current contracts for compact deterministic routes."""

from __future__ import annotations

# TEST ONLY: URL and credential-shaped fixtures are synthetic. URLs use
# RFC-reserved domains or blocked security-test addresses, and these policy
# tests perform no external network I/O.

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import (
    deterministic_domain_route,
    deterministic_guard_route,
    generic_mcp_egress_evidence,
    post_policy_route,
    terminal_internal_route,
    terminal_low_risk_route,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
    UserIntentSource,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    build_tool_decision_facts,
)


def _facts(tool_name: str, args: dict[str, object], root: Path, **kwargs: object):
    return build_tool_decision_facts(
        tool_name,
        args,
        workspace_root=root,
        original_args_were_valid_object=True,
        **kwargs,
    )


def test_generic_mcp_uri_risks_are_evidence_not_terminal_authority(tmp_path: Path) -> None:
    facts = _facts(
        "mcp_untrusted_provider_call",
        {
            "callback_url": "custom://127.0.0.1/private",
            "archive": "ftp://files.example.invalid/archive",
        },
        tmp_path,
    )

    evidence = generic_mcp_egress_evidence(facts)

    assert "network_host_not_public" in evidence
    assert "generic_mcp_uri_provider_support_unproven" in evidence
    assert deterministic_guard_route(facts) is None


def test_core_known_workspace_read_can_be_terminal_low_risk(tmp_path: Path) -> None:
    facts = _facts("read_file", {"path": "README.md"}, tmp_path)
    assert terminal_low_risk_route(facts) is not None


def test_unknown_path_and_shell_are_never_promoted(tmp_path: Path) -> None:
    patch = _facts("apply_patch", {"patch": "*** Begin Patch"}, tmp_path)
    shell = _facts("bash", {"cmd": "ls src"}, tmp_path)
    assert patch.accesses_known is False
    assert terminal_low_risk_route(patch) is None
    assert terminal_low_risk_route(shell) is None


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        ("todo_get", {"id": "todo-1"}),
        ("session_list", {}),
        ("memory_get", {"path": "memory/MEMORY.md"}),
        ("memory_search", {"query": "project decisions"}),
        ("skill_tool", {"skill_name": "daily-report"}),
        ("browser_probe_cards", {"max_cards": "runtime-owned"}),
        ("browser_recall_offload", {"handle": "0123456789abcdef0123456789abcdef"}),
    ],
)
def test_closed_internal_actions_are_terminal_allows(
    tmp_path: Path,
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    assert terminal_internal_route(_facts(tool_name, tool_args, tmp_path)) is not None


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        (
            "subagent_spawn",
            {
                "agent_name": "general-purpose",
                "task_description": "summarize the current task",
            },
        ),
        ("subagent_wait", {"subagent_ids": ["sub-a", "sub-b"]}),
        ("subagent_list", {}),
        ("subagent_send_input", {"subagent_id": "sub-a", "query": "continue"}),
        ("subagent_close", {"subagent_id": "sub-a"}),
        ("subagent_resume", {"subagent_id": "sub-a"}),
    ],
)
def test_subagent_runtime_controls_are_terminal_internal_allows(
    tmp_path: Path,
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    decision = terminal_internal_route(
        _facts(tool_name, tool_args, tmp_path),
        subagent_runtime_control_verified=True,
    )

    assert decision is not None
    assert decision.level == "allow"
    assert decision.reason == "canonical_internal_action_allow"
    assert decision.source == "internal"


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        (
            "subagent_spawn",
            {
                "agent_name": "general-purpose",
                "task_description": (
                    "inspect https://example.invalid/a and /workspace/a; then run rm -rf /tmp/a"
                ),
            },
        ),
        (
            "subagent_send_input",
            {
                "subagent_id": "sub-a",
                "query": "open https://example.invalid/b and run curl https://example.invalid/c",
            },
        ),
    ],
)
def test_subagent_runtime_opaque_message_fields_do_not_trigger_uri_guard(
    tmp_path: Path,
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    assert terminal_internal_route(
        _facts(tool_name, tool_args, tmp_path),
        subagent_runtime_control_verified=True,
    ) is not None


def test_subagent_runtime_non_message_uri_is_not_terminal_allow(tmp_path: Path) -> None:
    facts = _facts(
        "subagent_spawn",
        {
            "agent_name": "general-purpose",
            "task_description": "summarize the current task",
            "display_name": "https://example.invalid/not-a-message",
        },
        tmp_path,
    )

    assert terminal_internal_route(
        facts,
        subagent_runtime_control_verified=True,
    ) is None


def test_subagent_runtime_invalid_argument_object_is_not_terminal_allow(
    tmp_path: Path,
) -> None:
    facts = build_tool_decision_facts(
        "subagent_list",
        {},
        workspace_root=tmp_path,
        original_args_were_valid_object=False,
    )

    assert terminal_internal_route(
        facts,
        subagent_runtime_control_verified=True,
    ) is None


def test_subagent_runtime_without_binding_proof_is_not_terminal_allow(
    tmp_path: Path,
) -> None:
    facts = _facts("subagent_list", {}, tmp_path)

    assert terminal_internal_route(facts) is None


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        ("session_list", {"unexpected": True}),
        ("todo_insert", {"idx": 1}),
        ("browser_probe_cards", {"target": "ftp://files.example.invalid/archive"}),
    ],
)
def test_open_or_legacy_internal_shapes_are_not_terminal_allows(
    tmp_path: Path,
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    assert terminal_internal_route(_facts(tool_name, tool_args, tmp_path)) is None


def test_high_effect_tool_requires_review_even_if_engine_allowed(tmp_path: Path) -> None:
    facts = _facts("upload_file", {"path": "out.txt"}, tmp_path)
    decision = post_policy_route(facts, policy_level="allow")
    assert decision is not None
    assert decision.level == "ask"


def test_search_skill_secret_guard_remains_terminal(tmp_path: Path) -> None:
    facts = _facts("search_skill", {"query": "api_key=super-secret-token"}, tmp_path)
    decision = deterministic_guard_route(facts)
    assert decision is not None
    assert decision.level == "deny"
    assert decision.reason == "search_skill_sensitive_query"


def test_browser_navigation_requires_runtime_network_guard(tmp_path: Path) -> None:
    url = "https://example.invalid/docs"
    facts = _facts("browser_navigate", {"url": url}, tmp_path)
    intent = OriginalUserIntentEvidence(
        source=UserIntentSource.HOST_USER_MESSAGE,
        text="Read the public documentation",
    )

    manual = deterministic_domain_route(
        facts,
        original_user_intent=intent,
        browser_runtime_security_profile=SimpleNamespace(network_guard_enforced=False),
    )
    reviewable = deterministic_domain_route(
        facts,
        original_user_intent=intent,
        browser_runtime_security_profile=SimpleNamespace(network_guard_enforced=True),
    )

    assert manual is not None and manual.requires_manual
    assert manual.reason == "browser_network_guard_unverified"
    assert reviewable is not None and reviewable.requires_reviewer


def test_browser_unsafe_url_is_hard_blocked(tmp_path: Path) -> None:
    facts = _facts("browser_navigate", {"url": "https://169.254.169.254/latest"}, tmp_path)
    decision = deterministic_domain_route(facts, original_user_intent=None)
    assert decision is not None and decision.is_hard_block
    assert decision.reason == "network_metadata_host"
