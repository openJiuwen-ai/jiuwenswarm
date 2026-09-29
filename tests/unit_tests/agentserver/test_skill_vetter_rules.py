from jiuwenswarm.server.runtime.skill.skill_vetter.rules import RULES
from jiuwenswarm.server.runtime.skill.skill_vetter.vocabulary import (
    Severity,
    ThreatCategory,
)


def test_rules_cover_all_five_categories():
    categories = {r.category for r in RULES}
    assert categories == set(ThreatCategory)


def test_text_rule_matches_line_and_reports_line_number():
    curl_rule = next(r for r in RULES if r.rule_id == "network.curl")
    findings = curl_rule.scan("scripts/run.sh", "echo hi\ncurl https://evil.example\n")
    assert len(findings) == 1
    f = findings[0]
    assert f.line == 2
    assert f.category is ThreatCategory.NETWORK
    assert "curl" in f.evidence


def test_ssh_private_key_rule_emits_extreme():
    key_rule = next(r for r in RULES if r.rule_id == "credential.ssh-private-key")
    findings = key_rule.scan("x.py", 'data = open("/home/u/.ssh/id_rsa").read()\n')
    assert findings and findings[0].severity is Severity.EXTREME


def test_rule_ids_are_unique():
    ids = [r.rule_id for r in RULES]
    assert len(ids) == len(set(ids))


def test_named_secret_rules_are_high_confidence():
    by_id = {r.rule_id: r for r in RULES}
    assert by_id["credential.ssh-private-key"].confidence == "high"
    assert by_id["credential.cloud-creds"].confidence == "high"


def test_generic_credential_rules_are_low_confidence():
    by_id = {r.rule_id: r for r in RULES}
    assert by_id["credential.env-read"].confidence == "low"
    assert by_id["credential.dotenv"].confidence == "low"
