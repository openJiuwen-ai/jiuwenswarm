// Deck component library for pptxgenjs.
// Canvas: LAYOUT_16x9 (10" x 5.625"). Set pres.layout = "LAYOUT_16x9" before adding slides.
// API conventions:
//   - Framing components:  fn(pres, opts) -> slide   (they create the slide)
//   - Content components:  fn(pres, slide, opts)     (they draw on an existing slide)
// Colors are 6-char hex WITHOUT "#". Never pass "#"-prefixed or 8-char colors to pptxgenjs.

const path = require("path");

// System-diagram engine (declarative IR -> native shapes). Sibling module.
const { renderSystemDiagram, DEFAULT_THEME, item, group, bandNode, groupBand } = require(path.join(__dirname, "system-diagram.js"));
// Figure-panel engine (paper figures -> pasted images). Sibling module.
const { renderFigurePanel } = require(path.join(__dirname, "figure-panel.js"));

// Font bounds per diagram region size: keep text readable in narrow regions by
// clamping rather than scaling to nothing.
const DIAGRAM_DENSITY = {
  full:    { fontMin: 7,   fontMax: 13 },   // full-width main diagram (w >= 8)
  split:   { fontMin: 6.5, fontMax: 11 },   // left half of an image-text page (4 <= w < 8)
  compact: { fontMin: 6,   fontMax: 9.5 },  // small supporting diagram (w < 4)
};

// Design tokens. `accent` drives every emphasis in the deck — title, summary
// banner, KPI numbers, chart series 1 — so changing it here rethemes the whole
// output. Default is Office theme Accent1, matching references/template.pptx.
const THEME = {
  accent:      "4472C4",
  accentMid:   "8FAADC",
  accentLight: "B4C7E7",
  black:     "000000",
  darkText:  "1D1D1A",
  darkGray:  "595757",
  midGray:   "898989",
  lightGray: "B5B5B5",
  border:    "A6A6A6",
  paleGray:  "DDDDDD",
  white:     "FFFFFF",
};

const FONT = "Microsoft YaHei";

// Soft outer shadow shared by white cards — reference decks' cards float
// slightly instead of sitting flat as wireframe boxes.
//
// MUST stay a factory, never a shared constant. PptxGenJS rewrites
// options.shadow IN PLACE while emitting XML (blur ×12700, angle ×60000,
// opacity ×100000), so one object reused across shapes gets re-converted once
// per shape: card 2 emits blurRad="483870000", card 3 "6145149000000", and dir
// blows past the 21600000 ceiling of ST_PositiveFixedAngle. PowerPoint then
// refuses to open the deck and offers to repair it.
const cardShadow = () => ({ type: "outer", color: "B8B8B8", opacity: 0.3, blur: 3, offset: 1, angle: 90 });

const TY = {
  title:      { fontFace: FONT, fontSize: 22, bold: true,  color: THEME.accent,      align: "left"   },
  sectionLbl: { fontFace: FONT, fontSize: 10, bold: true,  color: THEME.accent,      align: "left"   },
  body:       { fontFace: FONT, fontSize: 10, bold: false, color: THEME.darkGray, align: "left"   },
  bodySmall:  { fontFace: FONT, fontSize: 8.5, bold: false, color: THEME.midGray, align: "left"   },
  statNumber: { fontFace: FONT, fontSize: 30, bold: true,  color: THEME.accent,      align: "center" },
  statLabel:  { fontFace: FONT, fontSize: 9,  bold: false, color: THEME.midGray,  align: "center" },
  footer:     { fontFace: FONT, fontSize: 7,  bold: false, color: THEME.midGray,  align: "left"   },
};

const LAYOUT = {
  slideW: 10,
  slideH: 5.625,
  marginX: 0.5,
  contentTop: 0.95,
  contentBottom: 4.65, // content must end above the summary banner
  bannerY: 4.70,
  bannerH: 0.40,
};

// Fixed content-page shell markers. These prevent accidental duplicate chrome
// when a caller composes a page from several helpers.
const FOOTER_MARK = Symbol("deckFooter");
const SUMMARY_MARK = Symbol("deckSummaryBanner");
const SOURCE_MARK = Symbol("deckSourceNote");

// Design-density heuristics are intentionally opt-in. They are useful during
// exploration, but they are not template rules and must not steer every
// page back toward a full grid. Geometry, argument, and data validation remain
// enabled independently. Set PPT_ADVISORY_QA=1 to print these hints.
const ADVISORY_QA_ENABLED = process.env.PPT_ADVISORY_QA === "1";

const withHash = (color) => `#${color}`;

// Split the body width into n columns; returns [{x, w}, ...] — ALL n columns,
// there is no "get column i" form. weights (e.g. [2, 1]) make unequal
// columns. Use for multi-region slides:
//   const [left, mid, right] = cols(3);
//   addPanelList(pres, s, { ...left, ... }); addComparisonTable(pres, s, { ...mid, ... });
function cols(n, opts = {}) {
  // Real-world failure mode: cols(2, 0) / cols(2, 1), guessing the 2nd arg
  // selects a single column by index. It doesn't — the 2nd arg is an options
  // object, so a bare number is silently ignored by default-destructuring
  // (0..x is undefined, falls back to the default), and the caller ends up
  // assigning the WHOLE array to what they think is one column's x/w. That
  // produces geometrically nonsensical positions with no NOTICE anywhere —
  // the deck renders severely broken with zero diagnostic output. Fail loud.
  if (opts !== null && typeof opts !== "object") {
    throw new Error(
      `[CALL-ERROR in YOUR slides.js — NOT a library bug] cols(${n}, ${JSON.stringify(opts)}): the 2nd argument ` +
      "must be an options object like {x, w, gap, weights} (or omitted) — cols() has no \"get column i\" form, " +
      "it always returns ALL n columns. Destructure the result: `const [left, right] = cols(2);` then spread " +
      "one column into a component call: `addPanelList(pres, s, { ...left, y: 1.05, h: 3.6, ... })`."
    );
  }
  const { x = 0.5, w = 9.0, gap = 0.18, weights = null } = opts;
  const ws = weights || Array(n).fill(1);
  const total = ws.reduce((a, b) => a + b, 0);
  const avail = w - gap * (ws.length - 1);
  const out = [];
  let cx = x;
  ws.forEach((wt) => {
    const cw = (avail * wt) / total;
    out.push({ x: cx, w: cw });
    cx += cw + gap;
  });
  return out;
}



// Normalize the many "almost right" chart data shapes LLM callers produce into
// pptxgenjs's required [{ name, labels, values }] — and fail with instructions
// (not cryptic library errors) when the data is genuinely unusable.
function normalizeChartData(fn, data) {
  let series = data;
  if (series && !Array.isArray(series)) series = [series];            // single series object → array
  if (Array.isArray(series) && series.length &&
      series[0] && series[0].labels === undefined &&
      (series[0].label !== undefined || series[0].value !== undefined)) {
    // array of {label, value} points → one series
    series = [{
      name: "数据",
      labels: series.map((pt) => String(pt.label != null ? pt.label : (pt.name != null ? pt.name : ""))),
      values: series.map((pt) => pt.value),
    }];
  }
  if (!Array.isArray(series) || !series.length) {
    throw new Error(
      `[CALL-ERROR in YOUR slides.js — NOT a library bug] ${fn}: \`data\` must be ` +
      "an array of series: [{ name: \"系列名\", labels: [\"甲\",\"乙\"], values: [1, 2] }]. " +
      "Fix the data you pass in; do NOT edit components.js."
    );
  }
  return series.map((s, i) => {
    const name = String((s && s.name) || `系列${i + 1}`);
    const labels = ((s && s.labels) || []).map(String);
    const values = ((s && s.values) || []).map((v) =>
      typeof v === "string" ? parseFloat(v.replace(/[^0-9.eE+-]/g, "")) : v);
    if (!labels.length || !values.length) {
      throw new Error(
        `[CALL-ERROR in YOUR slides.js — NOT a library bug] ${fn}: series "${name}" ` +
        "needs non-empty labels[] AND values[]. Fix the data; do NOT edit components.js."
      );
    }
    if (labels.length !== values.length) {
      throw new Error(
        `[CALL-ERROR in YOUR slides.js — NOT a library bug] ${fn}: series "${name}" has ` +
        `${labels.length} labels but ${values.length} values — they must match 1:1. ` +
        "Fix the data; do NOT edit components.js."
      );
    }
    if (values.some((v) => typeof v !== "number" || Number.isNaN(v))) {
      throw new Error(
        `[CALL-ERROR in YOUR slides.js — NOT a library bug] ${fn}: series "${name}" ` +
        "values must all be numbers (no null/undefined/non-numeric strings). " +
        "Fix the data; do NOT edit components.js."
      );
    }
    return { name, labels, values };
  });
}


// Build-time overlap check: when a callout row / summary banner is added,
// scan elements already on the slide and name the ones that intrude into its
// band. Uses pptxgenjs private _slideObjects — wrapped in try/catch so a
// library upgrade degrades to "no check" instead of a crash.
function warnBandOverlap(fn, slide, bandY, limitY) {
  try {
    const objs = slide._slideObjects || [];
    const offenders = [];
    objs.forEach((o) => {
      const opt = o && o.options;
      if (!opt || typeof opt.y !== "number" || typeof opt.h !== "number") return;
      const bottom = opt.y + opt.h;
      if (opt.y < bandY && bottom > bandY + 0.02) {
        const what = (o._type || o.type || "element") + (opt._name ? `(${opt._name})` : "");
        offenders.push(`${what} y=${opt.y.toFixed(2)} h=${opt.h.toFixed(2)} bottom=${bottom.toFixed(2)}`);
      }
    });
    if (offenders.length) {
      console.warn(
        `[LAYOUT-OVERLAP NOTICE — ${fn} succeeded, but ${offenders.length} element(s) extend into its band (y=${bandY}) ` +
        `and will be visually covered] ${offenders.slice(0, 4).join("; ")}. ` +
        `Fix YOUR slides.js geometry: content must end at y<=${limitY} (LAYOUT.contentBottom). ` +
        "Do NOT edit components.js."
      );
    }
  } catch (e) { /* private API changed — skip the check */ }
}

// Effective bounding rect of a pptxgenjs slide object. Tables carry no `h`
// (row heights live in the rowH array) — derive it, or every table would be
// invisible to the overlap and page-coverage checks.
function objRect(o) {
  const t = o && o.options;
  if (!t || typeof t.x !== "number" || typeof t.y !== "number" || typeof t.w !== "number") return null;
  // pptxgenjs converts TABLE x/y/w to EMU in-place (914400/inch) while rowH
  // stays in inches — normalize any suspiciously large value back to inches.
  const IN = (v) => (v > 100 ? v / 914400 : v);
  let h = t.h;
  if (typeof h !== "number" && Array.isArray(t.rowH)) h = t.rowH.reduce((a, b) => a + (b || 0), 0);
  if (typeof h !== "number") return null;
  return { x: IN(t.x), y: IN(t.y), w: IN(t.w), h: IN(h), rotate: t.rotate };
}

// Region-collision check: before a content component draws, scan elements
// already on the slide and name any that intrude into its rectangle. Catches
// the "KPI row stamped on top of tall cards" class of bug at build time.
// Uses pptxgenjs private _slideObjects — wrapped in try/catch so a library
// upgrade degrades to "no check" instead of a crash. Advisory only.
function warnRectOverlap(fn, slide, rx, ry, rw, rh) {
  try {
    const TOL = 0.08; // require real intrusion on BOTH axes, not mere edge contact
    const offenders = [];
    (slide._slideObjects || []).forEach((o) => {
      const opt = objRect(o);
      if (!opt) return;
      if (opt.rotate) return; // rotated boxes report pre-rotation rects
      const ix = Math.min(rx + rw, opt.x + opt.w) - Math.max(rx, opt.x);
      const iy = Math.min(ry + rh, opt.y + opt.h) - Math.max(ry, opt.y);
      if (ix > TOL && iy > TOL) {
        const what = o._type || o.type || "element";
        offenders.push(`${what} @(x=${opt.x.toFixed(2)},y=${opt.y.toFixed(2)},w=${opt.w.toFixed(2)},h=${opt.h.toFixed(2)})`);
      }
    });
    if (offenders.length) {
      console.warn(
        `[LAYOUT-OVERLAP NOTICE — ${fn} succeeded, but it is being drawn ON TOP of ${offenders.length} existing element(s)] ` +
        `${fn} occupies (x=${rx.toFixed(2)}, y=${ry.toFixed(2)}, w=${rw.toFixed(2)}, h=${rh.toFixed(2)}); colliding: ` +
        offenders.slice(0, 4).join("; ") + ". " +
        "Fix YOUR slides.js geometry — vertically adjacent regions must satisfy prevY + prevH <= nextY " +
        "(e.g. shrink the earlier region's h, or move this component down). Do NOT edit components.js."
      );
    }
  } catch (e) { /* private API changed — skip the check */ }
}

