"""Host-side state of a review-driven revision: persisted settings, frozen cells, cell cap, acceptance.

``revision.py`` builds the brief and the prompt addenda; this module holds what must not depend on
a prompt being obeyed or on the process staying alive:

* ``revisions/revision_NN/revision.json`` — the revision's settings (replication model, module
  models, experiment model / endpoint, pipeline config hash; never an API key: credentials stay in
  the external env files), their identity hash, the new-cell cap, the gate's start round, the cells
  executed so far, every explicit setting override, and the status (``open`` -> ``accepted`` |
  ``not_accepted``). A plain ``resume`` restores all of it from here.
* ``frozen_manifest.json`` + ``frozen/`` — every result that existed when the revision opened, with
  sha256 of its metrics file, its per-item records file and its config, plus the experiment code's
  file hashes. These cells are *referenced*, never re-run.
* ``ExecutionGuard`` — consulted by the execution wrapper (``execution_audit``): it strips frozen
  cells from what the official engine executes, references every new cell that already has a valid
  evidence version under its current spec (``evidence.reusable_version``: succeeded earlier in this
  run, hashes intact, condition and design entry unchanged) instead of re-running it, refuses new
  cells beyond the cap and re-runs beyond ``max_cell_executions`` (both count only new executions,
  so a cell at its limit with valid evidence is still referenced), skips retired cells, re-attaches
  the frozen cells' recorded metrics, re-verifies their hashes (a changed file stops their
  analysis), and copies the new cells' metrics into the revision folder.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.paper_pipeline.runner import PaperRunError, now_iso

REVISION_FILE = "revision.json"
MANIFEST_FILE = "frozen_manifest.json"
OPEN, ACCEPTED, NOT_ACCEPTED = "open", "accepted", "not_accepted"
# The pipeline finished but acceptance failed: the revision stays active (guard, cap, frozen
# cells, budgets) until it passes or the operator ends it (``rollback`` / ``abandon``).
NEEDS_REPAIR, ROLLED_BACK, ABANDONED = "needs_repair", "rolled_back", "abandoned"
# NOT_ACCEPTED was written automatically by older versions (it released the guard); it is read as
# NEEDS_REPAIR unless the operator rolled the revision back.
ACTIVE = (OPEN, NEEDS_REPAIR, NOT_ACCEPTED)
# settings whose change mid-revision changes what the new cells measure
ANSWERING_SETTINGS = ("experiment_model", "experiment_model_effective", "experiment_api_base", "replication_model")
# spend / delivery controls: restored on resume when omitted, recorded when overridden
BUDGET_SETTINGS = ("budget_soft", "budget_hard", "balance_floor", "balance_hard_floor")
EVIDENCE_SETTINGS = ("delivery_policy", "missing_primary_rule", "tier_gates")
_NOT_IDENTITY = ("experiment_env", *BUDGET_SETTINGS, *EVIDENCE_SETTINGS)
_DERIVED = ("experiment_api_base", "experiment_model_effective", "config_sha256")
DEFAULT_MAX_CELL_EXECUTIONS = 3
_CODE_SUFFIXES = (".py", ".yaml", ".yml", ".json", ".toml", ".txt", ".cfg")
_CODE_SKIP_DIRS = ("experiments", "results", "logs", "scratch", "__pycache__", ".git", "smoke")
_FAILED_STATUSES = ("failed", "error", "harness_failed")


def _now() -> str:
    return now_iso()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


# --------------------------------------------------------------------------- persisted settings
@dataclass
class RevisionState:
    index: int
    start_round: int
    max_new_cells: int
    settings: dict[str, Any] = field(default_factory=dict)
    identity: str = ""
    status: str = OPEN
    created_at: str = field(default_factory=_now)
    completion: list[str] = field(default_factory=lambda: [
        "new cells executed and evidence audit passed",
        "frozen results unchanged",
        "new paper written after the execution and its final PDF check passed",
    ])
    executed_new_cells: list[str] = field(default_factory=list)
    refused_cells: list[str] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    acceptance: dict[str, Any] = field(default_factory=dict)
    mode: str = "experiments"  # or "writing_only": no new cell may run
    paper_snapshot: str | None = None  # the pre-revision paper, kept as a separate candidate
    review_items: list[dict[str, str]] = field(default_factory=list)  # [{"id", "text"}], stable ids
    # executions per cell (a re-run is a new evidence version); separate from the new-cell cap
    cell_executions: dict[str, int] = field(default_factory=dict)
    max_cell_executions: int = DEFAULT_MAX_CELL_EXECUTIONS
    status_history: list[dict[str, Any]] = field(default_factory=list)

    def folder(self, run_dir: Path) -> Path:
        return Path(run_dir) / "revisions" / f"revision_{self.index:02d}"

    @property
    def active(self) -> bool:
        return self.status in ACTIVE and "rolled_back" not in self.acceptance

    def set_status(self, status: str, reason: str) -> None:
        self.status_history.append({"at": _now(), "from": self.status, "to": status, "reason": reason})
        self.status = status


def settings_of(opts) -> dict[str, Any]:
    """Revision-relevant settings of a run (no secrets: env files are referenced by path)."""
    api_base = effective_model = None
    if getattr(opts, "experiment_env", None):
        try:
            from jiuwenswarm.agents.harness.common.paper_pipeline.experiment_model import load_experiment_env

            override = load_experiment_env(Path(opts.experiment_env), opts.experiment_model)
            api_base, effective_model = override.get("API_BASE"), override.get("MODEL_NAME")
        except (OSError, ValueError, KeyError):
            pass
    config_path = getattr(opts, "config_path", None)
    config_sha = sha256_file(Path(config_path)) if config_path and Path(config_path).is_file() else None
    return {
        "replication_model": getattr(opts, "replication_model", None),
        "module_models": dict(sorted((getattr(opts, "module_models", None) or {}).items())),
        "experiment_env": str(opts.experiment_env) if getattr(opts, "experiment_env", None) else None,
        "experiment_model": getattr(opts, "experiment_model", None),
        "experiment_api_base": api_base,
        "experiment_model_effective": effective_model,
        "config_path": str(config_path) if config_path else None,
        "config_sha256": config_sha,
        **{k: getattr(opts, k, None) for k in BUDGET_SETTINGS + EVIDENCE_SETTINGS},
    }


def identity_of(settings: dict[str, Any]) -> str:
    return sha256_json({k: v for k, v in settings.items() if k not in _NOT_IDENTITY})[:16]


def save(state: RevisionState, run_dir: Path) -> Path:
    folder = state.folder(run_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / REVISION_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def load(path: Path) -> RevisionState:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    known = RevisionState.__dataclass_fields__
    return RevisionState(**{k: v for k, v in data.items() if k in known})


def all_revisions(run_dir: Path) -> list[RevisionState]:
    root = Path(run_dir) / "revisions"
    if not root.is_dir():
        return []
    return [load(p) for p in sorted(root.glob(f"revision_*/{REVISION_FILE}"))]


def find_open(run_dir: Path) -> RevisionState | None:
    """The active revision (open or needing repair); accepted / rolled back / abandoned ones are closed."""
    opened = [s for s in all_revisions(run_dir) if s.active]
    if len(opened) > 1:
        raise RuntimeError(f"several open revisions under {run_dir}/revisions: {[s.index for s in opened]}")
    return opened[0] if opened else None


def next_index(run_dir: Path) -> int:
    root = Path(run_dir) / "revisions"
    return len(list(root.glob("revision_*"))) + 1 if root.is_dir() else 1


def restore_settings(opts, state: RevisionState, run_dir: Path, *, allow_change: bool = False) -> list[dict]:
    """Fill unset options from the open revision; record (and for answering-model settings, gate)
    every explicit difference. Returns the overrides recorded by this call.

    An omitted option (None) means "as persisted"; an explicit one that differs is an override and
    is recorded. A revision persisted before budgets were recorded cannot tell "no budget" from
    "budget lost": resuming it without an explicit budget is refused unless ``allow_change``.
    """
    current = settings_of(opts)
    changes: list[dict[str, Any]] = []
    unrecorded = [k for k in BUDGET_SETTINGS + EVIDENCE_SETTINGS if k not in state.settings]
    if (any(k in BUDGET_SETTINGS for k in unrecorded) and all(current.get(k) is None for k in BUDGET_SETTINGS)
            and not allow_change):
        raise PaperRunError(
            f"revision {state.index:02d} was opened before budget limits were persisted, so the limits it ran "
            "under are unknown. Resume with the limits stated explicitly (--budget / --budget-hard / "
            "--balance-floor / --balance-hard-floor), or pass --allow-setting-change to continue without one."
        )
    for key in unrecorded:  # recorded from now on (None = explicitly none), so the next resume restores it
        changes.append({"setting": key, "from": "unrecorded", "to": current.get(key)})
    for key, saved in state.settings.items():
        if key in _DERIVED:
            continue  # derived from the options; compared after restoring them
        given = current.get(key)
        if given in (None, {}, ""):
            if saved not in (None, {}, ""):
                _set_option(opts, key, saved)
            continue
        if given != saved:
            changes.append({"setting": key, "from": saved, "to": given})
    restored = settings_of(opts)
    for key in _DERIVED:
        if restored.get(key) != state.settings.get(key) and state.settings.get(key) is not None:
            changes.append({"setting": key, "from": state.settings.get(key), "to": restored.get(key)})
    answering = [c for c in changes if c["setting"] in ANSWERING_SETTINGS or c["setting"] == "experiment_env"]
    if answering and state.executed_new_cells and not allow_change:
        raise PaperRunError(
            f"revision {state.index:02d} already executed {len(state.executed_new_cells)} new cells with "
            f"{ {c['setting']: c['from'] for c in answering} }; resuming with {[c['setting'] for c in answering]} "
            "changed would mix answering models inside one comparison. Resume without the override, or pass "
            "--allow-setting-change to record it explicitly."
        )
    if changes:
        for change in changes:
            change["at"] = _now()
        state.overrides += changes
        state.settings = restored
        state.identity = identity_of(restored)
        save(state, run_dir)
    return changes


def _set_option(opts, key: str, value: Any) -> None:
    if key == "module_models":
        opts.module_models = dict(value)
    elif key == "config_path":
        opts.config_path = value
    elif hasattr(opts, key):
        setattr(opts, key, value)


# --------------------------------------------------------------------------- frozen manifest
@dataclass
class FrozenCell:
    name: str
    metrics_path: str
    metrics_sha256: str
    snapshot: str
    config_sha256: str | None = None
    records_path: str | None = None
    records_sha256: str | None = None
    implementation_revision: Any = None


def _records_path(metrics: dict[str, Any], code_dir: Path | None) -> Path | None:
    artifacts = metrics.get("run_artifacts")
    raw = artifacts.get("records_path") if isinstance(artifacts, dict) else None
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() and code_dir is not None:
        path = code_dir / path
    return path


def _is_result(metrics: Any) -> bool:
    return isinstance(metrics, dict) and str(metrics.get("status", "")).lower() not in _FAILED_STATUSES


def code_hashes(code_dir: Path | None) -> dict[str, str]:
    if code_dir is None or not Path(code_dir).is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(Path(code_dir).rglob("*")):
        rel = path.relative_to(code_dir)
        if not path.is_file() or path.suffix not in _CODE_SUFFIXES or any(p in _CODE_SKIP_DIRS for p in rel.parts):
            continue
        if path.stat().st_size <= 5_000_000:
            out[rel.as_posix()] = sha256_file(path)
    return out


def build_manifest(results_dir: Path, code_dir: Path | None, folder: Path) -> dict[str, Any]:
    """Freeze every completed result present now; copy each metrics file into ``folder/frozen``."""
    results_dir, frozen_dir = Path(results_dir), Path(folder) / "frozen"
    frozen_dir.mkdir(parents=True, exist_ok=True)
    cells: list[dict[str, Any]] = []
    skipped: list[str] = []
    for path in sorted(results_dir.glob("*.metrics.json")) if results_dir.is_dir() else []:
        name = path.name[: -len(".metrics.json")]
        try:
            metrics = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append(f"{name}: unreadable")
            continue
        if not _is_result(metrics):
            skipped.append(f"{name}: status {metrics.get('status') if isinstance(metrics, dict) else '?'}")
            continue
        snapshot = frozen_dir / path.name
        shutil.copy2(path, snapshot)
        records = _records_path(metrics, code_dir)
        cell = FrozenCell(
            name=name, metrics_path=str(path), metrics_sha256=sha256_file(path), snapshot=str(snapshot),
            config_sha256=sha256_json(metrics["config"]) if isinstance(metrics.get("config"), dict) else None,
            records_path=str(records) if records else None,
            records_sha256=sha256_file(records) if records is not None and records.is_file() else None,
            implementation_revision=metrics.get("implementation_revision"),
        )
        cells.append(asdict(cell))
    manifest = {"created_at": _now(), "results_dir": str(results_dir), "code_dir": str(code_dir) if code_dir else None,
                "code_files": code_hashes(code_dir), "cells": cells, "not_frozen": skipped}
    (Path(folder) / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def load_manifest(folder: Path) -> dict[str, Any] | None:
    path = Path(folder) / MANIFEST_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def verify_manifest(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """One entry per frozen file that is missing or no longer matches its hash."""
    problems: list[dict[str, str]] = []
    for cell in manifest.get("cells", []):
        for kind, path_key, sha_key in (("metrics", "metrics_path", "metrics_sha256"),
                                        ("records", "records_path", "records_sha256"),
                                        ("snapshot", "snapshot", "metrics_sha256")):
            raw, expected = cell.get(path_key), cell.get(sha_key)
            if not raw or not expected:
                continue
            path = Path(raw)
            if not path.is_file():
                problems.append({"cell": cell["name"], "file": str(path), "reason": f"{kind} file missing"})
            elif sha256_file(path) != expected:
                problems.append({"cell": cell["name"], "file": str(path),
                                 "reason": f"{kind} file changed since the revision froze it (sha256 differs)"})
    return problems


def code_changes(manifest: dict[str, Any]) -> dict[str, list[str]]:
    code_dir = manifest.get("code_dir")
    now = code_hashes(Path(code_dir)) if code_dir else {}
    before = manifest.get("code_files") or {}
    return {"changed": sorted(k for k in before if k in now and now[k] != before[k]),
            "added": sorted(k for k in now if k not in before),
            "removed": sorted(k for k in before if k not in now)}


# --------------------------------------------------------------------------- execution guard
@dataclass
class ExecutionPlan:
    execute: list[str]
    reference: list[str]
    refused: list[str]
    # cells refused because they already ran ``max_cell_executions`` times (a separate limit from the
    # new-cell cap: the cap bounds how many cells the revision adds, this bounds re-runs of one cell)
    rerun_refused: list[str] = field(default_factory=list)
    # cells whose valid evidence version (same spec) is referenced instead of re-run: name -> ledger entry
    reuse: dict[str, dict[str, Any]] = field(default_factory=dict)
    # cells that must be re-run because their spec changed (name -> why); old versions are kept
    stale: dict[str, str] = field(default_factory=dict)
    retired: list[str] = field(default_factory=list)  # retired in the protocol: not executed
    attached_reuse: dict[str, dict[str, Any]] = field(default_factory=dict)  # reuse that ``merge`` attached


def answering_of(settings: dict[str, Any]) -> dict[str, Any]:
    """The settings that change what a new cell measures (part of every cell spec in a revision)."""
    return {k: settings.get(k) for k in ANSWERING_SETTINGS if settings.get(k) is not None}


class ExecutionGuard:
    """Host enforcement of "reference frozen cells, reuse valid evidence, execute only what has none,
    at most N new cells, each executed at most M times"."""

    def __init__(self, run_dir: Path, state: RevisionState, manifest: dict[str, Any]):
        self.run_dir = Path(run_dir)
        self.state = state
        self.manifest = manifest
        self.frozen = {c["name"]: c for c in manifest.get("cells", [])}
        self.results_dir = Path(manifest.get("results_dir") or "")

    def frozen_names(self, names: list[str]) -> set[str]:
        return {n for n in names if n in self.frozen}

    def frozen_metrics(self, names) -> dict[str, dict[str, Any]]:
        out = {}
        for name in names:
            try:
                out[name] = json.loads(Path(self.frozen[name]["snapshot"]).read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError):
                continue
        return out

    def plan(self, names: list[str], protocol: dict[str, Any] | None = None) -> ExecutionPlan:
        """``protocol``: the one frozen for this execution; with it, a non-frozen cell that has a valid
        evidence version under its current spec is referenced (whatever its execution count), and the
        caps apply only to cells that need a new execution.
        """
        from jiuwenswarm.agents.harness.common.paper_pipeline import evidence

        reference = [n for n in names if n in self.frozen]
        budget = set(self.state.executed_new_cells)
        specs = (protocol or {}).get("cell_specs") or {}
        retired = {r["cell"] for r in (protocol or {}).get("retired") or []}
        out = ExecutionPlan([], reference, [])
        for name in names:
            if name in self.frozen:
                continue
            if name in retired:
                out.retired.append(name)
                continue
            if protocol is not None and self.results_dir.is_dir():
                entry, note = evidence.reusable_version(self.results_dir, name, specs.get(name))
                if entry is not None:
                    out.reuse[name] = entry
                    continue
                if note:
                    out.stale[name] = note
            if self.state.mode == "writing_only" or (name not in budget and len(budget) >= self.state.max_new_cells):
                out.refused.append(name)
            elif self.state.cell_executions.get(name, 0) >= self.state.max_cell_executions:
                out.rerun_refused.append(name)
            else:
                budget.add(name)
                out.execute.append(name)
        return out

    def filter_inputs(self, inputs, protocol: dict[str, Any] | None = None):
        """``inputs`` with only the cells to execute; returns (inputs, plan)."""
        variants = list(inputs.implementation.variants)
        plan = self.plan([v.name for v in variants], protocol)
        kept = [v for v in variants if v.name in plan.execute]
        implementation = _copy(inputs.implementation, variants=kept)
        return _copy(inputs, implementation=implementation), plan

    def _attach_reuse(self, result, plan: ExecutionPlan, variant_cls) -> list[str]:
        """Re-attach each reused version (hash re-checked) and put its file back in the results dir
        if something replaced it there, so reporting reads the result the evidence describes."""
        from jiuwenswarm.agents.harness.common.paper_pipeline import evidence

        lines, attached = [], []
        for name, entry in sorted(plan.reuse.items()):
            copy = Path(entry["version_path"])
            if not copy.is_file() or evidence.sha256_file(copy) != entry.get("metrics_sha256"):
                lines.append(f"REVISION ERROR REUSED_EVIDENCE_CHANGED {name}: archived v{entry.get('version')} is "
                             "missing or changed; not attached")
                continue
            target = self.results_dir / f"{name}.metrics.json"
            if not target.is_file() or evidence.sha256_file(target) != entry["metrics_sha256"]:
                shutil.copy2(copy, target)
                lines.append(f"REVISION restored {target.name} from evidence version v{entry.get('version')}")
            metrics = json.loads(copy.read_text(encoding="utf-8"))
            result.variants.append(variant_cls(
                name=name, metrics=metrics, exit_code=0, log_path=str(target), failure_kind="ok",
                metrics_state="present", process_status="completed"))
            plan.attached_reuse[name] = entry
            attached.append(f"{name} (v{entry.get('version')})")
        if attached:
            lines.append(f"REVISION reused {len(attached)} verified evidence versions recorded in this run (same "
                         f"cell spec; not re-run, no execution counted): {', '.join(attached)}")
        from jiuwenswarm.agents.harness.common.paper_pipeline.execution_audit import code_fingerprint

        code_dir = self.manifest.get("code_dir")
        now = code_fingerprint(Path(code_dir)) if code_dir else None
        drifted = [n for n, e in sorted(plan.attached_reuse.items())
                   if now and e.get("code_sha256") and e["code_sha256"] != now]
        if drifted:
            lines.append(f"REVISION WARN REUSED_AFTER_CODE_CHANGE: the experiment code changed since "
                         f"{', '.join(drifted)} ran; their design `cells` entries are unchanged, so they are reused "
                         "— if the change affects them, bump `cells.<name>.implementation` in the experiment-protocol "
                         "block to have them re-run")
        return lines

    def merge(self, output, plan: ExecutionPlan) -> list[str]:
        """Attach frozen cells and reused versions, verify them, archive new metrics; returns host note lines."""
        result = output.result
        problems = verify_manifest(self.manifest)
        changed = {p["cell"] for p in problems}
        lines = [f"REVISION ERROR FROZEN_RESULT_CHANGED {p['cell']}: {p['file']} — {p['reason']}; its analysis "
                 "is stopped" for p in problems]
        variant_cls = type(result.variants[0]) if result.variants else _variant_result_cls()
        attached = []
        for name in plan.reference:
            if name in changed:
                continue
            cell = self.frozen[name]
            metrics = json.loads(Path(cell["snapshot"]).read_text(encoding="utf-8"))
            result.variants.append(variant_cls(
                name=name, metrics=metrics, exit_code=0, log_path=cell["metrics_path"], failure_kind="ok",
                metrics_state="present", process_status="completed"))
            attached.append(name)
        if attached:
            lines.append(f"REVISION referenced {len(attached)} frozen cells from the revision manifest (not re-run): "
                         + ", ".join(attached))
        lines += self._attach_reuse(result, plan, variant_cls)
        for name, why in sorted(plan.stale.items()):
            if name in plan.execute:
                lines.append(f"REVISION re-run required for {name}: {why}")
        if plan.retired:
            lines.append("REVISION retired cells (protocol retirement records) not executed: "
                         f"{', '.join(plan.retired)}")
        if plan.refused:
            lines.append(f"REVISION ERROR CELL_CAP_REFUSED: the cap of {self.state.max_new_cells} new cells is "
                         f"reached; not executed: {', '.join(plan.refused)}")
        if plan.rerun_refused:
            lines.append(f"REVISION ERROR CELL_RERUN_LIMIT: already executed {self.state.max_cell_executions} times "
                         f"in this revision and no valid evidence version matches the current spec; not executed "
                         f"again: {', '.join(plan.rerun_refused)}")
        out_dir = self.state.folder(self.run_dir) / "results"
        out_dir.mkdir(parents=True, exist_ok=True)
        results_dir = Path(self.manifest.get("results_dir") or "")
        executed = []
        for variant in result.variants:
            if variant.name in plan.execute:
                executed.append(variant.name)
                source = results_dir / f"{variant.name}.metrics.json"
                if source.is_file():
                    shutil.copy2(source, out_dir / source.name)
        if executed:
            lines.append(f"REVISION executed new cells: {', '.join(executed)}")
        elif not plan.execute:
            lines.append("REVISION WARN NO_NEW_CELLS: every cell in the implementation is frozen or reused; "
                         "nothing new ran")
        changes = code_changes(self.manifest)
        if any(changes.values()):
            lines.append(f"REVISION WARN CODE_CHANGED_SINCE_FREEZE: {json.dumps(changes)[:400]} — frozen results "
                         "are referenced as recorded, not re-derived with the new code")
        self.state.executed_new_cells = sorted(set(self.state.executed_new_cells) | set(executed))
        self.state.refused_cells = sorted(set(self.state.refused_cells) | set(plan.refused) | set(plan.rerun_refused))
        for name in plan.execute:  # attempted executions count, whether or not they completed
            self.state.cell_executions[name] = self.state.cell_executions.get(name, 0) + 1
        self.state.acceptance["last_integrity"] = {"at": _now(), "problems": problems}
        save(self.state, self.run_dir)
        return lines


