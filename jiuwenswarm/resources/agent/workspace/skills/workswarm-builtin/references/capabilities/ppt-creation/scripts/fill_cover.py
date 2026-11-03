#!/usr/bin/env python3
"""
scripts/fill_cover.py
Fill the official cover slide's title and 部门/作者/日期 placeholders in an
unpacked PPTX directory.

Usage:
    python scripts/fill_cover.py \\
        --target unpacked/ \\
        --title "Mixture-of-Agents 多智能体模式洞察" \\
        --meta "研发部|吴云凯|2026-07-22"

The template ships slide1 with an EMPTY ctrTitle placeholder, which PowerPoint
renders as blank space. Nothing in the merge pipeline writes it, so a deck
finalized without this step has no cover title at all. --meta appends values
after the existing 部门：/作者：/日期： labels, in that order; empty segments
leave their label bare.

Per base/template-contract.md the main title uses the accent color and stays on
one line. Modifies --target in place. Run clean.py then pack.py afterward.
"""
import logging
import sys
import re
from pathlib import Path
from argparse import ArgumentParser

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("fill_cover.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("fill_cover")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


class FillCoverError(RuntimeError):
    """Fatal condition raised by the helpers; main() turns it into SystemExit.

    Raising SystemExit outside the process entry point is disallowed, and it
    also makes these functions unusable as a library.
    """


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)

# Kept in sync with THEME.accent in scripts/components.js.
ACCENT = "4472C4"

# The cover's ctrTitle paragraph carries its own pPr (font/spacing from the
# template) — keep it and replace only the runs.
TITLE_RUN = (
    '<a:r><a:rPr lang="zh-CN" dirty="0"><a:solidFill>'
    f'<a:srgbClr val="{ACCENT}"/>'
    "</a:solidFill></a:rPr><a:t>{text}</a:t></a:r><a:endParaRPr/>"
)


def read(path):
    return Path(path).read_text(encoding="utf-8")


def write(path, text):
    Path(path).write_text(text, encoding="utf-8")


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def find_sp(xml, pattern):
    """Return (start, end) of the <p:sp> block matching pattern, or None.

    pattern is a regex so callers can match a placeholder tag without depending
    on XML attribute order — PowerPoint and python-pptx emit <p:ph> attributes
    in different orders for the same placeholder.
    """
    for m in re.finditer(r"<p:sp>.*?</p:sp>", xml, re.S):
        if re.search(pattern, m.group(0)):
            return m.span()
    return None


def set_title(sp, title):
    """Replace the runs of the first paragraph, keeping its pPr if it has one."""
    run_xml = TITLE_RUN.format(text=esc(title))

    # Paragraph with explicit properties: keep the pPr, replace what follows.
    new_sp, n = re.subn(
        r"(<a:p>.*?</a:pPr>).*?</a:p>",
        lambda m: m.group(1) + run_xml + "</a:p>",
        sp, count=1, flags=re.S,
    )
    if n:
        return new_sp

    # A placeholder that was never touched serializes as a bare <a:p/> with no
    # properties at all. Write a whole paragraph in its place.
    new_sp, n = re.subn(
        r"<a:p\s*/>|<a:p>.*?</a:p>",
        f"<a:p>{run_xml}</a:p>",
        sp, count=1, flags=re.S,
    )
    if n == 0:
        raise FillCoverError("fill_cover: could not locate the title paragraph in the ctrTitle placeholder")
    return new_sp


def set_meta(sp, values):
    """Append each value after the label text of the Nth paragraph."""
    paras = list(re.finditer(r"<a:p>.*?</a:p>", sp, re.S))
    if len(paras) < len(values):
        raise FillCoverError(
            f"fill_cover: cover meta placeholder has {len(paras)} lines but {len(values)} values were given"
        )
    # Patch back-to-front so earlier spans stay valid.
    for para, value in reversed(list(zip(paras, values))):
        if not value:
            continue
        body = para.group(0)
        new_body, n = re.subn(
            r"(<a:t>[^<]*)(</a:t>)(?!.*<a:t>)",
            lambda m: m.group(1) + esc(value) + m.group(2),
            body,
            count=1,
            flags=re.S,
        )
        if n == 0:
            raise FillCoverError("fill_cover: cover meta line has no text run to append to")
        sp = sp[: para.start()] + new_body + sp[para.end():]
    return sp


def _run():
    ap = ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, help="unpacked PPTX directory")
    ap.add_argument("--title", help="cover main title (accent color, single line)")
    ap.add_argument("--meta", help='部门|作者|日期, pipe-separated; empty segments are skipped')
    args = ap.parse_args()

    if not args.title and not args.meta:
        raise FillCoverError("fill_cover: nothing to do — pass --title and/or --meta")

    slides_dir = Path(args.target) / "ppt" / "slides"
    covers = [p for p in sorted(slides_dir.glob("slide*.xml")) if 'type="ctrTitle"' in read(p)]
    if not covers:
        raise FillCoverError(f"fill_cover: no slide with a ctrTitle placeholder found in {slides_dir}")
    if len(covers) > 1:
        names = ", ".join(p.name for p in covers)
        raise FillCoverError(f"fill_cover: expected exactly one cover slide, found {len(covers)} ({names})")

    path = covers[0]
    xml = read(path)

    if args.title:
        span = find_sp(xml, r'type="ctrTitle"')
        xml = xml[: span[0]] + set_title(xml[span[0]:span[1]], args.title) + xml[span[1]:]

    if args.meta:
        # The meta lines live in the cover's second placeholder. Match on idx
        # alone: PowerPoint templates type it "body", python-pptx's default
        # template types it "subTitle", and attribute order differs too.
        span = find_sp(xml, r'<p:ph[^>]*\bidx="1"')
        if span is None:
            raise FillCoverError("fill_cover: cover has no 部门/作者/日期 placeholder")
        values = [v.strip() for v in args.meta.split("|")]
        xml = xml[: span[0]] + set_meta(xml[span[0]:span[1]], values) + xml[span[1]:]

    write(path, xml)
    emit(f"fill_cover: {path.name} updated")



def main():
    """Process entry point: turn a fatal helper error into a clean exit."""
    try:
        return _run()
    except FillCoverError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
