// System-diagram renderer for the deck pptxgenjs skill.
// Declarative Diagram IR (bands / groups / items / edges) -> deterministic layout
// -> native pptxgenjs shapes (p:sp / p:cxnSp; never an image). CommonJS module.
//
// Adapted from the generic_system_diagram bundle: kept normalizeSpec's structural
// validation + the layout/draw engine; removed the OpenAI planner, CLI, offline
// demos, and the suitability (assessment / render_decision) gate. Palette and font
// bounds are injected per render via `theme`, so the engine is not hard-wired to
// any one palette — components.js passes the deck theme. Consumers call
// renderSystemDiagram({ pres, slide, spec, bounds, theme }).

const FONT = "Microsoft YaHei";

// Neutral fallback palette, in the internal (bundle) key shape. The soft
// green/yellow/orange tints fill bands and groups; emphasis uses the deck
// accent, genuine warnings use a distinct red, text/lines use grays.
const DEFAULT_COLORS = {
  accent: "4472C4", warning: "C00000",
  green: "E2F2D5", green2: "D9EAD3", yellow: "FFF2CC", orange: "FCE4D6",
  blue: "DDEBF7", comparisonB: "EDE7F6",
  white: "FFFFFF", text: "000000",
  gray: "B5B5B5", lightGray: "B5B5B5", darkGray: "595757",
};

// Public theme shape callers pass; mapped onto the internal COLORS at render entry.
const DEFAULT_THEME = {
  accent: "4472C4", warning: "C00000", white: "FFFFFF", text: "000000",
  gray: "B5B5B5", lightGray: "B5B5B5", darkGray: "595757",
  tints: { green: "E2F2D5", green2: "D9EAD3", yellow: "FFF2CC", orange: "FCE4D6", blue: "DDEBF7", gray: "EDE7F6" },
  fontMin: 6, fontMax: 13,
  pageW: 10, pageH: 5.625,   // LAYOUT_16x9 — used only to clamp loop-edge labels on-slide
};

// Module-level render state, (re)assigned at the start of every renderSystemDiagram call.
let SHAPE_TYPE = null;       // set from pres.ShapeType
let COLORS = DEFAULT_COLORS;
let FONT_MIN = 6;
let FONT_MAX = 13;
let PAGE_W = 10;
let PAGE_H = 5.625;

function clampFont(size) {
  return Math.max(FONT_MIN, Math.min(FONT_MAX, size));
}

// Legibility floors for node/item boxes, shared between the measure phase
// (measureBandMinHeight) and the render phase (computeBandLayout) so the two
// can never disagree about how small a box is allowed to get. See the
// [DIAGRAM-OVER-CAPACITY] comment on computeBandRects for why this needs to
// be a real floor rather than a bare Math.min.
const ITEM_H_FLOOR = 0.24;
const NODE_H_FLOOR = 0.26;
const SIMPLE_BAR_MIN_H = 0.42;

function themeToColors(theme) {
  if (!theme) return DEFAULT_COLORS;
  const t = theme.tints || DEFAULT_THEME.tints;
  return {
    accent: theme.accent, warning: theme.warning || theme.accent,
    green: t.green, green2: t.green2 || t.green, yellow: t.yellow, orange: t.orange,
    blue: t.blue, comparisonB: t.gray,
    white: theme.white, text: theme.text,
    gray: theme.gray, lightGray: theme.lightGray || theme.gray, darkGray: theme.darkGray,
  };
}

function assertString(value, name, allowEmpty = false) {
  if (typeof value !== "string" || (!allowEmpty && value.trim() === "")) {
    throw new Error(`${name} must be ${allowEmpty ? "a string" : "a non-empty string"}.`);
  }
}

// CJK/fullwidth glyphs render roughly twice as wide as Latin letters. `max` was
// tuned by eye against Chinese labels (1 char = 1 unit); counting Latin/digit/
// punctuation runs as 0.5 unit keeps that Chinese calibration unchanged while
// giving English labels ~2x the character budget. Without this, a plain
// `length > max` cut English labels far too early — "Residual Extraction
// Agent" (26 chars) was truncating to "Residual Extraction Age…" under a
// max of 24, well short of running out of box width.
function isWideChar(ch) {
  const code = ch.codePointAt(0);
  return (
    (code >= 0x1100 && code <= 0x115f) || // Hangul Jamo
    (code >= 0x2e80 && code <= 0xa4cf) || // CJK radicals..Yi (covers CJK Unified, Kana, Hangul-adjacent blocks)
    (code >= 0xac00 && code <= 0xd7a3) || // Hangul syllables
    (code >= 0xf900 && code <= 0xfaff) || // CJK compatibility ideographs
    (code >= 0xff00 && code <= 0xff60) || // Fullwidth forms
    (code >= 0xffe0 && code <= 0xffe6)
  );
}

function truncateLabel(text, max = 28) {
  const normalized = String(text ?? "").replace(/\s+/g, " ").trim();
  const chars = Array.from(normalized);
  let visualLen = 0;
  for (const ch of chars) visualLen += isWideChar(ch) ? 1 : 0.5;
  if (visualLen <= max) return normalized;

  let acc = 0;
  let cut = chars.length;
  for (let i = 0; i < chars.length; i++) {
    acc += isWideChar(chars[i]) ? 1 : 0.5;
    if (acc > max - 0.5) { cut = i; break; }
  }
  return `${chars.slice(0, Math.max(1, cut)).join("")}…`;
}

