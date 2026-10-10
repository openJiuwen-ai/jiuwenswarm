"""Evidence-first writing workflow; intentionally independent of legacy Part1/Part2."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from functools import wraps
from pathlib import Path

from openjiuwen.agent_teams.workflow.engine.facade import log, parallel, phase


_REPAIR_SESSION_TOKEN = secrets.token_hex(6)


def _terminal_verdict(result: dict) -> dict:
    """Attach a stable verdict to every direct workflow exit.

    The CLI also normalises transport status, but this lower-level wrapper is
    needed for callers that import ``run``/``finalize_existing`` directly.
    It prevents an early input, protocol, or audit exit from being reported as
    the unhelpful verdict ``unknown``.
    """
    if not isinstance(result, dict) or str(result.get("verdict") or "").strip():
        return result
    status = str(result.get("status") or "writing_error").strip().casefold()
    verdict = {
        "success": "pass",
        "partial": "writing_incomplete",
        "load_inputs_failed": "input_contract_failed",
        "preflight_failed": "preflight_failed",
        "preflight_error": "preflight_failed",
        "invalid_execution_evidence": "invalid_execution_evidence",
        "needs_experiment_data": "upstream_evidence_required",
        "agent_execution_failed": "agent_execution_failed",
        "aborted": "writing_aborted",
        "failed": "writing_failed",
    }.get(status, f"{status}_failed")
    return {**result, "verdict": verdict}


def _terminalized(async_workflow):
    """Apply terminal-result normalisation without duplicating 30+ exits."""
    @wraps(async_workflow)
    async def wrapped(*args, **kwargs):
        return _terminal_verdict(await async_workflow(*args, **kwargs))
    return wrapped


def _fingerprint(value: object) -> str:
    """Stable, replayable fingerprint for progress checks; never stores prose."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _apply_section_quality_patch(section_id: str, current: dict, patch: dict) -> dict:
    """Merge a small paragraph patch without letting it delete valid content.

    Real models may still return the legacy full-section shape; accept it for
    compatibility.  The preferred shape contains only paragraph_updates and
    append_paragraphs, keeping repair output far below API completion limits.
    """
    if not isinstance(patch, dict):
        return current
    if isinstance(patch.get("section"), dict):
        return {
            "section": patch["section"],
            "chapter_handoff": patch.get("chapter_handoff") or current.get("chapter_handoff") or {},
        }
    existing_section = current.get("section") if isinstance(current.get("section"), dict) else {}
    if str(existing_section.get("section_id") or "") != section_id:
        return current
    paragraphs = [dict(item) for item in existing_section.get("paragraphs") or [] if isinstance(item, dict)]
    by_id = {str(item.get("paragraph_id") or ""): index for index, item in enumerate(paragraphs) if item.get("paragraph_id")}
    updates = patch.get("paragraph_updates") if isinstance(patch.get("paragraph_updates"), list) else []
    appends = patch.get("append_paragraphs") if isinstance(patch.get("append_paragraphs"), list) else []
    changed = False
    for update in updates:
        if not isinstance(update, dict):
            continue
        paragraph_id = str(update.get("paragraph_id") or "")
        if paragraph_id not in by_id or not str(update.get("text") or "").strip():
            continue
        index = by_id[paragraph_id]
        merged = {**paragraphs[index], **update, "paragraph_id": paragraph_id}
        if _fingerprint(merged) != _fingerprint(paragraphs[index]):
            paragraphs[index] = merged
            changed = True
    for paragraph in appends:
        if not isinstance(paragraph, dict):
            continue
        paragraph_id = str(paragraph.get("paragraph_id") or "")
        if not paragraph_id or paragraph_id in by_id or not str(paragraph.get("text") or "").strip():
            continue
        by_id[paragraph_id] = len(paragraphs)
        paragraphs.append(dict(paragraph))
        changed = True
    if not changed:
        return current
    return {
        "section": {**existing_section, "paragraphs": paragraphs},
        "chapter_handoff": patch.get("chapter_handoff") or current.get("chapter_handoff") or {},
    }


def _issue_text_key(issue: dict) -> str:
    """Stable digest of what a finding actually asks for.

    A reviewer can report a new defect at the same paragraph and evidence
    locator.  Location-only matching would incorrectly call that new request a
    repeated failure.  Normalising the requested change keeps the loop bounded
    without conflating unrelated repairs at the same target.
    """
    raw = str(issue.get("required_change") or issue.get("message") or "")
    normalized = re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def _paragraph_section_target(issue: dict, section_ids: set[str]) -> tuple[str, str] | None:
    """Resolve an explicit paragraph locator to its owning section.

    Final visual review can find a figure-to-prose mismatch.  That is a prose
    repair even though the issue is related to an asset.  Older reviewers only
    had an ``asset_id`` field, so recover ownership from ``paragraph_id`` or an
    explicit locator in ``required_change`` without inspecting task-specific
    words, metrics, or experiment names.
    """
    paragraph_id = str(issue.get("paragraph_id") or "").strip()
    repair_text = str(issue.get("required_change") or "")
    for section_id in section_ids:
        aliases = {section_id, section_id.replace("_", "-")}
        for alias in aliases:
            pattern = rf"\b{re.escape(alias)}-p\d+\b"
            match = re.search(pattern, paragraph_id or repair_text, flags=re.I)
            if match:
                return section_id, match.group(0)
    return None


def _asset_action_is_verification_only(action: dict) -> bool:
    """True when the coordinator explicitly assigns the change to prose.

    A request to keep an already-correct caption/manifest unchanged is not an
    asset mutation and must not trigger rendering or a manifest hash gate.
    """
    required = str(action.get("required_change") or "").casefold()
    return (
        ("prose-side" in required or "prose side" in required)
        and ("handled by" in required or "section writer" in required or "writer" in required)
    )


def _visual_issue_requires_asset_change(issue: dict) -> bool:
    """Return true only when an issue needs a renderer/manifest mutation.

    A reviewer can name a related figure while asking solely for a paragraph
    clarification.  Re-rendering cannot repair that request and used to
    produce a false "manifest unchanged" loop.  Keep asset ownership for
    provenance, encoding and rendering facts; route located prose fixes to
    their section writer.
    """
    text = " ".join(str(issue.get(key) or "") for key in (
        "message", "required_change", "acceptance_criteria",
    )).casefold()
    asset_terms = (
        "manifest", "caption", "source snapshot", "source data", "pairing",
        "run_id", "mark encoding", "error bar", "render", "redraw", "plot",
        "chart", "figure file", "table file", "axis", "legend",
    )
    return any(term in text for term in asset_terms)


def _issue_signatures(reviews: dict[str, dict]) -> set[tuple[str, str, str, str, str, str, str, str]]:
    """Identify a review finding by target, evidence locator, and request."""
    signatures: set[tuple[str, str, str, str, str, str, str, str]] = set()
    for role, review in reviews.items():
        for issue in review.get("issues") or [] if isinstance(review, dict) else []:
            if not isinstance(issue, dict):
                continue
            target_kind = "asset" if issue.get("asset_id") else "section" if issue.get("section_id") else ""
            target_id = str(issue.get("asset_id") or issue.get("section_id") or "")
            evidence_kind = str(issue.get("evidence_kind") or "")
            evidence_id = str(issue.get("evidence_id") or "")
            if target_kind and target_id and evidence_kind and evidence_id:
                signatures.add((
                    str(role), target_kind, target_id, str(issue.get("paragraph_id") or ""),
                    str(issue.get("claim_id") or ""), evidence_kind, evidence_id,
                    _issue_text_key(issue),
                ))
    return signatures


def _review_issue_target(issue: dict) -> tuple[str, str] | None:
    """Return the explicit typed owner of a normalized review issue."""
    if not isinstance(issue, dict):
        return None
    if str(issue.get("asset_id") or "").strip():
        return "asset", str(issue["asset_id"]).strip()
    if str(issue.get("section_id") or "").strip():
        return "section", str(issue["section_id"]).strip()
    return None


def _normalize_coordination(coordination: dict, reviews: dict[str, dict]) -> dict:
    """Preserve reviewer locators in every coordinator action.

    The coordinator is allowed to merge requests, but it must not collapse
    structured review records into anonymous strings.  Section writers need a
    paragraph/evidence locator to make a narrow repair; otherwise a valid
    request can turn into a broad rewrite and reintroduce a previously fixed
    defect.  Missing coordinator actions are reconstructed from the review
    records rather than silently dropped.
    """
    normalized = dict(coordination) if isinstance(coordination, dict) else {}
    indexed: dict[tuple[str, str], list[dict]] = {}
    for review in reviews.values():
        for issue in review.get("issues") or [] if isinstance(review, dict) else []:
            target = _review_issue_target(issue)
            if target:
                indexed.setdefault(target, []).append(dict(issue))

    actions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw_action in normalized.get("actions") or []:
        if not isinstance(raw_action, dict):
            continue
        target_kind = str(raw_action.get("target_kind") or "").strip()
        target_id = str(raw_action.get("target_id") or raw_action.get("section_id") or raw_action.get("asset_id") or "").strip()
        target = (target_kind, target_id)
        if target_kind not in {"section", "asset"} or not target_id:
            continue
        # Prefer the original normalized issue object(s).  A coordinator
        # supplied dict is accepted only when it retains a usable locator.
        supplied = [dict(item) for item in raw_action.get("issues") or [] if isinstance(item, dict)]
        located = indexed.get(target) or supplied
        if not located and target in indexed:
            located = indexed[target]
        required = str(raw_action.get("required_change") or "").strip()
        if not required and located:
            required = " ".join(str(item.get("required_change") or "") for item in located).strip()
        actions.append({
            **raw_action,
            "target_kind": target_kind,
            "target_id": target_id,
            "issues": located,
            "required_change": required,
        })
        seen.add(target)
    # A reviewer can return a valid repair request that an unreliable
    # coordinator omits.  Preserve it as a one-target action so it has an
    # owner; later conflict validation still decides whether it is releasable.
    for target, issues in indexed.items():
        if target in seen:
            continue
        actions.append({
            "target_kind": target[0], "target_id": target[1],
            "issues": issues,
            "required_change": " ".join(str(item.get("required_change") or "") for item in issues).strip(),
        })
    normalized["actions"] = actions
    verdict = str(normalized.get("verdict") or "").casefold()
    if verdict == "pass" and actions:
        normalized["verdict"] = "revise"
    return normalized


def _action_targets(coordination: dict) -> set[tuple[str, str]]:
    """Return typed repair targets, preserving asset/section ownership."""
    return {
        (str(action.get("target_kind") or "section"), str(action.get("target_id") or action.get("section_id") or action.get("asset_id") or ""))
        for action in coordination.get("actions") or []
        if isinstance(action, dict) and str(action.get("target_id") or action.get("section_id") or action.get("asset_id") or "")
    }


