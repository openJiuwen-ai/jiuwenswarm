"""Versioned, evidence-bound contracts for collaborative paper writing."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
WRITER_SECTIONS = ("method", "experiments", "related_work", "introduction", "limitations", "conclusion")

# These are publication-oriented lower bounds, not targets.  They prevent a
# structurally valid one-paragraph synopsis from being released as a paper.
# The required rhetorical roles make the word bound harder to satisfy through
# repetition alone and give reviewers stable locations for argument checks.
SECTION_QUALITY_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "introduction": {
        "min_words": 450, "min_paragraphs": 4, "min_words_per_paragraph": 80,
        "required_roles": ["context", "gap", "research_question", "contributions"],
        "min_unique_citations": 2, "min_evidence_assertions": 0,
    },
    "related_work": {
        "min_words": 650, "min_paragraphs": 5, "min_words_per_paragraph": 90,
        "required_roles": ["landscape", "closest_methods", "comparison", "limitations_of_prior_work", "positioning"],
        "min_unique_citations": 5, "min_evidence_assertions": 0,
    },
    "method": {
        "min_words": 850, "min_paragraphs": 6, "min_words_per_paragraph": 100,
        "required_roles": ["overview", "problem_formulation", "components", "algorithm", "implementation", "complexity"],
        "min_unique_citations": 1, "min_evidence_assertions": 0,
    },
    "experiments": {
        "min_words": 900, "min_paragraphs": 7, "min_words_per_paragraph": 100,
        "required_roles": ["setup", "datasets", "baselines", "metrics", "main_results", "analysis", "robustness"],
        "min_unique_citations": 1, "min_evidence_assertions": 2,
    },
    "limitations": {
        "min_words": 300, "min_paragraphs": 3, "min_words_per_paragraph": 80,
        "required_roles": ["internal_validity", "external_validity", "reproducibility"],
        "min_unique_citations": 0, "min_evidence_assertions": 0,
    },
    "conclusion": {
        "min_words": 250, "min_paragraphs": 3, "min_words_per_paragraph": 70,
        "required_roles": ["findings", "scope", "implications"],
        "min_unique_citations": 0, "min_evidence_assertions": 1,
    },
}

_WORD = re.compile(r"\b[\w]+(?:[-'][\w]+)*\b", re.UNICODE)
_CITATION = re.compile(r"\[cite:([\w./:-]+)\]")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _ids(values: Any) -> list[str]:
    return sorted({_text(value) for value in values or [] if _text(value)})


def _string_list(value: Any) -> list[str] | None:
    """Accept only an explicit JSON list of non-empty strings.

    Architect output is an inter-agent protocol, not free-form prose.  In
    particular, accepting a scalar string here would iterate its characters in
    ``_ids`` and silently turn a malformed LLM response into a plausible plan.
    """
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    return _ids(value)


def _research_question(m1: dict[str, Any]) -> Any:
    question = m1.get("research_question")
    return question if isinstance(question, (str, dict)) else m1.get("topic")


def _stable_hash(value: Any) -> str:
    """Return a replayable digest without relying on a caller's file paths."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_paper_contract(
    inputs: dict[str, Any], inventory: dict[str, Any], readiness: dict[str, Any],
    claims_ledger: dict[str, Any], measurements: dict[str, Any], bibliography: dict[str, Any], execution_alignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the immutable factual core that every writing role must consume."""
    claims = [dict(item) for item in claims_ledger.get("claims") or [] if isinstance(item, dict) and _text(item.get("id"))]
    aggregates = [dict(item) for item in measurements.get("aggregates") or [] if isinstance(item, dict)]
    run_records = [
        dict(item) for item in measurements.get("measurements") or []
        if isinstance(item, dict) and isinstance(item.get("record"), dict)
    ]
    successful_run_records = [item for item in run_records if item["record"].get("success") is True]
    run_record_fields = sorted({
        str(field)
        for item in successful_run_records
        for field in item["record"].keys()
        if str(field)
    })
    claims_by_id = {str(item["id"]): item for item in claims}
    experiment_scopes: dict[str, dict[str, Any]] = defaultdict(lambda: {"methods": set(), "metrics": set(), "aggregate_ids": []})
    for index, aggregate in enumerate(aggregates):
        experiment_id = _text(aggregate.get("experiment_id"))
        if not experiment_id:
            continue
        scope = experiment_scopes[experiment_id]
        scope["methods"].add(_text(aggregate.get("method")))
        scope["metrics"].add(_text(aggregate.get("metric")))
        aggregate_id = _text(aggregate.get("id")) or f"aggregate-{index + 1}"
        scope["aggregate_ids"].append(aggregate_id)
        aggregate["id"] = aggregate_id
    normalized_scopes = {
        experiment_id: {"methods": sorted(value["methods"] - {""}), "metrics": sorted(value["metrics"] - {""}), "aggregate_ids": value["aggregate_ids"]}
        for experiment_id, value in sorted(experiment_scopes.items())
    }
    m1, m2 = inputs["m1"], inputs["m2"]
    design = m2.get("method_design") or {}
    plan = m2.get("experiment_plan") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": "v1",
        "input_hashes": {key: _stable_hash(inputs[key]) for key in ("m1", "m2", "m3")},
        "evidence_mode": inventory.get("evidence_mode"),
        "paper_readiness": {"status": readiness.get("status"), "decision": readiness.get("decision"), "recommended_actions": readiness.get("recommended_actions") or []},
        "execution_alignment": dict(execution_alignment or {}),
        "research_question": _research_question(m1),
        "terms": {
            "primary_methods": _ids(plan.get("primary_methods") or design.get("primary_methods") or design.get("primary_method")),
            "baselines": _ids(item.get("name") for item in plan.get("baselines") or [] if isinstance(item, dict)),
            "datasets": _ids(item.get("name") for item in plan.get("datasets") or [] if isinstance(item, dict)),
            "planned_metrics": _ids(plan.get("metrics")),
        },
        "claims": claims,
        "claims_by_id": claims_by_id,
        "aggregates": aggregates,
        "evidence_availability": {
            "run_level_records_available": bool(successful_run_records),
            "successful_run_record_count": len(successful_run_records),
            "run_record_fields": run_record_fields,
            "run_level_source": "evidence/measurements.json",
        },
        "experiment_scopes": normalized_scopes,
        "bibliography": [dict(item) for item in bibliography.get("entries") or [] if isinstance(item, dict) and _text(item.get("id"))],
        "bibliography_ids": _ids(item.get("id") for item in bibliography.get("entries") or [] if isinstance(item, dict)),
        "chapter_owners": {
            "method": "method_writer", "experiments": "results_writer",
            "related_work": "related_work_writer", "introduction": "introduction_writer",
            "limitations": "limitations_writer", "conclusion": "conclusion_writer",
        },
        "section_quality_requirements": SECTION_QUALITY_REQUIREMENTS,
        "argument_map": {"status": "pending_architect", "chapter_plans": {}},
    }


def validate_paper_contract(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if contract.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported paper contract schema")
    if not _text(contract.get("contract_version")):
        errors.append("paper contract has no contract_version")
    hashes = contract.get("input_hashes")
    if not isinstance(hashes, dict) or set(hashes) != {"m1", "m2", "m3"} or any(not _text(value) for value in hashes.values()):
        errors.append("paper contract has incomplete input hashes")
    claim_ids = [str(item.get("id")) for item in contract.get("claims") or [] if isinstance(item, dict)]
    if len(claim_ids) != len(set(claim_ids)):
        errors.append("paper contract has duplicate claim IDs")
    if set(claim_ids) != set((contract.get("claims_by_id") or {}).keys()):
        errors.append("paper contract claims_by_id does not match claims")
    quality = contract.get("section_quality_requirements")
    if not isinstance(quality, dict) or set(quality) != set(WRITER_SECTIONS):
        errors.append("paper contract has incomplete section quality requirements")
    return errors


def apply_architect_plan(contract: dict[str, Any], response: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Attach only a validated argument map; factual evidence stays immutable."""
    updated = dict(contract)
    plan = response.get("argument_map") if isinstance(response, dict) else None
    # Accept only harmless wrapper variations commonly produced by JSON-mode
    # models.  Scientific fields are still validated strictly below.
    if not isinstance(plan, dict) and isinstance(response, dict) and isinstance(response.get("chapter_plans"), dict):
        plan = {"chapter_plans": response["chapter_plans"]}
    if isinstance(plan, dict) and isinstance(plan.get("argument_map"), dict):
        plan = plan["argument_map"]
    if isinstance(plan, dict) and not isinstance(plan.get("chapter_plans"), dict):
        direct_sections = {key: plan.get(key) for key in WRITER_SECTIONS if key in plan}
        if direct_sections:
            plan = {"chapter_plans": direct_sections}
    if not isinstance(plan, dict):
        return updated, ["paper_architect returned no argument_map"]
    valid_claims = set((contract.get("claims_by_id") or {}).keys())
    plan = {key: value for key, value in plan.items() if key not in {"status", "contract_version"}}
    if set(plan) != {"chapter_plans"}:
        return updated, ["paper_architect argument_map must contain only chapter_plans"]
    chapter_plans = plan.get("chapter_plans") if isinstance(plan.get("chapter_plans"), dict) else None
    if chapter_plans is None:
        return updated, ["paper_architect chapter_plans must be an object"]
    errors: list[str] = []
    normalized: dict[str, Any] = {}
    section_ids = set(chapter_plans)
    unexpected_sections = sorted(section_ids - set(WRITER_SECTIONS))
    missing_sections = [section_id for section_id in WRITER_SECTIONS if section_id not in section_ids]
    errors.extend(f"paper_architect named invalid section {section_id}" for section_id in unexpected_sections)
    errors.extend(f"paper_architect omitted chapter plan for {section_id}" for section_id in missing_sections)
    required_fields = {"purpose", "claim_ids", "dependencies", "prohibitions"}
    for section_id in WRITER_SECTIONS:
        value = chapter_plans.get(section_id)
        if not isinstance(value, dict):
            if section_id in section_ids:
                errors.append(f"paper_architect chapter plan for {section_id} must be an object")
            continue
        extra_fields = sorted(set(value) - required_fields)
        missing_fields = sorted(required_fields - set(value))
        errors.extend(f"paper_architect chapter plan for {section_id} has unexpected field {field}" for field in extra_fields)
        errors.extend(f"paper_architect chapter plan for {section_id} is missing required field {field}" for field in missing_fields)
        purpose = value.get("purpose")
        claim_ids = _string_list(value.get("claim_ids"))
        dependencies = _string_list(value.get("dependencies"))
        prohibitions = _string_list(value.get("prohibitions"))
        if not isinstance(purpose, str) or not purpose.strip():
            errors.append(f"paper_architect chapter plan for {section_id} must have a non-empty string purpose")
        if claim_ids is None:
            errors.append(f"paper_architect chapter plan for {section_id} claim_ids must be a JSON string list")
        if dependencies is None:
            errors.append(f"paper_architect chapter plan for {section_id} dependencies must be a JSON string list")
        if prohibitions is None or not prohibitions:
            errors.append(f"paper_architect chapter plan for {section_id} prohibitions must be a non-empty JSON string list")
        if not isinstance(purpose, str) or not purpose.strip() or claim_ids is None or dependencies is None or prohibitions is None or not prohibitions or missing_fields or extra_fields:
            continue
        unknown = sorted(set(claim_ids) - valid_claims)
        if unknown:
            errors.append(f"paper_architect used unknown claims in {section_id}: {', '.join(unknown)}")
            continue
        invalid_dependencies = sorted(set(dependencies) - set(WRITER_SECTIONS))
        if section_id in dependencies:
            invalid_dependencies.append(section_id)
        if invalid_dependencies:
            errors.append(f"paper_architect used invalid dependencies in {section_id}: {', '.join(sorted(set(invalid_dependencies)))}")
            continue
        normalized[section_id] = {"purpose": purpose.strip(), "claim_ids": claim_ids, "dependencies": dependencies, "prohibitions": prohibitions}
    updated["argument_map"] = {
        "status": "ready" if not errors and len(normalized) == len(WRITER_SECTIONS) else "rejected",
        "chapter_plans": normalized if not errors and len(normalized) == len(WRITER_SECTIONS) else {},
    }
    return updated, errors