def _copy(model, **update):
    if hasattr(model, "model_copy"):
        return model.model_copy(update=update)
    clone = type(model).__new__(type(model))
    clone.__dict__.update({**model.__dict__, **update})
    return clone


def _variant_result_cls():
    try:
        from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution.schemas import (
            VariantResult,
        )

        return VariantResult
    except ImportError:  # unit tests without agent-core
        from types import SimpleNamespace

        return SimpleNamespace


# The execution wrapper (``execution_audit.install_execution_audit``) consults this.
_ACTIVE: ExecutionGuard | None = None


def install_execution_guard(guard: ExecutionGuard | None) -> None:
    global _ACTIVE
    _ACTIVE = guard


def active_guard() -> ExecutionGuard | None:
    return _ACTIVE


# --------------------------------------------------------------------------- evidence acceptance
def evidence_check(run_dir: Path, state: RevisionState, design_text: str | None = None) -> dict[str, Any]:
    """The revision's evidence, judged by ``evidence.verify`` (the same check the final acceptance
    and ``PaperEvidenceRail`` use) plus the frozen-manifest integrity only a revision has.
    """
    from jiuwenswarm.agents.harness.common.paper_pipeline import evidence

    folder = state.folder(run_dir)
    manifest = load_manifest(folder)
    if manifest is None:
        return evidence.finalize({"ok": False, "primary_hypothesis_verified": False, "limitations": [],
                                 "verified_comparisons": [], "unverified_comparisons": [],
                                 "blocking": [{"code": "NO_FROZEN_MANIFEST",
                                               "detail": "no frozen manifest for this revision"}]})
    result = evidence.verify(Path(manifest["results_dir"]), revision=state, design_text=design_text)
    problems = verify_manifest(manifest)
    if problems:
        result["blocking"].insert(0, {"code": "FROZEN_RESULT_CHANGED", "detail": "frozen results changed: " + "; ".join(
            f"{p['file']} ({p['reason']})" for p in problems[:3])})
    if state.mode != "writing_only" and not state.executed_new_cells:
        result["blocking"].append({"code": "NO_NEW_CELL", "detail": "no new cell has been executed in this revision"})
    return evidence.finalize(result)


def evidence_status(run_dir: Path, state: RevisionState) -> tuple[bool, str]:
    """(accepted, detail) for the revision gate; see ``evidence_check``."""
    from jiuwenswarm.agents.harness.common.paper_pipeline import evidence

    result = evidence_check(run_dir, state)
    if result["ok"]:
        return True, evidence.summary_line(result) + ", frozen results unchanged"
    return False, evidence.summary_line(result)
