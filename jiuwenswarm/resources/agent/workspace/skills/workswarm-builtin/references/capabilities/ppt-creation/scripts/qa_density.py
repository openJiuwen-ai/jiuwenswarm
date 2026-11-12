#!/usr/bin/env python3
"""Per-page text-volume gate.

Counts each slide's visible text in "visual chars" (CJK/fullwidth = 1,
ASCII/latin/digit = 0.5, whitespace ignored) and judges standard content pages
against floors calibrated on a real internal deck (content pages there
run 400-700 visual chars, median ~480):

    < 300   EMPTY  — hard error: the page reads as half-blank
    300-400 LOW    — warning: justify against the layout, or flesh out
    >= 400  OK

    python3 scripts/qa_density.py output/final.pptx --lock execution-lock.json

With --lock, page roles come from execution-lock.json and only
"standard-content" pages are judged (cover/TOC/closing are exempt). Without
it, pages under 100 visual chars are assumed structural and only warned —
always pass the lock in the deck pipeline.
"""
import json
import logging
import re
import sys
import zipfile
from argparse import ArgumentParser
from pathlib import Path

FLOOR_HARD = 300
FLOOR_WARN = 400
TARGET = "400-700"


# Program output (report bodies, --json payloads) goes to stdout, diagnostics
# to stderr. Both travel through logging; this logger owns stdout, keeps a bare
# "%(message)s" format so the text is unchanged, and does not propagate so the
# stderr root handler never sees it.
STDOUT_LOGGER = logging.getLogger("qa_density.stdout")
STDOUT_LOGGER.propagate = False
STDOUT_LOGGER.setLevel(logging.INFO)
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
STDOUT_LOGGER.addHandler(_stdout_handler)


def emit(line):
    """报告正文是工具输出，走 stdout logger，不与 stderr 诊断混在一起。"""
    STDOUT_LOGGER.info(line)


def vis_len(text):
    return sum(1.0 if ord(c) > 0x2E80 else 0.5 for c in text if not c.isspace())


def slide_chars(pptx):
    with zipfile.ZipFile(pptx) as z:
        names = sorted(
            (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[-1]).group()),
        )
        out = []
        for name in names:
            xml = z.read(name).decode("utf-8", errors="ignore")
            out.append(round(vis_len("".join(re.findall(r"<a:t>([^<]*)</a:t>", xml)))))
        return out


def main():
    ap = ArgumentParser(description=__doc__)
    ap.add_argument("pptx")
    ap.add_argument("--lock", help="execution-lock.json for per-page roles")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    chars = slide_chars(args.pptx)
    roles = [None] * len(chars)
    if args.lock:
        pages = json.loads(Path(args.lock).read_text(encoding="utf-8")).get("pages", [])
        if len(pages) == len(chars):
            roles = [p.get("role") for p in pages]
        else:
            emit(f"qa_density: WARNING lock has {len(pages)} pages but deck has "
                 f"{len(chars)} slides; falling back to role-less judgement")
            args.lock = None

    rows, errors, warnings = [], 0, 0
    for i, (n, role) in enumerate(zip(chars, roles), 1):
        if args.lock:
            judged = role == "standard-content"
        else:
            judged = not (i == 1 or i == len(chars) or n < 100)
        if not judged:
            verdict = "exempt" if args.lock or i in (1, len(chars)) else "structural?"
        elif n < FLOOR_HARD:
            verdict = "EMPTY"
            errors += 1
        elif n < FLOOR_WARN:
            verdict = "LOW"
            warnings += 1
        else:
            verdict = "OK"
        rows.append({"page": i, "role": role or "-", "vis_chars": n, "verdict": verdict})

    if args.json:
        emit(json.dumps({"pages": rows, "errors": errors, "warnings": warnings,
                         "target": TARGET}, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            emit(f"  p{r['page']:>2}  {r['vis_chars']:>5}  {r['verdict']:<11} {r['role']}")
        emit(f"qa_density: {errors} EMPTY (<{FLOOR_HARD}), {warnings} LOW "
             f"(<{FLOOR_WARN}); content-page target {TARGET} visual chars")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