def validate_argument_map(contract: dict[str, Any]) -> list[str]:
    argument_map = contract.get("argument_map") or {}
    plans = argument_map.get("chapter_plans") or {}
    errors: list[str] = []
    if argument_map.get("status") != "ready":
        errors.append("paper_architect argument map is not ready")
    missing = [section_id for section_id in WRITER_SECTIONS if section_id not in plans]
    return errors + [f"paper_architect omitted chapter plan for {section_id}" for section_id in missing]


def build_chapter_contract(contract: dict[str, Any], section_id: str, assets: list[dict[str, Any]]) -> dict[str, Any]:
    if section_id not in WRITER_SECTIONS:
        raise ValueError(f"unknown section {section_id}")
    chapter_plan = ((contract.get("argument_map") or {}).get("chapter_plans") or {}).get(section_id) or {}
    allowed_claim_ids = _ids(chapter_plan.get("claim_ids"))
    allowed_assets = [
        {"id": item.get("id"), "kind": item.get("kind"), "section": item.get("section"), "experiment_id": item.get("experiment_id"), "claim_ids": item.get("claim_ids") or [], "caption": item.get("caption")}
        for item in assets if isinstance(item, dict) and item.get("status") == "available" and item.get("section") == section_id
    ]
    claims = [dict((contract.get("claims_by_id") or {})[claim_id]) for claim_id in allowed_claim_ids if claim_id in (contract.get("claims_by_id") or {})]
    quality_requirements = dict((contract.get("section_quality_requirements") or {}).get(section_id) or {})
    min_words = int(quality_requirements.get("min_words") or 0)
    min_words_per_paragraph = int(quality_requirements.get("min_words_per_paragraph") or 0)
    drafting_targets = {
        # Model word estimates are routinely optimistic, especially when
        # citations, Figure/Table markers and hyphenated terms are present.
        # Give authors a meaningful buffer above the deterministic lower
        # bound; the validator still decides publication eligibility.
        "target_words": max(min_words + 150, int(min_words * 1.20)),
        "target_words_per_paragraph": max(min_words_per_paragraph + 35, int(min_words_per_paragraph * 1.30)),
        "required_paragraphs": int(quality_requirements.get("min_paragraphs") or 0),
        "required_unique_citations": int(quality_requirements.get("min_unique_citations") or 0),
        "required_evidence_assertions": int(quality_requirements.get("min_evidence_assertions") or 0),
    }
    return {
        "schema_version": SCHEMA_VERSION, "contract_version": contract["contract_version"],
        "section_id": section_id, "owner": (contract.get("chapter_owners") or {}).get(section_id),
        "purpose": chapter_plan.get("purpose", "Write only evidence-grounded content for this section."),
        "allowed_claims": claims, "allowed_claim_ids": allowed_claim_ids, "allowed_assets": allowed_assets,
        "allowed_citation_ids": contract.get("bibliography_ids") or [],
        "allowed_citations": [dict(item) for item in contract.get("bibliography") or [] if isinstance(item, dict)],
        "aggregates": [dict(item) for item in contract.get("aggregates") or [] if isinstance(item, dict)],
        "experiment_scopes": contract.get("experiment_scopes") or {},
        "evidence_availability": contract.get("evidence_availability") or {},
        "execution_alignment": contract.get("execution_alignment") or {},
        "prohibitions": chapter_plan.get("prohibitions") or ["Do not create facts, numbers, citations, assets, or stronger conclusions than the claim status permits."],
        "dependencies": chapter_plan.get("dependencies") or [],
        "quality_requirements": quality_requirements,
        "drafting_targets": drafting_targets,
        "evidence_identity_rule": (
            "A measurement is identified only by aggregate_id + experiment_id + method + metric; "
            "metric-value assertions must also copy statistic=mean, run_count and value from that aggregate. "
            "Equal numeric values in another aggregate or experiment are not interchangeable. "
            "Configured seeds do not establish repeated execution; a per-run value is not an aggregate mean, "
            "training time is not total wall-clock time, and unmeasured parameter counts are forbidden. "
            "For a claim, canonical_evidence is authoritative; when comparison_status is unresolved or ambiguous, "
            "report only the recorded verdict/difference and do not name inferred comparison arms."
        ),
    }


