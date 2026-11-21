"""Evidence contract of one paper run: the protocol frozen before execution, the evidence each
execution produced, and the one check every consumer uses to decide what the evidence supports.

Before this module, acceptance read "the latest ``audit.json``": its verdict and its timestamp. That
let a failed cell A be forgotten once a later execution ran only cell B, let metrics be edited
after the audit, and let an audit made under an older design count for a newer one. Here:

* ``evidence/protocol.json`` — written by the **host before execution** from the design text, the
  declared metrics, the planned cells and the operator's options (never from experiment output):
  primary metric, required cells, required primary comparisons, the rule for a missing primary
  value, and the delivery policy. Its id is the hash of its content; a different design gives a
  new protocol that ``supersedes`` the old one, and evidence audited under the old one no longer
  counts.
* ``evidence/manifest.json`` (+ ``executions/<id>.json``) — one per execution: protocol id,
  execution id, revision, each required cell's status / metrics sha256 / per-item records hash /
  model / dataset / budget / version, the structured status of every required comparison, and the
  audit verdict. New results are copied to ``cells/<name>/v<k>.metrics.json``: a re-run makes a new
  version, history is never overwritten (``cells/<name>/history.jsonl``).
* ``verify`` — the deterministic check used by the execution audit, the revision gate, the final
  acceptance and ``PaperEvidenceRail``: protocol unchanged, the *current design* (located and read
  by ``verify`` itself) still the one the protocol froze, every required cell completed and
  unblocked, its hashes still matching (re-hashed now), every required comparison verified over the
  pre-declared item set (or, under the ``descriptive`` policy, reported as a limitation), and — in
  a revision — evidence that belongs to this revision.
* ``evidence/ledger.json`` — every successful execution of a cell, as a hashed *evidence version*
  tied to the cell's spec (its condition, metrics, declared item set, activation gate, answering
  settings and design entry: ``cell_spec``). A later execution in a revision references a valid
  version whose spec still matches instead of re-running the cell; a changed spec requires a re-run
  and the old versions are kept.
* ``evidence/retirements.json`` — cells taken out of the design on purpose, each with a reason and
  the comparisons / claims it affects; they become stated limitations, never silent omissions.

The protocol also freezes, before execution, the parts of the design that must be checkable
(``experiment_protocol``): the pre-declared item set per setting, the activation gate per tier, the
comparisons each review item requires, retired and calibration cells.

Statuses of a required comparison: ``verified`` (computed over the full declared item set),
``failed`` (a required cell did not complete), ``unverified`` (could not be validly computed: budget
mismatch, item sets differ, ids missing, primary metric incomplete, declared items missing ...). A
verified comparison has an *outcome* (``a_better`` / ``a_worse`` / ``bounded_null`` /
``inconclusive``): a negative or null outcome is evidence; an unverified comparison is not, and is
never read as "no effect".
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jiuwenswarm.agents.harness.common.paper_pipeline import experiment_protocol, rigor_stats

EVIDENCE_DIR = "evidence"
PROTOCOL_FILE = "protocol.json"
MANIFEST_FILE = "manifest.json"
LEDGER_FILE = "ledger.json"
RETIREMENTS_FILE = "retirements.json"
POLICIES = ("confirmatory", "descriptive")
SCHEMA = 2
# protocol fields that are not part of its identity (where the design lives may move)
_PROTOCOL_META = ("protocol_id", "created_at", "supersedes", "design_path")
DEFAULT_GATE = 0.30
# spec of a referenced cell: a pre-revision frozen cell is referenced as recorded; a legacy cell has
# no ledger entry, so its compatibility with the current protocol cannot be checked
SPEC_FROZEN, SPEC_LEGACY = "frozen", "legacy"

_CONFIG: dict[str, Any] = {"missing_primary_rule": None, "delivery_policy": None, "tier_gates": None}


def configure(*, missing_primary_rule: str | None = None, delivery_policy: str | None = None,
              tier_gates: dict[str, float] | None = None) -> None:
    """Operator options for the next protocol (None = keep the current protocol's value)."""
    if missing_primary_rule is not None and missing_primary_rule not in rigor_stats.MISSING_RULES:
        raise ValueError(f"missing_primary_rule {missing_primary_rule!r} not in {rigor_stats.MISSING_RULES}")
    if delivery_policy is not None and delivery_policy not in POLICIES:
        raise ValueError(f"delivery_policy {delivery_policy!r} not in {POLICIES}")
    if tier_gates is not None:
        tier_gates, problems = experiment_protocol.check_gates(tier_gates, "operator")
        if problems:
            raise ValueError("; ".join(problems))
    _CONFIG.update(missing_primary_rule=missing_primary_rule, delivery_policy=delivery_policy,
                   tier_gates=tier_gates or None)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_dir(results_dir: Path) -> Path:
    return Path(results_dir).parent / EVIDENCE_DIR


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


class EvidenceFileError(ValueError):
    """An evidence file exists but cannot be read as JSON (disk error, partial write, manual edit)."""


def _read_json(path: Path) -> Any:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceFileError(f"{path}: {exc}") from exc


_RETIREMENT_FIELDS = ("cell", "reason", "affected_comparisons", "affected_claims", "source", "at")


def _blocks_cell(finding: dict[str, Any], name: str) -> bool:
    """An unwaived error naming ``name``; comparison-level findings are judged per comparison instead."""
    if finding.get("level") != "error" or finding.get("waived_by"):
        return False
    return name in (finding.get("variants") or []) and not str(finding.get("code")).startswith("PRIMARY_COMPARISON")


# --------------------------------------------------------------------------- protocol
def build_protocol(*, declared: list[str], planned: Iterable[str], frozen: Iterable[str] = (),
                   design_text: str = "", revision: int | None = None, missing_primary_rule: str = "refuse",
                   delivery_policy: str = "confirmatory", plan_metrics: Iterable[str] = (),
                   design_path: Path | str | None = None, code_dir: Path | None = None,
                   operator_gates: dict[str, float] | None = None, previous: dict[str, Any] | None = None,
                   fallback_item_sets: dict[str, dict] | None = None, retirements: Iterable[dict] = (),
                   answering: dict[str, Any] | None = None) -> dict[str, Any]:
    """The pre-registration the host holds the execution to (content only; ``save_protocol`` ids it).

    Required primary comparisons are the anchor comparisons ``rigor_stats.choose_pairs`` derives,
    within the original setting, over the pre-registered cells: all planned cells in a fresh run,
    the frozen (pre-revision) cells in a revision — post hoc cells are required to *run*, not to
    carry the hypothesis. Comparisons a review item requires are kept apart (``review_comparisons``).

    Item sets: the design's ``experiment-protocol`` block (or ``item_ids.json`` in the code), else
    the previous protocol's, else ``fallback_item_sets`` (in a revision: the frozen cells' items).
    Gates: the operator's, else the design's, else the default (recorded as not pre-registered).
    Anything that cannot be resolved is listed in ``problems`` and blocks verification.
    """
    block, problems = experiment_protocol.parse(design_text)
    problems = list(problems)
    block = block or {}
    primary = declared[0] if declared else None

    design_gates, gate_problems = experiment_protocol.check_gates(block.get("tier_gates"), "design")
    problems += gate_problems
    gates = {**design_gates, **(operator_gates or {})}
    sources = [s for s, given in (("design", design_gates), ("operator", operator_gates)) if given]
    gate_source = "+".join(sources) if gates and sources else "default"

    retired: dict[str, dict] = {}
    for entry in [*(block.get("retired") or []), *retirements]:
        why = experiment_protocol.check_retirement(entry)
        if why:
            problems.append(f"retirement record {json.dumps(entry, default=str)[:160]}: {why}")
            continue
        retired[str(entry["cell"])] = {k: entry.get(k) for k in _RETIREMENT_FIELDS}
    calibration = sorted(str(c) for c in block.get("calibration_cells") or [])
    item_sets, item_problems = experiment_protocol.resolve_item_sets(
        block, code_dir, inherited=[("previous protocol", (previous or {}).get("item_sets") or {}),
                                    ("revision frozen cells", fallback_item_sets or {})])
    problems += item_problems

    planned = sorted(n for n in dict.fromkeys(planned) if n not in retired and n not in calibration)
    frozen_set = set(frozen)
    pre_registered = [n for n in planned if n in frozen_set] if revision is not None else planned
    conditions = {n: rigor_stats.parse_condition(n) for n in planned}
    original = [n for n in pre_registered if not conditions[n].setting]
    pairs = rigor_stats.choose_pairs(original, conditions={n: conditions[n] for n in original}) if primary else []
    review, review_problems = experiment_protocol.review_comparisons(block, primary)
    problems += review_problems
    identity, basis = (experiment_protocol.design_identity(design_text, list(declared)) if design_text
                       else (None, None))
    body = {
        "schema": SCHEMA,
        "primary_metric": primary,
        "declared_metrics": list(declared),
        "plan_metrics": list(plan_metrics),
        "required_cells": planned,
        "required_comparisons": [{"metric": primary, "a": a, "b": b} for a, b in pairs],
        "review_comparisons": review,
        "missing_primary_rule": missing_primary_rule,
        "delivery_policy": delivery_policy,
        "design_sha256": sha256_text(design_text) if design_text else None,
        "design_identity": identity,
        "design_basis": basis,
        "design_path": str(design_path) if design_path else None,
        "revision": revision,
        "tier_gates": gates,
        "tier_gates_operator": dict(operator_gates or {}),
        "tier_gates_source": gate_source,
        "item_sets": item_sets,
        "retired": sorted(retired.values(), key=lambda r: r["cell"]),
        "calibration_cells": calibration,
        "cell_entries": {n: (block.get("cells") or {}).get(n) for n in planned if (block.get("cells") or {}).get(n)},
        "answering": dict(sorted((answering or {}).items())),
        "problems": problems,
    }
    body["cell_specs"] = {n: cell_spec(body, n) for n in planned}
    return body


def gate_for(protocol: dict[str, Any], name: str) -> tuple[float, str]:
    """(minimum activation rate, source) for a cell, from the protocol only — never from its output."""
    tier = rigor_stats.split_condition(rigor_stats.split_setting(name)[1])[1]
    gates = protocol.get("tier_gates") or {}
    if tier and tier in gates:
        return float(gates[tier]), f"protocol {protocol.get('tier_gates_source')} tier_gates[{tier}]"
    return DEFAULT_GATE, "default (no gate pre-registered for this tier)"


def setting_of(name: str) -> str:
    return rigor_stats.split_setting(name)[0]


def cell_spec(protocol: dict[str, Any], name: str) -> str:
    """Hash of everything that decides what a cell's result means; a result is reusable only under
    the same spec. Changing the cell's design entry (e.g. ``implementation``) changes it.
    """
    cond = rigor_stats.parse_condition(name)
    item_set = (protocol.get("item_sets") or {}).get(cond.setting) or {}
    tier = cond.tier
    gates = protocol.get("tier_gates") or {}
    return sha256_json({
        "name": name, "setting": cond.setting, "method": cond.method, "tier": tier,
        "primary": protocol.get("primary_metric"), "declared": protocol.get("declared_metrics"),
        "missing_primary_rule": protocol.get("missing_primary_rule"), "item_set": item_set.get("sha256"),
        "gate": gates.get(tier) if tier else None, "entry": (protocol.get("cell_entries") or {}).get(name),
        "answering": protocol.get("answering") or {},
    })[:16]


def protocol_id(protocol: dict[str, Any]) -> str:
    return sha256_json({k: v for k, v in protocol.items() if k not in _PROTOCOL_META})[:16]


def load_protocol(results_dir: Path) -> dict[str, Any] | None:
    return _read_json(evidence_dir(results_dir) / PROTOCOL_FILE)


def resolve_options(results_dir: Path) -> tuple[str, str, dict[str, float]]:
    """(missing_primary_rule, delivery_policy, operator tier gates): explicit options win, else the
    current protocol's, else the strict defaults.
    """
    current = load_protocol(results_dir) or {}
    rule = _CONFIG["missing_primary_rule"] or current.get("missing_primary_rule") or "refuse"
    policy = _CONFIG["delivery_policy"] or current.get("delivery_policy") or "confirmatory"
    gates = _CONFIG.get("tier_gates") or current.get("tier_gates_operator") or {}
    return rule, policy, dict(gates)


def save_protocol(results_dir: Path, body: dict[str, Any]) -> dict[str, Any]:
    """Freeze ``body`` as the current protocol (idempotent for identical content)."""
    pid = protocol_id(body)
    current = load_protocol(results_dir)
    if current is not None and current.get("protocol_id") == pid:
        return current
    protocol = {**body, "protocol_id": pid, "created_at": _now()}
    if current is not None:
        protocol["supersedes"] = current.get("protocol_id")
    folder = evidence_dir(results_dir)
    _write_json(folder / "protocols" / f"{pid}.json", protocol)
    _write_json(folder / PROTOCOL_FILE, protocol)
    return protocol


# --------------------------------------------------------------------------- execution record
def new_execution_id() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:6]


