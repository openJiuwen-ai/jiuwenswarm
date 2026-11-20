#!/usr/bin/env python3
"""Render a deck to a labelled grid of slide thumbnails.

    python3 thumbnail.py deck.pptx                  -> thumbnails.jpg
    python3 thumbnail.py deck.pptx grid --cols 4    -> grid.jpg

Reading an existing deck starts with seeing it. Each cell is labelled with the
part name (slide3.xml) so a visual finding maps straight onto the file to edit.

Requires LibreOffice (`soffice`) for rendering and PyMuPDF for rasterizing.
"""
import logging
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("thumbnail.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("thumbnail")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


class ThumbnailError(RuntimeError):
    """Fatal condition raised by the helpers; main() turns it into SystemExit.

    Raising SystemExit outside the process entry point is disallowed, and it
    also makes these functions unusable as a library.
    """


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)

CELL_W = 480          # px per thumbnail; readable without being huge
LABEL_H = 22
PAD = 8
MAX_PER_SHEET = 24    # beyond this, split into grid-1.jpg, grid-2.jpg, ...


def find_soffice() -> str:
    for candidate in ("soffice",
                      "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if shutil.which(candidate) or Path(candidate).is_file():
            return candidate
    raise ThumbnailError("thumbnail: LibreOffice not found (install it, or `brew install --cask libreoffice`)")


def render_pages(deck: Path, workdir: Path) -> list[Image.Image]:
    """Convert the deck to PDF, then rasterize each page."""
    subprocess.run([find_soffice(), "--headless", "--convert-to", "pdf",
                    str(deck), "--outdir", str(workdir)],
                   check=True, capture_output=True, timeout=300)
    pdf = workdir / f"{deck.stem}.pdf"
    if not pdf.is_file():
        raise ThumbnailError(f"thumbnail: LibreOffice produced no PDF for {deck}")

    try:
        import fitz
    except ImportError as exc:
        raise ThumbnailError("thumbnail: PyMuPDF is required (pip install pymupdf)") from exc

    pages = []
    with fitz.open(pdf) as doc:
        for page in doc:
            zoom = CELL_W / page.rect.width
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    return pages


def build_sheet(pages: list[Image.Image], labels: list[str], cols: int) -> Image.Image:
    rows = (len(pages) + cols - 1) // cols
    cell_h = max(p.height for p in pages) + LABEL_H
    sheet = Image.new("RGB",
                      (cols * (CELL_W + PAD) + PAD, rows * (cell_h + PAD) + PAD),
                      "white")
    draw = ImageDraw.Draw(sheet)
    for i, (img, label) in enumerate(zip(pages, labels)):
        x = PAD + (i % cols) * (CELL_W + PAD)
        y = PAD + (i // cols) * (cell_h + PAD)
        draw.text((x + 2, y + 4), label, fill="black")
        sheet.paste(img, (x, y + LABEL_H))
        draw.rectangle([x, y + LABEL_H, x + img.width, y + LABEL_H + img.height],
                       outline="#C0C0C0")
    return sheet


def _run():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("deck", help="input .pptx")
    ap.add_argument("prefix", nargs="?", default="thumbnails",
                    help="output name without extension (default: thumbnails)")
    ap.add_argument("--cols", type=int, default=3)
    args = ap.parse_args()

    deck = Path(args.deck)
    if not deck.is_file():
        raise ThumbnailError(f"thumbnail: {deck} not found")

    with tempfile.TemporaryDirectory() as tmp:
        pages = render_pages(deck, Path(tmp))

    labels = [f"slide{i + 1}.xml" for i in range(len(pages))]
    written = []
    for start in range(0, len(pages), MAX_PER_SHEET):
        chunk = pages[start:start + MAX_PER_SHEET]
        sheet = build_sheet(chunk, labels[start:start + MAX_PER_SHEET], args.cols)
        suffix = "" if len(pages) <= MAX_PER_SHEET else f"-{start // MAX_PER_SHEET + 1}"
        out = Path(f"{args.prefix}{suffix}.jpg")
        sheet.save(out, quality=88)
        written.append(str(out))

    emit(f"Wrote {', '.join(written)} ({len(pages)} slides)")



def main():
    """Process entry point: turn a fatal helper error into a clean exit."""
    try:
        return _run()
    except ThumbnailError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    sys.exit(main())
