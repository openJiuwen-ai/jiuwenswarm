"""Independent reviewer panel: score a generated paper with several *different* models.

The pipeline's own judge (``tree_provider/judge.py``) uses the same model that wrote the paper.
For choosing which version to send to the Stanford Agentic Reviewer (limited daily quota, ~10 h per
score) we want reviewers that did not write it. Each panel member gets the full LaTeX source and
returns a review in the Agentic Reviewer's layout: six sections, seven -1/0/+1 dimension scores
(Claims_Support, Experimental_Soundness, Writing_Clarity, Prior_Work_Context, Question_Importance,
Originality, Value_to_Community) and an overall 1-10 score. The panel reports per-member results,
the mean, and the weaknesses every member raised — the to-do list for a revision round.

Members are OpenAI-compatible endpoints; credentials come from KEY=VALUE env files
(``API_KEY`` / ``API_BASE``) and are never written to the output.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jiuwenswarm.agents.harness.common.paper_pipeline.experiment_model import read_env

DIMENSIONS = (
    "Claims_Support", "Experimental_Soundness", "Writing_Clarity", "Prior_Work_Context",
    "Question_Importance", "Originality", "Value_to_Community",
)
MAX_PAPER_CHARS = 180_000

REVIEW_PROMPT = """You are an expert reviewer for ICLR. Review the paper below (LaTeX source) as you
would for the main conference: rigorous, specific, and calibrated (the average ICLR submission scores
about 4/10, the average accepted paper about 5.4/10). Judge the evidence actually reported, not the
intentions. Quote or point to concrete passages, tables or numbers for every weakness.

Answer in exactly this layout:

1. Summary
2. Strengths
3. Weaknesses
4. Detailed Comments
5. Questions for Authors
6. Overall Assessment

TRIPLE_SCORES:
- Claims_Support: <-1|0|+1>
- Experimental_Soundness: <-1|0|+1>
- Writing_Clarity: <-1|0|+1>
- Prior_Work_Context: <-1|0|+1>
- Question_Importance: <-1|0|+1>
- Originality: <-1|0|+1>
- Value_to_Community: <-1|0|+1>
OVERALL_SCORE: <number from 1 to 10, one decimal>
TOP_WEAKNESSES:
- <the 3-5 most score-relevant problems, one line each, most important first>

