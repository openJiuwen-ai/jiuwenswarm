import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { HTML_PREVIEW_SANDBOX, previewKind } from '../node_modules/.cache/artifact-html-preview-sandbox/filePreviewModel.mjs';

const filePreviewSource = new URL('../src/components/ArtifactsPanel/FilePreview.tsx', import.meta.url);

test('html artifacts are routed to the iframe preview', () => {
  assert.equal(previewKind({ name: 'power-industry-brief.html' }), 'html');
  assert.equal(previewKind({ name: 'report.htm' }), 'html');
  assert.equal(previewKind({ name: 'report', mimeType: 'text/html' }), 'html');
});

test('html preview sandbox lets artifact scripts run without gaining the host origin', () => {
  const tokens = HTML_PREVIEW_SANDBOX.split(/\s+/).filter(Boolean);

  // Self-contained artifacts frequently render their whole body from inline JS
  // (empty placeholder + script). Without allow-scripts the iframe stays blank
  // even though the document itself loads fine.
  assert.ok(tokens.includes('allow-scripts'), 'artifact scripts must be able to run');

  // allow-scripts + allow-same-origin would let the document remove its own
  // sandboxing and read host cookies/localStorage or call first-party APIs.
  assert.ok(!tokens.includes('allow-same-origin'), 'the frame must keep an opaque origin');

  // "View original" style links inside artifacts should still open a tab; that
  // tab inherits the sandbox (and its opaque origin).
  assert.ok(tokens.includes('allow-popups'));

  // Artifact HTML is served from the host origin without CSP, so escaping the
  // sandbox would hand artifact scripts a top-level host-origin document —
  // cookies, storage, first-party API access. Equivalent to XSS.
  assert.ok(
    !tokens.includes('allow-popups-to-escape-sandbox'),
    'popups must not escape the sandbox',
  );
});

test('FilePreview keeps using the shared sandbox policy', async () => {
  const source = await readFile(filePreviewSource, 'utf8');

  assert.match(source, /sandbox=\{HTML_PREVIEW_SANDBOX\}/);
  assert.ok(!source.includes('sandbox=""'), 'an empty sandbox blocks scripts and renders script-driven artifacts blank');
});
