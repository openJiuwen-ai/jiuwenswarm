"""Paper-quality Rail — grade a generated paper after the agent finishes a task.

This adapts the pre-refactor ``symphony/experience/paper_quality_rails.py`` to
the current architecture.  Upstream moved the rail base classes into the
``openjiuwen`` Core package (``openjiuwen.harness.rails``), so the old
``harness.register_rail("after_task_iteration", hook)`` wiring is replaced by a
subclass of the Core rail base plus a factory function.

Two layers are provided:

* a **dependency-free** quality evaluator (:func:`evaluate_paper_quality`)
  that grades structure, references, length and formulas, and
* a :class:`PaperQualityRail` (built by :func:`build_paper_quality_rail`) that
  runs the evaluator in the ``after_task_iteration`` hook and exposes a
  ``_capture_quality_issues`` override mirroring
  ``jiuwenswarm.symphony.experience._build_graph_evolution_rail``.

If the Core rail base is unavailable (degradation mode) the module still
imports: :class:`PaperQualityRail` simply falls back to a minimal local base
and the evaluator keeps working.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

LOGGER = logging.getLogger(__name__)

# The Core rail base (``openjiuwen.harness.rails.base.AgentRail``) provides the
# class-based lifecycle hooks (``after_task_iteration`` etc.).  Import it
# defensively so the module degrades to the standalone evaluator when the Core
# package is not installed.
try:  # pragma: no cover - exercised implicitly by the installed/uninstalled env
    from openjiuwen.harness.rails.base import AgentRail as _AgentRail  # type: ignore[import-untyped]
except Exception:  # noqa: BLE001 - any import failure means "run standalone"
    _AgentRail = None  # type: ignore[assignment]

# Minimum score (0-100) for the rail to treat a paper as accepted.
DEFAULT_MIN_SCORE = 60
# A produced result is only treated as a paper above this length / with headings.
_MIN_PAPER_CHARS = 500

# Required paper sections: (name, heading pattern).
REQUIRED_SECTIONS = [
    ("abstract", r"(?i)#+\s*abstract|摘要"),
    ("introduction", r"(?i)#+\s*\d*\.?\s*introduction|引言"),
    ("method", r"(?i)#+\s*\d*\.?\s*method|方法"),
    ("experiment", r"(?i)#+\s*\d*\.?\s*experiment|实验"),
    ("conclusion", r"(?i)#+\s*\d*\.?\s*conclusion|结论"),
    ("references", r"(?i)#+\s*references|参考文献"),
]


@dataclass
class PaperQualityReport:
    """Structured paper-quality report."""

    is_complete: bool
    missing_sections: list[str]
    total_sections: int
    word_count: int
    reference_count: int
    has_latex_formulas: bool
    issues: list[str]
    score: int  # 0-100

    def to_dict(self) -> dict:
        return {
            "is_complete": self.is_complete,
            "missing_sections": self.missing_sections,
            "total_sections": self.total_sections,
            "word_count": self.word_count,
            "reference_count": self.reference_count,
            "has_latex_formulas": self.has_latex_formulas,
            "issues": self.issues,
            "score": self.score,
        }


def evaluate_paper_quality(paper_text: str) -> PaperQualityReport:
    """Evaluate the quality of a paper.

    This is the dependency-free core; it can be called directly or from a rail
    hook.  In the new architecture it is wired into the agent lifecycle by
    :class:`PaperQualityRail` / :func:`build_paper_quality_rail`.

    Args:
        paper_text: Paper Markdown text.

    Returns:
        :class:`PaperQualityReport`.
    """

    issues: list[str] = []
    missing: list[str] = []
    found_sections = 0

    text = paper_text if isinstance(paper_text, str) else ""

    # 1. section completeness
    for section_name, pattern in REQUIRED_SECTIONS:
        if re.search(pattern, text):
            found_sections += 1
        else:
            missing.append(section_name)

    # 2. word count
    word_count = len(text.split())

    # 3. references
    reference_count = len(re.findall(r"^\[\d+\]", text, re.MULTILINE))

    # 4. LaTeX formulas
    has_latex = bool(re.search(r"\$.*?\$", text))

    # 5. quality problems
    if word_count < 2000:
        issues.append(f"论文字数不足（{word_count}字，建议≥2000字）")
    if reference_count < 5:
        issues.append(f"参考文献不足（{reference_count}条，建议≥5条）")
    if not has_latex:
        issues.append("未检测到数学公式，建议在Method部分添加公式描述")
    if "failed" in text.lower() or "generation failed" in text.lower():
        issues.append("检测到生成失败的章节内容")

    # 6. quality score
    score = 0.0
    score += (found_sections / len(REQUIRED_SECTIONS)) * 30  # completeness: 30
    score += min(word_count / 3000, 1.0) * 20                # length: 20
    score += min(reference_count / 10, 1.0) * 20             # references: 20
    score += 10 if has_latex else 0                          # formulas: 10
    score += 20 if not issues else max(0, 20 - len(issues) * 5)  # cleanliness: 20
    score_int = int(min(score, 100))

    is_complete = len(missing) == 0 and len(issues) == 0

    return PaperQualityReport(
        is_complete=is_complete,
        missing_sections=missing,
        total_sections=found_sections,
        word_count=word_count,
        reference_count=reference_count,
        has_latex_formulas=has_latex,
        issues=issues,
        score=score_int,
    )


class _FallbackRail:
    """Minimal rail stand-in used when the Core rail base is unavailable."""

    priority = 50

    def init(self, agent: Any) -> None:  # noqa: D401 - matches Core signature
        """No-op init mirroring the Core rail lifecycle."""

    def uninit(self, agent: Any) -> None:
        """No-op uninit mirroring the Core rail lifecycle."""


_RailBase: type = _AgentRail if _AgentRail is not None else _FallbackRail


class PaperQualityRail(_RailBase):
    """Rail that grades a paper artifact after each task iteration.

    Args:
        min_score: Minimum score (0-100) for a paper to be considered accepted.
        on_issues: Optional callback invoked with the
            :class:`PaperQualityReport` when a paper fails the check.
    """

    priority = 60

    def __init__(
        self,
        *,
        min_score: int = DEFAULT_MIN_SCORE,
        on_issues: Callable[[PaperQualityReport], None] | None = None,
    ) -> None:
        super().__init__()
        self._min_score = int(min_score)
        self._on_issues = on_issues
        self._last_report: PaperQualityReport | None = None

    @property
    def last_report(self) -> PaperQualityReport | None:
        """Report produced by the most recent ``after_task_iteration``."""

        return self._last_report

    async def after_task_iteration(self, ctx: Any) -> None:
        """Grade the iteration result if it looks like a paper."""

        text = _extract_paper_text(getattr(ctx, "inputs", None))
        if text is None:
            return

        report = evaluate_paper_quality(text)
        self._last_report = report

        if report.is_complete and report.score >= self._min_score:
            LOGGER.info("Paper quality check PASSED (score=%d)", report.score)
            return

        LOGGER.warning(
            "Paper quality check FAILED (score=%d): missing=%s issues=%s",
            report.score,
            report.missing_sections,
            report.issues,
        )
        if self._on_issues is not None:
            try:
                self._on_issues(report)
            except Exception:  # noqa: BLE001 - a rail must never break the agent
                LOGGER.warning("paper quality on_issues callback failed", exc_info=True)

    def _capture_quality_issues(
        self,
        trajectory: Any,
    ) -> tuple[Mapping[str, object], ...]:
        """Override example: derive paper-quality issues from a trajectory.

        Mirrors the ``_capture_quality_issues`` hook that
        ``jiuwenswarm.symphony.experience._build_graph_evolution_rail`` wraps:
        it accepts an ``openjiuwen`` ``Trajectory`` (or its OTLP mapping) and
        returns a tuple of issue mappings, each carrying a ``code`` key.

        Args:
            trajectory: ``Trajectory`` value object, OTLP mapping, or text.

        Returns:
            Tuple of issue mappings; empty when no paper text is found.
        """

        text = _paper_text_from_trajectory(trajectory)
        if not text or "## " not in text:
            return ()

        report = evaluate_paper_quality(text)
        issues: list[Mapping[str, object]] = []
        for section in report.missing_sections:
            issues.append({"code": "paper_section_missing", "section": section})
        for detail in report.issues:
            issues.append({"code": "paper_quality_issue", "detail": detail})
        return tuple(issues)


def build_paper_quality_rail(
    *,
    min_score: int = DEFAULT_MIN_SCORE,
    on_issues: Callable[[PaperQualityReport], None] | None = None,
) -> PaperQualityRail:
    """Build a :class:`PaperQualityRail` (factory, like the graph rail builder).

    Args:
        min_score: Minimum accepted score (0-100).
        on_issues: Optional callback for failing reports.

    Returns:
        A configured :class:`PaperQualityRail`.
    """

    return PaperQualityRail(min_score=min_score, on_issues=on_issues)


def register_paper_quality_rail(harness: Any, **kwargs: Any) -> PaperQualityRail:
    """Best-effort registration of the paper-quality rail on a harness/agent.

    Newer harnesses accept a rail *instance* via ``register_rail`` (see the
    Core ``AgentRail`` docs); older ones may expose a different API, in which
    case the built rail is simply returned for the caller to register.

    Args:
        harness: Agent/harness exposing an optional ``register_rail``.
        **kwargs: Forwarded to :func:`build_paper_quality_rail`.

    Returns:
        The built :class:`PaperQualityRail`.
    """

    rail = build_paper_quality_rail(**kwargs)
    register = getattr(harness, "register_rail", None)
    if callable(register):
        register(rail)
    return rail


# -- helpers ----------------------------------------------------------------


def _extract_paper_text(inputs: Any) -> str | None:
    """Return paper text from a ``TaskIterationInputs``-like object, or None."""

    if inputs is None:
        return None
    text = _result_text(getattr(inputs, "result", None))
    if text is None:
        return None
    if len(text) < _MIN_PAPER_CHARS or "## " not in text:
        return None
    return text


def _result_text(result: Any) -> str | None:
    """Best-effort extraction of a text result from an iteration result."""

    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        for key in ("content", "text", "output", "paper", "message"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in result.values():
            if isinstance(value, str) and value.strip():
                return value
    return None


def _paper_text_from_trajectory(trajectory: Any) -> str:
    """Best-effort extraction of the longest string payload in a trajectory."""

    if trajectory is None:
        return ""
    if isinstance(trajectory, str):
        return trajectory

    payload = _trajectory_payload(trajectory)
    if payload is None:
        return ""

    best = ""
    for span in _iter_spans(payload):
        for value in _iter_span_strings(span):
            if len(value) > len(best):
                best = value
    return best


def _trajectory_payload(source: Any) -> Mapping[str, Any] | None:
    """Return an OTLP mapping for a Trajectory-like object, else ``None``."""

    to_otlp = getattr(source, "to_otlp", None)
    if callable(to_otlp):
        payload = to_otlp()
        return payload if isinstance(payload, Mapping) else None
    if isinstance(source, Mapping):
        return source
    return None


def _iter_spans(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    spans: list[Mapping[str, Any]] = []
    for resource_span in _as_mapping_list(payload.get("resourceSpans")):
        for scope_span in _as_mapping_list(resource_span.get("scopeSpans")):
            spans.extend(_as_mapping_list(scope_span.get("spans")))
    return spans


def _iter_span_strings(span: Mapping[str, Any]) -> list[str]:
    attributes = span.get("attributes")
    values: list[str] = []
    if isinstance(attributes, Mapping):
        items = attributes.items()
    elif isinstance(attributes, list):
        items = [
            (item.get("key"), item.get("value"))
            for item in attributes
            if isinstance(item, Mapping) and "key" in item
        ]
    else:
        items = []
    for _, encoded in items:
        decoded = _decode_attribute_value(encoded)
        if isinstance(decoded, str) and decoded.strip():
            values.append(decoded)
    return values


def _decode_attribute_value(encoded: Any) -> Any:
    if not isinstance(encoded, Mapping):
        return encoded
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in encoded:
            return encoded[key]
    return None


def _as_mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


__all__ = [
    "evaluate_paper_quality",
    "register_paper_quality_rail",
    "build_paper_quality_rail",
    "PaperQualityRail",
    "PaperQualityReport",
    "REQUIRED_SECTIONS",
    "DEFAULT_MIN_SCORE",
]
