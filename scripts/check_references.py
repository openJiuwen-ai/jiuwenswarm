#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Check every reference a manuscript cites against the DOI registry.

    python3 scripts/check_references.py --manuscript <paper>/source/main.tex \\
        --bib <paper>/source/references.bib --out ../run_records/reference_check.json [--replay]

The manuscript is read with every file it pulls in through \\input / \\include.
For each cited key the delivery gate reports: no bibliography entry (dangling),
an entry with neither DOI nor arXiv id (cannot be re-checked), or an identifier
the DOI registry does not know. The registry's answers are stored next to the
report; --replay rebuilds the report from them without the network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RAILS = REPO / "jiuwenswarm/agents/harness/team/rails"
EVIDENCE_BIND = (REPO / "jiuwenswarm/resources/agent/workspace/skills"
                 / "scholar-paper-writer/scripts/evidence_bind.py")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def parse_bib(text: str) -> dict[str, str]:
    """BibTeX key -> the raw body of its entry."""
    return {m.group(1): m.group(2)
            for m in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)\n\}", text, re.S)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manuscript", type=Path, required=True)
    ap.add_argument("--bib", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--replay", action="store_true")
    args = ap.parse_args()

    gate = _load("delivery_gate", RAILS / "delivery_gate.py")
    eb = _load("evidence_bind", EVIDENCE_BIND)
    files = eb.manuscript_files(args.manuscript)
    prose = "\n".join(f.read_text(errors="replace") for f in files)
    bib = parse_bib(args.bib.read_text(errors="replace"))

    cache_path = args.out.with_name(args.out.stem + "_registry.json")
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if args.replay:
        def resolve(identifier: str):
            if identifier not in cache:
                raise SystemExit(f"{identifier} is not in {cache_path.name}")
            return cache[identifier]
    else:
        resolve = gate.doi_registry_resolver(cache=cache)

    cited = list(dict.fromkeys(gate._cite_keys(prose)))
    findings = gate.citation_keys_resolve(prose, bib) + gate.references_resolve(prose, bib, resolve)
    identifiers = {k: gate.reference_identifier(bib[k]) for k in cited if k in bib}
    registered = sorted(k for k, i in identifiers.items() if i and cache.get(i) is True)
    report = {
        "files": [f.name for f in files],
        "cited_keys": len(cited),
        "with_identifier": sum(1 for i in identifiers.values() if i),
        "registered": len(registered),
        "findings": [{"check": f.check, "detail": f.detail} for f in findings],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if not args.replay:
        cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    print(f"cited references: {report['cited_keys']}, with DOI or arXiv id: "
          f"{report['with_identifier']}, registered: {report['registered']}")
    for f in findings:
        print(f"  {f.render()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
