"""Skill fingerprint extraction.

This module provides a stage-based pipeline for extracting structured
fingerprints from Skill folders containing SKILL.md files.

Stages:
- scan: Discover Skill folders containing SKILL.md entrypoints
- parse: Parse SKILL.md into frontmatter and body
- extract: LLM schema extraction from parsed content
- normalize: Normalize I/O names, types, and identities via vocabularies
- write: Write extraction artifacts to disk
"""

# Models - core data contracts
from jiuwenswarm.symphony.fingerprint.models import (
    ArtifactSpec,
    ExtractedSkillSchema,
    ExtractionDiagnostic,
    NormalizationConfig,
    NormalizationDecision,
    NormalizationResult,
    ParameterSpec,
    RawSkillManifest,
    FingerprintExtractionResult,
    Fingerprint,
    FingerprintType,
    SkillFolder,
    SkillSchemaExtractor,
)

# Stage: scan
from jiuwenswarm.symphony.fingerprint.scan import SkillFolderScanner

# Stage: parse
from jiuwenswarm.symphony.fingerprint.manifest import SkillManifestParser

# Stage: extract
from jiuwenswarm.symphony.fingerprint.extract import (
    LLMSchemaExtractor,
    schema_from_llm_payload,
)

# Stage: normalize
from jiuwenswarm.symphony.fingerprint.normalize import (
    # Metadata normalization
    MetadataNormalizer,
    # Base vocabulary
    BaseCandidate,
    BaseResolution,
    BaseVocabTerm,
    BaseVocabulary,
    term_similarity,
    # I/O name vocabulary
    IONameCandidate,
    IONameResolution,
    IONameResolver,
    IONameVocabTerm,
    IONameVocabulary,
    LLMIONameResolver,
    # Normalizer
    SkillFingerprintNormalizer,
)

# Stage: write
from jiuwenswarm.symphony.fingerprint.artifacts import (
    write_extraction_result,
    write_json_file,
)

# Pipeline
from jiuwenswarm.symphony.fingerprint.pipeline import FingerprintExtractor

# LLM config
from jiuwenswarm.symphony.llm import LLMConfig

__all__ = [
    # Models
    "ArtifactSpec",
    "MetadataNormalizer",
    "ExtractedSkillSchema",
    "ExtractionDiagnostic",
    "NormalizationConfig",
    "NormalizationDecision",
    "NormalizationResult",
    "ParameterSpec",
    "RawSkillManifest",
    "FingerprintExtractionResult",
    "Fingerprint",
    "FingerprintType",
    "SkillFolder",
    "SkillSchemaExtractor",
    # Stage: scan
    "SkillFolderScanner",
    # Stage: parse
    "SkillManifestParser",
    # Stage: extract
    "LLMSchemaExtractor",
    "schema_from_llm_payload",
    # Stage: normalize - base vocabulary
    "BaseCandidate",
    "BaseResolution",
    "BaseVocabTerm",
    "BaseVocabulary",
    "term_similarity",
    # Stage: normalize - I/O name vocabulary
    "IONameCandidate",
    "IONameResolution",
    "IONameResolver",
    "IONameVocabTerm",
    "IONameVocabulary",
    "LLMIONameResolver",
    # Stage: normalize - normalizer
    "SkillFingerprintNormalizer",
    # Stage: write
    "write_extraction_result",
    "write_json_file",
    # Pipeline
    "FingerprintExtractor",
    # LLM config
    "LLMConfig",
]