function collectIds(spec) {
  const ids = new Set();
  const add = (id, pathName) => {
    if (ids.has(id)) throw new Error(`Duplicate id '${id}' at ${pathName}.`);
    ids.add(id);
  };
  for (const band of spec.bands) {
    add(band.id, `band:${band.id}`);
    for (const node of band.nodes) add(node.id, `node:${node.id}`);
    for (const group of band.groups) {
      add(group.id, `group:${group.id}`);
      for (const item of group.items) add(item.id, `item:${item.id}`);
    }
  }
  return ids;
}


// 语义损失弱信号：一个 label 疑似把多个平级条目合并/省略了。引擎不会自己删条目，
// 也不会缩字换空间（4+ 项 itemGridShape 自动多列、不压扁）——所以 label 里出现「等/…」
// 省略、或「型号A/型号B」斜杠合并，几乎都是 authoring 时手工减条目造成的语义损失。
// 返回原因串或 null。
function mergeSuspect(label) {
  const s = String(label).trim();
  // 纯省略号（"……"/"…"）是"还有更多同类"的续行暗示，不算语义损失；只有"具体名字 + 等/…"
  // （如"Llama-3-70B等"）才是把某个条目名省略掉了。
  const stripped = s.replace(/(等|…|\.{2,}|、)+$/, "").trim();
  if (stripped.length > 0 && stripped.length < s.length) return "以「等/…」省略了条目";
  const parts = s.split("/");
  if (parts.length >= 2 && /[A-Za-z0-9]/.test(parts[0]) && /[A-Za-z0-9]/.test(parts[parts.length - 1])) {
    return "用「/」把多个条目合并进一个 label";
  }
  return null;
}

function normalizeSpec(rawSpec) {
  const spec = structuredClone(rawSpec);
  // orientation "left_to_right" 已移除。它是本 skill 里唯一不测量内容就渲染的路径：
  // computeBandRects 给每个 band 无条件分配整个区域高度，节点高度又被封顶在
  // 0.50*scale 并垂直居中——单行节点最多填 0.41" 于约 3.2" 的 content 高度，87% 的
  // 空白是结构性保证的，且页面级 UNDERFILL 检测器按图形覆盖判定，看不见空心 band。
  // top_to_bottom 会测量并在装不下时报 OVER-CAPACITY，于是严格的路径把作者推向宽松
  // 的这条。fail loud 而非静默转向：见 commit c7bc7b70（cols() 静默忽略参数）。
  if (spec.orientation === "left_to_right") {
    throw new Error(
      `[CALL-ERROR in YOUR slides.js — NOT a library bug] addSystemDiagram: ` +
      `orientation "left_to_right" 已移除——它不测量内容，band 无条件撑满区域高度，` +
      `稀疏内容必然产生大片空白且无任何警告。系统图一律纵向排（缺省即 top_to_bottom，` +
      `删掉这个字段即可）；真正线性的 3-5 步横向流程请改用 addPipelineDiagram。`
    );
  }
  // The suitability (assessment / render_decision) gate was intentionally removed:
  // every diagram-worthy page should render. Structural validation below stays —
  // it prevents a corrupt IR from producing a broken PPTX (duplicate ids, dangling
  // edges, empty groups, out-of-range counts).
  assertString(spec.title, "title", true);
  assertString(spec.subtitle, "subtitle", true);
  assertString(spec.core_message, "core_message", true);

  if (!Array.isArray(spec.bands)) throw new Error("bands must be an array.");
  if (!Array.isArray(spec.edges)) throw new Error("edges must be an array.");

  if (spec.bands.length < 2 || spec.bands.length > 8) {
    throw new Error("Renderable specs must contain 2–8 bands.");
  }

  let leafCount = 0;
  const mergeSuspects = [];
  for (const [bandIndex, band] of spec.bands.entries()) {
    assertString(band.id, `bands[${bandIndex}].id`);
    assertString(band.label, `bands[${bandIndex}].label`);
    band.label = truncateLabel(band.label, 32);
    if (!Array.isArray(band.nodes) || !Array.isArray(band.groups)) {
      throw new Error(`Band '${band.id}' must have nodes and groups arrays.`);
    }
    if (band.layout === "full_width" && (band.nodes.length > 0 || band.groups.length > 0)) {
      band.layout = band.groups.length > 0 ? "grid" : "row";
    }
    for (const node of band.nodes) {
      assertString(node.id, `${band.id}.node.id`);
      assertString(node.label, `${node.id}.label`);
      node.label = truncateLabel(node.label, 24);
      { const r = mergeSuspect(node.label); if (r) mergeSuspects.push(`「${node.label}」(${r})`); }
      leafCount += 1;
    }
    for (const group of band.groups) {
      assertString(group.id, `${band.id}.group.id`);
      assertString(group.title, `${group.id}.title`);
      group.title = truncateLabel(group.title, 20);
      if (!Array.isArray(group.items) || group.items.length === 0) {
        throw new Error(`Group '${group.id}' must contain at least one item.`);
      }
      for (const item of group.items) {
        assertString(item.id, `${group.id}.item.id`);
        assertString(item.label, `${item.id}.label`);
        item.label = truncateLabel(item.label, 24);
        { const r = mergeSuspect(item.label); if (r) mergeSuspects.push(`「${item.label}」(${r})`); }
        leafCount += 1;
      }
    }
  }
  if (leafCount > 36) throw new Error(`The diagram has ${leafCount} leaf nodes; limit is 36 for one slide.`);
  if (mergeSuspects.length > 0) {
    console.warn(
      `[SEMANTIC-LOSS NOTICE — addSystemDiagram succeeded] ${mergeSuspects.length} 个 label 疑似合并/省略了平级条目：` +
      `${mergeSuspects.join("；")}。平级 N 项应写成一个 group 的 N 个完整 items——4+ 项引擎会自动多列排布` +
      `（不压扁、不需你合并）。在 YOUR slides.js 里把它们拆成独立 items、恢复完整名称，不要用「等」「A/B」减条目（语义损失）。` +
      `放不下时靠扩大区域/换骨架/拆页，不靠合并。不要改 system-diagram.js。`
    );
  }

  const ids = collectIds(spec);
  for (const edge of spec.edges) {
    if (!ids.has(edge.from)) throw new Error(`Edge source '${edge.from}' does not exist.`);
    if (!ids.has(edge.to)) throw new Error(`Edge target '${edge.to}' does not exist.`);
    edge.label = truncateLabel(edge.label, 18);
  }

  if (spec.edges.length === 0 && ["process", "closed_loop", "layered"].includes(spec.diagram_type)) {
    for (let i = 0; i < spec.bands.length - 1; i += 1) {
      spec.edges.push({
        from: spec.bands[i].id,
        to: spec.bands[i + 1].id,
        relation: "flow",
        direction: "forward",
        label: "",
        line_style: "solid",
        emphasis: "normal",
        routing: "direct",
        multiplicity: "single",
      });
    }
  }

  return spec;
}

