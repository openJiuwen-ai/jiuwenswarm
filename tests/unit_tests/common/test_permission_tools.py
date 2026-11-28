# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the permission tool-name resolution helpers."""

import pytest

from jiuwenswarm.common.permission_tools import (
    PERMISSION_LEVEL_RANK,
    PERMISSION_TOOL_ALIASES,
    PermissionToolNameResolution,
    normalize_permission_tool_name,
    resolve_permission_tool_name,
    stricter_permission_level,
)


class TestResolvePermissionToolName:
    def test_alias_resolves_to_canonical_name(self):
        result = resolve_permission_tool_name("exec_command")
        assert result == PermissionToolNameResolution(
            registered_name="exec_command",
            canonical_name="mcp_exec_command",
            aliases=("exec_command",),
            conflict=False,
        )

    @pytest.mark.parametrize(
        ("alias", "canonical"),
        list(PERMISSION_TOOL_ALIASES.items()),
    )
    def test_every_alias_maps_to_its_canonical(self, alias: str, canonical: str):
        result = resolve_permission_tool_name(alias)
        assert result.canonical_name == canonical
        assert result.conflict is False
        assert alias in result.aliases

    def test_canonical_name_is_not_re_resolved(self):
        result = resolve_permission_tool_name("mcp_exec_command")
        assert result.canonical_name == "mcp_exec_command"
        assert result.aliases == ("exec_command",)
        assert result.conflict is False

    def test_unknown_name_passes_through_unchanged(self):
        result = resolve_permission_tool_name("some_other_tool")
        assert result.registered_name == "some_other_tool"
        assert result.canonical_name == "some_other_tool"
        assert result.aliases == ()
        assert result.conflict is False

    @pytest.mark.parametrize("tool_name", [None, ""])
    def test_none_and_empty_string_resolve_to_empty(self, tool_name):
        result = resolve_permission_tool_name(tool_name)
        assert result.registered_name == ""
        assert result.canonical_name == ""
        assert result.aliases == ()
        assert result.conflict is False

    def test_non_string_is_stringified(self):
        result = resolve_permission_tool_name(123)
        assert result.registered_name == "123"
        assert result.canonical_name == "123"
        assert result.conflict is False

    def test_whitespace_is_stripped(self):
        result = resolve_permission_tool_name("  exec_command  ")
        assert result.registered_name == "exec_command"
        assert result.canonical_name == "mcp_exec_command"
        assert result.conflict is False

    def test_alias_also_registered_canonically_fails_closed(self):
        result = resolve_permission_tool_name(
            "exec_command", canonical_names=("exec_command",)
        )
        assert result.conflict is True
        assert result.canonical_name == ""
        assert result.aliases == ()

    def test_no_conflict_when_canonical_names_is_empty(self):
        result = resolve_permission_tool_name("exec_command", canonical_names=())
        assert result.conflict is False
        assert result.canonical_name == "mcp_exec_command"

    def test_unstringifiable_name_fails_closed(self):
        class Unstringifiable:
            def __str__(self) -> str:
                raise RuntimeError("no str for you")

        result = resolve_permission_tool_name(Unstringifiable())
        assert result == PermissionToolNameResolution(
            registered_name="",
            canonical_name="",
            aliases=(),
            conflict=True,
        )


class TestNormalizePermissionToolName:
    def test_alias_returns_canonical(self):
        assert normalize_permission_tool_name("exec_command") == "mcp_exec_command"

    def test_canonical_returns_itself(self):
        assert normalize_permission_tool_name("mcp_exec_command") == "mcp_exec_command"

    def test_unknown_returns_input_string(self):
        assert normalize_permission_tool_name("read_file") == "read_file"

    def test_none_returns_empty_string(self):
        assert normalize_permission_tool_name(None) == ""


class TestStricterPermissionLevel:
    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            ("allow", "deny", "deny"),
            ("deny", "allow", "deny"),
            ("deny", "ask", "deny"),
            ("ask", "deny", "deny"),
            ("ask", "allow", "ask"),
            ("allow", "ask", "ask"),
        ],
    )
    def test_known_levels_pick_stricter(self, left: str, right: str, expected: str):
        assert stricter_permission_level(left, right) == expected

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("DENY", "allow"),
            ("deny", "DENY"),
            (" ASK ", "allow"),
        ],
    )
    def test_input_is_normalized_case_and_whitespace(self, left: str, right: str):
        result = stricter_permission_level(left, right)
        assert result == result.strip().lower()

    def test_unknown_level_treated_as_ask_but_returned_verbatim(self):
        # "weird" ranks as ask (1), which is stricter than allow (0), so the
        # left side wins and is returned in normalized (stripped/lower) form.
        assert stricter_permission_level("weird", "allow") == "weird"
        assert stricter_permission_level("allow", "weird") == "weird"

    def test_none_defaults_to_ask(self):
        assert stricter_permission_level(None, "allow") == "ask"
        assert stricter_permission_level("deny", None) == "deny"

    def test_empty_string_defaults_to_ask(self):
        assert stricter_permission_level("", "allow") == "ask"
        assert stricter_permission_level("deny", "") == "deny"

    def test_rank_table_is_total_order(self):
        assert PERMISSION_LEVEL_RANK["allow"] < PERMISSION_LEVEL_RANK["ask"]
        assert PERMISSION_LEVEL_RANK["ask"] < PERMISSION_LEVEL_RANK["deny"]
