#!/usr/bin/env python3
"""One-command deck finalize: merge generated content slides into the
template and emit the packed PPTX.

    python3 scripts/finalize_deck.py output/content.pptx output/final.pptx

replaces the manual unpack -> merge_slides -> clean -> pack pipeline. All
intermediate unpacked directories live in a throwaway temp dir — nothing to
inspect or clean up by hand, and no reason to read the unpacked XML.

--order tokens: tN = Nth template slide, sN = Nth content slide (1-indexed),
s* = all content slides in order. Default "t1,s*,t5" = template cover + all
generated content + template ending. Template slide roles: t1 cover, t2 TOC,
t3/t4 blank content pages, t5 ending.
"""
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from argparse import ArgumentParser
from pathlib import Path

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("finalize_deck.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("finalize_deck")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


class FinalizeError(RuntimeError):
    """Fatal condition raised by the helpers; main() turns it into SystemExit.

    Raising SystemExit outside the process entry point is disallowed, and it
    also makes these functions unusable as a library.
    """


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent


def run(argv, step):
    proc = subprocess.run([sys.executable, *argv])
    if proc.returncode != 0:
        raise FinalizeError(
            f"finalize_deck: {step} failed (exit {proc.returncode}); "
            f"rerun with --keep-workdir to inspect"
        )


def count_slides(unpacked):
    slides_dir = Path(unpacked) / "ppt" / "slides"
    return len([f for f in slides_dir.glob("slide*.xml")])


def digest(pptx_path):
    with zipfile.ZipFile(pptx_path) as z:
        names = z.namelist()
        slides = len([n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)])
        masters = len([n for n in names if re.fullmatch(r"ppt/slideMasters/slideMaster\d+\.xml", n)])
        layouts = len([n for n in names if re.fullmatch(r"ppt/slideLayouts/slideLayout\d+\.xml", n)])
        prs = z.read("ppt/presentation.xml").decode("utf-8", errors="ignore")
    tag = re.search(r"<p:sldSz\b[^>]*>", prs)
    cx = re.search(r'\bcx="(\d+)"', tag.group(0)) if tag else None
    cy = re.search(r'\bcy="(\d+)"', tag.group(0)) if tag else None
    canvas = (int(cx.group(1)) if cx else 0, int(cy.group(1)) if cy else 0)
    return slides, masters, layouts, canvas


def cover_title_filled(pptx_path):
    """True unless the deck has a cover whose ctrTitle placeholder is empty."""
    with zipfile.ZipFile(pptx_path) as z:
        for name in sorted(n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)):
            xml = z.read(name).decode("utf-8", errors="ignore")
            for m in re.finditer(r"<p:sp>.*?</p:sp>", xml, re.S):
                if 'type="ctrTitle"' not in m.group(0):
                    continue
                return bool("".join(re.findall(r"<a:t>([^<]*)</a:t>", m.group(0))).strip())
    return True  # no template cover in this deck (e.g. --order without t1)


