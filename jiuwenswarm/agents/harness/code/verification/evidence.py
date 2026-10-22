# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extract gate evidence from a ``run_tests`` tool result.

``ImplementCompletenessRail.after_tool_call`` receives whatever agent-core
passes as ``tool_result`` — normally the ``ToolOutput`` returned by
``RunTestsTool.invoke`` (structured ``data``), but a rendered string or a dict
is possible depending on the call path. This module accepts all three:
structured fields first, then the machine-readable first line of the summary
(``run_tests: outcome=<o> passed_languages=<a,b|->``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.agents.harness.code.verification.discovery import LANGUAGES

_HEADER_RE = re.compile(r"run_tests:\s+outcome=(\w+)\s+passed_languages=([^\s]+)")


@dataclass(frozen=True, slots=True)
class RunTestsEvidence:
    outcome: str
    passed_languages: frozenset[str]
    command: str

    @property
    def success(self) -> bool:
        return self.outcome == "passed"


def _data_of(result: Any) -> Any:
    for attr in ("data", "content"):
        value = getattr(result, attr, None)
        if value is not None:
            return value
    if isinstance(result, dict):
        return result.get("data", result.get("content", result))
    return result


def _effective_outcome(data: dict[str, Any]) -> str:
    """Outcome for the gate, judged per language rather than by the overall rank.

    The overall ``outcome`` ranks ``no_tests`` above ``passed`` (it is "passed"
    only when every framework passed), so a Python pass next to an npm suite
    with no tests would be dropped. Per language: any failure or timeout wins,
    otherwise a real pass for at least one language is evidence (the gate
    still requires every *changed* language to be among ``passed_languages``).
    """

    per_language = data.get("language_outcomes")
    if not isinstance(per_language, dict) or not per_language:
        return str(data.get("outcome"))
    outcomes = {str(v) for v in per_language.values()}
    if "failed" in outcomes:
        return "failed"
    if "timeout" in outcomes:
        return "timeout"
    if "passed" in outcomes:
        return "passed"
    return str(data.get("outcome"))


def parse_run_tests_evidence(result: Any) -> RunTestsEvidence | None:
    """Return evidence, or None when the result carries none (tool error, unknown shape)."""

    if result is None:
        return None
    if getattr(result, "success", True) is False:
        return None
    data = _data_of(result)
    if isinstance(data, dict) and "outcome" in data:
        langs = data.get("passed_languages") or []
        commands = [str(r.get("command", "")) for r in data.get("results") or [] if isinstance(r, dict)]
        return RunTestsEvidence(
            outcome=_effective_outcome(data),
            passed_languages=frozenset(lang for lang in langs if lang in LANGUAGES),
            command="run_tests: " + " ; ".join(c for c in commands if c),
        )
    text = data.get("content") if isinstance(data, dict) else data
    match = _HEADER_RE.search(str(text or ""))
    if match is None:
        return None
    langs = [] if match.group(2) == "-" else match.group(2).split(",")
    return RunTestsEvidence(
        outcome=match.group(1),
        passed_languages=frozenset(lang for lang in langs if lang in LANGUAGES),
        command="run_tests",
    )


__all__ = ["RunTestsEvidence", "parse_run_tests_evidence"]
