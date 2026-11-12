#!/usr/bin/env python3
"""Add a slide to an unpacked PPTX, by copying one or starting from a layout.

    python3 add_slide.py unpacked/ slide2.xml          # duplicate slide2
    python3 add_slide.py unpacked/ slideLayout2.xml    # blank page on that layout

Adding a slide by hand means touching four places at once — the part itself,
its .rels, the content types, and the presentation's slide list. Getting any
one wrong makes PowerPoint call the file corrupt, so this does all four.

Available layouts: ls unpacked/ppt/slideLayouts/
"""
import logging
import argparse
import re
import shutil
import sys
from pathlib import Path

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("add_slide.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("add_slide")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


class AddSlideError(RuntimeError):
    """Fatal condition raised by the helpers; main() turns it into SystemExit.

    Raising SystemExit outside the process entry point is disallowed, and it
    also makes these functions unusable as a library.
    """


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)

SLIDE_CT = ("application/vnd.openxmlformats-officedocument."
            "presentationml.slide+xml")
SLIDE_REL_TYPE = ("http://schemas.openxmlformats.org/officeDocument/2006/"
                  "relationships/slide")
LAYOUT_REL_TYPE = ("http://schemas.openxmlformats.org/officeDocument/2006/"
                   "relationships/slideLayout")

# A slide part with no shapes — the spTree skeleton PowerPoint expects.
EMPTY_SLIDE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" \
xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" \
xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr>
        <p:cNvPr id="1" name=""/>
        <p:cNvGrpSpPr/>
        <p:nvPr/>
      </p:nvGrpSpPr>
      <p:grpSpPr>
        <a:xfrm>
          <a:off x="0" y="0"/>
          <a:ext cx="0" cy="0"/>
          <a:chOff x="0" y="0"/>
          <a:chExt cx="0" cy="0"/>
        </a:xfrm>
      </p:grpSpPr>
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>
"""


def next_free(slides_dir: Path) -> int:
    used = []
    for f in slides_dir.glob("slide*.xml"):
        match = re.match(r"slide(\d+)\.xml$", f.name)
        if match:
            used.append(int(match.group(1)))
    return max(used) + 1 if used else 1


def next_rel_id(rels_text: str) -> str:
    used = {int(m) for m in re.findall(r'Id="rId(\d+)"', rels_text)}
    return f"rId{max(used) + 1 if used else 1}"


def add_slide(root: Path, source: str) -> str:
    slides = root / "ppt" / "slides"
    layouts = root / "ppt" / "slideLayouts"
    slides.mkdir(parents=True, exist_ok=True)
    (slides / "_rels").mkdir(exist_ok=True)

    number = next_free(slides)
    new_name = f"slide{number}.xml"
    new_part = slides / new_name

    if source.startswith("slideLayout"):
        layout = layouts / source
        if not layout.is_file():
            raise AddSlideError(f"add_slide: no such layout: {layout}")
        new_part.write_text(EMPTY_SLIDE, encoding="utf-8")
        rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{LAYOUT_REL_TYPE}" '
                f'Target="../slideLayouts/{source}"/></Relationships>')
        (slides / "_rels" / f"{new_name}.rels").write_text(rels, encoding="utf-8")
    else:
        src_part = slides / source
        if not src_part.is_file():
            raise AddSlideError(f"add_slide: no such slide: {src_part}")
        shutil.copy2(src_part, new_part)
        src_rels = slides / "_rels" / f"{source}.rels"
        if src_rels.is_file():
            shutil.copy2(src_rels, slides / "_rels" / f"{new_name}.rels")

    _register_content_type(root, new_name)
    rel_id = _register_in_presentation(root, new_name)
    return (f"Created ppt/slides/{new_name}\n"
            f'Added to presentation.xml as <p:sldId id="..." r:id="{rel_id}"/>')


def _register_content_type(root: Path, slide_name: str) -> None:
    ct = root / "[Content_Types].xml"
    text = ct.read_text(encoding="utf-8")
    part = f"/ppt/slides/{slide_name}"
    if f'PartName="{part}"' in text:
        return
    override = f'<Override PartName="{part}" ContentType="{SLIDE_CT}"/>'
    ct.write_text(text.replace("</Types>", f"{override}</Types>"), encoding="utf-8")


def _register_in_presentation(root: Path, slide_name: str) -> str:
    rels = root / "ppt" / "_rels" / "presentation.xml.rels"
    rels_text = rels.read_text(encoding="utf-8")
    rel_id = next_rel_id(rels_text)
    entry = (f'<Relationship Id="{rel_id}" Type="{SLIDE_REL_TYPE}" '
             f'Target="slides/{slide_name}"/>')
    rels.write_text(rels_text.replace("</Relationships>", f"{entry}</Relationships>"),
                    encoding="utf-8")

    pres = root / "ppt" / "presentation.xml"
    text = pres.read_text(encoding="utf-8")
    used = {int(m) for m in re.findall(r'<p:sldId id="(\d+)"', text)}
    # Slide ids are their own numbering space and must be >= 256.
    sld_id = max(max(used, default=255) + 1, 256)
    entry = f'<p:sldId id="{sld_id}" r:id="{rel_id}"/>'
    if "</p:sldIdLst>" in text:
        text = text.replace("</p:sldIdLst>", f"{entry}</p:sldIdLst>")
    else:
        text = re.sub(r"(<p:sldMasterIdLst>.*?</p:sldMasterIdLst>)",
                      rf"\1<p:sldIdLst>{entry}</p:sldIdLst>", text, count=1, flags=re.S)
    pres.write_text(text, encoding="utf-8")
    return rel_id


def _run():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("unpacked", help="unpacked PPTX directory")
    ap.add_argument("source", help="slideN.xml to duplicate, or slideLayoutN.xml to start from")
    args = ap.parse_args()

    emit(add_slide(Path(args.unpacked), args.source))



def main():
    """Process entry point: turn a fatal helper error into a clean exit."""
    try:
        return _run()
    except AddSlideError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    sys.exit(main())
