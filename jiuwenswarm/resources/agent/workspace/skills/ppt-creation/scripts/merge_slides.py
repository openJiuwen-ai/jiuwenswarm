#!/usr/bin/env python3
"""
scripts/merge_slides.py
Merge pptxgenjs content slides into a template unpacked directory.

Usage:
    python scripts/merge_slides.py \\
        --target unpacked/ \\
        --source unpacked_content/ \\
        --order t1,t2,s1,s2,t3,s3,t4

    tN = Nth slide from --target (1-indexed, by sort order)
    sN = Nth slide from --source (1-indexed, by sort order)

By default, source/PptxGenJS slides are relinked to the template's blank
content layout. This preserves the official footer, logo, theme, and master
structure while keeping the generated content editable. Use
--source-layout-mode source only for legacy decks that deliberately carry
their own layouts/masters/themes.

Modifies --target in place. Run clean.py then pack.py afterward.
"""
import logging
import sys
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from argparse import ArgumentParser

# Program output goes to stdout, diagnostics to stderr. Both travel through
# logging, with a bare "%(message)s" format so the text is unchanged and the two
# streams stay separate for anything parsing this tool's output.
_OUT = logging.getLogger("merge_slides.out")
_OUT.propagate = False
_OUT.setLevel(logging.INFO)
_out_handler = logging.StreamHandler(sys.stdout)
_out_handler.setFormatter(logging.Formatter("%(message)s"))
_OUT.addHandler(_out_handler)

LOGGER = logging.getLogger("merge_slides")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(message)s"))
LOGGER.addHandler(_err_handler)


def emit(line):
    """Program output on stdout."""
    _OUT.info(line)


def read(path):
    return Path(path).read_text(encoding="utf-8")


def write(path, content):
    Path(path).write_text(content, encoding="utf-8")


def slide_size(pptx_dir):
    """Return presentation slide size in EMUs."""
    prs = read(Path(pptx_dir) / "ppt" / "presentation.xml")
    tag = re.search(r"<p:sldSz\b[^>]*/?>", prs)
    if not tag:
        # Office default widescreen is 13.333 x 7.5 in.
        return 12192000, 6858000
    cx = re.search(r'\bcx="(\d+)"', tag.group(0))
    cy = re.search(r'\bcy="(\d+)"', tag.group(0))
    if not cx or not cy:
        return 12192000, 6858000
    return int(cx.group(1)), int(cy.group(1))


def _scale_attr(tag, attr, scale, offset=0):
    return re.sub(
        rf'\b{attr}="(-?\d+)"',
        lambda m: f'{attr}="{round(int(m.group(1)) * scale + offset)}"',
        tag
    )


def scale_slide_xml(content, source_size, target_size):
    """Scale source slide coordinates to the target presentation canvas."""
    src_cx, src_cy = source_size
    tgt_cx, tgt_cy = target_size
    missing_source_size = not src_cx or not src_cy
    same_canvas = src_cx == tgt_cx and src_cy == tgt_cy
    if missing_source_size or same_canvas:
        return content

    scale = min(tgt_cx / src_cx, tgt_cy / src_cy)
    dx = (tgt_cx - src_cx * scale) / 2
    dy = (tgt_cy - src_cy * scale) / 2

    def scale_off(m):
        tag = _scale_attr(m.group(0), "x", scale, dx)
        return _scale_attr(tag, "y", scale, dy)

    def scale_size(m):
        tag = _scale_attr(m.group(0), "cx", scale)
        return _scale_attr(tag, "cy", scale)

    def scale_child_off(m):
        tag = _scale_attr(m.group(0), "x", scale)
        return _scale_attr(tag, "y", scale)

    def scale_text_size(m):
        return _scale_attr(m.group(0), "sz", scale)

    def scale_line_width(m):
        return _scale_attr(m.group(0), "w", scale)

    content = re.sub(r"<a:off\b[^>]*/>", scale_off, content)
    content = re.sub(r"<a:ext\b[^>]*/>", scale_size, content)
    content = re.sub(r"<a:chOff\b[^>]*/>", scale_child_off, content)
    content = re.sub(r"<a:chExt\b[^>]*/>", scale_size, content)
    content = re.sub(r"<a:(?:rPr|defRPr|endParaRPr)\b[^>]*\bsz=\"\d+\"[^>]*>", scale_text_size, content)
    content = re.sub(r"<a:ln\b[^>]*\bw=\"\d+\"[^>]*>", scale_line_width, content)
    return content