function styleForVisualRole(role, isContainer = false) {
  switch (role) {
    case "primary":
      return { fill: COLORS.green, line: COLORS.gray, dash: "solid", text: COLORS.text };
    case "secondary":
      return { fill: COLORS.green2, line: COLORS.gray, dash: "solid", text: COLORS.text };
    case "factor_group":
      return { fill: COLORS.yellow, line: COLORS.gray, dash: "solid", text: COLORS.text };
    case "warning":
      return { fill: COLORS.white, transparency: isContainer ? 100 : 0, line: COLORS.warning, dash: "dash", text: COLORS.text };
    case "action":
      return { fill: COLORS.orange, line: COLORS.gray, dash: "solid", text: COLORS.text };
    case "result":
      return { fill: COLORS.blue, line: COLORS.gray, dash: "solid", text: COLORS.text };
    case "comparison_a":
      return { fill: COLORS.green, line: COLORS.gray, dash: "solid", text: COLORS.text };
    case "comparison_b":
      return { fill: COLORS.comparisonB, line: COLORS.gray, dash: "solid", text: COLORS.text };
    case "neutral":
    default:
      return { fill: COLORS.white, line: COLORS.gray, dash: "solid", text: COLORS.text };
  }
}

function addText(slide, text, rect, options = {}) {
  slide.addText(text, {
    x: rect.x,
    y: rect.y,
    w: rect.w,
    h: rect.h,
    fontFace: FONT,
    bold: options.bold ?? false,
    color: options.color ?? COLORS.text,
    align: options.align ?? "center",
    valign: options.valign ?? "mid",
    margin: options.margin ?? 0.03,
    fit: "shrink",
    breakLine: false,
    ...options,
    // Clamp last so no caller/spread pushes the nominal size below readability.
    fontSize: clampFont(options.fontSize ?? 12),
  });
}

function addRect(slide, rect, options = {}) {
  slide.addShape(SHAPE_TYPE.rect, {
    x: rect.x,
    y: rect.y,
    w: rect.w,
    h: rect.h,
    fill: options.fill ?? { color: COLORS.white },
    line: options.line ?? { color: COLORS.gray, width: 0.8 },
    shadow: options.shadow,
  });
}

function addLine(slide, x1, y1, x2, y2, options = {}) {
  slide.addShape(SHAPE_TYPE.line, {
    x: x1,
    y: y1,
    w: x2 - x1,
    h: y2 - y1,
    line: {
      color: options.color ?? COLORS.lightGray,
      width: options.width ?? 1,
      dashType: options.dashType ?? "solid",
      beginArrowType: options.beginArrowType ?? "none",
      endArrowType: options.endArrowType ?? "none",
    },
  });
}

function gridShape(count, preferredCols = 4) {
  if (count <= 0) return { cols: 1, rows: 1 };
  const cols = Math.min(preferredCols, count);
  return { cols, rows: Math.ceil(count / cols) };
}

// A group's items used to always stack in a single column, no matter how many
// there were — a group with 6 items (e.g. 6 model names under one "proposer"
// factor_group) needed 6x a single item's height, when 2 columns of 3 would
// need half that. Groups themselves already reflow via gridShape when a band
// has several of them; this gives items-within-one-group the same treatment.
// 1-3 items: single column (splitting 2-3 short items rarely saves useful
// height and just adds a second alignment axis). 4+: 2 columns.
function itemGridShape(count) {
  return gridShape(count, count >= 4 ? 2 : 1);
}

