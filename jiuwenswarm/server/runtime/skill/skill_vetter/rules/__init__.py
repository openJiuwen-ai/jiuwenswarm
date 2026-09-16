from __future__ import annotations

import re
from dataclasses import dataclass

from ..vocabulary import Finding, Severity, ThreatCategory


@dataclass(frozen=True)
class TextRule:
    rule_id: str
    category: ThreatCategory
    severity: Severity
    pattern: "re.Pattern[str]"

    def scan(self, rel_path: str, text: str) -> list[Finding]:
        findings: list[Finding] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if self.pattern.search(line):
                findings.append(
                    Finding(
                        category=self.category,
                        severity=self.severity,
                        file=rel_path,
                        line=lineno,
                        evidence=line.strip()[:200],
                        rule_id=self.rule_id,
                    )
                )
        return findings


def _rule(
    rule_id: str, category: ThreatCategory, severity: Severity, pattern: str
) -> TextRule:
    return TextRule(rule_id, category, severity, re.compile(pattern, re.IGNORECASE))


_NET = ThreatCategory.NETWORK
_CRED = ThreatCategory.CREDENTIAL_THEFT
_FILE = ThreatCategory.FILE_ACCESS
_OBF = ThreatCategory.OBFUSCATION
_ROOT = ThreatCategory.ROOT

RULES: list[TextRule] = [
    # network
    _rule("network.curl", _NET, Severity.MEDIUM, r"\bcurl\b"),
    _rule("network.wget", _NET, Severity.MEDIUM, r"\bwget\b"),
    _rule(
        "network.exfil", _NET, Severity.HIGH, r"\bcurl\b.*(-T|--upload-file)|\bscp\b"
    ),
    _rule(
        "network.python-http",
        _NET,
        Severity.MEDIUM,
        r"\b(requests|urllib|urllib2|httpx|aiohttp|socket|http\.client)\b",
    ),
    _rule(
        "network.powershell-http",
        _NET,
        Severity.MEDIUM,
        r"\b(Invoke-WebRequest|Invoke-RestMethod)\b",
    ),
    # credential-theft
    _rule(
        "credential.ssh-private-key",
        _CRED,
        Severity.EXTREME,
        r"\.ssh/(id_rsa|id_ed25519|id_dsa|id_ecdsa)\b",
    ),
    _rule(
        "credential.cloud-creds",
        _CRED,
        Severity.HIGH,
        r"(\.aws/credentials|\.config/gcloud|kubeconfig|\.kube/config|\.netrc)\b",
    ),
    _rule(
        "credential.env-read",
        _CRED,
        Severity.MEDIUM,
        r"(os\.environ|os\.getenv|getenv\(|\benv\[|\bprocess\.env\b)",
    ),
    _rule("credential.dotenv", _CRED, Severity.MEDIUM, r"(\.env\b|dotenv|load_dotenv)"),
    # file-access
    _rule("file.shadow", _FILE, Severity.EXTREME, r"/etc/(shadow|sudoers)\b"),
    _rule(
        "file.etc-read", _FILE, Severity.MEDIUM, r"\b/etc/(passwd|hosts|resolv\.conf)\b"
    ),
    _rule("file.traversal", _FILE, Severity.HIGH, r"(\.\./){2,}|\.\.[\\/]"),
    _rule("file.home-read", _FILE, Severity.MEDIUM, r"\b(?:/home/|/root/|~)\b"),
    # obfuscation
    _rule("obfuscation.eval", _OBF, Severity.HIGH, r"\b(eval|exec)\s*\("),
    _rule(
        "obfuscation.base64",
        _OBF,
        Severity.MEDIUM,
        r"\b(base64\.b64decode|base64\s+-(d|--decode)|fromhex)\b",
    ),
    _rule("obfuscation.blob", _OBF, Severity.MEDIUM, r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    # root
    _rule("root.sudo", _ROOT, Severity.HIGH, r"\b(sudo|doas)\b"),
    _rule(
        "root.setuid",
        _ROOT,
        Severity.EXTREME,
        r"\b(setuid|seteuid|setgid|os\.setuid|os\.seteuid)\b",
    ),
    _rule(
        "root.suid-bit",
        _ROOT,
        Severity.EXTREME,
        r"\b(chmod\s+[0-7]*4[0-7]{2}|chmod\s+u\+s|setcap)\b",
    ),
]