def sorted_slides(slides_dir):
    return sorted(
        Path(slides_dir).glob("slide*.xml"),
        key=lambda f: int(re.search(r"\d+", f.name).group())
    )


def next_media_num(media_dir):
    nums = [int(re.search(r"(\d+)", f.stem).group(1))
            for f in Path(media_dir).glob("image*")
            if re.search(r"\d+", f.stem)]
    return max(nums, default=0) + 1


def next_part_num(part_dir, prefix):
    nums = [int(re.search(r"(\d+)", f.stem).group(1))
            for f in Path(part_dir).glob(f"{prefix}*.xml")
            if re.search(r"\d+", f.stem)]
    return max(nums, default=0) + 1


def add_content_type(target_dir, part_name, content_type):
    ct_path = Path(target_dir) / "[Content_Types].xml"
    ct = read(ct_path)
    if f'PartName="{part_name}"' not in ct:
        ct = ct.replace("</Types>",
                        f'  <Override PartName="{part_name}" ContentType="{content_type}"/>\n</Types>')
        write(ct_path, ct)


CHART_CT = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def next_embedding_num(embed_dir):
    nums = []
    for f in Path(embed_dir).glob("mergedBook*.xlsx"):
        match = re.search(r"mergedBook(\d+)", f.stem)
        if match:
            nums.append(int(match.group(1)))
    return max(nums, default=0) + 1


def normalize_chart_xml(xml):
    """Fix the two ways pptxgenjs chart XML violates the OOXML schema.

    PowerPoint and LibreOffice both accept these, but pack.py validates against
    the ISO-IEC 29500 XSDs and rejects them, which forces --validate false on
    any deck containing a native chart:

    1. c:ser lists c:dPt after c:dLbls; the schema requires dPt first.
    2. c:barChart carries a third c:axId with no matching axis definition.
    """
    # --- 1. dangling axId: keep only ids that a real axis block defines ---
    defined = set()
    for block in re.finditer(r"<c:(catAx|valAx|serAx|dateAx)>(.*?)</c:\1>", xml, re.S):
        first = re.search(r'<c:axId val="(\d+)"\s*/>', block.group(2))
        if first:
            defined.add(first.group(1))

    def prune_axids(m):
        body = m.group(2)
        body = re.sub(
            r'<c:axId val="(\d+)"\s*/>',
            lambda a: a.group(0) if a.group(1) in defined else "",
            body,
        )
        return f"<c:{m.group(1)}>{body}</c:{m.group(1)}>"

    if defined:
        xml = re.sub(
            r"<c:(barChart|lineChart|areaChart|scatterChart|bubbleChart|radarChart)>(.*?)</c:\1>",
            prune_axids, xml, flags=re.S)

    # --- 2. dPt must precede dLbls inside each c:ser ---
    def reorder_ser(m):
        body = m.group(1)
        dpts = re.findall(r"<c:dPt>.*?</c:dPt>", body, re.S)
        if not dpts or "<c:dLbls>" not in body:
            return m.group(0)
        if body.index("<c:dPt>") > body.index("<c:dLbls>"):
            for chunk in dpts:
                body = body.replace(chunk, "", 1)
            body = body.replace("<c:dLbls>", "".join(dpts) + "<c:dLbls>", 1)
        return f"<c:ser>{body}</c:ser>"

    xml = re.sub(r"<c:ser>(.*?)</c:ser>", reorder_ser, xml, flags=re.S)
    return xml