function estimateBandHeight(band) {
  if (band.groups.length > 0) {
    const { rows } = gridShape(band.groups.length, band.groups.length <= 4 ? 4 : 3);
    const maxItems = Math.max(...band.groups.map((g) => g.items.length));
    const oneGroupH = 0.44 + maxItems * 0.44 + Math.max(0, maxItems - 1) * 0.08 + 0.20;
    return 0.46 + rows * oneGroupH + Math.max(0, rows - 1) * 0.16 + 0.18;
  }
  if (band.nodes.length > 0) {
    const cols = band.layout === "column" ? 1 : Math.min(5, band.nodes.length);
    const rows = Math.ceil(band.nodes.length / cols);
    return 0.42 + rows * 0.50 + Math.max(0, rows - 1) * 0.10 + 0.15;
  }
  return 0.48;
}

// Bottom-up measurement of the height a band truly cannot go below without
// crushing its own content past the ITEM_H_FLOOR/NODE_H_FLOOR/title floors
// that computeBandLayout enforces at render time. Mirrors computeBandLayout's
// arithmetic exactly (same title/pad/gap constants, same itemGridShape choice
// for item columns) so measure and render can never disagree — see
// computeBandRects for why that agreement matters.
function measureBandMinHeight(band, scale) {
  if (band.groups.length === 0 && band.nodes.length === 0) {
    return SIMPLE_BAR_MIN_H;
  }

  const titleH = Math.max(0.27, 0.38 * scale);
  const titleGap = 0.08 * scale;
  const bottomPad = 0.12 * scale;
  let contentMinH = 0;

  if (band.groups.length > 0) {
    const preferredCols = band.groups.length <= 4 ? 4 : 3;
    const { rows } = gridShape(band.groups.length, preferredCols);
    const gy = 0.14 * scale;
    const groupTitleH = Math.max(0.25, 0.34 * scale);
    const itemGap = Math.max(0.05, 0.08 * scale);
    // All group cells in a band share one uniform row height (computeBandLayout
    // divides content.h evenly across rows), so the binding constraint is the
    // single neediest group, not a per-row max.
    let maxGroupMinH = 0;
    for (const group of band.groups) {
      const { rows: itemRows } = itemGridShape(group.items.length);
      const itemsMinH = itemRows * ITEM_H_FLOOR + Math.max(0, itemRows - 1) * itemGap;
      maxGroupMinH = Math.max(maxGroupMinH, groupTitleH + 0.05 * scale + 0.11 * scale + itemsMinH);
    }
    contentMinH = rows * maxGroupMinH + Math.max(0, rows - 1) * gy;
  }

  if (band.nodes.length > 0) {
    let cols;
    if (band.layout === "column") cols = 1;
    else if (band.layout === "row") cols = band.nodes.length;
    else cols = Math.min(5, band.nodes.length);
    cols = Math.max(1, cols);
    const rows = Math.ceil(band.nodes.length / cols);
    const ny = 0.10 * scale;
    contentMinH = Math.max(contentMinH, rows * NODE_H_FLOOR + Math.max(0, rows - 1) * ny);
  }

  return titleH + titleGap + bottomPad + contentMinH;
}

function computeBandRects(spec, bounds) {
  const gap = 0.24;
  const natural = spec.bands.map(estimateBandHeight);
  const totalNaturalBands = natural.reduce((a, b) => a + b, 0);
  const gapCount = natural.length - 1;
  const totalNatural = totalNaturalBands + gap * gapCount;
  // fillScale stretches (or shrinks) the bands to fill the region vertically — a
  // sparse diagram (few full-width bands) should occupy the whole region, not pack
  // at the top. The font/padding scale stays bounded so labels don't balloon.
  const fillScale = bounds.h / totalNatural;
  const scale = Math.max(0.6, Math.min(1.2, fillScale));
  // Gaps carry inter-band edge labels (a ~0.28"-tall pill). A dense diagram
  // (many bands, or a band with a big group grid) can push fillScale well
  // below 1, shrinking scaledGap until the label pill no longer fits and
  // collides with the neighboring band's border. Keep a legibility floor on
  // the gap and let band heights (not gaps) absorb the rest of the squeeze.
  const gapFloor = Math.min(gap, 0.34);
  const scaledGap = Math.max(gap * fillScale, gapFloor);

  // measure -> allocate -> render. The old code went straight from `natural`
  // (a rough weight, not a hard requirement) to each band's rect height via
  // bandFillScale, with no floor — so a dense diagram (many bands, or a band
  // with a big group grid) could compress a band's rect below what its own
  // content floors (ITEM_H_FLOOR/NODE_H_FLOOR/titleH, all enforced in
  // computeBandLayout/measureBandMinHeight) need. The rect would shrink but
  // the content inside it wouldn't, so content spilled into the next band —
  // bands visually overlapping. Measure the true per-band minimum first; if
  // it fits, distribute only the leftover slack by content weight (same
  // `natural` weighting as before). If it doesn't fit, refuse to render a
  // diagram that can only be legible or overlap-free but not both — throw
  // with an actionable breakdown instead of silently producing either.
  const bandMinHs = spec.bands.map((band) => measureBandMinHeight(band, scale));
  const totalMinH = bandMinHs.reduce((a, b) => a + b, 0) + gapFloor * gapCount;
  if (totalMinH > bounds.h + 0.01) {
    const breakdown = spec.bands
      .map((band, i) => ({ id: band.id, label: band.label, minH: bandMinHs[i] }))
      .sort((a, b) => b.minH - a.minH)
      .slice(0, 3)
      .map((b) => `  - ${b.id} (${b.label}): ${b.minH.toFixed(2)}"`)
      .join("\n");
    throw new Error(
      `[DIAGRAM-OVER-CAPACITY] 引擎已自动做完前两级降级——①平级 items 二维网格重排（4+ 项自动多列、` +
      `不压扁）②按内容量分配各 band 高度（不平均分）——但内容仍需至少 ${totalMinH.toFixed(2)}" 高，` +
      `本区只有 ${bounds.h.toFixed(2)}"（超 ${(totalMinH - bounds.h).toFixed(2)}"）。最占高的 band：\n${breakdown}\n` +
      `请在 YOUR slides.js 里按顺序调整——**绝不缩字，更不要合并/删除条目或用"等"省略（那是语义损失，禁止）**：\n` +
      `  ③ 扩大本视觉区的 h（若右侧文字栏更短，还可加宽 w：越宽每行放的列越多、行数越少越矮）；\n` +
      `  ④ 换一个更省高度的整页骨架（上下双图拆开分列、或复杂结构改单图整页 w:9）；\n` +
      `  ⑤ 以上都不够，才拆成两页、每页承载部分 band。\n` +
      `不要改 system-diagram.js 绕过本检查——它保证文字不会被压到不可读、band 不会重叠。`
    );
  }

  const bandBudget = bounds.h - scaledGap * gapCount;
  const totalMinBands = bandMinHs.reduce((a, b) => a + b, 0);
  const slack = Math.max(0, bandBudget - totalMinBands);
  let y = bounds.y;
  return spec.bands.map((band, i) => {
    const h = bandMinHs[i] + slack * (natural[i] / totalNaturalBands);
    const result = { band, rect: { x: bounds.x, y, w: bounds.w, h }, scale };
    y += h + scaledGap;
    return result;
  });
}

