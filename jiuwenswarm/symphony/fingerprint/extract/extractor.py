"""LLM-backed schema extraction."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from jiuwenswarm.symphony.llm import (
    LLMConfig,
    create_llm_client,
    llm_usage_context,
    thinking_disabled_request_overrides,
)
from jiuwenswarm.symphony.fingerprint.models import (
    ArtifactSpec,
    ExtractedSkillSchema,
    ParameterSpec,
    RawSkillManifest,
)
from jiuwenswarm.symphony.shared.llm_payload import compact_json, prune_empty

SCHEMA_EXTRACTION_PROTOCOL_VERSION = "symphony-schema-extraction-v2"
_MAX_WARNING_COUNT = 3
_MAX_REASON_LENGTH = 160


class LLMSchemaExtractor:
    """Extract Skill IO schema using LLM."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        body_limit: int | None = None,
        batch_size: int = 1,
    ) -> None:
        self.config = config
        self.client = create_llm_client(config)
        self.batch_size = max(1, int(batch_size))
        self.use_batch = self.batch_size > 1
        self.body_limit = _normalize_body_limit(body_limit)

    async def extract(self, manifest: RawSkillManifest) -> ExtractedSkillSchema:
        with llm_usage_context("fingerprint_extraction", "schema_extraction"):
            content = await self.client.complete_json_async(
                system_prompt=_SCHEMA_EXTRACTION_PROMPT,
                user_content=compact_json(
                    _build_llm_context(manifest, body_limit=self.body_limit)
                ),
                error_context="LLM schema extraction",
                request_overrides=thinking_disabled_request_overrides(),
            )
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "LLM response is not valid JSON. "
                f"content_prefix={content[:1000]!r}"
            ) from exc
        return schema_from_llm_payload(payload)

    async def extract_many(
        self,
        manifests: List[RawSkillManifest],
    ) -> List[ExtractedSkillSchema]:
        if self.batch_size > 1:
            return await self._extract_many_prompt_batch(manifests)

        with llm_usage_context("fingerprint_extraction", "schema_extraction_batch"):
            contents = await self.client.complete_json_many_async(
                [
                    {
                        "system_prompt": _SCHEMA_EXTRACTION_PROMPT,
                        "user_content": compact_json(
                            _build_llm_context(manifest, body_limit=self.body_limit),
                        ),
                    }
                    for manifest in manifests
                ],
                error_context="LLM schema extraction batch",
                request_overrides=thinking_disabled_request_overrides(),
            )
        schemas: List[ExtractedSkillSchema] = []
        for index, content in enumerate(contents, start=1):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "LLM batch response item is not valid JSON. "
                    f"item={index} content_prefix={content[:1000]!r}"
                ) from exc
            schemas.append(schema_from_llm_payload(payload))
        return schemas

    async def _extract_many_prompt_batch(
        self,
        manifests: List[RawSkillManifest],
    ) -> List[ExtractedSkillSchema]:
        if not manifests:
            return []
        if len(manifests) == 1:
            return [await self.extract(manifests[0])]

        try:
            return await self._extract_many_prompt_batch_once(manifests)
        except RuntimeError:
            midpoint = len(manifests) // 2
            if midpoint <= 0:
                return [await self.extract(manifests[0])]
            return (
                await self._extract_many_prompt_batch(manifests[:midpoint])
                + await self._extract_many_prompt_batch(manifests[midpoint:])
            )

    async def _extract_many_prompt_batch_once(
        self,
        manifests: List[RawSkillManifest],
    ) -> List[ExtractedSkillSchema]:
        expected_refs = [f"s{index}" for index in range(1, len(manifests) + 1)]
        contexts = [
            _build_llm_context(
                manifest,
                body_limit=self.body_limit,
                skill_ref=skill_ref,
            )
            for manifest, skill_ref in zip(manifests, expected_refs)
        ]
        with llm_usage_context(
            "fingerprint_extraction",
            "schema_extraction_prompt_batch",
        ):
            content = await self.client.complete_json_async(
                system_prompt=_SCHEMA_EXTRACTION_BATCH_PROMPT,
                user_content=compact_json({"skills": contexts}),
                error_context="LLM schema extraction prompt batch",
                request_overrides=thinking_disabled_request_overrides(),
            )
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "LLM prompt batch response is not valid JSON. "
                f"content_prefix={content[:1000]!r}"
            ) from exc

        schemas_payload = payload.get("schemas")
        if not isinstance(schemas_payload, list):
            raise RuntimeError("LLM prompt batch response must contain schemas array.")

        by_ref: Dict[str, Dict[str, Any]] = {}
        for item in schemas_payload:
            if not isinstance(item, dict):
                raise RuntimeError("LLM prompt batch schema items must be objects.")
            skill_ref = str(item.get("skill_ref") or "")
            if skill_ref in by_ref:
                raise RuntimeError(
                    f"LLM prompt batch returned duplicate skill_ref: {skill_ref!r}."
                )
            by_ref[skill_ref] = item

        missing_refs = [
            skill_ref for skill_ref in expected_refs if skill_ref not in by_ref
        ]
        extra_refs = sorted(set(by_ref) - set(expected_refs))
        if missing_refs or extra_refs:
            raise RuntimeError(
                "LLM prompt batch response skill_ref mismatch. "
                f"missing={missing_refs!r} extra={extra_refs!r}"
            )

        return [schema_from_llm_payload(by_ref[skill_ref]) for skill_ref in expected_refs]