# --------------------------------------------------------------------------- evidence ledger
def load_ledger(results_dir: Path) -> dict[str, Any]:
    return _read_json(evidence_dir(results_dir) / LEDGER_FILE) or {"schema": SCHEMA, "cells": {}}


def _entry_digest(entry: dict[str, Any]) -> str:
    return sha256_json({k: v for k, v in entry.items() if k != "digest"})


def _add_version(results_dir: Path, name: str, entry: dict[str, Any]) -> None:
    ledger = load_ledger(results_dir)
    entry["digest"] = _entry_digest(entry)
    ledger["cells"].setdefault(name, []).append(entry)
    _write_json(evidence_dir(results_dir) / LEDGER_FILE, ledger)


def _version_problem(entry: dict[str, Any]) -> str:
    if entry.get("digest") != _entry_digest(entry):
        return "ledger entry edited (digest differs)"
    path = Path(entry.get("version_path") or "")
    if not path.is_file():
        return f"archived version {path.name} is missing"
    if sha256_file(path) != entry.get("metrics_sha256"):
        return f"archived version {path.name} changed (sha256 differs)"
    return ""


def reusable_version(results_dir: Path, name: str, spec: str | None) -> tuple[dict[str, Any] | None, str]:
    """(newest valid evidence version of ``name`` recorded under ``spec``, note).

    Valid = completed, no cell-level blocking finding, ledger entry and archived file unchanged. A
    damaged newer version does not hide an intact older one under the same spec. When only versions
    under another spec exist, the note says a re-run is required (they are kept).
    """
    entries = list(reversed(load_ledger(results_dir)["cells"].get(name, [])))
    valid = [e for e in entries if e.get("valid")]
    damaged = []
    for entry in valid:
        if spec is not None and entry.get("spec_sha256") == spec:
            problem = _version_problem(entry)
            if not problem:
                skipped = f"; newer v{', v'.join(damaged)} unusable" if damaged else ""
                return entry, f"v{entry['version']} (spec {spec}){skipped}"
            damaged.append(f"{entry['version']} ({problem})")
    if damaged:
        return None, f"no intact version under spec {spec}: v{', v'.join(damaged)}; re-run required"
    if valid:
        kept = ", ".join(f"v{e['version']}" for e in valid)
        return None, (f"spec changed since v{valid[0]['version']} ({valid[0].get('spec_sha256')} -> {spec}): "
                      f"re-run required; earlier versions kept ({kept})")
    return None, ""