function computeBandLayout(entry) {
  const { band, rect, scale } = entry;
  const titleH = Math.max(0.27, 0.38 * scale);
  const pad = Math.max(0.10, 0.16 * scale);
  const childLayouts = [];
  const anchors = new Map([[band.id, rect]]);

  if (band.groups.length === 0 && band.nodes.length === 0) {
    return { ...entry, titleRect: rect, childLayouts, anchors, simpleBar: true };
  }

  const titleRect = { x: rect.x + pad, y: rect.y + 0.02, w: rect.w - 2 * pad, h: titleH };
  const content = {
    x: rect.x + pad,
    y: rect.y + titleH + 0.08 * scale,
    w: rect.w - 2 * pad,
    h: rect.h - titleH - 0.12 * scale,
  };

  if (band.groups.length > 0) {
    const preferredCols = band.groups.length <= 4 ? 4 : 3;
    const { cols, rows } = gridShape(band.groups.length, preferredCols);
    const gx = 0.15 * scale;
    const gy = 0.14 * scale;
    const groupW = (content.w - gx * (cols - 1)) / cols;
    const groupH = (content.h - gy * (rows - 1)) / rows;

    band.groups.forEach((group, index) => {
      const row = Math.floor(index / cols);
      const col = index % cols;
      const groupRect = {
        x: content.x + col * (groupW + gx),
        y: content.y + row * (groupH + gy),
        w: groupW,
        h: groupH,
      };
      anchors.set(group.id, groupRect);

      const groupTitleH = Math.max(0.25, 0.34 * scale);
      const itemPad = Math.max(0.07, 0.12 * scale);
      const itemGap = Math.max(0.05, 0.08 * scale);
      const itemsAvailable = groupRect.h - groupTitleH - 0.11 * scale;
      // Items used to always stack in a single column (itemGridShape reflows
      // 4+ items into 2 columns — see its comment), which is what made a
      // 6-item group need 6x a single item's height instead of 3x.
      const itemShape = itemGridShape(group.items.length);
      // Legibility floor (ITEM_H_FLOOR), same idiom as titleH/groupTitleH above:
      // itemH used to be a bare Math.min (capped above, unbounded below), so a
      // dense diagram could shrink it toward zero; addText hardcodes
      // fit:"shrink", so a near-zero box crushed the item's fontSize far below
      // its nominal value at render time. computeBandRects now measures this
      // same floor before allocating band heights, so in practice the band
      // arrives with enough room — this Math.max is the last-line guarantee
      // that itemH itself never goes below what fontSize needs, independent of
      // upstream allocation.
      const itemH = Math.max(ITEM_H_FLOOR, Math.min(0.47 * scale, (itemsAvailable - itemGap * (itemShape.rows - 1)) / itemShape.rows));
      const itemGx = 0.08 * scale;
      const itemW = (groupRect.w - 2 * itemPad - itemGx * (itemShape.cols - 1)) / itemShape.cols;
      // Center the item grid in the slot instead of pinning it to the top —
      // sibling groups in the same row share groupRect.h (the row's tallest
      // group sets it), so a sparser group would otherwise strand its grid at
      // the top with dead space below.
      const stackH = itemShape.rows * itemH + itemGap * (itemShape.rows - 1);
      const itemsTop = groupRect.y + groupTitleH + 0.05 * scale + Math.max(0, (itemsAvailable - stackH) / 2);

      const itemLayouts = group.items.map((item, itemIndex) => {
        const row = Math.floor(itemIndex / itemShape.cols);
        const col = itemIndex % itemShape.cols;
        const itemRect = {
          x: groupRect.x + itemPad + col * (itemW + itemGx),
          y: itemsTop + row * (itemH + itemGap),
          w: itemW,
          h: itemH,
        };
        anchors.set(item.id, itemRect);
        return { item, rect: itemRect };
      });

      childLayouts.push({ type: "group", group, rect: groupRect, itemLayouts, scale });
    });
  }

  if (band.nodes.length > 0) {
    let cols;
    if (band.layout === "column") cols = 1;
    else if (band.layout === "row") cols = band.nodes.length;
    else cols = Math.min(5, band.nodes.length);
    cols = Math.max(1, cols);
    const rows = Math.ceil(band.nodes.length / cols);
    const nx = 0.14 * scale;
    const ny = 0.10 * scale;
    const nodeW = (content.w - nx * (cols - 1)) / cols;
    // Same legibility floor (NODE_H_FLOOR) as itemH above, and the same
    // last-line-guarantee framing — computeBandRects measures this floor
    // before allocating band heights, so it's expected to already have room.
    const nodeH = Math.max(NODE_H_FLOOR, Math.min(0.50 * scale, (content.h - ny * (rows - 1)) / rows));
    // Center the node grid vertically — a sparse band (few nodes, tall region)
    // would otherwise strand its grid at the top with dead space below.
    const gridH = rows * nodeH + ny * (rows - 1);
    const gridTop = content.y + Math.max(0, (content.h - gridH) / 2);

    band.nodes.forEach((node, index) => {
      const row = Math.floor(index / cols);
      const col = index % cols;
      const nodeRect = {
        x: content.x + col * (nodeW + nx),
        y: gridTop + row * (nodeH + ny),
        w: nodeW,
        h: nodeH,
      };
      anchors.set(node.id, nodeRect);
      childLayouts.push({ type: "node", node, rect: nodeRect, scale });
    });
  }

  return { ...entry, titleRect, childLayouts, anchors, simpleBar: false };
}

