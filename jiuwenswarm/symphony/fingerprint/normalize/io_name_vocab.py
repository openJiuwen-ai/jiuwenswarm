"""Dynamic vocabulary for normalizing Skill input/output names.

This module provides specialized vocabulary classes for normalizing
I/O names (like 'query', 'paper', 'summary'). It builds on the
shared vocabulary infrastructure from vocabulary.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Protocol, Union

from jiuwenswarm.symphony.fingerprint.normalize.vocabulary import (
    BaseCandidate,
    BaseResolution,
    BaseVocabTerm,
    DynamicVocabulary,
    _max_vocab_size_from_data,
)
from jiuwenswarm.symphony.fingerprint.models import NormalizationConfig


@dataclass(frozen=True)
class IONameCandidate(BaseCandidate):
    """Candidate for I/O name vocabulary resolution with direction and type context."""

    direction: str = ""
    data_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data["direction"] = self.direction
        data["type"] = self.data_type
        return data


# IONameResolution is identical to BaseResolution, use it as alias.
IONameResolution = BaseResolution


class IONameResolver(Protocol):
    """Async resolver protocol for unseen io_name_vocab terms."""

    async def resolve_async(
        self,
        candidates_by_skill: List[List[IONameCandidate]],
        vocabulary: "IONameVocabulary",
    ) -> Dict[str, IONameResolution]:
        ...


# IONameVocabTerm is identical to BaseVocabTerm, use it as alias.
IONameVocabTerm = BaseVocabTerm


class IONameVocabulary(DynamicVocabulary):
    """Mutable io_name_vocab with bounded canonical terms and unbounded aliases.

    This vocabulary manages I/O name terms used for graph linking.
    """

    @classmethod
    def from_config(cls, config: NormalizationConfig) -> "IONameVocabulary":
        """Build vocabulary from NormalizationConfig."""
        return cls(
            version=config.io_name_vocab_version,
            max_vocab_size=config.max_vocab_size,
            terms=[],
        )

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        config: NormalizationConfig,
    ) -> "IONameVocabulary":
        """Build vocabulary from a dictionary with config defaults."""
        return cls(
            version=str(data.get("version") or config.io_name_vocab_version),
            max_vocab_size=_max_vocab_size_from_data(
                data.get("max_vocab_size"),
                config.max_vocab_size,
            ),
            terms=[
                IONameVocabTerm.from_dict(item)
                for item in data.get("terms", [])
                if isinstance(item, dict)
            ],
        )

    @classmethod
    def load(
        cls,
        path: Union[Path, str],
        config: NormalizationConfig,
    ) -> "IONameVocabulary":
        """Load vocabulary from a JSON file with config defaults."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data, config)
