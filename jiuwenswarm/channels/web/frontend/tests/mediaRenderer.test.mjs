import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { JSDOM } from 'jsdom';

let MediaRenderer;
before(async () => {
  await build({
    entryPoints: ['src/components/ChatPanel/MediaRenderer.tsx'],
    bundle: true,
    platform: 'node',
    format: 'esm',
    packages: 'external',
    jsx: 'automatic',
    loader: { '.svg': 'dataurl' },
    define: { 'import.meta.env': '{}' },
    outfile: 'node_modules/.cache/media-renderer/MediaRenderer.mjs',
  });
  ({ MediaRenderer } = await import('../node_modules/.cache/media-renderer/MediaRenderer.mjs'));
});

const file = (filename, path) => ({ type: 'document', filename, path });

function renderedFileNames(items) {
  const dom = new JSDOM(renderToStaticMarkup(createElement(MediaRenderer, { items })));
  try {
    return Array.from(dom.window.document.querySelectorAll('[data-testid="chat-panel-msg-file-card"]'))
      .map((card) => card.getAttribute('data-variant'));
  } finally {
    dom.window.close();
  }
}

test('renders case-distinct filenames without paths as separate attachments', () => {
  assert.deepEqual(renderedFileNames([file('README.md'), file('readme.md')]), ['README.md', 'readme.md']);
});

test('renders case-distinct POSIX paths as separate attachments', () => {
  assert.deepEqual(
    renderedFileNames([file('B.png', '/a/B.png'), file('b.png', '/a/b.png')]),
    ['B.png', 'b.png'],
  );
});

test('deduplicates repeated filenames without paths', () => {
  assert.deepEqual(renderedFileNames([file('README.md'), file('README.md')]), ['README.md']);
});

test('deduplicates repeated attachment paths', () => {
  assert.deepEqual(
    renderedFileNames([file('README.md', '/a/README.md'), file('README.md', '/a/README.md')]),
    ['README.md'],
  );
});

test('keeps same-named attachments from different directories', () => {
  assert.deepEqual(
    renderedFileNames([file('README.md', '/a/README.md'), file('README.md', '/b/README.md')]),
    ['README.md', 'README.md'],
  );
});

test('deduplicates Windows paths with different case and slash direction', () => {
  assert.deepEqual(
    renderedFileNames([file('B.png', 'C:\\A\\B.png'), file('b.PNG', 'c:/a/b.PNG')]),
    ['B.png'],
  );
});

test('deduplicates an upload followed by its same-named asset reference', () => {
  assert.deepEqual(
    renderedFileNames([file('README.md'), file('README.md', '/a/README.md')]),
    ['README.md'],
  );
});
