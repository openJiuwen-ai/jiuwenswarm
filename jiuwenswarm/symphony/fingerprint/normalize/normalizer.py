"""Deterministic normalization for LLM-extracted Skill schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from jiuwenswarm.symphony.fingerprint.models import (
    ExtractedSkillSchema,
    ExtractionDiagnostic,
    NormalizationConfig,
    NormalizationDecision,
    NormalizationResult,
    RawSkillManifest,
    Fingerprint,
)
from jiuwenswarm.symphony.fingerprint.normalize.data_type_vocab import DataTypeVocabulary
from jiuwenswarm.symphony.fingerprint.normalize.data_types import DataTypeNormalizer
from jiuwenswarm.symphony.fingerprint.normalize.decisions import (
    NormalizationDecisionRecorder,
)
from jiuwenswarm.symphony.fingerprint.normalize.fields import SchemaFieldNormalizer
from jiuwenswarm.symphony.fingerprint.normalize.io_name_vocab import (
    IONameCandidate,
    IONameResolver,
    IONameVocabulary,
)
from jiuwenswarm.symphony.fingerprint.normalize.io_names import IONameNormalizer
from jiuwenswarm.symphony.fingerprint.normalize.metadata_normalizer import (
    MetadataNormalizer,
)

NormalizationItem = Tuple[RawSkillManifest, ExtractedSkillSchema]


@dataclass
class _NormalizationContext:
    manifest: RawSkillManifest
    extracted: ExtractedSkillSchema
    diagnostics: List[ExtractionDiagnostic]
    decisions: List[NormalizationDecision]
    skill_id: str
    name: str
    description: str
    version: str
    io_name_candidates: List[IONameCandidate]


class SkillFingerprintNormalizer:
    """Convert extracted Skill schemas into unified fingerprints."""

    def __init__(
        self,
        config: Optional[NormalizationConfig] = None,
        io_name_vocabulary: Optional[IONameVocabulary] = None,
        io_name_resolver: Optional[IONameResolver] = None,
        metadata_normalizer: Optional[MetadataNormalizer] = None,
    ) -> None:
        self.config = config or NormalizationConfig()
        self.data_type_vocabulary = DataTypeVocabulary.from_config(self.config)
        self._io_name_vocabulary = (
            io_name_vocabulary
            or IONameVocabulary.from_config(self.config)
        )
        if io_name_resolver is None:
            raise ValueError("SkillFingerprintNormalizer requires io_name_resolver.")
        self.io_name_resolver = io_name_resolver
        self.metadata_normalizer = metadata_normalizer or MetadataNormalizer(self.config)
        self.decision_recorder = NormalizationDecisionRecorder()
        self.data_type_normalizer = DataTypeNormalizer(
            self.config,
            self.data_type_vocabulary,
            self.decision_recorder,
        )
        self.io_name_normalizer = IONameNormalizer(
            self.config,
            self._io_name_vocabulary,
            self.io_name_resolver,
            self.decision_recorder,
        )
        self.field_normalizer = SchemaFieldNormalizer(
            self.config,
            self.io_name_normalizer,
            self.data_type_normalizer,
            self.decision_recorder,
        )

    @property
    def io_name_vocabulary(self) -> IONameVocabulary:
        return self._io_name_vocabulary

    @io_name_vocabulary.setter
    def io_name_vocabulary(self, vocabulary: IONameVocabulary) -> None:
        self._io_name_vocabulary = vocabulary
        self.io_name_normalizer.vocabulary = vocabulary

    async def normalize(
        self,
        items: List[NormalizationItem],
    ) -> List[NormalizationResult]:
        contexts = self._prepare_contexts(items)
        await self._prime_io_name_resolutions(contexts)
        return [await self._normalize_context(context) for context in contexts]

    async def normalize_single(
        self,
        manifest: RawSkillManifest,
        extracted: ExtractedSkillSchema,
    ) -> NormalizationResult:
        return (await self.normalize([(manifest, extracted)]))[0]

    def _prepare_contexts(
        self,
        items: List[NormalizationItem],
    ) -> List[_NormalizationContext]:
        contexts: List[_NormalizationContext] = []
        for manifest, extracted in items:
            identity = self.metadata_normalizer.normalize(manifest, extracted)
            candidates = self.io_name_normalizer.collect_candidates(
                manifest,
                extracted,
                identity.id,
            )
            contexts.append(
                _NormalizationContext(
                    manifest=manifest,
                    extracted=extracted,
                    diagnostics=list(manifest.diagnostics),
                    decisions=[],
                    skill_id=identity.id,
                    name=identity.name,
                    description=identity.description,
                    version=identity.version,
                    io_name_candidates=candidates,
                )
            )
        return contexts

    async def _prime_io_name_resolutions(
        self,
        contexts: List[_NormalizationContext],
    ) -> None:
        batch_size = max(1, int(getattr(self.io_name_resolver, "batch_size", 1)))
        for start in range(0, len(contexts), batch_size):
            batch = contexts[start: start + batch_size]
            await self.io_name_normalizer.prime_resolutions(
                [context.io_name_candidates for context in batch]
            )

    async def _normalize_context(
        self,
        context: _NormalizationContext,
    ) -> NormalizationResult:
        inputs = await self.field_normalizer.normalize_inputs(
            context.extracted.inputs,
            context.manifest,
            context.skill_id,
            context.diagnostics,
            context.decisions,
        )
        outputs = await self.field_normalizer.normalize_outputs(
            context.extracted.outputs,
            context.manifest,
            context.skill_id,
            context.diagnostics,
            context.decisions,
        )

        fingerprint = Fingerprint(
            type="skill",
            id=context.skill_id,
            name=context.name,
            description=context.description,
            version=context.version,
            inputs=inputs,
            outputs=outputs,
        )
        self.data_type_normalizer.validate(
            fingerprint,
            context.manifest,
            context.diagnostics,
        )
        return NormalizationResult(
            fingerprint=fingerprint,
            diagnostics=context.diagnostics,
            decisions=context.decisions,
        )
