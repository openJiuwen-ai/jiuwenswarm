import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  fromReactFlowGraph,
  resolvedNodeCanvasSize,
  toReactFlowGraph,
} from '../node_modules/.cache/designer-graph-adapter/designerGraphAdapter.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const fixturePath = path.join(__dirname, 'fixtures', 'designer-execution-graph.v1.json');
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));

test('toReactFlowGraph maps domain nodes and edges', () => {
  const view = toReactFlowGraph(fixture);
  assert.equal(view.nodes.length, fixture.nodes.length);
  assert.equal(view.edges.length, fixture.edges.length);
  const brief = view.nodes.find((node) => node.id === 'n_brief');
  assert.ok(brief);
  assert.deepEqual(brief.position, { x: 40, y: 240 });
  assert.equal(brief.type, 'text');
  assert.equal(brief.data.label, 'Brief');
  assert.ok(brief.width > 0 && brief.height > 0);
  assert.equal(brief.width, brief.style.width);
  assert.equal(brief.height, brief.style.height);
  assert.equal(brief.initialWidth, brief.width);
  assert.equal(brief.initialHeight, brief.height);
});

test('fromReactFlowGraph preserves domain semantics while updating layout', () => {
  const view = toReactFlowGraph(fixture);
  const moved = {
    ...view,
    nodes: view.nodes.map((node) =>
      node.id === 'n_brief'
        ? { ...node, position: { x: 100, y: 200 } }
        : node,
    ),
  };
  const merged = fromReactFlowGraph(moved, fixture);
  const brief = merged.nodes.find((node) => node.id === 'n_brief');
  assert.ok(brief);
  assert.equal(brief.layout?.x, 100);
  assert.equal(brief.layout?.y, 200);
  assert.equal(brief.type, 'text');
  assert.deepEqual(
    merged.edges.map((edge) => edge.id),
    fixture.edges.map((edge) => edge.id),
  );
  assert.equal(
    merged.edges.find((edge) => edge.id === 'e_scene_n_clip_1')?.source,
    'n_scene',
  );
  assert.equal(
    merged.edges.find((edge) => edge.id === 'e_n_clip_1_compose')?.target,
    'n_compose',
  );
});

test('resolvedNodeCanvasSize frames default media nodes at the canvas ratio', () => {
  const portrait = { video_settings: { aspect_ratio: '9:16' }, image_settings: { aspect_ratio: '16:9' } };
  const clip = {
    id: 'n_clip_1',
    type: 'video',
    label: 'Clip 1',
    config: {},
    layout: { x: 0, y: 0, width: 280, height: 160 },
    output_ref: null,
  };
  const video = resolvedNodeCanvasSize(clip, portrait);
  assert.ok(video.height > video.width);

  const image = resolvedNodeCanvasSize({ ...clip, id: 'n_scene', type: 'image' }, portrait);
  assert.ok(image.width > image.height);

  const text = resolvedNodeCanvasSize({ ...clip, id: 'n_brief', type: 'text' }, portrait);
  assert.deepEqual(text, { width: 280, height: 160 });
});
