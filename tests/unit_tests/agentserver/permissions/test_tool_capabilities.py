# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for auto permission tool capability taxonomy."""

from __future__ import annotations

import pytest

from openjiuwen.harness.security.permission_engine.fileguard.file_tool_specs import (
    FileToolSpec,
)

from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
    classify_tool,
    install_permission_file_semantics,
    normalize_tool_name,
)


def test_classifies_shell_and_shell_kinds() -> None:
    assert classify_tool("bash").category == "shell"
    assert classify_tool("cmd").category == "shell"
    assert classify_tool("powershell").category == "shell"
    assert classify_tool("mcp_exec_command").category == "shell"
    assert classify_tool("exec_command").category == "shell"


def test_classifies_high_effect_tools() -> None:
    assert "schedule" in classify_tool("cron_create_job").static_side_effects
    assert "external_send" in classify_tool("upload_file").static_side_effects
    assert "device_action" in classify_tool("call_phone").static_side_effects
    assert "memory_write" in classify_tool("mem0_conclude").static_side_effects


def test_domain_tools_remain_high_flex_by_default() -> None:
    for tool_name in (
        "browser_snapshot",
        "cron_preview_job",
        "task_tool",
        "write_memory",
    ):
        capability = classify_tool(tool_name)
        assert capability.high_flex is True


@pytest.mark.parametrize(
    "tool_name",
    [
        "subagent_spawn",
        "subagent_wait",
        "subagent_list",
        "subagent_send_input",
        "subagent_close",
        "subagent_resume",
    ],
)
def test_exact_subagent_runtime_tools_have_closed_internal_capability(
    tool_name: str,
) -> None:
    capability = classify_tool(tool_name)

    assert capability.registered_name == tool_name
    assert capability.tool_name == tool_name
    assert capability.aliases == ()
    assert capability.category == "subagent"
    assert capability.operation_family == "subagent_runtime_control"
    assert capability.static_side_effects == frozenset({"delegation"})
    assert capability.risk_tier == "low"
    assert capability.high_flex is False
    assert capability.facts_source == "host_static"


@pytest.mark.parametrize(
    "tool_name",
    [
        "subagent_future_tool",
        "Subagent_spawn",
        "SUBAGENT_WAIT",
        "subagent_list ",
        " subagent_resume",
    ],
)
def test_subagent_runtime_name_variants_are_not_host_static(tool_name: str) -> None:
    capability = classify_tool(tool_name)

    assert capability.operation_family != "subagent_runtime_control"
    assert capability.facts_source != "host_static"


def test_subagent_runtime_aliases_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.common.permission_tools as permission_tools

    monkeypatch.setitem(
        permission_tools.PERMISSION_TOOL_ALIASES,
        "subagent_runtime_alias",
        "subagent_spawn",
    )

    assert classify_tool("subagent_runtime_alias").facts_source != "host_static"
    assert classify_tool("subagent_spawn").facts_source != "host_static"


def test_subagent_runtime_alias_conflict_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jiuwenswarm.common.permission_tools as permission_tools

    monkeypatch.setitem(
        permission_tools.PERMISSION_TOOL_ALIASES,
        "subagent_spawn",
        "bash",
    )

    capability = classify_tool("subagent_spawn")
    assert capability.alias_conflict is True
    assert capability.facts_source == "unknown"


def test_unknown_mcp_is_high_flex() -> None:
    info = classify_tool("mcp_unknown_server_tool")
    assert info.category == "mcp"
    assert info.high_flex is True


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp_free_search",
        "mcp_paid_search",
        "mcp_fetch_webpage",
        "free_search",
        "paid_search",
        "fetch_webpage",
    ],
)
def test_search_and_fetch_tools_remain_network(tool_name: str) -> None:
    assert classify_tool(tool_name).category == "network"


def test_playwright_browser_mcp_wrapper_uses_browser_policy_domain() -> None:
    info = classify_tool("mcp_playwright-official_browser_navigate")
    assert info.category == "browser"
    assert "browser_action" in info.static_side_effects
    assert info.high_flex is True


def test_playwright_browser_mcp_unsafe_wrapper_uses_browser_policy_domain() -> None:
    info = classify_tool("mcp_playwright-official_browser_run_code_unsafe")
    assert info.category == "browser"


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp_playwright_browser_navigate",
        "mcp_playwright-officially_browser_navigate",
        "mcp_playwright-official_browserish_navigate",
        "mcp_unknown_browser_navigate",
    ],
)
def test_untrusted_or_spoofed_mcp_with_browser_word_remains_untrusted_mcp(
    tool_name: str,
) -> None:
    info = classify_tool(tool_name)
    assert info.category == "mcp"


def test_web_tool_aliases_are_normalized() -> None:
    assert normalize_tool_name("free_search") == "mcp_free_search"
    assert normalize_tool_name("paid_search") == "mcp_paid_search"
    assert normalize_tool_name("fetch_webpage") == "mcp_fetch_webpage"
    assert normalize_tool_name("exec_command") == "mcp_exec_command"


