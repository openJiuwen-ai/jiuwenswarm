"""Input and output field normalization."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple, Union

from jiuwenswarm.symphony.fingerprint.models import (
    ArtifactSpec,
    ExtractionDiagnostic,
    NormalizationConfig,
    NormalizationDecision,
    ParameterSpec,
    RawSkillManifest,
)
from jiuwenswarm.symphony.fingerprint.normalize.data_types import (
    DataTypeNormalizationContext,
    DataTypeNormalizer,
)
from jiuwenswarm.symphony.fingerprint.normalize.decisions import (
    NormalizationDecisionRecorder,
)
from jiuwenswarm.symphony.fingerprint.normalize.io_names import (
    IONameNormalizationContext,
    IONameNormalizer,
)
from jiuwenswarm.symphony.fingerprint.utils import to_dict


class SchemaFieldNormalizer:
    """Normalize extracted input/output fields into fingerprint specs."""

    def __init__(
        self,
        config: NormalizationConfig,
        io_names: IONameNormalizer,
        data_types: DataTypeNormalizer,
        recorder: NormalizationDecisionRecorder,
    ) -> None:
        self.config = config
        self.io_names = io_names
        self.data_types = data_types
        self.recorder = recorder

    async def normalize_inputs(
        self,
        raw_inputs: List[Union[ParameterSpec, Dict[str, Any]]],
        manifest: RawSkillManifest,
        skill_id: str,
        diagnostics: List[ExtractionDiagnostic],
        decisions: List[NormalizationDecision],
    ) -> List[ParameterSpec]:
        if not raw_inputs:
            return self._default_inputs(manifest, skill_id, diagnostics, decisions)

        inputs: List[ParameterSpec] = []
        for raw in raw_inputs:
            data = to_dict(raw)
            raw_type = str(data.get("type") or self.config.default_input_type)
            raw_name = str(data.get("name") or self.config.default_input_name)
            description = str(data.get("description") or "")
            name = await self.io_names.normalize(
                raw_name,
                IONameNormalizationContext(
                    raw_type=raw_type,
                    description=description,
                    manifest=manifest,
                    skill_id=skill_id,
                    direction="input",
                    diagnostics=diagnostics,
                    decisions=decisions,
                ),
            )
            if name is None:
                continue
            data_type = self.data_types.normalize(
                raw_type,
                DataTypeNormalizationContext(
                    io_name=name,
                    description=description,
                    manifest=manifest,
                    skill_id=skill_id,
                    direction="input",
                    diagnostics=diagnostics,
                    decisions=decisions,
                ),
            )
            inputs.append(
                ParameterSpec(
                    name=name,
                    type=data_type,
                    required=bool(data.get("required", True)),
                    description=description,
                    default=data.get("default"),
                )
            )
        return self._deduplicate_inputs(inputs, manifest, skill_id, diagnostics)

    async def normalize_outputs(
        self,
        raw_outputs: List[Union[ArtifactSpec, Dict[str, Any]]],
        manifest: RawSkillManifest,
        skill_id: str,
        diagnostics: List[ExtractionDiagnostic],
        decisions: List[NormalizationDecision],
    ) -> List[ArtifactSpec]:
        if not raw_outputs:
            return self._default_outputs(manifest, skill_id, diagnostics, decisions)

        outputs: List[ArtifactSpec] = []
        for raw in raw_outputs:
            data = to_dict(raw)
            raw_type = str(data.get("type") or self.config.unknown_type)
            raw_name = str(data.get("name") or self.config.default_output_name)
            description = str(data.get("description") or "")
            name = await self.io_names.normalize(
                raw_name,
                IONameNormalizationContext(
                    raw_type=raw_type,
                    description=description,
                    manifest=manifest,
                    skill_id=skill_id,
                    direction="output",
                    diagnostics=diagnostics,
                    decisions=decisions,
                ),
            )
            if name is None:
                continue
            data_type = self.data_types.normalize(
                raw_type,
                DataTypeNormalizationContext(
                    io_name=name,
                    description=description,
                    manifest=manifest,
                    skill_id=skill_id,
                    direction="output",
                    diagnostics=diagnostics,
                    decisions=decisions,
                ),
            )
            outputs.append(
                ArtifactSpec(
                    name=name,
                    type=data_type,
                    description=description,
                )
            )
        return self._deduplicate_outputs(outputs, manifest, skill_id, diagnostics)

    def _default_inputs(
        self,
        manifest: RawSkillManifest,
        skill_id: str,
        diagnostics: List[ExtractionDiagnostic],
        decisions: List[NormalizationDecision],
    ) -> List[ParameterSpec]:
        diagnostics.append(
            ExtractionDiagnostic(
                stage="normalization",
                severity="warning",
                code="default_input_created",
                message="inputs missing; created default text input",
                skill_id=skill_id,
                path=str(manifest.folder.path),
            )
        )
        self.recorder.record(
            decisions,
            skill_id=skill_id,
            path=str(manifest.folder.path),
            direction="input",
            field="name",
            raw_value=self.config.default_input_name,
            token=self.config.default_input_name,
            normalized_value=self.config.default_input_name,
            method="default",
            vocab="io_name_vocab",
            vocab_version=self.config.io_name_vocab_version,
            confidence=1.0,
        )
        self.recorder.record(
            decisions,
            skill_id=skill_id,
            path=str(manifest.folder.path),
            direction="input",
            field="type",
            raw_value=self.config.default_input_type,
            token=self.config.default_input_type,
            normalized_value=self.config.default_input_type,
            method="default",
            vocab="data_type_vocab",
            vocab_version=self.data_types.vocabulary.version,
            confidence=1.0,
        )
        return [
            ParameterSpec(
                name=self.config.default_input_name,
                type=self.config.default_input_type,
                required=True,
                description="Default text input",
                default=None,
            )
        ]

    def _default_outputs(
        self,
        manifest: RawSkillManifest,
        skill_id: str,
        diagnostics: List[ExtractionDiagnostic],
        decisions: List[NormalizationDecision],
    ) -> List[ArtifactSpec]:
        diagnostics.append(
            ExtractionDiagnostic(
                stage="normalization",
                severity="warning",
                code="unknown_output_created",
                message="outputs missing; created unknown result output",
                skill_id=skill_id,
                path=str(manifest.folder.path),
            )
        )
        self.recorder.record(
            decisions,
            skill_id=skill_id,
            path=str(manifest.folder.path),
            direction="output",
            field="name",
            raw_value=self.config.default_output_name,
            token=self.config.default_output_name,
            normalized_value=self.config.default_output_name,
            method="default",
            vocab="io_name_vocab",
            vocab_version=self.config.io_name_vocab_version,
            confidence=1.0,
        )
        self.recorder.record(
            decisions,
            skill_id=skill_id,
            path=str(manifest.folder.path),
            direction="output",
            field="type",
            raw_value=self.config.unknown_type,
            token=self.config.unknown_type,
            normalized_value=self.config.unknown_type,
            method="default_unknown",
            vocab="data_type_vocab",
            vocab_version=self.data_types.vocabulary.version,
            confidence=0.0,
        )
        return [
            ArtifactSpec(
                name=self.config.default_output_name,
                type=self.config.unknown_type,
                description="Unknown output",
            )
        ]

    def _deduplicate_inputs(
        self,
        inputs: List[ParameterSpec],
        manifest: RawSkillManifest,
        skill_id: str,
        diagnostics: List[ExtractionDiagnostic],
    ) -> List[ParameterSpec]:
        merged: List[ParameterSpec] = []
        by_name: Dict[str, int] = {}
        for item in inputs:
            existing_index = by_name.get(item.name)
            if existing_index is None:
                by_name[item.name] = len(merged)
                merged.append(item)
                continue

            existing = merged[existing_index]
            merged_item, details = self._merge_input(existing, item)
            merged[existing_index] = merged_item
            diagnostics.append(
                ExtractionDiagnostic(
                    stage="normalization",
                    severity="warning",
                    code="duplicate_input_merged",
                    message="duplicate normalized input name was merged",
                    skill_id=skill_id,
                    path=str(manifest.folder.path),
                    details={
                        "name": item.name,
                        "merged_type": merged_item.type,
                        **details,
                    },
                )
            )
        return merged

    def _deduplicate_outputs(
        self,
        outputs: List[ArtifactSpec],
        manifest: RawSkillManifest,
        skill_id: str,
        diagnostics: List[ExtractionDiagnostic],
    ) -> List[ArtifactSpec]:
        merged: List[ArtifactSpec] = []
        by_name: Dict[str, int] = {}
        for item in outputs:
            existing_index = by_name.get(item.name)
            if existing_index is None:
                by_name[item.name] = len(merged)
                merged.append(item)
                continue

            existing = merged[existing_index]
            merged_item, details = self._merge_output(existing, item)
            merged[existing_index] = merged_item
            diagnostics.append(
                ExtractionDiagnostic(
                    stage="normalization",
                    severity="warning",
                    code="duplicate_output_merged",
                    message="duplicate normalized output name was merged",
                    skill_id=skill_id,
                    path=str(manifest.folder.path),
                    details={
                        "name": item.name,
                        "merged_type": merged_item.type,
                        **details,
                    },
                )
            )
        return merged

    def _merge_input(
        self,
        existing: ParameterSpec,
        incoming: ParameterSpec,
    ) -> Tuple[ParameterSpec, Dict[str, Any]]:
        merged_type, details = self._merge_type(existing.type, incoming.type)
        default, default_conflict = self._merge_optional_value(
            existing.default,
            incoming.default,
        )
        details.update(
            {
                "required_values": [existing.required, incoming.required],
                "default_conflict": default_conflict,
            }
        )
        return (
            ParameterSpec(
                name=existing.name,
                type=merged_type,
                required=existing.required or incoming.required,
                description=self._merge_description(
                    existing.description,
                    incoming.description,
                ),
                default=default,
            ),
            details,
        )

    def _merge_output(
        self,
        existing: ArtifactSpec,
        incoming: ArtifactSpec,
    ) -> Tuple[ArtifactSpec, Dict[str, Any]]:
        merged_type, details = self._merge_type(existing.type, incoming.type)
        return (
            ArtifactSpec(
                name=existing.name,
                type=merged_type,
                description=self._merge_description(
                    existing.description,
                    incoming.description,
                ),
            ),
            details,
        )

    def _merge_type(self, existing: str, incoming: str) -> Tuple[str, Dict[str, Any]]:
        if existing == incoming:
            return existing, {"type_conflict": False, "type_values": [existing]}
        if existing == self.config.unknown_type:
            return incoming, {
                "type_conflict": True,
                "type_values": [existing, incoming],
            }
        return existing, {
            "type_conflict": True,
            "type_values": [existing, incoming],
        }

    @staticmethod
    def _merge_optional_value(
        existing: Any,
        incoming: Any,
    ) -> Tuple[Any, bool]:
        if existing in (None, ""):
            return incoming, False
        if incoming in (None, "") or incoming == existing:
            return existing, False
        return existing, True

    @staticmethod
    def _merge_description(existing: str, incoming: str) -> str:
        parts: List[str] = []
        seen: set[str] = set()
        for value in [existing, incoming]:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            parts.append(text)
            seen.add(text)
        return " ".join(parts)