def ledger_spec_of(results_dir: Path, name: str, metrics_sha256: str | None) -> str:
    """Spec under which the result now on disk was recorded (``legacy`` if the ledger does not know it)."""
    for entry in reversed(load_ledger(results_dir)["cells"].get(name, [])):
        if entry.get("valid") and entry.get("metrics_sha256") == metrics_sha256 and not _version_problem(entry):
            return str(entry.get("spec_sha256"))
    return SPEC_LEGACY


# --------------------------------------------------------------------------- retirements
def load_retirements(results_dir: Path) -> list[dict[str, Any]]:
    return (_read_json(evidence_dir(results_dir) / RETIREMENTS_FILE) or {}).get("retired", [])


def retire_cell(results_dir: Path, cell: str, *, reason: str, affected_comparisons: list[str],
                affected_claims: list[str], source: str = "operator") -> dict[str, Any]:
    """Record that ``cell`` leaves the design on purpose. It takes effect with the next protocol (the
    next execution, or ``jiuwenswarm-paper audit``); the cell's history and versions are kept.
    """
    entry = {"cell": cell, "reason": reason, "affected_comparisons": list(affected_comparisons),
             "affected_claims": list(affected_claims), "source": source, "at": _now()}
    why = experiment_protocol.check_retirement(entry)
    if why:
        raise ValueError(f"retirement of {cell}: {why}")
    entries = [e for e in load_retirements(results_dir) if e.get("cell") != cell] + [entry]
    _write_json(evidence_dir(results_dir) / RETIREMENTS_FILE, {"retired": entries})
    return entry


