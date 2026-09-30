# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scientific-verification rail provider for swarm provider-based team assembly.

Ports the EvoScientistOS deterministic verification gates from the SwarmFlow
mediator layer into a provider-mounted ``AgentRail`` so the same checks
protect *every* member model output, not only workflow-mediated steps.
Born from four live failure rounds (2026-09-11): writers drifted on ledger
id format, leaked ``[C-NNN]`` bookkeeping tokens into prose as if they were
citations, and emitted placeholder markers.

Checks are deterministic and side-effect free:
  1. bracketed ledger-id tokens (``[C-NNN]``) are stripped from prose —
     claim mappings are metadata and must never render into text;
  2. remaining ledger-id references (any ``C-NNN`` token) are matched against
     the approved-id set loaded from the team workspace claims ledger
     (status ``supported``/``frozen``);
  3. placeholder citation markers (``[?]``, ``[citation needed]``, ``TODO``,
     ``FIXME``, ``<ref>``) are flagged.

Strictness:
  - ``autopilot`` (default): the deterministic strip always runs; violations
    are logged and counted, the response passes through repaired.
  - ``full``: additionally pushes a steering message naming the violations
    and requests one more model iteration so the model fixes its own output.

The rail never raises: a rail must not break the model call it hooks.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    ElementKind,
    context_field,
    harness_element,
    param_field,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentRail,
    ModelCallInputs,
)

from jiuwenswarm.agents.swarm.context import SwarmBuildContext

logger = logging.getLogger(__name__)

# Provider name constants; namespaced under the shared "swarm." prefix.
RESEARCH_GATE = "swarm.research_gate"

# Deterministic check constants — mirror scripts/workflow.py of the
# evoresearch-swarm skill exactly, so both layers judge identically.
_LEDGER_ID_RE = re.compile(r"C-\d{3,}")
_LEDGER_ID_TOKEN_RE = re.compile(r"\[C-\d{3,}\]")
APPROVED_STATUSES = ("supported", "frozen")
_PLACEHOLDER_RES = (
    re.compile(r"\[\?\]"),
    re.compile(r"\[citation needed\]", re.IGNORECASE),
    re.compile(r"\bTODO\b"),
    re.compile(r"\bFIXME\b"),
    re.compile(r"<ref>"),
)


def strip_ledger_id_tokens(text: str) -> tuple[str, int]:
    """Remove bracketed ledger-id tokens leaked into prose.

    Mirrors ``_strip_ledger_ids`` in the evoresearch-swarm workflow: plain
    ``str.replace`` per unique id (no spacing inserted — the bracketed token
    simply vanishes, ``...[C-002].`` becomes ``....``).

    Returns:
        ``(cleaned_text, stripped_count)``.
    """
    out = str(text or "")
    found = _LEDGER_ID_TOKEN_RE.findall(out)
    for token in set(found):
        out = out.replace(token, "")
    return out, len(found)


def find_ledger_id_references(text: str) -> list[str]:
    """Return the sorted unique ledger ids referenced anywhere in *text*."""
    return sorted(set(_LEDGER_ID_RE.findall(str(text or ""))))


def find_placeholder_markers(text: str) -> list[str]:
    """Return the sorted unique placeholder citation markers in *text*."""
    out: set[str] = set()
    for rx in _PLACEHOLDER_RES:
        out.update(m.group(0) for m in rx.finditer(str(text or "")))
    return sorted(out)


