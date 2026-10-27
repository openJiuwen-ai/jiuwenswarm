import assert from 'node:assert/strict';
import test from 'node:test';
import {
  collectDesignerMaterials,
  designerImagePreviewUrl,
} from '../node_modules/.cache/designer-materials/designerMaterials.js';

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

function graphWith(nodes) {
  return { graph_id: 'g', project_id: 'p', nodes, edges: [] };
}

test('a canvas upload is listed as uploaded, a generated output as generated', () => {
  const uploaded = {
    id: 'n_up',
    label: 'cat.png',
    type: 'image',
    config: {
      interaction_mode: 'upload',
      upload: { filename: 'cat.png', mime_type: 'image/png', uri: 'file:///tmp/cat.png' },
    },
    output_ref: { kind: 'image', uri: 'file:///tmp/cat.png', mime_type: 'image/png', label: 'cat.png' },
  };
  const generated = {
    id: 'n_gen',
    label: 'Shot 1',
    type: 'video',
    config: { interaction_mode: 'generate' },
    output_ref: { kind: 'video', uri: 'file:///tmp/shot_1.mp4', mime_type: 'video/mp4' },
  };

  const materials = collectDesignerMaterials(graphWith([uploaded, generated]), null);

  assert.equal(materials.find((item) => item.nodeId === 'n_up')?.source, 'uploaded');
  assert.equal(materials.find((item) => item.nodeId === 'n_gen')?.source, 'generated');
});

test('an uploaded video keeps its uploaded source when the run store holds its output', () => {
  const node = {
    id: 'n_vid',
    label: 'clip.mp4',
    type: 'video',
    config: {
      interaction_mode: 'upload',
      upload: { filename: 'clip.mp4', mime_type: 'video/mp4', uri: 'file:///tmp/clip.mp4' },
    },
  };
  const run = {
    run_id: 'r',
    graph_id: 'g',
    project_id: 'p',
    status: 'completed',
    current_node_ids: [],
    node_states: {
      n_vid: { status: 'completed', output_ref: { kind: 'video', uri: 'file:///tmp/clip.mp4', mime_type: 'video/mp4' } },
    },
  };

  const [material] = collectDesignerMaterials(graphWith([node]), run);

  assert.equal(material.source, 'uploaded');
});