# --------------------------------------------------------------------------- declared item coverage
def item_coverage(protocol: dict[str, Any], name: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """The cell's records against the item set its setting pre-declared, for the primary metric.

    Missing items are counted by kind — no record at all, or a record without the primary value
    because it was unanswered / an API failure / a parse failure / for no recorded reason — and only
    the protocol's ``score_zero`` rule may score the three recorded kinds; a missing record never is.
    ``complete`` means the declared denominator is kept in full.
    """
    declared = (protocol.get("item_sets") or {}).get(setting_of(name))
    if not declared:
        return {"declared": False, "complete": False, "reason": f"no item set pre-declared for setting "
                                                                f"{setting_of(name) or 'original'!r}"}
    records = rigor_stats.records_of(metrics)
    ids, why = rigor_stats.item_ids(records)
    if ids is None:
        return {"declared": True, "declared_n": declared["n"], "complete": False, "reason": why}
    primary = protocol.get("primary_metric")
    fields = metrics.get("metric_fields") if isinstance(metrics.get("metric_fields"), dict) else {}
    col = fields.get(primary, primary)
    rule = protocol.get("missing_primary_rule", "refuse")
    by_id = dict(zip(ids, records))
    wanted = set(declared["ids"])
    missing_records = sorted(wanted - set(by_id))
    extra = sorted(set(by_id) - wanted)
    missing_metric: dict[str, int] = {}
    scored_zero = 0
    for item in wanted & set(by_id):
        if rigor_stats.as_number(by_id[item].get(col)) is not None:
            continue
        reason = rigor_stats.missing_reason(by_id[item])
        if rule == "score_zero" and reason in rigor_stats.MISSING_REASONS:
            scored_zero += 1
        else:
            missing_metric[reason] = missing_metric.get(reason, 0) + 1
    out = {"declared": True, "item_set_sha256": declared["sha256"], "declared_n": declared["n"],
           "records": len(records), "missing_records": len(missing_records), "missing_record_ids": missing_records[:10],
           "missing_metric": missing_metric, "scored_zero": scored_zero, "extra_records": len(extra),
           "extra_record_ids": extra[:10], "rule": rule}
    out["complete"] = not missing_records and not extra and not missing_metric
    if not out["complete"]:
        parts = []
        if missing_records:
            parts.append(f"{len(missing_records)} of {declared['n']} declared items have no record")
        if missing_metric:
            parts.append(f"declared items without `{primary}`: {missing_metric} (rule {rule})")
        if extra:
            parts.append(f"{len(extra)} records are not in the declared item set")
        out["reason"] = "; ".join(parts)
    return out


# --------------------------------------------------------------------------- comparison status
def _comparison_status(req: dict[str, Any], protocol: dict[str, Any], comparisons: list[dict[str, Any]],
                       cells: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One required (or review) comparison: ``failed`` if a side did not complete, else ``verified`` only
    if the statistics verified it *and* both sides cover the same pre-declared item set in full."""
    a, b, metric = req["a"], req["b"], req["metric"]
    failed = [s for s in (a, b) if cells.get(s, {}).get("status") != "completed"]
    if failed:
        return {**req, "status": "failed",
                "reason": "; ".join(f"{s} {cells.get(s, {}).get('status', 'not run')}" for s in failed)}
    match = next((c for c in comparisons if c["metric"] == metric and {c["a"], c["b"]} == {a, b}), None)
    if match is None:
        return {**req, "status": "unverified", "reason": "not computed in this execution"}
    entry = {**req, **{k: v for k, v in match.items() if k not in ("metric", "a", "b")}}
    if (match["a"], match["b"]) != (a, b) and "outcome" in entry:  # report from the protocol's side
        entry["outcome"] = {"a_better": "a_worse", "a_worse": "a_better"}.get(entry["outcome"], entry["outcome"])
        entry["mean_diff"] = -entry.get("mean_diff", 0.0)
        low, high = entry.get("ci95", [0.0, 0.0])
        entry["ci95"] = [-high, -low]
    if entry.get("status") != "verified" or metric != protocol.get("primary_metric"):
        return entry
    coverage = {s: cells[s].get("item_coverage") or {"complete": False, "reason": "item coverage not recorded"}
                for s in (a, b)}
    gaps = [f"{s}: {c.get('reason')}" for s, c in coverage.items() if not c.get("complete")]
    if not gaps and coverage[a].get("item_set_sha256") != coverage[b].get("item_set_sha256"):
        gaps.append("the two sides pre-declared different item sets")
    if gaps:
        entry.update(status="unverified", reason="declared item set not covered — " + "; ".join(gaps))
        for key in ("outcome", "verdict"):
            entry.pop(key, None)
    else:
        entry["n_declared"] = coverage[a].get("declared_n")
    return entry


def required_comparison_status(protocol: dict[str, Any], comparisons: list[dict[str, Any]],
                               cells: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [_comparison_status(req, protocol, comparisons, cells) for req in protocol.get("required_comparisons", [])]


def review_comparison_status(protocol: dict[str, Any], comparisons: list[dict[str, Any]],
                             cells: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Comparisons review items require; a recorded ``conditions`` claim is checked on both sides."""
    out = []
    for req in protocol.get("review_comparisons", []):
        entry = _comparison_status({k: req[k] for k in ("item", "metric", "a", "b")}, protocol, comparisons, cells)
        for key, wanted in (req.get("conditions") or {}).items():
            for side in (req["a"], req["b"]):
                have = setting_of(side) if key == "setting" else cells.get(side, {}).get(key)
                if entry["status"] == "verified" and str(have) != str(wanted):
                    entry.update(status="unverified",
                                 reason=f"{side}: {key} is {have!r}, the protocol requires {wanted!r}")
        out.append(entry)
    return out


def record_execution(results_dir: Path, protocol: dict[str, Any], *, execution_id: str, started_at: float,
                     revision: int | None, planned: Iterable[str], executed: Iterable[str],
                     frozen: Iterable[str], completed: dict[str, dict[str, Any]], findings: list[dict[str, Any]],
                     stats: rigor_stats.ExperimentStats | None, verdict: str, blocking: list[str],
                     reused: dict[str, dict[str, Any]] | None = None,
                     referenced_specs: dict[str, str] | None = None,
                     code_sha256: str | None = None) -> dict[str, Any]:
    """Write this execution's evidence manifest; returns it.

    ``reused``: cells referenced from a ledger version (name -> ledger entry) instead of re-run.
    ``referenced_specs``: the spec under which a referenced cell's result was recorded (default
    ``frozen`` for pre-revision frozen cells). Every executed cell that completed without a blocking
    finding becomes a new ledger version under the current spec; ``code_sha256`` (the experiment
    code's fingerprint) is stored with it so a later reuse can say the code changed since.
    """
    results_dir = Path(results_dir)
    folder = evidence_dir(results_dir)
    executed, frozen, reused = set(executed), set(frozen), dict(reused or {})
    referenced_specs = dict(referenced_specs or {})
    specs = protocol.get("cell_specs") or {}
    cells: dict[str, dict[str, Any]] = {}
    for name in sorted(set(planned) | set(completed)):
        path = results_dir / f"{name}.metrics.json"
        role = ("reused" if name in reused else "frozen" if name in frozen else
                "executed" if name in executed else "not_run")
        cell: dict[str, Any] = {"role": role, "status": "completed" if name in completed else
                                ("failed" if name in executed else "not_run"),
                                "metrics_path": str(path),
                                # comparison-level findings are judged per comparison, not per cell
                                "blocking": sorted({f["code"] for f in findings if _blocks_cell(f, name)})}
        if name in completed:
            metrics = completed[name]
            cell["metrics_sha256"] = sha256_file(path) if path.is_file() else None
            cell["records_sha256"] = sha256_json(rigor_stats.records_of(metrics))
            cell["n_items"] = len(rigor_stats.records_of(metrics))
            cell["item_coverage"] = item_coverage(protocol, name, metrics)
            cond = stats.conditions.get(name) if stats else None
            if cond is not None:
                cell.update(model=cond.model, dataset=cond.dataset, budget=cond.budget)
            if role == "executed" and path.is_file():
                history = folder / "cells" / name
                history.mkdir(parents=True, exist_ok=True)
                version = len(list(history.glob("v*.metrics.json"))) + 1
                copy = history / f"v{version}.metrics.json"
                shutil.copy2(path, copy)
                cell.update(version=version, version_path=str(copy), spec_sha256=specs.get(name))
            elif role == "reused":
                entry = reused[name]
                cell.update(version=entry.get("version"), version_path=entry.get("version_path"),
                            spec_sha256=entry.get("spec_sha256"), reused_from=entry.get("execution_id"))
                for key in ("model", "dataset", "budget"):  # conditions of the run that produced the version
                    if cell.get(key) is None and entry.get(key) is not None:
                        cell[key] = entry[key]
            elif role == "frozen":
                cell.update(version="frozen", spec_sha256=referenced_specs.get(name, SPEC_FROZEN))
        if role == "executed":
            (folder / "cells" / name).mkdir(parents=True, exist_ok=True)
            with (folder / "cells" / name / "history.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"execution_id": execution_id, "at": _now(), "status": cell["status"],
                                         "version": cell.get("version"), "spec_sha256": specs.get(name),
                                         "metrics_sha256": cell.get("metrics_sha256")}) + "\n")
            if cell.get("version_path"):
                _add_version(results_dir, name, {
                    "version": cell["version"], "execution_id": execution_id, "revision": revision,
                    "protocol_id": protocol["protocol_id"], "spec_sha256": specs.get(name),
                    "metrics_sha256": cell["metrics_sha256"], "records_sha256": cell["records_sha256"],
                    "version_path": cell["version_path"], "valid": not cell["blocking"], "blocking": cell["blocking"],
                    "model": cell.get("model"), "dataset": cell.get("dataset"), "budget": cell.get("budget"),
                    "n_items": cell["n_items"], "code_sha256": code_sha256, "recorded_at": _now()})
        cells[name] = cell
    comparisons = stats.comparisons if stats else []
    required = required_comparison_status(protocol, comparisons, cells)
    manifest = {
        "schema": SCHEMA,
        "protocol_id": protocol["protocol_id"],
        "execution_id": execution_id,
        "revision": revision,
        "started_at": started_at,
        "recorded_at": _now(),
        "audit": {"verdict": verdict, "blocking": blocking},
        "cells": cells,
        "required_comparisons": required,
        "review_comparisons": review_comparison_status(protocol, comparisons, cells),
        "primary_hypothesis_verified": bool(required) and all(r["status"] == "verified" for r in required),
        "comparisons": comparisons,
        "coverage": stats.coverage if stats else {},
        "missing_primary_rule": protocol.get("missing_primary_rule"),
        "delivery_policy": protocol.get("delivery_policy"),
    }
    manifest["digest"] = sha256_json(manifest)
    _write_json(folder / "executions" / f"{execution_id}.json", manifest)
    _write_json(folder / MANIFEST_FILE, manifest)
    return manifest


