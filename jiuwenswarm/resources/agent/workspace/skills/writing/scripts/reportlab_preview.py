"""Non-publication PDF preview for a generated LaTeX draft.

This renderer exists only when a real TeX compiler is unavailable.  It makes a
readable review copy; it does not claim to reproduce the target conference
template, mathematical layout, bibliography, or float placement.
"""
from __future__ import annotations

import re
from pathlib import Path


def is_pdf(path: Path) -> bool:
    return path.is_file() and path.read_bytes()[:8].startswith(b"%PDF-")


def render_preview(tex_path: Path, pdf_path: Path) -> Path:
    """Render title, abstract, and sections as a readable ReportLab preview."""
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    raw = tex_path.read_text(encoding="utf-8")
    title = _capture(raw, r"\\title\{([^}]*)\}")
    abstract = _capture(raw, r"\\begin\{abstract\}(.*?)\\end\{abstract\}", re.DOTALL)
    chunks = re.split(r"\\section\*?\{([^}]*)\}", raw)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PreviewTitle", parent=styles["Title"], alignment=TA_CENTER, fontSize=18, spaceAfter=16)
    heading_style = ParagraphStyle("PreviewHeading", parent=styles["Heading1"], fontSize=14, spaceBefore=16, spaceAfter=8)
    body_style = ParagraphStyle("PreviewBody", parent=styles["Normal"], alignment=TA_JUSTIFY, fontSize=10, leading=14, spaceAfter=8)
    abstract_style = ParagraphStyle("PreviewAbstract", parent=body_style, leftIndent=24, rightIndent=24, spaceAfter=16)

    story = [Paragraph(_plain(title or "Untitled"), title_style)]
    if abstract:
        story.extend([Paragraph("Abstract", heading_style), Paragraph(_plain(abstract), abstract_style), Spacer(1, 4)])
    for index in range(1, len(chunks), 2):
        heading = chunks[index]
        body = chunks[index + 1] if index + 1 < len(chunks) else ""
        if heading.strip().lower() in {"references", "appendix"}:
            continue
        story.append(Paragraph(_plain(heading), heading_style))
        for paragraph in re.split(r"\n\s*\n", body):
            clean = _plain(paragraph)
            if clean:
                story.append(Paragraph(clean, body_style))

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(pdf_path), pagesize=A4, leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch).build(story)
    return pdf_path


def _capture(text: str, pattern: str, flags: int = 0) -> str:
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else ""


def _plain(text: str) -> str:
    text = re.sub(r"%.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\\(begin|end)\{[^}]*\}", "", text)
    text = re.sub(r"\\(?:cite|citep|citet|ref|label)\{[^}]*\}", "", text)
    text = re.sub(r"\\(?:textbf|textit|emph|texttt)\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+(?:\[[^]]*\])?", "", text)
    text = text.replace("{", "").replace("}", "").replace("&", "and")
    return re.sub(r"\s+", " ", text).strip().replace("&", "and")