// Explicit list-fitting font size. PowerPoint does NOT recompute pptxgenjs's
// fit:"shrink" until the text box is edited, so overflowing text renders
// outside its panel. Compute a fitting size up front; fit:"shrink" stays on
// only as a backstop.
function fitListFontSize(texts, w, h, base, min) {
  for (let size = base; size >= min; size -= 0.25) {
    const charW = (size / 72) * 1.05;
    const lineH = (size / 72) * 1.45;
    const perLine = Math.max(Math.floor(w / charW), 1);
    let lines = 0;
    texts.forEach((t) => { lines += Math.max(Math.ceil(String(t).length / perLine), 1); });
    const total = lines * lineH + texts.length * (4 / 72); // paraSpaceAfter 4pt
    if (total <= h) return size;
  }
  return min;
}

// Page-level coverage audit, run from addSummaryBanner (the mandatory last
// band on every content slide): split the content zone (x 0.5-9.5, y 1.0-4.65)
// into a 4x3 grid and flag clusters of cells no element touches — the
// "one narrow panel + huge blank right half" failure that per-region checks
// cannot see. Advisory only.
function warnPageCoverage(slide) {
  if (!ADVISORY_QA_ENABLED) return;
  try {
    const objs = (slide._slideObjects || []).map(objRect).filter(Boolean);
    if (objs.length < 3) return; // framing-only slide (opening/TOC/closing)
    const X0 = 0.5, X1 = 9.5, Y0 = 1.0, Y1 = 4.65, COLS = 4, ROWS = 3;
    const cw = (X1 - X0) / COLS, ch = (Y1 - Y0) / ROWS;
    const empty = [];
    for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) {
      const gx = X0 + c * cw, gy = Y0 + r * ch;
      const hit = objs.some((t) =>
        Math.min(gx + cw, t.x + t.w) - Math.max(gx, t.x) > 0.15 &&
        Math.min(gy + ch, t.y + t.h) - Math.max(gy, t.y) > 0.15);
      if (!hit) empty.push([r, c]);
    }
    // Only warn on a cluster: 2+ empty cells sharing an edge.
    const adj = empty.some(([r, c]) => empty.some(([r2, c2]) =>
      (r === r2 && Math.abs(c - c2) === 1) || (c === c2 && Math.abs(r - r2) === 1)));
    if (empty.length >= 2 && adj) {
      const colName = ["左", "中左", "中右", "右"], rowName = ["上", "中", "下"];
      const where = [...new Set(empty.map(([r, c]) => `${colName[c]}${rowName[r]}`))].slice(0, 6).join("、");
      console.warn(
        `[DESIGN-ADVISORY — page-level. ~${empty.length}/12 of the content zone is untouched (${where})] ` +
        "Standard content pages must fill the content zone. Expand the regions, extend the body text, " +
        "or rework the page plan so the whitespace is closed."
      );
    }
  } catch (e) { /* private API changed — skip */ }
}

// Underfill heuristic: rough CJK capacity (in characters) of a text container.
// Used by top-anchored content components (cards, icon grids) to nudge the
// caller to EXPAND content from research when a region would render mostly
// empty. Advisory only — never fatal, never blocks the build.
function estCapacityChars(w, h, fontSize) {
  const charW = (fontSize / 72) * 1.05;   // CJK glyph ≈ 1em wide
  const lineH = (fontSize / 72) * 1.55;   // line height + paragraph spacing
  const lines = Math.max(Math.floor(h / lineH), 1);
  const perLine = Math.max(Math.floor(w / charW), 1);
  return lines * perLine;
}

// Display-width char count: CJK ≈ 1em, ASCII/halfwidth ≈ 0.55em — matches the
// width model in estCapacityChars, so fill rates stay honest for mixed
// Chinese/English content (raw .length overstates ASCII-heavy text by ~2x).
function visLen(s) {
  let n = 0;
  for (const ch of String(s || "")) n += ch.charCodeAt(0) < 0x2e80 ? 0.55 : 1;
  return n;
}

function warnUnderfill(fn, what, chars, capacity, suggestion) {
  if (!ADVISORY_QA_ENABLED) return;
  if (capacity <= 0) return;
  const fill = chars / capacity;
  if (fill < 0.7) {
    console.warn(
      `[DESIGN-ADVISORY — ${fn}: ${what} uses ~${Math.round(fill * 100)}% of its estimated text capacity] ` +
      "Keep it when the whitespace is intentional. If the region feels accidental or unfinished, improve the " +
      `content, reduce the container, or choose another composition (${suggestion}). Never add filler.`
    );
  }
}

// Guard for content components, which all take (pres, slide, opts). The most
// common caller mistake is omitting `pres`, which shifts every argument and
// surfaces as confusing errors like "slide.addTable is not a function".
function assertSlide(fn, pres, slide) {
  if (!pres || typeof pres.addSlide !== "function") {
    throw new Error(
      `[CALL-ERROR in YOUR slides.js — NOT a library bug] ${fn}(pres, slide, opts): ` +
      "1st argument must be the pptxgen presentation instance (`pres`)."
    );
  }
  if (!slide || typeof slide.addText !== "function") {
    throw new Error(
      `[CALL-ERROR in YOUR slides.js — NOT a library bug] ${fn}(pres, slide, opts): ` +
      "2nd argument must be a slide from pres.addSlide(). You most likely omitted `pres` " +
      `as the 1st argument — call C.${fn}(pres, slide, {...}). Fix the call site; ` +
      "do NOT edit components.js."
    );
  }
}

// ---------------------------------------------------------------------------
// Generic building blocks
// ---------------------------------------------------------------------------

function addSlideTitle(pres, slide, { title, subtitle = null, fontSize = 26 } = {}) {
  assertSlide("addSlideTitle", pres, slide);
  // slide titles never carry sequence numbers.
  // Strip a leading "3." / "3、" / "03:" style prefix defensively and tell the caller.
  const stripped = String(title || "").replace(/^\s*\d{1,2}\s*[\.、．:：]\s*/, "");
  if (stripped !== String(title || "")) {
    console.warn(
      "[STYLE-NOTICE — addSlideTitle succeeded] Removed the sequence-number prefix from " +
      `"${title}" — Slide titles carry no numbers. Drop the prefix in YOUR slides.js; ` +
      "do NOT edit components.js."
    );
    title = stripped;
  }
  // Dynamic title sizing: the title must stay on ONE line inside the 9" box.
  // CJK glyphs count as 1 em, ASCII roughly half; shrink from the requested
  // size just enough to fit (floor 15pt), and tell the caller when the title
  // is so long it should be reworded instead.
  const effLen = [...String(title)].reduce((s, ch) => s + (ch.charCodeAt(0) > 0x2e7f ? 1 : 0.55), 0);
  const titleW = LAYOUT.slideW - LAYOUT.marginX * 2;
  const fitSize = Math.floor((titleW * 72) / (1.15 * Math.max(effLen, 1)) * 2) / 2;
  const usedSize = Math.max(Math.min(fontSize, fitSize), 15);
  if (usedSize < fontSize && effLen > 30) {
    console.warn(
      `[CONTENT-LIMIT NOTICE — addSlideTitle succeeded] the title is ~${Math.round(effLen)} chars and was ` +
      `auto-shrunk to ${usedSize}pt to stay on one line. Titles read best at ≤22 chars — consider rewording ` +
      "it in YOUR slides.js (move detail into the subtitle); do NOT edit components.js."
    );
  }
  slide.addText(title, {
    x: LAYOUT.marginX, y: 0.18, w: titleW, h: 0.50,
    fontFace: FONT, fontSize: usedSize, bold: true, color: THEME.accent, align: "left",
    margin: 0, valign: "middle", fit: "shrink",
  });
  // darkGray, not midGray: at 10.5pt the 898989 mid grey reads as disabled text
  // against the accent title and washes out on a projector. 595757 is the same
  // weight the hand-written lead-in lines use, so component subtitles and
  // hand-composed ones match.
  if (subtitle) {
    slide.addText(subtitle, {
      x: LAYOUT.marginX, y: 0.68, w: LAYOUT.slideW - LAYOUT.marginX * 2, h: 0.24,
      fontFace: FONT, fontSize: 10.5, color: THEME.darkGray, align: "left", margin: 0,
    });
  }
}

function addPanel(pres, slide, { x, y, w, h, fill, border = null } = {}) {
  assertSlide("addPanel", pres, slide);
  if (fill === undefined && ADVISORY_QA_ENABLED) {
    console.warn(
      "[DESIGN-ADVISORY — addPanel has no explicit fill and will use gray DDDDDD] " +
      "Confirm that a gray grouping container is intentional; pass fill explicitly to record the decision."
    );
  }
  const usedFill = fill === undefined ? THEME.paleGray : fill;
  slide.addShape(pres.shapes.RECTANGLE, {
    x, y, w, h,
    fill: { color: usedFill },
    line: border ? { color: border, width: 0.5 } : { color: usedFill, width: 0 },
  });
}

// Content-page footer: bare page number at the far left, and an optional
// classification line next to it. Geometry matches the 13.33"x7.5" template
// master scaled x0.75 to this 10"x5.625" canvas.
//
// `classification` is opt-in and defaults to nothing: a deck that does not need
// a confidentiality marking should not carry an empty one. Pass a string to
// draw it (e.g. "Internal Use Only").
function addSlideFooter(pres, slide, { pageNum = 1, classification = null } = {}) {
  assertSlide("addSlideFooter", pres, slide);
  if (slide[FOOTER_MARK]) {
    throw new Error("[CALL-ERROR] addSlideFooter may be applied only once per content slide.");
  }
  slide[FOOTER_MARK] = true;
  slide.addText(String(pageNum), {
    x: 0.60, y: 5.21, w: 0.42, h: 0.20,
    fontFace: FONT, fontSize: 7, color: THEME.black, align: "left", valign: "middle", margin: 0,
  });
  const marking = String(classification || "").trim();
  if (marking) {
    slide.addText(marking, {
      x: 0.90, y: 5.21, w: 3.2, h: 0.20,
      fontFace: FONT, fontSize: 7, color: THEME.darkGray, align: "left", valign: "middle", margin: 0,
    });
  }
}

// Attribution line for pages that paste a paper figure. Required whenever the
// page embeds an image from assets/papers/ -- audit_pptx.py --evidence-plan
// turns a missing note into a build error.
//
// Sits on the footer row itself, right-aligned against x=8.30 so a longer
// citation grows leftwards into the empty right end of the row instead of
// colliding with the optional classification text that ends at x=4.10. The
// same band is empty under footerMode "master", where the template master draws
// its footer at y=6.95" on the 13.34" canvas -- which is this y=5.21 once
// merge_slides.py scales the page up.
//
// 7pt matches the rest of the footer and equals audit's absolute font floor,
// so it never trips the minimum-size check.
// Pass `url` (the evidence plan's source_url) to make the citation clickable.
// The "来源：" label stays plain and only the citation carries the link, so the
// underline marks exactly the clickable span. Colour is pinned to the footer
// grey rather than the theme's hyperlink blue: this is footer chrome, and a
// blue link would outrank the black confidentiality line next to it.
function addSourceNote(pres, slide, { source, url } = {}) {
  assertSlide("addSourceNote", pres, slide);
  const cite = String(source || "").trim();
  if (!cite) {
    throw new Error("[CALL-ERROR] addSourceNote requires a non-empty source, e.g. \"Wang et al., arXiv:2406.04692, Fig.1\".");
  }
  if (slide[SOURCE_MARK]) {
    throw new Error("[CALL-ERROR] addSourceNote may be applied only once per page. Combine multiple figures into one citation.");
  }
  const link = String(url || "").trim();
  if (link && !/^https?:\/\//i.test(link)) {
    throw new Error(`[CALL-ERROR] addSourceNote url must be an absolute http(s) URL, got "${link}".`);
  }
  slide[SOURCE_MARK] = true;

  const label = /^来源\s*[:：]/.test(cite) ? "" : "来源：";
  const body = label ? cite : cite.replace(/^来源\s*[:：]\s*/, "");
  const box = {
    x: 4.20, y: 5.21, w: 4.10, h: 0.20,
    fontFace: FONT, fontSize: 7, color: THEME.midGray,
    align: "right", valign: "middle", margin: 0, fit: "shrink",
  };

  if (!link) {
    slide.addText(`${label}${body}`, box);
    return;
  }
  slide.addText(
    [
      { text: label || "来源：", options: {} },
      { text: body, options: { hyperlink: { url: link, tooltip: body }, underline: true, color: THEME.midGray } },
    ],
    box
  );
}

// Required content-page takeaway band. Geometry, color, typography and
// alignment are fixed; callers supply only the page-specific one-sentence
// conclusion. Cover, TOC, section divider and closing pages use their own shell.
function addSummaryBanner(pres, slide, { text } = {}) {
  assertSlide("addSummaryBanner", pres, slide);
  const summary = String(text || "").trim();
  if (!summary) {
    throw new Error("[CALL-ERROR] addSummaryBanner requires a non-empty one-sentence takeaway.");
  }
  if (slide[SUMMARY_MARK]) {
    throw new Error("[CALL-ERROR] addSummaryBanner may be applied only once per content slide.");
  }
  slide[SUMMARY_MARK] = true;
  warnBandOverlap("addSummaryBanner", slide, LAYOUT.bannerY, LAYOUT.contentBottom);
  warnPageCoverage(slide);
  const y = LAYOUT.bannerY, h = LAYOUT.bannerH;
  slide.addShape(pres.shapes.RECTANGLE, {
    x: 0.3, y, w: 9.4, h,
    fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
  });
  slide.addText(summary, {
    x: 0.6, y, w: 8.8, h,
    fontFace: FONT, fontSize: 11, bold: true, color: THEME.white,
    align: "center", valign: "middle", margin: 0, fit: "shrink",
  });
}

