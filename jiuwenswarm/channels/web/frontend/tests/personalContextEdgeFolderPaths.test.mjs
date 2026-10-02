import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const output = fileURLToPath(new URL('../node_modules/.cache/personal-context-edge-folder-paths/edgeBookmarkFolders.mjs', import.meta.url));

await build({
  entryPoints: [fileURLToPath(new URL('../src/components/PersonalContext/edgeBookmarkFolders.ts', import.meta.url))],
  outfile: output,
  bundle: true,
  platform: 'node',
  format: 'esm',
});

const { parseEdgeBookmarkFolderPaths } = await import(
  new URL('../node_modules/.cache/personal-context-edge-folder-paths/edgeBookmarkFolders.mjs', import.meta.url)
);

test('Chinese and ASCII commas split folder paths and discard duplicates', () => {
  assert.deepEqual(
    parseEdgeBookmarkFolderPaths([' 收藏栏，收藏栏 ', '收藏栏/技术, 其他收藏夹', '  ']),
    ['收藏栏', '收藏栏/技术', '其他收藏夹'],
  );
});

test('a single folder path remains one filter', () => {
  assert.deepEqual(parseEdgeBookmarkFolderPaths(['收藏夹栏/技术']), ['收藏夹栏/技术']);
});
