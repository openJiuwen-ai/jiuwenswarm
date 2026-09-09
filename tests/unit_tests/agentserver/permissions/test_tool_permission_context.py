from __future__ import annotations

import asyncio

from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_REQUEST_ID,
)


def test_tool_permission_context_defaults_are_empty() -> None:
    assert TOOL_PERMISSION_CHANNEL_ID.get() == ""
    assert TOOL_PERMISSION_REQUEST_ID.get() == ""


def test_tool_permission_context_tokens_restore_previous_values() -> None:
    channel_token = TOOL_PERMISSION_CHANNEL_ID.set("web")
    request_token = TOOL_PERMISSION_REQUEST_ID.set("request-1")
    try:
        assert TOOL_PERMISSION_CHANNEL_ID.get() == "web"
        assert TOOL_PERMISSION_REQUEST_ID.get() == "request-1"
    finally:
        TOOL_PERMISSION_REQUEST_ID.reset(request_token)
        TOOL_PERMISSION_CHANNEL_ID.reset(channel_token)

    assert TOOL_PERMISSION_CHANNEL_ID.get() == ""
    assert TOOL_PERMISSION_REQUEST_ID.get() == ""


def test_tool_permission_context_is_task_local() -> None:
    async def run() -> tuple[tuple[str, str], tuple[str, str]]:
        async def worker(suffix: str) -> tuple[str, str]:
            request_token = TOOL_PERMISSION_REQUEST_ID.set(f"request-{suffix}")
            channel_token = TOOL_PERMISSION_CHANNEL_ID.set(f"channel-{suffix}")
            try:
                await asyncio.sleep(0)
                return (
                    TOOL_PERMISSION_REQUEST_ID.get(),
                    TOOL_PERMISSION_CHANNEL_ID.get(),
                )
            finally:
                TOOL_PERMISSION_CHANNEL_ID.reset(channel_token)
                TOOL_PERMISSION_REQUEST_ID.reset(request_token)

        first, second = await asyncio.gather(worker("a"), worker("b"))
        return first, second

    assert asyncio.run(run()) == (
        ("request-a", "channel-a"),
        ("request-b", "channel-b"),
    )
    assert TOOL_PERMISSION_REQUEST_ID.get() == ""
