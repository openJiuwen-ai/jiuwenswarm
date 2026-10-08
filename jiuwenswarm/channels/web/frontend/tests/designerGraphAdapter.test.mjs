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
  assert.equal(
    merged.edges.find((edge) => edge.id === 'e_frame_1_clip_1')?.source,
    'n_frame_1',
  );
  assert.equal(
    merged.edges.find((edge) => edge.id === 'e_clip_1_final')?.target,
    'n_final',
  );
  assert.equal(
    merged.edges.find((edge) => edge.id === 'e_character_storyboard'),
    undefined,
  );
});

test('resolvedNodeCanvasSize turns default landscape media nodes portrait', () => {
  const portrait = resolvedNodeCanvasSize({
    id: 'n_clip_1',
    type: 'video',
    label: 'Clip 1',
    config: { aspect_lock: { ratio: '9:16', video_size: '1080*1920' } },
    layout: { x: 0, y: 0, width: 280, height: 160 },
    output_ref: null,
  });
  assert.ok(portrait.height > portrait.width);

  const landscape = resolvedNodeCanvasSize({
    id: 'n_clip_2',
    type: 'video',
    label: 'Clip 2',
    config: { aspect_lock: { ratio: '16:9' } },
    layout: { x: 0, y: 0, width: 280, height: 160 },
    output_ref: null,
  });
  assert.ok(landscape.width > landscape.height);

  const text = resolvedNodeCanvasSize({
    id: 'n_brief',
    type: 'text',
    label: 'Brief',
    config: { aspect_lock: { ratio: '9:16' } },
    layout: { x: 0, y: 0, width: 280, height: 160 },
    output_ref: null,
  });
  assert.deepEqual(text, { width: 280, height: 160 });
});