// Preferred single entry point for the fixed bottom shell on standard content
// pages. It deliberately exposes no geometry or style overrides.
function addContentChrome(pres, slide, {
  summary,
  pageNum = 1,
  classification = null,
  footerMode = "drawn",
} = {}) {
  if (!new Set(["drawn", "master"]).has(footerMode)) {
    throw new Error('[CALL-ERROR] addContentChrome footerMode must be "drawn" or "master".');
  }
  addSummaryBanner(pres, slide, { text: summary });
  if (footerMode === "drawn") {
    addSlideFooter(pres, slide, { pageNum, classification });
  } else if (footerMode === "master") {
    if (slide[FOOTER_MARK]) {
      throw new Error("[CALL-ERROR] The content footer may be applied only once per content slide.");
    }
    // The generated slide will be relinked to the template's blank-content
    // layout by merge_slides.py. Mark the footer as accounted for without
    // drawing a second page number over the inherited master objects.
    slide[FOOTER_MARK] = true;
  }
}

// ---------------------------------------------------------------------------
// Framing slides (opening / TOC / section divider / closing)
// ---------------------------------------------------------------------------

// Cover page, drawn entirely with vectors — no background art, no logo.
// Geometry mirrors the cover in references/template.pptx (that canvas is
// 13.333" wide, this one 10", so every value there is this one x0.75), which
// keeps a deck built through the template path visually identical to one built
// purely in PptxGenJS.
//
// `classification` is opt-in: omit it and no marking is drawn.
function addOpeningSlide(pres, {
  title, department = "", author = "", date = "", classification = null,
} = {}) {
  const slide = pres.addSlide();
  slide.background = { color: THEME.white };
  slide.addText(title, {
    x: 0.83, y: 0.75, w: 6.42, h: 0.56,
    fontFace: FONT, fontSize: 22, bold: true, color: THEME.accent,
    align: "left", valign: "middle", margin: 0, fit: "shrink",
  });
  // Accent rule under the title — the cover's only ornament.
  slide.addShape(pres.shapes.RECTANGLE, {
    x: 0.83, y: 1.43, w: 0.59, h: 0.017,
    fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
  });
  slide.addText(
    [
      { text: `部门：${department}`, options: { breakLine: true } },
      { text: `汇报人：${author}`, options: { breakLine: true } },
      { text: `日期：${date}` },
    ],
    {
      x: 0.83, y: 1.65, w: 2.6, h: 0.75,
      fontFace: FONT, fontSize: 10.5, color: THEME.darkText,
      align: "left", margin: 0, lineSpacingMultiple: 1.25,
    }
  );
  const marking = String(classification || "").trim();
  if (marking) {
    slide.addText(marking, {
      x: 0.83, y: 5.12, w: 2.6, h: 0.23,
      fontFace: FONT, fontSize: 8, color: THEME.darkText,
      align: "left", valign: "middle", margin: 0,
    });
  }
  return slide;
}

function addTocSlide(pres, { title, sections = [] } = {}) {
  const slide = pres.addSlide();
  slide.background = { color: THEME.paleGray };
  slide.addText(title, {
    x: 0.85, y: 0.30, w: 4.0, h: 0.50,
    fontFace: FONT, fontSize: 22, bold: true, color: THEME.black, align: "left", margin: 0,
  });
  slide.addShape(pres.shapes.RECTANGLE, {
    x: 0.85, y: 0.86, w: 0.78, h: 0.05,
    fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
  });
  // 16pt section titles, no description line, whole list centered on the
  // slide (both axes). Uniform series colors: every badge the same accent, every
  // title black — no "current section" highlighting. Adaptive step keeps up
  // to 7 sections inside the canvas — never overflow.
  if (!Array.isArray(sections)) {
    throw new Error("addTocSlide sections must be an array of {num, title} objects or numbered title strings");
  }
  const normalizedSections = sections.map((section, index) => {
    if (typeof section === "string") {
      const text = section.trim();
      if (!text) throw new Error(`addTocSlide sections[${index}] is empty`);
      const match = text.match(/^\s*(\d{1,2})\s*[.、．:\-]?\s*(.+)$/);
      return match
        ? { num: match[1], title: match[2].trim() }
        : { num: String(index + 1), title: text };
    }
    if (!section || typeof section !== "object") {
      throw new Error(`addTocSlide sections[${index}] must be an object or string`);
    }
    const num = String(section.num == null ? index + 1 : section.num).trim();
    const sectionTitle = String(section.title == null ? "" : section.title).trim();
    if (!num || !sectionTitle) {
      throw new Error(`addTocSlide sections[${index}] requires non-empty num and title`);
    }
    return { num, title: sectionTitle };
  });
  const n = Math.max(normalizedSections.length, 1);
  if (normalizedSections.length > 7) {
    console.warn(
      "[CONTENT-LIMIT NOTICE — addTocSlide succeeded] TOC has " + normalizedSections.length +
      " sections; the TOC fits at most 7. Merge sections in YOUR slides.js — " +
      "do NOT edit components.js. Rendering the first 7 only."
    );
  }
  const shown = normalizedSections.slice(0, 7);
  const ZONE_TOP = 1.05, ZONE_BOTTOM = 5.35;
  const BADGE = 0.42;
  const STEP = Math.min(0.92, (ZONE_BOTTOM - ZONE_TOP) / shown.length);
  const blockH = STEP * (shown.length - 1) + BADGE;
  const top = ZONE_TOP + Math.max(0, (ZONE_BOTTOM - ZONE_TOP - blockH) / 2);
  // Horizontally centered group: badge column + 16pt title column.
  const BADGE_X = 3.30, TEXT_X = BADGE_X + BADGE + 0.22, TEXT_W = 3.30;
  shown.forEach((s, i) => {
    const y = top + i * STEP;
    slide.addShape(pres.shapes.RECTANGLE, {
      x: BADGE_X, y, w: BADGE, h: BADGE,
      fill: { color: THEME.accent },
      line: { color: THEME.accent, width: 0 },
    });
    slide.addText(s.num, {
      x: BADGE_X, y, w: BADGE, h: BADGE,
      fontFace: FONT, fontSize: 13, bold: true, color: THEME.white,
      align: "center", valign: "middle", margin: 0,
    });
    slide.addText(s.title, {
      x: TEXT_X, y, w: TEXT_W, h: BADGE,
      fontFace: FONT, fontSize: 16, bold: true,
      color: THEME.black, align: "left", valign: "middle", margin: 0, fit: "shrink",
    });
  });
  return slide;
}

// Closing slide. Matches the closing page in references/template.pptx.
function addClosingSlide(pres, { text = "Thank You" } = {}) {
  const slide = pres.addSlide();
  slide.background = { color: THEME.white };
  slide.addText(text, {
    x: 0, y: 2.40, w: 10, h: 0.75,
    fontFace: FONT, fontSize: 27, bold: true, color: THEME.accent,
    align: "center", valign: "middle", margin: 0,
  });
  return slide;
}

// ---------------------------------------------------------------------------
// Dense content layouts
// ---------------------------------------------------------------------------

// Row of big accent numbers with gray labels. Up to 5 items.
// rings: true draws a pale circular ring behind each number — the circular
// motif from references/product-slide.png (per-item override: it.ring).
function addKpiRow(pres, slide, { items = [], x: x0 = 0.5, w: w0 = 9.0, y = 3.55, rings = false } = {}) {
  assertSlide("addKpiRow", pres, slide);
  // Optional per-item icon (it.iconData / it.initial + it.color) stacks a small
  // disc above the number; the band grows accordingly.
  const hasIcons = items.some((it) => it.iconData || it.initial);
  const iconH = hasIcons ? 0.34 : 0;
  warnRectOverlap("addKpiRow", slide, x0, y, w0, 0.87 + iconH);
  const n = Math.max(items.length, 1);
  const cellW = w0 / n;
  items.forEach((it, i) => {
    const x = x0 + i * cellW;
    if (hasIcons && (it.iconData || it.initial)) {
      const D = 0.28, dx = x + cellW / 2 - D / 2;
      if (it.iconData) {
        slide.addImage({ data: it.iconData, x: dx, y, w: D, h: D });
      } else {
        slide.addShape(pres.shapes.OVAL, {
          x: dx, y, w: D, h: D,
          fill: { color: it.color || THEME.accent }, line: { color: it.color || THEME.accent, width: 0 },
        });
        slide.addText(String(it.initial).slice(0, 1), {
          x: dx, y, w: D, h: D,
          fontFace: FONT, fontSize: 10, bold: true, color: THEME.white,
          align: "center", valign: "middle", margin: 0,
        });
      }
    }
    if (it.ring !== undefined ? it.ring : rings) {
      const RING = 0.8;
      // Concentric with the number: text box is y..y+0.55 (valign middle), centered in the cell.
      slide.addShape(pres.shapes.OVAL, {
        x: x + cellW / 2 - RING / 2, y: y + iconH + 0.275 - RING / 2, w: RING, h: RING,
        fill: { color: THEME.white, transparency: 100 },
        line: { color: THEME.paleGray, width: 1.75 },
      });
    }
    slide.addText(String(it.value), {
      x, y: y + iconH, w: cellW, h: 0.55,
      fontFace: FONT, fontSize: 30, bold: true, color: THEME.accent,
      align: "center", valign: "middle", margin: 0,
      fit: "shrink",  // long values (e.g. "Apache 2.0") shrink to one line instead of wrapping onto the label
    });
    slide.addText(String(it.label), {
      x, y: y + iconH + 0.57, w: cellW, h: 0.30,
      fontFace: FONT, fontSize: 9, color: THEME.midGray,
      align: "center", valign: "top", margin: 0,
    });
    if (i > 0) {
      slide.addShape(pres.shapes.LINE, {
        x, y: y + iconH + 0.06, w: 0, h: 0.72,
        line: { color: THEME.paleGray, width: 0.75 },
      });
    }
  });
}

// Grid of pale-gray cards, each with a accent sub-headline, optional body, optional accent-bulleted list.
// Optional per-card small icon (card.iconData from iconToPng()/svgToPng(), or
// card.initial as a one-char fallback) renders in a fixed slot left of the
// title — flat monochrome per the deck style (accent or dark gray), never colorful.
function addCardGrid(pres, slide, {
  cards = [], columns = 2, x = 0.5, y = 1.05, w = 9.0, h = 3.5,
} = {}) {
  assertSlide("addCardGrid", pres, slide);
  warnRectOverlap("addCardGrid", slide, x, y, w, h);
  const gap = 0.18;
  const rows = Math.ceil(cards.length / columns);
  const cardW = (w - gap * (columns - 1)) / columns;
  const cardH = (h - gap * (rows - 1)) / rows;
  cards.forEach((card, i) => {
    const cx = x + (i % columns) * (cardW + gap);
    const cy = y + Math.floor(i / columns) * (cardH + gap);
    addPanel(pres, slide, { fill: THEME.paleGray, x: cx, y: cy, w: cardW, h: cardH });
    const hasIcon = !!(card.iconData || card.initial);
    if (hasIcon) {
      const D = 0.26;
      if (card.iconData) {
        slide.addImage({ data: card.iconData, x: cx + 0.12, y: cy + 0.11, w: D, h: D });
      } else {
        slide.addShape(pres.shapes.OVAL, {
          x: cx + 0.12, y: cy + 0.11, w: D, h: D,
          fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
        });
        slide.addText(String(card.initial).slice(0, 1), {
          x: cx + 0.12, y: cy + 0.11, w: D, h: D,
          fontFace: FONT, fontSize: 10, bold: true, color: THEME.white,
          align: "center", valign: "middle", margin: 0,
        });
      }
    }
    // Auto-fill: scale font size UP when this card's content is sparse
    // relative to its capacity (same sqrt-of-inverse-fill formula as
    // addTextBlock), so a card with modest content still reads full instead
    // of leaving visible dead space at a fixed font size.
    const chars = visLen(card.body) +
      (card.bullets || []).reduce((s2, b) => s2 + visLen(b) + 4, 0);
    const cardCapacity = estCapacityChars(cardW - 0.28, cardH - 0.56, 9);
    const cardFill = chars / Math.max(cardCapacity, 1);
    const cardScale = cardFill < 1 ? Math.min(1.35, Math.max(1, Math.sqrt(0.8 / Math.max(cardFill, 0.14)))) : 1;

    slide.addText(card.title, {
      x: cx + (hasIcon ? 0.46 : 0.14), y: cy + 0.10, w: cardW - (hasIcon ? 0.60 : 0.28), h: 0.30,
      fontFace: FONT, fontSize: 11.5 * Math.min(cardScale, 1.2), bold: true, color: THEME.accent,
      align: "left", valign: "middle", margin: 0, fit: "shrink",
    });
    const runs = [];
    if (card.body) runs.push({ text: card.body, options: { breakLine: true } });
    (card.bullets || []).forEach((b) => {
      runs.push({ text: b, options: { bullet: { code: "2022", color: THEME.accent }, breakLine: true, indentLevel: 0 } });
    });
    if (runs.length) {
      slide.addText(runs, {
        x: cx + 0.14, y: cy + 0.44, w: cardW - 0.28, h: cardH - 0.56,
        fontFace: FONT, fontSize: 9 * cardScale, color: THEME.darkGray,
        align: "left", valign: "top", margin: 0, paraSpaceAfter: 4 * cardScale, fit: "shrink",
      });
    }
    if (cardFill < 0.16) {
      warnUnderfill("addCardGrid", `card "${String(card.title).slice(0, 12)}"`,
        chars, cardCapacity,
        "grow the body to 2-3 sentences and give it 3-4 bullets with concrete mechanisms/numbers/examples, " +
        "or make the card region shorter / raise fontSize via more columns");
    }
  });
}