def copy_charts(source_dir, target_dir):
    """Copy source chart parts and their embedded workbooks under non-colliding
    names, returning {old_chart_name: new_chart_name}.

    Both pptxgenjs and the template start numbering at chart1.xml, so a
    plain copy (or no copy at all) silently leaves generated slides pointing at
    the template's demo chart — the values render as the template's sample data
    with no error anywhere. Renaming keeps both charts intact.
    """
    src_charts = Path(source_dir) / "ppt" / "charts"
    if not src_charts.exists():
        return {}

    tgt_charts = Path(target_dir) / "ppt" / "charts"
    tgt_charts.mkdir(parents=True, exist_ok=True)
    (tgt_charts / "_rels").mkdir(exist_ok=True)

    # Embedded workbooks first: chart rels point at them.
    embed_map = {}
    src_embed = Path(source_dir) / "ppt" / "embeddings"
    if src_embed.exists():
        tgt_embed = Path(target_dir) / "ppt" / "embeddings"
        tgt_embed.mkdir(parents=True, exist_ok=True)
        n = next_embedding_num(tgt_embed)
        for f in sorted(src_embed.iterdir()):
            if not f.is_file():
                continue
            new_name = f"mergedBook{n}{f.suffix}"
            shutil.copy2(f, tgt_embed / new_name)
            embed_map[f.name] = new_name
            add_content_type(target_dir, f"/ppt/embeddings/{new_name}", XLSX_CT)
            emit(f"  embedding: {f.name} → {new_name}")
            n += 1

    chart_map = {}
    n = next_part_num(tgt_charts, "chart")
    for f in sorted(src_charts.glob("chart*.xml"),
                    key=lambda x: int(re.search(r"\d+", x.stem).group())):
        new_name = f"chart{n}.xml"
        write(tgt_charts / new_name, normalize_chart_xml(read(f)))
        chart_map[f.name] = new_name
        add_content_type(target_dir, f"/ppt/charts/{new_name}", CHART_CT)
        emit(f"  chart: {f.name} → {new_name}")

        src_rels = src_charts / "_rels" / f"{f.name}.rels"
        if src_rels.exists():
            rels = read(src_rels)
            for old, new in embed_map.items():
                rels = rels.replace(f"../embeddings/{old}", f"../embeddings/{new}")
                rels = rels.replace(f"/ppt/embeddings/{old}", f"/ppt/embeddings/{new}")
            write(tgt_charts / "_rels" / f"{new_name}.rels", rels)
        n += 1

    return chart_map


def max_design_id(pptx_dir):
    """Maximum global master/layout numeric ID used by a presentation."""
    root = Path(pptx_dir) / "ppt"
    values = []
    presentation = root / "presentation.xml"
    if presentation.exists():
        values.extend(
            int(value)
            for value in re.findall(r'<p:sldMasterId\b[^>]*\bid="(\d+)"', read(presentation))
        )
    for master in (root / "slideMasters").glob("slideMaster*.xml"):
        values.extend(
            int(value)
            for value in re.findall(r'<p:sldLayoutId\b[^>]*\bid="(\d+)"', read(master))
        )
    return max(values, default=2147483647)


def remap_master_layout_ids(target_dir, master_names):
    """Give copied masters globally unique sldLayoutId values."""
    next_id = max_design_id(target_dir)
    for name in master_names:
        master_path = Path(target_dir) / "ppt" / "slideMasters" / name
        content = read(master_path)

        def replace_id(match):
            nonlocal next_id
            next_id += 1
            return re.sub(r'\bid="\d+"', f'id="{next_id}"', match.group(0), count=1)

        content = re.sub(r'<p:sldLayoutId\b[^>]*/>', replace_id, content)
        write(master_path, content)


def copy_numbered_parts(source_dir, target_dir, rel_dir, prefix):
    """Copy source parts into target with unique numbered names."""
    src_root = Path(source_dir) / "ppt" / rel_dir
    tgt_root = Path(target_dir) / "ppt" / rel_dir
    tgt_root.mkdir(parents=True, exist_ok=True)
    mapping = {}
    next_num = next_part_num(tgt_root, prefix)
    for src in sorted(src_root.glob(f"{prefix}*.xml"),
                      key=lambda f: int(re.search(r"\d+", f.name).group())):
        new_name = f"{prefix}{next_num}.xml"
        next_num += 1
        shutil.copy2(src, tgt_root / new_name)
        mapping[src.name] = new_name
        emit(f"  {rel_dir}: {src.name} → {new_name}")
    return mapping


def replace_part_targets(content, rel_dir, mapping):
    for old, new in mapping.items():
        content = content.replace(f'../{rel_dir}/{old}', f'../{rel_dir}/{new}')
        content = content.replace(f'Target="{old}"', f'Target="{new}"')
    return content


@dataclass(frozen=True)
class PartMaps:
    """Old part name -> new part name, one map per renamed part family.

    Passed as a unit because the rels rewriters need several of these together;
    threading them as separate parameters pushed both functions past the
    argument-count limit.
    """
    layout: dict = field(default_factory=dict)
    master: dict = field(default_factory=dict)
    theme: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    chart: dict = field(default_factory=dict)


