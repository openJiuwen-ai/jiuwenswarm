#!/usr/bin/env python3
"""Pack an unpacked OOXML directory back into a .pptx/.docx/.xlsx.

    python3 pack.py unpacked/ out.pptx
    python3 pack.py unpacked/ out.pptx --no-repair --no-validate

Runs three steps in order: repair generator quirks, validate against the
published schemas, then write the ZIP. Validation failures abort the write —
a package that fails here is one PowerPoint would offer to repair.

Whitespace handling is the subtle part. The pretty-printed tree carries indent
text nodes that must NOT survive into the package, because a text run's content
is whatever sits between its tags. Condensing therefore strips whitespace-only
nodes everywhere EXCEPT inside `*:t` elements, whose whitespace is the user's
actual text.
"""
import logging
import argparse
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.dom import minidom

# Append rather than insert(0, ...): prepending shadows same-named stdlib or
# site-packages modules for the whole process.
sys.path.append(str(Path(__file__).resolve().parent))
from repair import repair_all          # noqa: E402
from validate import validate          # noqa: E402

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("pack.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("pack")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


class PackError(RuntimeError):
    """Fatal condition raised by the helpers; main() turns it into SystemExit.

    Raising SystemExit outside the process entry point is disallowed, and it
    also makes these functions unusable as a library.
    """


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)

SUPPORTED = {".pptx", ".docx", ".xlsx"}

# [Content_Types].xml must be the first entry in an OPC package.
FIRST_ENTRY = "[Content_Types].xml"


def condense(path: Path) -> None:
    """Strip the indentation added by unpack, preserving text-run content."""
    dom = minidom.parse(str(path))
    for element in dom.getElementsByTagName("*"):
        # A `*:t` element holds visible text — never touch what's inside it.
        if element.tagName.split(":")[-1] == "t":
            continue
        for child in list(element.childNodes):
            drop = (
                child.nodeType == child.COMMENT_NODE
                or (child.nodeType == child.TEXT_NODE
                    and child.nodeValue is not None
                    and child.nodeValue.strip() == "")
            )
            if drop:
                element.removeChild(child)
    path.write_bytes(dom.toxml(encoding="UTF-8"))


def pack(unpacked: Path, output: Path, do_repair: bool = True,
         do_validate: bool = True, original: Path | None = None) -> None:
    if not unpacked.is_dir():
        raise PackError(f"pack: {unpacked} is not a directory")
    if output.suffix.lower() not in SUPPORTED:
        raise PackError(f"pack: {output} must be one of {sorted(SUPPORTED)}")

    if do_repair:
        totals = repair_all(unpacked)
        fixed = sum(totals.values())
        if fixed:
            detail = ", ".join(f"{n} x{c}" for n, c in totals.items() if c)
            emit(f"Auto-repaired {fixed} issue(s): {detail}")

    if do_validate:
        errors = validate(unpacked, baseline=original)
        if errors:
            LOGGER.error(f"Validation FAILED with {len(errors)} error(s):")
            for err in errors[:20]:
                LOGGER.error(f"  {err}")
            if len(errors) > 20:
                LOGGER.error(f"  ... and {len(errors) - 20} more")
            raise PackError(f"pack: refusing to write {output}")
        emit("All validations PASSED!")

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "pkg"
        shutil.copytree(unpacked, staged)
        for pattern in ("*.xml", "*.rels"):
            for part in staged.rglob(pattern):
                condense(part)

        files = sorted(p for p in staged.rglob("*") if p.is_file())
        # Order matters: consumers expect [Content_Types].xml up front.
        files.sort(key=lambda p: p.relative_to(staged).as_posix() != FIRST_ENTRY)

        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            for part in files:
                zf.write(part, part.relative_to(staged).as_posix())

    emit(f"Successfully packed {unpacked} to {output}")


def _run():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("unpacked", help="unpacked OOXML directory")
    ap.add_argument("output", help="output .pptx/.docx/.xlsx")
    ap.add_argument("--no-repair", action="store_true", help="skip generator-quirk repair")
    ap.add_argument("--no-validate", action="store_true", help="skip schema validation")
    ap.add_argument("--original",
                    help="source package; report only defects it does not already have")
    args = ap.parse_args()

    pack(Path(args.unpacked), Path(args.output),
         do_repair=not args.no_repair, do_validate=not args.no_validate,
         original=Path(args.original) if args.original else None)



def main():
    """Process entry point: turn a fatal helper error into a clean exit."""
    try:
        return _run()
    except PackError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    sys.exit(main())
