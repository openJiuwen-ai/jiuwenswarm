# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract tests for ToolResultErrorDetector.

The detector's own docstring invites any module to import and reuse it, so
its behaviour is a shared contract rather than a private helper of
CircuitBreakerRail. Different consumers can want opposite things from it: a
circuit breaker counting consecutive failures of one tool must not fire on a
healthy result, while a rail that holds a task open until a required tool has
succeeded must not read a failure as success.

An unrecognised shape resolves to "no error", which is conservative for a
consumer that only wants to avoid false positives and dangerous for one that
must not miss a failure. That asymmetry is why every tool contract the
detector is expected to understand is pinned here rather than left to
whichever rail happens to exercise it.

Adding a tool that signals failure in a bare string means registering its
marker via ``ToolResultErrorDetector.register_plain_error_prefix`` from the
feature's own module -- not editing ``_PLAIN_ERROR_PREFIXES``. That tuple
stays generic; only ``[ERROR]:``, the project-wide tool-failure convention,
belongs in it.
"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
    ToolResultErrorDetector as Detector,
)


class _Result:
    """A tool result object exposing success/data/error attributes."""

    def __init__(self, success: bool, data=None, error=None) -> None:
        self.success = success
        self.data = data
        self.error = error


# ---------------------------------------------------------------------------
# Failures the detector must recognise
# ---------------------------------------------------------------------------

FAILURES = [
    pytest.param({"success": False, "error": "boom"}, id="dict-success-false"),
    pytest.param({"is_error": True}, id="dict-is-error"),
    pytest.param({"isError": "true"}, id="dict-isError-string"),
    pytest.param({"status": "failed"}, id="dict-status-failed"),
    pytest.param({"result_type": "error"}, id="dict-result-type"),
    pytest.param({"error": "something went wrong"}, id="dict-error-field"),
    pytest.param({"data": {"success": False}}, id="dict-nested-data"),
    # mcp_exec_command: a completed command that exited non-zero.
    pytest.param(
        json.dumps({"command": "git log", "exit_code": 128, "stdout": "",
                    "stderr": "fatal: Out of memory, realloc failed"}),
        id="json-exit-code",
    ),
    pytest.param({"returncode": 1}, id="dict-returncode"),
    # Repr of a result object, as it reaches the session history.
    pytest.param("success=False data={'content': 'x'} error='boom'", id="repr-false"),
    # mcp_exec_command: the paths that never produce an exit code.
    pytest.param("[ERROR]: command timed out after 300s.", id="shell-timeout"),
    pytest.param("[ERROR]: command failed to start: No such file", id="shell-spawn"),
    pytest.param("[ERROR]: command execution failed: boom", id="shell-exec"),
    pytest.param("[ERROR]: background command failed: nope", id="shell-background"),
    # The same convention from other tool modules.
    pytest.param("[ERROR]: query cannot be empty.", id="search-empty-query"),
    pytest.param("[ERROR]: failed to fetch webpage: timeout", id="web-fetch-failed"),
    pytest.param("[ERROR]: read_pdf failed: boom", id="pdf-failed"),
    pytest.param(_Result(False, error="boom"), id="object-success-false"),
]


@pytest.mark.parametrize("value", FAILURES)
def test_failures_are_recognised(value) -> None:
    assert Detector.has_error(value) is True


# ---------------------------------------------------------------------------
# Successes the detector must not misread
# ---------------------------------------------------------------------------

SUCCESSES = [
    pytest.param({"success": True, "data": {"content": "ok"}}, id="dict-success-true"),
    pytest.param({"status": "ok"}, id="dict-status-ok"),
    pytest.param({"error": None}, id="dict-error-none"),
    pytest.param({"error": "none"}, id="dict-error-literal-none"),
    pytest.param(
        json.dumps({"command": "git log", "exit_code": 0, "stdout": "d4c3e32a",
                    "stderr": ""}),
        id="json-exit-zero",
    ),
    pytest.param("success=True data={'content': 'ok'} error=None", id="repr-true"),
    # mcp_free_search on success: bare prose, no envelope, no marker.
    pytest.param(
        "Free search results (DuckDuckGo) for: circuit breaker\n"
        "1. Circuit breaker pattern\n   https://example.invalid/cb",
        id="search-plain-success",
    ),
    # wiki_query on success: the model's answer, returned bare. Nothing in
    # its shape separates it from arbitrary prose, which is exactly why a
    # plain-string convention has to be a marker no success can start with.
    pytest.param(
        "The retry budget is consumed per tool, not per session.",
        id="wiki-plain-success",
    ),
    pytest.param(_Result(True, data={"content": "ok"}), id="object-success-true"),
]


@pytest.mark.parametrize("value", SUCCESSES)
def test_successes_are_not_flagged(value) -> None:
    assert Detector.has_error(value) is False


# ---------------------------------------------------------------------------
# Shapes the detector cannot classify
# ---------------------------------------------------------------------------

