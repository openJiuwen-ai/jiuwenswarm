# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for MCP CancelledError outer/internal discrimination."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.common.mcp_config import is_asyncio_outer_cancellation


@pytest.mark.asyncio
async def test_is_asyncio_outer_cancellation_false_when_not_cancelled() -> None:
    assert is_asyncio_outer_cancellation() is False


@pytest.mark.asyncio
async def test_is_asyncio_outer_cancellation_true_after_task_cancel() -> None:
    seen: list[bool] = []

    async def _body() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        seen.append(is_asyncio_outer_cancellation())
        raise asyncio.CancelledError()

    task = asyncio.create_task(_body())
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == [True]
