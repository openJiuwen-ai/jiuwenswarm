"""Batching helpers for fingerprint extraction stages."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Iterable, List, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def chunked(items: Sequence[T], batch_size: int) -> List[List[T]]:
    size = max(1, int(batch_size))
    return [list(items[start: start + size]) for start in range(0, len(items), size)]


async def gather_limited(
    batches: Iterable[List[T]],
    *,
    max_workers: int,
    run_batch: Callable[[List[T]], Awaitable[R]],
) -> List[R]:
    semaphore = asyncio.Semaphore(max(1, int(max_workers)))

    async def run_with_limit(batch: List[T]) -> R:
        async with semaphore:
            return await run_batch(batch)

    return await asyncio.gather(*(run_with_limit(batch) for batch in batches))


def ensure_result_count(actual: int, expected: int, label: str) -> None:
    if actual != expected:
        raise RuntimeError(
            f"{label} returned {actual} results for {expected} inputs."
        )