// Horizontal accent-axis timeline with 3-6 dated nodes.
function addTimeline(pres, slide, { events = [], x = 0.5, w = 9.0, axisY = 2.7 } = {}) {
  assertSlide("addTimeline", pres, slide);
  warnRectOverlap("addTimeline", slide, x, axisY - 0.42, w, 1.69);
  events.forEach((ev) => {
    if (String(ev.desc || "").length > 28) {
      console.warn(
        `[CONTENT-LIMIT NOTICE — addTimeline succeeded] node "${String(ev.title).slice(0, 10)}" has a ` +
        `${String(ev.desc).length}-char desc; node descs longer than ~25 chars bleed into neighbouring columns. ` +
        "Compress the wording in YOUR slides.js (电报体) — do NOT edit components.js."
      );
    }
  });
  if (events.length) {
    const avgDesc = events.reduce((s2, ev) => s2 + visLen(ev.desc), 0) / events.length;
    if (ADVISORY_QA_ENABLED && avgDesc < 12) {
      console.warn(
        `[DESIGN-ADVISORY — addTimeline: node descriptions average ~${Math.round(avgDesc)} chars] ` +
        "Confirm that the sparse labels are intentional; otherwise add factual context or choose a more compact timeline."
      );
    }
  }
  slide.addShape(pres.shapes.LINE, {
    x: x + 0.2, y: axisY, w: w - 0.4, h: 0,
    line: { color: THEME.accent, width: 1.5 },
  });
  const n = events.length;
  const cellW = Math.min((w - 0.8) / Math.max(n - 1, 1), 1.9);
  // Inset edge nodes so their centered label boxes stay inside [x, x+w].
  const inset = Math.max(0.4, cellW / 2);
  const span = w - inset * 2;
  events.forEach((ev, i) => {
    const cx = n === 1 ? x + w / 2 : x + inset + (span / (n - 1)) * i;
    slide.addShape(pres.shapes.OVAL, {
      x: cx - 0.06, y: axisY - 0.06, w: 0.12, h: 0.12,
      fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
    });
    slide.addText(String(ev.date), {
      x: cx - cellW / 2, y: axisY - 0.42, w: cellW, h: 0.24,
      fontFace: FONT, fontSize: 9, bold: true, color: THEME.accent,
      align: "center", margin: 0,
    });
    slide.addText(String(ev.title), {
      x: cx - cellW / 2, y: axisY + 0.14, w: cellW, h: 0.26,
      fontFace: FONT, fontSize: 9, bold: true, color: THEME.black,
      align: "center", margin: 0,
    });
    slide.addText(String(ev.desc || ""), {
      x: cx - cellW / 2, y: axisY + 0.42, w: cellW, h: 0.85,
      fontFace: FONT, fontSize: 7.5, color: THEME.midGray, fit: "shrink",
      align: "center", valign: "top", margin: 0,
    });
  });
}

// Bordered white panel with a accent header and a numbered / check / bullet list.
// This is the side-column workhorse: place 2-3 of these (or mix with a table,
// chart, or card region via cols()) to build a dense multi-region slide.
// items: strings or { title?, text }.
// Optional header icon (opts.iconData from iconToPng()/svgToPng()) replaces
// the accent accent bar next to the title — flat monochrome per the deck style.
function addPanelList(pres, slide, {
  x = 0.5, y = 1.05, w = 2.94, h = 3.5,
  title, items = [], style = "numbered", // "numbered" | "check" | "bullet"
  fontSize = 9, iconData = null,
} = {}) {
  assertSlide("addPanelList", pres, slide);
  warnRectOverlap("addPanelList", slide, x, y, w, h);
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x, y, w, h, rectRadius: 0.05,
    fill: { color: THEME.white }, line: { color: "D8D8D8", width: 0.75 },
    shadow: cardShadow(),
  });
  if (iconData) {
    slide.addImage({ data: iconData, x: x + 0.12, y: y + 0.10, w: 0.26, h: 0.26 });
  } else {
    slide.addShape(pres.shapes.RECTANGLE, {
      x: x + 0.14, y: y + 0.13, w: 0.05, h: 0.20,
      fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
    });
  }
  slide.addText(String(title || ""), {
    x: x + (iconData ? 0.46 : 0.27), y: y + 0.08, w: w - (iconData ? 0.60 : 0.41), h: 0.30,
    fontFace: FONT, fontSize: 11, bold: true, color: THEME.accent,
    align: "left", valign: "middle", margin: 0, fit: "shrink",
  });
  slide.addShape(pres.shapes.LINE, {
    x: x + 0.14, y: y + 0.46, w: w - 0.28, h: 0,
    line: { color: THEME.paleGray, width: 0.75 },
  });
  const top = y + 0.58;
  const n = Math.max(items.length, 1);
  const rowH = (h - 0.72) / n;
  if (rowH < 0.32) {
    console.warn(
      `[CONTENT-LIMIT NOTICE — addPanelList succeeded, but panel "${String(title).slice(0, 12)}" packs ` +
      `${n} items into h=${h}" (row height ${rowH.toFixed(2)}") — text will shrink below readable size. ` +
      "In YOUR slides.js cut to ≤" + Math.max(Math.floor((h - 0.72) / 0.34), 1) + " items or grow the region; " +
      "do NOT edit components.js."
    );
  }
  const panelChars = items.reduce((s2, it) => {
    const o = typeof it === "string" ? { text: it } : it;
    return s2 + visLen(o.title) + visLen(o.text);
  }, 0);
  const panelCapacity = estCapacityChars(w - 0.6, h - 0.72, fontSize);
  const panelFill = panelChars / Math.max(panelCapacity, 1);
  // Auto-fill: scale the caller's fontSize UP when items are sparse relative
  // to the panel's capacity (same formula as addTextBlock/addCardGrid) —
  // `fontSize` stays the floor a caller can still raise explicitly.
  const panelScale = panelFill < 1 ? Math.min(1.35, Math.max(1, Math.sqrt(0.8 / Math.max(panelFill, 0.14)))) : 1;
  if (panelFill < 0.16) {
    warnUnderfill("addPanelList", `panel "${String(title).slice(0, 12)}"`,
      panelChars, panelCapacity,
      "make each item a 35-50-char full sentence (fewer items → write each one longer; the only ceiling is fitting " +
      "the row at default font) and use 5-6 items on tall panels — concrete facts, not labels");
  }
  items.forEach((raw, i) => {
    const it = typeof raw === "string" ? { text: raw } : raw;
    const iy = top + i * rowH;
    const D = 0.22 * Math.min(panelScale, 1.2);
    if (style === "numbered") {
      slide.addShape(pres.shapes.OVAL, {
        x: x + 0.14, y: iy + 0.01, w: D, h: D,
        fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
      });
      slide.addText(String(i + 1), {
        x: x + 0.14, y: iy + 0.01, w: D, h: D,
        fontFace: FONT, fontSize: 9 * Math.min(panelScale, 1.2), bold: true, color: THEME.white,
        align: "center", valign: "middle", margin: 0,
      });
    } else if (style === "check") {
      slide.addShape(pres.shapes.OVAL, {
        x: x + 0.14, y: iy + 0.01, w: D, h: D,
        fill: { color: THEME.white }, line: { color: THEME.accent, width: 1.25 },
      });
      slide.addText("✓", {
        x: x + 0.14, y: iy, w: D, h: D,
        fontFace: FONT, fontSize: 10 * Math.min(panelScale, 1.2), bold: true, color: THEME.accent,
        align: "center", valign: "middle", margin: 0,
      });
    } else {
      slide.addShape(pres.shapes.OVAL, {
        x: x + 0.19, y: iy + 0.07, w: 0.08, h: 0.08,
        fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
      });
    }
    const runs = [];
    if (it.title) runs.push({ text: it.title, options: { bold: true, color: THEME.black, breakLine: !!it.text } });
    if (it.text) runs.push({ text: it.text, options: { color: THEME.darkGray } });
    slide.addText(runs, {
      x: x + 0.46, y: iy, w: w - 0.60, h: rowH - 0.02,
      fontFace: FONT, fontSize: fontSize * panelScale, align: "left", valign: "top", margin: 0, fit: "shrink",
    });
    if (i < items.length - 1) {
      slide.addShape(pres.shapes.LINE, {
        x: x + 0.46, y: top + (i + 1) * rowH - 0.06, w: w - 0.60, h: 0,
        line: { color: THEME.paleGray, width: 0.5 },
      });
    }
  });
}

