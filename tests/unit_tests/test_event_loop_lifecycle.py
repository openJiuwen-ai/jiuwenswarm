# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Regression coverage for event loops shared by pytest-asyncio tests."""

import asyncio

import pytest


def test_sync_asyncio_run_leaves_no_current_loop() -> None:
    asyncio.run(asyncio.sleep(0))

    with pytest.raises(RuntimeError):
        asyncio.get_event_loop()


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("iteration", [1, 2])
async def test_module_scoped_event_loop_survives_test_teardown(iteration: int) -> None:
    assert await asyncio.sleep(0, result=iteration) == iteration


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("iteration", [1, 2])
async def test_session_scoped_event_loop_survives_test_teardown(iteration: int) -> None:
    assert await asyncio.sleep(0, result=iteration) == iteration
