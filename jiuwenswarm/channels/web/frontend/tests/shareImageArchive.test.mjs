import assert from 'node:assert/strict';
import test from 'node:test';
import JSZip from 'jszip';

import {
  buildShareImageArtifact,
  getShareImagePartFilename,
} from '../node_modules/.cache/share-image/shareImageArchive.js';

test('keeps a single PNG as a PNG artifact', async () => {
  const png = new Blob(['png-one'], { type: 'image/png' });
  const artifact = await buildShareImageArtifact([png], 'share.png');

  assert.equal(artifact.filename, 'share.png');
  assert.equal(artifact.blob, png);
});

test('stores multiple numbered PNG parts in one ZIP without recompression', async () => {
  const progress = [];
  const artifact = await buildShareImageArtifact(
    [new TextEncoder().encode('part-one'), new TextEncoder().encode('part-two')],
    'share.png',
    () => progress.push(true),
  );

  assert.equal(artifact.filename, 'share.zip');
  assert.equal(artifact.blob.type, 'application/zip');
  assert.ok(progress.length > 0);
  const archive = await JSZip.loadAsync(await artifact.blob.arrayBuffer(), { checkCRC32: true });
  assert.deepEqual(Object.keys(archive.files), [
    'share-part-01-of-02.png',
    'share-part-02-of-02.png',
  ]);
  assert.equal(await archive.file('share-part-01-of-02.png').async('string'), 'part-one');
  assert.equal(await archive.file('share-part-02-of-02.png').async('string'), 'part-two');
});

test('pads part numbers consistently for large archives', () => {
  assert.equal(
    getShareImagePartFilename('share.png', 0, 120),
    'share-part-001-of-120.png',
  );
});
