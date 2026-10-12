# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Focused tests for the per-turn implementation completion state."""

import pytest

from jiuwenswarm.agents.harness.code.implementation_state import (
    ImplementationStateMachine,
)


@pytest.mark.parametrize(
    ("path", "good_command", "bad_command"),
    [
        ("feature.py", "python -m pytest tests/test_feature.py", "ruff check ."),
        ("feature.ts", "npm test -- feature", "npm run build"),
        ("feature.go", "go test ./...", "go build ./..."),
        ("feature.rs", "cargo check", "rustfmt --check feature.rs"),
    ],
)
def test_language_requires_successful_language_verification(
    path, good_command, bad_command
):
    state = ImplementationStateMachine()
    state.start_turn("implement the feature")
    state.record_mutation(path, "modify", edited_existing=True)

    state.record_verification(bad_command, True)
    assert state.completion_status().state == "wrong_verification"

    state.record_verification(good_command, True)
    assert state.completion_status().complete is True


def test_verification_becomes_stale_after_another_mutation():
    state = ImplementationStateMachine()
    state.start_turn("fix the parser")
    state.record_mutation("parser.py", "modify", edited_existing=True)
    state.record_verification("pytest tests/test_parser.py", True)
    assert state.completion_status().complete is True

    state.record_mutation("parser.py", "modify", edited_existing=True)

    status = state.completion_status()
    assert status.complete is False
    assert status.state == "stale_verification"


def test_non_implementation_task_bypasses_completion_requirements():
    state = ImplementationStateMachine()
    state.start_turn("explain how the parser works")

    status = state.completion_status()

    assert status.complete is True
    assert status.state == "bypassed"


def test_new_module_task_is_not_blocked_as_unwired():
    state = ImplementationStateMachine()
    state.start_turn("create a new Python module from scratch")
    state.record_mutation("new_module.py", "create")
    state.record_verification("pytest tests/test_new_module.py", True)

    assert state.created_source_files_may_be_unwired is False
    assert state.completion_status().complete is True


def test_created_helper_requires_existing_entrypoint_edit():
    state = ImplementationStateMachine()
    state.start_turn("fix the parser by adding validation")
    state.record_mutation("validation.py", "create")
    state.record_verification("pytest tests/test_parser.py", True)

    status = state.completion_status()
    assert status.complete is False
    assert status.state == "unwired_source"

    state.record_mutation("parser.py", "modify", edited_existing=True)
    state.record_verification("pytest tests/test_parser.py", True)
    assert state.completion_status().complete is True


def test_continuation_bookkeeping_is_bounded():
    state = ImplementationStateMachine(max_continuations=2)
    state.start_turn("implement a feature")

    assert state.remaining_continuations() == 2
    state.record_continuation()
    state.record_continuation()

    assert state.continuation_exhausted() is True
    assert state.remaining_continuations() == 0
