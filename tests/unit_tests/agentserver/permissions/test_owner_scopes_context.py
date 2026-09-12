from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT,
    cleanup_permission_context,
    current_permission_owner_scope,
    setup_permission_context,
)


def _request(
    *,
    channel_id: str = "web",
    principal_user_id: str = "",
    triggering_user_id: str = "",
    **metadata: object,
) -> SimpleNamespace:
    return SimpleNamespace(
        channel_id=channel_id,
        metadata={
            "principal_user_id": principal_user_id,
            "triggering_user_id": triggering_user_id,
            **metadata,
        },
    )


def test_setup_permission_context_uses_non_avatar_principal_metadata() -> None:
    token = setup_permission_context(
        _request(
            principal_user_id=" principal-42 ",
            triggering_user_id=" trigger-99 ",
        )
    )
    try:
        perm_ctx = TOOL_PERMISSION_CONTEXT.get()
        assert perm_ctx is not None
        assert perm_ctx.channel_id == "web"
        assert perm_ctx.principal_user_id == "principal-42"
        assert perm_ctx.triggering_user_id == "trigger-99"
        assert perm_ctx.avatar_mode is False
        assert current_permission_owner_scope() == "principal:web:principal-42"
    finally:
        cleanup_permission_context(token)

    assert TOOL_PERMISSION_CONTEXT.get() is None
    assert current_permission_owner_scope() == ""


def test_setup_permission_context_preserves_memory_disabled_behavior() -> None:
    token = setup_permission_context(_request(enable_memory=False))
    try:
        perm_ctx = TOOL_PERMISSION_CONTEXT.get()
        assert perm_ctx is not None
        assert perm_ctx.enable_memory is False
        assert perm_ctx.avatar_mode is False
        assert current_permission_owner_scope() == ""
    finally:
        cleanup_permission_context(token)


def test_setup_permission_context_without_principal_keeps_outer_context() -> None:
    outer = setup_permission_context(_request(principal_user_id="outer"))
    try:
        assert setup_permission_context(_request()) is None
        assert current_permission_owner_scope() == "principal:web:outer"
    finally:
        cleanup_permission_context(outer)


def test_nested_permission_context_reset_restores_outer_owner() -> None:
    outer = setup_permission_context(_request(channel_id="", principal_user_id="outer"))
    try:
        assert current_permission_owner_scope() == "principal:default:outer"
        inner = setup_permission_context(
            _request(channel_id="acp", principal_user_id="inner")
        )
        try:
            assert current_permission_owner_scope() == "principal:acp:inner"
        finally:
            cleanup_permission_context(inner)
        assert current_permission_owner_scope() == "principal:default:outer"
    finally:
        cleanup_permission_context(outer)


def test_permission_owner_context_is_task_local() -> None:
    async def run() -> tuple[str, str]:
        async def worker(principal: str) -> str:
            token = setup_permission_context(
                _request(channel_id="web", principal_user_id=principal)
            )
            try:
                await asyncio.sleep(0)
                return current_permission_owner_scope()
            finally:
                cleanup_permission_context(token)

        results = await asyncio.gather(worker("a"), worker("b"))
        return results[0], results[1]

    assert asyncio.run(run()) == ("principal:web:a", "principal:web:b")
    assert current_permission_owner_scope() == ""