def _signature_record(signature: tuple) -> dict[str, str]:
    role, target_kind, target_id, paragraph_id, claim_id, evidence_kind, evidence_id, request_digest = signature
    return {
        "reviewer": role,
        "target_kind": target_kind,
        "target_id": target_id,
        "paragraph_id": paragraph_id,
        "claim_id": claim_id,
        "evidence_kind": evidence_kind,
        "evidence_id": evidence_id,
        "request_digest": request_digest,
    }


def _repeated_targeted_issues(
    current: set[tuple],
    prior: set[tuple],
    changed_targets: set[tuple[str, str]],
) -> set[tuple]:
    """Keep issues whose evidence locator survived a material target change.

    Reviewers frequently paraphrase the same request, so the request digest is
    diagnostic rather than an escape hatch from the no-progress guard.
    """
    prior_locators = {signature[:7] for signature in prior}
    return {
        signature for signature in current
        if signature[:7] in prior_locators
        and (signature[1], signature[2]) in changed_targets
    }


def _normalize_adjudication(
    value: dict,
    repeated: set[tuple],
) -> dict:
    """Accept only a bounded, target-preserving repair contract.

    A repeated review finding is neither automatically a reviewer mistake nor
    a reason to keep paraphrasing a generic request. The adjudicator must
    either turn each repeated, evidence-located finding into one testable
    repair contract, or stop with an explicit reviewer-quality/evidence
    outcome. It cannot silently retarget a section.
    """
    normalized = dict(value) if isinstance(value, dict) else {}
    verdict = str(normalized.get("verdict") or "").casefold()
    if verdict in {"review_quality_failure", "failed"}:
        normalized["verdict"] = verdict
        normalized["repairs"] = []
        return normalized
    repairs = normalized.get("repairs") if isinstance(normalized.get("repairs"), list) else []
    expected = {(sig[1], sig[2], sig[3], sig[4], sig[5], sig[6]) for sig in repeated}
    actual: set[tuple[str, str, str, str, str, str]] = set()
    valid_repairs: list[dict] = []
    for repair in repairs:
        if not isinstance(repair, dict):
            continue
        signature = (
            str(repair.get("target_kind") or ""), str(repair.get("target_id") or ""),
            str(repair.get("paragraph_id") or ""), str(repair.get("claim_id") or ""),
            str(repair.get("evidence_kind") or ""), str(repair.get("evidence_id") or ""),
        )
        required_change = str(repair.get("required_change") or "").strip()
        criteria = repair.get("acceptance_criteria")
        if signature in expected and required_change and isinstance(criteria, list) and any(str(item).strip() for item in criteria):
            actual.add(signature)
            valid_repairs.append({**repair, "required_change": required_change})
    if verdict != "repair" or actual != expected or len(valid_repairs) != len(expected):
        return {
            "verdict": "failed",
            "repairs": [],
            "reason": "Revision adjudicator did not return one complete, testable repair contract for every repeated issue.",
        }
    normalized["verdict"] = "repair"
    normalized["repairs"] = valid_repairs
    return normalized


def _adjudicated_actions(adjudication: dict) -> list[dict]:
    """Convert validated repair contracts into writer-owned coordinator actions."""
    actions: list[dict] = []
    for repair in adjudication.get("repairs") or []:
        if not isinstance(repair, dict):
            continue
        actions.append({
            "target_kind": repair["target_kind"],
            "target_id": repair["target_id"],
            "issues": [{
                "paragraph_id": repair.get("paragraph_id"),
                "claim_id": repair.get("claim_id"),
                "evidence_kind": repair.get("evidence_kind"),
                "evidence_id": repair.get("evidence_id"),
                "required_change": repair["required_change"],
                "acceptance_criteria": repair["acceptance_criteria"],
            }],
            "required_change": repair["required_change"],
            "repair_contract": repair,
        })
    return actions


def _read_ledger(path: str) -> dict:
    """Inline evidence for LLM agents; a local path is not agent-readable."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _normalize_review(
    review: dict,
    role: str | None = None,
    paper_contract: dict | None = None,
) -> dict:
    """Reject self-contradictory reviewer output before entering revision.

    Reviewer JSON is advisory.  A ``revise`` verdict with only issues whose
    explicit required action is "No change needed" is a pass, not a reason to
    burn an LLM revision round or block publication.
    """
    normalized = dict(review) if isinstance(review, dict) else {}
    issues = normalized.get("issues") if isinstance(normalized.get("issues"), list) else []
    def normalized_action(issue: dict) -> str:
        return str(issue.get("required_change") or "").strip().casefold().rstrip(".!;:")

    def is_noop(action: str) -> bool:
        return (
            action in {"", "no change needed", "no change is needed"}
            or action.startswith("no change required")
            or action.startswith("optional")
            or action.startswith("optionally")
            or action.startswith("consider ")
        )

    actionable = [
        issue for issue in issues
        if isinstance(issue, dict)
        and not is_noop(normalized_action(issue))
    ]
    # Reviewers sometimes repeat the same manuscript-wide instruction for
    # several paragraphs. One exact request needs one owner and one repair;
    # duplicating it inflates revision rounds without adding evidence.
    deduplicated: list[dict] = []
    seen_requests: set[str] = set()
    for issue in actionable:
        request_key = _issue_text_key(issue)
        if request_key in seen_requests:
            continue
        seen_requests.add(request_key)
        deduplicated.append(issue)
    actionable = deduplicated
    if role and actionable:
        untraceable = [
            issue for issue in actionable
            if not isinstance(issue.get("evidence_kind"), str) or not issue["evidence_kind"].strip()
            or not isinstance(issue.get("evidence_id"), str) or not issue["evidence_id"].strip()
        ]
        if untraceable:
            return {
                "verdict": "failed",
                "issues": [{
                    "severity": "blocker",
                    "message": f"{role} returned an untraceable review issue.",
                    "required_change": "Return evidence_kind and evidence_id for every actionable issue.",
                }],
                "review_quality_failure": True,
            }
    ignored: list[dict] = []
    if role and paper_contract and actionable:
        claims = paper_contract.get("claims_by_id") if isinstance(paper_contract.get("claims_by_id"), dict) else {}
        known_experiments = set((paper_contract.get("experiment_scopes") or {}).keys())
        scoped: list[dict] = []
        for raw_issue in actionable:
            issue = dict(raw_issue)
            claim_id = str(issue.get("claim_id") or "").strip()
            claim = claims.get(claim_id) if claim_id else None
            canonical = claim.get("canonical_evidence") if isinstance(claim, dict) and isinstance(claim.get("canonical_evidence"), dict) else {}
            allowed_experiments = set(canonical.get("experiment_ids") or [])
            issue_text = " ".join(
                str(issue.get(field) or "") for field in ("message", "required_change")
            )
            mentioned_experiments = {value for value in known_experiments if value and value in issue_text}
            outside_experiments = sorted(mentioned_experiments - allowed_experiments) if allowed_experiments else []
            numerical_identity_audit = bool(
                claim_id and (
                    mentioned_experiments
                    or re.search(
                        r"\b(?:aggregate|metric|baseline|threshold|comparison arm|measurement|mean|accuracy|precision|recall|f1|auc|ndcg|mrr)\b|[-+]?\d+\.\d+",
                        issue_text, flags=re.I,
                    )
                )
            )
            # Literature review owns citations and contribution positioning,
            # not recomputation or attribution of experimental measurements.
            if role == "literature_novelty_reviewer" and numerical_identity_audit:
                ignored.append({
                    "reason": "numerical_evidence_identity_outside_reviewer_role",
                    "issue": issue,
                })
                continue
            if claim_id and outside_experiments:
                comparison = canonical.get("comparison") if canonical.get("comparison_status") == "resolved" else None
                if isinstance(comparison, dict):
                    left = comparison.get("minuend") or {}
                    right = comparison.get("subtrahend") or {}
                    identity_text = (
                        f"{left.get('aggregate_id')} ({left.get('experiment_id')}, {left.get('method')}, {left.get('metric')}) "
                        f"minus {right.get('aggregate_id')} ({right.get('experiment_id')}, {right.get('method')}, {right.get('metric')})"
                    )
                    required_change = (
                        f"Use the paper contract's canonical evidence binding for claim {claim_id}: {identity_text}. "
                        f"Remove attribution to out-of-scope experiment(s) {', '.join(outside_experiments)} and keep all sections consistent."
                    )
                else:
                    required_change = (
                        f"Keep claim {claim_id} within its canonical experiment scope "
                        f"{', '.join(sorted(allowed_experiments))}. Remove attribution to out-of-scope experiment(s) "
                        f"{', '.join(outside_experiments)}. Because no unique comparison-arm binding is recorded, "
                        "report only the supplied verdict/difference without naming inferred comparison arms."
                    )
                issue.update({
                    "evidence_kind": "claim",
                    "evidence_id": claim_id,
                    "message": (
                        f"The manuscript/review mentions evidence outside the canonical scope for claim {claim_id}; "
                        "the immutable paper contract must arbitrate the attribution."
                    ),
                    "required_change": required_change,
                })
            scoped.append(issue)
        actionable = scoped
    normalized["issues"] = actionable
    if ignored:
        normalized["ignored_out_of_scope_issues"] = ignored
    verdict = str(normalized.get("verdict") or "").casefold()
    if verdict == "revise" and not actionable:
        normalized["verdict"] = "pass"
    elif verdict not in {"pass", "revise", "failed"}:
        normalized["verdict"] = "failed"
        normalized["issues"] = actionable or [{"severity": "blocker", "message": "Reviewer returned an invalid verdict.", "required_change": "Return one of pass, revise, or failed."}]
    return normalized


def _normalize_visual_review(review: dict) -> dict:
    """A visual reviewer may not create a blocking loop with no actionable issue."""
    normalized = dict(review) if isinstance(review, dict) else {}
    issues = normalized.get("issues") if isinstance(normalized.get("issues"), list) else []
    section_ids = {"method", "experiments", "related_work", "introduction", "limitations", "conclusion"}
    def is_noop(action: str) -> bool:
        return (
            action in {"", "no change needed", "no change is needed"}
            or action.startswith("no change required")
            or action.startswith("optional")
            or action.startswith("optionally")
            or action.startswith("consider ")
        )

    actionable = []
    for raw_issue in issues:
        if not isinstance(raw_issue, dict):
            continue
        action_text = str(raw_issue.get("required_change") or "").strip().casefold().rstrip(".!;:")
        if is_noop(action_text):
            continue
        issue = dict(raw_issue)
        explicit_kind = str(issue.get("target_kind") or "").strip().casefold()
        explicit_id = str(issue.get("target_id") or "").strip()
        prose_target = _paragraph_section_target(issue, section_ids)
        if explicit_kind == "section" and explicit_id in section_ids:
            issue["section_id"] = explicit_id
            issue.pop("asset_id", None)
        elif explicit_kind == "asset" and explicit_id:
            if prose_target and not _visual_issue_requires_asset_change(issue):
                section_id, paragraph_id = prose_target
                issue.update({
                    "target_kind": "section", "target_id": section_id,
                    "section_id": section_id, "paragraph_id": paragraph_id,
                    "related_asset_id": explicit_id,
                })
                issue.pop("asset_id", None)
            else:
                issue["asset_id"] = explicit_id
        else:
            if prose_target:
                section_id, paragraph_id = prose_target
                related_asset_id = str(issue.get("asset_id") or "").strip()
                issue["target_kind"] = "section"
                issue["target_id"] = section_id
                issue["section_id"] = section_id
                issue["paragraph_id"] = paragraph_id
                if related_asset_id:
                    issue["related_asset_id"] = related_asset_id
                issue.pop("asset_id", None)
            elif issue.get("asset_id"):
                issue["target_kind"] = "asset"
                issue["target_id"] = str(issue["asset_id"])
        # Final visual review uses the same bounded-repair machinery as the
        # other reviewers.  Supply a deterministic evidence locator when the
        # model omitted one so repeated issues can be detected and adjudicated.
        if issue.get("asset_id"):
            issue.setdefault("evidence_kind", "asset")
            issue.setdefault("evidence_id", str(issue["asset_id"]))
        elif issue.get("section_id"):
            issue.setdefault("evidence_kind", "section")
            issue.setdefault("evidence_id", str(issue.get("paragraph_id") or issue["section_id"]))
        actionable.append(issue)
    normalized["issues"] = actionable
    verdict = str(normalized.get("verdict") or "").casefold()
    if verdict == "revise" and not actionable:
        normalized["verdict"] = "pass"
    elif verdict not in {"pass", "revise", "failed"}:
        normalized["verdict"] = "failed"
        normalized["issues"] = actionable or [{"severity": "blocker", "message": "Visual reviewer returned an invalid verdict.", "required_change": "Return one of pass, revise, or failed."}]
    return normalized


def _frontmatter_errors(front: dict) -> list[str]:
    """Keep title layout within the conference template's readable range."""
    if not isinstance(front, dict):
        return ["Formatter returned a non-object front matter payload."]
    title = str(front.get("title") or "").strip()
    if not title:
        return ["Formatter returned an empty title."]
    words = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", title)
    errors: list[str] = []
    if len(words) > 14:
        errors.append(f"Formatter title has {len(words)} words; maximum is 14.")
    # A 100-character hard stop rejects ordinary technical titles even when
    # the template wraps them safely. Keep a layout guard without making a
    # two-character preference a failed paper-generation run.
    if len(title) > 140:
        errors.append(f"Formatter title has {len(title)} characters; maximum is 140.")
    if "\n" in title or "\\" in title:
        errors.append("Formatter title contains a forbidden line break or LaTeX command.")
    abstract = str(front.get("abstract") or "").strip()
    abstract_words = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", abstract)
    if len(abstract_words) < 180:
        errors.append(f"Formatter abstract has {len(abstract_words)} words; publication contract requires at least 180.")
    if len(abstract_words) > 250:
        errors.append(f"Formatter abstract has {len(abstract_words)} words; maximum is 250.")
    return errors


