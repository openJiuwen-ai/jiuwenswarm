"""Keep the host-rendered results table readable.

agent-core's reporting module renders one column per numeric metric of every variant
(``reporting/agent.py`` -> ``latex.render_results_table``). Real experiments report dozens of
scalars — and the rigor statistics add ``<metric>_ci95_*`` / ``<metric>_diff_vs_<variant>`` keys —
so the table grows to 30-200 columns, which the writing agent then shrinks to an unreadable font
(bl4: 200 columns at 3pt; bl1's official review already complained about "host-rendered" tables).

Two fixes, both deterministic and host-side:

* ``install_compact_results_table``: future runs render only the metrics that carry intervals,
  as ``mean [low, high]`` cells, pivoted to method x budget-tier when variant names carry a tier
  suffix (``proposed_T1``);
* ``compact_paper_tables``: post-process an already written paper — any table whose tabular has
  more than ``MAX_COLUMNS`` columns is replaced by the compact table built from the paper's own
  ``results.json``; the PDF is rebuilt on a copy and adopted only when it compiles cleanly.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jiuwenswarm.agents.harness.common.paper_pipeline.rigor_stats import split_condition

MAX_COLUMNS = 12
_TABLE_RE = re.compile(r"\\begin\{table\*?\}.*?\\end\{table\*?\}", re.S)
_TABULAR_SPEC_RE = re.compile(r"\\begin\{tabular\}\{([^}]*)\}")


def _esc(text: str) -> str:
    return text.replace("\\", r"\textbackslash{}").replace("_", r"\_").replace("&", r"\&").replace("%", r"\%")


def _fmt(value: float) -> str:
    return f"{value:.3f}" if abs(value) < 100 else f"{value:.0f}"


def interval_metrics(variants: dict[str, dict]) -> list[str]:
    """Metrics that carry host intervals, in first-seen order (the primary metric comes first)."""
    seen: list[str] = []
    for metrics in variants.values():
        for key in metrics:
            if key.endswith("_ci95_low") and "_diff_vs_" not in key:
                name = key[: -len("_ci95_low")]
                if name in metrics and name not in seen:
                    seen.append(name)
    return seen


def cell(metrics: dict, name: str) -> str:
    value = metrics.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "--"
    low, high = metrics.get(f"{name}_ci95_low"), metrics.get(f"{name}_ci95_high")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return f"{_fmt(value)} [{_fmt(low)}, {_fmt(high)}]"
    return _fmt(value)


def compact_table(variants: dict[str, dict], *, n_items: int | None = None) -> str:
    """LaTeX table: rows = method, columns = tier (pivot) for the primary metric, or rows = variant,
    columns = up to three interval metrics when nothing is tiered.
    """
    metrics = interval_metrics(variants)
    if not metrics:
        return ""
    primary = metrics[0]
    parsed = {name: split_condition(name) for name in variants}
    tiers = sorted({t for _, t in parsed.values() if t})
    n_note = f" over {n_items} items" if n_items else ""
    if tiers:
        methods = list(dict.fromkeys(m for m, _ in parsed.values()))
        has_untiered = any(not t for _, t in parsed.values())
        # an untiered variant is either a pooled run of a tiered method or an unbudgeted reference
        columns = [t.upper() for t in tiers] + (["pooled / unbudgeted"] if has_untiered else [])
        lines = [r"\begin{table}[h]", r"\centering", r"\small",
                 f"\\begin{{tabular}}{{l{'c' * len(columns)}}}", r"\toprule",
                 " & ".join(["Method", *columns]) + r" \\", r"\midrule"]
        for method in methods:
            row = [_esc(method)]
            for tier in tiers + ([""] if has_untiered else []):
                name = next((n for n, (m, t) in parsed.items() if m == method and t == tier), None)
                row.append(cell(variants[name], primary) if name else "--")
            lines.append(" & ".join(row) + r" \\")
        caption = (f"{_esc(primary)} per method and budget tier (mean{n_note} with 95\\% paired-bootstrap "
                   "confidence interval). Every per-variant metric is in the released results.json.")
    else:
        shown = metrics[:3]
        lines = [r"\begin{table}[h]", r"\centering", r"\small",
                 f"\\begin{{tabular}}{{l{'c' * len(shown)}}}", r"\toprule",
                 " & ".join(["Variant", *map(_esc, shown)]) + r" \\", r"\midrule"]
        for name, values in variants.items():
            lines.append(" & ".join([_esc(name), *(cell(values, m) for m in shown)]) + r" \\")
        caption = f"Results by variant (mean{n_note} with 95\\% bootstrap confidence interval)."
    lines += [r"\bottomrule", r"\end{tabular}", f"\\caption{{{caption}}}", r"\end{table}"]
    return "\n".join(lines)


def _columns(table: str) -> int:
    match = _TABULAR_SPEC_RE.search(table)
    return len(re.findall(r"[lcrpXmb]", match.group(1))) if match else 0


def replace_wide_tables(tex: str, replacement: str) -> tuple[str, int]:
    count = 0

    def swap(match: re.Match) -> str:
        nonlocal count
        block = match.group(0)
        if _columns(block) <= MAX_COLUMNS:
            return block
        count += 1
        label = re.search(r"\\label\{[^}]+\}", block)
        keep_label = f"{label.group(0)}\n" if label else ""
        return replacement.replace(r"\end{table}", f"{keep_label}\\end{{table}}")

    return _TABLE_RE.sub(swap, tex), count


@dataclass
class CompactResult:
    replaced: int
    rebuilt: bool
    note: str = ""


def _variants_from_results(path: Path) -> tuple[dict[str, dict], int | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    variants = {v["name"]: v.get("metrics") or {} for v in payload.get("variants", [])
                if v.get("process_status", "completed") == "completed"}
    sizes = [m.get("n_questions") for m in variants.values() if isinstance(m.get("n_questions"), int)]
    return variants, (max(sizes) if sizes else None)


def compact_paper_tables(paper_dir: str | Path, main: str = "main", timeout: int = 300) -> CompactResult:
    paper_dir = Path(paper_dir)
    results = paper_dir / "results.json"
    if not results.is_file():
        return CompactResult(0, False, "no results.json")
    variants, n_items = _variants_from_results(results)
    table = compact_table(variants, n_items=n_items)
    if not table:
        return CompactResult(0, False, "no interval metrics; left as is")
    targets = [paper_dir / f"{main}.tex", *sorted((paper_dir / "sections").glob("*.tex"))]
    new_texts, replaced = {}, 0
    for path in targets:
        if path.is_file():
            text, count = replace_wide_tables(path.read_text(encoding="utf-8"), table)
            if count:
                new_texts[path.name if path.parent == paper_dir else f"sections/{path.name}"] = text
                replaced += count
    if not replaced:
        return CompactResult(0, False, "no table wider than the limit")

    pdflatex, bibtex = shutil.which("pdflatex"), shutil.which("bibtex")
    if not pdflatex or not bibtex:
        return CompactResult(replaced, False, "pdflatex/bibtex not on PATH; nothing changed")
    work = paper_dir.parent / f".{paper_dir.name}.compact"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(paper_dir, work)
    try:
        for rel, text in new_texts.items():
            (work / rel).write_text(text, encoding="utf-8")
        latex = [pdflatex, "-interaction=nonstopmode", main]

        def run(cmd: list[str]) -> int:
            return subprocess.run(cmd, cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=timeout, check=False).returncode

        codes = [run(latex), run([bibtex, main]), run(latex), run(latex)]
        from jiuwenswarm.agents.harness.common.paper_pipeline.latex_check import scan_log

        if (work / f"{main}.pdf").is_file() and scan_log(work / f"{main}.log").ok:
            backup = paper_dir / "pre_compact"
            backup.mkdir(exist_ok=True)
            for rel in [*new_texts, f"{main}.pdf"]:
                src = paper_dir / rel
                if src.is_file():
                    (backup / Path(rel).name).write_bytes(src.read_bytes())
            for rel in [*new_texts, f"{main}.pdf", f"{main}.log", f"{main}.aux", f"{main}.bbl"]:
                if (work / rel).is_file():
                    shutil.copy2(work / rel, paper_dir / rel)
            return CompactResult(replaced, True, f"exit codes {codes}; originals in pre_compact/")
        return CompactResult(replaced, False, f"rebuild not clean (exit codes {codes}); original kept")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install_compact_results_table() -> None:
    """Future runs: the host renders the compact table instead of one column per scalar."""
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting import agent as reporting

    current = reporting.render_results_table
    if hasattr(current, "__wrapped__"):
        return

    def render_results_table(rows, metric_order):
        original = current(rows, metric_order)
        variants = {}
        for name, cells in rows:
            plain = {k.replace(r"\_", "_"): v for k, v in cells.items()}
            parsed = {}
            for key, value in plain.items():
                try:
                    parsed[key] = float(value)
                except (TypeError, ValueError):
                    continue
            variants[name.replace(r"\_", "_")] = parsed
        if len(metric_order) <= MAX_COLUMNS:
            return original
        return compact_table(variants) or original

    render_results_table.__wrapped__ = current  # type: ignore[attr-defined]
    reporting.render_results_table = render_results_table
