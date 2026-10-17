import assert from 'node:assert/strict';
import test from 'node:test';

import { designerMaterialFilename } from '../node_modules/.cache/designer-material-filename/designerMaterials.js';

test('designerMaterialFilename appends the stored file extension to the node label', () => {
  assert.equal(
    designerMaterialFilename({ label: '镜头 2', uri: 'file:///C:/runs/r1/clip_2.mp4' }, 'fallback'),
    '镜头 2.mp4',
  );
  assert.equal(
    designerMaterialFilename({ label: 'Hero', uri: 'file:///runs/r1/hero.PNG' }, 'fallback'),
    'Hero.png',
  );
  assert.equal(
    designerMaterialFilename({ label: 'Brief', uri: 'file:///runs/r1/brief.md' }, 'fallback'),
    'Brief.md',
  );
});

test('designerMaterialFilename keeps a label that already carries the extension', () => {
  assert.equal(
    designerMaterialFilename({ label: 'cat.png', uri: 'file:///tmp/uploads/cat.png' }, 'fallback'),
    'cat.png',
  );
});

test('designerMaterialFilename ignores a query string and falls back to the MIME type', () => {
  assert.equal(
    designerMaterialFilename({ label: 'Clip', uri: 'https://cdn.example/v/clip.webm?sig=abc' }, 'fallback'),
    'Clip.webm',
  );
  assert.equal(
    designerMaterialFilename(
      { label: 'Voice', uri: 'https://cdn.example/a/123', mimeType: 'audio/mpeg' },
      'fallback',
    ),
    'Voice.mp3',
  );
  assert.equal(
    designerMaterialFilename(
      { label: 'Notes', uri: 'file:///runs/r1/notes', mimeType: 'text/plain; charset=utf-8' },
      'fallback',
    ),
    'Notes.txt',
  );
});

test('designerMaterialFilename uses the file name, then the fallback, when there is no label', () => {
  assert.equal(designerMaterialFilename({ label: ' ', uri: 'file:///runs/r1/scene_1.png' }, 'fallback'), 'scene_1.png');
  assert.equal(
    designerMaterialFilename({ label: '', uri: '', mimeType: 'video/mp4' }, 'designer-node-n1'),
    'designer-node-n1.mp4',
  );
});

test('designerMaterialFilename leaves the name alone when the type is unknown', () => {
  assert.equal(designerMaterialFilename({ label: 'Output', uri: 'file:///runs/r1/output' }, 'fallback'), 'Output');
});
