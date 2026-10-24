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
  cells from what the official engine executes, refuses new cells beyond the cap, re-attaches the
  frozen cells' recorded metrics, re-verifies their hashes (a changed file stops their analysis),
  and copies the new cells' metrics into the revision folder.
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
# settings whose change mid-revision changes what the new cells measure
ANSWERING_SETTINGS = ("experiment_model", "experiment_model_effective", "experiment_api_base", "replication_model")
_DERIVED = ("experiment_api_base", "experiment_model_effective", "config_sha256")
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

    def folder(self, run_dir: Path) -> Path:
        return Path(run_dir) / "revisions" / f"revision_{self.index:02d}"


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
    }


def identity_of(settings: dict[str, Any]) -> str:
    return sha256_json({k: v for k, v in settings.items() if k != "experiment_env"})[:16]


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
    opened = [s for s in all_revisions(run_dir) if s.status == OPEN]
    if len(opened) > 1:
        raise RuntimeError(f"several open revisions under {run_dir}/revisions: {[s.index for s in opened]}")
    return opened[0] if opened else None


def next_index(run_dir: Path) -> int:
    root = Path(run_dir) / "revisions"
    return len(list(root.glob("revision_*"))) + 1 if root.is_dir() else 1


def restore_settings(opts, state: RevisionState, run_dir: Path, *, allow_change: bool = False) -> list[dict]:
    """Fill unset options from the open revision; record (and for answering-model settings, gate)
    every explicit difference. Returns the overrides recorded by this call.
    """
    current = settings_of(opts)
    changes: list[dict[str, Any]] = []
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


class ExecutionGuard:
    """Host enforcement of "reference frozen cells, execute only new ones, at most N new"."""

    def __init__(self, run_dir: Path, state: RevisionState, manifest: dict[str, Any]):
        self.run_dir = Path(run_dir)
        self.state = state
        self.manifest = manifest
        self.frozen = {c["name"]: c for c in manifest.get("cells", [])}

    def plan(self, names: list[str]) -> ExecutionPlan:
        reference = [n for n in names if n in self.frozen]
        budget = set(self.state.executed_new_cells)
        execute, refused = [], []
        for name in names:
            if name in self.frozen:
                continue
            if self.state.mode != "writing_only" and (name in budget or len(budget) < self.state.max_new_cells):
                budget.add(name)
                execute.append(name)
            else:
                refused.append(name)
        return ExecutionPlan(execute, reference, refused)

    def filter_inputs(self, inputs):
        """``inputs`` with only the cells to execute; returns (inputs, plan)."""
        variants = list(inputs.implementation.variants)
        plan = self.plan([v.name for v in variants])
        kept = [v for v in variants if v.name in plan.execute]
        implementation = _copy(inputs.implementation, variants=kept)
        return _copy(inputs, implementation=implementation), plan

    def merge(self, output, plan: ExecutionPlan) -> list[str]:
        """Attach frozen cells, verify them, archive new metrics; returns host note lines."""
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
        if plan.refused:
            lines.append(f"REVISION ERROR CELL_CAP_REFUSED: the cap of {self.state.max_new_cells} new cells is "
                         f"reached; not executed: {', '.join(plan.refused)}")
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
            lines.append("REVISION WARN NO_NEW_CELLS: every cell in the implementation is frozen; nothing new ran")
        changes = code_changes(self.manifest)
        if any(changes.values()):
            lines.append(f"REVISION WARN CODE_CHANGED_SINCE_FREEZE: {json.dumps(changes)[:400]} — frozen results "
                         "are referenced as recorded, not re-derived with the new code")
        self.state.executed_new_cells = sorted(set(self.state.executed_new_cells) | set(executed))
        self.state.refused_cells = sorted(set(self.state.refused_cells) | set(plan.refused))
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
def evidence_status(run_dir: Path, state: RevisionState) -> tuple[bool, str]:
    """Whether the revision's evidence is accepted: the latest execution's audit passed (after this
    revision opened), at least one new cell ran, and every frozen result is unchanged.
    """
    folder = state.folder(run_dir)
    manifest = load_manifest(folder)
    if manifest is None:
        return False, "no frozen manifest for this revision"
    problems = verify_manifest(manifest)
    if problems:
        return False, "frozen results changed: " + "; ".join(f"{p['file']} ({p['reason']})" for p in problems[:3])
    if state.mode == "writing_only":
        return True, "writing-only revision: frozen results unchanged"
    if not state.executed_new_cells:
        return False, "no new cell has been executed in this revision"
    audit_path = Path(manifest["results_dir"]) / "audit.json"
    if not audit_path.is_file():
        return False, "no audit.json for the latest execution"
    try:
        report = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"audit.json unreadable ({exc}); evidence unverified"
    opened = datetime.fromisoformat(state.created_at).timestamp()
    if float(report.get("audited_at") or 0) < opened:
        return False, "the latest audit predates this revision"
    if report.get("verdict") != "passed":
        blocking = report.get("blocking") or []
        return False, f"audit verdict {report.get('verdict')}: " + " | ".join(map(str, blocking[:4]))[:600]
    return True, "audit passed, frozen results unchanged"
