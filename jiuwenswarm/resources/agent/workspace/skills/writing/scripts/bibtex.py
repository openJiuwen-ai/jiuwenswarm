from __future__ import annotations

import json
import html
import re
from pathlib import Path


def _bib_value(value: object) -> str:
    """Produce conservative BibTeX values without Python list representations."""
    if isinstance(value, (list, tuple)):
        value = " and ".join(str(item).strip() for item in value if str(item).strip())
    text = html.unescape(str(value or "")).replace("{", "").replace("}", "").strip()
    replacements = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_"}
    return re.sub(r"[&%$#_]", lambda match: replacements[match.group(0)], text)


def _normalise_bibtex_block(value: object) -> str:
    """Decode publisher HTML entities and protect bare BibTeX ampersands."""
    text = html.unescape(str(value or "")).strip()
    return re.sub(r"(?<!\\)&", r"\\&", text)


def write_refs_bib(bibliography_path: str, output_dir: str) -> str:
    entries = json.loads(Path(bibliography_path).read_text(encoding="utf-8")).get("entries", [])
    blocks: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("bibtex"):
            blocks.append(_normalise_bibtex_block(entry["bibtex"]))
            continue
        if not all(entry.get(key) for key in ("id", "title", "authors", "year", "venue")):
            continue
        blocks.append(
            "@inproceedings{%s,\n  author = {%s},\n  title = {%s},\n"
            "  booktitle = {%s},\n  year = {%s}\n}"
            % tuple(_bib_value(entry[key]) for key in ("id", "authors", "title", "venue", "year"))
        )
    path = Path(output_dir) / "refs.bib"
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return str(path)