===== PAPER =====
"""


@dataclass
class Member:
    model: str
    api_base: str
    api_key: str = field(repr=False)
    label: str = ""


@dataclass
class Review:
    member: str
    model: str
    ok: bool
    overall: float | None = None
    scores: dict[str, int] = field(default_factory=dict)
    top_weaknesses: list[str] = field(default_factory=list)
    text: str = ""
    error: str = ""
    seconds: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def members_from_spec(spec: str, env_dir: Path) -> list[Member]:
    """``spec``: comma-separated ``<env name>:<model>`` items, e.g. ``bailian:qwen3.8-max-0902``;
    the env file is ``<env_dir>/<env name>.env``.
    """
    members = []
    for item in [s.strip() for s in spec.split(",") if s.strip()]:
        env_name, _, model = item.partition(":")
        env = read_env(env_dir / f"{env_name}.env")
        members.append(Member(model=model or env["MODEL_NAME"], api_base=env["API_BASE"],
                              api_key=env["API_KEY"], label=item))
    return members


def flatten_tex(paper_dir: Path, main: str = "main.tex") -> str:
    """main.tex with \\input/\\include expanded one level deep (sections/*.tex), preamble dropped."""
    return prepare_paper(paper_dir, main)[0]


_SECTION = re.compile(r"(?=\\(?:section|appendix)\*?[{\s])")


def prepare_paper(paper_dir: Path, main: str = "main.tex", max_chars: int = MAX_PAPER_CHARS) -> tuple[str, dict]:
    """The paper text for reviewers plus a coverage record.

    Never cut silently: over ``max_chars`` whole sections are kept in order until the limit and the
    rest are listed as omitted (the bibliography is the last to go); the record says what was sent.
    """
    text = (paper_dir / main).read_text(encoding="utf-8", errors="replace")

    def expand(match: re.Match) -> str:
        name = match.group(1)
        path = paper_dir / (name if name.endswith(".tex") else f"{name}.tex")
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else match.group(0)

    text = re.sub(r"\\(?:input|include)\{([^}]+)\}", expand, text)
    start = text.find("\\begin{document}")
    if start >= 0:
        text = text[start:]
    parts = [p for p in _SECTION.split(text) if p]
    bbl = paper_dir / Path(main).with_suffix(".bbl").name
    if bbl.is_file():  # reviewers judge related work by the references actually listed
        parts.append("\n\n% ===== bibliography (.bbl) =====\n" + bbl.read_text(encoding="utf-8", errors="replace"))
    total = sum(len(p) for p in parts)
    kept, omitted, size = [], [], 0
    for part in parts:
        if size + len(part) <= max_chars:
            kept.append(part)
            size += len(part)
        else:
            omitted.append(_part_title(part))
    body = "".join(kept)
    if omitted:
        body += "\n\n% [HOST NOTE: omitted for length, not reviewed: " + "; ".join(omitted) + "]\n"
    coverage = {"chars_total": total, "chars_sent": size, "truncated": bool(omitted),
                "sections_sent": [_part_title(p) for p in kept], "sections_omitted": omitted}
    return body, coverage


def _part_title(part: str) -> str:
    match = re.match(r"\s*\\(section|appendix)\*?\s*(?:\{([^}]*)\})?", part)
    if match:
        return match.group(2) or match.group(1)
    return "bibliography" if "bibliography (.bbl)" in part[:80] else "front matter"


# --------------------------------------------------------------------------- host evidence
_PAIR_FIELDS = ("metric", "role", "a", "b", "n_paired", "mean_diff", "ci_low", "ci_high", "p_value", "p_holm",
                "family", "verdict", "test")


def evidence_pack(results_dir: Path, max_chars: int = 40_000) -> tuple[str, dict]:
    """Compact, structured host evidence for reviewers: statistics (pairs with role, Holm p and
    verdict; conditions; stopped analyses), the audit verdict and findings, and frozen-cell provenance.
    Returns (text, coverage) — what was included and what was left out for length.
    """
    results_dir = Path(results_dir)
    sections: list[tuple[str, object]] = []
    stats_path, audit_path = results_dir / "statistics.json", results_dir / "audit.json"
    if stats_path.is_file():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        sections.append(("statistics.method", stats.get("method")))
        sections.append(("statistics.pairs", [{k: p.get(k) for k in _PAIR_FIELDS} for p in stats.get("pairs", [])]))
        sections.append(("statistics.errors", stats.get("errors", [])))
        sections.append(("statistics.conditions", {
            n: {k: c.get(k) for k in ("setting", "method", "tier", "budget", "model", "problems")}
            for n, c in (stats.get("conditions") or {}).items()}))
    if audit_path.is_file():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        sections.append(("audit", {"verdict": audit.get("verdict"), "blocking": audit.get("blocking"),
                                   "findings": [{k: f.get(k) for k in ("level", "code", "detail", "waived_by")}
                                                for f in audit.get("findings", [])]}))
    included, omitted, chunks, size = [], [], [], 0
    for name, value in sections:
        chunk = f"## {name}\n{json.dumps(value, ensure_ascii=False, default=str)}\n"
        if size + len(chunk) <= max_chars:
            chunks.append(chunk)
            included.append(name)
            size += len(chunk)
        else:
            omitted.append(name)
    if not sections:
        omitted.append(f"no statistics.json / audit.json in {results_dir}")
    return "".join(chunks), {"results_dir": str(results_dir), "included": included, "omitted": omitted,
                             "chars": size}


EVIDENCE_NOTE = """

===== HOST EVIDENCE (structured, computed by the host from the per-item results) =====
Check the paper's claims against this: does each comparative claim use a pair that exists below, the
right role (confirmatory vs exploratory), the Holm-adjusted p, and the setting / model it names? Treat
audit errors and stopped analyses as threats to validity.
"""

EVIDENCE_CHECK_PROMPT = """You are the evidence checker of a review panel. You do not score the paper.
For every empirical claim in the paper (abstract, introduction, results, conclusion), find the host
evidence that supports it and check method, setting, model, metric, numbers and statistical status.
Statuses: supported (matches the evidence), overstated (evidence exists but the claim is stronger,
e.g. raw p instead of Holm, exploratory presented as confirmatory, bound presented as equivalence),
contradicted (evidence says otherwise), no_source (no matching evidence).

Answer with one line `CLAIMS_JSON:` followed by a JSON list, nothing after it:
CLAIMS_JSON: [{"claim": "...", "location": "section or label", "method": "...", "setting": "...",
"metric": "...", "source": "statistics pair a-b / variant / none",
"status": "supported|overstated|contradicted|no_source", "note": "..."}]

===== PAPER =====
"""


def parse_claims(text: str) -> list[dict] | None:
    tail = text.split("CLAIMS_JSON:", 1)
    if len(tail) != 2:
        return None
    start, end = tail[1].find("["), tail[1].rfind("]")
    try:
        claims = json.loads(tail[1][start:end + 1]) if start >= 0 and end > start else None
    except ValueError:
        return None
    return [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else None


def check_evidence(member: Member, paper: str, pack: str, *, timeout: float = 900.0) -> dict:
    """One evidence-checker call (costs one model call; only run when requested)."""
    from openai import OpenAI

    started = time.time()
    try:
        client = OpenAI(api_key=member.api_key, base_url=member.api_base, timeout=timeout, max_retries=2)
        response = client.chat.completions.create(
            model=member.model, temperature=0,
            messages=[{"role": "user", "content": EVIDENCE_CHECK_PROMPT + paper + EVIDENCE_NOTE + pack}])
        text = response.choices[0].message.content or ""
        claims = parse_claims(text)
        usage = getattr(response, "usage", None)
        result = {"member": member.label, "model": member.model, "ok": claims is not None, "claims": claims or [],
                  "text": text, "seconds": round(time.time() - started, 1),
                  "prompt_tokens": getattr(usage, "prompt_tokens", None),
                  "completion_tokens": getattr(usage, "completion_tokens", None),
                  "error": "" if claims is not None else "could not parse CLAIMS_JSON"}
    except Exception as exc:  # noqa: BLE001
        return {"member": member.label, "model": member.model, "ok": False, "claims": [], "error": repr(exc)[:500],
                "seconds": round(time.time() - started, 1), "prompt_tokens": None, "completion_tokens": None}
    result["status_counts"] = {s: sum(1 for c in result["claims"] if c.get("status") == s)
                               for s in ("supported", "overstated", "contradicted", "no_source")}
    return result


# --------------------------------------------------------------------------- layout (no model)
_OVERFULL = re.compile(r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\)")


def layout_check(paper_dir: Path, main: str = "main") -> dict:
    """What a text-only review cannot see, from the LaTeX log: overfull boxes and oversized floats.

    This is not a visual review: figure legibility and rendering still need someone to look at the PDF.
    """
    log = Path(paper_dir) / f"{main}.log"
    if not log.is_file():
        return {"checked": False, "note": f"no {log.name}; layout not checked"}
    text = log.read_text(encoding="utf-8", errors="replace")
    widths = [float(w) for w in _OVERFULL.findall(text)]
    return {"checked": True, "overfull_over_10pt": sum(1 for w in widths if w > 10),
            "max_overfull_pt": max(widths, default=0.0),
            "float_too_large": text.count("Float too large"),
            "note": "log-based layout check only; figures were not inspected visually"}


_SCORE_RE = re.compile(r"^\s*-?\s*(" + "|".join(DIMENSIONS) + r")\s*:\s*([+-]?\d)", re.MULTILINE)
_OVERALL_RE = re.compile(r"OVERALL_SCORE\s*:\s*(\d+(?:\.\d+)?)")


def parse_review(text: str) -> tuple[dict[str, int], float | None, list[str]]:
    scores = {name: max(-1, min(1, int(value))) for name, value in _SCORE_RE.findall(text)}
    overall_match = _OVERALL_RE.search(text)
    overall = float(overall_match.group(1)) if overall_match else None
    weaknesses: list[str] = []
    tail = text.split("TOP_WEAKNESSES:", 1)
    if len(tail) == 2:
        weaknesses = [line.strip()[1:].strip() for line in tail[1].splitlines() if line.strip().startswith("-")]
    return scores, overall, weaknesses[:5]


def review_one(member: Member, paper: str, *, timeout: float = 600.0, evidence: str = "") -> Review:
    from openai import OpenAI

    started = time.time()
    try:
        client = OpenAI(api_key=member.api_key, base_url=member.api_base, timeout=timeout, max_retries=2)
        response = client.chat.completions.create(
            model=member.model,
            messages=[{"role": "user",
                       "content": REVIEW_PROMPT + paper + (EVIDENCE_NOTE + evidence if evidence else "")}],
            temperature=0,
        )
        text = response.choices[0].message.content or ""
        scores, overall, weaknesses = parse_review(text)
        usage = getattr(response, "usage", None)
        return Review(member=member.label, model=member.model, ok=overall is not None and len(scores) == 7,
                      overall=overall, scores=scores, top_weaknesses=weaknesses, text=text,
                      seconds=round(time.time() - started, 1),
                      prompt_tokens=getattr(usage, "prompt_tokens", None),
                      completion_tokens=getattr(usage, "completion_tokens", None),
                      error="" if overall is not None else "could not parse OVERALL_SCORE / TRIPLE_SCORES")
    except Exception as exc:  # one failing member must not sink the panel
        return Review(member=member.label, model=member.model, ok=False, error=repr(exc)[:500],
                      seconds=round(time.time() - started, 1))


def run_panel(paper_dir: Path, members: list[Member], out_dir: Path | None = None, *,
              evidence_dir: Path | None = None, checker: Member | None = None,
              max_chars: int = MAX_PAPER_CHARS) -> dict:
    """Review ``paper_dir``. ``evidence_dir`` (a results directory) adds the host evidence to every
    member's prompt; ``checker`` adds one evidence-checker call (one extra model call).
    """
    paper, coverage = prepare_paper(paper_dir, max_chars=max_chars)
    pack, pack_coverage = evidence_pack(evidence_dir) if evidence_dir is not None else ("", None)
    with ThreadPoolExecutor(max_workers=len(members) or 1) as pool:
        reviews = list(pool.map(lambda m: review_one(m, paper, evidence=pack), members))
    evidence_check = check_evidence(checker, paper, pack) if checker is not None else None
    good = [r for r in reviews if r.ok]
    summary = {
        "paper_dir": str(paper_dir),
        "paper_chars": len(paper),
        "coverage": coverage,
        "evidence": pack_coverage,
        "evidence_check": evidence_check,
        "layout": layout_check(paper_dir),
        "visual_review": "not performed: members read LaTeX source only; figures and rendering are unreviewed",
        "members": [r.member for r in reviews],
        "ok": len(good),
        "overall_mean": round(statistics.mean(r.overall for r in good), 2) if good else None,
        "overall_each": {r.member: r.overall for r in reviews},
        "dimension_mean": {
            d: round(statistics.mean(r.scores[d] for r in good if d in r.scores), 2)
            for d in DIMENSIONS if any(d in r.scores for r in good)
        },
        "reviews": [asdict(r) for r in reviews],
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "review_panel.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        (out_dir / "review_panel.md").write_text(markdown(summary), encoding="utf-8")
    return summary


def markdown(summary: dict) -> str:
    ok_of = f"{summary['ok']}/{len(summary['members'])}"
    lines = [f"# Reviewer panel: mean overall {summary['overall_mean']} ({ok_of} ok)", ""]
    coverage = summary.get("coverage") or {}
    if coverage.get("truncated"):
        lines += [f"**Partial review**: {coverage['chars_sent']} of {coverage['chars_total']} characters sent; "
                  f"not reviewed: {', '.join(coverage['sections_omitted'])}.", ""]
    if summary.get("evidence"):
        omitted = summary["evidence"]["omitted"]
        lines += [f"Host evidence included: {', '.join(summary['evidence']['included']) or 'none'}"
                  + (f"; omitted: {', '.join(omitted)}" if omitted else ""),
                  ""]
    check = summary.get("evidence_check")
    if check:
        lines += [f"Evidence checker ({check['member']}): " + (
            ", ".join(f"{k} {v}" for k, v in check.get("status_counts", {}).items()) if check["ok"]
            else f"ERROR {check['error']}"), ""]
        for claim in check.get("claims", []):
            if claim.get("status") != "supported":
                lines.append(f"- [{claim.get('status')}] {claim.get('claim')} ({claim.get('location')}): "
                             f"{claim.get('note')}")
        lines.append("")
    if summary.get("visual_review"):
        lines += [f"Visual review: {summary['visual_review']}. Layout (log): {summary.get('layout')}", ""]
    lines += ["| member | overall | " + " | ".join(DIMENSIONS) + " |", "|---|---|" + "---|" * len(DIMENSIONS)]
    for r in summary["reviews"]:
        cells = [str(r["scores"].get(d, "")) for d in DIMENSIONS]
        lines.append(f"| {r['member']} | {r['overall']} | " + " | ".join(cells) + " |")
    lines += ["", "## Top weaknesses", ""]
    for r in summary["reviews"]:
        if r["error"]:
            lines.append(f"- **{r['member']}**: ERROR {r['error']}")
        for w in r["top_weaknesses"]:
            lines.append(f"- **{r['member']}**: {w}")
    return "\n".join(lines) + "\n"