function rectCenter(rect) {
  return { x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 };
}

function edgeEndpoints(source, target) {
  const sc = rectCenter(source);
  const tc = rectCenter(target);
  if (tc.y >= sc.y) return { x1: sc.x, y1: source.y + source.h, x2: tc.x, y2: target.y };
  return { x1: sc.x, y1: source.y, x2: tc.x, y2: target.y + target.h };
}

function drawEdgeLabel(slide, label, x, y, emphasis, scale = 1) {
  if (!label) return;
  const w = Math.max(0.85, Math.min(2.2, label.length * 0.13 + 0.34));
  const h = 0.28;
  addRect(slide, { x: x - w / 2, y: y - h / 2, w, h }, {
    fill: { color: COLORS.white },
    line: { color: COLORS.white, transparency: 100 },
  });
  addText(slide, label, { x: x - w / 2, y: y - h / 2, w, h }, {
    fontSize: 10.5 * scale,
    color: emphasis === "warning" ? COLORS.warning : COLORS.text,
    bold: emphasis === "warning",
  });
}

// Loop-back edges (left_loop/right_loop) run their label along a rail that
// hugs the page margin — typically ~0.5" wide, nowhere near enough for a
// horizontal pill (a 10+ char label needs ~1.5-2.2"). A horizontal label
// there either gets clipped by the page edge or (once clamped on-slide)
// lands underneath the diagram's own band rects, which are drawn after
// edges and paint over it. Setting the label vertical along the rail is the
// standard fix for tight-margin loop-back labels.
function drawLoopEdgeLabel(slide, label, railX, railY1, railY2, emphasis, scale = 1) {
  if (!label) return;
  const margin = 0.04;
  const railLen = Math.abs(railY2 - railY1);
  const h = Math.max(0.6, Math.min(railLen * 0.85, label.length * 0.13 + 0.34));
  const w = 0.30;
  const cy = (railY1 + railY2) / 2;
  const rx = Math.max(margin, Math.min(railX - w / 2, PAGE_W - w - margin));
  const ry = Math.max(margin, Math.min(cy - h / 2, PAGE_H - h - margin));
  addRect(slide, { x: rx, y: ry, w, h }, {
    fill: { color: COLORS.white },
    line: { color: COLORS.white, transparency: 100 },
  });
  addText(slide, label, { x: rx, y: ry, w, h }, {
    fontSize: 9 * scale,
    color: emphasis === "warning" ? COLORS.warning : COLORS.text,
    bold: emphasis === "warning",
    textDirection: "vert270",
  });
}

