from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Tuple, Optional
import re


_TERM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _validate_term(term: str) -> None:
    if not _TERM_RE.match(term):
        raise ValueError(
            "Invalid term: %s. Terms must start with a letter and contain only letters, numbers, '_' or '-'."
            % term
        )


@dataclass(frozen=True)
class CID:
    terms: Tuple[str, ...] = ()

    @staticmethod
    def from_str(value: str) -> "CID":
        value = (value or "").strip()
        if value == "":
            return CID(())
        parts = tuple(part.strip() for part in value.split("."))
        for term in parts:
            _validate_term(term)
        return CID(parts)

    def to_str(self) -> str:
        return ".".join(self.terms)

    def is_root(self) -> bool:
        return len(self.terms) == 0

    def parent(self) -> Optional["CID"]:
        if self.is_root():
            return None
        return CID(self.terms[:-1])

    def child(self, term: str) -> "CID":
        _validate_term(term)
        return CID(self.terms + (term,))

    def startswith(self, other: "CID") -> bool:
        if len(other.terms) > len(self.terms):
            return False
        return self.terms[:len(other.terms)] == other.terms

    def replace_prefix(self, old_prefix: "CID", new_prefix: "CID") -> "CID":
        if not self.startswith(old_prefix):
            raise ValueError(f"{self.to_str()} does not start with prefix {old_prefix.to_str()}")
        suffix = self.terms[len(old_prefix.terms):]
        return CID(new_prefix.terms + suffix)


class NodeType(str, Enum):
    BRANCH = "branch"
    LEAF = "leaf"
    SYSTEM = "system"


@dataclass(frozen=True)
class NodeSpec:
    cid: CID
    name: str
    description: str
    node_type: NodeType
    worker_id: Optional[str]
    keywords: Tuple[str, ...] = ()
    examples: Tuple[str, ...] = ()