def _fit_title_to_layout(title: str, *, max_words: int = 14, max_characters: int = 140) -> str:
    """Apply only a last-resort, word-boundary title layout fit.

    Formatter is always asked to repair first.  This fallback neither creates
    a scientific claim nor changes evidence: it removes forbidden line breaks,
    normalises whitespace, and cuts only at a word boundary when a provider
    repeatedly ignores an explicit layout constraint.
    """
    compact = re.sub(r"\s+", " ", str(title or "").replace("\\", " ")).strip()
    words = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", compact)
    if len(words) > max_words:
        compact = " ".join(words[:max_words])
    if len(compact) <= max_characters:
        return compact
    prefix = compact[:max_characters].rsplit(" ", 1)[0].strip()
    return prefix or compact[:max_characters].strip()


def _recoverable_frontmatter_errors(errors: list[str]) -> bool:
    """True only for bounded presentation/length defects after LLM repair."""
    recoverable = (
        "Formatter title has", "Formatter title contains a forbidden",
        "Formatter abstract has",
    )
    return bool(errors) and all(str(error).startswith(recoverable) for error in errors)


def _draft_release_enabled(args: dict) -> bool:
    """Whether non-blocking editorial findings may ship as a marked draft.

    Truth and provenance validation stay mandatory in both modes.  ``strict``
    is for a publication-ready release; the default ``draft`` mode avoids
    throwing away an otherwise valid PDF merely because bounded editorial
    refinement did not satisfy every stylistic reviewer preference.
    """
    policy = str(args.get("release_policy") or os.getenv("WRITING_RELEASE_POLICY", "draft")).strip().casefold()
    return policy != "strict"


def _has_blocking_review_issue(reviews: dict[str, dict]) -> bool:
    for review in reviews.values():
        if not isinstance(review, dict):
            continue
        if str(review.get("verdict") or "").casefold() == "failed":
            return True
        for issue in review.get("issues") or []:
            if isinstance(issue, dict) and str(issue.get("severity") or "").casefold() in {"blocker", "fatal", "critical"}:
                return True
    return False


def _draft_review_warnings(reviews: dict[str, dict], *, label: str) -> list[str]:
    warnings: list[str] = []
    for role, review in reviews.items():
        if not isinstance(review, dict):
            continue
        for issue in review.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            message = str(issue.get("message") or issue.get("required_change") or "editorial revision requested").strip()
            if message:
                warnings.append(f"draft_release[{label}:{role}]: {message}")
    return list(dict.fromkeys(warnings))


