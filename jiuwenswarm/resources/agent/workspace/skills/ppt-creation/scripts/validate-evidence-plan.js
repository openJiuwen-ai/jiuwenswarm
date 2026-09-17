#!/usr/bin/env node

const fs = require("fs");
const path = require("path");

const FILE_ROUTES = new Set([
  "user", "source-extracted", "paper-figure", "product-screenshot",
  "code-screenshot", "official-logo", "web", "ai-illustration", "formula", "manual",
]);
const ROUTES = new Set([...FILE_ROUTES, "native-chart", "native-drawing"]);
const KINDS = new Set([
  "user-material", "paper-figure", "paper-table", "product-screenshot",
  "code-screenshot", "official-logo", "data-chart", "web-image",
  "ai-illustration", "formula", "native-diagram", "other",
]);
const STATUSES = new Set(["planned", "acquiring", "ready", "used", "needs-manual", "skipped"]);
const REVIEW_STATUSES = new Set(["pending", "approved", "rejected", "not-required"]);
const PLACEMENT_ROLES = new Set(["hero", "evidence", "support", "background", "inline"]);

function nonEmpty(value) { return typeof value === "string" && value.trim().length > 0; }
function isWithin(parent, child) {
  const relative = path.relative(parent, child);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function validateEvidencePlan(planPath, { phase = "design", pageIds = null } = {}) {
  const resolved = path.resolve(planPath);
  const projectRoot = path.dirname(resolved);
  const errors = [];
  const warnings = [];
  let plan;
  try {
    plan = JSON.parse(fs.readFileSync(resolved, "utf8"));
  } catch (cause) {
    return { plan: null, itemsById: new Map(), errors: [`Cannot parse evidence plan: ${cause.message}`], warnings };
  }
  if (plan.version !== 1) errors.push(`evidence plan version must be 1; got ${JSON.stringify(plan.version)}`);
  if (!nonEmpty(plan.asset_root)) errors.push("asset_root must be a non-empty string");
  if (!nonEmpty(plan.analysis_root)) errors.push("analysis_root must be a non-empty string");
  if (!Array.isArray(plan.items)) errors.push("items must be an array");
  const assetRoot = path.resolve(projectRoot, nonEmpty(plan.asset_root) ? plan.asset_root : "assets");
  const analysisRoot = path.resolve(projectRoot, nonEmpty(plan.analysis_root) ? plan.analysis_root : "analysis");
  if (path.isAbsolute(plan.asset_root || "") || !isWithin(projectRoot, assetRoot)) {
    errors.push("asset_root must be a project-relative directory");
  }
  if (path.isAbsolute(plan.analysis_root || "") || !isWithin(projectRoot, analysisRoot)) {
    errors.push("analysis_root must be a project-relative directory");
  }
  const items = Array.isArray(plan.items) ? plan.items : [];
  const itemsById = new Map();

  for (const item of items) {
    const id = item && item.id;
    if (!/^[a-z0-9][a-z0-9-]*$/.test(id || "")) errors.push(`Invalid evidence id: ${JSON.stringify(id)}`);
    if (itemsById.has(id)) errors.push(`Duplicate evidence id: ${id}`);
    itemsById.set(id, item);
    if (!/^P\d{2,3}$/.test(item.page || "")) errors.push(`${id}.page is invalid`);
    if (pageIds && !pageIds.has(item.page)) errors.push(`${id}.page refers to unknown page ${item.page}`);
    for (const field of ["claim", "purpose", "reference", "fallback"]) {
      if (!nonEmpty(item[field])) errors.push(`${id}.${field} must be a non-empty string`);
    }
    if (!KINDS.has(item.kind)) errors.push(`${id}.kind is unsupported: ${JSON.stringify(item.kind)}`);
    if (!ROUTES.has(item.acquire_via)) errors.push(`${id}.acquire_via is unsupported: ${JSON.stringify(item.acquire_via)}`);
    if (typeof item.required !== "boolean") errors.push(`${id}.required must be boolean`);
    if (!PLACEMENT_ROLES.has(item.placement_role)) errors.push(`${id}.placement_role is unsupported`);
    if (!STATUSES.has(item.status)) errors.push(`${id}.status is unsupported: ${JSON.stringify(item.status)}`);
    const reviewStatus = item.review && item.review.status;
    if (!REVIEW_STATUSES.has(reviewStatus)) errors.push(`${id}.review.status is missing or unsupported`);

    const native = ["native-chart", "native-drawing"].includes(item.acquire_via);
    if (FILE_ROUTES.has(item.acquire_via)) {
      if (!nonEmpty(item.path)) errors.push(`${id}.${item.acquire_via} requires a destination path`);
      else if (!isWithin(assetRoot, path.resolve(projectRoot, item.path))) {
        errors.push(`${id}.path must stay inside asset_root (${plan.asset_root})`);
      }
    }
    if (native && reviewStatus !== "not-required") {
      errors.push(`${id}.review.status must be not-required for ${item.acquire_via}`);
    }
    if (FILE_ROUTES.has(item.acquire_via) && ["ready", "used"].includes(item.status)) {
      if (!nonEmpty(item.path)) errors.push(`${id} is ${item.status} but has no path`);
      else if (!fs.existsSync(path.resolve(projectRoot, item.path))) errors.push(`${id} path does not exist: ${item.path}`);
      if (reviewStatus !== "approved") errors.push(`${id} is ${item.status} but review.status is not approved`);
    }
    if (item.acquire_via === "native-chart" && ["ready", "used"].includes(item.status)) {
      if (!nonEmpty(item.source_path)) errors.push(`${id} native-chart requires source_path`);
      else if (!fs.existsSync(path.resolve(projectRoot, item.source_path))) errors.push(`${id} source_path does not exist: ${item.source_path}`);
    }
    if (["paper-figure", "web"].includes(item.acquire_via) && item.status === "used") {
      if (!nonEmpty(item.source)) errors.push(`${id} used ${item.acquire_via} evidence requires source citation`);
      if (!nonEmpty(item.license)) warnings.push(`${id} used ${item.acquire_via} evidence has no rights/license note`);
    }
    if (item.acquire_via === "ai-illustration" && item.placement_role === "evidence") {
      errors.push(`${id} uses AI illustration as evidence; AI visuals may only be hero/support/background/inline`);
    }
    if (phase !== "design" && item.required && !["ready", "used"].includes(item.status)) {
      errors.push(`${id} is required but not ready for ${phase}: ${item.status}`);
    }
    if (phase === "deliver" && item.required && item.status !== "used") {
      errors.push(`${id} is required and must be used before delivery; got ${item.status}`);
    }
  }
  return { plan, itemsById, errors, warnings };
}

function main() {
  const args = process.argv.slice(2);
  const phaseIndex = args.indexOf("--phase");
  const phase = phaseIndex >= 0 ? args[phaseIndex + 1] : "design";
  const planArg = args.find((arg, index) => arg !== "--phase" && index !== phaseIndex + 1);
  if (!planArg || !["design", "generate", "deliver"].includes(phase)) {
    console.error("Usage: node validate-evidence-plan.js <evidence-plan.json> [--phase design|generate|deliver]");
    process.exit(2);
  }
  // Same workspace rule as validate-execution-lock.js: task files must not
  // live inside the shared skill directory.
  const skillRoot = path.resolve(__dirname, "..");
  if (path.resolve(planArg).startsWith(skillRoot + path.sep)) {
    console.error(
      `[ERROR] task workspace is inside the skill directory (${skillRoot}). ` +
      "Move the whole workspace outside skills/ppt-creation (e.g. <workspace>/projects/<task>/) and rerun."
    );
    process.exit(1);
  }
  const result = validateEvidencePlan(planArg, { phase });
  for (const message of result.warnings) console.warn(`[WARN] ${message}`);
  for (const message of result.errors) console.error(`[ERROR] ${message}`);
  console.log(`evidence-plan: ${result.errors.length} error(s), ${result.warnings.length} warning(s), phase=${phase}`);
  process.exit(result.errors.length ? 1 : 0);
}

if (require.main === module) main();
module.exports = { validateEvidencePlan };
