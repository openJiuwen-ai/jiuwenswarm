#!/usr/bin/env node
/**
 * pack.js — ZIP the dist/ folder into a Chrome Web Store–ready .crx-style archive.
 *
 * Usage: node scripts/pack.js
 * Output: jiuwenswarm-browser-<version>.zip in the project root.
 *
 * Requires: npm install archiver (listed in devDependencies)
 */

import archiver from "archiver";
import { createWriteStream, existsSync, readFileSync } from "fs";
import { join, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const ROOT = resolve(__dirname, "..");
const DIST = join(ROOT, "dist");

if (!existsSync(DIST)) {
  console.error("[pack] dist/ not found — run `npm run build` first");
  process.exit(1);
}

const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf-8"));
const outFile = join(ROOT, `jiuwenswarm-browser-${pkg.version}.zip`);

const output = createWriteStream(outFile);
const archive = archiver("zip", { zlib: { level: 9 } });

output.on("close", () => {
  const kb = Math.round(archive.pointer() / 1024);
  console.log(`[pack] Created ${outFile} (${kb} KB)`);
});

archive.on("warning", (err) => {
  if (err.code === "ENOENT") {
    console.warn("[pack] warning:", err.message);
  } else {
    throw err;
  }
});

archive.on("error", (err) => { throw err; });

archive.pipe(output);
archive.directory(DIST, false);
archive.finalize();