def _run():
    ap = ArgumentParser(description=__doc__)
    ap.add_argument("content", help="generated content .pptx (PptxGenJS output)")
    ap.add_argument("out", help="final .pptx to write")
    ap.add_argument("--template", default=str(SKILL_ROOT / "references" / "template.pptx"))
    ap.add_argument("--order", default="t1,s*,t5",
                    help='slide order; tN/sN/s* tokens (default "t1,s*,t5")')
    ap.add_argument("--template-layout", default="slideLayout7.xml",
                    help="blank content layout inside the template (default: Office's Blank)")
    ap.add_argument("--source-layout-mode", choices=("template", "source"), default="template")
    ap.add_argument("--cover-title", help="cover main title; written into the template's empty ctrTitle placeholder")
    ap.add_argument("--cover-meta", metavar="DEPT|AUTHOR|DATE",
                    help="pipe-separated values appended to the cover's 部门/作者/日期 labels")
    ap.add_argument("--keep-workdir", metavar="DIR",
                    help="keep unpacked intermediates here for debugging (default: temp dir, deleted)")
    args = ap.parse_args()

    content = Path(args.content)
    template = Path(args.template)
    out = Path(args.out)
    for p, what in ((content, "content pptx"), (template, "template pptx")):
        if not p.is_file():
            raise FinalizeError(f"finalize_deck: {what} not found: {p}")
    # Same workspace rule as the validators: task artifacts must not live in
    # the shared skill directory (the template itself is skill material).
    for p in (content.resolve(), out.resolve()):
        if p.is_relative_to(SKILL_ROOT):
            raise FinalizeError(
                f"finalize_deck: task file {p} is inside the skill directory ({SKILL_ROOT}); "
                "move the whole workspace outside skills/ppt-creation (e.g. <workspace>/projects/<task>/)"
            )

    workdir = Path(args.keep_workdir) if args.keep_workdir else Path(tempfile.mkdtemp(prefix="finalize-deck-"))
    workdir.mkdir(parents=True, exist_ok=True)
    tdir, sdir = workdir / "template", workdir / "content"
    try:
        run([SCRIPTS / "opc" / "unpack.py", str(template), str(tdir)], "unpack template")
        run([SCRIPTS / "opc" / "unpack.py", str(content), str(sdir)], "unpack content")

        n = count_slides(sdir)
        if n == 0:
            raise FinalizeError(f"finalize_deck: no slides found in {content}")
        order = ",".join(
            tok if tok != "s*" else ",".join(f"s{i}" for i in range(1, n + 1))
            for tok in (t.strip() for t in args.order.split(","))
        )
        emit(f"finalize_deck: order = {order}")

        run([SCRIPTS / "merge_slides.py", "--target", str(tdir), "--source", str(sdir),
             "--order", order, "--source-layout-mode", args.source_layout_mode,
             "--template-layout", args.template_layout], "merge_slides")
        if args.cover_title or args.cover_meta:
            argv = [SCRIPTS / "fill_cover.py", "--target", str(tdir)]
            if args.cover_title:
                argv += ["--title", args.cover_title]
            if args.cover_meta:
                argv += ["--meta", args.cover_meta]
            run(argv, "fill_cover")
        run([SCRIPTS / "opc" / "prune.py", str(tdir)], "prune")
        out.parent.mkdir(parents=True, exist_ok=True)
        run([SCRIPTS / "opc" / "pack.py", str(tdir), str(out), "--original", str(template)], "pack")
    finally:
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    slides, masters, layouts, canvas = digest(out)
    emit(f"finalize_deck: {out} — {slides} slides, {masters} masters, {layouts} layouts, "
          f"canvas {canvas[0]}×{canvas[1]} EMU")
    # Master-inheritance hard gate: fewer masters/layouts in the output than the
    # template carried means the merge silently dropped template structure
    # (quality-gates.md). The baseline is read from the template itself rather
    # than hard-coded, so the check holds for any template a caller supplies.
    _, tpl_masters, tpl_layouts, _ = digest(template)
    inherits_template = args.source_layout_mode == "template"
    lost_structure = masters < tpl_masters or layouts < tpl_layouts
    if inherits_template and lost_structure:
        raise FinalizeError(
            f"finalize_deck: FAILED master-inheritance check — template carries "
            f"{tpl_masters} masters / {tpl_layouts} layouts but the output has "
            f"{masters} / {layouts}; the merge dropped template structure"
        )
    # cover-title hard gate: the template's ctrTitle ships empty and renders as
    # blank space, so an unfilled cover looks finished but has no title.
    if not cover_title_filled(out):
        raise FinalizeError(
            "finalize_deck: FAILED cover-title check — the template cover's title placeholder is empty; "
            "rerun with --cover-title \"...\" (see base/quality-gates.md)"
        )
    emit("finalize_deck: OK")



def main():
    """Process entry point: turn a fatal helper error into a clean exit."""
    try:
        return _run()
    except FinalizeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