@_terminalized
async def run(args: dict) -> dict:
    from scripts.load_inputs import load_inputs
    from scripts.evidence import build_inventory, write_blueprint, write_evidence_ledgers
    from scripts.assets import write_asset_manifest
    from scripts.visuals import build_visual_capability_catalog, normalize_visual_plan, summarize_visual_evidence
    inputs, errors = load_inputs(args["module1"], args["module2"], args["module3"], args.get("source_manifest"))
    if errors:
        return {"status": "load_inputs_failed", "errors": errors}
    out = Path(args["output_dir"]); out.mkdir(parents=True, exist_ok=True)
    allow_draft_release = _draft_release_enabled(args)
    draft_warnings: list[str] = []
    phase("输入清点")
    inventory = build_inventory({k: args[k] for k in ("module1", "module2", "module3")}, inputs["m3"], inputs.get("source_manifest"))
    ledgers = write_evidence_ledgers(out, inputs, inventory)
    blueprint = write_blueprint(out, inputs, inventory)
    readiness = _read_ledger(ledgers["paper_readiness"])
    execution_integrity = _read_ledger(ledgers["execution_integrity"])
    early_artifacts = [*ledgers.values(), *blueprint.values()]
    if execution_integrity.get("status") != "valid":
        return {"status": "invalid_execution_evidence", "output_dir": str(out), "artifacts": early_artifacts, "warnings": [item.get("code", "invalid_execution_evidence") for item in execution_integrity.get("findings", [])], "execution_integrity": execution_integrity}
    if readiness.get("status") != "ready":
        actions = readiness.get("recommended_actions") or []
        status = "needs_experiment_data" if "replan_experiment" in actions else "partial"
        return {
            "status": status,
            "output_dir": str(out),
            "artifacts": early_artifacts,
            "warnings": [item.get("message", "Paper readiness check failed.") for item in readiness.get("findings", [])],
            "paper_readiness": readiness,
        }
    from scripts._subagent import call_agent_async
    from scripts.paper_contract import (
        apply_architect_plan, build_chapter_contract, build_paper_contract, conflict_report,
        execution_alignment_conflicts,
        validate_argument_map,
        validate_integration_handoff, validate_paper_contract, validate_writer_response,
        validate_writer_response_layers,
        write_chapter_handoff, write_json,
    )
    phase("论文合同")
    paper_contract = build_paper_contract(
        inputs, inventory, readiness, _read_ledger(ledgers["claims"]),
        _read_ledger(ledgers["measurements"]), _read_ledger(ledgers["bibliography"]), _read_ledger(ledgers["execution_alignment"]),
    )
    contract_errors = validate_paper_contract(paper_contract)
    if contract_errors:
        return {"status": "partial", "output_dir": str(out), "artifacts": early_artifacts, "warnings": contract_errors}
    immutable_contract = paper_contract
    architect_history: list[dict] = []
    architect_errors: list[str] = []
    previous_architect_fingerprint: str | None = None
    raw_architect: dict = {}
    for architect_attempt in range(3):
        architect_payload = {"paper_contract": immutable_contract}
        if architect_errors:
            architect_payload.update({
                "task": "repair_argument_map",
                "prior_response": raw_architect,
                "validation_errors": architect_errors,
                "protocol_repair_session": _REPAIR_SESSION_TOKEN,
                "protocol_repair_attempt": architect_attempt,
            })
        raw_architect = await call_agent_async("paper_architect", architect_payload, phase="论文合同")
        candidate_contract, current_errors = apply_architect_plan(immutable_contract, raw_architect)
        current_errors.extend(validate_argument_map(candidate_contract))
        architect_history.append({"attempt": architect_attempt + 1, "raw": raw_architect, "errors": current_errors})
        paper_contract, architect_errors = candidate_contract, current_errors
        if not architect_errors:
            break
        fingerprint = _fingerprint(raw_architect)
        if fingerprint == previous_architect_fingerprint:
            break
        previous_architect_fingerprint = fingerprint
    contract_path = write_json(out / "contracts" / "paper_contract.json", paper_contract)
    architect_review_path = write_json(out / "reviews" / "paper_architect.json", {
        "raw": raw_architect, "errors": architect_errors, "attempts": architect_history,
    })
    early_artifacts.extend([contract_path, architect_review_path])
    if architect_errors:
        return {"status": "partial", "output_dir": str(out), "artifacts": early_artifacts, "warnings": architect_errors}
    # Two dedicated visual agents operate before prose: the planner selects
    # evidence-supported visual intents, then the reviewer checks the realized
    # manifest.  Only registered templates can reach the renderer.
    phase("图表能力清点")
    visual_catalog = build_visual_capability_catalog(inputs, inventory["evidence_mode"])
    claim_statuses = {str(item.get("id")): item for item in _read_ledger(ledgers["claims"]).get("claims") or [] if isinstance(item, dict)}
    visual_catalog["claims"] = [{**claim, "status": (claim_statuses.get(str(claim.get("id"))) or {}).get("status")} for claim in visual_catalog.get("claims") or []]
    visual_evidence_summary = summarize_visual_evidence(inputs)
    visual_catalog_path = out / "assets" / "visual_capability_catalog.json"
    visual_catalog_path.parent.mkdir(parents=True, exist_ok=True)
    visual_catalog_path.write_text(json.dumps(visual_catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    phase("图表规划")
    raw_visual_plan = await call_agent_async(
        "visual_planner", {"task": "plan_visuals", "blueprint": _read_ledger(blueprint["02_blueprint"]),
                           "claim_ledger": _read_ledger(ledgers["claims"]), "visual_capability_catalog": visual_catalog,
                           "visual_evidence_summary": visual_evidence_summary},
        phase="图表规划",
    )
    visual_plan = normalize_visual_plan(raw_visual_plan, visual_catalog)
    visual_plan_path = out / "assets" / "visual_plan.json"
    visual_plan_path.write_text(json.dumps({"schema_version": 1, "agent_output": raw_visual_plan, "assets": visual_plan}, ensure_ascii=False, indent=2), encoding="utf-8")
    phase("图表生成")
    manifest = write_asset_manifest(out, inputs, visual_plan=visual_plan, capability_catalog=visual_catalog)
    phase("图表审阅")
    raw_visual_review = await call_agent_async(
        "visual_reviewer", {"visual_plan": visual_plan, "asset_manifest": _read_ledger(manifest),
                            "visual_capability_catalog": visual_catalog, "measurements": _read_ledger(ledgers["measurements"])},
        phase="图表审阅",
    )
    visual_review = _normalize_visual_review(raw_visual_review)
    visual_review_path = out / "reviews" / "visual_review.json"; visual_review_path.parent.mkdir(exist_ok=True)
    visual_review_path.write_text(json.dumps({"raw": raw_visual_review, "normalized": visual_review}, ensure_ascii=False, indent=2), encoding="utf-8")
    visual_repairs = 0
    visual_revision_no_progress = False
    prior_visual_manifest_fingerprint = _fingerprint(_read_ledger(manifest))
    while (str(visual_review.get("verdict") or "pass") == "revise"
           and visual_repairs < 3 and visual_review.get("issues")):
        visual_repairs += 1
        phase("图表定点修订")
        revised_raw_plan = await call_agent_async(
            "visual_planner", {"task": "revise_visual_plan", "prior_plan": visual_plan,
                               "review": visual_review, "visual_capability_catalog": visual_catalog,
                               "visual_evidence_summary": visual_evidence_summary},
            phase="图表定点修订",
        )
        visual_plan = normalize_visual_plan(revised_raw_plan, visual_catalog)
        visual_plan_path.write_text(json.dumps({"schema_version": 1, "agent_output": revised_raw_plan, "assets": visual_plan}, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest = write_asset_manifest(out, inputs, visual_plan=visual_plan, capability_catalog=visual_catalog)
        current_visual_manifest_fingerprint = _fingerprint(_read_ledger(manifest))
        raw_visual_review = await call_agent_async(
            "visual_reviewer", {"visual_plan": visual_plan, "asset_manifest": _read_ledger(manifest),
                                "visual_capability_catalog": visual_catalog, "measurements": _read_ledger(ledgers["measurements"])},
            phase="图表复审",
        )
        visual_review = _normalize_visual_review(raw_visual_review)
        visual_review_path.write_text(json.dumps({"raw": raw_visual_review, "normalized": visual_review}, ensure_ascii=False, indent=2), encoding="utf-8")
        if (str(visual_review.get("verdict") or "pass") == "revise"
                and current_visual_manifest_fingerprint == prior_visual_manifest_fingerprint):
            visual_revision_no_progress = True
            break
        prior_visual_manifest_fingerprint = current_visual_manifest_fingerprint
    if str(visual_review.get("verdict") or "pass") == "failed":
        return {"status": "partial", "output_dir": str(out), "artifacts": [*ledgers.values(), *blueprint.values(), str(visual_catalog_path), str(visual_plan_path), manifest, str(visual_review_path)], "warnings": ["Visual reviewer found blocking evidence defects."]}
    if str(visual_review.get("verdict") or "pass") == "revise":
        warning = (
            "Visual revision produced no material asset-manifest change; stopped instead of repeating the same repair."
            if visual_revision_no_progress else
            f"Visual revisions were not resolved after {visual_repairs} targeted revision(s)."
        )
        if not allow_draft_release or _has_blocking_review_issue({"visual_reviewer": visual_review}):
            return {"status": "partial", "output_dir": str(out), "artifacts": [*ledgers.values(), *blueprint.values(), str(visual_catalog_path), str(visual_plan_path), manifest, str(visual_review_path)], "warnings": [warning]}
        draft_warnings.append(f"draft_release[visual_reviewer]: {warning}")
    evidence_context = {
        "claims": _read_ledger(ledgers["claims"]),
        "measurements": _read_ledger(ledgers["measurements"]),
        "bibliography": _read_ledger(ledgers["bibliography"]),
        "bibliography_gaps": _read_ledger(ledgers["bibliography_gaps"]),
        "paper_readiness": _read_ledger(ledgers["paper_readiness"]),
        "asset_manifest": _read_ledger(manifest),
    }
    artifacts = [*early_artifacts, str(visual_catalog_path), str(visual_plan_path), manifest, str(visual_review_path)]
    if inventory["evidence_mode"] == "unverified":
        return {"status": "needs_experiment_data", "output_dir": str(out), "artifacts": artifacts, "warnings": inventory["warnings"]}
    phase("章节合同")
    assets = _read_ledger(manifest).get("assets") or []
    section_ids = ("method", "experiments", "related_work", "introduction", "limitations", "conclusion")
    chapter_contracts = {}
    for section_id in section_ids:
        chapter_contract = build_chapter_contract(paper_contract, section_id, assets)
        chapter_contracts[section_id] = chapter_contract
        write_json(out / "contracts" / "chapters" / f"{section_id}.json", chapter_contract)
    artifacts.extend(str(out / "contracts" / "chapters" / f"{section_id}.json") for section_id in section_ids)
    phase("专职分节写作")
    sections: dict = {}
    handoffs: dict = {}
    sections_dir = out / "sections"; handoffs_dir = out / "handoffs"
    sections_dir.mkdir(exist_ok=True); handoffs_dir.mkdir(exist_ok=True)
    writer_by_section = {
        "method": "method_writer", "experiments": "results_writer",
        "related_work": "related_work_writer", "introduction": "introduction_writer",
        "limitations": "limitations_writer", "conclusion": "conclusion_writer",
    }

    async def request_section(section_id: str, prior_handoffs: dict) -> tuple[str, dict]:
        payload = {
            "task": "write_section", "section_id": section_id, "chapter_contract": chapter_contracts[section_id],
            "paper_contract": paper_contract, "prior_handoffs": prior_handoffs,
            "acceptance_checklist": chapter_contracts[section_id].get("quality_requirements") or {},
        }
        result = await call_agent_async(
            writer_by_section[section_id],
            payload,
            phase="专职分节写作",
        )
        # A role can produce fluent prose while confusing a claim ID with a
        # bibliography ID or a threshold with a compared method.  Give that
        # same owner one narrow, evidence-located repair before allowing a
        # structural error to abort the whole manuscript.
        protocol_errors, quality_errors = validate_writer_response_layers(result, chapter_contracts[section_id])
        # Give the section owner two bounded chances to repair schema/source
        # syntax.  Real models occasionally repeat a malformed claim-as-cite
        # marker once even when the first repair request identifies it.
        protocol_repairs = 0
        previous_repair_request: str | None = None
        while protocol_errors and protocol_repairs < 4 and isinstance(result.get("section"), dict):
            repair_basis = {
                **payload, "task": "revise_section", "section": result["section"],
                "actions": [{"target_kind": "section", "target_id": section_id, "required_change": error} for error in protocol_errors],
            }
            repair_request = _fingerprint(repair_basis)
            if repair_request == previous_repair_request:
                break
            previous_repair_request = repair_request
            protocol_repairs += 1
            repair_payload = {
                **repair_basis,
                # Protocol-invalid responses must not be permanently replayed
                # from a previous failed run's exact-call journal.
                "protocol_repair_session": _REPAIR_SESSION_TOKEN,
                "protocol_repair_attempt": protocol_repairs,
            }
            result = await call_agent_async(
                writer_by_section[section_id],
                repair_payload,
                phase="章节合同修复",
            )
            protocol_errors, quality_errors = validate_writer_response_layers(result, chapter_contracts[section_id])
        # Quality failures are not malformed protocol. Ask only for the
        # deficient paragraphs or missing rhetorical roles, merge that small
        # patch deterministically, and revalidate after every bounded attempt.
        quality_repairs = 0
        prior_quality_fingerprint = _fingerprint(result)
        while not protocol_errors and quality_errors and quality_repairs < 4:
            quality_repairs += 1
            patch = await call_agent_async(
                writer_by_section[section_id],
                {
                    "task": "repair_section_quality",
                    "section_id": section_id,
                    "section": result.get("section"),
                    "chapter_handoff": result.get("chapter_handoff"),
                    "chapter_contract": chapter_contracts[section_id],
                    "quality_errors": quality_errors,
                    "response_contract": {
                        "paragraph_updates": "list of complete replacement paragraph objects for existing paragraph_id values",
                        "append_paragraphs": "list of complete new paragraph objects for missing required roles",
                        "chapter_handoff": "optional updated handoff; omit to preserve the current handoff",
                        "forbidden": "do not return or rewrite unaffected paragraphs",
                    },
                    "protocol_repair_session": _REPAIR_SESSION_TOKEN,
                    "quality_repair_attempt": quality_repairs,
                },
                phase="章节质量定点修复",
            )
            merged = _apply_section_quality_patch(section_id, result, patch)
            merged_fingerprint = _fingerprint(merged)
            if merged_fingerprint == prior_quality_fingerprint:
                break
            result = merged
            prior_quality_fingerprint = merged_fingerprint
            protocol_errors, quality_errors = validate_writer_response_layers(result, chapter_contracts[section_id])
        if isinstance(result, dict):
            result["_validation"] = {
                "protocol_errors": protocol_errors,
                "quality_errors": quality_errors,
                "protocol_repairs_used": protocol_repairs,
                "quality_repairs_used": quality_repairs,
            }
        return section_id, result

    def record_section(section_id: str, result: dict) -> list[str]:
        protocol_errors, quality_errors = validate_writer_response_layers(result, chapter_contracts[section_id])
        protocol_path = write_json(out / "reviews" / f"{section_id}_writer_protocol.json", {
            "role": writer_by_section[section_id],
            "section_id": section_id,
            "response_top_level_keys": sorted(result) if isinstance(result, dict) else [],
            "section_keys": sorted(result.get("section")) if isinstance(result.get("section"), dict) else [],
            "handoff_keys": sorted(result.get("chapter_handoff")) if isinstance(result.get("chapter_handoff"), dict) else [],
            "protocol_errors": protocol_errors,
            "quality_errors": quality_errors,
            "repair_summary": result.get("_validation") if isinstance(result, dict) else None,
            "worker_error": result.get("_worker_error") if isinstance(result, dict) else None,
        })
        artifacts.append(protocol_path)
        if protocol_errors:
            return protocol_errors
        if quality_errors:
            return [f"section_quality_exhausted: {error}" for error in quality_errors]
        sections[section_id] = result["section"]
        target = sections_dir / f"{section_id}.json"
        target.write_text(json.dumps(sections[section_id], ensure_ascii=False, indent=2), encoding="utf-8")
        handoff_path, handoff_errors = write_chapter_handoff(
            handoffs_dir / f"{section_id}.json", sections[section_id], result["chapter_handoff"], chapter_contracts[section_id],
        )
        handoffs[section_id] = _read_ledger(handoff_path)
        artifacts.extend([str(target), handoff_path])
        return handoff_errors

    def writer_failure(errors: list[str]) -> dict:
        quality_exhausted = bool(errors) and all(str(error).startswith("section_quality_exhausted:") for error in errors)
        return {
            "status": "partial", "output_dir": str(out), "artifacts": artifacts,
            "warnings": errors,
            "verdict": "section_quality_exhausted" if quality_exhausted else "writer_protocol_failed",
        }

    async def request_section_safe(section_id: str, prior_handoffs: dict) -> tuple[str, dict]:
        """Preserve branch failures that Jiuwen ``parallel`` otherwise maps to None.

        The workflow engine deliberately isolates parallel branches and returns
        ``None`` when one raises.  Returning a typed protocol-invalid payload
        keeps the other branches bounded while making the real failure visible
        in the ordinary writer protocol report and terminal status.
        """
        try:
            return await request_section(section_id, prior_handoffs)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            log(f"section writer branch failed: section={section_id}; reason={reason}")
            return section_id, {"_worker_error": reason}

    # The factual contract makes these three drafts independent.  Their
    # handoffs are the explicit synchronization point for the later chapters.
    initial_sections = ("method", "experiments", "related_work")
    initial_results = await parallel([
        lambda section_id=section_id: request_section_safe(section_id, {})
        for section_id in initial_sections
    ])
    for index, branch_result in enumerate(initial_results):
        if not isinstance(branch_result, tuple) or len(branch_result) != 2:
            section_id = initial_sections[index]
            result = {"_worker_error": "parallel branch returned no typed result"}
        else:
            section_id, result = branch_result
        writer_errors = record_section(section_id, result)
        if writer_errors:
            return writer_failure(writer_errors)
    for section_id in ("introduction", "limitations", "conclusion"):
        _, result = await request_section(section_id, handoffs)
        writer_errors = record_section(section_id, result)
        if writer_errors:
            return writer_failure(writer_errors)
    phase("全文整合")
    integration_payload = {"paper_contract": paper_contract, "sections": sections, "handoffs": handoffs}
    async def request_valid_integration(payload: dict, phase_name: str) -> tuple[dict, list[str]]:
        """Repair protocol/schema errors with a bounded no-progress guard."""
        integrated_value = await call_agent_async("integration_editor", payload, phase=phase_name)
        errors = validate_integration_handoff(integrated_value, paper_contract)
        previous = _fingerprint(integrated_value)
        previous_request: str | None = None
        repairs = 0
        while errors and repairs < 3 and isinstance(integrated_value, dict) and isinstance(integrated_value.get("sections"), dict):
            repair_basis = {
                **payload, "task": "revise_integration", "prior_integration": integrated_value,
                "actions": [{"target_kind": "section", "target_id": "integration", "required_change": error} for error in errors],
            }
            repair_request = _fingerprint(repair_basis)
            if repair_request == previous_request:
                break
            previous_request = repair_request
            repairs += 1
            repaired = await call_agent_async(
                "integration_editor",
                {
                    **repair_basis,
                    "protocol_repair_session": _REPAIR_SESSION_TOKEN,
                    "protocol_repair_attempt": repairs,
                },
                phase="整合合同修复",
            )
            fingerprint = _fingerprint(repaired)
            integrated_value = repaired
            errors = validate_integration_handoff(integrated_value, paper_contract)
            if errors and fingerprint == previous:
                break
            previous = fingerprint
        return integrated_value, errors

    integrated, integration_errors = await request_valid_integration(integration_payload, "全文整合")
    integration_protocol_path = write_json(out / "reviews" / "integration_editor_protocol.json", {
        "response_top_level_keys": sorted(integrated) if isinstance(integrated, dict) else [],
        "section_ids": sorted(integrated.get("sections")) if isinstance(integrated, dict) and isinstance(integrated.get("sections"), dict) else [],
        "validation_errors": integration_errors,
    })
    artifacts.append(integration_protocol_path)
    if integration_errors:
        return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": integration_errors}
    if isinstance(integrated.get("sections"), dict):
        sections = integrated["sections"]
    for section_id, section in sections.items():
        (sections_dir / f"{section_id}.json").write_text(json.dumps(section, ensure_ascii=False, indent=2), encoding="utf-8")
    integration_path = write_json(out / "handoffs" / "integration.json", integrated)
    artifacts.append(integration_path)
    alignment_conflicts = execution_alignment_conflicts(paper_contract.get("execution_alignment"))
    preliminary_conflicts = conflict_report(handoffs, integrated, evidence_conflicts=alignment_conflicts)
    phase("专项审稿")
    review_roles = ("claim_verifier", "literature_novelty_reviewer", "argument_reviewer")
    reviews: dict[str, dict] = {}
    for role in review_roles:
        raw_review = await call_agent_async(role, {"paper_contract": paper_contract, "sections": sections, "handoffs": handoffs, "contract_conflicts": preliminary_conflicts, "evidence": evidence_context, "asset_manifest": _read_ledger(manifest)}, phase="专项审稿")
        reviews[role] = _normalize_review(raw_review, role, paper_contract)
        path = write_json(out / "reviews" / f"{role}.json", {"raw": raw_review, "normalized": reviews[role]})
        artifacts.append(path)
    final_visual_raw = await call_agent_async("visual_reviewer", {"task": "review_final_paper_relationships", "sections": sections, "paper_contract": paper_contract, "asset_manifest": _read_ledger(manifest)}, phase="图文联合审查")
    reviews["visual_reviewer_final"] = _normalize_visual_review(final_visual_raw)
    final_visual_path = write_json(out / "reviews" / "visual_reviewer_final.json", {"raw": final_visual_raw, "normalized": reviews["visual_reviewer_final"]})
    artifacts.append(final_visual_path)
    if any(str(review.get("verdict") or "failed") == "failed" for review in reviews.values()):
        return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["A specialized reviewer found a blocking defect."], "verdict": "failed"}
    phase("修订协调")
    coordination = await call_agent_async("revision_coordinator", {"reviews": reviews, "paper_contract": paper_contract, "sections": sections, "contract_conflicts": preliminary_conflicts}, phase="修订协调")
    coordination = _normalize_coordination(coordination, reviews)
    coordinator_path = write_json(out / "reviews" / "revision_coordinator.json", coordination)
    conflicts = conflict_report(handoffs, integrated, coordination, evidence_conflicts=alignment_conflicts)
    conflict_path = write_json(out / "contracts" / "contract_conflicts.json", conflicts)
    artifacts.extend([coordinator_path, conflict_path])
    rounds_used = 0
    coordination_verdict = str(coordination.get("verdict") or "failed")
    if coordination_verdict == "failed":
        return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Revision coordinator found unresolved conflicting review requirements."], "verdict": "failed", "rounds_used": rounds_used}
    if coordination_verdict not in {"pass", "revise"}:
        return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Revision coordinator returned an invalid verdict."], "verdict": "failed", "rounds_used": rounds_used}
    if conflicts["unresolved_conflict_ids"]:
        return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Unresolved paper-contract conflicts: " + ", ".join(conflicts["unresolved_conflict_ids"])], "verdict": "failed", "rounds_used": rounds_used}
    if coordination_verdict == "pass" and any(str(review.get("verdict") or "failed") != "pass" for review in reviews.values()):
        if not allow_draft_release or _has_blocking_review_issue(reviews):
            return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Revision coordinator passed despite unresolved specialized review."], "verdict": "failed", "rounds_used": rounds_used}
        # The coordinator supplied no safe, located repair action.  Preserve
        # the reviewer finding in the release record rather than inventing a
        # broad rewrite or looping on the same manuscript.
        draft_warnings.extend(_draft_review_warnings(reviews, label="unassigned_initial_review"))
    revision_limit = max(0, int(args.get("max_revision_rounds", 2)))
    prior_section_fingerprint = _fingerprint(sections)
    prior_issue_signatures = _issue_signatures(reviews)
    adjudicated_locators: set[tuple] = set()
    active_repair_contracts: list[dict] = []
    while coordination_verdict == "revise":
        if rounds_used >= revision_limit:
            if not allow_draft_release or _has_blocking_review_issue(reviews):
                return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Specialized revisions exceeded the configured round limit."], "verdict": "revise_exhausted", "rounds_used": rounds_used}
            draft_warnings.extend(_draft_review_warnings(reviews, label="revision_limit"))
            coordination_verdict = "pass"
            break
        phase("定点修订")
        changed_sections = 0
        changed_assets = 0
        manifest_payload = _read_ledger(manifest)
        asset_ids = {str(item.get("id")) for item in manifest_payload.get("assets") or [] if isinstance(item, dict) and item.get("id")}
        raw_actions = [action for action in coordination.get("actions") or [] if isinstance(action, dict)]
        explicit_section_targets = {
            str(action.get("target_id") or action.get("section_id") or "").strip()
            for action in raw_actions
            if str(action.get("target_kind") or "").strip() == "section"
        }
        section_actions: list[dict] = []
        asset_actions: list[dict] = []
        invalid_targets: list[str] = []
        for action in raw_actions:
            target_kind = str(action.get("target_kind") or "").strip()
            target_id = str(action.get("target_id") or action.get("section_id") or action.get("asset_id") or "").strip()
            # Compatibility for existing coordinators: infer an unambiguous target,
            # but never silently ignore an unknown identifier.
            if not target_kind:
                target_kind = "section" if target_id in sections else "asset" if target_id in asset_ids else ""
            if target_kind == "section" and target_id in sections:
                section_actions.append({**action, "target_id": target_id})
            elif target_kind == "asset" and target_id in asset_ids:
                if _asset_action_is_verification_only(action):
                    # The manifest is already authoritative; this action exists
                    # only because an older coordinator duplicated a prose
                    # repair under its related asset.  Do not regenerate it.
                    continue
                prose_target = _paragraph_section_target(action, set(sections))
                if prose_target:
                    section_id, paragraph_id = prose_target
                    if section_id not in explicit_section_targets:
                        section_actions.append({
                            **action, "target_kind": "section", "target_id": section_id,
                            "paragraph_id": paragraph_id, "related_asset_id": target_id,
                        })
                    continue
                asset_actions.append({**action, "target_id": target_id})
            else:
                invalid_targets.append(target_id or "<missing>")
        if invalid_targets:
            return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Revision coordinator named an unknown or untyped target: " + ", ".join(sorted(set(invalid_targets)))], "verdict": "invalid_revision_target", "rounds_used": rounds_used}
        action_targets = {("section", str(action["target_id"])) for action in section_actions}
        action_targets.update(("asset", str(action["target_id"])) for action in asset_actions)
        pre_revision_section_fingerprints = {
            section_id: _fingerprint(section)
            for section_id, section in sections.items()
            if section_id in {target_id for target_kind, target_id in action_targets if target_kind == "section"}
        }
        progress = {
            "round": rounds_used + 1,
            "action_targets": sorted(target_id for target_kind, target_id in action_targets if target_kind == "section"),
            "asset_targets": sorted(target_id for target_kind, target_id in action_targets if target_kind == "asset"),
            "input_issue_fingerprints": sorted((_signature_record(signature) for signature in prior_issue_signatures), key=lambda item: (item["reviewer"], item["target_kind"], item["target_id"], item["paragraph_id"], item["evidence_kind"], item["evidence_id"])),
            "section_changes": [],
            "asset_manifest_changed": False,
        }
        if asset_actions:
            phase("图表定点修订")
            revised_raw_plan = await call_agent_async(
                "visual_planner",
                {"task": "revise_visual_plan", "prior_plan": {"assets": visual_plan}, "review": {"verdict": "revise", "issues": asset_actions},
                 "visual_capability_catalog": visual_catalog, "visual_evidence_summary": visual_evidence_summary},
                phase="图表定点修订",
            )
            revised_visual_plan = normalize_visual_plan(revised_raw_plan, visual_catalog)
            previous_manifest = _fingerprint(manifest_payload)
            visual_plan = revised_visual_plan
            manifest = write_asset_manifest(out, inputs, visual_plan=visual_plan, capability_catalog=visual_catalog)
            updated_manifest = _read_ledger(manifest)
            progress["asset_manifest_changed"] = _fingerprint(updated_manifest) != previous_manifest
            artifacts.append(str(manifest))
            if not progress["asset_manifest_changed"]:
                progress_path = write_json(out / "reviews" / f"revision_progress.{rounds_used + 1}.json", progress)
                artifacts.append(progress_path)
                if not allow_draft_release or _has_blocking_review_issue(reviews):
                    return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Visual revision produced no asset-manifest change."], "verdict": "revision_no_progress", "rounds_used": rounds_used}
                draft_warnings.extend(_draft_review_warnings(reviews, label="unchanged_visual_manifest"))
                coordination_verdict = "pass"
                break
            changed_assets = 1
            chapter_contracts = {section_id: build_chapter_contract(paper_contract, section_id, updated_manifest.get("assets") or []) for section_id in writer_by_section}
            # The visual plan, manifest, chapter contracts and reviewer evidence
            # are one versioned snapshot.  Refresh every consumer atomically after
            # a visual repair so reviewers never validate new prose against an
            # obsolete asset list.
            evidence_context["asset_manifest"] = updated_manifest
            visual_plan_path.write_text(json.dumps({"schema_version": 1, "agent_output": revised_raw_plan, "assets": visual_plan}, ensure_ascii=False, indent=2), encoding="utf-8")
            for section_id, contract in chapter_contracts.items():
                write_json(out / "contracts" / "chapters" / f"{section_id}.json", contract)
        for action in section_actions:
            section_id = str(action["target_id"])
            previous_section = sections[section_id]
            revised = await call_agent_async(
                writer_by_section[section_id],
                {"task": "revise_section", "section_id": section_id, "section": sections[section_id],
                 "chapter_contract": chapter_contracts[section_id], "paper_contract": paper_contract,
                 "review_actions": action.get("issues") or [action], "prior_handoffs": handoffs},
                phase="定点修订",
            )
            revision_protocol_errors, revision_quality_errors = validate_writer_response_layers(
                revised, chapter_contracts[section_id],
            )
            if revision_protocol_errors:
                return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": revision_protocol_errors, "rounds_used": rounds_used}
            # Targeted revisions may accidentally shorten an argument unit.
            # That is a repairable quality defect, not malformed inter-agent
            # data, so use the same bounded paragraph patch protocol as the
            # initial section-writing path.
            quality_repairs = 0
            while revision_quality_errors and quality_repairs < 3:
                quality_repairs += 1
                patch = await call_agent_async(
                    writer_by_section[section_id],
                    {
                        "task": "repair_section_quality",
                        "section_id": section_id,
                        "section": revised.get("section"),
                        "chapter_handoff": revised.get("chapter_handoff"),
                        "chapter_contract": chapter_contracts[section_id],
                        "quality_errors": revision_quality_errors,
                        "response_contract": {
                            "paragraph_updates": "complete replacement objects for only deficient paragraph_id values",
                            "append_paragraphs": "complete objects only for missing required rhetorical roles",
                            "forbidden": "do not rewrite unaffected paragraphs",
                        },
                        "protocol_repair_session": _REPAIR_SESSION_TOKEN,
                        "quality_repair_attempt": quality_repairs,
                    },
                    phase="修订章节质量定点修复",
                )
                revised = _apply_section_quality_patch(section_id, revised, patch)
                revision_protocol_errors, revision_quality_errors = validate_writer_response_layers(
                    revised, chapter_contracts[section_id],
                )
                if revision_protocol_errors:
                    return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": revision_protocol_errors, "rounds_used": rounds_used}
            if revision_quality_errors:
                if not allow_draft_release or _has_blocking_review_issue(reviews):
                    return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": revision_quality_errors, "rounds_used": rounds_used}
                # Retain the last valid section rather than replacing it with
                # an incomplete cosmetic rewrite.  The outstanding request is
                # visible in draft warnings for the next editorial pass.
                draft_warnings.extend(
                    f"draft_release[revision_quality:{section_id}]: {error}"
                    for error in revision_quality_errors
                )
                continue
            if isinstance(revised.get("section"), dict):
                sections[section_id] = revised["section"]
                changed = _fingerprint(previous_section) != _fingerprint(sections[section_id])
                progress["section_changes"].append({"section_id": section_id, "changed": changed})
                if not changed:
                    continue
                (sections_dir / f"{section_id}.json").write_text(json.dumps(sections[section_id], ensure_ascii=False, indent=2), encoding="utf-8")
                handoff_path, handoff_errors = write_chapter_handoff(handoffs_dir / f"{section_id}.json", sections[section_id], revised.get("chapter_handoff") or {}, chapter_contracts[section_id])
                handoffs[section_id] = _read_ledger(handoff_path)
                artifacts.extend([handoff_path, str(sections_dir / f"{section_id}.json")])
                changed_sections += 1
                if handoff_errors:
                    return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": handoff_errors, "rounds_used": rounds_used}
        if not changed_sections and not changed_assets:
            progress_path = write_json(out / "reviews" / f"revision_progress.{rounds_used + 1}.json", progress)
            artifacts.append(progress_path)
            if not allow_draft_release or _has_blocking_review_issue(reviews):
                return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Revision produced no changed target section; stopping instead of spending another review round."], "verdict": "revision_no_progress", "rounds_used": rounds_used}
            draft_warnings.extend(_draft_review_warnings(reviews, label="unchanged_targeted_revision"))
            coordination_verdict = "pass"
            break
        rounds_used += 1
        phase("修订后整合")
        integrated, integration_errors = await request_valid_integration(
            {"paper_contract": paper_contract, "sections": sections, "handoffs": handoffs},
            "修订后整合",
        )
        integration_protocol_path = write_json(out / "reviews" / f"integration_editor_protocol.after_revision.{rounds_used}.json", {
            "response_top_level_keys": sorted(integrated) if isinstance(integrated, dict) else [],
            "section_ids": sorted(integrated.get("sections")) if isinstance(integrated, dict) and isinstance(integrated.get("sections"), dict) else [],
            "validation_errors": integration_errors,
        })
        artifacts.append(integration_protocol_path)
        if integration_errors:
            return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": integration_errors, "rounds_used": rounds_used}
        if isinstance(integrated.get("sections"), dict):
            sections = integrated["sections"]
        current_section_fingerprint = _fingerprint(sections)
        progress["integrated_sections_changed"] = current_section_fingerprint != prior_section_fingerprint
        changed_target_refs = {
            ("section", section_id)
            for section_id, before in pre_revision_section_fingerprints.items()
            if section_id in sections and _fingerprint(sections[section_id]) != before
        }
        if changed_assets:
            changed_target_refs.update(("asset", target_id) for target_kind, target_id in action_targets if target_kind == "asset")
        progress["changed_targets"] = [
            {"target_kind": target_kind, "target_id": target_id}
            for target_kind, target_id in sorted(changed_target_refs)
        ]
        progress_path = write_json(out / "reviews" / f"revision_progress.{rounds_used}.json", progress)
        artifacts.append(progress_path)
        if not progress["integrated_sections_changed"] and not changed_assets:
            if not allow_draft_release or _has_blocking_review_issue(reviews):
                return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Integration left all targeted sections unchanged after revision."], "verdict": "revision_no_progress", "rounds_used": rounds_used}
            draft_warnings.extend(_draft_review_warnings(reviews, label="unchanged_integration"))
            coordination_verdict = "pass"
            break
        for section_id, section in sections.items():
            (sections_dir / f"{section_id}.json").write_text(json.dumps(section, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts.append(write_json(out / "handoffs" / f"integration.after_revision.{rounds_used}.json", integrated))
        alignment_conflicts = execution_alignment_conflicts(paper_contract.get("execution_alignment"))
        preliminary_conflicts = conflict_report(handoffs, integrated, evidence_conflicts=alignment_conflicts)
        phase("专项复审")
        for role in review_roles:
            raw_review = await call_agent_async(role, {"paper_contract": paper_contract, "sections": sections, "handoffs": handoffs, "contract_conflicts": preliminary_conflicts, "evidence": evidence_context, "active_repair_contracts": active_repair_contracts, "asset_manifest": _read_ledger(manifest)}, phase="专项复审")
            reviews[role] = _normalize_review(raw_review, role, paper_contract)
            artifacts.append(write_json(out / "reviews" / f"{role}.after_revision.json", {"raw": raw_review, "normalized": reviews[role]}))
        final_visual_raw = await call_agent_async("visual_reviewer", {"task": "review_final_paper_relationships", "sections": sections, "paper_contract": paper_contract, "asset_manifest": _read_ledger(manifest)}, phase="图文联合复审")
        reviews["visual_reviewer_final"] = _normalize_visual_review(final_visual_raw)
        artifacts.append(write_json(out / "reviews" / "visual_reviewer_final.after_revision.json", {"raw": final_visual_raw, "normalized": reviews["visual_reviewer_final"]}))
        if any(str(review.get("verdict") or "failed") == "failed" for review in reviews.values() if isinstance(review, dict)):
            return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["A specialized reviewer found a blocking defect after revision."], "verdict": "failed", "rounds_used": rounds_used}
        current_issue_signatures = _issue_signatures(reviews)
        repeated_targeted = _repeated_targeted_issues(
            current_issue_signatures, prior_issue_signatures, changed_target_refs,
        )
        # A finding that repeats immediately after its stated target changed
        # must first receive one bounded, evidence-located repair contract.
        # If that contract has already been applied and the same signature
        # survives, stop: a second generic rewrite would be unbounded churn.
        stuck = sorted(repeated_targeted)
        if stuck:
            progress["repeated_issue_targets"] = sorted({signature[2] for signature in stuck})
            progress["repeated_issue_fingerprints"] = sorted((_signature_record(signature) for signature in stuck), key=lambda item: (item["reviewer"], item["target_kind"], item["target_id"], item["paragraph_id"], item["evidence_kind"], item["evidence_id"]))
            if any(signature[:7] in adjudicated_locators for signature in stuck):
                progress["adjudication_outcome"] = "repair_applied_but_same_issue_persisted"
                progress["review_quality_follow_up"] = "The bounded repair contract was applied and the same evidence-located issue persisted; stop for reviewer-quality or evidence-sufficiency adjudication."
                artifacts.append(write_json(out / "reviews" / f"revision_progress.{rounds_used}.json", progress))
                if not allow_draft_release or _has_blocking_review_issue(reviews):
                    return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["The same evidence-located issue persisted after its bounded repair contract was applied."], "verdict": "revision_no_progress", "rounds_used": rounds_used}
                draft_warnings.extend(_draft_review_warnings(reviews, label="repeated_nonblocking_issue"))
                coordination_verdict = "pass"
                break
            phase("重复问题裁决")
            raw_adjudication = await call_agent_async(
                "revision_adjudicator",
                {
                    "paper_contract": paper_contract,
                    "sections": sections,
                    "handoffs": handoffs,
                    "repeated_issue_fingerprints": [_signature_record(signature) for signature in stuck],
                    "prior_reviews": reviews,
                    "evidence": evidence_context,
                },
                phase="重复问题裁决",
            )
            adjudication = _normalize_adjudication(raw_adjudication, set(stuck))
            adjudication_path = write_json(out / "reviews" / f"revision_adjudication.{rounds_used}.json", {
                "raw": raw_adjudication,
                "normalized": adjudication,
                "repeated_issue_fingerprints": [_signature_record(signature) for signature in stuck],
            })
            artifacts.append(adjudication_path)
            progress["adjudication_path"] = adjudication_path
            progress["adjudication_outcome"] = str(adjudication.get("verdict") or "failed")
            if adjudication.get("verdict") != "repair":
                progress["review_quality_follow_up"] = str(adjudication.get("reason") or "Adjudication did not produce a valid bounded repair contract.")
                artifacts.append(write_json(out / "reviews" / f"revision_progress.{rounds_used}.json", progress))
                if not allow_draft_release or _has_blocking_review_issue(reviews):
                    return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": [progress["review_quality_follow_up"]], "verdict": str(adjudication.get("verdict") or "failed"), "rounds_used": rounds_used}
                draft_warnings.extend(_draft_review_warnings(reviews, label="unrepairable_nonblocking_review"))
                coordination_verdict = "pass"
                break
            adjudicated_locators.update(signature[:7] for signature in stuck)
            active_repair_contracts = list(adjudication["repairs"])
            progress["review_quality_follow_up"] = "A one-time bounded repair contract was issued for each repeated evidence-located issue; re-review its acceptance criteria before any further coordination."
            artifacts.append(write_json(out / "reviews" / f"revision_progress.{rounds_used}.json", progress))
            prior_section_fingerprint = current_section_fingerprint
            prior_issue_signatures = current_issue_signatures
            coordination = {"verdict": "revise", "actions": _adjudicated_actions(adjudication), "conflicts": []}
            artifacts.append(write_json(out / "reviews" / f"revision_coordinator.adjudicated.{rounds_used}.json", coordination))
            continue
        prior_section_fingerprint = current_section_fingerprint
        prior_issue_signatures = current_issue_signatures
        phase("修订复核协调")
        coordination = await call_agent_async("revision_coordinator", {"reviews": reviews, "paper_contract": paper_contract, "sections": sections, "contract_conflicts": preliminary_conflicts}, phase="修订复核协调")
        coordination = _normalize_coordination(coordination, reviews)
        artifacts.append(write_json(out / "reviews" / f"revision_coordinator.after_revision.{rounds_used}.json", coordination))
        conflicts = conflict_report(handoffs, integrated, coordination, evidence_conflicts=alignment_conflicts)
        conflict_path = write_json(out / "contracts" / "contract_conflicts.json", conflicts)
        coordination_verdict = str(coordination.get("verdict") or "failed")
        if coordination_verdict not in {"pass", "revise"} or conflicts["unresolved_conflict_ids"]:
            return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Revision coordination left an invalid or unresolved contract conflict."], "verdict": "failed", "rounds_used": rounds_used}
        if coordination_verdict == "pass" and any(str(review.get("verdict") or "failed") != "pass" for review in reviews.values() if isinstance(review, dict)):
            if not allow_draft_release or _has_blocking_review_issue(reviews):
                return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Revision coordinator passed despite unresolved specialized review."], "verdict": "failed", "rounds_used": rounds_used}
            draft_warnings.extend(_draft_review_warnings(reviews, label="unassigned_post_revision_review"))
    phase("标题与摘要")
    front = await call_agent_async("formatter", {"sections": sections, "paper_contract": paper_contract, "evidence": evidence_context}, phase="标题与摘要")
    front_errors = _frontmatter_errors(front)
    # A title/abstract length defect is local formatting work, not a reason
    # to discard verified sections or restart the whole workflow. Give the
    # formatter two explicitly measured, schema-preserving repair attempts.
    front_repairs = 0
    while front_errors and front_repairs < 3:
        front_repairs += 1
        revised_front = await call_agent_async(
            "formatter",
            {
                "task": "repair_frontmatter", "front": front,
                "frontmatter_errors": front_errors, "sections": sections,
                "paper_contract": paper_contract, "evidence": evidence_context,
                "constraints": {
                    "max_title_words": 14, "max_title_characters": 140,
                    "abstract_word_range": [180, 250], "preserve_supported_scope": True,
                },
            },
            phase="标题摘要定点修复",
        )
        if not isinstance(revised_front, dict):
            break
        front = revised_front
        front_errors = _frontmatter_errors(front)
    (out / "stages" / "06_title_abstract.json").write_text(
        json.dumps({**front, "_validation": {"errors": front_errors, "repair_attempts": front_repairs}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if front_errors:
        if not allow_draft_release or not _recoverable_frontmatter_errors(front_errors):
            return {"status": "partial", "output_dir": str(out), "artifacts": artifacts + [str(out / "stages" / "06_title_abstract.json")], "warnings": front_errors, "verdict": "frontmatter_layout_failed", "rounds_used": rounds_used}
        original_title = str(front.get("title") or "")
        fitted_title = _fit_title_to_layout(original_title)
        if fitted_title:
            front["title"] = fitted_title
        remaining_errors = _frontmatter_errors(front)
        # A title can be made renderer-safe deterministically.  A slightly
        # short/long abstract cannot be safely invented in Python, so retain
        # it as an explicit editorial follow-up after Formatter has tried.
        draft_warnings.extend(
            f"draft_release[frontmatter]: {error}" for error in remaining_errors
        )
        if original_title != fitted_title:
            draft_warnings.append(
                "draft_release[frontmatter]: Formatter title was fitted to the layout limit after bounded repair attempts."
            )
    # Persist the effective front matter (including any renderer-safe title
    # fitting) rather than the first formatter attempt, so recovery and audit
    # tools inspect exactly what the PDF renderer receives.
    (out / "stages" / "06_title_abstract.json").write_text(
        json.dumps({**front, "_validation": {"errors": _frontmatter_errors(front), "repair_attempts": front_repairs}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    from scripts.structured_render import audit_sections, render
    phase("受控渲染")
    audit_errors = audit_sections(
        sections, ledgers["bibliography"], ledgers["claims"], manifest,
        ledgers["measurements"], front=front,
    )
    graph_audit_path = out / "reviews" / "evidence_graph_audit.json"
    graph_audit_path.write_text(json.dumps({"passed": not audit_errors, "errors": audit_errors}, ensure_ascii=False, indent=2), encoding="utf-8")
    if audit_errors:
        return {"status": "partial", "output_dir": str(out), "artifacts": artifacts + [str(out / "sections"), str(graph_audit_path)], "warnings": audit_errors, "verdict": "controlled_audit_failed", "rounds_used": rounds_used}
    phase("终稿审查")
    raw_final_review = await call_agent_async(
        "final_paper_reviewer",
        {"paper_contract": paper_contract, "sections": sections, "front": front, "reviews": reviews, "asset_manifest": _read_ledger(manifest)},
        phase="终稿审查",
    )
    final_review = _normalize_review(raw_final_review)
    final_review_path = write_json(out / "reviews" / "final_paper_reviewer.json", {"raw": raw_final_review, "normalized": final_review})
    artifacts.extend([str(graph_audit_path), final_review_path])
    if str(final_review.get("verdict") or "failed") != "pass":
        if not allow_draft_release or _has_blocking_review_issue({"final_paper_reviewer": final_review}):
            return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Final paper review did not pass."], "verdict": str(final_review.get("verdict") or "failed"), "rounds_used": rounds_used}
        draft_warnings.extend(_draft_review_warnings({"final_paper_reviewer": final_review}, label="final_review"))
    title = args.get("title_override") or "Anonymous Research Submission"
    from scripts.bibtex import write_refs_bib
    refs_bib = write_refs_bib(ledgers["bibliography"], str(out))
    tex = render(
        sections,
        front.get("title") or title,
        ledgers["bibliography"],
        str(args.get("conference") or "iclr2024"),
        front.get("abstract") or "",
        manifest,
    )
    from scripts.stage7_compile import compile_paper
    phase("Tectonic 编译")
    compiled = compile_paper(tex, str(out), conference=str(args.get("conference") or "iclr2024"))
    from scripts.pdf_validation import validate_pdf
    validation = validate_pdf(compiled.get("pdf_path"), out) if compiled.get("status") == "success" else {"passed": False}
    status = "synthetic_test" if inventory["evidence_mode"] == "synthetic" else ("success" if validation["passed"] else "partial")
    warnings = ([] if validation.get("passed") else validation.get("errors", [])) + draft_warnings
    return {"status": status, "output_dir": str(out), "pdf_path": compiled.get("pdf_path"), "artifacts": artifacts + [str(out / "sections"), refs_bib, compiled.get("tex_path", ""), str(out / "pdf_validation.json")], "warnings": warnings, "verdict": "pass", "rounds_used": rounds_used, "release_mode": "draft_with_warnings" if draft_warnings else "strict_pass"}


@_terminalized
async def finalize_existing(args: dict) -> dict:
    """Continue a frozen, specialist-approved manuscript through final gates.

    This recovery path is intentionally narrow: it never regenerates prose,
    assets, reviews, or the abstract. It is valid only after an interrupted
    run has already written those artifacts. It re-runs deterministic evidence
    audit under the current rules, obtains a real final-paper review of that
    exact version, and then renders and validates its PDF.
    """
    from scripts._subagent import call_agent_async
    from scripts.paper_contract import write_json
    from scripts.structured_render import audit_sections, render
    from scripts.bibtex import write_refs_bib
    from scripts.stage7_compile import compile_paper
    from scripts.pdf_validation import validate_pdf

    out = Path(args["output_dir"])
    allow_draft_release = _draft_release_enabled(args)
    draft_warnings: list[str] = []
    required = {
        "paper_contract": out / "contracts" / "paper_contract.json",
        "bibliography": out / "evidence" / "bibliography.json",
        "claims": out / "evidence" / "claims.json",
        "measurements": out / "evidence" / "measurements.json",
        "manifest": out / "assets" / "manifest.json",
        "front": out / "stages" / "06_title_abstract.json",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    sections_dir = out / "sections"
    if not sections_dir.is_dir():
        missing.append("sections")
    if missing:
        return {"status": "partial", "output_dir": str(out), "warnings": ["Cannot resume finalization; missing frozen artifacts: " + ", ".join(missing)], "verdict": "resume_artifacts_missing"}
    sections = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(sections_dir.glob("*.json"))
    }
    paper_contract = _read_ledger(str(required["paper_contract"]))
    manifest = str(required["manifest"])
    front_record = _read_ledger(str(required["front"]))
    # Accept short-lived wrapper files from earlier workflow versions, while
    # persisting the normal title/abstract schema for future recovery runs.
    front = front_record.get("front") if isinstance(front_record.get("front"), dict) else front_record
    front_evidence = {
        "claims": _read_ledger(str(required["claims"])),
        "measurements": _read_ledger(str(required["measurements"])),
        "bibliography": _read_ledger(str(required["bibliography"])),
        "asset_manifest": _read_ledger(manifest),
    }
    if args.get("refresh_front"):
        phase("标题与摘要")
        front = await call_agent_async(
            "formatter",
            {
                "sections": sections,
                "paper_contract": paper_contract,
                "evidence": front_evidence,
            },
            phase="标题与摘要",
        )
    front_errors = _frontmatter_errors(front)
    front_repairs = 0
    while front_errors and front_repairs < 3:
        front_repairs += 1
        repaired_front = await call_agent_async(
            "formatter",
            {
                "task": "repair_frontmatter", "front": front,
                "frontmatter_errors": front_errors, "sections": sections,
                "paper_contract": paper_contract, "evidence": front_evidence,
                "constraints": {
                    "max_title_words": 14, "max_title_characters": 140,
                    "abstract_word_range": [180, 250], "preserve_supported_scope": True,
                },
            },
            phase="标题摘要定点修复",
        )
        if not isinstance(repaired_front, dict):
            break
        front = repaired_front
        front_errors = _frontmatter_errors(front)
    if front_errors:
        if not allow_draft_release or not _recoverable_frontmatter_errors(front_errors):
            return {"status": "partial", "output_dir": str(out), "warnings": front_errors, "verdict": "frontmatter_layout_failed"}
        original_title = str(front.get("title") or "")
        fitted_title = _fit_title_to_layout(original_title)
        if fitted_title:
            front["title"] = fitted_title
        draft_warnings.extend(f"draft_release[frontmatter]: {error}" for error in _frontmatter_errors(front))
        if original_title != fitted_title:
            draft_warnings.append("draft_release[frontmatter]: Formatter title was fitted to the layout limit after bounded repair attempts.")
    (out / "stages" / "06_title_abstract.json").write_text(
        json.dumps({**front, "_validation": {"errors": _frontmatter_errors(front), "repair_attempts": front_repairs}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # A resumed finalization may not turn an interrupted draft into a release
    # merely because a new final reviewer says "pass".  Reconstruct the latest
    # decision for every specialist and require all four domains to have passed
    # the exact frozen manuscript before compilation.
    required_reviewers = (
        "claim_verifier", "literature_novelty_reviewer", "argument_reviewer",
        "visual_reviewer_final",
    )
    specialist_reviews: dict[str, dict] = {}
    missing_reviews: list[str] = []
    nonpassing_reviews: dict[str, dict] = {}
    for role in required_reviewers:
        candidates = [out / "reviews" / f"{role}.after_revision.json", out / "reviews" / f"{role}.json"]
        record = next((_read_ledger(str(path)) for path in candidates if path.is_file()), {})
        normalized = record.get("normalized") if isinstance(record, dict) else None
        if not isinstance(normalized, dict):
            missing_reviews.append(role)
            continue
        specialist_reviews[role] = normalized
        if str(normalized.get("verdict") or "failed") != "pass":
            nonpassing_reviews[role] = normalized
    if missing_reviews:
        return {
            "status": "partial", "output_dir": str(out),
            "warnings": ["Cannot resume delivery; specialist review is missing: " + ", ".join(missing_reviews)],
            "verdict": "resume_specialist_review_missing",
        }
    if nonpassing_reviews:
        if not allow_draft_release or _has_blocking_review_issue(nonpassing_reviews):
            return {
                "status": "partial", "output_dir": str(out),
                "warnings": ["Cannot resume delivery; a specialist review has a blocking unresolved issue."],
                "verdict": "resume_specialist_review_blocked",
            }
        draft_warnings.extend(_draft_review_warnings(nonpassing_reviews, label="resume_specialist_review"))
    artifacts: list[str] = []
    phase("受控渲染")
    audit_errors = audit_sections(
        sections, required["bibliography"], required["claims"], manifest,
        required["measurements"], front=front,
    )
    graph_audit_path = out / "reviews" / "evidence_graph_audit.json"
    graph_audit_path.write_text(json.dumps({"passed": not audit_errors, "errors": audit_errors, "resumed": True}, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append(str(graph_audit_path))
    if audit_errors:
        return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": audit_errors, "verdict": "controlled_audit_failed"}
    phase("终稿审查")
    raw_final_review = await call_agent_async(
        "final_paper_reviewer",
        {"paper_contract": paper_contract, "sections": sections, "front": front, "reviews": specialist_reviews, "asset_manifest": _read_ledger(manifest)},
        phase="终稿审查",
    )
    final_review = _normalize_review(raw_final_review)
    final_review_path = write_json(out / "reviews" / "final_paper_reviewer.json", {"raw": raw_final_review, "normalized": final_review, "resumed": True})
    artifacts.append(final_review_path)
    if str(final_review.get("verdict") or "failed") != "pass":
        if not allow_draft_release or _has_blocking_review_issue({"final_paper_reviewer": final_review}):
            return {"status": "partial", "output_dir": str(out), "artifacts": artifacts, "warnings": ["Final paper review did not pass."], "verdict": str(final_review.get("verdict") or "failed")}
        draft_warnings.extend(_draft_review_warnings({"final_paper_reviewer": final_review}, label="resume_final_review"))
    phase("Tectonic 编译")
    refs_bib = write_refs_bib(str(required["bibliography"]), str(out))
    tex = render(
        sections,
        str(args.get("title_override") or front.get("title") or "Anonymous Research Submission"),
        required["bibliography"],
        str(args.get("conference") or "iclr2024"),
        str(front.get("abstract") or ""),
        manifest,
    )
    compiled = compile_paper(tex, str(out), conference=str(args.get("conference") or "iclr2024"))
    validation = validate_pdf(compiled.get("pdf_path"), out) if compiled.get("status") == "success" else {"passed": False, "errors": ["PDF compiler did not return success."]}
    inventory_path = out / "stages" / "00_input_inventory.json"
    inventory = _read_ledger(str(inventory_path)) if inventory_path.is_file() else {}
    status = "synthetic_test" if inventory.get("evidence_mode") == "synthetic" else ("success" if validation.get("passed") else "partial")
    finalization_path = write_json(out / "reviews" / "finalization_resume.json", {
        "status": status, "controlled_audit_passed": True, "final_review_verdict": final_review.get("verdict"),
        "pdf_validation_passed": bool(validation.get("passed")), "resumed": True,
    })
    artifacts.extend([refs_bib, compiled.get("tex_path", ""), str(out / "pdf_validation.json"), finalization_path])
    warnings = ([] if validation.get("passed") else validation.get("errors", [])) + draft_warnings
    return {"status": status, "output_dir": str(out), "pdf_path": compiled.get("pdf_path"), "artifacts": artifacts, "warnings": warnings, "verdict": "pass" if validation.get("passed") else "pdf_validation_failed", "rounds_used": 1, "release_mode": "draft_with_warnings" if draft_warnings else "strict_pass"}
