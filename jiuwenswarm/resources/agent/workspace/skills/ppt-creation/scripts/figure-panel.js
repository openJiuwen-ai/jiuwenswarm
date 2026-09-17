// Figure panel engine — reads PNG dimensions and computes pure geometry for
// laying out extracted paper figures into a content-page region. Mirrors the
// system-diagram.js split: this file holds the engine, components.js
// holds the thin addFigurePanel wrapper.
//
// Everything here is SYNCHRONOUS on purpose: slides.js calls components in
// sequence, so sharp (async) is not an option for reading image sizes.
const fs = require("fs");

// A figure this wide (or wider) takes a full row on its own; anything squarer
// pairs up with a neighbour. Calibrated against real papers: must sit between
// 1.48 (Alita GAIA table) and 2.96 (Alita architecture figure).
const WIDE_AR = 2.5;
const LABEL_H = 0.22;   // inches reserved under each row for its Chinese label
const GAP = 0.12;       // inches between rows and between paired figures

// Mirrored from components.js (FONT, THEME.darkGray). Duplicated rather
// than imported because that module requires this one — same reason
// system-diagram.js carries its own DEFAULT_THEME palette.
const LABEL_FONT = "Microsoft YaHei";
const LABEL_COLOR = "595757";

// Read a PNG's pixel dimensions from its IHDR header, synchronously.
// PNG layout: 8-byte signature, 4-byte chunk length, 4-byte "IHDR",
// then width and height as big-endian uint32 at offsets 16 and 20.
function pngSize(filePath) {
  const fd = fs.openSync(filePath, "r");
  const buf = Buffer.alloc(24);
  try {
    fs.readSync(fd, buf, 0, 24, 0);
  } finally {
    fs.closeSync(fd);
  }
  if (buf.readUInt32BE(0) !== 0x89504e47) throw new Error(`not a PNG: ${filePath}`);
  if (buf.toString("ascii", 12, 16) !== "IHDR") throw new Error(`not a PNG: missing IHDR: ${filePath}`);
  return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
}

// Lay figures out in rows inside a w×h region. Returns rects relative to the
// region's top-left. Labels and gaps keep their absolute size; only the images
// scale, so labels stay readable however tight the packing gets.
function layoutFigures(figs, w, h) {
  const rows = [];
  for (let i = 0; i < figs.length; i++) {
    if (figs[i].ar >= WIDE_AR) rows.push([i]);
    else if (i + 1 < figs.length && figs[i + 1].ar < WIDE_AR) { rows.push([i, i + 1]); i++; }
    else rows.push([i]);
  }

  const natural = rows.map((r) => {
    const cw = r.length === 2 ? (w - GAP) / 2 : w;
    return Math.max(...r.map((i) => cw / figs[i].ar));
  });

  const avail = h - rows.length * LABEL_H - GAP * (rows.length - 1);
  const naturalSum = natural.reduce((a, b) => a + b, 0);
  const scale = Math.min(1, avail / naturalSum);

  let y = 0;
  const rects = [];
  rows.forEach((r, ri) => {
    const cw = (r.length === 2 ? (w - GAP) / 2 : w) * scale;
    const rowH = natural[ri] * scale;
    const rowW = r.length === 2 ? cw * 2 + GAP : cw;
    let x = (w - rowW) / 2;
    r.forEach((i) => {
      const ih = cw / figs[i].ar;
      rects.push({ i, x, y: y + (rowH - ih) / 2, w: cw, h: ih, labelY: y + rowH });
      x += cw + GAP;
    });
    y += rowH + LABEL_H + (ri < rows.length - 1 ? GAP : 0);
  });

  return { rects, rows: rows.length, scale, totalH: y };
}

// Draw the figures into `bounds`. Images are pasted verbatim — no border, no
// fill, no pixel-level edit (the Python extractor already trimmed white
// margins). Labels are centered Chinese captions under each figure.
// No `pres` parameter: unlike renderSystemDiagram (which needs pres.ShapeType),
// pasting images and captions only ever touches the slide.
function renderFigurePanel({ slide, figures, bounds }) {
  const figs = figures.map((f) => {
    const s = pngSize(f.path);
    return { ar: s.w / s.h };
  });
  const out = layoutFigures(figs, bounds.w, bounds.h);
  const oy = bounds.y + (bounds.h - out.totalH) / 2; // 内容不足时垂直居中

  out.rects.forEach((c) => {
    const f = figures[c.i];
    slide.addImage({ path: f.path, x: bounds.x + c.x, y: oy + c.y, w: c.w, h: c.h });
    if (f.label) {
      slide.addText(String(f.label), {
        x: bounds.x + c.x, y: oy + c.labelY, w: c.w, h: LABEL_H,
        fontFace: LABEL_FONT, fontSize: 10, color: LABEL_COLOR,
        align: "center", valign: "middle", margin: 0, fit: "shrink",
      });
    }
  });
  return out;
}

module.exports = { pngSize, layoutFigures, renderFigurePanel, WIDE_AR, LABEL_H, GAP };