@pytest.mark.parametrize(
    ("tool_name", "operation_family", "side_effect"),
    [
        ("free_search", "public_search", "network_search"),
        ("paid_search", "public_search", "network_search"),
        ("fetch_webpage", "public_web_read", "network_read"),
    ],
)
def test_public_web_tools_project_canonical_security_facts(
    tool_name: str,
    operation_family: str,
    side_effect: str,
) -> None:
    info = classify_tool(tool_name)

    assert info.operation_family == operation_family
    assert info.resource_class == "public_web"
    assert info.static_side_effects == frozenset({side_effect})
    assert info.facts_source == "host_static"


def test_malformed_tool_names_normalize_to_empty_fallback() -> None:
    assert normalize_tool_name(None) == ""
    assert normalize_tool_name(0) == ""
    assert normalize_tool_name("") == ""
    assert normalize_tool_name("   ") == ""


def test_search_skill_is_medium_skill_discovery_without_high_flex() -> None:
    info = classify_tool("search_skill")
    assert info.category == "skill"
    assert info.risk_tier == "medium"
    assert info.high_flex is False
    assert info.static_side_effects == frozenset()


def test_session_list_is_low_risk_task_status_query() -> None:
    info = classify_tool("session_list")
    assert info.category == "task_management"
    assert info.risk_tier == "low"
    assert info.high_flex is False
    assert info.static_side_effects == frozenset()


def test_install_uninstall_skill_remain_high_risk_manual_tools() -> None:
    for tool_name in ("install_skill", "uninstall_skill"):
        info = classify_tool(tool_name)
        assert info.category == "skill"
        assert info.risk_tier == "high"
        assert info.high_flex is True
        assert "skill_change" in info.static_side_effects


def test_skill_tool_is_a_canonical_read_capability() -> None:
    info = classify_tool("skill_tool")

    assert info.category == "skill"
    assert info.operation_family == "skill_read"
    assert info.risk_tier == "low"
    assert info.high_flex is False
    assert info.static_side_effects == frozenset()


def test_only_host_local_memory_reads_have_static_authority() -> None:
    assert classify_tool("memory_get").operation_family == "memory_read"
    assert classify_tool("memory_search").operation_family == "memory_search"
    reviewed = classify_tool("mem0_search")
    assert reviewed.facts_source == "name_classification_hint"
    assert reviewed.high_flex is True


@pytest.mark.parametrize(
    ("tool_name", "operation_family", "side_effects"),
    [
        (
            "browser_probe_cards",
            "browser_session_observe",
            frozenset({"session_cache_write"}),
        ),
        (
            "browser_probe_interactives",
            "browser_session_observe",
            frozenset({"session_cache_write"}),
        ),
        ("browser_recall_offload", "browser_session_recall", frozenset()),
    ],
)
def test_fixed_browser_tools_have_source_proven_security_facts(
    tool_name: str,
    operation_family: str,
    side_effects: frozenset[str],
) -> None:
    info = classify_tool(tool_name)

    assert info.operation_family == operation_family
    assert info.static_side_effects == side_effects
    assert info.risk_tier == "low"
    assert info.high_flex is False


def test_capability_exposes_facts_without_policy_default() -> None:
    info = classify_tool("read_file")

    assert info.registered_name == "read_file"
    assert info.operation_family == "workspace_read"
    assert info.resource_class == "workspace_path"
    assert info.facts_version == "2"
    assert not hasattr(info, "side_effects")
    assert not hasattr(info, "default_level")


def test_read_pdf_is_a_static_read_path_tool() -> None:
    info = classify_tool("read_pdf")

    assert info.category == "path"
    assert info.operation_family == "workspace_read"
    assert info.static_side_effects == frozenset()
    assert info.facts_source == "host_static"


def test_permission_file_semantics_installer_is_explicit_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities as mod

    state: list[FileToolSpec] = []
    monkeypatch.setattr(
        mod,
        "lookup_file_tool_specs",
        lambda _tool_name: list(state) or None,
    )
    monkeypatch.setattr(mod, "register_file_tool", state.append)

    install_permission_file_semantics()
    install_permission_file_semantics()

    assert state == [FileToolSpec("read_pdf", "pdf_path", "read")]


def test_permission_file_semantics_installer_rejects_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities as mod

    monkeypatch.setattr(
        mod,
        "lookup_file_tool_specs",
        lambda _tool_name: [FileToolSpec("read_pdf", "path", "write")],
    )

    with pytest.raises(RuntimeError, match="permission_file_semantics_conflict:read_pdf"):
        install_permission_file_semantics()


def test_host_todo_tools_have_static_authority() -> None:
    assert classify_tool("todo_get").operation_family == "task_state"


@pytest.mark.parametrize("tool_name", ["todo_insert", "todo_complete", "todo_remove"])
def test_legacy_todo_names_remain_non_authoritative(tool_name: str) -> None:
    info = classify_tool(tool_name)

    assert info.operation_family == "task_state"
    assert info.facts_source == "name_classification_hint"
    assert info.high_flex is True
