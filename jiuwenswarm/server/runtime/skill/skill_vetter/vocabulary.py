from __future__ import annotations

import enum
from dataclasses import dataclass


class Severity(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"

    def __init__(self, value: str) -> None:
        self._order = ("low", "medium", "high", "extreme").index(value)


class ThreatCategory(enum.Enum):
    NETWORK = "network"
    CREDENTIAL_THEFT = "credential-theft"
    FILE_ACCESS = "file-access"
    OBFUSCATION = "obfuscation"
    ROOT = "root"


@dataclass(frozen=True)
class Finding:
    category: ThreatCategory
    severity: Severity
    file: str
    line: int
    evidence: str
    rule_id: str
