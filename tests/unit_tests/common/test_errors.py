# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pin the ``JiuwenError`` stringification and ``status``/``code`` contract.

``JiuwenError.__str__`` renders message-only when the default ``ERROR`` status
was attached (so existing ``str(e)`` call sites keep their historical output),
and switches to ``BaseError``'s ``"[{code}] {message}"`` rendering once a
specific ``StatusCode`` is attached. This pins that boundary, plus the fact
that ``BaseError`` always exposes both ``status`` (the enum) and ``code`` (the
integer) — the two attributes ``record_boundary_exception`` and call sites
reach for interchangeably.
"""

from __future__ import annotations

from openjiuwen.core.common.exception.codes import StatusCode

from jiuwenswarm.common.errors import JiuwenError


def test_str_is_message_only_for_default_error_status() -> None:
    """No specific status attached -> ``str(e)`` is just the message."""
    err = JiuwenError("boom")
    assert str(err) == "boom"


def test_str_prepends_code_for_specific_status() -> None:
    """A specific status switches to ``[code] message`` rendering."""
    status = StatusCode.MODEL_CALL_FAILED
    err = JiuwenError("boom", status=status)
    assert str(err) == f"[{status.code}] boom"


def test_base_error_style_positional_status_renders_with_code() -> None:
    """``JiuwenError(StatusCode, msg=...)`` is accepted and keeps the code prefix."""
    status = StatusCode.MODEL_CALL_FAILED
    err = JiuwenError(status, msg="boom")
    assert str(err) == f"[{status.code}] boom"


def test_status_and_code_are_both_always_exposed() -> None:
    """``status`` (enum) and ``code`` (int) are both set and consistent.

    Guards the ``__str__`` reliance on ``self.status`` against regressions: the
    attribute is assigned unconditionally by ``BaseError.__init__``.
    """
    err = JiuwenError("boom")
    assert err.status is StatusCode.ERROR
    assert err.code == StatusCode.ERROR.code
    assert err.code == err.status.code


def test_str_never_raises_for_empty_message() -> None:
    """``JiuwenError()`` stringifies to the ERROR template, not an exception."""
    err = JiuwenError()
    assert str(err) == StatusCode.ERROR.errmsg
