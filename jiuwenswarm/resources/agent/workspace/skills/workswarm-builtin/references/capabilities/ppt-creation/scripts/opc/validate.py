#!/usr/bin/env python3
"""Validate an unpacked OOXML package against the published ECMA/ISO schemas.

    python3 validate.py unpacked/

Catches the malformed XML that makes PowerPoint show "repair this file" — while
the file still looks fine to a plain XML parser. Two layers:

  1. Schema validation of each part against its XSD (schemas/ ships the
     ECMA-376 / ISO-IEC 29500 documents, which are public standards).
  2. Structural checks that no single-part schema can express, e.g. a slide
     pointing at a layout that isn't in its relationships.
"""
import logging
import argparse
import re
import sys
from pathlib import Path

from lxml import etree

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("validate.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("validate")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)

SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas"

# Pick the schema from the part's ROOT ELEMENT NAMESPACE, not its path. A
# chart under ppt/charts/ is drawingml, not presentationml, and validating it
# against pml.xsd reports nonsense. Anything whose namespace isn't listed is a
# vendor extension with no published schema, and is skipped.
NS_TO_XSD = {
    "http://schemas.openxmlformats.org/presentationml/2006/main":
        "ISO-IEC29500-4_2016/pml.xsd",
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main":
        "ISO-IEC29500-4_2016/wml.xsd",
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main":
        "ISO-IEC29500-4_2016/sml.xsd",
    "http://schemas.openxmlformats.org/drawingml/2006/main":
        "ISO-IEC29500-4_2016/dml-main.xsd",
    "http://schemas.openxmlformats.org/drawingml/2006/chart":
        "ISO-IEC29500-4_2016/dml-chart.xsd",
    "http://schemas.openxmlformats.org/drawingml/2006/chartDrawing":
        "ISO-IEC29500-4_2016/dml-chartDrawing.xsd",
    "http://schemas.openxmlformats.org/drawingml/2006/diagram":
        "ISO-IEC29500-4_2016/dml-diagram.xsd",
    "http://schemas.openxmlformats.org/package/2006/relationships":
        "ecma/fouth-edition/opc-relationships.xsd",
    "http://schemas.openxmlformats.org/package/2006/content-types":
        "ecma/fouth-edition/opc-contentTypes.xsd",
}

# docProps/ is skipped by path: its schema imports the Dublin Core XSDs, which
# ECMA references by URL rather than shipping, and nothing in there can stop
# PowerPoint opening the file.
SKIP_PARTS = re.compile(r"(^|/)docProps/", re.I)

_schema_cache: dict[str, etree.XMLSchema | None] = {}


def schema_for(root_ns: str | None):
    """Return a compiled XMLSchema for this namespace, or None if unchecked."""
    xsd = NS_TO_XSD.get(root_ns or "")
    if not xsd:
        return None
    if xsd not in _schema_cache:
        path = SCHEMA_ROOT / xsd
        if not path.is_file():
            return None
        try:
            _schema_cache[xsd] = etree.XMLSchema(etree.parse(str(path)))
        except etree.XMLSchemaParseError as exc:
            # A schema we cannot compile (usually an unresolvable import) must
            # not take the whole run down — skip that family and say so once.
            LOGGER.error(f"validate: skipping {xsd} — {exc}")
            _schema_cache[xsd] = None
    return _schema_cache[xsd]


def validate_schemas(unpacked: Path) -> list[str]:
    errors: list[str] = []
    for part in sorted(unpacked.rglob("*")):
        if not part.is_file() or part.suffix.lower() not in {".xml", ".rels"}:
            continue
        rel = part.relative_to(unpacked).as_posix()
        if SKIP_PARTS.search(rel):
            continue
        try:
            doc = etree.parse(str(part))
        except etree.XMLSyntaxError as exc:
            errors.append(f"{rel}: not well-formed XML — {exc}")
            continue
        schema = schema_for(etree.QName(doc.getroot()).namespace)
        if schema is None:
            continue
        if not schema.validate(doc):
            for err in schema.error_log:
                errors.append(f"{rel}:{err.line}: {err.message}")
    return errors


def validate_structure(unpacked: Path) -> list[str]:
    """Cross-part checks a per-part schema cannot see."""
    errors: list[str] = []
    slides_dir = unpacked / "ppt" / "slides"
    if not slides_dir.is_dir():
        return errors

    for slide in sorted(slides_dir.glob("slide*.xml")):
        rels = slide.parent / "_rels" / f"{slide.name}.rels"
        if not rels.is_file():
            errors.append(f"ppt/slides/{slide.name}: missing relationships part")
            continue
        rels_text = rels.read_text(encoding="utf-8")
        rel_ids = set(re.findall(r'Id="([^"]+)"', rels_text))

        # Every r:embed / r:id referenced by the slide must exist in its rels.
        slide_text = slide.read_text(encoding="utf-8")
        for rid in set(re.findall(r'r:(?:id|embed)="([^"]+)"', slide_text)):
            if rid not in rel_ids:
                errors.append(f"ppt/slides/{slide.name}: references {rid}, absent from its .rels")

        # Exactly one slideLayout relationship. Match the relationship Type,
        # not the substring: a Target of "../slideLayouts/slideLayout1.xml"
        # contains "/slideLayout" twice on its own.
        layouts = len(re.findall(r'Type="[^"]*/slideLayout"', rels_text))
        if layouts != 1:
            errors.append(f"ppt/slides/{slide.name}: expected 1 slideLayout relationship, found {layouts}")

    # Shape ids must be unique within each slide.
    for slide in sorted(slides_dir.glob("slide*.xml")):
        ids = re.findall(r'<p:cNvPr\b[^>]*\bid="(\d+)"', slide.read_text(encoding="utf-8"))
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            errors.append(f"ppt/slides/{slide.name}: duplicate shape id(s) {sorted(dupes)}")

    return errors


def _fingerprint(error: str) -> str:
    """Identity of an error, independent of where it sits in the file.

    Editing shifts line numbers, so `slide1.xml:88: X` and `slide1.xml:91: X`
    are the same defect. Strip the line number and keep part + message.
    """
    return re.sub(r"^([^:]+):\d+:", r"\1:", error)


def validate(unpacked: Path, baseline: Path | None = None) -> list[str]:
    """Validate `unpacked`, optionally reporting only what `baseline` lacks.

    Generators emit packages that already violate the published schemas — the
    charts PptxGenJS writes order their child elements wrongly, for instance.
    Those files still open fine, and refusing to repack them would block every
    edit of an existing deck. So when a baseline package is supplied, only
    defects ABSENT from it are reported: the question is whether this edit
    introduced a problem, not whether the source was ever perfect.
    """
    errors = validate_schemas(unpacked) + validate_structure(unpacked)
    if baseline is None or not errors:
        return errors

    import tempfile
    import zipfile

    with tempfile.TemporaryDirectory() as tmp:
        base_dir = Path(tmp) / "baseline"
        base_dir.mkdir()
        try:
            with zipfile.ZipFile(baseline) as zf:
                zf.extractall(base_dir)
        except (zipfile.BadZipFile, OSError):
            return errors      # unreadable baseline: report everything
        baseline_errors = validate_schemas(base_dir) + validate_structure(base_dir)
        known = {_fingerprint(e) for e in baseline_errors}

    return [e for e in errors if _fingerprint(e) not in known]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("unpacked", help="unpacked OOXML directory")
    ap.add_argument("--baseline", help="original package; only report defects it lacks")
    args = ap.parse_args()

    errors = validate(Path(args.unpacked),
                      Path(args.baseline) if args.baseline else None)
    if errors:
        LOGGER.error(f"Validation FAILED with {len(errors)} error(s):")
        for err in errors[:40]:
            LOGGER.error(f"  {err}")
        if len(errors) > 40:
            LOGGER.error(f"  ... and {len(errors) - 40} more")
        return 1
    emit("All validations PASSED!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