// Editorial table: accent header band, horizontal hairlines only — no vertical
// grid, no outer frame. Spreadsheet-style full grids read as Excel paste-ins.
function addComparisonTable(pres, slide, {
  headers = [], rows = [], x = 0.5, y = 1.05, w = 9.0, colW = null, fontSize = 9, h = null,
  rowIcons = null, // per-row icon: dataURI string or { initial, color } — needs h (row heights must be known)
} = {}) {
  assertSlide("addComparisonTable", pres, slide);
  if (h) warnRectOverlap("addComparisonTable", slide, x, y, w, h);
  if (rowIcons && !h) {
    console.warn(
      "[CONTENT-LIMIT NOTICE — addComparisonTable succeeded, but rowIcons were skipped] row icons need " +
      "known row heights — pass `h` (the region height) to this call in YOUR slides.js."
    );
    rowIcons = null;
  }
  const NO_BORDER = { type: "none" };
  const HAIRLINE = { pt: 0.5, color: "E5E5E5" };
  const headerRow = headers.map((hd) => ({
    text: String(hd),
    options: {
      bold: true, color: THEME.white, fill: { color: THEME.accent }, align: "center", valign: "middle",
      border: [NO_BORDER, NO_BORDER, NO_BORDER, NO_BORDER],
    },
  }));
  const bodyRows = rows.map((r) => r.map((c, ci) => ({
    text: String(c && typeof c === "object" && c.text !== undefined ? c.text : c),
    options: {
      color: THEME.darkGray, fill: { color: THEME.white }, align: "left", valign: "middle",
      border: [NO_BORDER, NO_BORDER, HAIRLINE, NO_BORDER],
      margin: rowIcons && ci === 0 ? [0.03, 0.05, 0.03, 0.4] : undefined,
    },
  })));
  // With h set, stretch rows evenly to fill the region (header slightly shorter)
  // so the table doesn't collapse into a compact strip inside a tall zone.
  // pptxgenjs tables have NO autofit: text longer than its cell silently
  // overflows the table region (and bleeds under callouts/banners). So when h
  // is known, estimate the worst cell's line count and shrink the font until
  // everything fits its row. CJK chars are ≈ fontSize pt wide — conservative
  // for Latin, which is fine.
  let rowH;
  let fs = fontSize;
  if (h) {
    const nRows = Math.max(rows.length, 1);
    const headerH = Math.min(0.34, h / (nRows + 1));
    const availPerRow = (h - headerH) / nRows;
    const nCols = Math.max(headers.length, 1);
    const widths = Array.isArray(colW) ? colW
      : (typeof colW === "number" && colW > 0) ? Array(nCols).fill(colW)
      : Array(nCols).fill(w / nCols);
    const cellText = (c) => String(c && typeof c === "object" && c.text !== undefined ? c.text : c);
    let fitted = false;
    for (;;) {
      const lineH = (fs * 1.45) / 72;
      fitted = rows.every((r) => r.every((c, ci) => {
        const cw = (widths[ci] || widths[0] || w / nCols) - 0.12;
        const charsPerLine = Math.max(Math.floor(cw / (fs / 72)), 1);
        const lines = Math.ceil(cellText(c).length / charsPerLine) || 1;
        return lines * lineH <= availPerRow - 0.05;
      }));
      if (fitted || fs <= 7) break;
      fs = Math.max(fs - 0.5, 7);
    }
    if (!fitted) {
      console.warn(
        "[CONTENT-LIMIT NOTICE — NOT an API error. The addComparisonTable call succeeded and the deck was generated.] " +
        "Some table cells hold more text than fits a row even at 7pt. " +
        "Fix the page content or geometry: shorten each cell to ≤2 lines (~30 CJK chars), widen/reflow the table, " +
        "or split it across two slides. 这是内容超限提示，不是组件或调用错误。"
      );
    }
    // Underfill: a tall region with too few rows reads as fat empty stripes no
    // matter how long the cells are — row count is checked before cell fill.
    if (ADVISORY_QA_ENABLED && h >= 1.4 && rows.length < 4) {
      console.warn(
        `[DESIGN-ADVISORY — addComparisonTable: ${rows.length} rows are stretched over h=${h}"] ` +
        "Confirm the generous row height is intentional; otherwise reduce the table height or choose another composition."
      );
    }
    // Underfill: rows are stretched to fill h, so thin label-like cells read as
    // empty stripes. Estimate each row's tallest cell text height (display
    // width: CJK 1em / ASCII 0.55em) against the stretched row height.
    if (fitted && rows.length) {
      const lineH = (fs * 1.45) / 72;
      const fills = rows.map((r) => {
        const need = Math.max(...r.map((c, ci) => {
          const cw = (widths[ci] || widths[0] || w / nCols) - 0.12;
          const charsPerLine = Math.max(cw / (fs / 72), 1);
          return (Math.ceil(visLen(cellText(c)) / charsPerLine) || 1) * lineH;
        }));
        return Math.min(need / availPerRow, 1);
      });
      const fill = fills.reduce((a, b) => a + b, 0) / fills.length;
      if (ADVISORY_QA_ENABLED && fill < 0.4) {
        console.warn(
          `[DESIGN-ADVISORY — addComparisonTable: rows use ~${Math.round(fill * 100)}% of their estimated text height] ` +
          "Keep the spacing when it improves scanning; otherwise reduce the region or use a different comparison treatment."
        );
      }
    }
    rowH = [headerH, ...rows.map(() => availPerRow)];
  }
  slide.addTable([headerRow, ...bodyRows], {
    x, y, w, colW: colW || undefined, rowH,
    fontFace: FONT, fontSize: fs, margin: 0.06, autoPage: false,
  });
  // Row-head icons (tier-2 enhancement): drawn inside the first column's left
  // padding; positions derive from the computed row heights.
  if (rowIcons && rowH) {
    let ry = y + rowH[0];
    rows.forEach((r, i) => {
      const ic = rowIcons[i];
      const rh = rowH[i + 1];
      if (ic) {
        const D = Math.min(0.24, rh - 0.08);
        const iy = ry + (rh - D) / 2;
        if (typeof ic === "string") {
          slide.addImage({ data: ic.replace(/^data:/, ""), x: x + 0.08, y: iy, w: D, h: D });
        } else {
          slide.addShape(pres.shapes.OVAL, {
            x: x + 0.08, y: iy, w: D, h: D,
            fill: { color: ic.color || THEME.accent }, line: { color: ic.color || THEME.accent, width: 0 },
          });
          slide.addText(String(ic.initial || "").slice(0, 1), {
            x: x + 0.08, y: iy, w: D, h: D,
            fontFace: FONT, fontSize: 8.5, bold: true, color: THEME.white,
            align: "center", valign: "middle", margin: 0,
          });
        }
      }
      ry += rh;
    });
  }
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------

// Themed native chart: accent primary series, gray comparison series,
// value-axis-only grid, flat white plot area, data labels on. Any extra option
// is passed through to pptxgenjs addChart and overrides the styled defaults —
// use it for per-page tuning (barGapWidthPct, valAxisMinVal/MaxVal, label
// sizes …) instead of dropping to slide.addChart and losing the house style.
function addDeckChart(pres, slide, {
  type, data, x = 0.5, y = 1.0, w = 5.4, h = 3.4, showLegend = null, ...extra
} = {}) {
  assertSlide("addDeckChart", pres, slide);
  const typeMap = {
    bar: pres.charts.BAR, line: pres.charts.LINE,
    pie: pres.charts.PIE, doughnut: pres.charts.DOUGHNUT,
  };
  const chartType = typeMap[type];
  if (!chartType) {
    throw new Error(
      `[CALL-ERROR in YOUR slides.js — NOT a library bug] addDeckChart: type "${type}" ` +
      'is not supported. Use one of: "bar", "line", "pie", "doughnut".'
    );
  }
  data = normalizeChartData("addDeckChart", data);
  const multiSeries = Array.isArray(data) && data.length > 1;
  const legend = showLegend === null ? multiSeries : showLegend;
  const opts = {
    x, y, w, h,
    chartColors: [THEME.accent, THEME.midGray, THEME.lightGray, THEME.darkGray, THEME.accentMid, THEME.paleGray],
    chartArea: { fill: { color: THEME.white }, roundedCorners: false },
    catAxisLabelColor: THEME.midGray, catAxisLabelFontSize: 8, catAxisLabelFontFace: FONT,
    catAxisLineColor: "C9C9C9",
    valAxisLabelColor: THEME.midGray, valAxisLabelFontSize: 8, valAxisLabelFontFace: FONT,
    valAxisLineShow: false,
    valGridLine: { color: "E8E8E8", size: 0.5 },
    catGridLine: { style: "none" },
    showLegend: legend, legendPos: "b", legendFontSize: 8, legendColor: THEME.darkGray,
    dataLabelColor: THEME.black, dataLabelFontSize: 8, dataLabelFontFace: FONT,
  };
  // Data-label precision follows the data: bare defaults round 58.8 → "59" and
  // 0.7412 → "1", which misreports researched numbers. Pick a format that
  // preserves the significant digits actually present. Pie/doughnut labels are
  // percentage fractions — they need a percent format, not the value format.
  if (type === "pie" || type === "doughnut") {
    opts.showPercent = true; opts.showLegend = true;
    opts.dataLabelFormatCode = "0%";
  } else {
    const allValues = data.flatMap((s) => s.values);
    opts.dataLabelFormatCode =
      allValues.every((v) => Number.isInteger(v)) ? "0" :
      allValues.every((v) => Math.abs(v) < 1) ? "0.00" : "0.0";
  }
  if (type === "bar") {
    opts.barDir = "col"; opts.showValue = true; opts.dataLabelPosition = "outEnd";
    // 140 keeps columns slim (gap ≈ 1.4× bar width) — the 60 default rendered
    // fat, loose bars. A renderer left to auto-scale starts the value axis just
    // below the data minimum, silently truncating the axis; anchor it at 0
    // whenever the data allows. Both are per-page overridable via `extra`.
    opts.barGapWidthPct = 140;
    const vals = data.flatMap((s) => s.values);
    if (vals.every((v) => v >= 0)) opts.valAxisMinVal = 0;
  }
  if (type === "line") { opts.lineSize = 1.5; opts.lineSmooth = false; opts.showValue = true; }
  Object.assign(opts, extra);
  slide.addChart(chartType, data, opts);
}

// Optional chart-plus-interpretation layout: chart on a white shadow card on
// the left, numbered insight card + caveat card stacked on the right. All
// three regions share the cardShadow() white-card shell — no gray boxes.
function addChartWithInsights(pres, slide, {
  chart, insights = [], caveats = [],
  insightsTitle = "关键信号", caveatsTitle = "注意事项",
} = {}) {
  assertSlide("addChartWithInsights", pres, slide);
  const cardShell = (cx2, cy2, cw2, ch2) => slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x: cx2, y: cy2, w: cw2, h: ch2, rectRadius: 0.05,
    fill: { color: THEME.white }, line: { color: "D8D8D8", width: 0.75 },
    shadow: cardShadow(),
  });
  const cardTitle = (cx2, cy2, cw2, text) => {
    slide.addShape(pres.shapes.RECTANGLE, {
      x: cx2 + 0.12, y: cy2 + 0.10, w: 0.05, h: 0.20,
      fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
    });
    slide.addText(String(text), {
      x: cx2 + 0.25, y: cy2 + 0.08, w: cw2 - 0.37, h: 0.26,
      fontFace: FONT, fontSize: 10.5, bold: true, color: THEME.accent, align: "left", margin: 0, fit: "shrink",
    });
  };
  cardShell(0.5, 1.00, 5.6, 3.55);
  addDeckChart(pres, slide, Object.assign({}, chart, { x: 0.65, y: 1.12, w: 5.3, h: 3.3 }));

  const cardX = 6.3, cardW = 3.2;
  const half = caveats.length ? 1.72 : 3.55;
  // PowerPoint does not recompute fit:"shrink" until the box is edited, so we
  // size the list text explicitly to its panel; shrink stays on as a backstop.
  const warnCardOverflow = (what, fitted, count) => {
    if (fitted <= 7 || count > 5) {
      console.warn(
        `[CONTENT-LIMIT NOTICE — addChartWithInsights succeeded] the ${what} card holds ` +
        `${count} items and needs ${fitted}pt to fit — text this dense reads badly and can spill outside the panel. ` +
        "In YOUR slides.js keep 4-5 items of ≤2 short lines each (电报体) — do NOT edit components.js."
      );
    }
  };
  if (insights.length) {
    cardShell(cardX, 1.00, cardW, half);
    cardTitle(cardX, 1.00, cardW, insightsTitle);
    const insFs = fitListFontSize(insights.map((t, i) => `${i + 1}. ${t}`), cardW - 0.24, half - 0.50, 8.5, 7);
    warnCardOverflow("insights", insFs, insights.length);
    warnUnderfill("addChartWithInsights", "the insights card",
      insights.reduce((s2, t) => s2 + visLen(t) + 3, 0),
      estCapacityChars(cardW - 0.24, half - 0.50, 8.5),
      "give 4-5 insights, each a ~20-30-char full sentence that interprets the chart, not a label");
    slide.addText(
      insights.map((t, i) => ({ text: `${i + 1}. ${t}`, options: { breakLine: true } })),
      {
        x: cardX + 0.12, y: 1.38, w: cardW - 0.24, h: half - 0.46,
        fontFace: FONT, fontSize: insFs, color: THEME.darkGray,
        align: "left", valign: "top", margin: 0, paraSpaceAfter: 4, fit: "shrink",
      }
    );
  }
  if (caveats.length) {
    const cy = 1.00 + half + 0.12;
    cardShell(cardX, cy, cardW, 4.55 - cy);
    cardTitle(cardX, cy, cardW, caveatsTitle);
    const cavFs = fitListFontSize(caveats.map(String), cardW - 0.44, 4.55 - cy - 0.50, 8.5, 7);
    warnCardOverflow("caveats", cavFs, caveats.length);
    warnUnderfill("addChartWithInsights", "the caveats card",
      caveats.reduce((s2, t) => s2 + visLen(t) + 2, 0),
      estCapacityChars(cardW - 0.44, 4.55 - cy - 0.50, 8.5),
      "give 4-5 caveats, each a ~20-30-char full sentence stating a concrete limit or risk");
    slide.addText(
      caveats.map((t) => ({ text: t, options: { bullet: { code: "2022", color: THEME.midGray }, breakLine: true } })),
      {
        x: cardX + 0.12, y: cy + 0.38, w: cardW - 0.24, h: 4.55 - cy - 0.46,
        fontFace: FONT, fontSize: cavFs, color: THEME.darkGray,
        align: "left", valign: "top", margin: 0, paraSpaceAfter: 4, fit: "shrink",
      }
    );
  }
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

// Rasterize an arbitrary SVG string to a PNG data URI (requires sharp).
// This is the unlimited-supply path for pipeline hero visuals: draw a custom
// Flat geometric SVG (geometric shapes, palette the accent color + grays, square
// corners, stroke-width >= 6 on a 240x240 viewBox) and pass the result as
// `image` to addPipelineDiagram's glyph stage.
async function svgToPng(svg, sizePx = 512) {
  let sharp;
  try {
    sharp = require(require.resolve("sharp", { paths: [process.cwd(), __dirname] }));
  } catch (e) {
    throw new Error("svgToPng requires the 'sharp' npm package — run: npm install sharp");
  }
  const buf = await sharp(Buffer.from(String(svg)), { density: 300 })
    .resize(sizePx, sizePx, { fit: "inside" }).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}

// Render a react-icons component to a PNG data URI. Requires react, react-dom, sharp.
// Only three icon colors are permitted: the accent color, "#000000", "#FFFFFF".
async function iconToPng(IconComponent, color = "#000000", sizePx = 256) {
  let React, ReactDOMServer, sharp;
  try {
    React = require("react");
    ReactDOMServer = require("react-dom/server");
    sharp = require("sharp");
  } catch (e) {
    throw new Error(
      "iconToPng needs react, react-dom and sharp. Install them, or pass `initial` instead of `iconData` to addCardGrid/addPanelList."
    );
  }
  const svg = ReactDOMServer.renderToStaticMarkup(
    React.createElement(IconComponent, { color, size: String(sizePx) })
  );
  const buf = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}


