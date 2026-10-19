"""I/O semantic name normalization."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Dict, List, Optional

from jiuwenswarm.symphony.fingerprint.models import (
    ExtractedSkillSchema,
    ExtractionDiagnostic,
    NormalizationConfig,
    NormalizationDecision,
    RawSkillManifest,
)
from jiuwenswarm.symphony.fingerprint.normalize.decisions import (
    NormalizationDecisionRecorder,
)
from jiuwenswarm.symphony.fingerprint.normalize.io_name_vocab import (
    IONameCandidate,
    IONameResolver,
    IONameResolution,
    IONameVocabulary,
)
from jiuwenswarm.symphony.fingerprint.normalize.vocabulary import term_similarity
from jiuwenswarm.symphony.fingerprint.utils import normalize_parameter_name, to_dict

NATURAL_LANGUAGE_COMMAND_NAMES = frozenset(
    {
        "command",
        "instruction",
        "user_command",
        "user_instruction",
    }
)
NATURAL_LANGUAGE_COMMAND_TYPES = frozenset(
    {
        "markdown",
        "natural_language",
        "string",
        "text",
    }
)
NATURAL_LANGUAGE_COMMAND_HINTS = frozenset(
    {
        "free-form",
        "free form",
        "natural language",
        "request",
        "user input",
        "user instruction",
        "user request",
        "用户指令",
        "用户输入",
        "用户请求",
        "自然语言",
    }
)
CONTROL_COMMAND_HINTS = frozenset(
    {
        "allowed values",
        "cli",
        "command line",
        "enum",
        "flag",
        "subcommand",
        "命令行",
        "可选值",
        "子命令",
        "枚举",
    }
)
NATURAL_LANGUAGE_COMMAND_ALIAS_REASON = (
    "natural-language user request/input, not control command"
)


@dataclass(frozen=True)
class IONameNormalizationContext:
    raw_type: str
    description: str
    manifest: RawSkillManifest
    skill_id: str
    direction: str
    diagnostics: List[ExtractionDiagnostic]
    decisions: List[NormalizationDecision]


class IONameNormalizer:
    """Normalize Skill input/output semantic names through io_name_vocab."""

    def __init__(
        self,
        config: NormalizationConfig,
        vocabulary: IONameVocabulary,
        resolver: IONameResolver,
        recorder: NormalizationDecisionRecorder,
    ) -> None:
        self.config = config
        self.vocabulary = vocabulary
        self.resolver = resolver
        self.recorder = recorder
        self._resolution_cache: Dict[str, IONameResolution] = {}
        self._resolution_cache_lock = RLock()

    def collect_candidates(
        self,
        manifest: RawSkillManifest,
        extracted: ExtractedSkillSchema,
        skill_id: str,
    ) -> List[IONameCandidate]:
        candidates: List[IONameCandidate] = []
        seen: set[str] = set()
        fields = [
            ("input", extracted.inputs, self.config.default_input_name, self.config.default_input_type),
            ("output", extracted.outputs, self.config.default_output_name, self.config.unknown_type),
        ]
        for direction, items, default_name, default_type in fields:
            for raw in items:
                data = to_dict(raw)
                raw_name = str(data.get("name") or default_name)
                raw_type = str(data.get("type") or default_type)
                token = normalize_parameter_name(raw_name)
                if not token or token in seen:
                    continue
                if self.vocabulary.lookup(token) is not None:
                    continue
                if _is_natural_language_command_input(
                    token,
                    raw_type,
                    str(data.get("description") or ""),
                    direction,
                ):
                    continue
                with self._resolution_cache_lock:
                    cached = token in self._resolution_cache
                if cached:
                    continue
                seen.add(token)
                candidates.append(
                    IONameCandidate(
                        raw_value=raw_name,
                        token=token,
                        direction=direction,
                        data_type=raw_type,
                        description=str(data.get("description") or ""),
                        skill_id=skill_id,
                        path=str(manifest.folder.path),
                    )
                )
        return candidates

    async def prime_resolutions(
        self,
        candidates_by_skill: List[List[IONameCandidate]],
    ) -> None:
        candidates_by_skill = [candidates for candidates in candidates_by_skill if candidates]
        if not candidates_by_skill:
            return
        resolutions = await self.resolver.resolve_async(
            candidates_by_skill,
            self.vocabulary,
        )
        self._ensure_batch_canonical_terms(candidates_by_skill, resolutions)
        with self._resolution_cache_lock:
            self._resolution_cache.update(resolutions)

    async def normalize(
        self,
        raw_name: str,
        context: IONameNormalizationContext,
    ) -> Optional[str]:
        token = normalize_parameter_name(raw_name)
        if _is_natural_language_command_input(
            token,
            context.raw_type,
            context.description,
            context.direction,
        ):
            normalized = self.vocabulary.ensure_term(
                "text",
                alias=token,
                example=context.description,
                definition="Natural-language user task, request, or instruction text.",
            )
            self.recorder.record(
                context.decisions,
                skill_id=context.skill_id,
                path=str(context.manifest.folder.path),
                direction=context.direction,
                field="name",
                raw_value=raw_name,
                token=token,
                normalized_value=normalized,
                method="semantic_alias",
                vocab="io_name_vocab",
                vocab_version=self.vocabulary.version,
                confidence=0.95,
                details={
                    "reason": NATURAL_LANGUAGE_COMMAND_ALIAS_REASON,
                    "vocab_size": self.vocabulary.size(),
                    "max_vocab_size": self.vocabulary.max_vocab_size,
                },
            )
            return normalized

        resolution = self._cached_resolution(token)
        if resolution is None:
            existing = self.vocabulary.lookup(token)
            if existing is not None:
                method = "vocab_alias" if existing != token else "vocab_exact"
                confidence = 0.95 if existing != token else 1.0
                self.recorder.record(
                    context.decisions,
                    skill_id=context.skill_id,
                    path=str(context.manifest.folder.path),
                    direction=context.direction,
                    field="name",
                    raw_value=raw_name,
                    token=token,
                    normalized_value=existing,
                    method=method,
                    vocab="io_name_vocab",
                    vocab_version=self.vocabulary.version,
                    confidence=confidence,
                )
                return existing

            candidate = IONameCandidate(
                raw_value=raw_name,
                token=token,
                direction=context.direction,
                data_type=context.raw_type,
                description=context.description,
                skill_id=context.skill_id,
                path=str(context.manifest.folder.path),
            )
            resolutions = await self.resolver.resolve_async(
                [[candidate]],
                self.vocabulary,
            )
            resolution = resolutions.get(token)
            if resolution is None:
                resolution = IONameResolution(
                    normalized_value=token,
                    action="create_new",
                    confidence=0.0,
                    reason="resolver omitted candidate",
                )
            with self._resolution_cache_lock:
                self._resolution_cache[token] = resolution
        normalized = self._apply_resolution(
            token,
            resolution,
            context,
        )
        self.recorder.record(
            context.decisions,
            skill_id=context.skill_id,
            path=str(context.manifest.folder.path),
            direction=context.direction,
            field="name",
            raw_value=raw_name,
            token=token,
            normalized_value=normalized or "",
            method=resolution.action,
            vocab="io_name_vocab",
            vocab_version=self.vocabulary.version,
            confidence=resolution.confidence,
            details={
                "reason": resolution.reason,
                "forced_merge": resolution.forced_merge,
                "vocab_size": self.vocabulary.size(),
                "max_vocab_size": self.vocabulary.max_vocab_size,
            },
        )
        return normalized

    def _cached_resolution(self, token: str) -> IONameResolution | None:
        with self._resolution_cache_lock:
            return self._resolution_cache.get(token)

    def _ensure_batch_canonical_terms(
        self,
        candidates_by_skill: List[List[IONameCandidate]],
        resolutions: Dict[str, IONameResolution],
    ) -> None:
        candidates_by_token = {
            candidate.token: candidate
            for candidates in candidates_by_skill
            for candidate in candidates
        }
        known_terms = set(self.vocabulary.term_names())
        for token, resolution in resolutions.items():
            if resolution.action != "create_new":
                continue
            target = (resolution.normalized_value or token).strip()
            if not target or target in known_terms:
                continue
            candidate = candidates_by_token.get(token)
            self.vocabulary.ensure_term(
                target,
                alias=token,
                example=candidate.description if candidate is not None else "",
                definition=resolution.definition,
            )
            known_terms.add(target)

    def _apply_resolution(
        self,
        token: str,
        resolution: IONameResolution,
        context: IONameNormalizationContext,
    ) -> Optional[str]:
        if resolution.action == "exclude_from_vocab":
            return None

        if resolution.action == "create_new" and not self.vocabulary.is_full():
            self._warn_possible_duplicate(
                token,
                context,
            )
            return self.vocabulary.create_term(
                resolution.normalized_value or token,
                alias=token,
                example=context.description,
                definition=resolution.definition,
            )

        target = resolution.normalized_value or self.vocabulary.closest_term(token)
        if target is None:
            return self.vocabulary.create_term(
                token,
                alias=token,
                example=context.description,
                definition=context.description,
            )
        if target not in self.vocabulary.term_names():
            closest = self.vocabulary.closest_term(target)
            if closest is None:
                return self.vocabulary.create_term(
                    token,
                    alias=token,
                    example=context.description,
                    definition=context.description,
                )
            target = closest
        self.vocabulary.add_alias(
            token,
            target,
            example=context.description,
            definition=resolution.definition,
        )
        return target

    def _warn_possible_duplicate(
        self,
        token: str,
        context: IONameNormalizationContext,
    ) -> None:
        threshold = self.config.possible_duplicate_name_similarity_threshold
        if threshold <= 0:
            return
        term_names = self.vocabulary.term_names()
        if not term_names:
            return
        closest = max(term_names, key=lambda name: term_similarity(token, name))
        score = term_similarity(token, closest)
        if score < threshold:
            return
        context.diagnostics.append(
            ExtractionDiagnostic(
                stage="normalization",
                severity="warning",
                code="possible_duplicate_io_name",
                message="new I/O name is similar to an existing vocabulary term; review aliasing",
                skill_id=context.skill_id,
                path=str(context.manifest.folder.path),
                details={
                    "direction": context.direction,
                    "token": token,
                    "closest_term": closest,
                    "similarity": round(score, 4),
                    "threshold": threshold,
                },
            )
        )


def _is_natural_language_command_input(
    token: str,
    raw_type: str,
    description: str,
    direction: str,
) -> bool:
    if direction != "input":
        return False
    if token not in NATURAL_LANGUAGE_COMMAND_NAMES:
        return False
    if normalize_parameter_name(raw_type) not in NATURAL_LANGUAGE_COMMAND_TYPES:
        return False

    folded_description = str(description or "").casefold()
    if any(hint in folded_description for hint in CONTROL_COMMAND_HINTS):
        return False
    return any(hint in folded_description for hint in NATURAL_LANGUAGE_COMMAND_HINTS)
