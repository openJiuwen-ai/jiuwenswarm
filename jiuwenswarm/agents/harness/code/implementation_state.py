# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure state tracking for completion of one Code-mode turn."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePath

from jiuwenswarm.agents.harness.code.execution_discipline import (
    looks_like_implementation_task,
)

DEFAULT_MAX_CONTINUATIONS = 3

_PYTHON_EXTENSIONS = frozenset({".py", ".pyi"})
_TYPESCRIPT_JAVASCRIPT_EXTENSIONS = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
)
_GO_EXTENSIONS = frozenset({".go"})
_NATIVE_RUST_EXTENSIONS = frozenset(
    {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".rs"}
)
_SOURCE_EXTENSIONS = (
    _PYTHON_EXTENSIONS
    | _TYPESCRIPT_JAVASCRIPT_EXTENSIONS
    | _GO_EXTENSIONS
    | _NATIVE_RUST_EXTENSIONS
)

_PYTHON_VERIFY = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:pytest\b|python(?:3(?:\.\d+)?)?\s+-m\s+"
    r"(?:pytest|unittest)\b|tox\b|nox\b)",
    re.IGNORECASE,
)
_TYPESCRIPT_JAVASCRIPT_VERIFY = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b|"
    r"(?:npx\s+)?(?:jest|vitest|mocha|ava)\b|deno\s+test\b)",
    re.IGNORECASE,
)
_GO_VERIFY = re.compile(r"(?:^|[;&|]\s*|\s)go\s+test\b", re.IGNORECASE)
_NATIVE_RUST_VERIFY = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:make(?:\s|$)|ninja(?:\s|$)|"
    r"(?:gcc|clang)(?:\s|$)|cargo\s+(?:check|build|test|clippy)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CompletionStatus:
    """Immutable completion decision for the current turn."""

    complete: bool
    reason: str
    state: str