// Infographic pipeline (the "Hermes MOA 是什么" reference page): N numbered
// stages, each a bordered column holding icon chips, a supplied hero visual, or badge
// rows, joined by arrows, with an optional dashed feedback loop underneath.
// Everything self-sizes to the given rect (chip heights, fonts, visual size),
// so content never spills — the caller only supplies structure and text.
//
// stages: 3-5 of {
//   title,                       // short column heading (≤8 chars ideal)
//   kind: "chips" | "visual" | "badges",
//   items: [{ iconData?|initial?, color?, label, desc? }],   // chips/badges
//   image?, initial?, label, desc, // supplied visual or native fallback + caption
// }
// feedback: { from, to, label? } — dashed loop from stage[from] back to stage[to].
// Chip icon colors may use the secondary palette (soft categorical colors) —
// the ONE approved colorful-icon context; standalone icon grids stay accent/black/white.
function addPipelineDiagram(pres, slide, {
  stages = [], x = 0.5, y = 1.05, w = 9.0, h = 3.6, feedback = null,
} = {}) {
  assertSlide("addPipelineDiagram", pres, slide);
  warnRectOverlap("addPipelineDiagram", slide, x, y, w, h);
  if (stages.length < 3 || stages.length > 5) {
    console.warn(
      `[CONTENT-LIMIT NOTICE — addPipelineDiagram succeeded] ${stages.length} stages given; ` +
      "the pipeline reads best with 3-5 stages. Merge or split stages in YOUR slides.js — " +
      "do NOT edit components.js."
    );
  }
  const n = Math.max(stages.length, 1);
  const GAP = 0.36;
  const colW = (w - GAP * (n - 1)) / n;
  const HDR = 0.62;
  const fbH = feedback ? 0.36 : 0;
  const bodyY = y + HDR;
  const bodyH = h - HDR - fbH;

  stages.forEach((st, i) => {
    const sx = x + i * (colW + GAP);
    // Numbered accent disc + stage title
    const D = 0.30;
    slide.addShape(pres.shapes.OVAL, {
      x: sx + colW / 2 - D / 2, y, w: D, h: D,
      fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
    });
    slide.addText(String(i + 1), {
      x: sx + colW / 2 - D / 2, y, w: D, h: D,
      fontFace: FONT, fontSize: 11, bold: true, color: THEME.white,
      align: "center", valign: "middle", margin: 0,
    });
    slide.addText(String(st.title || ""), {
      x: sx, y: y + D + 0.03, w: colW, h: 0.24,
      fontFace: FONT, fontSize: 10, bold: true, color: THEME.black,
      align: "center", valign: "middle", margin: 0, fit: "shrink",
    });
    // Column panel
    slide.addShape(pres.shapes.RECTANGLE, {
      x: sx, y: bodyY, w: colW, h: bodyH,
      fill: { color: THEME.white }, line: { color: THEME.border, width: 0.75 },
    });
    // Inter-stage arrow
    if (i < n - 1) {
      slide.addShape(pres.shapes.RIGHT_ARROW, {
        x: sx + colW + 0.05, y: bodyY + bodyH / 2 - 0.09, w: GAP - 0.10, h: 0.18,
        fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
      });
    }

    const PAD = 0.10;
    if (st.kind === "visual") {
      const size = Math.min(colW * 0.60, bodyH * 0.50);
      // Hero visual: caller-supplied image/data URI, or a native initial fallback.
      const imgOpts = { x: sx + (colW - size) / 2, y: bodyY + PAD + 0.04, w: size, h: size };
      if (st.image && /^(image\/|data:)/.test(String(st.image))) {
        slide.addImage(Object.assign({ data: String(st.image).replace(/^data:/, "") }, imgOpts));
      } else if (st.image) {
        slide.addImage(Object.assign({ path: st.image }, imgOpts));
      } else {
        slide.addShape(pres.shapes.OVAL, {
          ...imgOpts,
          fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
        });
        slide.addText(String(st.initial || st.label || st.title || "").slice(0, 1), {
          ...imgOpts,
          fontFace: FONT, fontSize: Math.max(16, size * 18), bold: true, color: THEME.white,
          align: "center", valign: "middle", margin: 0,
        });
      }
      slide.addText(String(st.label || st.title || ""), {
        x: sx + PAD, y: bodyY + PAD + size + 0.08, w: colW - PAD * 2, h: 0.24,
        fontFace: FONT, fontSize: 10, bold: true, color: THEME.accent,
        align: "center", valign: "middle", margin: 0, fit: "shrink",
      });
      if (st.desc) {
        slide.addText(String(st.desc), {
          x: sx + PAD, y: bodyY + PAD + size + 0.34, w: colW - PAD * 2,
          h: Math.max(bodyH - PAD * 2 - size - 0.34, 0.2),
          fontFace: FONT, fontSize: 8, color: THEME.midGray,
          align: "center", valign: "top", margin: 0, fit: "shrink",
        });
      }
    } else {
      // chips / badges: vertical stack that self-sizes to the panel
      const items = st.items || [];
      const k = Math.max(items.length, 1);
      if (items.length > 5) {
        console.warn(
          `[CONTENT-LIMIT NOTICE — addPipelineDiagram succeeded] stage "${st.title}" holds ` +
          `${items.length} items; ≤5 fit a column. Merge items in YOUR slides.js — do NOT edit components.js.`
        );
      }
      const IGAP = 0.08;
      const rowH = Math.min(Math.max((bodyH - PAD * 2 - IGAP * (k - 1)) / k, 0.32), 0.66);
      const stackH = rowH * k + IGAP * (k - 1);
      const top = bodyY + Math.max(PAD, (bodyH - stackH) / 2);
      const withDesc = rowH >= 0.5;
      items.forEach((it, j) => {
        const cy = top + j * (rowH + IGAP);
        if (st.kind === "chips") {
          slide.addShape(pres.shapes.RECTANGLE, {
            x: sx + PAD, y: cy, w: colW - PAD * 2, h: rowH,
            fill: { color: THEME.paleGray }, line: { color: THEME.paleGray, width: 0 },
          });
        }
        const disc = Math.min(rowH - 0.10, 0.34);
        const dx = sx + PAD + 0.07, dy = cy + (rowH - disc) / 2;
        if (it.iconData) {
          slide.addImage({ data: it.iconData, x: dx, y: dy, w: disc, h: disc });
        } else {
          slide.addShape(pres.shapes.OVAL, {
            x: dx, y: dy, w: disc, h: disc,
            fill: { color: it.color || THEME.accent }, line: { color: it.color || THEME.accent, width: 0 },
          });
          slide.addText(String(it.initial || "").slice(0, 1), {
            x: dx, y: dy, w: disc, h: disc,
            fontFace: FONT, fontSize: Math.max(disc * 26, 8), bold: true, color: THEME.white,
            align: "center", valign: "middle", margin: 0,
          });
        }
        const runs = [{ text: String(it.label || ""), options: { bold: true, color: THEME.darkText, breakLine: !!(withDesc && it.desc) } }];
        if (withDesc && it.desc) runs.push({ text: String(it.desc), options: { color: THEME.midGray, fontSize: 7.5 } });
        slide.addText(runs, {
          x: dx + disc + 0.09, y: cy, w: colW - PAD * 2 - disc - 0.20, h: rowH,
          fontFace: FONT, fontSize: withDesc ? 9 : 8.5, align: "left", valign: "middle",
          margin: 0, fit: "shrink",
        });
      });
    }
  });

  // Dashed feedback loop along the bottom (e.g. 评估与验证 → 重新聚合)
  if (feedback && (feedback.from >= n || feedback.to < 0 || feedback.from <= feedback.to)) {
    console.warn(
      `[CONTENT-LIMIT NOTICE — addPipelineDiagram succeeded, but the feedback loop was SKIPPED] ` +
      `feedback {from: ${feedback.from}, to: ${feedback.to}} is invalid for ${n} stages — indexes are 0-based ` +
      `(0..${n - 1}) and must satisfy from > to. Fix the indexes in YOUR slides.js; do NOT edit components.js.`
    );
    feedback = null;
  }
  if (feedback && feedback.from > feedback.to) {
    const cxOf = (i2) => x + i2 * (colW + GAP) + colW / 2;
    const x1 = cxOf(feedback.from), x2 = cxOf(feedback.to);
    const ly = y + h - fbH + 0.16;
    const mk = () => ({ color: THEME.midGray, width: 1, dashType: "dash" });
    slide.addShape(pres.shapes.LINE, { x: x1, y: bodyY + bodyH, w: 0, h: ly - (bodyY + bodyH), line: mk() });
    slide.addShape(pres.shapes.LINE, { x: x2, y: ly, w: x1 - x2, h: 0, line: mk() });
    slide.addShape(pres.shapes.LINE, { x: x2, y: bodyY + bodyH, w: 0, h: ly - (bodyY + bodyH), line: mk() });
    slide.addShape(pres.shapes.ISOSCELES_TRIANGLE, {
      x: x2 - 0.05, y: bodyY + bodyH - 0.02, w: 0.10, h: 0.09,
      fill: { color: THEME.midGray }, line: { color: THEME.midGray, width: 0 },
    });
    if (feedback.label) {
      // Label box spans the full loop width (min 1.6") and shrinks to fit —
      // narrow adjacent-column loops must not clip the text.
      const lw = Math.max(Math.abs(x1 - x2) - 0.3, 1.6);
      slide.addText(String(feedback.label), {
        x: Math.max((x1 + x2) / 2 - lw / 2, x), y: ly - 0.20, w: Math.min(lw, w - 0.2), h: 0.18,
        fontFace: FONT, fontSize: 7.5, color: THEME.midGray, align: "center", margin: 0, fit: "shrink",
      });
    }
  }
}

// ---------------------------------------------------------------------------
// Graphic-first skeletons (tier-1 main-region components)
// ---------------------------------------------------------------------------

// Feature cards where a BIG icon is the visual anchor (not a caption garnish):
// pale card, 0.52" icon disc on top, bold title, 2-3 lines of fact text.
// cards: [{ iconData?|initial?, color?, title, text }] — disc colors may use
// the soft secondary palette; keep one concept = one color across the deck.
function addIconCards(pres, slide, {
  cards = [], columns = 0, x = 0.5, y = 1.05, w = 9.0, h = 3.6,
} = {}) {
  assertSlide("addIconCards", pres, slide);
  warnRectOverlap("addIconCards", slide, x, y, w, h);
  const n = Math.max(cards.length, 1);
  const cols = columns || (n <= 3 ? n : Math.ceil(n / 2));
  const rows = Math.ceil(n / cols);
  const gap = 0.18;
  const cardW = (w - gap * (cols - 1)) / cols;
  const cardH = (h - gap * (rows - 1)) / rows;
  cards.forEach((card, i) => {
    const cx = x + (i % cols) * (cardW + gap);
    const cy = y + Math.floor(i / cols) * (cardH + gap);
    addPanel(pres, slide, { fill: THEME.paleGray, x: cx, y: cy, w: cardW, h: cardH });
    const D = Math.min(0.52, cardH * 0.30);
    const dx = cx + (cardW - D) / 2, dy = cy + 0.14;
    if (card.iconData) {
      slide.addImage({ data: card.iconData, x: dx, y: dy, w: D, h: D });
    } else {
      slide.addShape(pres.shapes.OVAL, {
        x: dx, y: dy, w: D, h: D,
        fill: { color: card.color || THEME.accent }, line: { color: card.color || THEME.accent, width: 0 },
      });
      slide.addText(String(card.initial || "").slice(0, 1), {
        x: dx, y: dy, w: D, h: D,
        fontFace: FONT, fontSize: D * 32, bold: true, color: THEME.white,
        align: "center", valign: "middle", margin: 0,
      });
    }
    slide.addText(String(card.title || ""), {
      x: cx + 0.10, y: cy + 0.14 + D + 0.06, w: cardW - 0.20, h: 0.28,
      fontFace: FONT, fontSize: 10.5, bold: true, color: THEME.accent,
      align: "center", valign: "middle", margin: 0, fit: "shrink",
    });
    slide.addText(String(card.text || ""), {
      x: cx + 0.14, y: cy + 0.14 + D + 0.38, w: cardW - 0.28,
      h: Math.max(cardH - 0.14 - D - 0.48, 0.2),
      fontFace: FONT, fontSize: 8.5, color: THEME.darkGray,
      align: "left", valign: "top", margin: 0, fit: "shrink",
    });
    warnUnderfill("addIconCards", `card "${String(card.title).slice(0, 12)}"`,
      visLen(card.text),
      estCapacityChars(cardW - 0.28, cardH - 0.14 - D - 0.48, 8.5),
      "give this card ~2-3 sentences of concrete facts, or reduce the region height");
  });
}