def _section_quality_errors(section: dict[str, Any], requirements: dict[str, Any], *, location: str) -> list[str]:
    """Validate depth and argument coverage without judging scientific truth."""
    if not requirements:
        return []
    paragraphs = section.get("paragraphs") if isinstance(section, dict) else None
    if not isinstance(paragraphs, list):
        return []
    errors: list[str] = []
    valid_paragraphs = [item for item in paragraphs if isinstance(item, dict)]
    min_paragraphs = int(requirements.get("min_paragraphs") or 0)
    if len(valid_paragraphs) < min_paragraphs:
        errors.append(f"{location} has {len(valid_paragraphs)} paragraphs; publication contract requires at least {min_paragraphs}")
    total_words = 0
    roles: set[str] = set()
    citations: set[str] = set()
    assertion_count = 0
    min_words_per_paragraph = int(requirements.get("min_words_per_paragraph") or 0)
    for index, paragraph in enumerate(valid_paragraphs):
        text = _text(paragraph.get("text"))
        count = len(_WORD.findall(text))
        total_words += count
        role = _text(paragraph.get("rhetorical_role"))
        if role:
            roles.add(role)
        else:
            errors.append(f"{location} paragraph {index} is missing rhetorical_role")
        if count < min_words_per_paragraph:
            errors.append(
                f"{location} paragraph {index} has {count} words; its argument unit requires at least {min_words_per_paragraph}"
            )
        citations.update(_CITATION.findall(text))
        if isinstance(paragraph.get("evidence_assertions"), list):
            assertion_count += len(paragraph["evidence_assertions"])
    min_words = int(requirements.get("min_words") or 0)
    if total_words < min_words:
        errors.append(f"{location} has {total_words} words; publication contract requires at least {min_words}")
    required_roles = {str(value) for value in requirements.get("required_roles") or []}
    missing_roles = sorted(required_roles - roles)
    if missing_roles:
        errors.append(f"{location} is missing required rhetorical roles: {', '.join(missing_roles)}")
    unexpected_roles = sorted(roles - required_roles)
    if unexpected_roles:
        errors.append(f"{location} uses unsupported rhetorical roles: {', '.join(unexpected_roles)}")
    min_citations = int(requirements.get("min_unique_citations") or 0)
    if len(citations) < min_citations:
        errors.append(f"{location} cites {len(citations)} unique sources; publication contract requires at least {min_citations}")
    min_assertions = int(requirements.get("min_evidence_assertions") or 0)
    if assertion_count < min_assertions:
        errors.append(f"{location} has {assertion_count} evidence assertions; publication contract requires at least {min_assertions}")
    return errors


