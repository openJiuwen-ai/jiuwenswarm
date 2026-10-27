import assert from 'node:assert/strict';
import test from 'node:test';
import { designerImagePreviewUrl } from '../node_modules/.cache/designer-materials/designerMaterials.js';

test('an upstream image output gets an image preview', () => {
  const url = designerImagePreviewUrl({ uri: 'file:///tmp/shot_1.png', mime_type: 'image/png' }, 'image');
  assert.ok(url);
  assert.ok(url.includes('shot_1.png'));
});

test('upstream video and audio outputs get no image preview', () => {
  assert.equal(designerImagePreviewUrl({ uri: 'file:///tmp/clip_1.mp4', mime_type: 'video/mp4' }, 'video'), null);
  assert.equal(designerImagePreviewUrl({ uri: 'file:///tmp/clip_2.mp4' }, 'video'), null);
  assert.equal(designerImagePreviewUrl({ uri: 'file:///tmp/voice.mp3', kind: 'audio' }, 'audio'), null);
});

test('a text sidecar on an image node gets no image preview', () => {
  assert.equal(designerImagePreviewUrl({ uri: 'file:///tmp/prompt.md' }, 'image'), null);
});

test('a missing output gets no image preview', () => {
  assert.equal(designerImagePreviewUrl(null, 'image'), null);
  assert.equal(designerImagePreviewUrl({ uri: '' }, 'image'), null);
});
