# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The capability pack's writing chain as JiuwenSwarm tools.

Each tool reads its inputs from files, writes its full output to a file, and
returns a short JSON summary, so an agent can chain stages without carrying a
manuscript through its context. Every model call a tool makes goes through the
``Transport`` it was built with and lands in that run's ``usage.jsonl`` under
the tool's step name.

    transport = Transport(run_dir); transport.install(paperbanana=pb_dir)
    tools = build_tools(transport)   # list[LocalFunction]
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

from .model_transport import Transport

def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _write(path: str, data: Any) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        from research_harness.primitives.types import _to_jsonable
        p.write_text(json.dumps(_to_jsonable(data), indent=1, ensure_ascii=False), encoding="utf-8")
    return str(p)


def export_exemplar_units(pool_db: str, topic_id: int, out_path: str) -> int:
    """Copy a topic's exemplar units (structure only: moves and gists, no source text) to JSON."""
    con = sqlite3.connect(f"file:{pool_db}?mode=ro", uri=True)
    rows = con.execute("SELECT unit_type, source_paper_id, source_title, venue, venue_tier, quality, "
                       "form_json, summary FROM exemplar_units WHERE topic_id=? ORDER BY id", (topic_id,)).fetchall()
    keys = ("unit_type", "source_paper_id", "source_title", "venue", "venue_tier", "quality", "form", "summary")
    units = [dict(zip(keys, r[:6] + (json.loads(r[6] or "{}"),) + r[7:])) for r in rows]
    _write(out_path, units)
    return len(units)


def polish_review(t: Transport, manuscript_path: str, out_path: str, venue: str = "ICLR") -> dict:
    from research_harness.primitives.polish_impls import manuscript_polish
    with t.step("polish/review"):
        out = manuscript_polish(manuscript=_read(manuscript_path), venue=venue, run_consistency=False)
    _write(out_path, out)
    return {"out": out_path, "readiness": out.submission_readiness,
            "high": out.n_high, "medium": out.n_medium, "low": out.n_low}


def polish_unify(t: Transport, manuscript_path: str, out_path: str, venue: str = "ICLR",
                 style_card: str = "") -> dict:
    """Whole-manuscript unification; the rewrite is kept only if its gate passes."""
    from research_harness.primitives.polish_impls import manuscript_unify
    text = _read(manuscript_path)
    with t.step("polish/unify"):
        out = manuscript_unify(manuscript=text, venue=venue, style_card=style_card)
    _write(out_path, out.unified_text if out.status == "unified" else text)
    _write(out_path + ".json", out)
    return {"out": out_path, "status": out.status, "changes": len(out.changes or [])}


def destyle(t: Transport, manuscript_path: str, out_path: str) -> dict:
    from research_harness.primitives.polish_impls import manuscript_destyle
    with t.step("polish/destyle"):
        out = manuscript_destyle(manuscript=_read(manuscript_path))
    _write(out_path, out["text"])
    _write(out_path + ".json", {k: v for k, v in out.items() if k != "text"})
    return {"out": out_path, "applied": len(out["applied"]), "vetoed": len(out["vetoed"]),
            "flags": [out["flags_before"], out["flags_after"]]}


def eag_generate(t: Transport, unit_type: str, content_path: str, units_path: str, out_path: str,
                 target_venue: str = "ICLR 2026", max_rounds: int = 2) -> dict:
    """Exemplar-anchored rewrite of one unit: plan the form from same-venue exemplars, then draft."""
    from research_harness.primitives.exemplar_impls import exemplar_generate
    units = json.loads(_read(units_path))
    with t.step(f"eag/{unit_type}"):
        out = exemplar_generate(unit_type=unit_type, target_content=_read(content_path),
                                retrieved_units=units, exemplar_texts=[u["summary"] for u in units],
                                target_venue=target_venue, backend="self", max_rounds=max_rounds)
    _write(out_path, out.draft)
    _write(out_path + ".json", out)
    return {"out": out_path, "passed": out.passed, "rounds": out.rounds}


def arena_panel(t: Transport, manuscript_path: str, out_path: str, scope: str = "experimental") -> dict:
    """Five blind reviewers, one coverage axis each, over the paper's results surface.

    The panel reads the manuscript through its input view (``discriminator.input_view``);
    the count of normalized terms is kept next to the findings.
    """
    from .discriminator import AXES, input_view, review_axis

    def chat(system: str, user: str) -> str:
        return t.chat(user, system=system, requested_model=t.chat_model, temperature=0.0)

    tex, renamed = input_view(_read(manuscript_path))
    findings = {}
    for axis in AXES:
        with t.step(f"arena/{axis}"):
            findings[axis] = review_axis(tex, axis, chat, scope=scope)
    _write(out_path, {"terms_renamed_for_blind_view": renamed, "findings": findings})
    return {"out": out_path, "terms_renamed": renamed, **{a: len(f) for a, f in findings.items()}}


