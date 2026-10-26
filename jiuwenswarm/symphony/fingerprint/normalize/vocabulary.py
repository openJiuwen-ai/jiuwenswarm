# -*- coding: utf-8 -*-
"""Base classes and utilities for dynamic vocabularies."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Protocol, Set, Union


def term_similarity(left: str, right: str) -> float:
    """Compute similarity between two vocabulary terms.

    Uses both word overlap (Jaccard on split parts) and sequence matching.
    """
    left_parts = set(left.split("_"))
    right_parts = set(right.split("_"))
    overlap = len(left_parts & right_parts) / max(1, len(left_parts | right_parts))
    ratio = SequenceMatcher(None, left, right).ratio()
    return max(overlap, ratio)


def _merge_definitions(existing: str, incoming: str) -> str:
    """Merge two definitions, avoiding duplication.

    If the incoming definition is already contained in the existing one,
    return the existing definition unchanged. Otherwise, append the
    incoming definition with a separator.
    """
    existing = existing.strip()
    incoming = incoming.strip()
    if not incoming:
        return existing
    if not existing:
        return incoming
    # Check if incoming is already contained (case-insensitive)
    if incoming.lower() in existing.lower():
        return existing
    # Avoid duplication of similar content
    if existing.lower() == incoming.lower():
        return existing
    # Append with separator
    return f"{existing}; {incoming}"


@dataclass(frozen=True)
class BaseCandidate:
    """Base class for vocabulary resolution candidates."""

    raw_value: str
    token: str
    description: str
    skill_id: str
    path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_value": self.raw_value,
            "token": self.token,
            "description": self.description,
            "skill_id": self.skill_id,
            "path": self.path,
        }


@dataclass(frozen=True)
class BaseResolution:
    """Base class for vocabulary resolution results."""

    action: str
    normalized_value: Optional[str]
    confidence: float
    reason: str = ""
    forced_merge: bool = False
    definition: str = ""


@dataclass
class BaseVocabTerm:
    """Base class for vocabulary terms."""

    name: str
    aliases: Set[str] = field(default_factory=set)
    definition: str = ""
    examples: List[str] = field(default_factory=list)
    count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "definition": self.definition,
            "aliases": sorted(alias for alias in self.aliases if alias != self.name),
            "examples": list(self.examples),
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaseVocabTerm":
        name = str(data.get("name") or "").strip()
        aliases = {
            str(alias).strip()
            for alias in data.get("aliases", [])
            if str(alias).strip()
        }
        return cls(
            name=name,
            aliases=aliases,
            definition=str(data.get("definition") or ""),
            examples=[str(item) for item in data.get("examples", [])],
            count=int(data.get("count") or 0),
        )


class BaseVocabulary(Protocol):
    """Protocol defining the common interface for all vocabularies.

    Both static and dynamic vocabularies implement this interface,
    allowing consistent usage across different vocabulary types.
    """

    version: str

    def lookup(self, token: str) -> Optional[str]:
        """Look up a token in the vocabulary, returning the canonical term."""
        ...

    def term_names(self) -> List[str]:
        """Return sorted list of canonical term names."""
        ...

    def to_dict(self) -> Dict[str, Any]:
        """Serialize vocabulary to a dictionary."""
        ...

    def save(self, path: Union[Path, str]) -> None:
        """Save vocabulary to a JSON file."""
        ...


@dataclass(frozen=True)
class StaticVocabulary:
    """Immutable vocabulary with fixed terms and alias mappings.

    Suitable for controlled vocabularies that don't need dynamic growth,
    such as DataType vocabulary with predefined formats/carriers.
    """

    version: str
    vocab: frozenset
    aliases: Dict[str, str] = field(default_factory=dict)

    def lookup(self, token: str) -> Optional[str]:
        """Look up a token, returning the canonical term if found."""
        if token in self.vocab:
            return token
        return self.aliases.get(token)

    def resolve(self, raw_token: str) -> str:
        """Resolve a raw token to a canonical vocabulary term.

        Returns the canonical term if found, otherwise returns "unknown"
        (or the raw token if "unknown" is not in vocab).
        """
        normalized = raw_token.lower().strip()
        # Direct match in vocab
        if normalized in self.vocab:
            return normalized
        # Check aliases
        alias_result = self.aliases.get(normalized)
        if alias_result and alias_result in self.vocab:
            return alias_result
        # Fallback to "unknown" if available
        if "unknown" in self.vocab:
            return "unknown"
        return normalized

    def term_names(self) -> List[str]:
        """Return sorted list of canonical term names."""
        return sorted(self.vocab)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize vocabulary to a dictionary."""
        return {
            "version": self.version,
            "vocab": sorted(self.vocab),
            "aliases": dict(self.aliases),
        }

    def save(self, path: Union[Path, str]) -> None:
        """Save vocabulary to a JSON file."""
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        *,
        default_version: str = "static-vocab-v1",
    ) -> "StaticVocabulary":
        """Build vocabulary from a dictionary."""
        vocab_data = data.get("vocab", [])
        vocab = frozenset(
            str(item).strip()
            for item in vocab_data
            if str(item).strip()
        )
        aliases = {
            str(k).strip(): str(v).strip()
            for k, v in data.get("aliases", {}).items()
            if str(k).strip() and str(v).strip()
        }
        return cls(
            version=str(data.get("version") or default_version),
            vocab=vocab,
            aliases=aliases,
        )

    @classmethod
    def load(
        cls,
        path: Union[Path, str],
        *,
        default_version: str = "static-vocab-v1",
    ) -> "StaticVocabulary":
        """Load vocabulary from a JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data, default_version=default_version)

    def __contains__(self, token: str) -> bool:
        """Check if a token is in the vocabulary (canonical or alias)."""
        return token in self.vocab or token in self.aliases


class DynamicVocabulary:
    """Thread-safe mutable bounded vocabulary for semantic fields.

    Suitable for vocabularies that need to grow dynamically based on
    observed data, such as I/O name vocabulary.
    """

    def __init__(
        self,
        *,
        version: str,
        max_vocab_size: Optional[int],
        terms: Optional[List[BaseVocabTerm]] = None,
    ) -> None:
        self.version = version
        self.max_vocab_size = _normalize_max_vocab_size(max_vocab_size)
        self._terms: Dict[str, BaseVocabTerm] = {}
        self._aliases: Dict[str, str] = {}
        self._lock = RLock()
        for term in terms or []:
            self.add_term(term)

    @classmethod
    def from_aliases(
        cls,
        *,
        version: str,
        max_vocab_size: Optional[int],
        aliases: Dict[str, str],
    ) -> "DynamicVocabulary":
        """Build vocabulary from an alias mapping."""
        terms: Dict[str, BaseVocabTerm] = {}
        for alias, name in aliases.items():
            term = terms.setdefault(name, BaseVocabTerm(name=name))
            term.aliases.add(alias)
        for name, term in list(terms.items()):
            term.aliases.discard(name)
        return cls(
            version=version,
            max_vocab_size=max_vocab_size,
            terms=list(terms.values()),
        )

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        *,
        default_version: str,
        default_max_vocab_size: Optional[int],
    ) -> "DynamicVocabulary":
        return cls(
            version=str(data.get("version") or default_version),
            max_vocab_size=_max_vocab_size_from_data(
                data.get("max_vocab_size"),
                default_max_vocab_size,
            ),
            terms=[
                BaseVocabTerm.from_dict(item)
                for item in data.get("terms", [])
                if isinstance(item, dict)
            ],
        )

    @classmethod
    def load(
        cls,
        path: Union[Path, str],
        *,
        default_version: str,
        default_max_vocab_size: Optional[int],
    ) -> "DynamicVocabulary":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(
            data,
            default_version=default_version,
            default_max_vocab_size=default_max_vocab_size,
        )

    def save(self, path: Union[Path, str]) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add_term(self, term: BaseVocabTerm) -> None:
        if not term.name:
            return
        with self._lock:
            self._terms[term.name] = term
            self._aliases[term.name] = term.name
            for alias in term.aliases:
                if alias:
                    self._aliases[alias] = term.name

    def lookup(self, token: str) -> Optional[str]:
        with self._lock:
            return self._aliases.get(token)

    def term_names(self) -> List[str]:
        with self._lock:
            return sorted(self._terms)

    def size(self) -> int:
        with self._lock:
            return len(self._terms)

    def is_full(self) -> bool:
        return self.max_vocab_size is not None and self.size() >= self.max_vocab_size

    def add_alias(
        self,
        alias: str,
        target: str,
        *,
        example: str = "",
        definition: str = "",
    ) -> None:
        with self._lock:
            term = self._terms.get(target)
            if term is None:
                return
            if alias and alias != target:
                term.aliases.add(alias)
                self._aliases[alias] = target
            if example and example not in term.examples:
                term.examples.append(example)
            if definition:
                term.definition = _merge_definitions(term.definition, definition)
            term.count += 1

    def ensure_term(
        self,
        name: str,
        *,
        alias: str = "",
        example: str = "",
        definition: str = "",
    ) -> str:
        with self._lock:
            if (
                self.max_vocab_size is not None
                and name not in self._terms
                and len(self._terms) >= self.max_vocab_size
            ):
                return self.closest_term(name) or name
            term = self._terms.get(name)
            if term is None:
                term = BaseVocabTerm(name=name, definition=definition)
                self._terms[name] = term
                self._aliases[name] = name
            elif definition:
                term.definition = _merge_definitions(term.definition, definition)
            if alias and alias != name:
                term.aliases.add(alias)
                self._aliases[alias] = name
            if example and example not in term.examples:
                term.examples.append(example)
            return name

    def create_term(
        self,
        name: str,
        *,
        alias: str = "",
        example: str = "",
        definition: str = "",
    ) -> str:
        with self._lock:
            if (
                self.max_vocab_size is not None
                and name not in self._terms
                and len(self._terms) >= self.max_vocab_size
            ):
                return self.closest_term(name) or name
            term = self._terms.get(name)
            if term is None:
                term = BaseVocabTerm(name=name, definition=definition)
                self._terms[name] = term
                self._aliases[name] = name
            if alias and alias != name:
                term.aliases.add(alias)
                self._aliases[alias] = name
            if example:
                term.examples.append(example)
            term.count += 1
            return name

    def closest_term(self, token: str) -> Optional[str]:
        with self._lock:
            if not self._terms:
                return None
            return max(self._terms, key=lambda name: term_similarity(token, name))

    def resolver_context(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": self.version,
                "max_vocab_size": self.max_vocab_size,
                "is_full": self.is_full(),
                "terms": [
                    self._terms[name].to_dict()
                    for name in sorted(self._terms)
                ],
            }

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": self.version,
                "max_vocab_size": self.max_vocab_size,
                "terms": [
                    self._terms[name].to_dict()
                    for name in sorted(self._terms)
                ],
            }


# Backward compatibility alias: BaseVocabulary was previously the dynamic class.
# Now BaseVocabulary is a Protocol, but we keep this alias for gradual migration.
BaseVocabularyImpl = DynamicVocabulary


def _normalize_max_vocab_size(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    return max(1, int(value))


def _max_vocab_size_from_data(
    value: Any,
    default: Optional[int],
) -> Optional[int]:
    if value is None:
        return default
    return _normalize_max_vocab_size(int(value))