UNCLASSIFIABLE = [
    pytest.param(None, id="none"),
    pytest.param("", id="empty-string"),
    pytest.param("plain prose with no contract", id="free-text"),
    pytest.param("{not json", id="broken-json"),
]


@pytest.mark.parametrize("value", UNCLASSIFIABLE)
def test_unclassifiable_shapes_claim_neither(value) -> None:
    """Neither an error nor an explicit success, and the consequences differ.

    A consumer that only wants to avoid false positives reads "not an error"
    and declines to count the call -- conservative, and it will not interrupt
    a run spuriously.

    A consumer that must not miss a failure reads the same value as
    satisfied, because its rule is `if not errored`. That is not an oversight
    to be tightened later: several tools return a bare string on success --
    `wiki_query` returns the model's answer, `mcp_free_search` returns a
    formatted result list -- so a gate that demanded explicit success would
    reject every one of them.

    The cost is real and worth stating plainly. A tool that signals failure
    in free text opens the gate for such a consumer. That is exactly the
    failure mode `_PLAIN_ERROR_PREFIXES` and `register_plain_error_prefix`
    exist to close: for any tool whose failure is a bare string, a registered
    marker is the only thing standing between a rejection and a task marked
    complete.
    """
    assert Detector.has_error(value) is False
    assert Detector.has_explicit_success(value) is False


# ---------------------------------------------------------------------------
# has_explicit_success is narrower than "not an error"
# ---------------------------------------------------------------------------


def test_explicit_success_requires_the_success_field() -> None:
    assert Detector.has_explicit_success({"success": True}) is True
    assert Detector.has_explicit_success({"status": "ok"}) is False
    assert Detector.has_explicit_success("Free search results for: anything") is False


def test_a_success_field_beats_a_nonzero_exit_code() -> None:
    """An explicit success wins over the exit-code heuristic."""
    assert Detector.has_error({"success": True, "exit_code": 1}) is False


def test_plain_error_prefix_is_matched_case_insensitively() -> None:
    assert Detector.has_error("[error]: command timed out after 300s.") is True


def test_a_prefix_must_start_the_string() -> None:
    """A mention inside prose is not a failure signal."""
    assert Detector.has_error(
        "The tool replied [ERROR]: earlier, but this run succeeded."
    ) is False


# ---------------------------------------------------------------------------
# A deliberate non-match
# ---------------------------------------------------------------------------

WIKI_FAILURES = [
    pytest.param("Error: Query cannot be empty.", id="wiki-error-colon"),
    pytest.param("Error querying wiki: connection refused", id="wiki-error-querying"),
    pytest.param("Wiki Query Error: boom", id="wiki-error-suffix"),
]


@pytest.mark.parametrize("value", WIKI_FAILURES)
def test_bare_error_prose_is_not_matched(value) -> None:
    """`wiki_tools` signals failure in prose, and that stays unrecognised.

    Three different shapes appear in one file, and the common part is the word
    "Error" in ordinary prose. Adding it as a prefix would flag any tool whose
    successful output begins with that word, so the detector deliberately does
    not match it.

    This is a decision, not an oversight. The fix belongs in the tools:
    converge those returns on the project's `[ERROR]:` convention, and they
    are covered by the existing entry with no change here. Teaching this
    detector every dialect is what produced the gap it was written to close.
    """
    assert Detector.has_error(value) is False


# ---------------------------------------------------------------------------
# register_plain_error_prefix: feature-owned markers, without editing the rail
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_registered_prefixes():
    """Isolate a test's registrations from every other test in the suite."""
    saved = set(Detector._extra_plain_error_prefixes)
    try:
        yield
    finally:
        Detector._extra_plain_error_prefixes.clear()
        Detector._extra_plain_error_prefixes.update(saved)


def test_registered_prefix_is_recognised(_clean_registered_prefixes) -> None:
    """A feature can teach the detector its own bare-string convention.

    A tool whose failure contract is a bare string of its own -- rather than
    the project-wide `[ERROR]:` -- registers that marker from its own module.
    Nothing feature-specific has to be added to the rail for it to be counted.
    """
    assert Detector.has_error("EXAMPLE_TOOL_ERROR: unknown snapshot id.") is False

    Detector.register_plain_error_prefix("EXAMPLE_TOOL_ERROR:")

    assert Detector.has_error(
        "EXAMPLE_TOOL_ERROR: unknown snapshot id. Correct the payload and "
        "call the tool again."
    ) is True


def test_registered_prefix_is_matched_case_insensitively(_clean_registered_prefixes) -> None:
    Detector.register_plain_error_prefix("my_tool_error:")
    assert Detector.has_error("My_Tool_Error: boom") is True


def test_registering_a_blank_prefix_is_a_no_op(_clean_registered_prefixes) -> None:
    before = set(Detector._extra_plain_error_prefixes)
    Detector.register_plain_error_prefix("   ")
    assert Detector._extra_plain_error_prefixes == before
