from jiuwenswarm.server.runtime.skill.skill_vetter.report import (
    VetReport,
    build_report,
    grade_from_findings,
    run_vet,
)
from jiuwenswarm.server.runtime.skill.skill_vetter.scanner import scan_skill
from jiuwenswarm.server.runtime.skill.skill_vetter.vocabulary import Finding, Severity, ThreatCategory


def _finding(sev: Severity, category: ThreatCategory) -> Finding:
    return Finding(category=category, severity=sev, file="x.py", line=1, evidence="e", rule_id="r")


def test_grade_is_max_severity():
    assert grade_from_findings([]) == "unvetted"
    assert grade_from_findings([_finding(Severity.LOW, ThreatCategory.NETWORK)]) == "low"
    assert grade_from_findings([
        _finding(Severity.LOW, ThreatCategory.NETWORK),
        _finding(Severity.EXTREME, ThreatCategory.ROOT),
    ]) == "extreme"


def test_build_report_roundtrips(tmp_path):
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n", encoding="utf-8")
    findings = [_finding(Severity.MEDIUM, ThreatCategory.NETWORK)]
    r = build_report(skill, findings, "abc123", escalated=False)
    assert r.grade == "medium"
    assert r.content_hash == "abc123"
    d = r.to_dict()
    r2 = VetReport.from_dict(d)
    assert r2.grade == r.grade
    assert len(r2.findings) == 1
    assert r2.findings[0]["category"] == "network"


def test_run_vet_scans_and_grades(tmp_path):
    skill = tmp_path / "s"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n", encoding="utf-8")
    (skill / "scripts" / "run.sh").write_text("curl https://x.example\n", encoding="utf-8")
    report = run_vet(skill)
    assert report.grade in {"medium", "high", "extreme"}
    assert report.content_hash and len(report.content_hash) == 64


def test_reviewer_cannot_lower_deterministic_grade(tmp_path):
    skill = tmp_path / "s"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n", encoding="utf-8")
    (skill / "scripts" / "run.sh").write_text("sudo chmod 4755 /bin/sh\n", encoding="utf-8")

    deterministic_grade = grade_from_findings(scan_skill(skill))
    assert deterministic_grade in {"high", "extreme"}

    def reviewer(findings, skill_dir):
        return [_finding(Severity.LOW, ThreatCategory.NETWORK)]

    report = run_vet(skill, reviewer=reviewer)

    assert report.grade == deterministic_grade
    assert report.escalated is True
    assert len(report.findings) == 1
    assert report.findings[0]["severity"] == "low"