def record_unverified(results_dir: Path, reason: str, *, started_at: float | None = None,
                      revision: int | None = None) -> dict[str, Any]:
    """The audit of an execution crashed: replace the current manifest so that earlier evidence is
    not read as describing the (possibly changed) results on disk.
    """
    protocol = load_protocol(results_dir) or {}
    execution_id = new_execution_id()
    manifest = {"schema": SCHEMA, "protocol_id": protocol.get("protocol_id"), "execution_id": execution_id,
                "revision": revision, "started_at": started_at, "recorded_at": _now(),
                "audit": {"verdict": "unverified", "blocking": [f"AUDIT_CRASHED: {reason}"]}, "cells": {},
                "required_comparisons": [], "primary_hypothesis_verified": False, "comparisons": [], "coverage": {}}
    manifest["digest"] = sha256_json(manifest)
    folder = evidence_dir(results_dir)
    _write_json(folder / "executions" / f"{execution_id}.json", manifest)
    _write_json(folder / MANIFEST_FILE, manifest)
    return manifest


def load_manifest(results_dir: Path) -> dict[str, Any] | None:
    return _read_json(evidence_dir(results_dir) / MANIFEST_FILE)


# --------------------------------------------------------------------------- verification
_TASKS = {
    "EVIDENCE_UNREADABLE": "an evidence file is not valid JSON: restore it, or re-audit / re-execute to rewrite it",
    "NO_PROTOCOL": "run the experiment through the host execution step (it freezes the protocol first)",
    "NO_EVIDENCE_MANIFEST": "execute the experiment (or re-audit existing results with `jiuwenswarm-paper audit`)",
    "PROTOCOL_CHANGED": "the protocol changed since the evidence was produced: re-execute under the current design "
                        "(compatible cells are reused, not re-run) or re-audit with `jiuwenswarm-paper audit`",
    "PROTOCOL_INVALID": "fix the design's `experiment-protocol` block (or the operator options) listed here, then "
                        "re-execute or re-audit",
    "DESIGN_CHANGED": "the design changed after the protocol was frozen: re-execute (or re-audit) so the protocol "
                      "and the evidence match it",
    "DESIGN_UNVERIFIED": "the current design could not be located or read: restore experiment_design.md (the path "
                         "is in the protocol) or re-audit with --design",
    "TAMPERED": "evidence files were edited by hand: re-execute",
    "AUDIT_NOT_PASSED": "fix the blocking audit errors (code_implementation repair) and re-execute",
    "CELL_NOT_COMPLETED": "make the listed cells run to completion (repair the code) and re-execute",
    "CELL_BLOCKED": "fix the audit errors of the listed cells and re-execute them",
    "CELL_SPEC_CHANGED": "the cell's condition or implementation entry changed since its result was recorded: "
                         "re-run that cell under the current protocol",
    "RESULT_CHANGED": "a result changed after it was audited: re-execute that cell (never edit result files)",
    "PRIMARY_COMPARISON_FAILED": "a required comparison lost a cell: repair and re-execute that cell",
    "PRIMARY_COMPARISON_UNVERIFIED": "make the required comparison computable (same item ids, recorded equal "
                                     "budget/model/dataset, complete primary metric over the declared item set) "
                                     "and re-execute",
    "ITEM_SET_UNDECLARED": "declare the item set before execution (`item_sets` in the design's experiment-protocol "
                           "block, or item_ids.json in the experiment code) and re-execute",
    "NO_PRIMARY_COMPARISON": "declare a primary metric and an anchor (`proposed`) variant in the design",
    "NOT_THIS_REVISION": "execute the revision's cells: this evidence predates the revision",
    "REVISION_CELL_WITHOUT_EVIDENCE": "the revision executed a cell that has no passing evidence now: re-execute "
                                      "it until it passes (a valid earlier version is reused automatically)",
    "REVISION_CELL_DROPPED": "a cell this revision executed is no longer in the design: keep it, or retire it "
                             "explicitly (`retired` in the experiment-protocol block, or `jiuwenswarm-paper retire`) "
                             "with the reason and the affected comparisons / claims",
}


