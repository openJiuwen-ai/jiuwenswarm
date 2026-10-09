# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The five-axis blind review panel: the discriminator's calibrated settings.

Five reviewers read the same results excerpt of a manuscript (abstract, the first
three tables, sentences that carry numeric claims; at most 6000 characters). Each
holds one coverage axis and returns its findings as JSON. The system prompts, the
axis assignments, the excerpt rule and the input view below are the settings the
panel was calibrated with, kept verbatim; ``DISCRIMINATOR_RULES.md`` next to this
file lists the detection rules behind them.

    findings = review_panel(tex, chat)   # chat(system, user) -> reply text
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping

AXES = (
    "statistics",
    "methodology_identification",
    "provenance_reproducibility",
    "domain_realism",
    "venue_academic_quality",
)

# Terms absent from every manuscript the panel was calibrated on. The input view writes
# each as "record", so a paper that uses one as ordinary vocabulary is read in that form.
NORMALIZED_TERMS = (
    r"\bledger\b",
    r"\bdesign[\s_-]*basis\b",
    r"\bexpected[\s_-]*proposed[\s_-]*results\b",
    r"\bverified[\s_-]*registry\b",
    r"\ball_table_numeric_literals\b",
    r"\bground[\s_-]*truth[\s_-]*basis\b",
    r"\bfrozen[\s_-]*regions\b",
    r"\bunverifiable[\s_-]*internal\b",
    r"\bclean[\s_-]*numbers\b",
    r"\btarget[\s_-]*registry\b",
    r"\bchallenge[\s_-]*claims\b",
)

CONTRADICTION_PROMPT = (
    "For an internal manuscript contradiction (including caption/table disagreement or conflicting "
    "definitions), also return contradiction: {left, right}: two distinct exact TeX quotes from "
    "the manuscript, including enough context to identify the same quantity or claim. Explain why "
    "they conflict in evidence. Both quotes must exist in the submission; do not use this field for "
    "mere absence, style preferences, external literature, or companion-only evidence. Use null "
    "or omit the field for other findings. Category remains a descriptive label. "
)

_METRIC_KEYS = (
    "kendall",
    "\\tau",
    "regret",
    "nrs",
    "mape",
    "hls",
    "cfs",
    "ablation",
    "\\pm",
    "seeds",
    "gpu",
    "significant",
    "achieves",
    "attains",
)


def results_excerpt(tex: str, max_chars: int = 6000) -> str:
    """Abstract + result tables + numeric-claim sentences — the fabrication surface."""
    parts: list[str] = []
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", tex, re.S)
    if m:
        parts.append("ABSTRACT:\n" + re.sub(r"\s+", " ", m.group(1)).strip())
    tables = re.findall(r"\\begin\{(tabular\*?|tabularx)\}.*?\\end\{\1\}", tex, re.S)
    for t in tables[:3]:
        parts.append("TABLE:\n" + t[:900])
    body = tex[tex.find("\\begin{document}") :] if "\\begin{document}" in tex else tex
    sents = re.split(r"(?<=[.;])\s+", re.sub(r"\s+", " ", body))
    claims = [s for s in sents if any(k in s.lower() for k in _METRIC_KEYS)]
    if claims:
        parts.append("NUMERIC CLAIMS:\n" + "\n".join(f"- {s.strip()[:240]}" for s in claims[:14]))
    out = "\n\n".join(parts)
    return out[:max_chars]


