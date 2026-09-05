"""Tests for bounded human file-delivery approval scopes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.send_file_approval_scope import (
    SendFileHumanApprovalBridge,
    SendFileSessionApprovalStore,
    parse_human_approval_scope,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_audit import (
    _parse_confirmation_payload,
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"approved": True, "auto_confirm": False}, "allow_once"),
        ({"approved": True, "auto_confirm": True}, "session"),
        (
            {"approved": True, "auto_confirm": True, "persist_allow": True},
            "permanent",
        ),
        ({"approved": False}, "reject"),
        ({"approved": True, "auto_confirm": False, "persist_allow": True}, None),
        ({"approved": 1, "auto_confirm": False}, None),
    ],
)
def test_confirmation_aliases_map_to_strict_human_scopes(
    payload: dict[str, Any], expected: str | None
) -> None:
    assert parse_human_approval_scope(payload) == expected


def test_object_confirmation_parser_preserves_persist_allow_alias() -> None:
    payload = _parse_confirmation_payload(
        SimpleNamespace(
            approved=True,
            auto_confirm=True,
            persist_allow=True,
            feedback="remember",
        )
    )

    assert payload == {
        "approved": True,
        "auto_confirm": True,
        "persist_allow": True,
        "feedback": "remember",
    }


def test_session_store_binds_owner_session_tool_and_exact_ask_paths() -> None:
    store = SendFileSessionApprovalStore()
    path_a = ("/external/a.md", "read")
    path_b = ("/external/b.md", "read")

    assert store.remember(
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        accesses=(path_a,),
    )
    assert store.matches(
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        accesses=(path_a,),
    )
    assert not store.matches(
        owner_scope="owner-b",
        session_id="session-a",
        tool_name="send_file_to_user",
        accesses=(path_a,),
    )
    assert not store.matches(
        owner_scope="owner-a",
        session_id="session-b",
        tool_name="send_file_to_user",
        accesses=(path_a,),
    )
    assert not store.matches(
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        accesses=(path_b,),
    )
    assert not store.matches(
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="other_tool",
        accesses=(path_a,),
    )

    assert store.remember(
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        accesses=(path_b,),
    )
    assert store.matches(
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        accesses=(path_a, path_b),
    )
    store.clear_session(owner_scope="owner-a", session_id="session-a")
    assert not store.matches(
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        accesses=(path_a,),
    )


def test_session_scope_without_exact_access_or_session_degrades_to_once() -> None:
    store = SendFileSessionApprovalStore()
    bridge = SendFileHumanApprovalBridge(session_store=store)

    unevaluable = bridge.apply(
        scope="session",
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        tool_args={},
        ask_accesses=(),
    )
    missing_session = bridge.apply(
        scope="session",
        owner_scope="owner-a",
        session_id=None,
        tool_name="send_file_to_user",
        tool_args={},
        ask_accesses=(("/external/a.md", "read"),),
    )

    assert unevaluable.remembered is False
    assert unevaluable.reason == "send_file_approval_accesses_unevaluable"
    assert missing_session.remembered is False
    assert missing_session.reason == "send_file_session_scope_unavailable"


def test_permanent_scope_passes_only_exact_delta_to_host() -> None:
    persisted: list[
        tuple[str, dict[str, Any], tuple[tuple[str, str], ...]]
    ] = []
    bridge = SendFileHumanApprovalBridge(
        session_store=SendFileSessionApprovalStore(),
        exact_persist_callback=(
            lambda tool, args, accesses: persisted.append(
                (tool, args, accesses)
            )
            or True
        ),
    )

    outcome = bridge.apply(
        scope="permanent",
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        tool_args={"abs_file_path_list": "/external/a.md"},
        ask_accesses=(("/external/a.md", "read"),),
    )

    assert outcome.remembered is True
    assert outcome.persisted is True
    assert persisted == [
        (
            "send_file_to_user",
            {"abs_file_path_list": "/external/a.md"},
            (("/external/a.md", "read"),),
        )
    ]


@pytest.mark.parametrize("persist_mode", ["false", "exception"])
def test_permanent_persistence_failure_is_not_remembered(
    persist_mode: str,
) -> None:
    def persist(
        _tool: str,
        _args: dict[str, Any],
        _accesses: tuple[tuple[str, str], ...],
    ) -> bool:
        if persist_mode == "exception":
            raise RuntimeError("disk failed")
        return False

    bridge = SendFileHumanApprovalBridge(
        session_store=SendFileSessionApprovalStore(),
        exact_persist_callback=persist,
    )
    outcome = bridge.apply(
        scope="permanent",
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        tool_args={"abs_file_path_list": "/external/a.md"},
        ask_accesses=(("/external/a.md", "read"),),
    )

    assert outcome.remembered is False
    assert outcome.persisted is False
    assert outcome.reason == "send_file_permanent_persist_failed"


def test_permanent_scope_requires_exact_persist_callback() -> None:
    bridge = SendFileHumanApprovalBridge(
        session_store=SendFileSessionApprovalStore(),
    )

    outcome = bridge.apply(
        scope="permanent",
        owner_scope="owner-a",
        session_id="session-a",
        tool_name="send_file_to_user",
        tool_args={"abs_file_path_list": "/external/a.md"},
        ask_accesses=(("/external/a.md", "read"),),
    )

    assert outcome.remembered is False
    assert outcome.reason == "send_file_permanent_scope_unavailable"
