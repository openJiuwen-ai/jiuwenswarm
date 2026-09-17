#!/usr/bin/env node
// Sync the repo copy of the ppt-creation skill to the runtime copy the agent
// actually loads, register any new files in agent-data.json manifests, and verify
// the two copies match by hash. No third-party deps.
//
//   node scripts/sync-runtime-skill.js
//
// Exits 0 and prints "ALL IN SYNC" when the key files are byte-identical on both
// sides; exits 1 and lists the mismatches otherwise.

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const SRC = path.resolve(__dirname, "..");                       // .../skills/ppt-creation (repo copy)
const DST = path.resolve(
  process.env.HOME, ".jiuwenswarm/agent/workspace/skills/ppt-creation" // runtime copy
);
const AGENT_DATA = [
  path.resolve(SRC, "../../agent-data.json"),                    // repo manifest
  path.resolve(process.env.HOME, ".jiuwenswarm/agent/workspace/agent-data.json"), // runtime manifest
];

const EXCLUDE_DIRS = new Set(["node_modules", "output", ".git"]);
const EXCLUDE_EXT = new Set([".pptx", ".pdf", ".png.tmp"]);
const EXCLUDE_NAME = new Set([".DS_Store"]);

// Repo-only files stay in the repo copy and must never reach the runtime
// workspace or its manifests — the agent treats everything there as reference
// material. Unlike EXCLUDE_*, these are still walked on the runtime side so a
// previously synced copy gets removed as stale.
function isRepoOnly(name) {
  return /^demo[_-]/.test(name);
}

function shouldSkip(name) {
  if (EXCLUDE_DIRS.has(name) || EXCLUDE_NAME.has(name)) return true;
  if (EXCLUDE_EXT.has(path.extname(name))) return true;
  return false;
}

function walk(dir, base = "") {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (shouldSkip(entry.name)) continue;
    const rel = path.join(base, entry.name);
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(abs, rel));
    else if (entry.isFile()) out.push(rel);
  }
  return out;
}

function sha256(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

// 1) Mirror repo -> runtime (create/overwrite current files, remove stale files).
const files = walk(SRC).filter((rel) => !isRepoOnly(path.basename(rel)));
const sourceFiles = new Set(files);
let copied = 0;
for (const rel of files) {
  const from = path.join(SRC, rel);
  const to = path.join(DST, rel);
  fs.mkdirSync(path.dirname(to), { recursive: true });
  const changed = !fs.existsSync(to) || sha256(from) !== sha256(to);
  if (changed) { fs.copyFileSync(from, to); copied += 1; }
}
// Stale removal is confined to path roots the repo actually mirrors. Anything
// else in the runtime copy (e.g. a task workspace an agent created next to the
// skill files) is not ours to manage — deleting it destroys live task state.
// 2026-07-22: the unguarded version wiped a just-finished deck workspace.
const ownedRoots = new Set(files.map((rel) => rel.split(path.sep)[0]));
let removedRuntime = 0;
if (fs.existsSync(DST)) {
  for (const rel of walk(DST)) {
    if (sourceFiles.has(rel)) continue;
    if (!ownedRoots.has(rel.split(path.sep)[0])) continue;
    fs.rmSync(path.join(DST, rel), { force: true });
    removedRuntime += 1;
  }

  // File mirroring can leave empty directories behind (for example when an
  // obsolete asset family is removed). Prune those without touching excluded
  // runtime directories such as node_modules.
  const pruneEmptyDirs = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isDirectory() || shouldSkip(entry.name)) continue;
      const child = path.join(dir, entry.name);
      pruneEmptyDirs(child);
      if (fs.readdirSync(child).length === 0) fs.rmdirSync(child);
    }
  };
  for (const entry of fs.readdirSync(DST, { withFileTypes: true })) {
    if (!entry.isDirectory() || shouldSkip(entry.name) || !ownedRoots.has(entry.name)) continue;
    const child = path.join(DST, entry.name);
    pruneEmptyDirs(child);
    if (fs.readdirSync(child).length === 0) fs.rmdirSync(child);
  }
}
console.log(`Mirrored ${files.length} file(s): ${copied} changed, ${removedRuntime} stale runtime file(s) removed.`);

// 2) Reconcile files in the skill root and owned first-level directories with
//    the agent-data.json manifests (idempotent). Including the root makes a
//    git-restored compatibility file discoverable again on the next sync.
const MANIFEST_DIRS = ["", "agents", "base", "components", "references", "scripts", "workflows"];

function reconcileManifest(manifestPath) {
  if (!fs.existsSync(manifestPath)) { console.warn(`  (skip, absent) ${manifestPath}`); return; }
  const data = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  let added = 0, removed = 0;
  const skillKey = "workspace/skills/ppt-creation";
  const prefix = `${skillKey}/`;

  // Remove directory keys and file entries whose source paths no longer exist.
  for (const key of Object.keys(data)) {
    if ((key !== skillKey && !key.startsWith(prefix)) || !Array.isArray(data[key])) continue;
    const dirRel = key === skillKey ? "" : key.slice(prefix.length);
    const dirPath = path.join(SRC, dirRel);
    if (!fs.existsSync(dirPath) || !fs.statSync(dirPath).isDirectory()) {
      removed += data[key].length;
      delete data[key];
      continue;
    }
    const kept = data[key].filter((entry) => {
      const exists = entry && entry.name && !isRepoOnly(entry.name) &&
        fs.existsSync(path.join(dirPath, entry.name));
      if (!exists) removed += 1;
      return exists;
    });
    data[key] = kept;
  }

  for (const dirRel of MANIFEST_DIRS) {
    const dirPath = path.join(SRC, dirRel);
    if (!fs.existsSync(dirPath)) continue;
    const key = dirRel
      ? `workspace/skills/ppt-creation/${dirRel}`
      : "workspace/skills/ppt-creation";
    if (!Array.isArray(data[key])) data[key] = [];
    const present = new Set(data[key].map((e) => e.name));
    for (const name of fs.readdirSync(dirPath)) {
      if (shouldSkip(name) || isRepoOnly(name) || fs.statSync(path.join(dirPath, name)).isDirectory()) continue;
      if (!present.has(name)) {
        data[key].push({
          name,
          path: dirRel
            ? `agent/workspace/skills/ppt-creation/${dirRel}/${name}`
            : `agent/workspace/skills/ppt-creation/${name}`,
          isMarkdown: name.endsWith(".md"),
        });
        added += 1;
      }
    }
  }
  if (added || removed) fs.writeFileSync(manifestPath, JSON.stringify(data, null, 2));
  console.log(
    `  ${path.basename(path.dirname(manifestPath))}/agent-data.json: +${added}, -${removed} file entr${removed === 1 ? "y" : "ies"}`
  );
}
console.log("Manifest reconciliation:");
for (const m of AGENT_DATA) reconcileManifest(m);

// 3) Hash-compare the key working files.
const KEY = files.filter((r) =>
  MANIFEST_DIRS.some((dir) => dir && r.startsWith(`${dir}/`)) ||
  (!r.includes("/") && r.endsWith(".md"))
);
const diffs = [];
for (const rel of KEY) {
  const to = path.join(DST, rel);
  if (!fs.existsSync(to) || sha256(path.join(SRC, rel)) !== sha256(to)) diffs.push(rel);
}
console.log(`Hash check: ${KEY.length - diffs.length}/${KEY.length} key files identical.`);
if (diffs.length) {
  console.error("DIFF:", diffs.join(", "));
  process.exit(1);
}
console.log("ALL IN SYNC");
