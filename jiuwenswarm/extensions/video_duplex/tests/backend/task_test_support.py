"""Bounded waits for controlled task-service tests."""

import asyncio


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)
