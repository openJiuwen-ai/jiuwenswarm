"""Re-typeset the pipeline's paper with the official ICLR template.

agent-core's reporting stage only bundles ``neurips_2025.sty`` (ts-latex skill assets), while the
BDCI track and Stanford Agentic Reviewer expect ICLR formatting. The generated ``main.tex`` is a thin
skeleton (style package + \\input of sections + bibliography), so switching templates means swapping
the style package and the bibliography style, then rebuilding.

The conversion is done on a copy and adopted only when the ICLR build is clean (PDF produced, no
undefined citations/references); otherwise the original NeurIPS build is left untouched.
Template files: https://github.com/ICLR/Master-Template (iclr2027/).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jiuwenswarm.agents.harness.common.paper_pipeline.latex_check import scan_log

TEMPLATE_DIR = Path(__file__).with_name("templates") / "iclr2027"
STYLE = "iclr2027_conference"
TEMPLATE_FILES = (f"{STYLE}.sty", f"{STYLE}.bst", "fancyhdr.sty", "natbib.sty")

_STYLE_LINE = re.compile(r"^\\usepackage(\[[^\]]*\])?\{neurips_\d+\}\s*$", re.MULTILINE)
_BIBSTYLE = re.compile(r"\\bibliographystyle\{[^}]*\}")


@dataclass
class TemplateResult:
    converted: bool
    note: str


def rewrite_main_tex(text: str) -> str | None:
    """Return the ICLR version of ``main.tex``, or None if it does not use a NeurIPS style line."""
    if not _STYLE_LINE.search(text):
        return None
    # Callables: the returned text is inserted verbatim (no backslash-escape processing).
    text = _STYLE_LINE.sub(lambda _m: f"\\usepackage{{{STYLE},times}}", text, count=1)
    if _BIBSTYLE.search(text):
        text = _BIBSTYLE.sub(lambda _m: f"\\bibliographystyle{{{STYLE}}}", text, count=1)
    return text


def _run(cmd: list[str], cwd: Path, timeout: int) -> int:
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=timeout, check=False).returncode


def convert_to_iclr(paper_dir: str | Path, main: str = "main", timeout: int = 300) -> TemplateResult:
    paper_dir = Path(paper_dir)
    tex = paper_dir / f"{main}.tex"
    if not tex.is_file():
        return TemplateResult(False, f"missing {tex.name}")
    original = tex.read_text(encoding="utf-8")
    rewritten = rewrite_main_tex(original)
    if rewritten is None:
        return TemplateResult(False, "main.tex does not load a neurips_* style; left as is")
    if not all((TEMPLATE_DIR / name).is_file() for name in TEMPLATE_FILES):
        return TemplateResult(False, "ICLR template files not installed; left as is")
    pdflatex, bibtex = shutil.which("pdflatex"), shutil.which("bibtex")
    if not pdflatex or not bibtex:
        return TemplateResult(False, "pdflatex/bibtex not on PATH; left as is")

    work = paper_dir.parent / f".{paper_dir.name}.iclr"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(paper_dir, work)
    try:
        for name in TEMPLATE_FILES:
            shutil.copy2(TEMPLATE_DIR / name, work / name)
        (work / f"{main}.tex").write_text(rewritten, encoding="utf-8")
        for stale in (".aux", ".bbl", ".blg", ".out", ".toc", ".fdb_latexmk", ".fls"):
            (work / f"{main}{stale}").unlink(missing_ok=True)
        latex = [pdflatex, "-interaction=nonstopmode", main]
        codes = [_run(latex, work, timeout), _run([bibtex, main], work, timeout),
                 _run(latex, work, timeout), _run(latex, work, timeout)]
        check = scan_log(work / f"{main}.log")
        log_path = work / f"{main}.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        fatal = "Fatal error occurred" in log_text or "Emergency stop" in log_text
        if not (work / f"{main}.pdf").is_file() or fatal or not check.ok:
            return TemplateResult(False, f"ICLR build not clean (exit codes {codes}, undefined="
                                         f"{check.undefined_citations + check.undefined_references}); original kept")
        (paper_dir / f"{main}.neurips.tex").write_text(original, encoding="utf-8")
        if (paper_dir / f"{main}.pdf").is_file():
            shutil.copy2(paper_dir / f"{main}.pdf", paper_dir / f"{main}.neurips.pdf")
        built = [f"{main}{suffix}" for suffix in (".tex", ".pdf", ".log", ".aux", ".bbl", ".blg")]
        for name in (*TEMPLATE_FILES, *built):
            if (work / name).is_file():
                shutil.copy2(work / name, paper_dir / name)
        return TemplateResult(True, f"converted to {STYLE} (exit codes {codes}); "
                                    f"NeurIPS build kept as {main}.neurips.*")
    finally:
        shutil.rmtree(work, ignore_errors=True)