def figure_suite(t: Transport, spec_path: str, records_path: str, out_dir: str, suite_id: str = "experiment") -> dict:
    """Deterministic data figures: numbers come only from the records, then the integrity check."""
    from research_harness.visualization.experiment_figures import render_figure_suite_spec
    from research_harness.visualization.figure_integrity import check_figure_dir, format_report
    with t.step(f"figures/{suite_id}"):
        suite = render_figure_suite_spec(spec=json.loads(_read(spec_path)), records=json.loads(_read(records_path)),
                                         output_dir=out_dir, suite_id=suite_id)
    report = format_report(check_figure_dir(out_dir))
    _write(str(Path(out_dir) / f"{suite_id}_integrity.txt"), report)
    _write(str(Path(out_dir) / f"{suite_id}_suite.json"), suite)
    return {"out": out_dir, "integrity": report.splitlines()[0]}


def paperbanana_render(t: Transport, spec_path: str, out_png: str) -> dict:
    """Method figure through PaperBanana (retrieve, plan, style, paint, critique, polish)."""
    import pb_generate
    with t.step(f"figures/{Path(out_png).stem}"):
        code = pb_generate.main(["pb_generate", spec_path, out_png])
    return {"out": out_png, "exit_code": code}


_RANK = {"critical": 0, "high": 0, "major": 1, "medium": 1, "minor": 2, "low": 2}


def read_findings(path: str, limit: int = 12) -> list[dict]:
    """The most severe findings in a polish_review or arena_panel output, one short record each."""
    data = json.loads(_read(path))
    if "findings" in data and isinstance(data["findings"], dict):   # arena_panel: findings per axis
        items = [dict(f, source=f"arena/{axis}") for axis, fs in data["findings"].items() for f in fs]
    else:
        items = [dict(f, source=f"polish/{f.get('angle', '')}") for f in data.get("findings", [])]
    items.sort(key=lambda f: _RANK.get(f.get("severity"), 1))
    return [{"source": f["source"], "severity": f.get("severity"), "location": f.get("location", ""),
             "issue": (f.get("issue") or f.get("evidence") or "")[:400],
             "fix": (f.get("fix") or f.get("recommended_check") or "")[:300]} for f in items[:int(limit)]]


_P = {"type": "string"}
_SPECS = [
    (polish_review, "Review a LaTeX manuscript on three angles plus deterministic checks; writes findings JSON.",
     {"manuscript_path": _P, "out_path": _P, "venue": _P}, ["manuscript_path", "out_path"]),
    (polish_unify, "Unify terminology, seams and rhythm across the whole manuscript; a gate rejects any "
     "rewrite that loses citations, numbers or headings.", {"manuscript_path": _P, "out_path": _P, "venue": _P,
                                                             "style_card": _P}, ["manuscript_path", "out_path"]),
    (destyle, "Remove machine-writing fingerprints; every edit must keep the content invariant.",
     {"manuscript_path": _P, "out_path": _P}, ["manuscript_path", "out_path"]),
    (eag_generate, "Rewrite one unit (intro_arc, abstract, method_arc, experiment_arc, discussion_arc) in the "
     "form of same-venue exemplars; content stays this paper's.",
     {"unit_type": _P, "content_path": _P, "units_path": _P, "out_path": _P, "target_venue": _P},
     ["unit_type", "content_path", "units_path", "out_path"]),
    (arena_panel, "Run the five-axis blind review panel over the manuscript; writes findings per axis.",
     {"manuscript_path": _P, "out_path": _P, "scope": _P}, ["manuscript_path", "out_path"]),
    (figure_suite, "Render data figures from a suite spec and result records, then check figure integrity.",
     {"spec_path": _P, "records_path": _P, "out_dir": _P, "suite_id": _P}, ["spec_path", "records_path", "out_dir"]),
    (paperbanana_render, "Draw a method figure from a PaperBanana spec. The figure carries structure only, no numbers.",
     {"spec_path": _P, "out_png": _P}, ["spec_path", "out_png"]),
]


def build_tools(t: Transport) -> list[LocalFunction]:
    def bind(fn):
        return lambda **kw: json.dumps(fn(t, **kw), ensure_ascii=False)

    def card(name, desc, props, req):
        return ToolCard(name=name, description=desc,
                        input_params={"type": "object", "properties": props, "required": req})

    return [LocalFunction(card=card(fn.__name__, desc, props, req), func=bind(fn))
            for fn, desc, props, req in _SPECS] + [
        LocalFunction(card=card("read_findings", "Read the most severe findings (source, severity, location, "
                                "issue, fix) from a polish_review or arena_panel output file.",
                                {"path": _P, "limit": {"type": "integer"}}, ["path"]),
                      func=lambda **kw: json.dumps(read_findings(**kw), ensure_ascii=False))]
