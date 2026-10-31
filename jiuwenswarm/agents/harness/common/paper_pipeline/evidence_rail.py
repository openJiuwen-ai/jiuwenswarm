"""``PaperEvidenceRail``: the evidence contract as a JiuwenSwarm / openJiuwen harness rail.

A thin layer: every judgement comes from ``evidence.verify`` (and, in a revision,
``revision_state.evidence_check``) — the same deterministic check the execution audit, the revision
gate and ``acceptance.json`` use. The rail adds no statistics and no acceptance rule of its own; it
passes no design either: ``verify`` locates and checks the current design itself.

Where it is mounted (agent-core ``9e3390195``, the version ``uv.lock`` pins). The paper pipeline
builds a fresh ``DeepAgent`` per manager round (``ManagerAgent._create_agent``) and per reporting
round (``ReportingAgent._build_paper_agent``). ``install_paper_evidence_rail`` wraps those two
builders and queues the rail on each built agent through the framework's public
``DeepAgent.add_rail`` (once per agent), so it runs in every manager / reporting agent of the real
pipeline — it is not merely registered somewhere:

======================  ======================================  =====================================
point                   hook                                    behaviour
======================  ======================================  =====================================
experiment executed     host wrapper ``execution_audit``        the audit records the execution in
                        (``AFTER_RECORD_HOOKS``)                the evidence manifest; the rail logs
                                                                its identity and drops its cache
writing starts          reporting ``on_user_message``           prepends the verified-evidence
                        (``UserMessageInputs.parts``)           manifest and required limitations
DONE requested          manager ``before_tool_call`` on         without accepted evidence the call
                        ``submit_manager_decision``             is **skipped** (framework skip
                                                                mechanism, ``_skip_tool_calls``) and
                                                                the agent receives the pending tasks
======================  ======================================  =====================================

Limitation, stated plainly: in this agent-core version experiment execution is a host-side runner,
not a ``DeepAgent``, so there is no rail callback at "execution finished"; that point is the
existing host wrapper, which calls the same evidence service. Hard blocking of delivery remains the
host's ``acceptance.json`` (and the revision gate); the rail rejects DONE earlier and tells the
manager why, it does not replace those checks.

A check that cannot run (missing results, unreadable files, an exception) fails closed: DONE is
rejected as unverified. Only the rail's own log writing is best-effort.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.paper_pipeline import evidence

try:  # the rail base class of the pinned agent-core
    from openjiuwen.harness.rails.base import DeepAgentRail
except ImportError:  # pragma: no cover - unit tests without agent-core
    DeepAgentRail = object  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)
DECISION_TOOL = "submit_manager_decision"
LOG_FILE = "rail_log.jsonl"


def _unverified(code: str, detail: str) -> dict[str, Any]:
    """A check result that blocks: the evidence could not be judged, which is never a pass."""
    return evidence.finalize({"ok": False, "primary_hypothesis_verified": False, "limitations": [],
                              "verified_comparisons": [], "unverified_comparisons": [],
                              "blocking": [{"code": code, "detail": detail}]})
_MODULES = {
    "manager": "openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.manager.agent",
    "reporting": "openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.agent",
}


class EvidenceService:
    """Locates the run's results and answers "is the evidence accepted?" (cached per state on disk)."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self._cache: tuple[Any, dict[str, Any]] | None = None

    def results_dir(self) -> Path | None:
        states = sorted(self.run_dir.glob("experiments/*/manager/state.json"), key=lambda p: p.stat().st_mtime)
        return states[-1].parent.parent / "results" if states else None

    def _key(self, results: Path) -> Any:
        """Changes whenever anything the verdict depends on changes (so no stale verdict, no re-hash)."""
        folder = evidence.evidence_dir(results)
        protocol = evidence.load_protocol(results) or {}
        design = evidence.locate_design(protocol, results) if protocol else None
        watched = [folder / evidence.MANIFEST_FILE, folder / evidence.PROTOCOL_FILE, folder / evidence.LEDGER_FILE,
                   folder / evidence.RETIREMENTS_FILE, *([design] if design else []),
                   *sorted(results.glob("*.metrics.json")), *sorted(self.run_dir.glob("revisions/*/revision.json"))]
        # a design that disappears must change the key too
        return (str(design),) + tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in watched if p.is_file())

    def check(self, *, fresh: bool = False) -> dict[str, Any]:
        """``fresh``: bypass the cache (the DONE gate always re-hashes from disk)."""
        from jiuwenswarm.agents.harness.common.paper_pipeline import revision_state

        if fresh:
            self._cache = None

        try:
            results = self.results_dir()
            if results is None:
                return _unverified("NO_RESULTS", f"no run under {self.run_dir}")
            key = (str(results), self._key(results))
            if self._cache is not None and self._cache[0] == key:
                return self._cache[1]
            revision = revision_state.find_open(self.run_dir)
            result = (revision_state.evidence_check(self.run_dir, revision) if revision is not None
                      else evidence.verify(results))
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is unverified, never a pass
            return _unverified("EVIDENCE_CHECK_FAILED", repr(exc))
        self._cache = (key, result)
        return result

    def invalidate(self) -> None:
        self._cache = None

    def log(self, record: dict[str, Any]) -> None:
        """Best-effort audit trail (paths, ids, verdicts; never credentials)."""
        try:
            results = self.results_dir()
            folder = evidence.evidence_dir(results) if results is not None else self.run_dir
            folder.mkdir(parents=True, exist_ok=True)
            at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            with (folder / LOG_FILE).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": at, **record}, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001 - logging must not change the outcome
            logger.warning("PaperEvidenceRail: could not write %s", LOG_FILE, exc_info=True)


