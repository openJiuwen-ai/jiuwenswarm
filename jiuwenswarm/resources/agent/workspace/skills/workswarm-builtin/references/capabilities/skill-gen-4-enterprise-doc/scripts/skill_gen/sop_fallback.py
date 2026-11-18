"""Intent-only fallback when no SOP plain text is available.

Recovery paths (paste text, URL fetch, local file) stay primary when the user
can supply a document. When there is still no source body, use
:func:`build_intent_fallback_sop`: **by default** pass ``invoke_llm_json``
whenever the runtime can invoke the model—it builds a deterministic
:class:`SOPStructure` plus Markdown ``raw_text``, then always runs
:func:`enrich_fallback_sop_with_llm` for that call. Pass ``invoke_llm_json=None``
(or use :func:`build_fallback_sop_structure` alone) **only** when the system
cannot run an LLM for this step (tests, offline, unwired host, policy).
Downstream drafting sees the same fields as LLM extraction (title, purpose,
scope, roles, steps, knowledge_items, sections, branches, exceptions,
references).
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from typing import Any, Callable

from .models import SOPStep, SOPStructure

logger = logging.getLogger(__name__)

_MAX_RAW_TEXT_IN_ENRICH_PROMPT = 8000

_ENRICH_FALLBACK_PROMPT = """\
You refine a **draft SOP JSON** that was built **without** an authoritative source document (intent-only template).

## Rules (strict)

- Output **only** one JSON object with the same top-level keys as the input draft: \
title, purpose, scope, sop_type, roles, steps, knowledge_items, sections, \
decision_points, exceptions, references, raw_text.
- Improve wording for clarity; align steps and knowledge_items with the stated intent where obvious.
- **Do not** invent numeric SLAs, approval limits, legal deadlines, fines, system URLs, or \
 company-specific policy not present in the draft JSON or the **Stated intent** block below.
- If a field would require guessing policy detail, keep the draft’s cautious / TBD wording or \
 leave lists shorter rather than fabricating.
- steps[].step_number must be JSON strings (e.g. "1", "2a").
- raw_text should be Markdown that reflects the refined structure (can shorten boilerplate).

## Stated intent (verbatim, may be partial)

{user_intent}

## Draft JSON (refine this)

{draft_json}

Output JSON only, no markdown fences or commentary."""


def _draft_dict_for_enrich_prompt(sop: SOPStructure) -> dict[str, Any]:
    data = sop.to_dict()
    rt = str(data.get("raw_text", ""))
    if len(rt) > _MAX_RAW_TEXT_IN_ENRICH_PROMPT:
        data["raw_text"] = rt[: _MAX_RAW_TEXT_IN_ENRICH_PROMPT - 1].rstrip() + "…"
        data["_raw_text_truncated_for_enrich_prompt"] = True
    else:
        data.pop("_raw_text_truncated_for_enrich_prompt", None)
    return data

_MAX_INTENT_LINE_LEN = 220
_MAX_INTENT_STRUCTURED_LINES = 12

_NUMBERED_LINE = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")
_BULLET_LINE = re.compile(r"^\s*[-*•]\s+(.+)$")

_POLICY_LEXICON_KEYWORDS = (
    "sla",
    "policy",
    "compliance",
    "regulation",
    "audit",
    "threshold",
    "retention",
    "gdpr",
    "phi",
    "pii",
)


def _infer_sop_type_from_intent(intent: str) -> tuple[str, str]:
    """Return ``(sop_type, reason_tag)`` using cheap heuristics (no LLM)."""
    if not intent.strip():
        return "hybrid", "empty_intent_default_hybrid"
    lines = intent.splitlines()
    numbered = 0
    for line in lines:
        if _NUMBERED_LINE.match(line):
            numbered += 1
    bullets = 0
    for line in lines:
        if _BULLET_LINE.match(line):
            bullets += 1
    lower = intent.lower()
    policy_lex = False
    for kw in _POLICY_LEXICON_KEYWORDS:
        if kw in lower:
            policy_lex = True
            break
    if policy_lex and numbered < 2 and bullets < 2:
        return "knowledge", "policy_lexicon_low_procedure_signal"
    if numbered >= 2 or (numbered + bullets) >= 4:
        if policy_lex:
            return "hybrid", "ordered_steps_plus_policy_language"
        return "procedural", "multiple_ordered_or_bullet_lines"
    return "hybrid", "default_hybrid"


def _structured_lines_from_intent(intent: str) -> list[str]:
    """Pull numbered / bullet lines out of free-form intent into short snippets."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in intent.splitlines():
        if len(out) >= _MAX_INTENT_STRUCTURED_LINES:
            break
        m_num = _NUMBERED_LINE.match(raw)
        if m_num:
            text = m_num.group(2).strip()
        else:
            m_bul = _BULLET_LINE.match(raw)
            if not m_bul:
                continue
            text = m_bul.group(1).strip()
        if len(text) < 3:
            continue
        if len(text) > _MAX_INTENT_LINE_LEN:
            text = text[: _MAX_INTENT_LINE_LEN - 1].rstrip() + "…"
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(f"From stated intent (unverified structure): {text}")
    return out


