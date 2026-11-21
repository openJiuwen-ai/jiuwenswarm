# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pin the ``JiuwenError`` stringification, pickling and code semantics.

``JiuwenError.__str__`` renders message-only when the default ``ERROR`` status
was attached (so existing ``str(e)`` call sites keep their historical output),
and switches to ``BaseError``'s ``"[{code}] {message}"`` rendering once a
specific ``StatusCode`` is attached. This pins that boundary, plus the fact
that ``BaseError`` always exposes both ``status`` (the enum) and ``code`` (the
integer) — the two attributes ``record_boundary_exception`` and call sites
reach for interchangeably.

Also covers two review-flagged bugs, both rooted in message-first errors
(``TeamError("boom")``, ``A2XError("boom")``) never attaching a ``StatusCode``:

1. ``args``/``to_dict()["message"]`` rendered the generic ``StatusCode.ERROR``
   template ("error") while ``str(e)``/``to_dict()["raw_message"]`` showed the
   real text - three views of the same error disagreeing with each other.
2. ``JiuwenStoreError``/``JiuwenConfigError``'s ``recoverable``/``fatal`` flags
   were the exact opposite of openjiuwen SDK's ``StoreError``/
   ``ConfigurationError``, so a boundary handler trusting those flags would
   reach the opposite retry/abort conclusion depending on which side raised.
"""

from __future__ import annotations

import pickle

from openjiuwen.core.common.exception.codes import StatusCode

from jiuwenswarm.common.errors import (
    JiuwenConfigError,
    JiuwenError,
    JiuwenStoreError,
    JiuwenToolError,
)


class _TeamCreateError(JiuwenError):
    pass


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


def test_pickle_round_trip_preserves_status_and_message() -> None:
    """Pickling relies on ``_reconstruct`` passing ``status`` explicitly.

    ``BaseError.__reduce__`` rebuilds instances via a positional call that
    would otherwise land in ``JiuwenError.__init__``'s message-first ``message``
    parameter instead of ``status``. Pin the round trip so a future change to
    either side can't silently reintroduce that mismatch.
    """
    status = StatusCode.MODEL_CALL_FAILED
    err = JiuwenToolError(status, msg="boom", details={"x": 1})
    restored = pickle.loads(pickle.dumps(err))
    assert type(restored) is JiuwenToolError
    assert restored.status is status
    assert restored.message == "boom"
    assert restored.details == {"x": 1}
    assert str(restored) == str(err)


def test_message_first_args_and_to_dict_agree_with_str() -> None:
    err = JiuwenError("boom")
    assert str(err) == "boom"
    assert err.args == ("boom",)
    data = err.to_dict()
    assert data["message"] == "boom"
    assert data["raw_message"] == "boom"


def test_status_first_still_uses_the_status_template() -> None:
    """Explicit StatusCode calls are untouched: template stays code-driven."""
    status = StatusCode.EXPRESSION_SYNTAX_ERROR  # no {placeholders} to format
    err = JiuwenError("boom", status=status)
    assert str(err) == f"[{status.code}] boom"
    assert err.args == (status.errmsg,)


def test_to_dict_exposes_a_stable_per_class_identifier() -> None:
    assert _TeamCreateError("boom").to_dict()["error_type"] == "_TeamCreateError"


def test_pickling_preserves_message_first_semantics() -> None:
    err = pickle.loads(pickle.dumps(JiuwenError("boom")))
    assert str(err) == "boom"
    assert err.args == ("boom",)


def test_pickling_preserves_status_first_semantics() -> None:
    status = StatusCode.EXPRESSION_SYNTAX_ERROR
    err = pickle.loads(pickle.dumps(JiuwenError("boom", status=status)))
    assert str(err) == f"[{status.code}] boom"


def test_store_error_recoverable_matches_openjiuwen_store_error() -> None:
    assert JiuwenStoreError.recoverable is True
    assert JiuwenStoreError.fatal is False


def test_config_error_fatal_matches_openjiuwen_configuration_error() -> None:
    assert JiuwenConfigError.recoverable is False
    assert JiuwenConfigError.fatal is True