def _decision_signal(args: Any) -> str | None:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if hasattr(args, "signal"):
        return str(args.signal)
    return str(args.get("signal")) if isinstance(args, dict) and args.get("signal") else None


def rejection_text(result: dict[str, Any]) -> str:
    lines = ["DONE rejected by the host evidence check (PaperEvidenceRail): " + evidence.summary_line(result), "",
             "Pending tasks:"]
    lines += [f"- {task}" for task in result.get("pending_tasks") or []]
    lines += ["", "Blocking details:"] + [f"- {b['code']}: {b['detail']}" for b in result.get("blocking", [])[:8]]
    lines += ["", f"Call {DECISION_TOOL} again in this round with EXECUTE (route the repair, e.g. "
              "code_implementation with these lines in repair_instruction, then experiment_execution and "
              "reporting), or BLOCKED with a blocked_reason if the evidence cannot be produced."]
    return "\n".join(lines)


class PaperEvidenceRail(DeepAgentRail):  # type: ignore[misc,valid-type]
    """See the module docstring. ``role``: ``manager`` (gates DONE) or ``reporting`` (evidence brief)."""

    priority = 150

    def __init__(self, service: EvidenceService, role: str):
        if DeepAgentRail is not object:
            super().__init__()
        if role not in _MODULES:
            raise ValueError(f"role {role!r} not in {sorted(_MODULES)}")
        self.service = service
        self.role = role
        self._briefed = False

    async def before_tool_call(self, ctx) -> None:
        if self.role != "manager":
            return
        inputs = ctx.inputs
        if getattr(inputs, "tool_name", "") != DECISION_TOOL or _decision_signal(inputs.tool_args) != "DONE":
            return
        result = self.service.check(fresh=True)
        self.service.log({"event": "done_requested", "accepted": result["ok"], "protocol_id": result.get("protocol_id"),
                          "execution_id": result.get("execution_id"),
                          "blocking": [b["code"] for b in result["blocking"]]})
        if result["ok"]:
            return
        skip_tool_call(ctx, rejection_text(result), {"success": False, "rejected_by": "PaperEvidenceRail",
                                                     "pending_tasks": result.get("pending_tasks")})

    async def on_user_message(self, ctx) -> None:
        if self.role != "reporting" or self._briefed:
            return
        inputs = ctx.inputs
        if getattr(inputs, "source", "query") != "query" or not isinstance(getattr(inputs, "parts", None), list):
            return
        result = self.service.check()
        inputs.parts.insert(0, evidence.writing_brief(result))
        self._briefed = True
        self.service.log({"event": "writing_brief", "accepted": result["ok"], "protocol_id": result.get("protocol_id"),
                          "verified": len(result["verified_comparisons"]),
                          "unverified": len(result["unverified_comparisons"])})


