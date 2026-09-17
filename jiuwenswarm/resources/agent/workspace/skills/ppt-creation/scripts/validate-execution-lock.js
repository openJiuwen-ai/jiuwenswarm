#!/usr/bin/env node

// Dependency-free semantic validator for a deck execution-lock.json.
// JSON Schema documents the shape; this script also checks brand constants,
// page-level strategy, and asset readiness.

const fs = require("fs");
const path = require("path");
const { validateEvidencePlan } = require("./validate-evidence-plan.js");

const args = process.argv.slice(2);
const phaseIndex = args.indexOf("--phase");
const phase = phaseIndex >= 0 ? args[phaseIndex + 1] : "design";
const lockArg = args.find((arg, index) => arg !== "--phase" && index !== phaseIndex + 1);

if (!lockArg || !["design", "generate", "deliver"].includes(phase)) {
  console.error("Usage: node validate-execution-lock.js <execution-lock.json> [--phase design|generate|deliver]");
  process.exit(2);
}

const lockPath = path.resolve(lockArg);
const projectRoot = path.dirname(lockPath);
const skillRoot = path.resolve(__dirname, "..");

// Task workspaces are forbidden inside the skill directory: the skill tree is
// shared reference material for every run, and sync tooling only guarantees
// its own mirrored content there.
if (lockPath.startsWith(skillRoot + path.sep)) {
  console.error(
    `[ERROR] task workspace is inside the skill directory (${skillRoot}). ` +
    "Move the whole workspace outside skills/ppt-creation (e.g. <workspace>/projects/<task>/) and rerun."
  );
  process.exit(1);
}
const errors = [];
const warnings = [];

function error(message) { errors.push(message); }
function warn(message) { warnings.push(message); }
function at(value, keys) {
  return keys.reduce((current, key) => current && current[key], value);
}
function expect(actual, expected, label) {
  if (actual !== expected) error(`${label} must be ${JSON.stringify(expected)}; got ${JSON.stringify(actual)}`);
}
function nonEmpty(value) { return typeof value === "string" && value.trim().length > 0; }

let lock;
try {
  lock = JSON.parse(fs.readFileSync(lockPath, "utf8"));
} catch (cause) {
  console.error(`[ERROR] Cannot parse ${lockPath}: ${cause.message}`);
  process.exit(1);
}

if (![1, 2].includes(lock.version)) error(`version must be 1 or 2; got ${JSON.stringify(lock.version)}`);
expect(at(lock, ["template", "content_shell"]), "blank-content", "template.content_shell");
if (!["template-master", "10x5.625-compatible"].includes(at(lock, ["template", "canvas_mode"]))) {
  error("template.canvas_mode must be template-master or 10x5.625-compatible");
}
const templateSource = at(lock, ["template", "source"]);
const templatePath = [
  path.resolve(projectRoot, templateSource || ""),
  path.resolve(skillRoot, templateSource || ""),
].find((candidate) => templateSource && fs.existsSync(candidate));
if (!templatePath) error(`template.source does not exist in the project or skill: ${JSON.stringify(templateSource)}`);
if (at(lock, ["template", "canvas_mode"]) === "template-master") {
  expect(at(lock, ["template", "authoring_canvas"]), "10x5.625-scaled", "template.authoring_canvas");
  expect(at(lock, ["template", "merge_mode"]), "template-layout", "template.merge_mode");
  expect(at(lock, ["template", "content_layout"]), "slideLayout7.xml", "template.content_layout");
  expect(at(lock, ["template", "footer_mode"]), "master", "template.footer_mode");
}

// Kept in sync with base/execution-lock-reference.json and THEME in
// scripts/components.js. Rethemeing the deck means changing all three.
const brandConstants = [
  [["brand", "colors", "primary"], "4472C4"],
  [["brand", "colors", "secondary"], "8FAADC"],
  [["brand", "colors", "text"], "1D1D1A"],
  [["brand", "colors", "secondary_text"], "666666"],
  [["brand", "colors", "background"], "FFFFFF"],
  [["brand", "typography", "cjk"], "Microsoft YaHei"],
  [["brand", "typography", "latin"], "Arial"],
  [["brand", "footer", "required_on"], "standard-content"],
  [["brand", "summary_banner", "required_on"], "standard-content"],
  [["brand", "summary_banner", "fill"], "4472C4"],
  [["brand", "summary_banner", "text_color"], "FFFFFF"],
  [["brand", "summary_banner", "position"], "above-footer"],
];
for (const [keys, expected] of brandConstants) expect(at(lock, keys), expected, keys.join("."));

