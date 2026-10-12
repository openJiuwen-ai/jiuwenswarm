# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The model-facing ``run_tests`` tool.

Built by ``JiuwenSwarmCodeAdapter._build_run_tests_tool`` (and the swarm
``swarm.run_tests`` element) when ``run_tests`` is listed in
``modes.code.tools``. One call:

1. discovers the workspace's frameworks (:mod:`.discovery`, cached);
2. validates every ``tests`` / ``files`` entry (inside the workspace root after
   resolving symlinks, never ``-``-prefixed) and every ``extra_args`` entry
   (per-runner allow-list, :mod:`.argpolicy`), then picks targets — explicit ``tests`` ids, else tests
   mapped from ``files`` (:mod:`.mapping`); a language whose changed files map
   to nothing runs its whole suite only when ``fallback_whole_suite`` is on,
   otherwise it reports ``no_tests`` and asks for explicit ``tests``;
3. runs each framework (:mod:`.runner`) and returns a capped summary whose
   first line (``run_tests: outcome=... passed_languages=...``) plus the
   structured ``data`` fields are the verification evidence
   ``ImplementCompletenessRail`` records.

The test process runs on the host with the project's own interpreter and
code (``conftest.py``, ``package.json`` scripts, build scripts). When the Code
session runs in a sandbox (``OperationMode.SANDBOX``), the tool refuses to run
rather than bypass the sandbox; the builders do not register it at all then.

Test failures are a successful *tool* call (``success=True``,
``outcome=failed``) so the model sees the summary rather than an error; only
"nothing to run" and bad arguments return ``success=False``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from openjiuwen.core.common.logging import tool_logger as logger
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolExposure
from openjiuwen.core.foundation.tool.schema import ToolOutput

from jiuwenswarm.agents.harness.code.verification.argpolicy import (
    allowed_options,
    check_extra_args,
    npm_script_runner,
)
from jiuwenswarm.agents.harness.code.verification.discovery import (
    SCAN_MAX_DEPTH,
    Discovery,
    discover_tests,
    language_of,
)
from jiuwenswarm.agents.harness.code.verification.mapping import map_files_to_tests
from jiuwenswarm.agents.harness.code.verification.runner import (
    DEFAULT_MAX_SUMMARY_CHARS,
    DEFAULT_SPILL_MAX_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    RunResult,
    default_spill_root,
    overall_outcome,
    render_summary,
    run_framework,
)

TOOL_NAME = "run_tests"

_DESCRIPTION_EN = (
    "Run the project's tests and get a compact result: pass/fail counts, failing test ids "
    "with the assertion and traceback tail, and a path to the full output. Pass `files` "
    "(the source files you changed) to run the tests that cover them, or `tests` (test "
    "files or ids such as tests/test_x.py::test_y) to run exactly those. Paths must be "
    "inside the project. Detects pytest/unittest, jest/vitest/npm test, go test and "
    "cargo test. Prefer this over running test commands through bash: its result counts "
    "as verification for the completion gate."
)
_DESCRIPTION_CN = (
    "运行项目测试并返回精简结果：通过/失败数量、失败用例 id 及断言与回溯尾部、完整输出文件路径。"
    "传 `files`（你修改过的源文件）运行覆盖它们的测试，或传 `tests`（测试文件或 id，如 "
    "tests/test_x.py::test_y）精确运行。路径必须位于项目内。支持 pytest/unittest、"
    "jest/vitest/npm test、go test、cargo test。验证时优先使用本工具而不是 bash：其结果会被完成门控记为验证证据。"
)


def run_tests_input_params() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Changed source files (relative to the project root) to find tests for.",
            },
            "tests": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Explicit test files or ids to run instead of mapping.",
            },
            "extra_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Extra runner options from a fixed allow-list (filters and output detail), "
                    "e.g. [\"-k\", \"name\"] or [\"-x\"] for pytest, [\"-t\", \"name\"] for jest, "
                    "[\"-run\", \"TestX\"] for go. Test paths go in `tests`, not here."
                ),
            },
            "timeout": {"type": "integer", "description": "Seconds before the run is killed."},
        },
        "required": [],
    }


#: Seconds added to the configured timeout for the outer agent-core deadline, so
#: the tool's own budget (and its structured "timeout" result) always fires first.
OUTER_TIMEOUT_MARGIN_SECONDS = 60.0