def skip_tool_call(ctx, content: str, result: dict[str, Any]) -> None:
    """Skip the pending tool call through the framework's mechanism (``ability_manager``
    ``_railed_execute_single_tool_call``) and hand the agent ``content`` as the tool's answer.
    """
    from openjiuwen.core.foundation.llm import ToolMessage

    call = ctx.inputs.tool_call
    call_id = str(getattr(call, "id", "") or "")
    ctx.inputs.tool_result = result
    ctx.inputs.tool_msg = ToolMessage(content=content, tool_call_id=call_id)
    if call_id:
        ctx.extra.setdefault("_skip_tool_calls", {})[call_id] = True
    else:
        ctx.extra["_skip_tool"] = True


# --------------------------------------------------------------------------- installation
_INSTALLED: dict[str, Any] = {}


def after_execution(results: Path, manifest: dict[str, Any]) -> None:
    """Execution-end point (called by the host audit): record the execution identity."""
    service = _INSTALLED.get("service")
    if service is None:
        return
    service.invalidate()
    service.log({"event": "execution_recorded", "execution_id": manifest.get("execution_id"),
                 "protocol_id": manifest.get("protocol_id"), "revision": manifest.get("revision"),
                 "verdict": (manifest.get("audit") or {}).get("verdict"),
                 "primary_hypothesis_verified": manifest.get("primary_hypothesis_verified"),
                 "cells": {n: c.get("status") for n, c in (manifest.get("cells") or {}).items()}})


# the builder of each module's DeepAgent: (class, method)
_BUILDERS = {"manager": ("ManagerAgent", "_create_agent"), "reporting": ("ReportingAgent", "_build_paper_agent")}


def mount(agent: Any, service: EvidenceService, role: str) -> bool:
    """Queue the rail on a built ``DeepAgent`` via its public ``add_rail`` (once per agent)."""
    if not hasattr(agent, "add_rail"):
        return False  # an injected test double / non-DeepAgent: nothing to mount on
    existing = agent.find_rails_by_type((PaperEvidenceRail,)) if hasattr(agent, "find_rails_by_type") else []
    if existing:
        return False
    agent.add_rail(PaperEvidenceRail(service, role))
    return True


def _wrap_builder(original, role: str):
    def build(self, *args, **kwargs):
        agent = original(self, *args, **kwargs)
        service = _INSTALLED.get("service")
        if service is not None:
            mount(agent, service, role)
        return agent

    build.__evidence_rail__ = role  # type: ignore[attr-defined]
    build.__wrapped__ = original  # type: ignore[attr-defined]
    return build


def install_paper_evidence_rail(run_dir: Path) -> list[str]:
    """Mount the rail on every manager and reporting agent the paper pipeline builds. Idempotent: a
    second call rebinds the service to ``run_dir`` and does not stack wrappers.
    """
    import importlib

    from jiuwenswarm.agents.harness.common.paper_pipeline import execution_audit

    _INSTALLED["service"] = EvidenceService(Path(run_dir))
    patched = []
    for role, module_name in _MODULES.items():
        cls_name, method = _BUILDERS[role]
        cls = getattr(importlib.import_module(module_name), cls_name)
        current = cls.__dict__[method]
        if not getattr(current, "__evidence_rail__", None):
            setattr(cls, method, _wrap_builder(current, role))
        patched.append(f"{cls_name}.{method}")
    if after_execution not in execution_audit.AFTER_RECORD_HOOKS:
        execution_audit.AFTER_RECORD_HOOKS.append(after_execution)
    return patched


def uninstall_paper_evidence_rail() -> None:
    import importlib

    from jiuwenswarm.agents.harness.common.paper_pipeline import execution_audit

    for role, module_name in _MODULES.items():
        cls_name, method = _BUILDERS[role]
        cls = getattr(importlib.import_module(module_name), cls_name)
        current = cls.__dict__[method]
        if getattr(current, "__evidence_rail__", None):
            setattr(cls, method, current.__wrapped__)
    if after_execution in execution_audit.AFTER_RECORD_HOOKS:
        execution_audit.AFTER_RECORD_HOOKS.remove(after_execution)
    _INSTALLED.clear()