def locate_design(protocol: dict[str, Any], results_dir: Path) -> Path | None:
    """The design file the protocol was frozen from, else the pipeline's conventional location."""
    candidates = [Path(protocol["design_path"])] if protocol.get("design_path") else []
    candidates.append(Path(results_dir).parent / "design" / "experiment_design.md")
    for path in candidates:
        if path.is_file():
            return path
    found = sorted(Path(results_dir).parent.rglob("experiment_design.md")) if Path(results_dir).parent.is_dir() else []
    return found[0] if found else None


def design_problem(protocol: dict[str, Any], results_dir: Path, design_text: str | None = None) -> tuple[str, str]:
    """("", "") when the current design is the one the protocol froze, else (code, detail)."""
    if not protocol.get("design_identity") and not protocol.get("design_sha256"):
        return "DESIGN_UNVERIFIED", "the protocol was frozen without a design, so no design can be checked against it"
    if design_text is None:
        path = locate_design(protocol, results_dir)
        if path is None:
            return "DESIGN_UNVERIFIED", f"design not found (protocol path {protocol.get('design_path')!r})"
        try:
            design_text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, ValueError) as exc:
            return "DESIGN_UNVERIFIED", f"design {path} unreadable ({exc!r})"
    if not protocol.get("design_identity"):  # a protocol from before structured identities
        if sha256_text(design_text) != protocol["design_sha256"]:
            return "DESIGN_CHANGED", "the experiment design differs from the one the protocol froze"
        return "", ""
    declared = experiment_protocol.declared_metrics(design_text, protocol.get("plan_metrics") or [])
    identity, basis = experiment_protocol.design_identity(design_text, declared)
    if identity != protocol["design_identity"]:
        frozen_basis = protocol.get("design_basis")
        what = ("its experiment-protocol block or declared metrics" if basis == frozen_basis == "structured"
                else "its text" if basis == "full_text" == frozen_basis else "its protocol block")
        return "DESIGN_CHANGED", (f"the experiment design ({what}) differs from the one protocol "
                                  f"{protocol.get('protocol_id')} froze")
    return "", ""