const narrativeModes = new Set([
  "executive-report", "technical-explainer", "research-review", "showcase", "briefing",
]);
if (!narrativeModes.has(at(lock, ["deck", "narrative_mode"]))) {
  error("deck.narrative_mode is missing or unsupported");
}
const typography = at(lock, ["deck", "typography_policy"]) || {};
if (!(typography.title_min_pt >= 20)) error("deck.typography_policy.title_min_pt must be at least 20");
if (!(typography.body_min_pt >= 9)) error("deck.typography_policy.body_min_pt must be at least 9");
if (!(typography.absolute_min_pt >= 7)) error("deck.typography_policy.absolute_min_pt must be at least 7");

const referenceIndexPath = path.join(skillRoot, "references", "index.yaml");
const knownReferenceIds = new Set();
if (fs.existsSync(referenceIndexPath)) {
  const indexText = fs.readFileSync(referenceIndexPath, "utf8");
  for (const match of indexText.matchAll(/^\s*-\s+id:\s*([^\s#]+)/gm)) knownReferenceIds.add(match[1]);
}

// The bundled icon libraries were removed from this skill. Old locks may still
// carry a deck.icons block; only library "none" (or no block at all) is valid.
const iconConfig = at(lock, ["deck", "icons"]);
if (iconConfig != null && iconConfig.library !== "none") {
  error(
    `deck.icons.library ${JSON.stringify(iconConfig.library)} is unsupported: ` +
    "the bundled icon libraries were removed; omit deck.icons or set library to \"none\""
  );
}
if (iconConfig != null && (iconConfig.inventory || []).length) {
  error("deck.icons.inventory must be empty: the bundled icon libraries were removed");
}

const roles = new Set(["fixed-cover", "toc", "standard-content", "section", "fixed-closing"]);
const rhythms = new Set(["anchor", "navigation", "dense"]);
const strategies = new Set([
  "template", "user-material", "paper", "web", "native-drawing", "component", "svg", "hybrid", "none",
]);
const componentPolicies = new Set(["optional", "preferred", "avoid"]);
const pages = Array.isArray(lock.pages) ? lock.pages : [];
if (!pages.length) error("pages must contain at least one page contract");
const pageIds = new Set();

for (const page of pages) {
  const id = page && page.id;
  if (!/^P\d{2,3}$/.test(id || "")) error(`Invalid page id: ${JSON.stringify(id)}`);
  if (pageIds.has(id)) error(`Duplicate page id: ${id}`);
  pageIds.add(id);
  if (!roles.has(page.role)) error(`${id || "page"}.role is missing or unsupported`);
  if (!rhythms.has(page.rhythm)) error(`${id || "page"}.rhythm is missing or unsupported`);
  if (!nonEmpty(page.core_message)) error(`${id || "page"}.core_message must be a non-empty string`);
  if (!componentPolicies.has(page.component_policy)) error(`${id || "page"}.component_policy is missing or unsupported`);
  if (!Array.isArray(page.reference_ids)) error(`${id || "page"}.reference_ids must be an array`);
  if (!nonEmpty(page.composition)) error(`${id || "page"}.composition must describe the intended visual hierarchy`);
  const evidenceKind = at(page, ["evidence_visual", "kind"]);
  const evidenceAssetIds = at(page, ["evidence_visual", "asset_ids"]);
  const evidenceKinds = new Set([
    "template", "user-material", "paper", "product-screenshot", "code-screenshot",
    "official-diagram", "data-chart", "native-diagram", "none",
  ]);
  if (!evidenceKinds.has(evidenceKind)) error(`${id || "page"}.evidence_visual.kind is missing or unsupported`);
  if (!Array.isArray(evidenceAssetIds)) error(`${id || "page"}.evidence_visual.asset_ids must be an array`);
  for (const referenceId of page.reference_ids || []) {
    if (!knownReferenceIds.has(referenceId)) error(`${id}.reference_ids contains unknown reference: ${referenceId}`);
  }
  const primary = at(page, ["visual_strategy", "primary"]);
  const fallback = at(page, ["visual_strategy", "fallback"]);
  if (!strategies.has(primary)) error(`${id || "page"}.visual_strategy.primary is missing or unsupported`);
  if (!strategies.has(fallback) || fallback === "hybrid") error(`${id || "page"}.visual_strategy.fallback is missing or unsupported`);

  if (page.role === "standard-content" && !nonEmpty(page.summary)) {
    error(`${id}.summary is required for standard-content pages`);
  }
  if (page.role !== "standard-content" && page.summary != null && String(page.summary).trim()) {
    warn(`${id}.summary is ignored because ${page.role} does not use the standard content chrome`);
  }
  if (["fixed-cover", "fixed-closing"].includes(page.role) && primary !== "template") {
    error(`${id}.visual_strategy.primary must be template for ${page.role}`);
  }
  if (page.role === "standard-content" && primary === "template") {
    error(`${id}.visual_strategy.primary cannot be template for a standard content page`);
  }
  if (page.role === "standard-content" && ["dense", "anchor"].includes(page.rhythm)) {
    if (!(page.reference_ids || []).length && !nonEmpty(page.reference_waiver)) {
      error(`${id} is a ${page.rhythm} content page and requires reference_ids or a specific reference_waiver`);
    }
    if (evidenceKind === "none") {
      error(`${id} is a ${page.rhythm} content page and requires an evidence_visual strategy`);
    }
  }
}

let evidenceItemsById = null;
if (lock.version === 2) {
  if (!nonEmpty(lock.evidence_plan)) {
    error("version 2 requires evidence_plan");
  } else {
    const evidencePath = path.resolve(projectRoot, lock.evidence_plan);
    if (!fs.existsSync(evidencePath)) {
      error(`evidence_plan does not exist: ${lock.evidence_plan}`);
    } else {
      const result = validateEvidencePlan(evidencePath, { phase, pageIds });
      evidenceItemsById = result.itemsById;
      for (const message of result.warnings) warn(`evidence-plan: ${message}`);
      for (const message of result.errors) error(`evidence-plan: ${message}`);
    }
  }
} else {
  warn("execution-lock version 1 uses the legacy inline assets list; migrate to version 2 + evidence-plan.json");
}

if (lock.version === 1 && !Array.isArray(lock.assets)) error("assets must be an array");
const assets = Array.isArray(lock.assets) ? lock.assets : [];
const assetIds = new Set();
for (const asset of assets) {
  if (!nonEmpty(asset.id)) { error("Every asset requires a non-empty id"); continue; }
  if (assetIds.has(asset.id)) error(`Duplicate asset id: ${asset.id}`);
  assetIds.add(asset.id);
  if (!pageIds.has(asset.page)) error(`Asset ${asset.id} refers to unknown page ${asset.page}`);
  if (!["user", "paper", "web", "ai", "icon", "diagram", "screenshot", "code", "logo", "other"].includes(asset.kind)) {
    error(`Asset ${asset.id} has unsupported kind ${JSON.stringify(asset.kind)}`);
  }
  if (!["planned", "acquiring", "ready", "used", "needs-manual", "skipped"].includes(asset.status)) {
    error(`Asset ${asset.id} has unsupported status ${JSON.stringify(asset.status)}`);
  }
  if (!nonEmpty(asset.fallback)) error(`Asset ${asset.id} requires a fallback`);
  if (["ready", "used"].includes(asset.status)) {
    if (!nonEmpty(asset.path)) error(`Asset ${asset.id} is ${asset.status} but has no path`);
    else if (!fs.existsSync(path.resolve(projectRoot, asset.path))) {
      error(`Asset ${asset.id} path does not exist: ${asset.path}`);
    }
  }
  if (["web", "paper"].includes(asset.kind) && asset.status === "used") {
    if (!nonEmpty(asset.source)) error(`Used ${asset.kind} asset ${asset.id} requires source`);
    if (!nonEmpty(asset.license)) warn(`Used ${asset.kind} asset ${asset.id} has no license/rights note`);
  }
  if (phase !== "design" && asset.required && ["planned", "acquiring", "needs-manual"].includes(asset.status)) {
    error(`Required asset ${asset.id} is not ready for ${phase}: ${asset.status}`);
  }
  if (phase === "deliver" && asset.required && asset.status !== "used") {
    error(`Required asset ${asset.id} must be used before delivery; got ${asset.status}`);
  }
}

for (const page of pages) {
  const referenced = at(page, ["evidence_visual", "asset_ids"]) || [];
  for (const assetId of referenced) {
    const known = evidenceItemsById ? evidenceItemsById.has(assetId) : assetIds.has(assetId);
    if (!known) error(`${page.id}.evidence_visual refers to unknown evidence item ${assetId}`);
    if (evidenceItemsById && evidenceItemsById.has(assetId) && evidenceItemsById.get(assetId).page !== page.id) {
      error(`${page.id}.evidence_visual refers to ${assetId}, which belongs to ${evidenceItemsById.get(assetId).page}`);
    }
  }
  const kind = at(page, ["evidence_visual", "kind"]);
  if (["paper", "product-screenshot", "code-screenshot", "official-diagram", "data-chart", "user-material"].includes(kind) && !referenced.length) {
    error(`${page.id}.evidence_visual.kind=${kind} requires at least one evidence asset_id`);
  }
}

for (const message of warnings) console.warn(`[WARN] ${message}`);
for (const message of errors) console.error(`[ERROR] ${message}`);
console.log(`execution-lock: ${errors.length} error(s), ${warnings.length} warning(s), phase=${phase}`);
process.exit(errors.length ? 1 : 0);
