#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Certify the harness's own auditors on a real manuscript, with no model call.

The universe is the verified registry the experiment stage wrote: every printed
number that was bound to a record, at the line it is printed. Each round plants
K perturbed values into the LaTeX sources, hands the planted text to an auditor,
and tests its accusations exactly (see rails/auditor_certificate.py).

Two auditors ship with the harness and both are certified here:

``registry_trace``
    The delivery gate's number check (`numbers_trace_to_registry`): a printed
    number with no registry row is accused.
``rigor_floor``
    The arithmetic floor of `RigorAuditRail` (`audit_text`): GRIM, percentage,
    interval and p-value feasibility. Needs the framework installed; skipped
    when it is not.

    python3 scripts/certify_auditor.py --registry run_records/completion/verified_registry.json \\
        --source-dir paper/source --out certificates.json

The output holds one certificate per auditor and every round's ledger
commitment, so a reader can check the rounds were fixed before the audits.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

RAILS = Path(__file__).resolve().parents[1] / "jiuwenswarm/agents/harness/team/rails"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cert = _load("auditor_certificate", RAILS / "auditor_certificate.py")
gate = _load("delivery_gate", RAILS / "delivery_gate.py")


def _token(literal: str) -> re.Pattern:
    return re.compile(r"(?<![\w.])%s(?![\w]|\.\d)" % re.escape(literal))


def universe_from_registry(entries: list[dict], source_dir: Path) -> dict[str, str]:
    """Slot per registered printing: name 'file:line#literal', value the literal.

    A registry row whose literal is not printed as a number at its recorded
    line (a layout length such as "0.4pt", a stale line number) cannot be
    planted there, so it is not part of the declared universe.
    """
    lines: dict[str, list[str]] = {}
    universe = {}
    for e in entries:
        name, line = e["printed_at"].rsplit(":", 1)
        if name not in lines:
            lines[name] = (source_dir / name).read_text(encoding="utf-8").splitlines()
        if _token(e["literal"]).search(lines[name][int(line) - 1]):
            universe[f"{e['printed_at']}#{e['literal']}"] = e["literal"]
    return universe


def renderer(source_dir: Path, universe: dict[str, str]):
    """render(facts) -> the manuscript text with each slot's literal at its printed line."""
    files = {}
    for slot in universe:
        name = slot.split(":", 1)[0]
        if name not in files:
            files[name] = (source_dir / name).read_text(encoding="utf-8").splitlines()

    def render(facts: dict[str, str]) -> str:
        lines = {name: list(body) for name, body in files.items()}
        for slot, value in facts.items():
            where, literal = slot.split("#", 1)
            name, line = where.rsplit(":", 1)
            if value == literal:
                continue
            i = int(line) - 1
            lines[name][i] = _token(literal).sub(value, lines[name][i], count=1)
        return "\n".join("\n".join(body) for body in lines.values())

    return render


def registry_trace_auditor(entries: list[dict]):
    registry = {e["literal"]: e["record_file"] for e in entries}

    def audit(text: str) -> list[str]:
        return [f.detail.split(" ", 1)[0] for f in gate.numbers_trace_to_registry(text, registry)]

    return audit


def rigor_floor_auditor():
    try:
        from jiuwenswarm.agents.harness.team.rails.rigor_audit_rail import audit_text
    except ImportError:
        return None

    def audit(text: str) -> list[str]:
        return [tok for f in audit_text(text) for tok in cert.NUMBER.findall(f.evidence)]

    return audit


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--registry", type=Path, required=True)
    ap.add_argument("--source-dir", type=Path, required=True)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--null-rounds", type=int, default=20)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--delta", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=Path("certificates.json"))
    args = ap.parse_args()

    entries = json.loads(args.registry.read_text(encoding="utf-8"))["entries"]
    universe = universe_from_registry(entries, args.source_dir)
    render = renderer(args.source_dir, universe)
    auditors = {"registry_trace": registry_trace_auditor(entries), "rigor_floor": rigor_floor_auditor()}
    out = {}
    for name, audit in auditors.items():
        if audit is None:
            print(f"{name}: skipped (framework not installed)")
            continue
        c = cert.certify(universe, render, audit, name=name, rounds=args.rounds, k=args.k,
                         delta=args.delta, null_rounds=args.null_rounds, seed=args.seed)
        rec = c.to_record()
        out[name] = rec
        verdict = f"certified at round {rec['certified_at_round']}" if rec["certified"] else "not certified"
        print(f"{name}: U' {rec['collision_free_slots']}/{rec['declared_slots']} slots, recall "
              f"{rec['recall']:.2f}, rejections {rec['rejections']}/{rec['rounds']}, e {rec['e_final']:.3g}, "
              f"{verdict}; false-accusation bound {rec['false_accusation_upper']:.3f}")
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"certificates: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
