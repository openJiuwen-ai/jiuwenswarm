"""Post-compile publication gate for a formal PDF."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


_UNRESOLVED_REFERENCE = re.compile(r"(?:\?\?|\b(?:Figure|Table)\s+(?:\[?[A-Za-z][\w-]*_[\w:-]+\]?|fig:|tab:))", re.IGNORECASE)


def _publication_text_errors(page_text: list[str]) -> list[str]:
    """Detect release-breaking text left behind by TeX or the renderer."""
    errors: list[str] = []
    for index, text in enumerate(page_text, start=1):
        normalized = text.strip()
        if not normalized:
            errors.append(f"PDF page {index} has no extractable text")
        if _UNRESOLVED_REFERENCE.search(normalized):
            errors.append(f"PDF page {index} contains an unresolved figure or table reference")
    return errors


def _extract_pdf_pages(path: Path) -> list[str]:
    """Extract inspectable page text with a library-free Poppler fallback.

    The release gate must validate the generated PDF, not the accidental
    presence of one optional Python package in the worker environment.  The
    bundled desktop runtime normally has Poppler, and its form-feed separated
    text is sufficient for this gate's page-level checks.
    """
    try:
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        return [(page.extract_text() or "") for page in PdfReader(str(path)).pages]
    except ImportError:
        executable = shutil.which("pdftotext")
        if not executable:
            raise RuntimeError("PDF text extractor is unavailable")
        completed = subprocess.run(
            [executable, "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=True,
        )
        # pdftotext uses form feeds between pages; discard only a terminal
        # separator so an intentionally blank page remains observable.
        return completed.stdout.split("\f")[:-1] if completed.stdout.endswith("\f") else completed.stdout.split("\f")


def _pdf_page_count(path: Path) -> int:
    """Read page count through Poppler when no Python PDF reader is installed."""
    executable = shutil.which("pdfinfo")
    if not executable:
        ppm = shutil.which("pdftoppm")
        candidate = Path(ppm).with_name("pdfinfo.exe") if ppm else None
        executable = str(candidate) if candidate and candidate.is_file() else None
    if not executable:
        return 0
    completed = subprocess.run(
        [executable, str(path)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=True,
    )
    match = re.search(r"^Pages:\s*(\d+)\s*$", completed.stdout, flags=re.MULTILINE)
    return int(match.group(1)) if match else 0


def validate_pdf(pdf_path: str | Path | None, output_dir: str | Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    pages = 0
    path = Path(pdf_path) if pdf_path else None
    if not path or not path.is_file() or path.stat().st_size <= 5000:
        errors.append("formal PDF is missing or too small")
    else:
        try:
            try:
                page_text = _extract_pdf_pages(path)
            except RuntimeError as exc:
                pages = _pdf_page_count(path)
                if pages:
                    warnings.append(f"PDF text extraction unavailable; structural page checks used instead: {exc}")
                    page_text = []
                else:
                    raise
            pages = len(page_text)
            if not page_text and pages == 0:
                pages = _pdf_page_count(path)
            if pages == 0:
                errors.append("PDF has no pages")
            if page_text:
                errors.extend(_publication_text_errors(page_text))
                text = "\n".join(page_text[:2])
                if "anonymous author" not in text.casefold():
                    errors.append("PDF does not appear to be anonymized")
        except Exception as exc:
            errors.append(f"PDF cannot be inspected: {exc}")
    out = Path(output_dir)
    tex_path = out / "paper.tex"
    bib_path = out / "refs.bib"
    blg_path = out / "paper.blg"
    if bib_path.is_file() and bib_path.read_text(encoding="utf-8").strip():
        tex = tex_path.read_text(encoding="utf-8") if tex_path.is_file() else ""
        if not re.search(r"\\cite(?:p|t)?\{[^}]+\}", tex):
            errors.append("bibliography has entries but paper.tex has no in-text citations")
        if blg_path.is_file() and "no \\citation commands" in blg_path.read_text(encoding="utf-8", errors="replace"):
            errors.append("BibTeX found no citation commands; references would be empty")
    log_path = out / "paper.log"
    if log_path.is_file() and re.search(r"(?:Overfull|Underfull) \\[hv]box", log_path.read_text(encoding="utf-8", errors="replace")):
        warnings.append("TeX log reports an overfull or underfull box; inspect the affected PDF page visually.")
    report = {"passed": not errors, "pdf_path": str(path) if path else "", "page_count": pages, "errors": errors, "warnings": warnings}
    (out / "pdf_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