class ImplementationStateMachine:
    """Track mutation and verification evidence for one implementation turn."""

    def __init__(self, max_continuations: int = DEFAULT_MAX_CONTINUATIONS) -> None:
        self.max_continuations = max(0, int(max_continuations))
        self.start_turn("")

    def start_turn(self, task_text: str) -> None:
        """Reset all evidence and begin a new turn."""

        self.task_text = str(task_text or "")
        self.is_implementation_task = looks_like_implementation_task(self.task_text)
        self.explore_count = 0
        self.mutation_revision = 0
        self.mutated_paths: list[str] = []
        self.mutated_kinds: list[str] = []
        self.python = False
        self.typescript_javascript = False
        self.go = False
        self.native_rust = False
        self.edited_existing_file = False
        self.created_source_files_may_be_unwired = False
        self.verification_attempted = False
        self.verification_succeeded = False
        self.verification_revision: int | None = None
        self.verification_command = ""
        self.continuation_count = 0
        self._real_mutation_count = 0
        self._shallow_mutation_count = 0
        self._created_source_paths: set[str] = set()
        self._verified_languages: set[str] = set()

    @property
    def typescript(self) -> bool:
        return self.typescript_javascript

    @property
    def javascript(self) -> bool:
        return self.typescript_javascript

    def record_explore(self) -> None:
        self.explore_count += 1

    def record_mutation(
        self,
        path: str,
        kind: str,
        shallow: bool = False,
        edited_existing: bool = False,
    ) -> None:
        """Record one applied mutation and invalidate older verification."""

        normalized_path = str(path or "")
        normalized_kind = str(kind or "").strip().lower() or "modify"
        self.mutation_revision += 1
        self.mutated_paths.append(normalized_path)
        self.mutated_kinds.append(normalized_kind)
        if shallow:
            self._shallow_mutation_count += 1
        else:
            self._real_mutation_count += 1
        self.edited_existing_file = self.edited_existing_file or bool(
            edited_existing or normalized_kind in {"edit", "modify", "delete"}
        )

        suffix = PurePath(normalized_path).suffix.lower()
        self.python = self.python or suffix in _PYTHON_EXTENSIONS
        self.typescript_javascript = (
            self.typescript_javascript or suffix in _TYPESCRIPT_JAVASCRIPT_EXTENSIONS
        )
        self.go = self.go or suffix in _GO_EXTENSIONS
        self.native_rust = self.native_rust or suffix in _NATIVE_RUST_EXTENSIONS
        if (
            normalized_kind in {"create", "add", "write"}
            and suffix in _SOURCE_EXTENSIONS
        ):
            self._created_source_paths.add(normalized_path)
        self._refresh_unwired_flag()

    def record_verification(self, command: str, result_success: bool) -> None:
        """Record a verification attempt against the current mutation revision."""

        self.verification_attempted = True
        self.verification_succeeded = bool(result_success)
        self.verification_revision = self.mutation_revision
        self.verification_command = str(command or "")
        self._verified_languages = self._languages_verified_by(
            self.verification_command
        )

    def record_verification_evidence(
        self,
        languages: Iterable[str],
        result_success: bool,
        command: str = "run_tests",
    ) -> None:
        """Record structured verification evidence (e.g. from ``run_tests``).

        Unlike :meth:`record_verification`, the verified languages are given
        explicitly instead of being inferred from a shell command string, so a
        structured test run counts for exactly the languages whose tests passed.
        Staleness and completion rules are the same as for shell verification.
        """

        self.verification_attempted = True
        self.verification_succeeded = bool(result_success)
        self.verification_revision = self.mutation_revision
        self.verification_command = str(command or "run_tests")
        known = {"python", "typescript/javascript", "go", "native/rust"}
        self._verified_languages = (
            {str(lang) for lang in languages if str(lang) in known}
            if result_success
            else set()
        )

    def completion_status(self) -> CompletionStatus:
        """Return the current hard completion decision."""

        if not self.is_implementation_task:
            return CompletionStatus(True, "not an implementation task", "bypassed")
        if self.mutation_revision == 0:
            return CompletionStatus(False, "no mutation recorded", "awaiting_mutation")
        if self._real_mutation_count == 0:
            return CompletionStatus(
                False, "only shallow mutations were recorded", "shallow_mutation"
            )
        if self.created_source_files_may_be_unwired:
            return CompletionStatus(
                False,
                "created source files may not be wired into the existing code",
                "unwired_source",
            )
        if not self.verification_attempted:
            return CompletionStatus(
                False, "verification has not been attempted", "awaiting_verification"
            )
        if self.verification_revision != self.mutation_revision:
            return CompletionStatus(
                False,
                "verification is stale after a later mutation",
                "stale_verification",
            )
        if not self.verification_succeeded:
            return CompletionStatus(
                False, "latest verification failed", "verification_failed"
            )

        missing = self._required_languages() - self._verified_languages
        if missing:
            language = sorted(missing)[0]
            return CompletionStatus(
                False,
                f"successful {language} test verification is required",
                "wrong_verification",
            )
        return CompletionStatus(True, "implementation verified", "complete")

    def record_continuation(self) -> int:
        """Consume one continuation and return the new count."""

        self.continuation_count += 1
        return self.continuation_count

    def can_continue(self) -> bool:
        return self.continuation_count < self.max_continuations

    def has_continuations_remaining(self) -> bool:
        return self.can_continue()

    def remaining_continuations(self) -> int:
        return max(0, self.max_continuations - self.continuation_count)

    def continuation_exhausted(self) -> bool:
        return not self.can_continue()

    def _required_languages(self) -> set[str]:
        required: set[str] = set()
        if self.python:
            required.add("python")
        if self.typescript_javascript:
            required.add("typescript/javascript")
        if self.go:
            required.add("go")
        if self.native_rust:
            required.add("native/rust")
        return required

    @staticmethod
    def _languages_verified_by(command: str) -> set[str]:
        verified: set[str] = set()
        if _PYTHON_VERIFY.search(command):
            verified.add("python")
        if _TYPESCRIPT_JAVASCRIPT_VERIFY.search(command):
            verified.add("typescript/javascript")
        if _GO_VERIFY.search(command):
            verified.add("go")
        if _NATIVE_RUST_VERIFY.search(command):
            verified.add("native/rust")
        return verified

    def _refresh_unwired_flag(self) -> None:
        pure_create = bool(self._created_source_paths) and not self.edited_existing_file
        create_intent = bool(
            re.search(
                r"\b(?:create|implement|build)\s+(?:a\s+)?(?:new\s+)?"
                r"(?:module|package|project|application|file)\b|"
                r"\bfrom scratch\b|(?:创建新|新建|从零)",
                self.task_text,
                re.IGNORECASE,
            )
        )
        self.created_source_files_may_be_unwired = bool(
            self._created_source_paths
            and not self.edited_existing_file
            and not (pure_create and create_intent)
        )


__all__ = [
    "CompletionStatus",
    "DEFAULT_MAX_CONTINUATIONS",
    "ImplementationStateMachine",
]