function drawEdge(slide, edge, anchors, spec, bounds) {
  const source = anchors.get(edge.from);
  const target = anchors.get(edge.to);
  if (!source || !target) return;

  // "normal" edges now use darkGray, not lightGray — lightGray reads as nearly
  // invisible at 1pt against a white slide, which made every un-emphasized
  // connector (the majority of edges) disappear on render.
  const color = edge.emphasis === "warning" ? COLORS.warning : COLORS.darkGray;
  const width = edge.emphasis === "strong" || edge.emphasis === "warning" ? 1.35 : 1.1;
  const dashType = edge.line_style === "dashed" ? "dash" : "solid";
  const beginArrowType = edge.direction === "bidirectional" ? "triangle" : "none";
  const endArrowType = edge.direction === "none" ? "none" : "triangle";

  if (edge.routing === "right_loop" || edge.routing === "left_loop") {
    const right = edge.routing === "right_loop";
    const margin = 0.15;
    // Clamp the loop's outer rail to stay on-slide — an unclamped loopX can
    // push past x=0 (or past the page's right edge) when bounds.x is small,
    // physically cutting off the edge label's text against the slide boundary.
    const loopX = right
      ? Math.min(bounds.x + bounds.w + 0.28, PAGE_W - margin)
      : Math.max(bounds.x - 0.28, margin);
    const sx = right ? source.x + source.w : source.x;
    const tx = right ? target.x + target.w : target.x;
    const sy = source.y + source.h / 2;
    const ty = target.y + target.h / 2;
    addLine(slide, sx, sy, loopX, sy, { color, width, dashType });
    addLine(slide, loopX, sy, loopX, ty, { color, width, dashType });
    addLine(slide, loopX, ty, tx, ty, { color, width, dashType, endArrowType });
    drawLoopEdgeLabel(slide, edge.label, loopX, sy, ty, edge.emphasis, 0.9);
    return;
  }

  const endpoints = edgeEndpoints(source, target);

  if (edge.direction === "bidirectional" && edge.multiplicity === "repeated") {
    const count = 8;
    // 纵向是唯一排布方式，不再判 orientation：以前写 `spec.orientation === "top_to_bottom"`，
    // 而缺省时该字段是 undefined，多箭头分支实际上从来没走进来过。
    if (Math.abs(source.w - target.w) < 0.6) {
      for (let i = 0; i < count; i += 1) {
        const x = source.x + 0.72 + i * ((source.w - 1.44) / (count - 1));
        const down = i % 2 === 1;
        const yTop = source.y + source.h + 0.02;
        const yBottom = target.y - 0.02;
        if (down) addLine(slide, x, yTop, x, yBottom, { color, width, dashType, endArrowType: "triangle" });
        else addLine(slide, x, yBottom, x, yTop, { color, width, dashType, endArrowType: "triangle" });
      }
    } else {
      addLine(slide, endpoints.x1, endpoints.y1, endpoints.x2, endpoints.y2, {
        color, width, dashType, beginArrowType, endArrowType,
      });
    }
    drawEdgeLabel(slide, edge.label, (endpoints.x1 + endpoints.x2) / 2, (endpoints.y1 + endpoints.y2) / 2, edge.emphasis);
    return;
  }

  if (edge.routing === "orthogonal") {
    const midY = (endpoints.y1 + endpoints.y2) / 2;
    addLine(slide, endpoints.x1, endpoints.y1, endpoints.x1, midY, { color, width, dashType, beginArrowType });
    addLine(slide, endpoints.x1, midY, endpoints.x2, midY, { color, width, dashType });
    addLine(slide, endpoints.x2, midY, endpoints.x2, endpoints.y2, { color, width, dashType, endArrowType });
    drawEdgeLabel(slide, edge.label, (endpoints.x1 + endpoints.x2) / 2, midY, edge.emphasis);
    return;
  }

  addLine(slide, endpoints.x1, endpoints.y1, endpoints.x2, endpoints.y2, {
    color, width, dashType, beginArrowType, endArrowType,
  });
  drawEdgeLabel(slide, edge.label, (endpoints.x1 + endpoints.x2) / 2, (endpoints.y1 + endpoints.y2) / 2, edge.emphasis);
}

function drawBand(slide, layout) {
  const { band, rect, titleRect, childLayouts, simpleBar, scale } = layout;
  const style = styleForVisualRole(band.visual_role, !simpleBar);

  if (simpleBar) {
    addRect(slide, rect, {
      fill: { color: style.fill, transparency: style.transparency ?? 0 },
      line: { color: style.line, width: band.visual_role === "warning" ? 1.5 : 0.8, dashType: style.dash },
    });
    addText(slide, band.label, rect, {
      fontSize: 13.5 * Math.max(0.78, scale),
      bold: band.visual_role === "warning",
    });
    return;
  }

  addRect(slide, rect, {
    fill: { color: style.fill, transparency: style.transparency ?? (band.visual_role === "warning" ? 100 : 72) },
    line: { color: style.line, width: band.visual_role === "warning" ? 1.5 : 0.8, dashType: style.dash },
  });

  // Band titles render straight onto the band's own fill — no separate
  // opaque white backing plate. That backing used to draw as a hard-edged
  // white rectangle sitting on top of the band's pale tint (bands default to
  // 72% transparency, i.e. a faint wash), which reads as a visible sticker
  // rather than a title bar. Bold text is legible directly on the tint (and
  // on "warning" bands, which have no fill at all — text sits on the white
  // canvas either way), so the backing was pure liability, no benefit.
  const titleBacking = { x: rect.x, y: titleRect.y, w: rect.w, h: titleRect.h };
  addText(slide, band.label, titleBacking, {
    fontSize: 12.6 * Math.max(0.78, scale),
    bold: true,
  });

  for (const child of childLayouts) {
    if (child.type === "group") {
      const groupStyle = styleForVisualRole(child.group.visual_role, true);
      addRect(slide, child.rect, {
        fill: { color: groupStyle.fill, transparency: groupStyle.transparency ?? 0 },
        line: { color: groupStyle.line, width: 0.75, dashType: groupStyle.dash },
      });
      addText(slide, child.group.title, {
        x: child.rect.x + 0.04,
        y: child.rect.y + 0.02,
        w: child.rect.w - 0.08,
        h: Math.max(0.24, 0.32 * child.scale),
      }, {
        fontSize: 10.9 * Math.max(0.78, child.scale),
        bold: false,
      });

      for (const itemLayout of child.itemLayouts) {
        const itemStyle = styleForVisualRole(itemLayout.item.visual_role, false);
        addRect(slide, itemLayout.rect, {
          fill: { color: itemStyle.fill, transparency: itemStyle.transparency ?? 0 },
          line: { color: itemStyle.line, width: 0.75, dashType: itemStyle.dash },
        });
        addText(slide, itemLayout.item.label, itemLayout.rect, {
          fontSize: (itemLayout.item.label.length > 10 ? 9.2 : 10.2) * Math.max(0.78, child.scale),
          margin: 0.02,
        });
      }
    } else if (child.type === "node") {
      const nodeStyle = styleForVisualRole(child.node.visual_role, false);
      addRect(slide, child.rect, {
        fill: { color: nodeStyle.fill, transparency: nodeStyle.transparency ?? 0 },
        line: { color: nodeStyle.line, width: 0.8, dashType: nodeStyle.dash },
      });
      addText(slide, child.node.label, child.rect, {
        fontSize: (child.node.label.length > 10 ? 9.5 : 10.8) * Math.max(0.78, child.scale),
      });
    }
  }
}