def copy_rels_with_mapped_targets(source_dir, target_dir, rel_dir, mapping, maps):
    src_rels = Path(source_dir) / "ppt" / rel_dir / "_rels"
    tgt_rels = Path(target_dir) / "ppt" / rel_dir / "_rels"
    if not src_rels.exists():
        return
    tgt_rels.mkdir(parents=True, exist_ok=True)
    for old_name, new_name in mapping.items():
        src = src_rels / f"{old_name}.rels"
        if not src.exists():
            continue
        content = read(src)
        content = replace_part_targets(content, "slideLayouts", maps.layout)
        content = replace_part_targets(content, "slideMasters", maps.master)
        content = replace_part_targets(content, "theme", maps.theme)
        for old, new in maps.media.items():
            content = content.replace(f"../media/{old}", f"../media/{new}")
            content = content.replace(f"../../ppt/media/{old}", f"../../ppt/media/{new}")
        write(tgt_rels / f"{new_name}.rels", content)


def copy_source_design(source_dir, target_dir, media_map):
    """Copy pptxgenjs-owned layouts/masters/themes so source slides do not use template layouts."""
    layout_map = copy_numbered_parts(source_dir, target_dir, "slideLayouts", "slideLayout")
    master_map = copy_numbered_parts(source_dir, target_dir, "slideMasters", "slideMaster")
    theme_map = copy_numbered_parts(source_dir, target_dir, "theme", "theme")

    remap_master_layout_ids(target_dir, master_map.values())

    design_maps = PartMaps(layout=layout_map, master=master_map,
                           theme=theme_map, media=media_map)
    copy_rels_with_mapped_targets(source_dir, target_dir, "slideLayouts",
                                  layout_map, design_maps)
    copy_rels_with_mapped_targets(source_dir, target_dir, "slideMasters",
                                  master_map, design_maps)

    for name in layout_map.values():
        add_content_type(target_dir, f"/ppt/slideLayouts/{name}",
                         "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml")
    for name in master_map.values():
        add_content_type(target_dir, f"/ppt/slideMasters/{name}",
                         "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml")
    for name in theme_map.values():
        add_content_type(target_dir, f"/ppt/theme/{name}",
                         "application/vnd.openxmlformats-officedocument.theme+xml")

    return layout_map, master_map, theme_map


def relink_slide_layout(content, template_layout):
    """Relink a slide relationship file to one existing template layout."""
    relationship = re.compile(
        r'<Relationship\b[^>]*Type="[^"]*/slideLayout"[^>]*/>', re.DOTALL
    )
    existing = relationship.search(content)
    target = f"../slideLayouts/{template_layout}"
    if existing:
        updated = re.sub(r'Target="[^"]+"', f'Target="{target}"', existing.group(0))
        return content[:existing.start()] + updated + content[existing.end():]

    rids = [int(value) for value in re.findall(r'Id="rId(\d+)"', content)]
    rid = max(rids, default=0) + 1
    rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
    entry = f'  <Relationship Id="rId{rid}" Type="{rel_type}" Target="{target}"/>\n'
    return content.replace("</Relationships>", entry + "</Relationships>")


def fix_rels(content, maps, source_layout_mode, template_layout):
    """Redirect slideLayout, media and chart targets in a .rels file."""
    if source_layout_mode == "source":
        content = replace_part_targets(content, "slideLayouts", maps.layout)
    else:
        content = relink_slide_layout(content, template_layout)
    # Drop notesSlide relationships
    content = re.sub(r'<Relationship\b[^>]*/>', _drop_notes, content)
    # Rename media
    for old, new in maps.media.items():
        content = content.replace(f"../../ppt/media/{old}", f"../../ppt/media/{new}")
        content = content.replace(f"../media/{old}", f"../media/{new}")
    # Rename charts. pptxgenjs writes absolute targets ("/ppt/charts/chart1.xml");
    # PowerPoint-authored decks use relative ones.
    for old, new in maps.chart.items():
        content = content.replace(f"/ppt/charts/{old}", f"/ppt/charts/{new}")
        content = content.replace(f"../charts/{old}", f"../charts/{new}")
    return content