def load_claims_ledger(path: str) -> list[dict]:
    """Parse a claims ledger yaml defensively.

    Accepts a bare list of claim dicts or a mapping holding the list under
    ``claims`` / ``entries`` / ``items``. Any parse or shape problem yields
    ``[]`` — the rail degrades to "every ledger reference is unmapped"
    instead of failing to mount.

    Returns:
        The claim dict list (possibly empty).
    """
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - defensive boundary by design
        logger.warning("[swarm.research_gate] claims ledger unreadable (%s): %s", path, exc)
        return []
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    if isinstance(data, dict):
        for key in ("claims", "entries", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [c for c in value if isinstance(c, dict)]
    return []


def approved_claim_ids(claims: list[dict]) -> set[str]:
    """Return ids whose status is approved for writing (``supported``/``frozen``)."""
    return {
        str(c.get("id"))
        for c in claims
        if isinstance(c, dict) and c.get("status") in APPROVED_STATUSES and c.get("id")
    }


def verify_scientific_text(text: str, approved_ids: set[str]) -> dict[str, Any]:
    """Run all deterministic checks on one model-output text.

    Pure function: no I/O, no mutation of *text*.

    Returns:
        ``{"cleaned_text", "stripped_ids", "unmapped_ids",
        "placeholder_markers"}`` — ``unmapped_ids`` are ledger references not
        in the approved set, evaluated on the *cleaned* text.
    """
    cleaned, stripped = strip_ledger_id_tokens(text)
    refs = find_ledger_id_references(cleaned)
    approved = {str(i) for i in (approved_ids or set())}
    return {
        "cleaned_text": cleaned,
        "stripped_ids": stripped,
        "unmapped_ids": sorted(r for r in refs if r not in approved),
        "placeholder_markers": find_placeholder_markers(cleaned),
    }


def _steering_message(report: dict[str, Any], source: str) -> str:
    parts = []
    if report["unmapped_ids"]:
        parts.append(
            "unmapped ledger ids (not in the approved claims set): "
            + ", ".join(report["unmapped_ids"])
        )
    if report["placeholder_markers"]:
        parts.append(
            "placeholder citation markers: " + ", ".join(report["placeholder_markers"])
        )
    return (
        "[{source}] scientific verification failed — {parts}. "
        "Remove ledger bookkeeping from prose, cite only approved claims, "
        "and replace every placeholder with a complete reference, then "
        "re-emit the corrected output."
    ).format(source=source, parts="; ".join(parts))


class ScientificVerificationRail(AgentRail):
    """Deterministic scientific-integrity gate on every member model output.

    Primary hook ``after_model_call``: the bracketed ledger-id strip is
    applied in place on the response (the same ``AssistantMessage`` object
    flows into the conversation, so an in-place content rewrite is the
    effective deterministic repair — same pattern as
    ``BrowserWorkingContextRail``). ``full`` strictness then pushes a
    steering message and requests one more ReAct iteration; ``autopilot``
    only logs. Never raises.
    """

    # VerificationRail tier: deterministic checks gate the model output
    # before lower-priority rails consume it.
    priority = 90

    def __init__(
        self,
        *,
        strictness: str = "autopilot",
        approved_ids: set[str] | None = None,
        source_label: str = RESEARCH_GATE,
    ) -> None:
        self._strictness = strictness if strictness in ("full", "autopilot") else "autopilot"
        self._approved_ids = {str(i) for i in (approved_ids or set())}
        self._source_label = source_label or RESEARCH_GATE

    @property
    def strictness(self) -> str:
        return self._strictness

    @property
    def approved_ids(self) -> set[str]:
        return set(self._approved_ids)

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ModelCallInputs):
            return
        response = inputs.response
        content = getattr(response, "content", None)
        if not isinstance(content, str) or not content:
            return

        report = verify_scientific_text(content, self._approved_ids)
        if report["stripped_ids"]:
            response.content = report["cleaned_text"]
            logger.info(
                "[%s] stripped %d bracketed ledger-id token(s) from model output",
                self._source_label,
                report["stripped_ids"],
            )
        if not report["unmapped_ids"] and not report["placeholder_markers"]:
            return

        detail = (
            "unmapped=" + ",".join(report["unmapped_ids"])
            + " placeholders=" + ",".join(report["placeholder_markers"])
        )
        if self._strictness == "full":
            ctx.push_steering(_steering_message(report, self._source_label))
            ctx.request_model_continue()
            logger.warning(
                "[%s] full mode: %s — steering pushed, one more iteration requested",
                self._source_label,
                detail,
            )
        else:
            logger.warning(
                "[%s] autopilot: %s — passing through after deterministic strip",
                self._source_label,
                detail,
            )


def _resolve_claims_path(team_ws_root: str, claims_file: str) -> str | None:
    """Locate the claims ledger under the team workspace root."""
    name = str(claims_file or "").strip() or "claims.yml"
    root = Path(team_ws_root)
    for candidate in (root / name, root / "workspace" / name):
        if candidate.is_file():
            return str(candidate)
    return None


class ResearchGateInput(ConstructionInput):
    """Construction inputs for the scientific verification rail."""

    strictness: str = param_field(
        default="",
        description="full|autopilot; empty defaults to autopilot "
        "(config_specs bakes react.research_gates.strictness into params).",
    )
    claims_file: str = param_field(
        default="claims.yml",
        description="Claims ledger file name resolved under the team workspace root.",
    )
    role: str | None = context_field(attr="role", description="Team role value.")
    team_ws_root: str | None = context_field(
        attr="team_ws_root", description="Team shared workspace root."
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=RESEARCH_GATE,
    description="Deterministic scientific-integrity checks on member model output "
    "(ledger-id leak strip, unmapped-claim detection, placeholder-citation detection).",
    input_model=ResearchGateInput,
)
def build_research_gate_rail(
    params: dict[str, Any],
    ctx: SwarmBuildContext,
) -> list[Any]:
    """Build the scientific verification rail for one team member.

    Mounting is decided at spec time: ``config_specs`` only emits this
    provider's ``RailSpec`` when ``react.research_gates.enabled`` is true and
    bakes the configured strictness into ``params`` (the swarm assembly
    doctrine — factories read params + build-context environment, not
    ``ctx.config``). The factory therefore mounts whenever a team workspace
    root exists, and degrades to ``[]`` on any build failure rather than
    breaking team assembly — the checks are a safety net, not a launch gate.

    Returns:
        ``[ScientificVerificationRail]``, or ``[]`` when unwired or failed.
    """
    if not ctx.team_ws_root:
        return []

    try:
        inp = ResearchGateInput.resolve(params, ctx)
        strictness = inp.strictness.strip().lower() or "autopilot"
        claims_path = _resolve_claims_path(ctx.team_ws_root, inp.claims_file)
        claims = load_claims_ledger(claims_path) if claims_path else []
        approved = approved_claim_ids(claims)
        rail = ScientificVerificationRail(
            strictness=strictness,
            approved_ids=approved,
            source_label=RESEARCH_GATE,
        )
        logger.info(
            "[%s] mounted for role=%s claims=%s approved=%d strictness=%s",
            RESEARCH_GATE,
            inp.role or "unknown",
            claims_path or "none",
            len(approved),
            rail.strictness,
        )
        return [rail]
    except Exception as exc:  # noqa: BLE001 - a rail must never break assembly
        logger.warning("[%s] build failed, mounting nothing: %s", RESEARCH_GATE, exc)
        return []
