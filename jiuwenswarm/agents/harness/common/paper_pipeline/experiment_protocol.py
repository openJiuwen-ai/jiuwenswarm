"""The structured part of an experiment design: what the host freezes into the evidence protocol.

A design is prose; a few things in it must be machine-checkable before execution. They live in one
fenced JSON block of the design file::

    ```experiment-protocol
    {
      "item_sets": {"": {"dataset": "hotpotqa-dev", "ids_file": "item_ids.json"},
                    "m2": {"same_as": ""}},
      "tier_gates": {"t1": 0.5, "t2": 0.3},
      "calibration_cells": ["calib_reference"],
      "cells": {"abl_rank_T1": {"implementation": "v2", "post_hoc": true}},
      "review_comparisons": [{"item": "R-1a2b3c4d", "a": "proposed_T1", "b": "abl_rank_T1"}],
      "retired": [{"cell": "abl_old_T1", "reason": "...", "affected_comparisons": ["proposed_T1 vs abl_old_T1"],
                   "affected_claims": ["component X helps"]}]
    }
    ```

* ``item_sets`` — per setting (the variant-name prefix, ``""`` = original): the pre-declared item ids
  (``ids`` inline, or ``ids_file`` relative to the experiment code directory: a JSON list, a JSON
  object with ``ids``, or one id per line), the dataset identity, or ``same_as`` another setting.
* ``tier_gates`` — the minimum constraint-activation rate per tier, in [0, 1]. Frozen before the
  formal execution; ``calibration_cells`` (and nothing else) may run before the gates are fixed and
  are never evidence.
* ``cells`` — per-cell entries; any change to a cell's entry (e.g. ``implementation``) marks its
  earlier results incompatible, so the host re-runs it instead of reusing them.
* ``review_comparisons`` — the comparisons a review item requires (bound to its stable id).
* ``retired`` — cells taken out of the design, with why and what comparisons / claims they affect.

The design's identity is the hash of this block plus the declared metric names when the block is
present (so rewording the prose does not force a re-execution), else the hash of the full text.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

BLOCK_TAG = "experiment-protocol"
_BLOCK = re.compile(r"```" + BLOCK_TAG + r"[ \t]*\r?\n(.*?)```", re.DOTALL)
KEYS = ("item_sets", "tier_gates", "calibration_cells", "cells", "review_comparisons", "retired")
ITEM_IDS_FILE = "item_ids.json"  # host convention when the design declares no item set
MIN_RETIRE_REASON = 20


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


_METRIC_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?Metric\s+`?([A-Za-z][A-Za-z0-9_]*)`?", re.MULTILINE)


def declared_metrics(design_text: str, fallback: list[str]) -> list[str]:
    """Metric names the design declares (``Metric <name>`` lines, primary first), then ``fallback``."""
    names = [m for m in _METRIC_LINE.findall(design_text or "")]
    names += [m for m in fallback if m]
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def parse(design_text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """(block or None, problems). More than one block, invalid JSON or unknown keys are problems."""
    blocks = _BLOCK.findall(design_text or "")
    if not blocks:
        return None, []
    if len(blocks) > 1:
        return None, [f"{len(blocks)} `{BLOCK_TAG}` blocks in the design; exactly one is allowed"]
    try:
        block = json.loads(blocks[0])
    except ValueError as exc:
        return None, [f"`{BLOCK_TAG}` block is not valid JSON: {exc}"]
    if not isinstance(block, dict):
        return None, [f"`{BLOCK_TAG}` block must be a JSON object"]
    problems = [f"unknown key `{k}` in the `{BLOCK_TAG}` block" for k in block if k not in KEYS]
    return block, problems


def design_identity(design_text: str, declared: list[str]) -> tuple[str, str]:
    """(identity sha256, basis): ``structured`` when the design carries a valid block, else ``full_text``."""
    block, problems = parse(design_text)
    if block is not None and not problems:
        return _sha({"protocol": block, "declared_metrics": list(declared)}), "structured"
    return hashlib.sha256((design_text or "").encode("utf-8")).hexdigest(), "full_text"


def check_gates(gates: Any, source: str) -> tuple[dict[str, float], list[str]]:
    """Tier -> gate in [0, 1] (tiers lower-cased); out-of-range or non-numeric values are problems."""
    if gates in (None, {}):
        return {}, []
    if not isinstance(gates, dict):
        return {}, [f"{source} tier_gates must be an object tier -> rate"]
    out, problems = {}, []
    for tier, raw in gates.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0.0 <= float(raw) <= 1.0:
            problems.append(f"{source} tier_gates[{tier}] = {raw!r} is not a rate in [0, 1]")
            continue
        out[str(tier).lower()] = float(raw)
    return out, problems


def parse_gate_options(values: list[str]) -> dict[str, float]:
    """``["T1=0.5", "t2=0.3"]`` (CLI) -> {"t1": 0.5, "t2": 0.3}; raises ValueError when out of range."""
    raw: dict[str, Any] = {}
    for value in values:
        tier, sep, rate = value.partition("=")
        if not sep or not tier.strip():
            raise ValueError(f"--tier-gate {value!r}: expected TIER=RATE")
        try:
            raw[tier.strip()] = float(rate)
        except ValueError as exc:
            raise ValueError(f"--tier-gate {value!r}: rate is not a number") from exc
    gates, problems = check_gates(raw, "--tier-gate")
    if problems:
        raise ValueError("; ".join(problems))
    return gates


def read_ids(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        return [line.strip() for line in text.splitlines() if line.strip()]
    if isinstance(data, dict):
        data = data.get("ids", data.get("item_ids"))
    if not isinstance(data, list):
        raise ValueError("expected a list of ids, or an object with `ids`")
    return [str(i) for i in data]


def item_set_entry(ids: list[str], *, dataset: Any, source: str) -> tuple[dict[str, Any] | None, str]:
    if not ids:
        return None, "the declared item set is empty"
    if len(set(ids)) != len(ids):
        return None, "the declared item set repeats ids"
    ordered = sorted(ids)
    return {"ids": ordered, "n": len(ordered), "sha256": _sha(ordered), "dataset": dataset, "source": source}, ""


def resolve_item_sets(block: dict[str, Any] | None, code_dir: Path | None,
                      inherited: list[tuple[str, dict[str, dict]]] = ()) -> tuple[dict[str, dict], list[str]]:
    """setting -> {ids, n, sha256, dataset, source} declared before execution, and problems.

    Order: the block's entries, the code's ``item_ids.json`` (original setting), then each
    ``inherited`` (label, sets) source for settings still undeclared; ``same_as`` resolves last.
    """
    specs = (block or {}).get("item_sets") or {}
    out: dict[str, dict] = {}
    problems: list[str] = []
    if not isinstance(specs, dict):
        return {}, ["item_sets must be an object setting -> spec"]
    aliases = {}
    for setting, spec in specs.items():
        if not isinstance(spec, dict):
            problems.append(f"item_sets[{setting!r}] must be an object")
            continue
        if "same_as" in spec:
            aliases[setting] = str(spec["same_as"])
            continue
        try:
            if isinstance(spec.get("ids"), list):
                ids, source = [str(i) for i in spec["ids"]], "design"
            elif spec.get("ids_file"):
                path = Path(spec["ids_file"])
                if not path.is_absolute() and code_dir is not None:
                    path = Path(code_dir) / path
                ids, source = read_ids(path), f"design ids_file {spec['ids_file']}"  # relative: run dir may move
            else:
                problems.append(f"item_sets[{setting!r}] needs `ids`, `ids_file` or `same_as`")
                continue
        except (OSError, ValueError) as exc:
            problems.append(f"item_sets[{setting!r}]: {exc}")
            continue
        entry, why = item_set_entry(ids, dataset=spec.get("dataset"), source=source)
        if entry is None:
            problems.append(f"item_sets[{setting!r}]: {why}")
        else:
            out[setting] = entry
    code_ids = Path(code_dir) / ITEM_IDS_FILE if code_dir is not None else None
    if "" not in {**specs, **out} and code_ids is not None and code_ids.is_file():
        try:
            entry, why = item_set_entry(read_ids(code_ids), dataset=None, source=f"code {ITEM_IDS_FILE}")
        except (OSError, ValueError) as exc:
            entry, why = None, str(exc)
        if entry is None:
            problems.append(f"{ITEM_IDS_FILE}: {why}")
        else:
            out[""] = entry
    for label, sets in inherited:
        for setting, entry in (sets or {}).items():
            if setting not in out and setting not in specs and entry:
                out[setting] = {**entry, "source": entry.get("source") or label}
    for setting, target in aliases.items():
        if target in out:
            out[setting] = {**out[target], "source": f"same_as {target!r}"}
        else:
            problems.append(f"item_sets[{setting!r}] same_as {target!r}, which declares no item set")
    return out, problems


def check_retirement(entry: Any) -> str:
    """Why a retirement record is not acceptable ("" when it is)."""
    if not isinstance(entry, dict) or not entry.get("cell"):
        return "needs `cell`"
    if len(str(entry.get("reason", "")).strip()) < MIN_RETIRE_REASON:
        return "needs a substantive `reason`"
    comparisons, claims = entry.get("affected_comparisons"), entry.get("affected_claims")
    if not isinstance(comparisons, list) or not isinstance(claims, list):
        return "needs `affected_comparisons` and `affected_claims` (lists; empty only when nothing is affected)"
    return ""


def review_comparisons(block: dict[str, Any] | None, primary: str | None) -> tuple[list[dict], list[str]]:
    """[{item, a, b, metric, conditions}] bound to review item ids, and problems."""
    out, problems = [], []
    for raw in (block or {}).get("review_comparisons") or []:
        if not isinstance(raw, dict) or not all(raw.get(k) for k in ("item", "a", "b")):
            problems.append(f"review_comparisons entry needs `item`, `a` and `b`: {json.dumps(raw)[:160]}")
            continue
        metric = raw.get("metric") or primary
        conditions = raw.get("conditions") if isinstance(raw.get("conditions"), dict) else {}
        out.append({"item": str(raw["item"]), "a": str(raw["a"]), "b": str(raw["b"]), "metric": metric,
                    "conditions": conditions})
    return out, problems