// Hub-and-spoke: one core concept in the middle, 3-6 related facets around it,
// dashed connectors. center: { image?, label, desc? }.
// spokes: [{ iconData?|initial?, color?, label, desc? }].
function addHubSpoke(pres, slide, {
  center = {}, spokes = [], x = 0.4, y = 1.05, w = 6.1, h = 3.6,
} = {}) {
  assertSlide("addHubSpoke", pres, slide);
  warnRectOverlap("addHubSpoke", slide, x, y, w, h);
  if (spokes.length < 3 || spokes.length > 6) {
    console.warn(
      `[CONTENT-LIMIT NOTICE — addHubSpoke succeeded] ${spokes.length} spokes given; 3-6 read best. ` +
      "Merge or split facets in YOUR slides.js — do NOT edit components.js."
    );
  }
  const cx = x + w / 2, cy = y + h / 2;
  const R = Math.min(0.62, h * 0.17);          // hub circle radius
  const rx = w / 2 - 1.05, ry = h / 2 - 0.62;  // spoke orbit
  const n = Math.max(spokes.length, 1);
  // Connectors first (under everything)
  spokes.forEach((sp, i) => {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / n;
    const sx = cx + rx * Math.cos(a), sy = cy + ry * Math.sin(a);
    slide.addShape(pres.shapes.LINE, {
      x: Math.min(cx, sx), y: Math.min(cy, sy), w: Math.abs(sx - cx), h: Math.abs(sy - cy),
      flipH: sx < cx, flipV: (sy < cy) !== (sx < cx),
      line: { color: THEME.lightGray, width: 1, dashType: "dash" },
    });
  });
  // Hub
  slide.addShape(pres.shapes.OVAL, {
    x: cx - R, y: cy - R, w: R * 2, h: R * 2,
    fill: { color: THEME.white }, line: { color: THEME.accent, width: 1.75 },
  });
  const hubImg = center.image || null;
  if (hubImg) {
    const G = R * 1.05;
    const io = /^(image\/|data:)/.test(String(hubImg))
      ? { data: String(hubImg).replace(/^data:/, "") } : { path: hubImg };
    slide.addImage(Object.assign(io, { x: cx - G / 2, y: cy - G / 2 - R * 0.18, w: G, h: G }));
    slide.addText(String(center.label || ""), {
      x: cx - R, y: cy + R * 0.42, w: R * 2, h: R * 0.5,
      fontFace: FONT, fontSize: 8.5, bold: true, color: THEME.accent,
      align: "center", valign: "middle", margin: 0, fit: "shrink",
    });
  } else {
    slide.addText(String(center.label || ""), {
      x: cx - R, y: cy - R, w: R * 2, h: R * 2,
      fontFace: FONT, fontSize: 11, bold: true, color: THEME.accent,
      align: "center", valign: "middle", margin: 0, fit: "shrink",
    });
  }
  // Spokes
  const D = 0.4, TW = Math.min(1.85, (w - 1) / 3);
  spokes.forEach((sp, i) => {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / n;
    const sx = cx + rx * Math.cos(a), sy = cy + ry * Math.sin(a);
    if (sp.iconData) {
      slide.addImage({ data: sp.iconData, x: sx - D / 2, y: sy - D / 2, w: D, h: D });
    } else {
      slide.addShape(pres.shapes.OVAL, {
        x: sx - D / 2, y: sy - D / 2, w: D, h: D,
        fill: { color: sp.color || THEME.accent }, line: { color: sp.color || THEME.accent, width: 0 },
      });
      slide.addText(String(sp.initial || "").slice(0, 1), {
        x: sx - D / 2, y: sy - D / 2, w: D, h: D,
        fontFace: FONT, fontSize: 12, bold: true, color: THEME.white,
        align: "center", valign: "middle", margin: 0,
      });
    }
    const below = sy >= cy;                    // stack text away from the hub
    const ty = below ? sy + D / 2 + 0.04 : sy - D / 2 - 0.66;
    slide.addText(String(sp.label || ""), {
      x: sx - TW / 2, y: ty, w: TW, h: 0.22,
      fontFace: FONT, fontSize: 9, bold: true, color: THEME.black,
      align: "center", valign: "middle", margin: 0, fit: "shrink",
    });
    slide.addText(String(sp.desc || ""), {
      x: sx - TW / 2, y: ty + 0.22, w: TW, h: 0.40,
      fontFace: FONT, fontSize: 7.5, color: THEME.midGray,
      align: "center", valign: "top", margin: 0, fit: "shrink",
    });
  });
}

// Thin spectrum/positioning axis (低成本 → 高质量): accent arrow baseline with
// 2-5 tick dots. A thin band — pair it under a table/matrix, never alone.
// ticks: [{ label, desc?, pos? }] (pos 0-1 optional, defaults to even spread).
function addSpectrumAxis(pres, slide, {
  ticks = [], x = 0.5, y = 3.8, w = 9.0, leftLabel = "", rightLabel = "",
} = {}) {
  assertSlide("addSpectrumAxis", pres, slide);
  warnRectOverlap("addSpectrumAxis", slide, x, y, w, 0.85);
  const pad = leftLabel || rightLabel ? 0.95 : 0.25;
  const ax = x + pad, aw = w - pad * 2;
  slide.addShape(pres.shapes.LINE, {
    x: ax, y: y + 0.10, w: aw, h: 0,
    line: { color: THEME.accent, width: 2 },
  });
  slide.addShape(pres.shapes.ISOSCELES_TRIANGLE, {
    x: ax + aw - 0.02, y: y + 0.03, w: 0.14, h: 0.14, rotate: 90,
    fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
  });
  if (leftLabel) slide.addText(String(leftLabel), {
    x, y: y + 0.01, w: pad - 0.08, h: 0.2, fontFace: FONT, fontSize: 8.5,
    bold: true, color: THEME.darkGray, align: "right", valign: "middle", margin: 0, fit: "shrink",
  });
  if (rightLabel) slide.addText(String(rightLabel), {
    x: ax + aw + 0.12, y: y + 0.01, w: pad - 0.08, h: 0.2, fontFace: FONT, fontSize: 8.5,
    bold: true, color: THEME.darkGray, align: "left", valign: "middle", margin: 0, fit: "shrink",
  });
  const n = Math.max(ticks.length, 1);
  const TW = Math.min(2.2, aw / n);
  ticks.forEach((t, i) => {
    const p = typeof t.pos === "number" ? t.pos : (n === 1 ? 0.5 : i / (n - 1) * 0.84 + 0.08);
    const tx = ax + aw * p;
    slide.addShape(pres.shapes.OVAL, {
      x: tx - 0.055, y: y + 0.045, w: 0.11, h: 0.11,
      fill: { color: THEME.accent }, line: { color: THEME.white, width: 1 },
    });
    slide.addText(String(t.label || ""), {
      x: tx - TW / 2, y: y + 0.22, w: TW, h: 0.2,
      fontFace: FONT, fontSize: 8.5, bold: true, color: THEME.black,
      align: "center", valign: "middle", margin: 0, fit: "shrink",
    });
    if (t.desc) slide.addText(String(t.desc), {
      x: tx - TW / 2, y: y + 0.42, w: TW, h: 0.34,
      fontFace: FONT, fontSize: 7.5, color: THEME.midGray,
      align: "center", valign: "top", margin: 0, fit: "shrink",
    });
  });
}


// Paper-figure visual region — the peer of addSystemDiagram for pages that
// introduce a specific paper. Figures are the paper's own, extracted verbatim
// by scripts/extract_arxiv_visuals_v2_2.py; they are pasted as-is (no border,
// no fill) because they are quotations, not our artwork. `figures` is
// { path, label }[] — 1-3 of them. Optional `heading` draws a black centered
// column heading atop the region (see addRegionHeading), exactly as
// addSystemDiagram/addTextBlock do. Returns the layout descriptor.
function addFigurePanel(pres, slide, { x = 0.5, y = 1.05, w = 5.4, h = 3.6, heading, figures = [] } = {}) {
  assertSlide("addFigurePanel", pres, slide);
  warnRectOverlap("addFigurePanel", slide, x, y, w, h);
  if (figures.length > 3) {
    console.warn(
      `[CONTENT-LIMIT NOTICE — addFigurePanel succeeded] the panel packs ${figures.length} figures; ` +
      "a visual region reads best with 1-3. Drop the weakest ones in YOUR slides.js, or split the page — " +
      "do NOT edit components.js."
    );
  }
  let by = y, bh = h;
  if (heading) { by = addRegionHeading(pres, slide, { text: heading, x, y, w }); bh = h - (by - y); }
  return renderFigurePanel({ slide, figures, bounds: { x, y: by, w, h: bh } });
}


// Plain black, bold, CENTER-aligned heading drawn just above a region, summarizing
// what that region shows (e.g. "传统Agent优化流程" over a diagram, "关键问题" over a
// text column). NOT the accent page title (addSlideTitle) and NOT addTextBlock's
// accent-bar `title` — it's the black centered label the 图文分栏 recipe puts atop each
// column. PUBLIC component: call it directly to put ONE shared heading atop a
// multi-chart / multi-diagram visual region (the sub-figures then start below the
// returned y), since addDeckChart et al. have no `heading` param of their own. For
// a single addSystemDiagram / addTextBlock region, pass `heading` to those instead —
// they call this internally. Returns the y the region's content should start at.
const REGION_HEADING_H = 0.30;
const REGION_HEADING_GAP = 0.04;
function addRegionHeading(pres, slide, { text, x = 0.5, y = 1.05, w = 9.0 } = {}) {
  assertSlide("addRegionHeading", pres, slide);
  slide.addText(String(text), {
    x, y, w, h: REGION_HEADING_H,
    fontFace: FONT, fontSize: 12.5, bold: true, color: THEME.black,
    align: "center", valign: "middle", margin: 0, fit: "shrink",
  });
  return y + REGION_HEADING_H + REGION_HEADING_GAP;
}

// Declarative system diagram (bands/groups/items/edges) drawn as native shapes
// into a content-page region. `spec` is a Diagram IR; see system-diagram.js and
// pptxgenjs.md. `density` auto-selects from region width when omitted. Optional
// `heading` draws a black centered column heading atop the region (see
// addRegionHeading) and shrinks the diagram area beneath it. Returns the engine's
// layout descriptor { bounds, nodes, groups, edgeLabels, warnings }.
function addSystemDiagram(pres, slide, { spec, x = 0.5, y = 1.05, w = 9.0, h = 3.6, density, heading } = {}) {
  assertSlide("addSystemDiagram", pres, slide);
  warnRectOverlap("addSystemDiagram", slide, x, y, w, h);
  let by = y, bh = h;
  if (heading) { by = addRegionHeading(pres, slide, { text: heading, x, y, w }); bh = h - (by - y); }
  const dens = density || (w >= 8 ? "full" : w >= 4 ? "split" : "compact");
  const d = DIAGRAM_DENSITY[dens] || DIAGRAM_DENSITY.full;
  const theme = { ...DEFAULT_THEME, fontMin: d.fontMin, fontMax: d.fontMax };
  return renderSystemDiagram({ pres, slide, spec, bounds: { x, y: by, w, h: bh }, theme });
}

// Unboxed structured text region — the DEFAULT for text areas (contrast the
// gray-filled addCardGrid/addPanel). White, NO background fill. Items are
// { title, body, children[] } (a bare string is treated as { body }). Style only
// controls presentation; markers are native shapes / safe glyphs, never exotic
// characters that font substitution mangles.
//   "numbered"  — accent disc + white number, title (accent bold), body, bulleted children
//   "sectioned" — accent bar + title (accent bold), body, bulleted children
//   "bullets"   — title (accent bold), body + children as gray bullets, no marker
// Optional `heading` draws a black centered column heading atop the region (see
// addRegionHeading) and shrinks the text area beneath it — this is the black
// "关键问题"-style label, distinct from the accent-bar `title`.
function addTextBlock(pres, slide, { x = 0.5, y = 1.05, w = 3.3, h = 3.5, title, items = [], style = "numbered", heading } = {}) {
  assertSlide("addTextBlock", pres, slide);
  warnRectOverlap("addTextBlock", slide, x, y, w, h);
  if (heading) { const ny = addRegionHeading(pres, slide, { text: heading, x, y, w }); h -= ny - y; y = ny; }
  let top = y;
  if (title) {
    slide.addShape(pres.shapes.RECTANGLE, {
      x, y: y + 0.04, w: 0.06, h: 0.24, fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
    });
    slide.addText(String(title), {
      x: x + 0.16, y, w: w - 0.16, h: 0.32, fontFace: FONT, fontSize: 12, bold: true,
      color: THEME.accent, align: "left", valign: "middle", margin: 0, fit: "shrink",
    });
    top = y + 0.44;
  }
  const norm = items.map((it) => (typeof it === "string" ? { body: it } : it || {}));
  const n = Math.max(norm.length, 1);
  const gap = 0.12;
  const slotH = (y + h - top - gap * (n - 1)) / n;

  // Auto-fill: scale font size UP when content is sparse relative to the
  // block's capacity, instead of rendering fixed-size text that leaves the
  // container looking thin. Capacity/size are inversely related by ~the
  // square (more chars per line AND more lines as font shrinks), so solving
  // charsUsed ≈ capacity(fontScale) gives fontScale ≈ sqrt(capacityAtBase /
  // charsUsed). Floor at 1 (never shrink here — `fit:"shrink"` below is the
  // separate overflow safety valve for genuinely dense content).
  const blockChars = norm.reduce((sum, it) =>
    sum + visLen(it.title) + visLen(it.body) +
    (it.children || []).reduce((a, c) => a + visLen(c), 0), 0);
  const capacityAtBase = estCapacityChars(w - 0.5, (y + h) - top, 9.5);
  const fillRatio = blockChars / Math.max(capacityAtBase, 1);
  const fontScale = fillRatio < 1 ? Math.min(1.4, Math.max(1, Math.sqrt(0.8 / Math.max(fillRatio, 0.13)))) : 1;

  norm.forEach((it, i) => {
    const sy = top + i * (slotH + gap);
    let textX = x;
    if (style === "numbered") {
      const D = 0.26 * Math.min(fontScale, 1.25);
      slide.addShape(pres.shapes.OVAL, {
        x, y: sy, w: D, h: D, fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
      });
      slide.addText(String(i + 1), {
        x, y: sy, w: D, h: D, fontFace: FONT, fontSize: 11 * Math.min(fontScale, 1.25), bold: true, color: THEME.white,
        align: "center", valign: "middle", margin: 0,
      });
      textX = x + D + 0.12;
    } else if (style === "sectioned") {
      slide.addShape(pres.shapes.RECTANGLE, {
        x, y: sy + 0.03, w: 0.06, h: 0.20, fill: { color: THEME.accent }, line: { color: THEME.accent, width: 0 },
      });
      textX = x + 0.18;
    }
    const runs = [];
    if (it.title) runs.push({ text: String(it.title), options: { bold: true, color: THEME.accent, fontSize: 11 * fontScale, breakLine: true } });
    if (it.body) runs.push({ text: String(it.body), options: { color: THEME.darkGray, fontSize: 9.5 * fontScale, breakLine: true } });
    (it.children || []).forEach((c) => runs.push({
      text: String(c), options: { color: THEME.midGray, fontSize: 9 * fontScale, bullet: { code: "2022", indent: 12 }, breakLine: true },
    }));
    if (runs.length) {
      slide.addText(runs, {
        x: textX, y: sy, w: x + w - textX, h: slotH, fontFace: FONT,
        align: "left", valign: "top", margin: 0, paraSpaceAfter: 3 * fontScale, fit: "shrink",
      });
    }
  });
  // Density signal: a very low fill ratio means the container is left mostly
  // empty. Content pages must be filled, so nudge the caller to write more.
  if (fillRatio < 0.16) {
    warnUnderfill("addTextBlock", `text block "${String(title || (norm[0] && norm[0].title) || "").slice(0, 12)}"`,
      blockChars, capacityAtBase,
      "每条给 title + 1-2 句完整 body（35-50 字）+ 必要时 2-3 条 children；纯文字页每列 2-4 段、拉满高度");
  }
}

