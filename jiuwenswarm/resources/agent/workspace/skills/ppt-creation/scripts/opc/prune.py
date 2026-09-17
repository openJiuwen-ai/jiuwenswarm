#!/usr/bin/env python3
"""Drop unreferenced parts from an unpacked OOXML package.

    python3 prune.py unpacked/

Reordering or removing slides leaves orphans behind: the slide XML itself, its
.rels, and any media/chart/notes part only that slide referenced. PowerPoint
tolerates some of this and rejects the rest, so the package is garbage-collected
before packing.

Reachability starts at the package root relationships and follows every
relationship Target transitively — exactly the graph a consumer walks. Anything
unreachable is deleted, and [Content_Types].xml loses the matching overrides.
"""
import logging
import argparse
import re
import sys
from pathlib import Path
from urllib.parse import unquote

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("prune.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("prune")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)

# External targets (hyperlinks) are not package parts.
_REL = re.compile(r'<Relationship\b[^>]*>', re.S)
_ATTR = {k: re.compile(rf'\b{k}="([^"]*)"') for k in ("Id", "Type", "Target", "TargetMode")}


def _attrs(tag: str) -> dict:
    out = {}
    for key, rx in _ATTR.items():
        match = rx.search(tag)
        out[key] = match.group(1) if match else None
    return out


def rels_path_for(part: Path, root: Path) -> Path:
    """Where the .rels for `part` lives."""
    return part.parent / "_rels" / f"{part.name}.rels"


def targets_of(rels_file: Path, root: Path) -> list[Path]:
    """Package parts referenced by one .rels file."""
    if not rels_file.is_file():
        return []
    # A part's .rels sits in <dir>/_rels/<name>.rels and its Targets are
    # relative to <dir>.
    base = rels_file.parent.parent
    out = []
    for tag in _REL.findall(rels_file.read_text(encoding="utf-8")):
        a = _attrs(tag)
        if a["TargetMode"] == "External" or not a["Target"]:
            continue
        target = unquote(a["Target"])
        resolved = (base / target).resolve() if not target.startswith("/") \
            else (root / target.lstrip("/")).resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            # Target escapes the package; skip it rather than treat it as a part.
            LOGGER.error(f"prune: ignoring out-of-package target {target}")
            continue
        out.append(resolved)
    return out


def reachable_parts(root: Path) -> set[Path]:
    """Every part reachable from the package root relationships."""
    seen: set[Path] = set()
    queue = []

    root_rels = root / "_rels" / ".rels"
    if root_rels.is_file():
        seen.add(root_rels.resolve())
        queue.extend(targets_of(root_rels, root))

    while queue:
        part = queue.pop()
        if part in seen or not part.exists():
            continue
        seen.add(part)
        rels = rels_path_for(part, root)
        if rels.is_file():
            seen.add(rels.resolve())
            queue.extend(targets_of(rels, root))
    return seen


_SLD_ID = re.compile(r'<p:sldId\b[^>]*\br:id="([^"]+)"')


def drop_unlisted_slides(root: Path) -> list[str]:
    """Unlink slides missing from sldIdLst, before reachability is computed.

    A slide is only live if it appears in BOTH presentation.xml.rels and the
    sldIdLst ordering. Reordering tools rewrite the list but leave the stale
    relationship behind, which would keep the dropped slide "reachable" and
    silently re-add it to the deck.
    """
    pres = root / "ppt" / "presentation.xml"
    rels = root / "ppt" / "_rels" / "presentation.xml.rels"
    if not pres.is_file() or not rels.is_file():
        return []

    live_ids = set(_SLD_ID.findall(pres.read_text(encoding="utf-8")))
    rels_text = rels.read_text(encoding="utf-8")

    dropped = []
    for tag in _REL.findall(rels_text):
        a = _attrs(tag)
        if not a["Type"] or not a["Type"].endswith("/slide"):
            continue
        if a["Id"] in live_ids:
            continue
        rels_text = rels_text.replace(tag, "")
        dropped.append(a["Target"])

    if dropped:
        rels.write_text(rels_text, encoding="utf-8")
    return dropped


def prune(root: Path) -> list[str]:
    """Delete unreachable parts. Returns their package-relative paths."""
    drop_unlisted_slides(root)
    keep = reachable_parts(root)
    # [Content_Types].xml is package metadata, never a relationship target.
    keep.add((root / "[Content_Types].xml").resolve())

    removed = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.resolve() in keep:
            continue
        removed.append(path.relative_to(root).as_posix())
        path.unlink()

    # Drop now-empty directories, deepest first.
    for path in sorted((p for p in root.rglob("*") if p.is_dir()),
                       key=lambda p: -len(p.parts)):
        if not any(path.iterdir()):
            path.rmdir()

    if removed:
        _drop_content_type_overrides(root, removed)
    return removed


def _drop_content_type_overrides(root: Path, removed: list[str]) -> None:
    ct = root / "[Content_Types].xml"
    if not ct.is_file():
        return
    text = ct.read_text(encoding="utf-8")
    for rel in removed:
        text = re.sub(rf'\s*<Override[^>]*PartName="/{re.escape(rel)}"[^>]*/>', "", text)
    ct.write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("unpacked", help="unpacked OOXML directory")
    args = ap.parse_args()

    removed = prune(Path(args.unpacked))
    if removed:
        emit(f"Removed {len(removed)} unreferenced part(s):")
        for rel in removed:
            emit(f"  {rel}")
    else:
        emit("No unreferenced parts found")


if __name__ == "__main__":
    sys.exit(main())