def schema_from_llm_payload(payload: Dict[str, Any]) -> ExtractedSkillSchema:
    """Convert a raw LLM JSON payload into ExtractedSkillSchema."""

    warnings = _short_texts(payload.get("warnings", []))
    raw_output_notes = payload.get("raw_output_notes", [])
    if isinstance(raw_output_notes, str):
        raw_output_notes = [raw_output_notes]
    warnings.extend(_short_texts(raw_output_notes))
    warnings = warnings[:_MAX_WARNING_COUNT]

    return ExtractedSkillSchema(
        description=str(payload.get("description") or ""),
        inputs=[_parameter_from_payload(item) for item in payload.get("inputs", [])],
        outputs=[_artifact_from_payload(item) for item in payload.get("outputs", [])],
        confidence=payload.get("confidence"),
        warnings=warnings,
    )


def _parameter_from_payload(payload: Dict[str, Any]) -> ParameterSpec:
    return ParameterSpec(
        name=str(payload.get("name") or "input"),
        type=_combined_type_from_payload(payload, "text"),
        required=bool(payload.get("required", True)),
        description=str(payload.get("description") or ""),
        default=payload.get("default"),
    )


def _artifact_from_payload(payload: Dict[str, Any]) -> ArtifactSpec:
    return ArtifactSpec(
        name=str(payload.get("name") or "result"),
        type=_combined_type_from_payload(payload, "unknown"),
        description=str(payload.get("description") or ""),
    )


def _combined_type_from_payload(payload: Dict[str, Any], default: str) -> str:
    return str(payload.get("format") or payload.get("type") or default)


def _build_llm_context(
    manifest: RawSkillManifest,
    *,
    body_limit: int | None = None,
    skill_ref: str | None = None,
) -> Dict[str, Any]:
    body_limit = _normalize_body_limit(body_limit)
    body = manifest.body if body_limit is None else manifest.body[:body_limit]
    return prune_empty({
        "skill_ref": skill_ref,
        "frontmatter": manifest.frontmatter,
        "body": body,
        "body_truncated": (
            True if body_limit is not None and len(manifest.body) > body_limit else None
        ),
    })


def _short_texts(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    texts: List[str] = []
    for item in values:
        text = str(item).strip()
        if text:
            texts.append(text[:_MAX_REASON_LENGTH])
    return texts


def _normalize_body_limit(body_limit: int | None) -> int | None:
    if body_limit is None:
        return None
    parsed = int(body_limit)
    return parsed if parsed > 0 else None

_SCHEMA_EXTRACTION_PROMPT = """Extract a normalized Skill I/O fingerprint from the supplied SKILL.md.

Return JSON only:
{
  "description": "concise capability summary",
  "inputs": [{"name": "semantic_role", "type": "type", "required": true,
              "description": "short description"}],
  "outputs": [{"name": "semantic_deliverable", "type": "type",
               "description": "short description"}],
  "confidence": 0.0,
  "warnings": []
}

Rules:
1. Use only capabilities and runtime I/O supported by all supplied content.
   If body_truncated is true, report uncertainty instead of inventing fields.
2. Inputs are caller-provided runtime values. Exclude credentials, environment
   setup, permissions, caches, telemetry and internal state.
3. Outputs are user-facing or downstream deliverables. Extract the semantic
   artifact, not API wrappers, status fields, logs, debug data or containers.
4. Use short canonical semantic names and put details in descriptions.
5. Classify by artifact semantics rather than transport: image/audio/video
   URLs, paths or base64 remain image/audio/video. Use url for ordinary links.
6. Preferred types: text, markdown, json, csv, table, yaml, xml, pdf, html,
   docx, pptx, xlsx, image, audio, video, file, path, url, code, archive, unknown.
7. Set required=true only when normal execution requires caller input.
8. Treat free-form tasks as query, text or topic; reserve command for explicit
   control operations. Do not emit duplicate semantic inputs or outputs.
9. Keep warnings factual and brief. Return at most three.
"""

_SCHEMA_EXTRACTION_BATCH_PROMPT = f"""{_SCHEMA_EXTRACTION_PROMPT}

Input is {{"skills": [...]}}. Return {{"schemas": [...]}} with exactly one
schema per input Skill. Preserve each short skill_ref exactly, keep Skills
independent, and return no additional Skills.
"""
