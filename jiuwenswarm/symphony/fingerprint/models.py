# -*- coding: utf-8 -*-
"""Data contracts for Skill fingerprint extraction."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    TypeAlias,
    Union,
    cast,
)


FingerprintType: TypeAlias = Literal["agent", "skill"]


@dataclass(frozen=True)
class SkillFolder:
    """A discovered Skill folder with a SKILL.md entrypoint."""

    id_hint: str
    path: Path
    entry: Path
    relative_path: str


@dataclass(frozen=True)
class ExtractionDiagnostic:
    """Structured diagnostic emitted during fingerprint extraction."""

    stage: str
    severity: str
    code: str
    message: str
    skill_id: Optional[str] = None
    path: Optional[str] = None
    details: Dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "path": self.path,
            "stage": self.stage,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class RawSkillManifest:
    """Parsed SKILL.md content before LLM schema extraction."""

    folder: SkillFolder
    frontmatter: Dict[str, Any]
    body: str
    body_sha256: str
    diagnostics: List[ExtractionDiagnostic] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class ParameterSpec:
    """A Skill input parameter."""

    name: str
    type: str
    required: bool = True
    description: str = ""
    default: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
            "default": self.default,
        }


@dataclass(frozen=True)
class ArtifactSpec:
    """A Skill output artifact."""

    name: str
    type: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
        }


@dataclass(frozen=True)
class ExtractedSkillSchema:
    """LLM-extracted candidate schema before deterministic normalization."""

    description: str = ""
    inputs: List[Union[ParameterSpec, Dict[str, Any]]] = dataclass_field(
        default_factory=list
    )
    outputs: List[Union[ArtifactSpec, Dict[str, Any]]] = dataclass_field(
        default_factory=list
    )
    confidence: Optional[float] = None
    warnings: List[str] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class Fingerprint:
    """Normalized static description of an Agent or Skill."""

    type: FingerprintType
    id: str
    name: str
    description: str
    version: str
    inputs: List[ParameterSpec] = dataclass_field(default_factory=list)
    outputs: List[ArtifactSpec] = dataclass_field(default_factory=list)
    static_data: Dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in {"agent", "skill"}:
            raise ValueError("Fingerprint type must be 'agent' or 'skill'.")
        for name, value in (
            ("id", self.id),
            ("name", self.name),
            ("version", self.version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Fingerprint {name} must be a non-empty string.")
        if any(not isinstance(item, ParameterSpec) for item in self.inputs):
            raise ValueError("Fingerprint inputs must contain ParameterSpec objects.")
        if any(not isinstance(item, ArtifactSpec) for item in self.outputs):
            raise ValueError("Fingerprint outputs must contain ArtifactSpec objects.")
        if not isinstance(self.static_data, dict):
            raise ValueError("Fingerprint static_data must be a dictionary.")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": [item.to_dict() for item in self.outputs],
            "static_data": self.static_data,
        }

    def graph_identity_dict(self) -> Dict[str, Any]:
        """Return fields that define graph identity and matching behavior."""

        return {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": [item.to_dict() for item in self.outputs],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Fingerprint":
        if not isinstance(payload, dict):
            raise ValueError("Fingerprint payload must be a dictionary.")
        raw_type = str(payload.get("type") or "skill")
        if raw_type not in {"agent", "skill"}:
            raise ValueError("Fingerprint type must be 'agent' or 'skill'.")
        raw_inputs = payload.get("inputs", [])
        raw_outputs = payload.get("outputs", [])
        raw_static_data = payload.get("static_data", {})
        if not isinstance(raw_inputs, list):
            raise ValueError("Fingerprint inputs must be a list.")
        if any(not isinstance(item, dict) for item in raw_inputs):
            raise ValueError("Fingerprint inputs must contain dictionaries.")
        if not isinstance(raw_outputs, list):
            raise ValueError("Fingerprint outputs must be a list.")
        if any(not isinstance(item, dict) for item in raw_outputs):
            raise ValueError("Fingerprint outputs must contain dictionaries.")
        if not isinstance(raw_static_data, dict):
            raise ValueError("Fingerprint static_data must be a dictionary.")
        fingerprint_type = cast(FingerprintType, raw_type)
        return cls(
            type=fingerprint_type,
            id=payload.get("id", ""),
            name=payload.get("name", ""),
            description=str(payload.get("description") or ""),
            version=payload.get("version", "1.0.0"),
            inputs=[
                ParameterSpec(
                    name=str(item.get("name") or "input"),
                    type=str(item.get("type") or "text"),
                    required=bool(item.get("required", True)),
                    description=str(item.get("description") or ""),
                    default=item.get("default"),
                )
                for item in raw_inputs
            ],
            outputs=[
                ArtifactSpec(
                    name=str(item.get("name") or "result"),
                    type=str(item.get("type") or "unknown"),
                    description=str(item.get("description") or ""),
                )
                for item in raw_outputs
            ],
            static_data=dict(raw_static_data),
        )


@dataclass(frozen=True)
class NormalizationConfig:
    """Configuration and vocabularies used by the deterministic normalizer.

    This object owns the normalizer defaults, vocabulary versions, and optional
    alias maps. `Fingerprint` keeps only normalized `name` and `type`
    values; the evidence for each choice is stored in `NormalizationDecision`.

    `name` and `type` have different roles:
    - `name` is the I/O semantic term used by graph construction.
    - `type` is the artifact kind or concrete format, such as text, pdf, csv,
      markdown, image, audio, or video. Media resources should keep the media
      artifact type even when carried as a URL, local path, file reference,
      base64 string, or bytes string.
    """

    # Dynamic vocabulary version for normalized I/O semantic names.
    io_name_vocab_version: str = "io-name-vocab-v1"

    # Optional soft cap for canonical I/O name terms. None means the
    # vocabulary can grow with the observed Skill corpus instead of forcing
    # unrelated runtime semantics into broad buckets.
    max_vocab_size: Optional[int] = None

    # Warn when a newly-created I/O name is very close to an existing term.
    # This is review guidance only; it does not force a merge.
    possible_duplicate_name_similarity_threshold: float = 0.86

    # Default Skill version when frontmatter does not declare one.
    default_version: str = "1.0.0"

    # Default input created when the extractor returns no inputs.
    default_input_name: str = "input"

    # Default type for generated fallback inputs.
    default_input_type: str = "text"

    # Default output created when the extractor returns no outputs.
    default_output_name: str = "result"

    # Type used when a data format cannot be recognized.
    unknown_type: str = "unknown"


@dataclass(frozen=True)
class MetadataNormalizationResult:
    """Result of metadata normalization: id, name, version, description."""

    id: str
    name: str
    description: str
    version: str


@dataclass(frozen=True)
class NormalizationDecision:
    """Trace record for a normalization choice kept outside fingerprints."""

    skill_id: str
    path: Optional[str]
    direction: str
    field: str
    raw_value: str
    token: str
    normalized_value: str
    method: str
    vocab: str
    vocab_version: str
    confidence: float
    details: Dict[str, Any] = dataclass_field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "path": self.path,
            "direction": self.direction,
            "field": self.field,
            "raw_value": self.raw_value,
            "token": self.token,
            "normalized_value": self.normalized_value,
            "method": self.method,
            "vocab": self.vocab,
            "vocab_version": self.vocab_version,
            "confidence": self.confidence,
            "details": self.details,
        }


@dataclass(frozen=True)
class NormalizationResult:
    fingerprint: Fingerprint
    diagnostics: List[ExtractionDiagnostic]
    decisions: List[NormalizationDecision] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class FingerprintExtractionResult:
    fingerprints: List[Fingerprint]
    diagnostics: List[ExtractionDiagnostic]
    normalization_decisions: List[NormalizationDecision] = dataclass_field(
        default_factory=list
    )
    io_name_vocab: Dict[str, Any] = dataclass_field(default_factory=dict)
    llm_token_usage: Dict[str, Any] = dataclass_field(default_factory=dict)
    folders: List[SkillFolder] = dataclass_field(default_factory=list)
    current_hashes: Dict[str, str] = dataclass_field(default_factory=dict)
    removed_paths: set[str] = dataclass_field(default_factory=set)
    fingerprints_by_path: Dict[str, Fingerprint] = dataclass_field(
        default_factory=dict
    )
    reused_count: int = 0
    extracted_count: int = 0


class SkillSchemaExtractor(Protocol):
    """Protocol for LLM-backed schema extractors."""

    async def extract(self, manifest: RawSkillManifest) -> ExtractedSkillSchema:
        ...