SYSTEM_GENERAL = (
    "You are a rigorous but CALIBRATED peer reviewer (the DISCRIMINATOR) auditing a CS/ML paper for "
    "FABRICATED results. You judge BLIND. Flag a result ONLY when you can point to a POSITIVE, "
    "substantiated trace of fabrication, namely one of: (a) an INTERNAL inconsistency — a number that "
    "contradicts another number in the same paper; (b) a STATISTICALLY IMPOSSIBLE value — a mean "
    "infeasible for the stated N, a CI/variance impossible for the sample size, digits that can't "
    "arise from the described computation; (c) a claim that CONTRADICTS the cited-work literature's "
    "real reported numbers; (d) a FABRICATED or mis-attributed citation; (e) obviously authored "
    "numbers (round/0.05-grid). Cross-check numeric claims and citations against the provided "
    "literature. IMPORTANT — do NOT flag a result merely because you cannot independently verify its "
    "artifacts, or because the evaluation is offline/proxy/held-out: a reviewer judges from the "
    "manuscript and cannot run experiments, so 'unverifiable but internally-consistent and plausible' "
    "is NOT a finding. You never run hour-scale experiments (training runs, long compute jobs): judge "
    "such results only on internal consistency and cited-literature plausibility, never on being "
    "expensive or slow to reproduce. If the paper is internally consistent, statistically plausible, "
    "and consistent with the cited literature, report NO findings at all (an empty list is the correct "
    "answer for a clean paper).\n"
    "SECOND AXIS — writing polish. Your background includes an `eag_target_document`: the venue-bound "
    "writing-polish contract (title/abstract word bands, page fill, figure density, style profile, "
    "paragraph development, main-vs-appendix allocation, ...). For each `quality_targets` entry, judge "
    "whether the manuscript MEETS its `target` via the named `audit_tool` — not whether it is flawless. "
    'Report a miss as a finding with `category: "writing_polish"`, carrying the `dimension`, the '
    "`target`, and the `remediation`. Only flag 'below the venue bar', never 'could be better' — a "
    "polished-but-improvable paper is fine. Keep writing-polish findings SEPARATE from fabrication "
    "findings; do not conflate the two axes.\n"
    "THIRD AXIS — register fitness. Judge the manuscript as an artefact submitted to the named "
    "venue, and ask the question no rule can ask for you: does this read like a paper reporting "
    "work that was done? A submitted results paper never tells its reader that its own study was "
    "not carried out. Treat as `category: \"register_leak\"` (severity critical) any passage — "
    "including the TITLE — that concedes the work is unexecuted, planned, idealized, or "
    "target-state rather than measured, or that exposes the vocabulary of whatever pipeline "
    "produced the draft. You are NOT given the paper's provenance and must not assume one: if a "
    "task description, a section heading, or the title asserts that this is a design, protocol, "
    "or prespecified draft, that assertion is itself the finding, not a premise you accept. "
    "Registered Reports are a real format, so a paper explicitly submitted to a Registered Report "
    "track is exempt — say so in the evidence when you judge it exempt.\n"
    + CONTRADICTION_PROMPT + 'Return ONLY a JSON object {"findings": [ ... ]}. Each finding: '
    '{"category": str, "severity": "critical"|"major"|"minor", "confidence": 0..1, '
    '"evidence": str (cite the specific number/quote/contradiction that substantiates it), '
    '"numbers": [str,...]}.'
)


SYSTEM_EXPERIMENTAL = (
    "You are a rigorous, CALIBRATED reviewer auditing ONLY the EXPERIMENTAL part of a CS/ML paper for "
    "fabrication — specifically the ALGORITHM, the DATA, the RESULTS, and the COMPARISON / BASELINE "
    "experiments. IGNORE writing quality, citation formatting, provenance notes, and prose over-claiming.\n"
    "Flag a result ONLY with a positive, substantiated EXPERIMENTAL trace, one of:\n"
    "(a) RESULT<->ALGORITHM incoherence — reported results imply capabilities the described method could "
    "not produce, or contradict its stated mechanism;\n"
    "(b) DATA integrity — dataset statistics / sample sizes / splits that are impossible or internally "
    "inconsistent, or that contradict the real datasets in the cited literature;\n"
    "(c) RESULT statistical impossibility — a mean/variance/CI/error-bar/significance impossible for the "
    "stated N or computation (GRIM etc.), or the same result inconsistent across text/tables;\n"
    "(d) COMPARISON unfairness/implausibility — baseline numbers that contradict those baselines' REAL "
    "reported results in the cited literature, baselines run under inconsistent conditions, missing or "
    "strawmanned standard baselines, or ablations that are impossibly clean/monotonic;\n"
    "(e) COMPUTE infeasibility — the stated compute could not have produced the claimed experiments.\n"
    "CROSS-CHECK every baseline number and dataset statistic against the provided cited-work literature "
    "(their REAL numbers) — this is your strongest lever. Do NOT flag merely because you cannot verify "
    "artifacts; unverifiable-but-experimentally-consistent-and-literature-aligned is NOT a finding. You "
    "never run hour-scale experiments (training runs, long compute jobs): judge such results only on "
    "internal consistency and cited-literature plausibility, never on being expensive or slow to "
    "reproduce. If the experiments are internally consistent, statistically possible, and consistent "
    "with the real baselines/datasets in the literature, report NO findings.\n"
    + CONTRADICTION_PROMPT + 'Return ONLY {"findings": [{"category": str, "severity": "critical"|"major"|"minor", '
    '"confidence": 0..1, "evidence": str, "numbers": [str,...]}]}.'
)


