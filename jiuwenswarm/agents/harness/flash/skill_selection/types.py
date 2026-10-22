"""Framework-independent retrieval contracts."""
from dataclasses import dataclass, field
from typing import Protocol, Sequence


@dataclass(frozen=True)
class SkillDocument:
    id: str
    name: str
    directory: str
    text: str
    fingerprint: str
    description: str = ''
    capability_record: dict = field(default_factory=dict)
    # A lightweight index keeps the exact root material that produced its text.
    # Candidate enrichment must never silently combine another root version with it.
    root_sha256: str = ''
    root_stamp: tuple | None = None
    root_raw: str = ''
    capability_description: str = ''
    catalog_root: str = ''


@dataclass(frozen=True)
class Candidate:
    skill_id: str
    score: float


class Retriever(Protocol):
    """Prepare once, then serve concurrent read-only searches."""

    def prepare(self, documents: Sequence[SkillDocument]) -> None:
        ...

    def search(self, query: str, keywords: Sequence[str], *, limit: int,
               allowed_ids: frozenset[str]) -> list[Candidate]:
        ...