def verify(results_dir: Path, *, revision: Any = None, design_text: str | None = None) -> dict[str, Any]:
    """Whether the run's evidence supports delivery, re-checked from disk now (hashes included).

    The current design is located and checked here (``design_text`` overrides reading it), so no
    caller can skip it. ``revision``: a ``revision_state.RevisionState`` whose evidence must come
    from its own executions (a writing-only revision only needs the existing evidence to be intact).
    Returns ``{"ok", "primary_hypothesis_verified", "policy", "blocking": [{code, detail}],
    "pending_tasks", "limitations", "verified_comparisons", "unverified_comparisons",
    "review_comparisons", "retired", ...}``.
    """
    results_dir = Path(results_dir)
    blocking: list[dict[str, str]] = []
    limitations: list[str] = []

    def block(code: str, detail: str) -> None:
        blocking.append({"code": code, "detail": detail})

    out: dict[str, Any] = {"ok": False, "primary_hypothesis_verified": False, "policy": None, "protocol_id": None,
                           "execution_id": None, "blocking": blocking, "limitations": limitations,
                           "verified_comparisons": [], "unverified_comparisons": [], "review_comparisons": [],
                           "retired": [], "checked_at": _now()}
    try:
        protocol = load_protocol(results_dir)
        manifest = load_manifest(results_dir)
    except EvidenceFileError as exc:  # fail closed with a reason instead of crashing the caller
        block("EVIDENCE_UNREADABLE", str(exc))
        return finalize(out)
    if protocol is None:
        block("NO_PROTOCOL", f"no {EVIDENCE_DIR}/{PROTOCOL_FILE} next to {results_dir}")
    if manifest is None:
        block("NO_EVIDENCE_MANIFEST", f"no {EVIDENCE_DIR}/{MANIFEST_FILE} next to {results_dir}")
    if protocol is None or manifest is None:
        return finalize(out)
    policy = protocol.get("delivery_policy", "confirmatory")
    out.update(policy=policy, protocol_id=protocol.get("protocol_id"), execution_id=manifest.get("execution_id"),
               primary_metric=protocol.get("primary_metric"), retired=protocol.get("retired") or [])
    if protocol_id(protocol) != protocol.get("protocol_id"):
        block("TAMPERED", "protocol.json content does not match its id")
    if sha256_json({k: v for k, v in manifest.items() if k != "digest"}) != manifest.get("digest"):
        block("TAMPERED", "manifest.json content does not match its digest")
    if manifest.get("protocol_id") != protocol.get("protocol_id"):
        block("PROTOCOL_CHANGED", f"evidence audited under protocol {manifest.get('protocol_id')}, current protocol "
                                  f"is {protocol.get('protocol_id')}")
    for problem in protocol.get("problems") or []:
        block("PROTOCOL_INVALID", problem)
    code, detail = design_problem(protocol, results_dir, design_text)
    if code:
        block(code, detail)
    audit = manifest.get("audit") or {}
    if audit.get("verdict") != "passed":
        block("AUDIT_NOT_PASSED", f"audit verdict {audit.get('verdict')}: "
              + " | ".join(map(str, (audit.get("blocking") or [])[:4]))[:600])

    cells = manifest.get("cells") or {}
    specs = protocol.get("cell_specs") or {}
    legacy = []
    for name in protocol.get("required_cells", []):
        cell = cells.get(name)
        if cell is None or cell.get("status") != "completed":
            block("CELL_NOT_COMPLETED", f"{name}: {cell.get('status') if cell else 'not in this execution'}")
            continue
        if cell.get("blocking"):
            block("CELL_BLOCKED", f"{name}: {', '.join(cell['blocking'])}")
        problem = hash_problem(cell)
        if problem:
            block("RESULT_CHANGED", f"{name}: {problem}")
        recorded = cell.get("spec_sha256")
        if recorded == SPEC_LEGACY:
            legacy.append(name)
        elif recorded not in (None, SPEC_FROZEN) and specs.get(name) and recorded != specs[name]:
            block("CELL_SPEC_CHANGED", f"{name}: result recorded under spec {recorded}, the current protocol "
                                       f"requires {specs[name]}")
    if legacy:
        limitations.append(f"no evidence version records the spec of {', '.join(legacy)} (results from before the "
                           "evidence ledger): their compatibility with the current protocol was not checked")

    settings = sorted({setting_of(n) for n in protocol.get("required_cells", [])})
    item_sets = protocol.get("item_sets") or {}
    undeclared = [s for s in settings if s not in item_sets]
    if undeclared:
        detail = ("no pre-declared item set for setting(s) " + ", ".join(repr(s or "original") for s in undeclared)
                  + ": a shared omission of items could not be detected")
        if policy == "confirmatory":
            block("ITEM_SET_UNDECLARED", detail)
        else:
            limitations.append(detail)
    for setting, entry in sorted(item_sets.items()):
        if str(entry.get("source", "")).startswith("operator"):
            limitations.append(f"the item set of setting {setting or 'original'!r} was declared by the operator "
                               "after execution, not pre-registered")
    if protocol.get("tier_gates_source") == "default":
        limitations.append(f"no activation gate was pre-registered; the default {DEFAULT_GATE:.0%} gate was applied "
                           "(gates reported by the experiment output are ignored)")

    required = manifest.get("required_comparisons") or []
    wanted = {(r["metric"], r["a"], r["b"]) for r in protocol.get("required_comparisons", [])}
    if {(r["metric"], r["a"], r["b"]) for r in required} != wanted:
        block("PROTOCOL_CHANGED", "the manifest's required comparisons differ from the protocol's")
    for entry in required:
        label = f"{entry['metric']}: {entry['a']} vs {entry['b']}"
        if entry["status"] == "verified":
            out["verified_comparisons"].append(entry)
            continue
        out["unverified_comparisons"].append(entry)
        code = "PRIMARY_COMPARISON_FAILED" if entry["status"] == "failed" else "PRIMARY_COMPARISON_UNVERIFIED"
        if policy == "confirmatory":
            block(code, f"{label} — {entry.get('reason')}")
        else:
            limitations.append(f"{label} is {entry['status']} ({entry.get('reason')}): no claim about it may be made; "
                               "missing evidence is not a null result")
    verified_count = len(out["verified_comparisons"])
    out["primary_hypothesis_verified"] = bool(wanted) and verified_count == len(required) == len(wanted)
    if not wanted:
        if policy == "confirmatory":
            block("NO_PRIMARY_COMPARISON", "the protocol declares no primary comparison")
        else:
            limitations.append("no primary comparison was pre-registered: the paper is descriptive")
    if policy == "descriptive" and not out["primary_hypothesis_verified"]:
        limitations.append("delivery policy `descriptive`: the paper may not state that the primary hypothesis was "
                           "tested or confirmed")
    # review comparisons do not block delivery here: an item may be answered by narrowing the claim;
    # `revision.check_response` refuses to count an item as resolved by evidence without them
    out["review_comparisons"] = manifest.get("review_comparisons") or []
    for entry in out["retired"]:
        limitations.append(f"cell {entry['cell']} was retired from the design ({entry.get('reason')}); affected "
                           f"comparisons {entry.get('affected_comparisons') or 'none'}, claims "
                           f"{entry.get('affected_claims') or 'none'}: no result of it may be reported as evidence")

    if revision is not None and getattr(revision, "mode", "experiments") != "writing_only":
        opened = datetime.fromisoformat(revision.created_at).timestamp()
        if manifest.get("revision") != revision.index or float(manifest.get("started_at") or 0) < opened:
            block("NOT_THIS_REVISION", f"latest evidence is from execution {manifest.get('execution_id')} "
                                       f"(revision {manifest.get('revision')}), not from revision {revision.index}")
        # every cell the revision attempted, including one a later design dropped after it ran
        attempted = set(getattr(revision, "executed_new_cells", [])) | set(getattr(revision, "cell_executions", {}))
        retired = {r["cell"] for r in out["retired"]}
        required_cells = set(protocol.get("required_cells", []))
        for name in sorted(attempted):
            if name in retired:
                continue
            if name not in required_cells:
                block("REVISION_CELL_DROPPED", f"{name} was executed in this revision but is not in the current "
                                               "design and has no retirement record")
                continue
            cell = cells.get(name)
            if cell is None or cell.get("status") != "completed" or cell.get("blocking"):
                block("REVISION_CELL_WITHOUT_EVIDENCE",
                      f"{name}: {'not in the latest execution' if cell is None else cell.get('status')}"
                      + (f" ({', '.join(cell['blocking'])})" if cell and cell.get("blocking") else ""))
    out["coverage"] = manifest.get("coverage") or {}
    return finalize(out)