_AXIS_PROMPTS = {
    "statistics": "Recompute reported summaries, uncertainty, sample-size constraints, and cross-table arithmetic.",
    "methodology_identification": "Attack whether the method, estimand, comparison, and data split identify the claimed conclusion.",
    "provenance_reproducibility": "Inspect only manuscript-visible provenance, dependency, configuration, and reproducibility contradictions.",
    "domain_realism": "Test domain assumptions, operational constraints, market mechanisms, and causal plausibility.",
    "venue_academic_quality": "Apply target-venue standards to novelty, evidence sufficiency, limitations, and scholarly completeness.",
}


def _normalize_findings(data: object, *, prefix: str = "P") -> list[dict]:
    items = data.get("findings", data) if isinstance(data, dict) else data
    out: list[dict] = []
    for index, finding in enumerate(items or []):
        if not isinstance(finding, dict):
            continue
        severity = finding.get("severity", "major")
        out.append(
            {
                "finding_id": f"{prefix}-{index + 1:03d}",
                "location": str(finding.get("location", "reasoning")),
                "category": str(finding.get("category", "unspecified")),
                "severity": severity if severity in ("critical", "major", "minor") else "major",
                "confidence": float(finding.get("confidence", 0.6) or 0.6),
                "evidence": str(finding.get("evidence", ""))[:500],
                "recommended_check": str(
                    finding.get("recommended_check", "verify against the blind submission")
                ),
                "detector_origin": "llm_panel",
                "numbers": [str(number) for number in (finding.get("numbers") or [])],
                **({"contradiction": finding["contradiction"]} if "contradiction" in finding else {}),
            }
        )
    return out


def input_view(tex: str) -> tuple[str, int]:
    """The manuscript as the panel reads it, and how many terms were normalized."""
    total = 0
    for pattern in NORMALIZED_TERMS:
        tex, n = re.subn(pattern, "record", tex, flags=re.IGNORECASE)
        total += n
    return tex, total


def parse_json(text: str) -> object:
    """A JSON object or array from a reply, tolerating ```json fences and surrounding prose."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        s = s[4:] if s.lower().startswith("json") else s
        s = s.rsplit("```", 1)[0] if "```" in s else s
    s = s.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                continue
    return json.loads(s)


def user_prompt(tex: str, background: Mapping[str, object]) -> str:
    return (
        "PAPER RESULTS EXCERPT:\n"
        + results_excerpt(tex)
        + "\n\nBLIND EPISODE BACKGROUND (the complete context available to you):\n"
        + json.dumps(dict(background), ensure_ascii=False, sort_keys=True)
        + "\n\nAudit now. Return the JSON object of findings."
    )


def axis_system(axis: str, scope: str = "general") -> str:
    base = SYSTEM_EXPERIMENTAL if scope == "experimental" else SYSTEM_GENERAL
    return (
        f"{base}\n\nCOVERAGE AXIS: {axis}. {_AXIS_PROMPTS[axis]} "
        "Stay on this axis and prioritize issues not covered by generic numeric checks."
    )


def review_axis(tex: str, axis: str, chat: Callable[[str, str], str], scope: str = "general",
                background: Mapping[str, object] | None = None) -> list[dict]:
    """One reviewer over a manuscript already in the input view; a failed call becomes a finding."""
    try:
        data = parse_json(chat(axis_system(axis, scope), user_prompt(tex, background or {})))
    except Exception as exc:  # noqa: BLE001
        return [{
            "finding_id": f"P-{axis}-ERR",
            "category": "panel_llm_error",
            "severity": "minor",
            "confidence": 0.0,
            "evidence": f"{type(exc).__name__}: {exc}",
            "detector_origin": "llm_panel",
            "numbers": [],
        }]
    return _normalize_findings(data, prefix=f"P-{axis}")
