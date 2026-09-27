"""Unit tests for the paper-quality evaluator and rail.

Covers the dependency-free evaluator (structure/length/reference/formula
scoring), the ``PaperQualityRail`` built by ``build_paper_quality_rail``, the
``_capture_quality_issues`` override, the ``after_task_iteration`` hook and
best-effort registration.
"""

from __future__ import annotations

from jiuwenswarm.symphony.paper_quality_rails import (
    REQUIRED_SECTIONS,
    PaperQualityRail,
    PaperQualityReport,
    build_paper_quality_rail,
    evaluate_paper_quality,
    register_paper_quality_rail,
)

try:  # pragma: no cover - depends on the installed Core package
    from openjiuwen.harness.rails.base import (  # type: ignore[import-untyped]
        AgentRail as _AgentRail,
    )
except Exception:  # noqa: BLE001
    _AgentRail = None

_SECTION_NAMES = [name for name, _ in REQUIRED_SECTIONS]


def _complete_paper() -> str:
    """A paper that satisfies every section, length, reference and formula rule."""

    section = "word " * 420  # 5 sections * 420 words > 2000 words
    return "\n\n".join(
        [
            "## Abstract\n" + section,
            "## 1. Introduction\n" + section,
            "## 2. Method\n" + section + "\n$E = mc^2$",
            "## 3. Experiment\n" + section,
            "## 4. Conclusion\n" + section,
            "## References\n" + "\n".join(f"[{i}] Reference {i}" for i in range(1, 7)),
        ]
    )


def _incomplete_paper() -> str:
    """A long-enough markdown doc that is missing most sections."""

    return "## Abstract\n" + "word " * 120


def _chinese_paper(chars_per_section: int = 1200) -> str:
    """A Chinese paper with enough CJK characters to clear the length check."""

    sentence = "本文提出了一种新颖的方法来解决重要问题并进行了充分的实验验证。"
    block = sentence * (chars_per_section // len(sentence) + 1)
    return "\n\n".join(
        [
            "## 摘要\n" + block,
            "## 引言\n" + block,
            "## 方法\n" + block + "\n$E = mc^2$",
            "## 实验\n" + block,
            "## 结论\n" + block,
            "## 参考文献\n" + "\n".join(f"[{i}] 参考文献 {i}" for i in range(1, 7)),
        ]
    )


def _trajectory_with_text(text: str) -> dict:
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "write_paper",
                                "attributes": [
                                    {
                                        "key": "gen_ai.tool.call.result",
                                        "value": {"stringValue": text},
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


class _FakeTrajectory:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_otlp(self) -> dict:
        return self._payload


class _Inputs:
    def __init__(self, result) -> None:
        self.result = result


class _Ctx:
    def __init__(self, result) -> None:
        self.inputs = _Inputs(result)


def test_complete_paper_passes():
    report = evaluate_paper_quality(_complete_paper())
    assert isinstance(report, PaperQualityReport)
    assert report.missing_sections == []
    assert report.issues == []
    assert report.is_complete is True
    assert report.score >= 60
    assert report.has_latex_formulas is True
    assert set(report.to_dict()) == {
        "is_complete",
        "missing_sections",
        "total_sections",
        "word_count",
        "reference_count",
        "has_latex_formulas",
        "issues",
        "score",
    }


def test_missing_sections_are_reported():
    report = evaluate_paper_quality("## Abstract\n" + "word " * 100)
    assert report.missing_sections == [
        name for name in _SECTION_NAMES if name != "abstract"
    ]
    assert report.is_complete is False


def test_short_paper_flags_length_references_and_formula():
    report = evaluate_paper_quality("## Abstract\nsome short text")
    joined = " ".join(report.issues)
    assert "论文字数不足" in joined
    assert "参考文献不足" in joined
    assert "未检测到数学公式" in joined


def test_chinese_paper_passes_length_check():
    # A whitespace split would return a handful of tokens for Chinese text;
    # the CJK-aware count must recognise a normal Chinese paper as long enough.
    report = evaluate_paper_quality(_chinese_paper())
    assert "论文字数不足" not in " ".join(report.issues)
    assert report.word_count >= 4000  # 5 sections * 1200 CJK chars
    assert report.missing_sections == []
    assert report.is_complete is True


def test_short_chinese_paper_flags_length():
    report = evaluate_paper_quality("## 摘要\n这是一篇很短的论文。")
    assert any("论文字数不足" in issue for issue in report.issues)
    assert report.word_count > 0  # CJK characters are counted, not split on spaces


def test_empty_text_scores_low_and_misses_everything():
    report = evaluate_paper_quality("")
    assert report.missing_sections == _SECTION_NAMES
    assert report.word_count == 0
    assert report.reference_count == 0
    assert report.has_latex_formulas is False
    assert report.score == 5
    assert report.is_complete is False


def test_build_paper_quality_rail_returns_agent_rail():
    rail = build_paper_quality_rail()
    assert isinstance(rail, PaperQualityRail)
    assert rail.priority == 60
    assert callable(rail._capture_quality_issues)
    if _AgentRail is not None:
        assert isinstance(rail, _AgentRail)


def test_capture_quality_issues_from_otlp_payload():
    rail = build_paper_quality_rail()
    issues = rail._capture_quality_issues(_trajectory_with_text(_incomplete_paper()))

    codes = {issue["code"] for issue in issues}
    assert "paper_section_missing" in codes
    assert any(issue.get("section") == "references" for issue in issues)


def test_capture_quality_issues_from_trajectory_object():
    rail = build_paper_quality_rail()
    payload = _trajectory_with_text(_incomplete_paper())
    issues = rail._capture_quality_issues(_FakeTrajectory(payload))
    assert issues  # same shape as the mapping path


def test_capture_quality_issues_empty_for_none_and_non_paper():
    rail = build_paper_quality_rail()
    assert rail._capture_quality_issues(None) == ()
    assert rail._capture_quality_issues("no headings here") == ()


async def test_after_task_iteration_records_report_and_calls_callback():
    captured: list[PaperQualityReport] = []
    rail = build_paper_quality_rail(on_issues=captured.append)

    await rail.after_task_iteration(_Ctx(_incomplete_paper()))

    assert rail.last_report is not None
    assert rail.last_report.is_complete is False
    assert captured == [rail.last_report]


async def test_after_task_iteration_ignores_non_paper_results():
    rail = build_paper_quality_rail()
    await rail.after_task_iteration(_Ctx("too short"))
    assert rail.last_report is None


def test_register_paper_quality_rail_registers_or_returns():
    class _Harness:
        def __init__(self) -> None:
            self.rails: list = []

        def register_rail(self, rail) -> None:
            self.rails.append(rail)

    harness = _Harness()
    rail = register_paper_quality_rail(harness)
    assert harness.rails == [rail]

    # a harness without register_rail still yields the built rail
    fallback = register_paper_quality_rail(object())
    assert isinstance(fallback, PaperQualityRail)