def _drop_notes(m):
    return "" if "notesSlide" in m.group(0) else m.group(0)


def main():
    ap = ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--order", required=True)
    ap.add_argument(
        "--source-layout-mode",
        choices=("template", "source"),
        default="template",
        help="template (default) inherits an existing official layout; source keeps legacy PptxGenJS design parts",
    )
    ap.add_argument(
        "--template-layout",
        default="slideLayout6.xml",
        help="template layout used by generated content slides (default: slideLayout6.xml)",
    )
    args = ap.parse_args()

    target = Path(args.target)
    source = Path(args.source)
    order = [x.strip() for x in args.order.split(",")]

    template_layout_path = target / "ppt" / "slideLayouts" / args.template_layout
    if args.source_layout_mode == "template" and not template_layout_path.exists():
        raise SystemExit(f"Template layout not found: {template_layout_path}")

    t_slides = [f.name for f in sorted_slides(target / "ppt" / "slides")]
    s_slides = [f.name for f in sorted_slides(source / "ppt" / "slides")]
    emit(f"Template slides : {t_slides}")
    emit(f"Source slides   : {s_slides}")
    target_size = slide_size(target)
    source_size = slide_size(source)
    emit(f"Target slide size: {target_size[0]} × {target_size[1]} EMU")
    emit(f"Source slide size: {source_size[0]} × {source_size[1]} EMU")

    # --- Copy source media with renamed names ---
    tgt_media = target / "ppt" / "media"
    src_media = source / "ppt" / "media"
    tgt_media.mkdir(parents=True, exist_ok=True)
    media_map = {}
    if src_media.exists():
        n = next_media_num(tgt_media)
        for f in sorted(src_media.iterdir()):
            if f.is_file():
                new_name = f"image{n}{f.suffix}"
                shutil.copy2(f, tgt_media / new_name)
                media_map[f.name] = new_name
                emit(f"  media: {f.name} → {new_name}")
                n += 1

    # --- Copy source charts + embedded workbooks with renamed parts ---
    chart_map = copy_charts(source, target)

    if args.source_layout_mode == "source":
        emit("Source layout mode: source (legacy copied layouts/masters/themes)")
        layout_map, master_map, theme_map = copy_source_design(source, target, media_map)
    else:
        emit(f"Source layout mode: template ({args.template_layout})")
        layout_map, master_map, theme_map = {}, {}, {}

    # The per-slide rels rewriter needs these three together on every iteration.
    slide_maps = PartMaps(layout=layout_map, media=media_map, chart=chart_map)

    # Next available slide number in target
    next_num = max((int(re.search(r"\d+", n).group()) for n in t_slides), default=0) + 1

    # --- Copy source slides with new names ---
    s_name_map = {}  # original source name -> new name in target
    for item in order:
        if not item.startswith("s"):
            continue
        idx = int(item[1:]) - 1
        if idx >= len(s_slides):
            emit(f"WARNING: s{idx+1} out of range")
            continue
        src_name = s_slides[idx]
        if src_name in s_name_map:
            continue
        new_name = f"slide{next_num}.xml"
        next_num += 1
        s_name_map[src_name] = new_name

        slide_xml = read(source / "ppt/slides" / src_name)
        slide_xml = scale_slide_xml(slide_xml, source_size, target_size)
        write(target / "ppt/slides" / new_name, slide_xml)
        emit(f"  slide: {src_name} → {new_name}")

        src_rels_f = source / "ppt/slides/_rels" / f"{src_name}.rels"
        tgt_rels_f = target / "ppt/slides/_rels" / f"{new_name}.rels"
        if src_rels_f.exists():
            write(
                tgt_rels_f,
                fix_rels(
                    read(src_rels_f), slide_maps,
                    args.source_layout_mode, args.template_layout,
                ),
            )
        else:
            ns = "http://schemas.openxmlformats.org/package/2006/relationships"
            lt = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
            if args.source_layout_mode == "source" and layout_map:
                layout_ref = f"../slideLayouts/{next(iter(layout_map.values()))}"
            else:
                layout_ref = f"../slideLayouts/{args.template_layout}"
            write(tgt_rels_f,
                  f'<?xml version="1.0" encoding="UTF-8"?>\n'
                  f'<Relationships xmlns="{ns}">\n'
                  f'  <Relationship Id="rId1" Type="{lt}" Target="{layout_ref}"/>\n'
                  f'</Relationships>\n')

    # --- Resolve final ordered slide list ---
    final = []
    for item in order:
        idx = int(item[1:]) - 1
        if item.startswith("t"):
            if idx < len(t_slides):
                final.append(t_slides[idx])
        elif item.startswith("s"):
            src = s_slides[idx] if idx < len(s_slides) else None
            if src and src in s_name_map:
                final.append(s_name_map[src])
    emit(f"\nFinal order: {final}")

    # --- Update presentation.xml.rels ---
    prs_rels_path = target / "ppt/_rels/presentation.xml.rels"
    prs_rels = read(prs_rels_path)

    # Map existing slide name -> rId
    existing_rids = {m.group(2): m.group(1)
                     for m in re.finditer(r'Id="(rId\d+)"[^>]+slides/(slide\d+\.xml)"', prs_rels)}
    # Relationship IDs are unique across every relationship type, not only
    # slide relationships. Templates commonly use higher rIds for masters,
    # themes, views, and properties.
    max_rid = max((int(value) for value in re.findall(r'Id="rId(\d+)"', prs_rels)), default=0)

    slide_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
    master_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
    new_rids = {}
    for name in final:
        if name not in existing_rids:
            max_rid += 1
            rid = f"rId{max_rid}"
            new_rids[name] = rid
            prs_rels = prs_rels.replace(
                "</Relationships>",
                f'  <Relationship Id="{rid}" Type="{slide_type}" Target="slides/{name}"/>\n</Relationships>'
            )
    existing_master_rids = {m.group(2): m.group(1)
                            for m in re.finditer(r'Id="(rId\d+)"[^>]+slideMasters/(slideMaster\d+\.xml)"', prs_rels)}
    master_rids = {}
    for name in master_map.values():
        if name not in existing_master_rids:
            max_rid += 1
            rid = f"rId{max_rid}"
            master_rids[name] = rid
            prs_rels = prs_rels.replace(
                "</Relationships>",
                f'  <Relationship Id="{rid}" Type="{master_type}" Target="slideMasters/{name}"/>\n</Relationships>'
            )
    write(prs_rels_path, prs_rels)
    all_rids = {**existing_rids, **new_rids}

    # --- Update presentation.xml sldIdLst ---
    prs_path = target / "ppt/presentation.xml"
    prs = read(prs_path)
    # Slide IDs and slide-master IDs have different numeric domains. Looking
    # at every generic id="..." can pick the template's 2^31-range master ID
    # and overflow the slide ID schema.
    max_sld_id = max(
        (int(m.group(1)) for m in re.finditer(r'<p:sldId\b[^>]*\bid="(\d+)"', prs)),
        default=255,
    )
    rns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    if master_rids:
        next_master_id = max_design_id(target)
        master_entries = []
        for rid in master_rids.values():
            next_master_id += 1
            master_entries.append(f'    <p:sldMasterId id="{next_master_id}" r:id="{rid}"/>')
        prs = prs.replace("</p:sldMasterIdLst>",
                          "\n".join(master_entries) + "\n  </p:sldMasterIdLst>")
    entries = []
    for name in final:
        rid = all_rids.get(name)
        if rid:
            max_sld_id += 1
            entries.append(f'    <p:sldId id="{max_sld_id}" r:id="{rid}"/>')
    new_lst = "<p:sldIdLst>\n" + "\n".join(entries) + "\n  </p:sldIdLst>"
    prs = re.sub(r"<p:sldIdLst>.*?</p:sldIdLst>", new_lst, prs, flags=re.DOTALL)
    write(prs_path, prs)

    # --- Update [Content_Types].xml ---
    ct_path = target / "[Content_Types].xml"
    ct = read(ct_path)
    slide_ct = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
    for name in final:
        part = f"/ppt/slides/{name}"
        if part not in ct:
            ct = ct.replace("</Types>",
                            f'  <Override PartName="{part}" ContentType="{slide_ct}"/>\n</Types>')
    write(ct_path, ct)

    emit("\nDone. Next:")
    emit(f"  python scripts/opc/prune.py {target}")
    emit(f"  python scripts/opc/pack.py {target} output.pptx --original template.pptx")


if __name__ == "__main__":
    main()