function computeLayouts(spec, bounds) {
  const bandEntries = computeBandRects(spec, bounds);
  const layouts = bandEntries.map(computeBandLayout);
  const anchors = new Map();
  for (const layout of layouts) {
    for (const [id, rect] of layout.anchors.entries()) anchors.set(id, rect);
  }
  return { layouts, anchors };
}

function rectsOverlap(a, b) {
  const pad = 0.02;
  return a.x < b.x + b.w - pad && a.x + a.w > b.x + pad &&
         a.y < b.y + b.h - pad && a.y + a.h > b.y + pad;
}

// Draw a system diagram into a region of an existing slide, using native shapes.
// No title / core_message chrome — the host content slide owns those. Returns a
// layout descriptor { bounds, nodes, groups, edgeLabels, warnings } so QA and the
// diagram-verifier can inspect geometry without re-parsing the PPTX.
function renderSystemDiagram({ pres, slide, spec, bounds, theme = DEFAULT_THEME } = {}) {
  if (!pres || !slide) throw new Error("renderSystemDiagram requires { pres, slide }.");
  SHAPE_TYPE = pres.ShapeType;
  COLORS = themeToColors(theme);
  FONT_MIN = theme.fontMin ?? DEFAULT_THEME.fontMin;
  FONT_MAX = theme.fontMax ?? DEFAULT_THEME.fontMax;
  PAGE_W = theme.pageW ?? DEFAULT_THEME.pageW;
  PAGE_H = theme.pageH ?? DEFAULT_THEME.pageH;

  const normalized = normalizeSpec(spec);
  const region = bounds || { x: 0.5, y: 1.05, w: 9.0, h: 3.6 };
  const { layouts, anchors } = computeLayouts(normalized, region);

  // Edges first so node boxes sit above connector lines; labels live in the
  // inter-band gaps, not over nodes. Bands (containers + leaves) drawn last.
  for (const edge of normalized.edges) drawEdge(slide, edge, anchors, normalized, region);
  for (const layout of layouts) drawBand(slide, layout);

  const nodes = [];
  const groups = [];
  for (const band of normalized.bands) {
    for (const node of band.nodes) {
      if (anchors.has(node.id)) nodes.push({ id: node.id, rect: anchors.get(node.id) });
    }
    for (const group of band.groups) {
      if (anchors.has(group.id)) groups.push({ id: group.id, rect: anchors.get(group.id) });
      for (const item of group.items) {
        if (anchors.has(item.id)) nodes.push({ id: item.id, rect: anchors.get(item.id) });
      }
    }
  }
  const warnings = [];
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      if (rectsOverlap(nodes[i].rect, nodes[j].rect)) {
        warnings.push(`nodes '${nodes[i].id}' & '${nodes[j].id}' overlap`);
      }
    }
  }
  return { bounds: region, nodes, groups, edgeLabels: [], warnings };
}

// ---- Diagram IR 构造器 ----------------------------------------------------
// 裸 IR 要求每个 band/group/item 自带唯一 id，并重复 nodes:[] / groups:[] 样板。
// 这是纯记账负担，且密度越高税越重——实测两份同题 deck：自建构造器的那份平均每图
// 8.3 个条目、2 张带嵌套 groups；手搓裸 IR 的那份 6.7 个、零嵌套，稀疏到出废图。
// 构造器把记账收进库里，让「写密集内容」变便宜。刻意不发射 `role`：渲染器不读它、
// 文档不提它、normalizeSpec 不校验它。
function item(id, label, visual_role = "neutral") {
  return { id, label, visual_role };
}

// labels 收字符串数组（item id 自动派生为 `${id}_1`、`${id}_2`…）。数组元素也可
// 直接传 item() 对象——只为给单项定制 visual_role，避免「需要一个 warning 单项就得
// 放弃构造器、手写整个 group」的悬崖。
function group(id, title, labels, visual_role = "factor_group") {
  return {
    id,
    title,
    visual_role,
    items: labels.map((l, i) => (typeof l === "string" ? item(`${id}_${i + 1}`, l) : l)),
  };
}

function bandNode(id, label, nodes, visual_role = "neutral", layout = "row") {
  return { id, label, visual_role, layout, nodes, groups: [] };
}

function groupBand(id, label, groups, visual_role = "factor_group") {
  return { id, label, visual_role, layout: "grid", nodes: [], groups };
}

module.exports = { renderSystemDiagram, normalizeSpec, DEFAULT_THEME, item, group, bandNode, groupBand };

