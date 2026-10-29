const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { createRequire } = require('node:module');

const ROOT = path.resolve(__dirname, '../..');
const DOWNLOAD_RESOURCES = ['browser_downloads.cjs', 'browser_download_bubble.cjs', 'dist/bubble.html'];
const bash = process.platform === 'win32'
  ? path.join(process.env.ProgramFiles || 'C:/Program Files', 'Git/bin/bash.exe')
  : 'bash';

for (const platform of ['windows', 'macos']) {
  const shell = platform === 'windows' ? (process.platform === 'win32' ? 'powershell.exe' : 'pwsh') : bash;
  const available = spawnSync(shell, ['--version'], { windowsHide: true }).error === undefined;
  for (const missing of [null, ...DOWNLOAD_RESOURCES]) {
    test(`${platform} assembled downloads: ${missing ? `reject missing ${missing}` : 'complete bundle loads'}`, {
      skip: !available && `${shell} is unavailable`,
    }, (t) => {
      const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'electron-download-resources-'));
      t.after(() => {
        assert.equal(path.dirname(temp), path.resolve(os.tmpdir()));
        fs.rmSync(temp, { recursive: true, force: true });
      });
      const app = path.join(temp, 'resources/app');
      const desktop = path.join(ROOT, 'jiuwenswarm/channels/desktop/electron');
      fs.mkdirSync(app, { recursive: true });
      // Non-shell fixtures stand in for the frontend and npm build outputs.
      for (const name of ['package.json', 'dist/index.html', 'dist/bubble.html',
        'node_modules/@playwright/mcp/package.json', 'node_modules/@playwright/mcp/cli.js']) {
        const file = path.join(app, name);
        fs.mkdirSync(path.dirname(file), { recursive: true });
        fs.writeFileSync(file, name.endsWith('.json') ? '{}' : 'fixture');
      }
      fs.writeFileSync(path.join(temp, 'WorkSwarm.exe'), 'fixture');
      const extension = platform === 'windows' ? 'ps1' : 'sh';
      const source = fs.readFileSync(path.join(ROOT, `scripts/build-electron-exe.${extension}`), 'utf8');
      const copy = source.slice(source.indexOf('# Copy the mature Electron desktop shell'), source.indexOf('# Copy logo'));
      assert.ok(copy.includes('main.cjs'), 'shell copy block must be found');
      const env = { ...process.env, APP_DIR: app.replaceAll('\\', '/'),
        DESKTOP_DIR: desktop.replaceAll('\\', '/'), ELECTRON_APP_DIR: temp,
        MISSING_RESOURCE: missing || '', PROCESSOR_ARCHITECTURE: 'AMD64' };
      let script;
      let args;
      if (platform === 'windows') {
        const gate = source.slice(source.indexOf('$RequiredFiles = @('), source.indexOf('# Sign only after'));
        assert.ok(gate.includes('foreach ($RequiredFile'), 'resource gate must be found');
        script = "$ErrorActionPreference = 'Stop'\n"
          + '$DesktopDir = $env:DESKTOP_DIR\n$AppDir = $env:APP_DIR\n'
          + '$ElectronAppDir = $env:ELECTRON_APP_DIR\n$ShellExeName = "WorkSwarm.exe"\n$FrontendOnly = $true\n'
          + copy + '\nif ($env:MISSING_RESOURCE -and (Test-Path -LiteralPath (Join-Path $AppDir $env:MISSING_RESOURCE))) { Remove-Item -LiteralPath (Join-Path $AppDir $env:MISSING_RESOURCE) -Force }\n'
          + gate;
        args = ['-NoProfile', '-NonInteractive', '-Command'];
      } else {
        const gate = source.slice(source.indexOf('REQUIRED_FILES=('), source.indexOf('# Test build: write .test marker file'));
        assert.ok(gate.includes('for required in'), 'resource gate must be found');
        script = 'set -euo pipefail\n' + copy
          + '\nif [ -n "$MISSING_RESOURCE" ] && [ -f "$APP_DIR/$MISSING_RESOURCE" ]; then rm -- "$APP_DIR/$MISSING_RESOURCE"; fi\n' + gate;
        args = [];
      }
      const scriptPath = path.join(temp, `check.${extension}`);
      fs.writeFileSync(scriptPath, script.replaceAll('\r\n', '\n'));
      const input = platform === 'windows' ? script : scriptPath;
      const result = spawnSync(shell, [...args, input], { env, encoding: 'utf8', windowsHide: true });
      const output = `${result.stdout}\n${result.stderr}`;
      assert.ifError(result.error);
      if (missing) {
        assert.notEqual(result.status, 0, output);
        assert.match(output, /[Rr]equired .*missing/, output);
        assert.ok(output.includes(path.basename(missing)), output);
      } else {
        assert.equal(result.status, 0, output);
        const requireFromBundle = createRequire(path.join(app, 'main.cjs'));
        assert.equal(typeof requireFromBundle('./browser_downloads.cjs').createBrowserDownloadService, 'function');
        assert.equal(typeof requireFromBundle('./browser_download_bubble.cjs').createDownloadBubbleController, 'function');
        for (const name of DOWNLOAD_RESOURCES.slice(0, 2)) {
          assert.deepEqual(fs.readFileSync(path.join(app, name)), fs.readFileSync(path.join(desktop, name)));
        }
      }
    });
  }
}