def hash_problem(cell: dict[str, Any]) -> str:
    path = Path(cell.get("metrics_path") or "")
    expected = cell.get("metrics_sha256")
    if not expected:
        return "no recorded hash"
    if not path.is_file():
        return f"{path.name} is missing"
    if sha256_file(path) != expected:
        return f"{path.name} changed after the audit (sha256 differs)"
    copy = cell.get("version_path")
    if copy and (not Path(copy).is_file() or sha256_file(Path(copy)) != expected):
        return f"archived evidence version {Path(copy).name} missing or changed"
    return ""


def finalize(out: dict[str, Any]) -> dict[str, Any]:
    out["ok"] = not out["blocking"]
    seen: list[str] = []
    for item in out["blocking"]:
        task = f"{item['code']}: {_TASKS.get(item['code'], 'fix and re-execute')}"
        if task not in seen:
            seen.append(task)
    out["pending_tasks"] = seen
    return out


def summary_line(result: dict[str, Any]) -> str:
    if result["ok"]:
        return ("evidence verified" + (" (primary hypothesis tested)" if result["primary_hypothesis_verified"]
                                       else " (descriptive delivery)"))
    return "evidence not accepted: " + " | ".join(f"{b['code']} {b['detail']}" for b in result["blocking"][:4])[:700]


def writing_brief(result: dict[str, Any]) -> str:
    """What the paper may and may not claim, for the writing agent (host-generated, deterministic)."""
    lines = ["## Host evidence manifest (binding for the paper)", "",
             f"Status: {summary_line(result)}. Protocol {result.get('protocol_id')}, execution "
             f"{result.get('execution_id')}, delivery policy {result.get('policy')}.", ""]
    if result["verified_comparisons"]:
        lines.append("Verified primary comparisons (state each with its paired difference, 95% CI and Holm p):")
        for c in result["verified_comparisons"]:
            lines.append(f"- {c['metric']}: {c['a']} vs {c['b']} — outcome `{c.get('outcome')}`, mean diff "
                         f"{c.get('mean_diff')}, 95% CI {c.get('ci95')}, p_holm {c.get('p_holm')}, "
                         f"n {c.get('n_paired')}")
    if result["unverified_comparisons"]:
        lines.append("NOT verified — do not state any result for these; describe them only as limitations "
                     "(missing evidence is not evidence of no effect):")
        lines += [f"- {c['metric']}: {c['a']} vs {c['b']} — {c['status']}: {c.get('reason')}"
                  for c in result["unverified_comparisons"]]
    review = result.get("review_comparisons") or []
    if review:
        lines.append("Comparisons required by review items (a verified negative or null outcome still answers the "
                      "item; an unverified one does not — then narrow the claim or state the limitation):")
        for c in review:
            tail = (f"outcome `{c.get('outcome')}`, mean diff {c.get('mean_diff')}, 95% CI {c.get('ci95')}"
                    if c["status"] == "verified" else f"{c['status']}: {c.get('reason')}")
            lines.append(f"- `{c['item']}` {c['metric']}: {c['a']} vs {c['b']} — {tail}")
    if result.get("retired"):
        lines.append("Retired cells (out of the design; report none of their results as evidence):")
        lines += [f"- {r['cell']}: {r.get('reason')}" for r in result["retired"]]
    if result["limitations"]:
        lines += ["Required limitations:"] + [f"- {item}" for item in result["limitations"]]
    if result["blocking"]:
        lines += ["Blocking problems (the paper cannot be delivered until they are fixed):"]
        lines += [f"- {b['code']}: {b['detail']}" for b in result["blocking"][:8]]
    return "\n".join(lines) + "\n"