def _intent_title(user_intent: str, skill_name_hint: str) -> str:
    hint = (skill_name_hint or "").strip()
    if hint:
        words = hint.replace("-", " ").replace("_", " ").strip()
        return f"{words.title()} — procedure (draft from stated intent)"
    intent = (user_intent or "").strip()
    if intent:
        one_line = " ".join(intent.split())
        if len(one_line) > 90:
            one_line = one_line[:87].rstrip() + "…"
        return one_line
    return "User-defined procedure (intent-only draft)"


def build_fallback_sop_structure(
    *,
    user_intent: str = "",
    skill_name_hint: str = "",
) -> tuple[SOPStructure, dict[str, Any]]:
    """Return a well-formed ``SOPStructure`` when no SOP document exists yet.

    Use after optional recovery (file / URL / paste) is declined or unavailable.
    The object is intentionally explicit that content is a **template** filled
    from conversation, not extracted from an authoritative source.

    For async hosts and LLM polish in **one** call, prefer
    :func:`build_intent_fallback_sop` with ``invoke_llm_json`` whenever the
    runtime can invoke the model (deterministic skeleton, then LLM refine).

    Returns ``(sop, extraction_meta)`` with the same ``extraction_meta`` keys
    callers already expect from :func:`parse_sop_raw_text`, plus
    ``fallback_sop`` / ``fallback_reason`` for telemetry and prompts.
    """
    intent = (user_intent or "").strip()
    title = _intent_title(intent, skill_name_hint)
    sop_type, type_reason = _infer_sop_type_from_intent(intent)
    intent_snippets = _structured_lines_from_intent(intent)

    purpose = (
        "Operationalize the user's stated goal as an executable agent skill. "
        "Refine purpose, constraints, and success criteria using the conversation "
        "until they match what a formal SOP would specify."
    )
    if intent:
        purpose = (
            f"{purpose} Stated intent (verbatim, may be partial): {intent}"
        )

    scope = (
        "Applies to the workflow the user described in this session until a "
        "canonical policy or SOP document is attached. Out of scope: "
        "obligations not mentioned by the user unless required for safety "
        "or compliance—then surface as open questions in outputs."
    )

    roles = [
        "Process owner / requester (user)",
        "Executing agent",
        "Optional reviewers or approvers (name when known)",
    ]

    steps = [
        SOPStep(
            step_number="1",
            actor="User or agent",
            action=(
                "Confirm required inputs, systems, and deliverable format from "
                "the conversation or any supplied files."
            ),
            system="",
            output="Input checklist or explicit assumption list",
            notes="If blocking data is missing, prefer a short clarification over silent guesses.",
        ),
        SOPStep(
            step_number="2",
            actor="Agent",
            action="Execute the core workflow aligned to sections and decision points below.",
            system="Per skill tools and environment",
            output="Intermediate artifacts (draft tables, messages, file edits) as needed",
            notes="State provisional vs confirmed facts.",
        ),
        SOPStep(
            step_number="3",
            actor="Agent",
            action="Validate outcomes against acceptance criteria implied by the intent and any later attachments.",
            system="",
            output="Pass/fail with gaps",
            notes="Escalate ambiguities to the user.",
        ),
        SOPStep(
            step_number="4",
            actor="Agent",
            action="Produce final deliverables using the naming and structure conventions in generator-worker-spec.",
            system="",
            output="Final user-facing artifacts",
            notes="Include assumptions when the source SOP is still this template.",
        ),
    ]

    knowledge_items = [
        "This structure is a fallback skeleton, not an extracted enterprise SOP. "
        "Replace generic language with organization-specific rules when a real document arrives.",
        "Numeric SLAs, approval limits, legal text, and system IDs must not be invented; "
        "capture them from the user or from linked documentation.",
        "When the user pastes policy fragments or URLs later, merge them into knowledge_items "
        "and steps in a follow-up revision of the generated skill.",
    ]
    knowledge_items.extend(intent_snippets)

    sections = [
        {
            "id": "1",
            "title": "Purpose and outcomes",
            "content_summary": "Why the skill exists and what “done” means for this intent.",
        },
        {
            "id": "2",
            "title": "Scope and applicability",
            "content_summary": "Boundaries until a formal SOP is attached.",
        },
        {
            "id": "3",
            "title": "Roles and responsibilities",
            "content_summary": "Who supplies inputs, who executes, who approves.",
        },
        {
            "id": "4",
            "title": "Procedure",
            "content_summary": "Ordered steps; expand with real systems and approvals from the user.",
        },
        {
            "id": "5",
            "title": "Decision points and exceptions",
            "content_summary": "Branches, error paths, and when to stop for human input.",
        },
        {
            "id": "6",
            "title": "Deliverables and references",
            "content_summary": "Artifacts to hand back plus pointers to external policies.",
        },
    ]

    decision_points = [
        "Are mandatory inputs present? If not, pause for user clarification vs proceed with labeled assumptions.",
        "Does the task require human approval before external side effects (messages, tickets, payments)?",
    ]

    exceptions = [
        (
            "No authoritative SOP text in-session: "
            "treat all operational detail as provisional until a file or URL is provided."
        ),
        (
            "High-risk domains (finance, health, legal): require explicit user confirmation "
            "before irreversible actions."
        ),
    ]

    references = [
        "Conversation with the user",
        "Attachments, URLs, or tickets supplied later in the thread",
        "Internal policy library (when the user names it)",
    ]

    raw_lines = [
        f"# {title}",
        "",
        "## 1. Purpose",
        textwrap.fill(purpose, width=92),
        "",
        "## 2. Scope",
        textwrap.fill(scope, width=92),
        "",
        "## 3. Roles",
        "\n".join(f"- {r}" for r in roles),
        "",
        "## 4. Procedure",
    ]
    for st in steps:
        raw_lines.append(
            f"{st.step_number}. **{st.actor}** — {st.action} "
            f"(output: {st.output or 'n/a'})"
        )
        if st.notes:
            raw_lines.append(f"   - Note: {st.notes}")
    raw_lines.extend(
        [
            "",
            "## 5. Rules and standards (to be grounded in real policy)",
            "\n".join(f"- {k}" for k in knowledge_items),
            "",
            "## 6. Decision points",
            "\n".join(f"- {d}" for d in decision_points),
            "",
            "## 7. Exceptions",
            "\n".join(f"- {e}" for e in exceptions),
            "",
            "## 8. References",
            "\n".join(f"- {r}" for r in references),
        ]
    )
    raw_text = "\n".join(raw_lines)

    sop = SOPStructure(
        title=title,
        purpose=purpose,
        scope=scope,
        sop_type=sop_type,
        roles=roles,
        steps=steps,
        knowledge_items=knowledge_items,
        sections=sections,
        decision_points=decision_points,
        exceptions=exceptions,
        references=references,
        raw_text=raw_text,
    )

    meta: dict[str, Any] = {
        "source_path": "",
        "merge_warnings": ["intent_only_fallback_skeleton"],
        "weak_reasons": ["intent_only_no_source_document"],
        "use_raw_excerpt_draft_fallback": False,
        "fallback_sop": True,
        "fallback_reason": "no_document_intent_skeleton",
        "fallback_inferred_sop_type": sop_type,
        "fallback_sop_type_reason": type_reason,
        "fallback_intent_structured_line_count": len(intent_snippets),
        "output_counts": {
            "steps": len(sop.steps),
            "knowledge_items": len(sop.knowledge_items),
            "sections": len(sop.sections),
            "decision_points": len(sop.decision_points),
            "exceptions": len(sop.exceptions),
        },
    }
    return sop, meta


