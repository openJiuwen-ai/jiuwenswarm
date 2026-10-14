"""Semantic evidence helpers; snapshots never retain live SDK handles."""
import asyncio
import inspect
import math
from collections.abc import Mapping
from ..._identity import Record


def clone(value, memo=None):
    if value is None or type(value) in (str, bool, int, float): return value
    if memo is None: memo = {}
    if id(value) in memo: return memo[id(value)]
    if isinstance(value, Mapping):
        result = Record(); memo[id(value)] = result
        for key, item in value.items(): result[key] = clone(item, memo)
        return result
    if isinstance(value, (list, tuple)):
        result = []; memo[id(value)] = result
        result.extend(clone(item, memo) for item in value)
        return result
    if isinstance(value, (bytes, bytearray)): return type(value)(value)
    raise TypeError("Evidence must contain ordinary data, not SDK handles or accessors.")


def equal(left, right, seen=None):
    if left is right: return True
    if type(left) in (int, float) and type(right) in (int, float):
        if math.isnan(left) and math.isnan(right): return True
        if left == right == 0: return math.copysign(1, left) == math.copysign(1, right)
        return left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right): return False
        pairs = ((left[key], right[key]) for key in left)
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right): return False
        pairs = zip(left, right)
    else: return type(left) is type(right) and left == right
    if seen is None: seen = set()
    pair = (id(left), id(right))
    if pair in seen: return True
    seen.add(pair)
    return all(equal(a, b, seen) for a, b in pairs)


async def resolve(value):
    return await value if inspect.isawaitable(value) else value


def member(target, key):
    return target.get(key) if isinstance(target, Mapping) else getattr(target, key, None)


def scheduled(run, pending):
    # Like Promise.resolve().then(...), do not run user code synchronously.
    async def operation():
        try: return await resolve(run())
        finally: pending.discard(asyncio.current_task())
    task = asyncio.create_task(operation()); pending.add(task)
    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    return task


async def drain(pending):
    if pending: await asyncio.gather(*list(pending), return_exceptions=True)


def errorText(error):
    return f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)


def slice16(text, start, end=None):
    raw = text.encode("utf-16-le", "surrogatepass")
    return raw[start * 2:None if end is None else end * 2].decode("utf-16-le", "surrogatepass")