// ---------------------------------------------------------------------------
// Reference-vocabulary structural components — the tinted matrix, converge
// funnel, and chevron stage band that the reference decks use as their
// signature "one big structural graphic" page language. Combine top-to-bottom
// (matrix → funnel → stages) to reproduce that page shape, or use each alone.

// Pastel fills reserved for group headers — the deck-wide "淡彩仅限分组头"
// palette exception. Body cards stay white; accents stay the accent color.
const MATRIX_TINTS = [
  { fill: "EEEAF7", line: "C8BEDF" }, // violet
  { fill: "FAEEE4", line: "E5C9B3" }, // orange
  { fill: "E8F1F8", line: "B9D1E2" }, // blue
  { fill: "F7E9EE", line: "DDBFCC" }, // pink
  { fill: "EAF3E8", line: "BFD8BC" }, // green
  { fill: "FFF6DE", line: "E3D3A3" }, // yellow
];

// Three-row classification matrix: tinted group headers, white sub-headers,
// white item cards with hairline dividers. Group width follows its sub count.
// groups: [{ name, subs: [{ name, items: ["...", ...] }] }]
// rowLabels: e.g. ["族群", "子类", "具体条目"] for the narrow left label column
// (pass null to omit). Subs without items collapse the matrix to two rows.
function addTintMatrix(pres, slide, {
  x = 0.5, y = 1.05, w = 9.0, h = 1.7,
  rowLabels = null, groups = [],
} = {}) {
  assertSlide("addTintMatrix", pres, slide);
  warnRectOverlap("addTintMatrix", slide, x, y, w, h);
  const normGroups = groups.map((g) => ({
    ...g, subs: g.subs && g.subs.length ? g.subs : [{ name: g.name, items: [] }],
  }));
  const totalSubs = normGroups.reduce((s, g) => s + g.subs.length, 0);
  if (!totalSubs) return;
  if (totalSubs > 9) {
    console.warn(
      `[CONTENT-LIMIT NOTICE — addTintMatrix succeeded, but ${totalSubs} sub-columns squeeze below readable ` +
      `width on w=${w}". In YOUR slides.js merge to ≤9 sub-columns; do NOT edit components.js.`
    );
  }
  const hasItems = normGroups.some((g) => g.subs.some((sb) => (sb.items || []).length));
  const labelW = rowLabels ? 0.62 : 0;
  const dataX = x + (rowLabels ? labelW + 0.08 : 0);
  const dataW = x + w - dataX;
  const unit = dataW / totalSubs;
  const headH = 0.30, subH = 0.31, gapY = 0.035;
  const itemH = hasItems ? Math.max(h - headH - subH - gapY * 2, 0.4) : 0;
  if (rowLabels) {
    const rows = [
      { text: rowLabels[0], ry: y, rh: headH },
      { text: rowLabels[1], ry: y + headH + gapY, rh: subH },
      { text: rowLabels[2], ry: y + headH + subH + gapY * 2, rh: itemH },
    ];
    rows.forEach((r) => {
      if (!r.text || !r.rh) return;
      slide.addShape(pres.shapes.RECTANGLE, {
        x, y: r.ry, w: labelW, h: r.rh,
        fill: { color: "F2F2F2" }, line: { color: "E1E1E1", width: 0.55 },
      });
      slide.addText(String(r.text), {
        x, y: r.ry, w: labelW, h: r.rh, fontFace: FONT, fontSize: 8, bold: true,
        color: THEME.midGray, align: "center", valign: "middle", margin: 0.02, fit: "shrink",
      });
    });
  }
  let gx = dataX;
  normGroups.forEach((g, gi) => {
    const tint = MATRIX_TINTS[gi % MATRIX_TINTS.length];
    const gw = unit * g.subs.length;
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: gx, y, w: gw - 0.05, h: headH, rectRadius: 0.04,
      fill: { color: tint.fill }, line: { color: tint.line, width: 0.55 },
    });
    slide.addText(String(g.name), {
      x: gx, y, w: gw - 0.05, h: headH, fontFace: FONT, fontSize: 10, bold: true,
      color: THEME.darkText, align: "center", valign: "middle", margin: 0.02, fit: "shrink",
    });
    g.subs.forEach((sub, si) => {
      const sx = gx + si * unit;
      const sy = y + headH + gapY;
      slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
        x: sx, y: sy, w: unit - 0.05, h: subH, rectRadius: 0.04,
        fill: { color: THEME.white }, line: { color: tint.line, width: 0.55 },
      });
      slide.addText(String(sub.name), {
        x: sx, y: sy, w: unit - 0.05, h: subH, fontFace: FONT, fontSize: 9, bold: true,
        color: THEME.darkText, align: "center", valign: "middle", margin: 0.02, fit: "shrink",
      });
      const items = sub.items || [];
      if (!hasItems) return;
      const iy = sy + subH + gapY;
      slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
        x: sx, y: iy, w: unit - 0.05, h: itemH, rectRadius: 0.04,
        fill: { color: THEME.white }, line: { color: "D8D8D8", width: 0.55 },
        shadow: cardShadow(),
      });
      const cellH = itemH / Math.max(items.length, 1);
      items.forEach((it, ii) => {
        slide.addText(String(it), {
          x: sx + 0.06, y: iy + ii * cellH, w: unit - 0.17, h: cellH,
          fontFace: FONT, fontSize: 8.5, color: THEME.darkText,
          align: "center", valign: "middle", margin: 0, fit: "shrink",
        });
        if (ii < items.length - 1) {
          slide.addShape(pres.shapes.LINE, {
            x: sx + 0.12, y: iy + (ii + 1) * cellH, w: unit - 0.29, h: 0,
            line: { color: "E5E5E5", width: 0.5 },
          });
        }
      });
    });
    gx += gw;
  });
}

// Layered translucent gray funnel + down arrow: "everything above converges
// into what follows". Optional accent caption below, flanked by hairlines.
function addConvergeFunnel(pres, slide, { x = 0.5, y = 1.05, w = 9.0, h = 0.62, label } = {}) {
  assertSlide("addConvergeFunnel", pres, slide);
  warnRectOverlap("addConvergeFunnel", slide, x, y, w, h);
  const funnelH = label ? Math.max(h - 0.26, 0.3) : h;
  slide.addShape(pres.shapes.TRAPEZOID, {
    x: x + 0.06, y, w: w - 0.12, h: funnelH * 0.58, rotate: 180,
    fill: { color: "D6D6D6", transparency: 50 }, line: { color: "D6D6D6", transparency: 100 },
  });
  slide.addShape(pres.shapes.TRAPEZOID, {
    x: x + w * 0.12, y: y + funnelH * 0.16, w: w * 0.76, h: funnelH * 0.5, rotate: 180,
    fill: { color: "C9C9C9", transparency: 65 }, line: { color: "C9C9C9", transparency: 100 },
  });
  slide.addShape(pres.shapes.DOWN_ARROW, {
    x: x + w / 2 - 0.43, y: y + funnelH * 0.36, w: 0.86, h: funnelH * 0.64,
    fill: { color: "C4C4C4", transparency: 36 }, line: { color: "C4C4C4", transparency: 100 },
  });
  if (label) {
    const ly = y + funnelH + 0.02;
    const textW = Math.min(Math.max(visLen(label) * 0.16, 1.6), w * 0.5);
    const lineW = (w - textW) / 2 - 0.15;
    slide.addShape(pres.shapes.LINE, {
      x, y: ly + 0.12, w: lineW, h: 0, line: { color: "C7C7C7", width: 0.8 },
    });
    slide.addShape(pres.shapes.LINE, {
      x: x + w - lineW, y: ly + 0.12, w: lineW, h: 0, line: { color: "C7C7C7", width: 0.8 },
    });
    slide.addText(String(label), {
      x: x + (w - textW) / 2, y: ly, w: textW, h: 0.24, fontFace: FONT, fontSize: 10.2,
      bold: true, color: THEME.accent, align: "center", valign: "middle", margin: 0, fit: "shrink",
    });
  }
}

// Chevron stage band with optional white output cards under each stage.
// stages: [{ name, card?: { title, items: ["...", "..."] } }] (items ≤3).
// Band only when no stage has a card; cards get the shared soft shadow and a
// small tick connecting them to their stage.
function addChevronStages(pres, slide, {
  x = 0.5, y = 1.05, w = 9.0, h = 1.3, stages = [],
} = {}) {
  assertSlide("addChevronStages", pres, slide);
  warnRectOverlap("addChevronStages", slide, x, y, w, h);
  const n = stages.length;
  if (!n) return;
  if (n > 5) {
    console.warn(
      `[CONTENT-LIMIT NOTICE — addChevronStages succeeded, but ${n} stages crowd w=${w}". ` +
      "In YOUR slides.js merge to ≤5 stages; do NOT edit components.js."
    );
  }
  const bandH = 0.34;
  const gap = 0.06;
  const cellW = (w - gap * (n - 1)) / n;
  const anyCard = stages.some((st) => st.card);
  stages.forEach((st, i) => {
    const sx = x + i * (cellW + gap);
    slide.addShape(i < n - 1 ? pres.shapes.CHEVRON : pres.shapes.PENTAGON, {
      x: sx, y, w: cellW, h: bandH,
      fill: { color: i % 2 ? "EAEAEA" : "F0F0F0" }, line: { color: "D5D5D5", width: 0.55 },
    });
    slide.addText(String(st.name), {
      x: sx + 0.05, y, w: cellW - 0.18, h: bandH, fontFace: FONT, fontSize: 9.7,
      bold: true, color: THEME.accent, align: "center", valign: "middle", margin: 0.01, fit: "shrink",
    });
    if (!anyCard) return;
    const cardY = y + bandH + 0.12;
    const cardH = Math.max(h - bandH - 0.12, 0.5);
    slide.addShape(pres.shapes.LINE, {
      x: sx + cellW / 2, y: y + bandH - 0.02, w: 0, h: cardY - y - bandH + 0.02,
      line: { color: "BDBDBD", width: 1.0 },
    });
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: sx, y: cardY, w: cellW, h: cardH, rectRadius: 0.06,
      fill: { color: THEME.white }, line: { color: "D6D6D6", width: 0.65 },
      shadow: cardShadow(),
    });
    const card = st.card;
    if (!card) return;
    const items = card.items || [];
    if (items.length > 3) {
      console.warn(
        `[CONTENT-LIMIT NOTICE — addChevronStages succeeded, but stage "${String(st.name).slice(0, 12)}" ` +
        "packs " + items.length + " card items (≤3 stay readable). Trim in YOUR slides.js; do NOT edit components.js."
      );
    }
    const titleW = card.title ? Math.min(cellW * 0.34, 1.0) : 0;
    if (card.title) {
      slide.addText(String(card.title), {
        x: sx + 0.14, y: cardY + 0.08, w: titleW, h: 0.22, fontFace: FONT, fontSize: 9.3,
        bold: true, color: THEME.accent, align: "left", valign: "middle", margin: 0, fit: "shrink",
      });
    }
    const tx0 = sx + 0.14 + (card.title ? titleW + 0.06 : 0);
    const tw = sx + cellW - 0.12 - tx0;
    const rowH = (cardH - 0.14) / Math.max(items.length, 1);
    items.forEach((it, ii) => {
      slide.addText(String(it), {
        x: tx0, y: cardY + 0.07 + ii * rowH, w: tw, h: rowH, fontFace: FONT, fontSize: 8.3,
        color: THEME.darkText, align: "left", valign: "middle", margin: 0, fit: "shrink",
      });
      if (ii < items.length - 1) {
        slide.addShape(pres.shapes.LINE, {
          x: tx0, y: cardY + 0.07 + (ii + 1) * rowH, w: tw - 0.05, h: 0,
          line: { color: "E4E4E4", width: 0.55 },
        });
      }
    });
  });
}

module.exports = {
  THEME, TY, LAYOUT, withHash, cols,
  addSlideTitle, addPanel, addSlideFooter, addSourceNote, addSummaryBanner, addContentChrome,
  addOpeningSlide, addTocSlide, addClosingSlide,
  addKpiRow, addCardGrid, addTimeline, addComparisonTable,
  addPanelList,
  addDeckChart, addChartWithInsights,
  iconToPng, svgToPng,
  addPipelineDiagram, addIconCards, addHubSpoke, addSpectrumAxis,
  addSystemDiagram, addTextBlock, addRegionHeading, addFigurePanel,
  addTintMatrix, addConvergeFunnel, addChevronStages,
  item, group, bandNode, groupBand,
};
