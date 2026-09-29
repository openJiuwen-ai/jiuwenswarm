from pathlib import Path

from jiuwenswarm.server.runtime.skill.skill_vetter.llm_escalation import review_findings
from jiuwenswarm.server.runtime.skill.skill_vetter.vocabulary import Finding, Severity, ThreatCategory


def _f(sev: Severity, cat: ThreatCategory) -> Finding:
    return Finding(category=cat, severity=sev, file="x", line=1, evidence="e", rule_id="r")


def test_no_reviewer_returns_unchanged_and_not_escalated():
    findings = [_f(Severity.MEDIUM, ThreatCategory.NETWORK)]
    out, escalated = review_findings(findings, Path("."))
    assert escalated is False
    assert out == findings


def test_reviewer_result_is_used_and_flagged_escalated():
    def reviewer(fs, skill_dir):
        return [_f(Severity.HIGH, ThreatCategory.NETWORK)]

    out, escalated = review_findings([_f(Severity.LOW, ThreatCategory.NETWORK)], Path("."), reviewer=reviewer)
    assert escalated is True
    assert out[0].severity is Severity.HIGH


def test_reviewer_exception_falls_back_to_deterministic():
    def broken(fs, skill_dir):
        raise RuntimeError("model down")

    findings = [_f(Severity.EXTREME, ThreatCategory.ROOT)]
    out, escalated = review_findings(findings, Path("."), reviewer=broken)
    assert escalated is False
    assert out == findings