async def enrich_fallback_sop_with_llm(
    sop: SOPStructure,
    *,
    user_intent: str = "",
    invoke_llm_json: Callable[..., Any],
    trace_tag: str = "sop_fallback_enrich",
) -> tuple[SOPStructure, dict[str, Any]]:
    """Refine a fallback :class:`SOPStructure` with one LLM JSON call (internal step).

    Prefer :func:`build_intent_fallback_sop` (pass ``invoke_llm_json`` by
    default when the runtime can invoke the model) so deterministic build +
    enrich stay one call.
    This function remains
    for tests or custom pipelines. On empty/invalid model output, returns the
    input ``sop`` unchanged.

    ``invoke_llm_json`` must match the same contract as ``sop_parser`` (async
    callable returning a dict parsed from model JSON).
    """
    enrich_meta: dict[str, Any] = {
        "fallback_llm_enrich": False,
        "fallback_llm_enrich_trace": trace_tag,
    }
    draft = _draft_dict_for_enrich_prompt(sop)
    draft.pop("_raw_text_truncated_for_enrich_prompt", None)
    try:
        draft_json = json.dumps(draft, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        enrich_meta["merge_warnings"] = [f"fallback_llm_enrich_skipped_serialize_error:{exc}"]
        return sop, enrich_meta

    prompt = _ENRICH_FALLBACK_PROMPT.format(
        user_intent=(user_intent or "").strip() or "(none)",
        draft_json=draft_json,
    )
    try:
        data = await invoke_llm_json(prompt, fallback={}, trace_tag=trace_tag)
    except Exception as exc:
        logger.warning("[SOPFallback] enrich LLM call failed: %s", exc)
        enrich_meta["merge_warnings"] = [f"fallback_llm_enrich_failed:{type(exc).__name__}"]
        return sop, enrich_meta

    if not isinstance(data, dict) or not data:
        enrich_meta["merge_warnings"] = ["fallback_llm_enrich_empty_response"]
        return sop, enrich_meta

    refined = SOPStructure.from_dict(data)
    if not str(refined.raw_text or "").strip():
        refined.raw_text = sop.raw_text
    enrich_meta["fallback_llm_enrich"] = True
    enrich_meta["merge_warnings"] = []
    enrich_meta["output_counts"] = {
        "steps": len(refined.steps),
        "knowledge_items": len(refined.knowledge_items),
        "sections": len(refined.sections),
        "decision_points": len(refined.decision_points),
        "exceptions": len(refined.exceptions),
    }
    return refined, enrich_meta


async def build_intent_fallback_sop(
    *,
    user_intent: str = "",
    skill_name_hint: str = "",
    invoke_llm_json: Callable[..., Any] | None = None,
) -> tuple[SOPStructure, dict[str, Any]]:
    """Single entry point for intent-only SOP-shaped output (no source document).

    **Default:** pass ``invoke_llm_json`` whenever the runtime can invoke the
    model. This always runs the deterministic skeleton via
    :func:`build_fallback_sop_structure`, then calls
    :func:`enrich_fallback_sop_with_llm`. **Exception:** pass
    ``invoke_llm_json=None`` only when the system cannot call the model for this
    step—then the skeleton is returned unchanged.

    ``fallback_sop`` semantics in ``extraction_meta`` are unchanged either way.
    """
    sop, meta = build_fallback_sop_structure(
        user_intent=user_intent,
        skill_name_hint=skill_name_hint,
    )
    if invoke_llm_json is None:
        return sop, meta

    sop2, enrich_meta = await enrich_fallback_sop_with_llm(
        sop,
        user_intent=user_intent,
        invoke_llm_json=invoke_llm_json,
    )
    meta["fallback_llm_enrich"] = enrich_meta.get("fallback_llm_enrich", False)
    meta["fallback_llm_enrich_trace"] = enrich_meta.get("fallback_llm_enrich_trace", "")
    for w in enrich_meta.get("merge_warnings", []) or []:
        meta.setdefault("merge_warnings", []).append(w)
    if enrich_meta.get("fallback_llm_enrich"):
        meta["output_counts"] = enrich_meta.get("output_counts", meta.get("output_counts", {}))
    return sop2, meta
