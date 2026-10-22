# coding: utf-8
"""Convert anyio/BaseExceptionGroup escapes from the MCP SDK into exceptions
that ordinary ``except Exception`` blocks can catch.

Background: when a remote streamable-http MCP server returns an HTTP error
(403/500/...), the mcp SDK's ``streamablehttp_client`` tears down its anyio
task group, and the teardown raises a ``BaseExceptionGroup`` whose sub-exceptions
include ``GeneratorExit`` (a ``BaseException`` but NOT an ``Exception``). A
``BaseExceptionGroup`` is only an ``Exception`` subclass when *every* sub-
exception is — so a group containing ``GeneratorExit`` is NOT caught by
``except Exception``. The group then escapes every ``except Exception`` on the
way up (the handler, the dispatch top-level), the frontend never receives a
response frame, and the UI hangs forever.

This module provides :func:`reraise_as_exception`: call it inside a bare
``except BaseException`` block; it either re-raises the meaningful ``Exception``
part (so the real error message — e.g. an ``httpx.HTTPStatusError`` — reaches
the frontend) or wraps the uncatchable ``BaseException`` part in a
``RuntimeError`` that ordinary ``except Exception`` can catch.
``KeyboardInterrupt`` and bare ``asyncio.CancelledError`` are re-raised verbatim
so Ctrl+C and cooperative cancellation (e.g. ws disconnect cancelling the
request task) still propagate instead of being reported as a "request failed"
response.
"""
from __future__ import annotations

__all__ = ["reraise_as_exception"]


def _describe(exc: BaseException) -> str:
    """A short, lossy string for the wrapped RuntimeError — enough to debug
    which BaseException escaped without leaking the full group structure."""
    if isinstance(exc, BaseExceptionGroup):
        # Show each sub-exception's type + message so the real error
        # (e.g. "ValueError: http 500") is preserved alongside the
        # uncatchable ones (e.g. "GeneratorExit").
        parts = ", ".join(f"{type(sub).__name__}: {sub}" for sub in exc.exceptions)
        return f"BaseExceptionGroup({parts})"
    return f"{type(exc).__name__}: {exc}"


def _is_cancellation_only(group: "BaseExceptionGroup") -> bool:
    """True if every sub-exception in ``group`` is an asyncio.CancelledError.

    Such a group represents cooperative cancellation (e.g. a ws disconnect
    cancelling the request task rippling through anyio's task-group teardown),
    not an MCP error — it must propagate as cancellation, not be reported as
    a request failure.
    """
    import asyncio

    return all(isinstance(sub, asyncio.CancelledError) for sub in group.exceptions)


def reraise_as_exception(exc: BaseException) -> None:
    """Re-raise ``exc`` in a form catchable by ``except Exception``.

    Call this from a bare ``except BaseException`` block at the boundary where
    an MCP SDK exception group escapes. Semantics:

    * ``KeyboardInterrupt`` and bare ``asyncio.CancelledError`` — re-raised
      verbatim. Ctrl+C and cooperative cancellation (ws disconnect) must
      propagate as signals, not be reported as a "request failed" response.
    * ``BaseExceptionGroup`` — split via ``split(Exception)``:
        - Only an ``Exception`` subgroup → re-raise it (the real error message
          propagates; the caller's ``except Exception`` catches it).
        - Only a ``BaseException`` subgroup that is cancellation-only →
          re-raise it verbatim (cooperative cancellation).
        - Only a ``BaseException`` subgroup (GeneratorExit/...) →
          wrap it in a ``RuntimeError`` so it becomes catchable.
        - Both → fold into a ``RuntimeError`` carrying both descriptions.
    * Plain ``Exception`` — re-raised unchanged.
    * Other ``BaseException`` (bare ``GeneratorExit``) → wrapped in a
      ``RuntimeError``.

    Never returns normally; always raises.
    """
    import asyncio

    if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
        raise exc

    if isinstance(exc, BaseExceptionGroup):
        exc_subgroup, base_subgroup = exc.split(Exception)
        if exc_subgroup is not None and base_subgroup is None:
            # Clean Exception subgroup — the real error (e.g. HTTPStatusError).
            # Unwrap the group: if it's a single sub-exception, raise that
            # directly so str() gives the specific message rather than
            # "x (1 sub-exception)".
            subs = exc_subgroup.exceptions
            if len(subs) == 1:
                raise subs[0]
            raise exc_subgroup
        if exc_subgroup is None and base_subgroup is not None:
            # Only BaseException sub-exceptions — would escape except Exception.
            # If the group is purely cooperative cancellation, propagate it.
            if _is_cancellation_only(base_subgroup):
                subs = base_subgroup.exceptions
                if len(subs) == 1:
                    raise subs[0]
                raise base_subgroup
            raise RuntimeError(_describe(base_subgroup))
        # Both subgroups present — fold into one RuntimeError carrying both
        # descriptions. _describe expands each group's sub-exceptions so the
        # real error message (e.g. "ValueError: http 500") is preserved.
        raise RuntimeError(
            f"{_describe(exc_subgroup)}; plus uncatchable: {_describe(base_subgroup)}"
        )

    if isinstance(exc, Exception):
        raise exc

    # Bare BaseException (GeneratorExit, ...): wrap it. CancelledError was
    # already handled above.
    raise RuntimeError(_describe(exc))
