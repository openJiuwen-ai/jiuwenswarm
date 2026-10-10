"""Detect unresolved citations / references in a compiled paper and rebuild once.

The reporting stage's ts-latex skill can report success while the final ``main.log`` still says
``Citation `x' undefined`` — the PDF then shows "?" in place of every such citation (seen on a
20-reference run). A bibtex pass plus two pdflatex passes is the standard fix.

``final_check`` is the deliverability gate: it always rebuilds from source in a scratch copy and
passes only when every pass exits cleanly, a fresh non-empty PDF is produced, and the final log has
no undefined citation / reference and no LaTeX error. An existing PDF or an older clean log is not
evidence. Without pdflatex the result is ``unverified`` (not deliverable), never ``passed``.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_UNDEFINED = re.compile(r"(Citation|Reference) `([^']+)' on page \d+ undefined")
_LATEX_ERROR = re.compile(r"^! (.+)$", re.MULTILINE)
_BIB_USED = re.compile(r"\\(?:bibliography|addbibresource)\{")


@dataclass
class LatexCheck:
    ok: bool
    undefined_citations: list[str] = field(default_factory=list)
    undefined_references: list[str] = field(default_factory=list)
    rebuilt: bool = False
    note: str = ""


def scan_log(log_path: Path) -> LatexCheck:
    if not log_path.is_file():
        return LatexCheck(ok=False, note=f"missing {log_path.name}")
    text = log_path.read_text(encoding="utf-8", errors="replace")
    cites, refs = set(), set()
    for kind, key in _UNDEFINED.findall(text):
        (cites if kind == "Citation" else refs).add(key)
    return LatexCheck(ok=not cites and not refs, undefined_citations=sorted(cites), undefined_references=sorted(refs))


def _run(cmd: list[str], cwd: Path, timeout: int) -> int:
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=timeout, check=False).returncode


def check_and_rebuild(paper_dir: str | Path, main: str = "main", timeout: int = 300) -> LatexCheck:
    """Scan ``<main>.log``; if anything is undefined, run bibtex + pdflatex x2 on a copy and adopt it
    only when the rebuilt log is clean. The original files are left untouched otherwise.
    """
    paper_dir = Path(paper_dir)
    result = scan_log(paper_dir / f"{main}.log")
    if result.ok or not (paper_dir / f"{main}.tex").is_file():
        return result
    pdflatex, bibtex = shutil.which("pdflatex"), shutil.which("bibtex")
    if not pdflatex or not bibtex:
        result.note = "pdflatex/bibtex not on PATH; not rebuilt"
        return result

    work = paper_dir.parent / f".{paper_dir.name}.rebuild"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(paper_dir, work)
    try:
        latex = [pdflatex, "-interaction=nonstopmode", "-halt-on-error", main]
        codes = [_run(latex, work, timeout), _run([bibtex, main], work, timeout),
                 _run(latex, work, timeout), _run(latex, work, timeout)]
        rebuilt = scan_log(work / f"{main}.log")
        if rebuilt.ok and (work / f"{main}.pdf").is_file():
            for name in (f"{main}.pdf", f"{main}.log", f"{main}.aux", f"{main}.bbl", f"{main}.blg"):
                if (work / name).is_file():
                    shutil.copy2(work / name, paper_dir / name)
            rebuilt.rebuilt = True
            rebuilt.note = f"rebuilt (exit codes {codes}); fixed {len(result.undefined_citations)} citations"
            return rebuilt
        result.note = f"rebuild did not resolve everything (exit codes {codes}); original kept"
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


@dataclass
class FinalCheck:
    status: str  # passed | failed | unverified
    reasons: list[str] = field(default_factory=list)
    exit_codes: dict[str, int] = field(default_factory=dict)
    undefined_citations: list[str] = field(default_factory=list)
    undefined_references: list[str] = field(default_factory=list)
    pdf_sha256: str | None = None
    adopted: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "passed"


def final_check(paper_dir: str | Path, main: str = "main", timeout: int = 300, *, adopt: bool = True) -> FinalCheck:
    """Rebuild ``<main>.tex`` from scratch in a copy and verify the result (see module docstring).

    With ``adopt`` the verified PDF / log / aux / bbl replace the paper's; a failed build never
    touches the paper directory.
    """
    paper_dir = Path(paper_dir)
    tex = paper_dir / f"{main}.tex"
    if not tex.is_file():
        return FinalCheck("failed", [f"missing {tex.name}"])
    pdflatex, bibtex = shutil.which("pdflatex"), shutil.which("bibtex")
    if not pdflatex:
        return FinalCheck("unverified", ["pdflatex not on PATH; the PDF was not rebuilt or checked"])
    uses_bib = bool(_BIB_USED.search(tex.read_text(encoding="utf-8", errors="replace")))
    if uses_bib and not bibtex:
        return FinalCheck("unverified", ["the paper has a bibliography but bibtex is not on PATH"])

    work = paper_dir.parent / f".{paper_dir.name}.final_check"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(paper_dir, work, ignore=shutil.ignore_patterns(
        f"{main}.pdf", f"{main}.aux", f"{main}.log", f"{main}.out"))
    check = FinalCheck("failed")
    try:
        latex = [pdflatex, "-interaction=nonstopmode", "-halt-on-error", main]
        steps = [("pdflatex_1", latex)] + ([("bibtex", [bibtex, main])] if uses_bib else []) + \
                [("pdflatex_2", latex), ("pdflatex_3", latex)]
        for label, cmd in steps:
            try:
                check.exit_codes[label] = _run(cmd, work, timeout)
            except subprocess.TimeoutExpired:
                check.exit_codes[label] = -1
                check.reasons.append(f"{label} timed out after {timeout}s")
                break
        for label, code in check.exit_codes.items():
            # bibtex exits 1 on warnings (e.g. an empty field); 2+ is an error
            if code != 0 and not (label == "bibtex" and code == 1):
                check.reasons.append(f"{label} exited with {code}")
        pdf, log = work / f"{main}.pdf", work / f"{main}.log"
        if not pdf.is_file() or pdf.stat().st_size == 0 or pdf.read_bytes()[:4] != b"%PDF":
            check.reasons.append("no valid PDF was produced")
        if not log.is_file():
            check.reasons.append(f"no {log.name} was produced")
        else:
            text = log.read_text(encoding="utf-8", errors="replace")
            scanned = scan_log(log)
            check.undefined_citations, check.undefined_references = (scanned.undefined_citations,
                                                                     scanned.undefined_references)
            if scanned.undefined_citations:
                check.reasons.append(f"undefined citations {scanned.undefined_citations[:10]}")
            if scanned.undefined_references:
                check.reasons.append(f"undefined references {scanned.undefined_references[:10]}")
            errors = _LATEX_ERROR.findall(text)
            if errors:
                check.reasons.append(f"LaTeX errors {errors[:5]}")
        if uses_bib and not (work / f"{main}.bbl").is_file():
            check.reasons.append("bibliography requested but no .bbl was produced")
        if not check.reasons:
            check.status = "passed"
            check.pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
            if adopt:
                for name in (f"{main}.pdf", f"{main}.log", f"{main}.aux", f"{main}.bbl", f"{main}.blg"):
                    if (work / name).is_file():
                        shutil.copy2(work / name, paper_dir / name)
                check.adopted = True
        return check
    finally:
        shutil.rmtree(work, ignore_errors=True)