def validate_writer_response_layers(
    response: dict[str, Any], chapter_contract: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Separate fatal protocol defects from locally repairable prose quality.

    Protocol defects make the structured handoff unsafe to consume. Quality
    defects keep the section unpublished but belong to a bounded paragraph
    patch loop rather than aborting the workflow as malformed JSON.
    """
    section_id = chapter_contract["section_id"]
    if not isinstance(response, dict):
        return [f"{section_id} writer returned a non-object response"], []
    section = response.get("section")
    handoff = response.get("chapter_handoff")
    errors: list[str] = []
    quality_errors: list[str] = []
    if not isinstance(section, dict):
        errors.append(f"{section_id} writer response is missing object section")
    else:
        if _text(section.get("section_id")) != section_id:
            errors.append(f"{section_id} writer section_id does not match its chapter contract")
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list) or not paragraphs:
            errors.append(f"{section_id} writer section must contain at least one paragraph")
        else:
            for index, paragraph in enumerate(paragraphs):
                if not isinstance(paragraph, dict):
                    errors.append(f"{section_id} writer paragraph {index} must be an object")
                    continue
                if not _text(paragraph.get("paragraph_id")):
                    errors.append(f"{section_id} writer paragraph {index} is missing paragraph_id")
                if not _text(paragraph.get("text")):
                    errors.append(f"{section_id} writer paragraph {index} is missing text")
                errors.extend(
                    _validate_paragraph_facts(
                        paragraph,
                        section_id=section_id,
                        paragraph_index=index,
                        citation_ids=set(chapter_contract.get("allowed_citation_ids") or []),
                        experiment_scopes=chapter_contract.get("experiment_scopes") or {},
                        aggregates=chapter_contract.get("aggregates") or [],
                        claims_by_id={
                            str(item.get("id")): item for item in chapter_contract.get("allowed_claims") or []
                            if isinstance(item, dict) and item.get("id")
                        },
                    )
                )
                allowed_assets = {
                    _text(item.get("id")): _text(item.get("kind"))
                    for item in chapter_contract.get("allowed_assets") or []
                    if isinstance(item, dict) and _text(item.get("id"))
                }
                for asset_id in _ids(paragraph.get("asset_ids")):
                    if asset_id not in allowed_assets:
                        errors.append(f"{section_id} writer paragraph {index} uses asset {asset_id} outside its chapter contract")
                        continue
                    prefix = "Figure" if allowed_assets[asset_id] == "figure" else "Table"
                    if f"{prefix} [{asset_id}]" not in _text(paragraph.get("text")):
                        errors.append(
                            f"{section_id} writer paragraph {index} declares asset {asset_id} but must explicitly "
                            f"refer to it as {prefix} [{asset_id}]"
                        )
            quality_errors.extend(
                _section_quality_errors(
                    section,
                    chapter_contract.get("quality_requirements") or {},
                    location=f"{section_id} writer section",
                )
            )
    if not isinstance(handoff, dict):
        errors.append(f"{section_id} writer response is missing object chapter_handoff")
    else:
        errors.extend(_validate_conflicts(handoff.get("conflicts"), f"{section_id} chapter_handoff"))
    return errors, quality_errors


def validate_writer_response(response: dict[str, Any], chapter_contract: dict[str, Any]) -> list[str]:
    """Compatibility wrapper returning all release-blocking errors."""
    protocol_errors, quality_errors = validate_writer_response_layers(response, chapter_contract)
    return [*protocol_errors, *quality_errors]


def _validate_conflicts(value: Any, location: str) -> list[str]:
    """Require machine-routable conflicts instead of anonymous prose blockers."""
    if value is None:
        return []
    if not isinstance(value, list):
        return [f"{location} conflicts must be a list"]
    errors: list[str] = []
    required = ("conflict_id", "owner", "required_action", "next_action")
    for index, item in enumerate(value):
        prefix = f"{location} conflict {index}"
        if not isinstance(item, dict):
            errors.append(
                f"{prefix} must be a structured object; ordinary caveats belong in open_dependencies"
            )
            continue
        for field in required:
            if not _text(item.get(field)):
                errors.append(f"{prefix} is missing {field}")
        if not _text(item.get("statement") or item.get("message")):
            errors.append(f"{prefix} is missing statement")
        status = _text(item.get("status")) or "unresolved"
        if status not in {"unresolved", "resolved"}:
            errors.append(f"{prefix} has unsupported status {status}")
    return errors


def _validate_paragraph_facts(
    paragraph: dict[str, Any],
    *,
    section_id: str,
    paragraph_index: int,
    citation_ids: set[str],
    experiment_scopes: dict[str, Any],
    aggregates: list[dict[str, Any]],
    claims_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Reject LLM confusion between claim IDs, citations, methods, and thresholds."""
    location = f"{section_id} writer paragraph {paragraph_index}"
    errors: list[str] = []
    for citation_id in re.findall(r"\[cite:([\w./:-]+)\]", _text(paragraph.get("text"))):
        if citation_id not in citation_ids:
            errors.append(
                f"{location} uses [cite:{citation_id}], but {citation_id} is not an allowed bibliography ID; "
                "remove that marker from paragraph text and keep a claim ID only in claim_ids/used_claim_ids metadata"
            )
    paragraph_claim_ids = _ids(paragraph.get("claim_ids"))
    unknown_claim_ids = sorted(set(paragraph_claim_ids) - set(claims_by_id))
    if unknown_claim_ids:
        errors.append(
            f"{location} uses claim IDs outside its chapter contract: {', '.join(unknown_claim_ids)}; "
            "fact/configuration/analysis IDs are not claim IDs"
        )
    assertions = paragraph.get("evidence_assertions")
    if assertions is None:
        return errors
    if not isinstance(assertions, list):
        return errors + [f"{location} evidence_assertions must be a list"]
    aggregate_by_id = {
        _text(item.get("id")): item
        for item in aggregates if isinstance(item, dict) and _text(item.get("id"))
    }
    paragraph_claims = [claims_by_id[value] for value in paragraph_claim_ids if value in claims_by_id]
    for assertion_index, assertion in enumerate(assertions):
        if not isinstance(assertion, dict):
            errors.append(f"{location} evidence assertion {assertion_index} must be an object")
            continue
        experiment_id = _text(assertion.get("experiment_id"))
        scope = experiment_scopes.get(experiment_id) if experiment_id else None
        methods = set(scope.get("methods") or []) if isinstance(scope, dict) else set()
        assertion_type = _text(assertion.get("type"))
        if assertion_type not in {"metric_value", "metric_comparison"}:
            errors.append(f"{location} evidence assertion {assertion_index} has unsupported type {assertion_type or '<missing>'}")
            continue
        fields = ("method", "comparison_method") if assertion_type == "metric_comparison" else ("method",)
        for field in fields:
            method = _text(assertion.get(field))
            if methods and method and method not in methods:
                errors.append(
                    f"{location} evidence assertion {assertion_index} names {method} as {field}, "
                    f"but it is not a method in {experiment_id}"
                )
        metric = _text(assertion.get("metric"))
        method = _text(assertion.get("method"))
        aggregate_id = _text(assertion.get("aggregate_id"))
        aggregate = aggregate_by_id.get(aggregate_id)
        if not aggregate_id or aggregate is None:
            errors.append(
                f"{location} evidence assertion {assertion_index} must name one existing aggregate_id"
            )
        elif (
            _text(aggregate.get("experiment_id")) != experiment_id
            or _text(aggregate.get("method")) != method
            or _text(aggregate.get("metric")) != metric
        ):
            errors.append(
                f"{location} evidence assertion {assertion_index} identity does not match registered {aggregate_id} "
                "(aggregate_id + experiment_id + method + metric are indivisible)"
            )
        if assertion_type == "metric_value":
            if aggregate is None:
                pass
            else:
                if _text(assertion.get("statistic")) != "mean":
                    errors.append(f"{location} evidence assertion {assertion_index} must declare statistic=mean")
                try:
                    supplied_run_count = int(assertion.get("run_count"))
                    registered_run_count = int(aggregate.get("run_count"))
                    if supplied_run_count != registered_run_count:
                        errors.append(
                            f"{location} evidence assertion {assertion_index} run_count does not match registered {aggregate_id}"
                        )
                except (TypeError, ValueError):
                    errors.append(f"{location} evidence assertion {assertion_index} has no valid run_count")
                try:
                    supplied_value = float(assertion.get("value"))
                    registered_value = float(aggregate.get("mean"))
                    if not math.isclose(supplied_value, registered_value, rel_tol=1e-5, abs_tol=5e-5):
                        errors.append(
                            f"{location} evidence assertion {assertion_index} value does not match "
                            f"registered {aggregate.get('id')}"
                        )
                except (TypeError, ValueError):
                    errors.append(f"{location} evidence assertion {assertion_index} has a non-numeric value")
        else:
            comparison_aggregate_id = _text(assertion.get("comparison_aggregate_id"))
            comparison = aggregate_by_id.get(comparison_aggregate_id)
            comparison_method = _text(assertion.get("comparison_method"))
            if not comparison_aggregate_id or comparison is None:
                errors.append(
                    f"{location} evidence assertion {assertion_index} must name one existing comparison_aggregate_id"
                )
            elif (
                _text(comparison.get("experiment_id")) != experiment_id
                or _text(comparison.get("method")) != comparison_method
                or _text(comparison.get("metric")) != metric
            ):
                errors.append(
                    f"{location} evidence assertion {assertion_index} comparison identity does not match "
                    f"registered {comparison_aggregate_id}"
                )
        assertion_claim_id = _text(assertion.get("claim_id"))
        if assertion_claim_id and assertion_claim_id not in claims_by_id:
            errors.append(
                f"{location} evidence assertion {assertion_index} uses unknown claim_id {assertion_claim_id}"
            )
        bound_claims = (
            [claims_by_id[assertion_claim_id]]
            if assertion_claim_id in claims_by_id
            else [claim for claim in paragraph_claims if isinstance(claim.get("canonical_evidence"), dict)]
        )
        # Without an assertion-level claim_id, scope enforcement is safe only
        # for a paragraph tied to one evaluated claim. Multi-claim Results
        # paragraphs may legitimately report auxiliary or ablation aggregates.
        canonical_experiments = {
            str(value)
            for claim in bound_claims
            for value in ((claim.get("canonical_evidence") or {}).get("experiment_ids") or [])
            if str(value)
        } if len(bound_claims) == 1 else set()
        if canonical_experiments and experiment_id and experiment_id not in canonical_experiments:
            errors.append(
                f"{location} evidence assertion {assertion_index} uses {experiment_id} outside the canonical "
                f"evidence scope of its paragraph claims: {', '.join(sorted(canonical_experiments))}"
            )
    return errors


def write_json(path: str | Path, payload: dict[str, Any]) -> str:
    target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def write_chapter_handoff(path: str | Path, section: dict[str, Any], handoff: dict[str, Any], chapter_contract: dict[str, Any]) -> tuple[str, list[str]]:
    valid_claims = set(chapter_contract.get("allowed_claim_ids") or [])
    valid_assets = {str(item.get("id")) for item in chapter_contract.get("allowed_assets") or []}
    used_claims = _ids(handoff.get("used_claim_ids"))
    used_assets = _ids(handoff.get("used_asset_ids"))
    errors = []
    supplied_version = _text(handoff.get("contract_version"))
    if supplied_version != chapter_contract["contract_version"]:
        errors.append(f"{chapter_contract['section_id']} handoff consumed contract version {supplied_version or '<missing>'}, expected {chapter_contract['contract_version']}")
    if set(used_claims) - valid_claims:
        errors.append(f"{chapter_contract['section_id']} handoff cites a claim outside its chapter contract")
    if set(used_assets) - valid_assets:
        errors.append(f"{chapter_contract['section_id']} handoff cites an asset outside its chapter contract")
    payload = {
        "schema_version": SCHEMA_VERSION, "contract_version": chapter_contract["contract_version"],
        "section_id": chapter_contract["section_id"], "section_path": str(path).replace("handoffs", "sections"),
        "used_claim_ids": used_claims, "used_asset_ids": used_assets,
        "defined_terms": _ids(handoff.get("defined_terms")), "open_dependencies": _ids(handoff.get("open_dependencies")),
        "conflicts": [
            item for item in (handoff.get("conflicts") or [])
            if isinstance(item, dict)
        ] if isinstance(handoff.get("conflicts"), list) else [],
        "validation_errors": errors,
    }
    return write_json(path, payload), errors


def validate_integration_handoff(integration: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    if not isinstance(integration, dict):
        return ["integration editor returned a non-object response"]
    handoff = integration.get("integration_handoff") if isinstance(integration.get("integration_handoff"), dict) else {}
    errors: list[str] = []
    if _text(handoff.get("contract_version")) != _text(contract.get("contract_version")):
        errors.append("integration editor did not consume the current paper contract version")
    errors.extend(_validate_conflicts(handoff.get("conflicts"), "integration_handoff"))
    sections = integration.get("sections")
    if not isinstance(sections, dict):
        return errors + ["integration editor response is missing object sections"]
    expected_sections = set(WRITER_SECTIONS)
    unexpected = sorted(set(sections) - expected_sections)
    missing = [section_id for section_id in WRITER_SECTIONS if section_id not in sections]
    errors.extend(f"integration editor named invalid section {section_id}" for section_id in unexpected)
    errors.extend(f"integration editor omitted section {section_id}" for section_id in missing)
    for section_id in WRITER_SECTIONS:
        section = sections.get(section_id)
        if not isinstance(section, dict):
            if section_id in sections:
                errors.append(f"integration editor section {section_id} must be an object")
            continue
        if _text(section.get("section_id")) != section_id:
            errors.append(f"integration editor section {section_id} has a mismatched section_id")
        if not _text(section.get("title")):
            errors.append(f"integration editor section {section_id} is missing title")
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list) or not paragraphs:
            errors.append(f"integration editor section {section_id} must contain at least one paragraph object")
            continue
        for index, paragraph in enumerate(paragraphs):
            if not isinstance(paragraph, dict):
                errors.append(f"integration editor section {section_id} paragraph {index} must be an object")
                continue
            if not _text(paragraph.get("paragraph_id")):
                errors.append(f"integration editor section {section_id} paragraph {index} is missing paragraph_id")
            if not _text(paragraph.get("text")):
                errors.append(f"integration editor section {section_id} paragraph {index} is missing text")
            errors.extend(
                _validate_paragraph_facts(
                    paragraph,
                    section_id=f"integration editor section {section_id}",
                    paragraph_index=index,
                    citation_ids=set(contract.get("bibliography_ids") or []),
                    experiment_scopes=contract.get("experiment_scopes") or {},
                    aggregates=contract.get("aggregates") or [],
                    claims_by_id=contract.get("claims_by_id") or {},
                )
            )
        errors.extend(
            _section_quality_errors(
                section,
                ((contract.get("section_quality_requirements") or {}).get(section_id) or {}),
                location=f"integration editor section {section_id}",
            )
        )
    return errors


def execution_alignment_conflicts(alignment: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Turn material plan-versus-execution mismatches into routed conflicts.

    Alignment is deterministic evidence, so an editor must not silently choose
    the planned or observed side.  Secondary experiments outside the primary
    scope are reported in the ledger but are not publication-blocking here.
    """
    blocking_kinds = {
        "planned_experiment_not_executed", "planned_primary_method_not_executed",
        "planned_baseline_not_executed", "planned_dataset_not_executed",
        "executed_dataset_not_in_plan", "planned_metric_not_observed",
        "observed_metric_not_in_plan", "planned_seed_not_executed",
        "executed_seed_not_in_config",
    }
    output: list[dict[str, Any]] = []
    for difference in (alignment or {}).get("differences") or []:
        if not isinstance(difference, dict) or _text(difference.get("kind")) not in blocking_kinds:
            continue
        kind = _text(difference.get("kind"))
        experiment_id = _text(difference.get("experiment_id"))
        subject = next((_text(difference.get(field)) for field in ("method", "dataset", "metric", "seed") if _text(difference.get(field))), "scope")
        owner = _text(difference.get("owner")) or "experiment"
        output.append({
            "conflict_id": f"execution-alignment:{kind}:{experiment_id or 'unknown'}:{subject}",
            "statement": f"The planned and observed execution scope disagree: {kind} for {experiment_id or 'an unspecified experiment'} ({subject}).",
            "experiment_ids": [experiment_id] if experiment_id else [],
            "evidence": {"source": "execution_alignment", "difference": difference},
            "owner": owner,
            "required_action": "Resolve the deterministic plan-execution mismatch before treating the affected scope as an executed result.",
            "next_action": f"replan_{owner}",
        })
    return output


def conflict_report(
    handoffs: dict[str, dict[str, Any]], integration: dict[str, Any], coordination: dict[str, Any] | None = None,
    evidence_conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Make every reported cross-role conflict explicit and machine-gateable.

    A role may provide either a string or an object.  The workflow preserves
    the original statement, but publication is blocked unless the coordinator
    explicitly records the conflict as resolved with an owner and next action.
    """
    sources: list[tuple[str, Any]] = []
    for section_id, handoff in handoffs.items():
        sources.extend((f"handoff:{section_id}", item) for item in handoff.get("conflicts") or [])
    sources.extend(("integration", item) for item in integration.get("integration_handoff", {}).get("conflicts", []) if isinstance(integration.get("integration_handoff"), dict))
    sources.extend(("execution_alignment", item) for item in evidence_conflicts or [])
    resolutions = {
        _text(item.get("conflict_id")): item
        for item in (coordination or {}).get("conflicts") or []
        if isinstance(item, dict) and _text(item.get("conflict_id"))
    }
    conflicts: list[dict[str, Any]] = []
    for index, (source, item) in enumerate(sources, start=1):
        raw = dict(item) if isinstance(item, dict) else {"statement": _text(item)}
        conflict_id = _text(raw.get("conflict_id")) or f"conflict-{index}"
        resolution = resolutions.get(conflict_id, {})
        conflicts.append({
            "conflict_id": conflict_id,
            "source": source,
            "status": _text(resolution.get("status") or raw.get("status")) or "unresolved",
            "statement": _text(raw.get("statement") or raw.get("message")),
            "claim_ids": _ids(raw.get("claim_ids")),
            "section_ids": _ids(raw.get("section_ids")),
            "experiment_ids": _ids(raw.get("experiment_ids")),
            "asset_ids": _ids(raw.get("asset_ids")),
            "evidence": raw.get("evidence") or {},
            "owner": _text(resolution.get("owner") or raw.get("owner")),
            "required_action": _text(resolution.get("required_action") or raw.get("required_action")),
            "next_action": _text(resolution.get("next_action") or raw.get("next_action")),
        })
    unresolved = [item["conflict_id"] for item in conflicts if item["status"] != "resolved"]
    return {"schema_version": SCHEMA_VERSION, "contract_version": next(iter(handoffs.values()), {}).get("contract_version"), "conflicts": conflicts, "unresolved_conflict_ids": unresolved}