def build_run_tests_card(
    *,
    agent_id: str | None = None,
    language: str = "en",
    timeout: float | None = None,
) -> ToolCard:
    """Card with DIRECT exposure (agent-core defers undeclared host tools).

    agent-core wraps every tool call in ``DEFAULT_TOOL_CALL_TIMEOUT`` (300 s)
    unless the card declares ``properties["resilience"]["timeout_s"]``. run_tests
    enforces its own overall budget (``timeout``), so the card declares that
    budget plus a margin; otherwise a 600 s test run is killed at 300 s with a
    generic error instead of the structured ``timeout`` outcome.
    """

    budget = _as_float(timeout, DEFAULT_TIMEOUT_SECONDS, low=5, high=7200)
    return ToolCard(
        id=f"{TOOL_NAME}_{agent_id}" if agent_id else TOOL_NAME,
        name=TOOL_NAME,
        description=_DESCRIPTION_CN if language == "cn" else _DESCRIPTION_EN,
        input_params=run_tests_input_params(),
        exposure=ToolExposure.DIRECT,
        properties={"resilience": {"timeout_s": budget + OUTER_TIMEOUT_MARGIN_SECONDS}},
    )


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _as_float(value: Any, default: float, *, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


def contain_entry(root: Path, entry: str) -> tuple[str | None, str]:
    """Normalise one ``tests`` / ``files`` entry to a root-relative form.

    Returns ``(normalised, "")`` or ``(None, reason)``. The path part (before an
    optional ``::node::id`` suffix) is resolved against ``root`` with symlinks
    followed and must stay inside it; entries starting with ``-`` are refused so
    no target can be read as a runner option. Go package patterns such as
    ``./...`` / ``./pkg/...`` are kept in ``./`` form.
    """

    text = str(entry or "").strip()
    if not text:
        return None, "empty entry"
    if text.startswith("-"):
        return None, f"{text!r} looks like an option; pass runner options in extra_args"
    path_part, sep, node = text.partition("::")
    go_dots = path_part.endswith("/...") or path_part == "./..."
    base_part = path_part.removesuffix("/...")
    if path_part == "./...":
        base_part = "."
    candidate = Path(base_part).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        rel = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, f"{text!r} is outside the project root"
    rel_text = rel.as_posix()
    if go_dots:
        rel_text = "./..." if rel_text in ("", ".") else f"./{rel_text}/..."
    elif path_part.startswith("./") and not rel_text.startswith("."):
        rel_text = "./" if rel_text in ("", ".") else f"./{rel_text}"
    return rel_text + (sep + node if sep else ""), ""


def plan_runs(
    discovery: Discovery,
    root: Path,
    files: list[str],
    tests: list[str],
    *,
    fallback_whole_suite: bool = False,
) -> tuple[list[tuple[Any, list[str]]], list[str], list[RunResult]]:
    """Return ``([(framework, targets)], notes, skipped)``.

    An empty target list means "the whole suite" and is only produced when the
    model asked for no targets at all or ``fallback_whole_suite`` allows it.
    ``skipped`` holds synthetic ``no_tests`` results for languages whose
    changed files mapped to no tests while the fallback is off.
    """

    notes: list[str] = []
    plan: list[tuple[Any, list[str]]] = []
    skipped: list[RunResult] = []
    if tests:
        grouped: dict[str, list[str]] = {}
        for test in tests:
            lang = language_of(test.split("::", 1)[0]) or (discovery.languages[0] if discovery.languages else None)
            if lang is not None:
                grouped.setdefault(lang, []).append(test)
        for lang, targets in grouped.items():
            fw = discovery.for_language(lang)
            if fw is None:
                notes.append(f"no {lang} test framework detected; skipped {', '.join(targets)}")
                continue
            plan.append((fw, targets))
        return plan, notes, skipped
    if files:
        mapped = map_files_to_tests(root, files)
        changed_langs = []
        for f in files:
            lang = language_of(f)
            if lang and lang not in changed_langs:
                changed_langs.append(lang)
        for lang in changed_langs:
            fw = discovery.for_language(lang)
            if fw is None:
                notes.append(f"changed {lang} files but no {lang} test framework was detected")
                continue
            targets = mapped.get(lang, [])
            if not targets and lang != "native/rust":
                if not fallback_whole_suite:
                    skipped.append(
                        RunResult(
                            fw.name, lang, [], "no_tests", None, 0.0,
                            note=(
                                f"no {lang} tests mapped from the changed files; pass `tests` "
                                "explicitly (test files or ids) to run the ones that cover them"
                            ),
                        )
                    )
                    continue
                notes.append(f"no focused {lang} tests mapped from the changed files; running the whole {fw.name} suite")
            plan.append((fw, targets))
        if plan or skipped or changed_langs:
            return plan, notes, skipped
        if not fallback_whole_suite:
            notes.append("no source files among `files`; pass `tests` explicitly to run specific tests")
            return plan, notes, skipped
        notes.append("no source files among `files`; running every detected suite")
    for fw in discovery.frameworks:
        plan.append((fw, []))
    return plan, notes, skipped


def _language_outcomes(results: list[RunResult]) -> dict[str, str]:
    order = ("timeout", "failed", "error", "no_tests", "passed")
    merged: dict[str, str] = {}
    for r in results:
        previous = merged.get(r.language)
        if previous is None or order.index(r.outcome) < order.index(previous):
            merged[r.language] = r.outcome
    return merged


class RunTestsTool(Tool):
    """Model-facing ``run_tests`` (see module docstring)."""

    def __init__(
        self,
        root_resolver: Callable[[], str] | None = None,
        *,
        agent_id: str | None = None,
        language: str = "en",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
        spill: bool = True,
        spill_dir: str | None = None,
        spill_max_bytes: int = DEFAULT_SPILL_MAX_BYTES,
        python: str | None = None,
        fallback_whole_suite: bool = False,
        inherit_runner_env: bool = False,
        sandboxed: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(build_run_tests_card(agent_id=agent_id, language=language, timeout=timeout))
        self._python = str(python).strip() if python else None
        self._fallback_whole_suite = bool(fallback_whole_suite)
        self._inherit_runner_env = bool(inherit_runner_env)
        self._sandboxed = sandboxed
        self._root_resolver = root_resolver
        self._timeout = _as_float(timeout, DEFAULT_TIMEOUT_SECONDS, low=5, high=7200)
        self._max_summary_chars = int(_as_float(max_summary_chars, DEFAULT_MAX_SUMMARY_CHARS, low=500, high=50_000))
        self._spill = bool(spill)
        self._spill_dir = spill_dir
        self._spill_max_bytes = int(_as_float(spill_max_bytes, DEFAULT_SPILL_MAX_BYTES, low=0, high=10 * 1024**3))

    def _root(self) -> Path:
        raw = self._root_resolver() if self._root_resolver else None
        if not raw:
            from openjiuwen.core.sys_operation.cwd import get_cwd

            raw = get_cwd()
        return Path(raw).expanduser().resolve()

    def _is_sandboxed(self) -> bool:
        if self._sandboxed is None:
            return False
        try:
            return bool(self._sandboxed())
        except Exception:  # noqa: BLE001 - unknown state: refuse rather than run on the host
            return True

    def _spill_path(self) -> Path | None:
        if not self._spill:
            return None
        return Path(self._spill_dir).expanduser() if self._spill_dir else default_spill_root()

    async def invoke(self, inputs: dict[str, Any], **kwargs: Any) -> ToolOutput:
        inputs = inputs if isinstance(inputs, dict) else {}
        if self._is_sandboxed():
            return ToolOutput(
                success=False,
                error=(
                    "run_tests: unavailable while the session runs in a sandbox (it would run the "
                    "project's code on the host); run the test command with bash inside the sandbox"
                ),
            )
        try:
            root = self._root()
        except Exception as exc:  # noqa: BLE001 - resolver misconfiguration must not kill the turn
            return ToolOutput(success=False, error=f"run_tests: cannot resolve project root: {exc}")
        if not root.is_dir():
            return ToolOutput(success=False, error=f"run_tests: project root {root} does not exist")
        rejected: list[str] = []
        files: list[str] = []
        for entry in _str_list(inputs.get("files")):
            normalised, reason = contain_entry(root, entry)
            if normalised is None:
                rejected.append(reason)
            else:
                files.append(normalised)
        tests: list[str] = []
        for entry in _str_list(inputs.get("tests")):
            normalised, reason = contain_entry(root, entry)
            if normalised is None:
                rejected.append(reason)
            else:
                tests.append(normalised)
        if rejected:
            return ToolOutput(success=False, error="run_tests: rejected " + "; ".join(rejected))
        extra = _str_list(inputs.get("extra_args"))
        timeout = _as_float(inputs.get("timeout"), self._timeout, low=5, high=max(self._timeout, 5))
        discovery = discover_tests(root)
        if not discovery.frameworks:
            return ToolOutput(
                success=False,
                error=(
                    f"run_tests: no test framework detected in {root}. Checked: pytest configuration "
                    "(pytest.ini, [tool.pytest] in pyproject.toml, tox.ini, setup.cfg, conftest.py); "
                    f"Python test files named test_*.py or *_test.py up to {SCAN_MAX_DEPTH} directories "
                    "deep (virtual environments, node_modules, build output and hidden directories "
                    "are skipped); a Python project file for unittest (pyproject.toml, setup.py, "
                    "setup.cfg, tox.ini); package.json test dependencies or a test script; go.mod; "
                    "Cargo.toml. Run the project's test command with bash instead."
                ),
            )
        plan, notes, skipped = plan_runs(
            discovery, root, files, tests, fallback_whole_suite=self._fallback_whole_suite
        )
        if extra:
            for fw, _targets in plan:
                runner = fw.name
                if runner == "npm":
                    runner = npm_script_runner(root, fw.npm_script) or ""
                    if not runner:
                        return ToolOutput(
                            success=False,
                            error=(
                                f"run_tests: extra_args are not supported for the npm script "
                                f"{fw.npm_script or 'test'!r} (it does not run jest or vitest directly); "
                                "call run_tests without extra_args"
                            ),
                        )
                reason = check_extra_args(runner, extra)
                if reason is not None:
                    return ToolOutput(
                        success=False,
                        error=(
                            f"run_tests: {reason}. Allowed {runner} options: "
                            + " ".join(allowed_options(runner))
                        ),
                    )
        if not plan and not skipped:
            return ToolOutput(success=False, error="run_tests: nothing to run. " + " ".join(notes))
        spill_dir = self._spill_path()
        results: list[RunResult] = list(skipped)
        # ``timeout`` is the budget for the whole call, shared by the frameworks
        # run one after another; the card's outer deadline assumes this.
        deadline = time.monotonic() + timeout
        for fw, targets in plan:
            remaining = deadline - time.monotonic()
            if remaining < 5:
                results.append(
                    RunResult(fw.name, fw.language, [], "timeout", None, 0.0,
                              targets=list(targets),
                              note="not started: the run_tests time budget was used up")
                )
                continue
            try:
                results.append(
                    await run_framework(
                        fw, root, targets, extra, timeout=remaining,
                        spill_dir=spill_dir, spill_max_bytes=self._spill_max_bytes,
                        python=self._python,
                        inherit_runner_env=self._inherit_runner_env,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - never fail the turn on a runner hiccup
                logger.warning("[run_tests] %s failed to run: %s", fw.name, exc)
                results.append(RunResult(fw.name, fw.language, [], "error", None, 0.0, note=str(exc)))
        summary = render_summary(results, max_chars=self._max_summary_chars, notes=notes)
        passed_languages = sorted({r.language for r in results if r.passed})
        data = {
            "content": summary,
            "outcome": overall_outcome(results),
            "passed_languages": passed_languages,
            "languages_run": sorted({r.language for r in results}),
            # Per-language outcome for the gate: one framework reporting
            # ``no_tests`` must not hide another language's real pass.
            "language_outcomes": _language_outcomes(results),
            "results": [
                {
                    "framework": r.framework,
                    "language": r.language,
                    "outcome": r.outcome,
                    "exit_code": r.exit_code,
                    "counts": r.counts,
                    "failing": [f.test_id for f in r.failures],
                    "targets": r.targets,
                    "spill_path": r.spill_path,
                    "command": " ".join(r.argv),
                }
                for r in results
            ],
        }
        return ToolOutput(success=True, data=data)

    async def stream(self, inputs: dict[str, Any], **kwargs: Any) -> AsyncIterator[Any]:
        """A test run has one result; ``invoke`` is the API."""
        yield await self.invoke(inputs, **kwargs)


def sys_operation_is_sandboxed(sys_operation: Any) -> bool:
    """True when ``sys_operation`` (or its card) runs in ``OperationMode.SANDBOX``."""

    for holder in (sys_operation, getattr(sys_operation, "card", None)):
        mode = getattr(holder, "mode", None)
        if mode is not None:
            return str(getattr(mode, "value", mode)).lower() == "sandbox"
    return False


__all__ = [
    "TOOL_NAME",
    "RunTestsTool",
    "build_run_tests_card",
    "contain_entry",
    "plan_runs",
    "run_tests_input_params",
    "sys_operation_is_sandboxed",
]
