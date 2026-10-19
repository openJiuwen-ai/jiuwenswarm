"""DataType normalization for Skill fingerprint fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from jiuwenswarm.symphony.fingerprint.models import (
    ExtractionDiagnostic,
    NormalizationConfig,
    NormalizationDecision,
    RawSkillManifest,
    Fingerprint,
)
from jiuwenswarm.symphony.fingerprint.normalize.data_type_vocab import (
    DataTypeInference,
    DataTypeVocabulary,
)
from jiuwenswarm.symphony.fingerprint.normalize.decisions import (
    NormalizationDecisionRecorder,
)
from jiuwenswarm.symphony.fingerprint.utils import normalize_token

_MEDIA_REFERENCE_CARRIER_TYPES = frozenset({"file", "path", "text", "url"})
_MEDIA_ARTIFACT_NAME_TERMS = {
    "image": frozenset(
        {
            "image",
            "imageurl",
            "photo",
            "picture",
            "poster",
            "screenshot",
        }
    ),
    "audio": frozenset(
        {
            "audio",
            "audio_file",
            "audio_path",
            "audio_url",
            "audiourl",
            "m4a",
            "mp3",
            "music",
            "recording",
            "sound",
            "speech",
            "track",
            "voice",
            "wav",
        }
    ),
    "video": frozenset(
        {
            "animation",
            "clip",
            "footage",
            "mov",
            "movie",
            "mp4",
            "video",
            "video_file",
            "video_path",
            "video_url",
            "videourl",
            "webm",
        }
    ),
}
_NON_ARTIFACT_MEDIA_NAME_TERMS = frozenset(
    {
        "alt",
        "analysis",
        "caption",
        "description",
        "language",
        "metadata",
        "prompt",
        "query",
        "subtitle",
        "text",
        "transcript",
    }
)
_TEXT_MEDIA_REFERENCE_DESCRIPTION_TERMS = frozenset(
    {"base64", "bytes", "file", "local", "path", "url"}
)


@dataclass(frozen=True)
class DataTypeNormalizationContext:
    io_name: str
    description: str
    manifest: RawSkillManifest
    skill_id: str
    direction: str
    diagnostics: List[ExtractionDiagnostic]
    decisions: List[NormalizationDecision]


class DataTypeNormalizer:
    """Normalize artifact kind/concrete format types against DataTypeVocabulary."""

    def __init__(
        self,
        config: NormalizationConfig,
        vocabulary: DataTypeVocabulary,
        recorder: NormalizationDecisionRecorder,
    ) -> None:
        self.config = config
        self.vocabulary = vocabulary
        self.recorder = recorder

    def normalize(
        self,
        raw_type: str,
        context: DataTypeNormalizationContext,
    ) -> str:
        token = normalize_token(raw_type)
        resolution = self.vocabulary.resolve(token)
        if resolution.normalized_value == self.vocabulary.unknown_type:
            inferred = self.vocabulary.infer_from_io_semantics(
                context.io_name,
                context.description,
            )
            if inferred is not None:
                context.diagnostics.append(
                    ExtractionDiagnostic(
                        stage="normalization",
                        severity="info",
                        code="unknown_type_inferred",
                        message="unknown artifact type inferred from I/O semantics",
                        skill_id=context.skill_id,
                        path=str(context.manifest.folder.path),
                        details={
                            "original_type": raw_type,
                            "normalized_token": token,
                            "inferred_type": inferred.data_type,
                            "io_name": context.io_name,
                            "method": inferred.method,
                            "confidence": inferred.confidence,
                            "reason": inferred.reason,
                            "evidence": inferred.evidence,
                        },
                    )
                )
                self._record_inference(
                    context,
                    raw_type,
                    token,
                    inferred,
                )
                return inferred.data_type
        if resolution.normalized_value is not None:
            media_override = _media_artifact_type_override(
                resolution.normalized_value,
                context,
            )
            if media_override is not None:
                self.recorder.record(
                    context.decisions,
                    skill_id=context.skill_id,
                    path=str(context.manifest.folder.path),
                    direction=context.direction,
                    field="type",
                    raw_value=raw_type,
                    token=token,
                    normalized_value=media_override,
                    method="semantic_media_override",
                    vocab="data_type_vocab",
                    vocab_version=self.vocabulary.version,
                    confidence=0.9,
                    details={
                        "original_type": resolution.normalized_value,
                        "io_name": context.io_name,
                        "media_type": media_override,
                        "reason": (
                            f"I/O name denotes a single {media_override} artifact; raw "
                            "carrier type is preserved in the description."
                        ),
                    },
                )
                return media_override
            self.recorder.record(
                context.decisions,
                skill_id=context.skill_id,
                path=str(context.manifest.folder.path),
                direction=context.direction,
                field="type",
                raw_value=raw_type,
                token=token,
                normalized_value=resolution.normalized_value,
                method=resolution.method,
                vocab="data_type_vocab",
                vocab_version=self.vocabulary.version,
                confidence=resolution.confidence,
            )
            return resolution.normalized_value

        inferred = self.vocabulary.infer_from_io_semantics(
            context.io_name,
            context.description,
        )
        if inferred is not None:
            context.diagnostics.append(
                ExtractionDiagnostic(
                    stage="normalization",
                    severity="info",
                    code="unsupported_type_inferred",
                    message="unsupported artifact type inferred from I/O semantics",
                    skill_id=context.skill_id,
                    path=str(context.manifest.folder.path),
                    details={
                        "original_type": raw_type,
                        "normalized_token": token,
                        "inferred_type": inferred.data_type,
                        "io_name": context.io_name,
                        "method": inferred.method,
                        "confidence": inferred.confidence,
                        "reason": inferred.reason,
                        "evidence": inferred.evidence,
                    },
                )
            )
            self._record_inference(
                context,
                raw_type,
                token,
                inferred,
            )
            return inferred.data_type

        context.diagnostics.append(
            ExtractionDiagnostic(
                stage="normalization",
                severity="warning",
                code="unsupported_type_normalized",
                message="artifact type is not supported; normalized to unknown",
                skill_id=context.skill_id,
                path=str(context.manifest.folder.path),
                details={"original_type": raw_type, "normalized_token": token},
            )
        )
        self.recorder.record(
            context.decisions,
            skill_id=context.skill_id,
            path=str(context.manifest.folder.path),
            direction=context.direction,
            field="type",
            raw_value=raw_type,
            token=token,
            normalized_value=self.vocabulary.unknown_type,
            method="unknown",
            vocab="data_type_vocab",
            vocab_version=self.vocabulary.version,
            confidence=0.0,
        )
        return self.vocabulary.unknown_type

    def validate(
        self,
        fingerprint: Fingerprint,
        manifest: RawSkillManifest,
        diagnostics: List[ExtractionDiagnostic],
    ) -> None:
        if not fingerprint.outputs:
            diagnostics.append(
                ExtractionDiagnostic(
                    stage="normalization",
                    severity="error",
                    code="schema_validation_failed",
                    message="outputs must not be empty",
                    skill_id=fingerprint.id,
                    path=str(manifest.folder.path),
                )
            )
        for item in [*fingerprint.inputs, *fingerprint.outputs]:
            if not self.vocabulary.contains(item.type):
                diagnostics.append(
                    ExtractionDiagnostic(
                        stage="normalization",
                        severity="error",
                        code="schema_validation_failed",
                        message="type is outside DataType vocabulary",
                        skill_id=fingerprint.id,
                        path=str(manifest.folder.path),
                        details={"type": item.type},
                    )
                )

    def _record_inference(
        self,
        context: DataTypeNormalizationContext,
        raw_type: str,
        token: str,
        inferred: DataTypeInference,
    ) -> None:
        self.recorder.record(
            context.decisions,
            skill_id=context.skill_id,
            path=str(context.manifest.folder.path),
            direction=context.direction,
            field="type",
            raw_value=raw_type,
            token=token,
            normalized_value=inferred.data_type,
            method=inferred.method,
            vocab="data_type_vocab",
            vocab_version=self.vocabulary.version,
            confidence=inferred.confidence,
            details={
                "reason": inferred.reason,
                "io_name": context.io_name,
                "method": inferred.method,
                "evidence": inferred.evidence,
            },
        )


def _media_artifact_type_override(
    normalized_type: str,
    context: DataTypeNormalizationContext,
) -> Optional[str]:
    if normalized_type not in _MEDIA_REFERENCE_CARRIER_TYPES:
        return None
    media_type = _single_media_artifact_type_from_name(context.io_name)
    if media_type is None:
        return None
    if normalized_type == "text" and not _text_description_can_carry_media(
        context.description,
    ):
        return None
    return media_type


def _single_media_artifact_type_from_name(io_name: str) -> Optional[str]:
    token = normalize_token(io_name)
    if not token:
        return None

    parts = set(token.split("_"))
    matches = []
    for media_type, terms in _MEDIA_ARTIFACT_NAME_TERMS.items():
        if token in terms:
            matches.append(media_type)
            continue
        if parts & terms and not parts & _NON_ARTIFACT_MEDIA_NAME_TERMS:
            matches.append(media_type)
    if len(matches) != 1:
        return None
    return matches[0]


def _text_description_can_carry_media(description: str) -> bool:
    token = normalize_token(description)
    if not token:
        return False
    parts = set(token.split("_"))
    return bool(parts & _TEXT_MEDIA_REFERENCE_DESCRIPTION_TERMS)
