#!/usr/bin/env python3
"""Unpack an OOXML package (.pptx/.docx/.xlsx) into an editable directory.

    python3 unpack.py deck.pptx unpacked/

An OOXML file is a ZIP of XML parts. Editing them by hand — or with the regex
passes in merge_slides.py — needs the XML pretty-printed, because the shipped
form is one enormous line.

The output format is a contract, not a preference: merge_slides.py matches
`<p:sp>` blocks and attribute patterns against this text. Two properties matter:

  - Two-space indent, one element per line.
  - An element whose only child is text stays on ONE line
    (`<a:t>标题</a:t>`, never `<a:t>\\n  标题\\n</a:t>`). Splitting it would
    inject whitespace INTO the slide's visible text. Python's minidom does this
    correctly for single-text-node elements; pack.py additionally refuses to
    strip whitespace inside `*:t` elements when condensing.
"""
import logging
import argparse
import sys
import zipfile
from pathlib import Path
from xml.dom import minidom

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("unpack.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("unpack")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


class UnpackError(RuntimeError):
    """Fatal condition raised by the helpers; main() turns it into SystemExit.

    Raising SystemExit outside the process entry point is disallowed, and it
    also makes these functions unusable as a library.
    """


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)

SUPPORTED = {".pptx", ".docx", ".xlsx"}
XML_PATTERNS = ("*.xml", "*.rels")


def pretty_print(path: Path) -> bool:
    """Rewrite one XML part with indentation. Returns False if it isn't XML."""
    try:
        dom = minidom.parseString(path.read_text(encoding="utf-8"))
    except Exception:
        # Not every .xml in a package is well-formed text we can reformat
        # (thumbnails and vendor blobs slip in). Leave those byte-identical.
        return False
    path.write_bytes(dom.toprettyxml(indent="  ", encoding="utf-8"))
    return True


def unpack(archive: Path, target: Path) -> int:
    """Extract `archive` into `target` and pretty-print its XML parts."""
    if archive.suffix.lower() not in SUPPORTED:
        raise UnpackError(f"unpack: {archive} must be one of {sorted(SUPPORTED)}")
    if not archive.is_file():
        raise UnpackError(f"unpack: {archive} does not exist")

    target.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            # Refuse absolute paths and ../ escapes before writing anything.
            for name in zf.namelist():
                resolved = (target / name).resolve()
                if not str(resolved).startswith(str(target.resolve())):
                    raise UnpackError(f"unpack: refusing unsafe path in archive: {name}")
            zf.extractall(target)
    except zipfile.BadZipFile as exc:
        raise UnpackError(f"unpack: {archive} is not a valid OOXML package") from exc

    formatted = 0
    for pattern in XML_PATTERNS:
        for part in target.rglob(pattern):
            if pretty_print(part):
                formatted += 1
    return formatted


def _run():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archive", help="OOXML file to unpack")
    ap.add_argument("target", help="directory to unpack into")
    args = ap.parse_args()

    count = unpack(Path(args.archive), Path(args.target))
    emit(f"Unpacked {args.archive} ({count} XML files)")



def main():
    """Process entry point: turn a fatal helper error into a clean exit."""
    try:
        return _run()
    except UnpackError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    sys.exit(main())
