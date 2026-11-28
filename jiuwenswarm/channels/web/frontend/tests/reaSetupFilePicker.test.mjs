import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const frontendRoot = new URL('..', import.meta.url);
let reaSetupApi;

before(async () => {
  const cacheDir = fileURLToPath(new URL('node_modules/.cache/rea-setup-file-picker/', frontendRoot));
  await mkdir(cacheDir, { recursive: true });
  await build({
    entryPoints: [fileURLToPath(new URL('src/features/reaSetup/reaSetupApi.ts', frontendRoot))],
    absWorkingDir: fileURLToPath(frontendRoot),
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: join(cacheDir, 'reaSetupApi.mjs'),
    plugins: [{
      name: 'rea-setup-picker-ports',
      setup(pluginBuild) {
        pluginBuild.onResolve({ filter: /services[\\/]webClient$/ }, () => ({
          path: 'web-client',
          namespace: 'picker-test',
        }));
        pluginBuild.onResolve({ filter: /workspace[\\/]localFilePicker$/ }, () => ({
          path: 'local-file-picker',
          namespace: 'picker-test',
        }));
        pluginBuild.onResolve({ filter: /workspace[\\/]projectDirectoryPicker$/ }, () => ({
          path: 'directory-picker',
          namespace: 'picker-test',
        }));
        pluginBuild.onLoad({ filter: /.*/, namespace: 'picker-test' }, ({ path }) => {
          if (path === 'web-client') {
            return { contents: 'export const webRequest = (...args) => globalThis.__reaPickerWebRequest(...args);' };
          }
          if (path === 'local-file-picker') {
            return {
              contents:
                'export const selectLocalFiles = (...args) => globalThis.__reaSelectLocalFiles(...args);',
            };
          }
          return {
            contents:
              'export const selectProjectDirectory = (...args) => '
              + 'globalThis.__reaSelectProjectDirectory(...args);',
          };
        });
      },
    }],
  });
  ({ reaSetupApi } = await import(pathToFileURL(join(cacheDir, 'reaSetupApi.mjs')).href));
});

test('file selection uses the desktop file bridge instead of path.select_file', async () => {
  const calls = [];
  globalThis.__reaSelectLocalFiles = async (...args) => {
    calls.push(args);
    return {
      ok: true,
      files: [{ path: 'C:\\fixtures\\ida-mcp.json', filename: 'ida-mcp.json', size: 12, kind: 'document' }],
    };
  };

  assert.deepEqual(await reaSetupApi.selectFile('IDA JSON'), {
    path: 'C:\\fixtures\\ida-mcp.json',
    cancelled: false,
  });
  assert.deepEqual(calls, [[false]]);
});

test('file selection preserves cancellation and reports bridge failures', async () => {
  globalThis.__reaSelectLocalFiles = async () => ({ ok: false, reason: 'cancelled' });
  assert.deepEqual(await reaSetupApi.selectFile('IDA JSON'), {
    path: null,
    cancelled: true,
  });

  globalThis.__reaSelectLocalFiles = async () => ({
    ok: false,
    reason: 'unsupported',
    message: 'file picker unavailable',
  });
  await assert.rejects(() => reaSetupApi.selectFile('IDA JSON'), /file picker unavailable/);
});

test('directory selection uses the shared desktop/browser picker', async () => {
  let calls = 0;
  globalThis.__reaSelectProjectDirectory = async () => {
    calls += 1;
    return { ok: true, path: 'C:\\fixtures\\jdk', name: 'jdk' };
  };

  assert.deepEqual(await reaSetupApi.selectDirectory('JDK'), {
    path: 'C:\\fixtures\\jdk',
    cancelled: false,
  });
  assert.equal(calls, 1);
});
