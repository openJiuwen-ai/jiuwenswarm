from jiuwenswarm.server.runtime.skill.skill_vetter.vocabulary import (
    Severity,
    ThreatCategory,
    Finding,
)


def test_severity_values_are_lowercase():
    assert [s.value for s in Severity] == ["low", "medium", "high", "extreme"]


def test_severity_ordering_for_max():
    assert (
        Severity.EXTREME._order
        > Severity.HIGH._order
        > Severity.MEDIUM._order
        > Severity.LOW._order
    )


def test_threat_categories():
    assert {c.value for c in ThreatCategory} == {
        "network",
        "credential-theft",
        "file-access",
        "obfuscation",
        "root",
    }


def test_finding_is_frozen_and_serializable():
    f = Finding(
        category=ThreatCategory.NETWORK,
        severity=Severity.MEDIUM,
        file="scripts/run.py",
        line=3,
        evidence="curl https://evil.example",
        rule_id="network.curl",
    )
    assert f.file == "scripts/run.py"
    assert f.line == 3
    # frozen: reassignment raises
    try:
        f.severity = Severity.HIGH
        raised = False
    except Exception:
        raised = True
    assert raised
