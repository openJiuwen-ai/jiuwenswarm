"""Static vocabulary for DataType (formats/carriers).

This module defines the controlled vocabulary for data types used in
Skill I/O specifications, such as text, markdown, json, pdf, etc.
The default vocabulary is loaded from an embedded YAML resource so the
controlled vocabulary can be reviewed and maintained in one place.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional

import yaml

from jiuwenswarm.symphony.fingerprint.normalize.vocabulary import StaticVocabulary
from jiuwenswarm.symphony.fingerprint.utils import normalize_token

_VOCAB_RESOURCE_PACKAGE = __package__ or "jiuwenswarm.symphony.fingerprint.normalize"
_VOCAB_RESOURCE_NAME = "data_type_vocab.yaml"
_INFERENCE_MIN_SCORE = 0.65
_INFERENCE_TIE_EPSILON = 0.05


@dataclass(frozen=True)
class DataTypeResolution:
    """Resolution result for DataType vocabulary lookup."""

    normalized_value: Optional[str]
    method: str
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "normalized_value": self.normalized_value,
            "method": self.method,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class DataTypeInference:
    """Semantic inference result for DataType lookup."""

    data_type: str
    reason: str
    confidence: float
    evidence: Dict[str, Any]
    method: str = "semantic_score"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "data_type": self.data_type,
            "reason": self.reason,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "method": self.method,
        }


@dataclass(frozen=True)
class _DataTypeDefaults:
    version: str
    vocab: FrozenSet[str]
    aliases: Dict[str, str]
    hierarchy: Dict[str, FrozenSet[str]]
    content_carrier_types: FrozenSet[str]
    markup_text_types: FrozenSet[str]
    structured_text_types: FrozenSet[str]
    type_hints: Dict[str, Dict[str, Any]]


class DataTypeVocabulary(StaticVocabulary):
    """Static vocabulary for DataType formats/carriers.

    This vocabulary is immutable and predefined, containing common
    data formats like text, markdown, json, pdf, etc. Default terms,
    aliases, hierarchy, traits, and semantic inference hints are loaded
    from the adjacent ``data_type_vocab.yaml`` resource.

    Example usage:
        vocab = DataTypeVocabulary()  # Uses defaults
        vocab.resolve("natural_language")  # Returns "text"
        vocab.resolve("pdf")  # Returns "pdf"
        vocab.resolve("unknown_format")  # Returns normalized_value=None
    """

    VERSION: ClassVar[str] = "data-type-v1"
    DEFAULT_VOCAB: ClassVar[FrozenSet[str]]
    DEFAULT_ALIASES: ClassVar[Dict[str, str]]
    TYPE_HIERARCHY: ClassVar[Dict[str, FrozenSet[str]]]
    CONTENT_CARRIER_TYPES: ClassVar[FrozenSet[str]]
    MARKUP_TEXT_TYPES: ClassVar[FrozenSet[str]]
    STRUCTURED_TEXT_TYPES: ClassVar[FrozenSet[str]]
    TYPE_HINTS: ClassVar[Dict[str, Dict[str, Any]]]
    _DEFAULTS: ClassVar[_DataTypeDefaults]

    @classmethod
    def install_defaults(cls, defaults: _DataTypeDefaults) -> None:
        cls.VERSION = defaults.version
        cls._DEFAULTS = defaults
        cls.DEFAULT_VOCAB = defaults.vocab
        cls.DEFAULT_ALIASES = defaults.aliases
        cls.TYPE_HIERARCHY = defaults.hierarchy
        cls.CONTENT_CARRIER_TYPES = defaults.content_carrier_types
        cls.MARKUP_TEXT_TYPES = defaults.markup_text_types
        cls.STRUCTURED_TEXT_TYPES = defaults.structured_text_types
        cls.TYPE_HINTS = defaults.type_hints

    def __init__(
        self,
        version: Optional[str] = None,
        vocab: Optional[FrozenSet[str]] = None,
        aliases: Optional[Dict[str, str]] = None,
    ) -> None:
        """Initialize DataTypeVocabulary with optional custom parameters.

        Args:
            version: Vocabulary version string. Defaults to VERSION.
            vocab: Set of canonical vocabulary terms. Defaults to DEFAULT_VOCAB.
            aliases: Alias mappings. Defaults to DEFAULT_ALIASES.
        """
        base_aliases = aliases or self.DEFAULT_ALIASES
        normalized_aliases = dict(base_aliases)
        for alias, target in base_aliases.items():
            token = normalize_token(alias)
            if token:
                existing = normalized_aliases.get(token)
                if existing is not None and existing != target:
                    raise ValueError(
                        f"DataType alias '{alias}' normalizes to '{token}', "
                        f"which already maps to '{existing}', not '{target}'."
                    )
                normalized_aliases[token] = target

        super().__init__(
            version=version or self.VERSION,
            vocab=vocab or self.DEFAULT_VOCAB,
            aliases=normalized_aliases,
        )

    @classmethod
    def default(cls) -> "DataTypeVocabulary":
        """Create the default DataType vocabulary with predefined terms and aliases."""
        return cls(
            version=cls.VERSION,
            vocab=cls.DEFAULT_VOCAB,
            aliases=cls.DEFAULT_ALIASES,
        )

    @classmethod
    def from_config(cls, config: Any) -> "DataTypeVocabulary":
        """Build vocabulary from NormalizationConfig.

        Uses the default vocabulary terms. If config provides custom
        aliases via data_type_aliases, they are merged with defaults.
        """
        aliases = cls.DEFAULT_ALIASES
        if hasattr(config, "data_type_aliases") and config.data_type_aliases:
            aliases = {**cls.DEFAULT_ALIASES, **config.data_type_aliases}
        return cls(
            version=cls.VERSION,
            vocab=cls.DEFAULT_VOCAB,
            aliases=aliases,
        )

    @classmethod
    def with_custom_aliases(
        cls,
        aliases: Dict[str, str],
    ) -> "DataTypeVocabulary":
        """Create a DataType vocabulary with additional custom aliases.

        Custom aliases are merged with the default aliases. If a custom alias
        conflicts with a default alias, the custom alias takes precedence.

        Args:
            aliases: Additional alias mappings to add.

        Returns:
            A new DataTypeVocabulary instance with merged aliases.
        """
        merged_aliases = {**cls.DEFAULT_ALIASES, **aliases}
        return cls(
            version=cls.VERSION,
            vocab=cls.DEFAULT_VOCAB,
            aliases=merged_aliases,
        )

    @property
    def unknown_type(self) -> str:
        """Return the unknown type marker."""
        return "unknown"

    def resolve(self, raw_token: str) -> DataTypeResolution:
        """Resolve a raw token to a canonical vocabulary term.

        Returns a DataTypeResolution with the normalized value, method,
        and confidence score.
        """
        normalized = raw_token.lower().strip()
        token = normalize_token(raw_token)
        if normalized in self.vocab or token in self.vocab:
            return DataTypeResolution(
                normalized_value=normalized if normalized in self.vocab else token,
                method="exact",
                confidence=1.0,
            )
        alias_result = self.aliases.get(normalized) or self.aliases.get(token)
        if alias_result and alias_result in self.vocab:
            return DataTypeResolution(
                normalized_value=alias_result,
                method="alias_map",
                confidence=0.95,
            )
        return DataTypeResolution(
            normalized_value=None,
            method="unknown",
            confidence=0.0,
        )

    def contains(self, token: str) -> bool:
        """Check if a token is in the vocabulary (canonical or alias)."""
        normalized = token.lower().strip()
        normalized_token = normalize_token(token)
        return (
            normalized in self.vocab
            or normalized in self.aliases
            or normalized_token in self.vocab
            or normalized_token in self.aliases
        )

    def infer_from_io_semantics(
        self,
        io_name: str,
        description: str,
    ) -> Optional[DataTypeInference]:
        """Infer a DataType from I/O name and description evidence."""
        name_token = normalize_token(io_name)
        description_token = normalize_token(description)
        name_tokens = _semantic_tokens(io_name)
        description_tokens = _semantic_tokens(description)
        all_tokens = name_tokens | description_tokens

        candidates: List[Dict[str, Any]] = []
        for target in sorted(self.vocab):
            if target == self.unknown_type:
                continue
            score, evidence = _score_inference_candidate(
                target=target,
                aliases=self.aliases,
                hints=self.TYPE_HINTS.get(target, {}),
                name_token=name_token,
                description_token=description_token,
                name_tokens=name_tokens,
                description_tokens=description_tokens,
                all_tokens=all_tokens,
            )
            if score < _INFERENCE_MIN_SCORE:
                continue
            candidates.append(
                {
                    "target": target,
                    "score": score,
                    "evidence": evidence,
                }
            )
        if not candidates:
            return None

        candidates.sort(key=lambda item: (-float(item["score"]), str(item["target"])))
        winner = candidates[0]
        if (
            len(candidates) > 1
            and float(winner["score"]) - float(candidates[1]["score"])
            <= _INFERENCE_TIE_EPSILON
        ):
            return None

        target = str(winner["target"])
        score = float(winner["score"])
        evidence = dict(winner["evidence"])
        evidence["score"] = round(score, 4)
        if len(candidates) > 1:
            evidence["runner_up"] = {
                "data_type": candidates[1]["target"],
                "score": round(float(candidates[1]["score"]), 4),
            }
        confidence = min(0.95, round(0.5 + score / 4.0, 4))
        return DataTypeInference(
            data_type=target,
            reason=f"semantic evidence matched DataType '{target}'",
            confidence=confidence,
            evidence=evidence,
        )

    def is_subtype(self, sub_type: str, super_type: str) -> bool:
        """Check if sub_type is a subtype of super_type in the type hierarchy.

        This method supports transitive hierarchy checks. For example,
        is_subtype("png", "file") returns True because:
        png -> image -> file

        Args:
            sub_type: The potential subtype to check.
            super_type: The potential supertype to check against.

        Returns:
            True if sub_type is a (direct or transitive) subtype of super_type,
            False otherwise.
        """
        if super_type not in self.TYPE_HIERARCHY:
            return False
        children = self.TYPE_HIERARCHY[super_type]
        if sub_type in children:
            return True
        return any(self.is_subtype(sub_type, child) for child in children)

    def can_feed_by_type(self, output_type: str, input_type: str) -> bool:
        """Check if output_type can feed into input_type.

        This implements the type compatibility logic for Skill graph construction:
        1. Unknown types cannot feed
        2. Same types can always feed
        3. Subtypes can feed to their supertypes
        4. Textual carriers can feed generic text inputs
        5. Closely related text formats can interoperate conservatively

        Args:
            output_type: The type of the output artifact.
            input_type: The type of the input parameter.

        Returns:
            True if the output type can satisfy the input type requirement.
        """
        if output_type == "unknown" or input_type == "unknown":
            return False
        if output_type == input_type:
            return True
        if self.is_subtype(output_type, input_type):
            return True
        if input_type == "text" and output_type in self.CONTENT_CARRIER_TYPES:
            return True
        if output_type == "text" and input_type in self.MARKUP_TEXT_TYPES:
            return True
        if output_type in self.MARKUP_TEXT_TYPES and input_type in self.MARKUP_TEXT_TYPES:
            return True
        if (
            output_type in self.STRUCTURED_TEXT_TYPES
            and input_type in self.STRUCTURED_TEXT_TYPES
        ):
            return True
        return False


def _load_default_data_type_defaults() -> _DataTypeDefaults:
    raw = _load_default_vocab_resource()
    version = str(raw.get("version") or DataTypeVocabulary.VERSION)
    types = raw.get("types")
    if not isinstance(types, dict) or not types:
        raise ValueError(
            "DataType vocabulary YAML must define a non-empty 'types' mapping."
        )

    vocab = frozenset(str(name).strip() for name in types if str(name).strip())
    aliases: Dict[str, str] = {}
    hierarchy: Dict[str, set[str]] = {}
    traits: Dict[str, set[str]] = {}
    type_hints: Dict[str, Dict[str, Any]] = {}

    for raw_name, raw_spec in types.items():
        name = str(raw_name).strip()
        if not name:
            continue
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        for alias in _string_list(spec.get("aliases")):
            aliases[alias] = name
        for parent in _string_list(spec.get("parents")):
            if parent not in vocab:
                raise ValueError(
                    f"DataType vocabulary parent '{parent}' for '{name}' is not a canonical type."
                )
            hierarchy.setdefault(parent, set()).add(name)
        for trait in _string_list(spec.get("traits")):
            traits.setdefault(trait, set()).add(name)
        hints = spec.get("hints")
        if hints is not None:
            if not isinstance(hints, dict):
                raise ValueError(
                    f"DataType vocabulary hints for '{name}' must be a mapping."
                )
            type_hints[name] = {
                "name_terms": frozenset(_normalized_items(hints.get("name_terms"))),
                "description_terms": frozenset(
                    _normalized_items(hints.get("description_terms"))
                ),
                "suffixes": tuple(_normalized_suffixes(hints.get("suffixes"))),
                "requires_any": frozenset(_normalized_items(hints.get("requires_any"))),
            }

    return _DataTypeDefaults(
        version=version,
        vocab=vocab,
        aliases=aliases,
        hierarchy={key: frozenset(value) for key, value in hierarchy.items()},
        content_carrier_types=frozenset(traits.get("content_carrier", set())),
        markup_text_types=frozenset(traits.get("markup_text", set())),
        structured_text_types=frozenset(traits.get("structured_text", set())),
        type_hints=type_hints,
    )


def _load_default_vocab_resource() -> Dict[str, Any]:
    resource = resources.files(_VOCAB_RESOURCE_PACKAGE).joinpath(_VOCAB_RESOURCE_NAME)
    data = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("DataType vocabulary YAML must contain a mapping.")
    return data


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        item = str(value).strip()
        return [item] if item else []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalized_items(value: Any) -> List[str]:
    return [normalize_token(item) for item in _string_list(value) if normalize_token(item)]


def _normalized_suffixes(value: Any) -> List[str]:
    suffixes: List[str] = []
    for item in _string_list(value):
        suffix = normalize_token(item)
        if suffix:
            suffixes.append(f"_{suffix}" if not suffix.startswith("_") else suffix)
    return suffixes


def _semantic_tokens(value: str) -> set[str]:
    token = normalize_token(value)
    if not token:
        return set()
    parts = token.split("_")
    tokens = set(parts)
    tokens.add(token)
    for size in (2, 3):
        for index in range(0, max(0, len(parts) - size + 1)):
            tokens.add("_".join(parts[index: index + size]))
    for raw_match in re.findall(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", value):
        raw_token = normalize_token(raw_match)
        if raw_token:
            tokens.add(raw_token)
    return tokens


def _score_inference_candidate(
    *,
    target: str,
    aliases: Dict[str, str],
    hints: Dict[str, Any],
    name_token: str,
    description_token: str,
    name_tokens: set[str],
    description_tokens: set[str],
    all_tokens: set[str],
) -> tuple[float, Dict[str, Any]]:
    required = set(hints.get("requires_any", frozenset()))
    if required and not required.intersection(all_tokens):
        return 0.0, {}

    evidence: Dict[str, Any] = {
        "name_token": name_token,
        "description_token": description_token,
        "matches": {},
    }
    score = 0.0
    matched_terms: set[str] = set()

    def add(amount: float, key: str, values: Iterable[str]) -> None:
        nonlocal score
        clean_values = sorted({value for value in values if value})
        if not clean_values:
            return
        score += amount
        evidence["matches"][key] = clean_values
        matched_terms.update(clean_values)

    if name_token == target:
        add(0.95, "name_exact", [target])
    if target in name_tokens:
        add(0.75, "name_type_term", [target])
    if target in description_tokens:
        add(0.65, "description_type_term", [target])

    alias_tokens = {
        normalize_token(alias)
        for alias, mapped_target in aliases.items()
        if mapped_target == target and normalize_token(alias)
    }
    add(0.85, "name_alias", alias_tokens & name_tokens)
    add(0.65, "description_alias", alias_tokens & description_tokens)

    suffixes = tuple(hints.get("suffixes", ()))
    add(
        0.7,
        "name_suffix",
        [suffix for suffix in suffixes if name_token.endswith(suffix)],
    )

    name_term_matches = set(hints.get("name_terms", frozenset())) & name_tokens
    add(0.45, "name_hint", name_term_matches)

    description_term_matches = (
        set(hints.get("description_terms", frozenset())) & description_tokens
    )
    if description_term_matches:
        amount = min(0.5, 0.25 * len(description_term_matches))
        add(amount, "description_hint", description_term_matches)

    evidence["matched_terms"] = sorted(matched_terms)
    return score, evidence


_DEFAULTS = _load_default_data_type_defaults()
DataTypeVocabulary.install_defaults(_DEFAULTS)
