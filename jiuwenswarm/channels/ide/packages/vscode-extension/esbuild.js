const esbuild = require('esbuild');
const fs = require('fs');
const path = require('path');
const watch = process.argv.includes('--watch');

// ── Shared webview assets ──────────────────────────────────────────
// shared-webview/ is the single source of truth for the webview html
// and the panel icon. They are copied into resources/ on every build so
// the packaged extension always ships copies in sync with the source —
// no committed duplicates, no drift.
const sharedWebviewDir = path.join(__dirname, '..', 'shared-webview');
const resourcesDir = path.join(__dirname, 'resources');
const SHARED_ASSETS = ['chat.html', 'swarm_map.html', 'icon.svg'];

function copyWebviewAssets() {
  fs.mkdirSync(resourcesDir, { recursive: true });
  for (const name of SHARED_ASSETS) {
    const src = path.join(sharedWebviewDir, name);
    const dest = path.join(resourcesDir, name);
    try {
      fs.copyFileSync(src, dest);
      console.log(`[esbuild] copied ${name} → resources/${name}`);
    } catch (err) {
      console.error(`[esbuild] failed to copy ${name}: ${err.message}`);
      process.exitCode = 1;
    }
  }
}

copyWebviewAssets();

if (watch) {
  // Re-copy on shared-webview changes so hot reload keeps the assets current.
  fs.watch(sharedWebviewDir, (eventType, filename) => {
    if (filename && SHARED_ASSETS.includes(filename)) {
      console.log(`[esbuild] shared-webview/${filename} changed — re-copying`);
      copyWebviewAssets();
    }
  });
}

const ctx = esbuild.context({
  entryPoints: ['src/extension.ts'],
  bundle: true,
  outfile: 'out/extension.js',
  external: ['vscode'],
  format: 'cjs',
  platform: 'node',
  target: 'node18',
  sourcemap: true,
  minify: !watch,
  logLevel: 'info',
}).then(ctx => {
  if (watch) {
    ctx.watch().then(() => console.log('[esbuild] watching…'));
  } else {
    ctx.rebuild().then(() => {
      console.log('[esbuild] build complete');
      ctx.dispose();
    });
  }
}).catch(() => process.exit(1));
