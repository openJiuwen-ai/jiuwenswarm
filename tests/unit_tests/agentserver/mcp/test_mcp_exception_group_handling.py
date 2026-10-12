# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: MCP SDK BaseExceptionGroup escape coercion.

When a remote streamable-http MCP server returns an HTTP error (403/500/...),
the mcp SDK's anyio task group raises a ``BaseExceptionGroup`` containing the
real exception (e.g. httpx.HTTPStatusError) AND a ``GeneratorExit`` (from the
generator-based client context being closed during teardown). A
``BaseExceptionGroup`` is only an ``Exception`` subclass when *every* sub-
exception is — a group containing ``GeneratorExit`` is NOT, so it escapes
every ``except Exception`` and hangs the frontend.

These tests pin:
  1. The root cause (such a group is not caught by ``except Exception``).
  2. ``reraise_as_exception`` unwraps the real Exception subgroup so its
     message reaches the caller.
  3. ``reraise_as_exception`` wraps a pure BaseException subgroup (GeneratorExit
     / CancelledError) in a RuntimeError that ``except Exception`` catches.
  4. KeyboardInterrupt is preserved (Ctrl+C still works).
"""
from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.runtime.mcp.exc_group import reraise_as_exception


def _group_with_generator_exit(real_exc: Exception) -> BaseExceptionGroup:
    """Mirror the real mcp SDK failure shape: a group with one real Exception
    sub-exception plus a GeneratorExit (which is a BaseException, not an
    Exception). This group is NOT an Exception subclass."""
    return BaseExceptionGroup("mcp task group teardown", [real_exc, GeneratorExit()])


def test_root_cause_group_with_generator_exit_not_caught_by_except_exception() -> None:
    """Pin the root cause: a BaseExceptionGroup containing GeneratorExit is
    NOT an Exception subclass, so `except Exception` lets it escape. This is
    why the frontend hangs — every `except Exception` in the handler chain
    misses it."""
    group = _group_with_generator_exit(ValueError("403 Forbidden"))
    assert not issubclass(type(group), Exception)
    caught_by_exception = False
    try:
        try:
            raise group
        except Exception:  # noqa: BLE001 — the point of the test
            caught_by_exception = True
    except BaseException:  # noqa: BLE001 — re-caught at the outer level
        pass
    assert not caught_by_exception, (
        "BaseExceptionGroup with GeneratorExit must NOT be caught by except Exception"
    )


def test_reraise_wraps_pure_exception_group_unwraps_single_sub() -> None:
    """When the group has ONLY Exception sub-exceptions (no BaseException
    subgroup), reraise_as_exception unwraps a single sub-exception so its
    message propagates directly (rather than the opaque 'N sub-exceptions'
    str of a group)."""
    real = ValueError("Client error '403 Forbidden'")
    group = BaseExceptionGroup("clean", [real])
    with pytest.raises(ValueError, match="403 Forbidden"):
        try:
            raise group
        except BaseException as exc:
            reraise_as_exception(exc)


def test_reraise_wraps_pure_baseexception_subgroup_in_runtime_error() -> None:
    """A group with ONLY BaseException sub-exceptions (no Exception subgroup)
    is wrapped in a RuntimeError so it becomes catchable by `except Exception`."""
    group = BaseExceptionGroup("teardown", [GeneratorExit(), asyncio.CancelledError()])
    caught: Exception | None = None
    try:
        try:
            raise group
        except BaseException as exc:
            reraise_as_exception(exc)
    except Exception as exc:  # now catchable
        caught = exc
    assert caught is not None
    assert isinstance(caught, RuntimeError)
    # The wrapper carries enough info to debug which BaseException escaped.
    assert "GeneratorExit" in str(caught) or "CancelledError" in str(caught)


def test_reraise_preserves_keyboard_interrupt() -> None:
    """Ctrl+C must still work — KeyboardInterrupt is re-raised verbatim, not
    swallowed into a RuntimeError (otherwise the process couldn't be
    interrupted from the keyboard during a hung MCP call)."""
    with pytest.raises(KeyboardInterrupt):
        try:
            raise KeyboardInterrupt()
        except BaseException as exc:
            reraise_as_exception(exc)


def test_reraise_plain_exception_unchanged() -> None:
    """A plain Exception is re-raised unchanged (no wrapping)."""
    real = RuntimeError("plain failure")
    with pytest.raises(RuntimeError, match="plain failure"):
        try:
            raise real
        except BaseException as exc:
            reraise_as_exception(exc)


def test_reraise_bare_cancelled_error_propagates() -> None:
    """A bare asyncio.CancelledError (not in a group) is re-raised verbatim,
    NOT wrapped — cooperative cancellation (e.g. a ws disconnect cancelling
    the request task) must propagate as a signal instead of being reported
    as a "request failed" response. Mirrors the KeyboardInterrupt handling."""
    with pytest.raises(asyncio.CancelledError):
        try:
            raise asyncio.CancelledError()
        except BaseException as exc:
            reraise_as_exception(exc)


def test_reraise_cancellation_only_group_propagates() -> None:
    """A BaseExceptionGroup whose sub-exceptions are ALL CancelledError is
    cooperative cancellation rippling through anyio's task-group teardown,
    not an MCP error — propagate it verbatim instead of reporting failure."""
    group = BaseExceptionGroup("cancel", [asyncio.CancelledError(), asyncio.CancelledError()])
    with pytest.raises(BaseExceptionGroup):
        try:
            raise group
        except BaseException as exc:
            reraise_as_exception(exc)


def test_reraise_single_cancellation_in_group_unwrapped() -> None:
    """A cancellation-only group with a single sub re-raises that sub directly
    (consistent with the Exception-subgroup unwrap path)."""
    single = asyncio.CancelledError()
    group = BaseExceptionGroup("cancel", [single])
    with pytest.raises(asyncio.CancelledError):
        try:
            raise group
        except BaseException as exc:
            reraise_as_exception(exc)


def test_reraise_both_subgroups_folded_into_runtime_error() -> None:
    """When a group has BOTH an Exception subgroup and a BaseException
    subgroup, reraise folds them into one RuntimeError (the Exception
    subgroup alone can't be re-raised cleanly because the BaseException
    subgroup would still escape)."""
    real = ValueError("http 500")
    group = BaseExceptionGroup("mixed", [real, GeneratorExit()])
    caught: Exception | None = None
    try:
        try:
            raise group
        except BaseException as exc:
            reraise_as_exception(exc)
    except Exception as exc:
        caught = exc
    # A RuntimeError carrying both descriptions — NOT the bare ValueError,
    # because the GeneratorExit subgroup would otherwise escape.
    assert caught is not None
    assert isinstance(caught, RuntimeError)
    assert "http 500" in str(caught)
