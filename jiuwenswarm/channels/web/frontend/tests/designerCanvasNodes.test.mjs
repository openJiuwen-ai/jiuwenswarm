import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canvasEditGlance,
  DESIGNER_ADD_TEMPLATES,
  DESIGNER_DOC_FIT_MIN_BODY,
  DESIGNER_LAYOUT_ORIGIN_X,
  DESIGNER_NODE_HEADER_HEIGHT,
  DESIGNER_LAYOUT_ORIGIN_Y,
  DESIGNER_SUCCESSOR_GAP_X,
  DESIGNER_SUCCESSOR_GAP_Y,
  autoLayoutDesignerNodes,
  buildManualDesignerNode,
  buildNodeFromLibraryAsset,
  appendUserCanvasEdit,
  connectNodeToGraph,
  contentAspectForNode,
  isDefaultLandscapeNodeSize,
  nextTypeIndex,
  offsetCanvasPosition,
  packDesignerNodeLayouts,
  parseContentAspect,
  portAddTemplatesFor,
  positionLeftOfNode,
  positionRightOfNode,
  removeNodesFromGraph,
  resolvedNodeCanvasSize,
  sizeNodeForContentAspect,
  sizeNodeForDocumentContent,
  templateForAssetKind,
} from '../node_modules/.cache/designer-canvas-nodes/designerCanvasNodes.js';

test('add templates are image video and audio only', () => {
  assert.deepEqual(
    DESIGNER_ADD_TEMPLATES.map((item) => item.id),
    ['image', 'video', 'audio'],
  );
  assert.deepEqual(
    DESIGNER_ADD_TEMPLATES.map((item) => item.role),
    ['image', 'video', 'audio'],
  );
});

test('buildManualDesignerNode assigns unique id role and centered offset layout', () => {
  const template = DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'image');
  assert.ok(template);
  const first = buildManualDesignerNode({
    template,
    existing: [],
    position: offsetCanvasPosition({ x: 400, y: 300 }, 0),
  });
  assert.equal(first.type, 'image');
  assert.equal(first.config.role, 'image');
  assert.equal(first.config.interaction_mode, 'generate');
  assert.equal(first.label, 'Image 1');
  assert.ok(String(first.id).startsWith('n_image_'));
  const second = buildManualDesignerNode({
    template,
    existing: [first],
    position: offsetCanvasPosition({ x: 400, y: 300 }, 1),
  });
  assert.notEqual(second.id, first.id);
  assert.equal(second.label, 'Image 2');
  assert.equal(second.layout.x, first.layout.x + 28);
});

test('same modality increments the canvas label', () => {
  assert.equal(nextTypeIndex([{ type: 'image' }, { type: 'video' }], 'image'), 2);
});

test('library asset becomes an upload node of matching modality', () => {
  const video = buildNodeFromLibraryAsset({
    asset: {
      id: 'asset_abc',
      filename: 'walk.mp4',
      mime_type: 'video/mp4',
      kind: 'video',
      source: 'uploaded',
      objectUrl: 'blob:walk',
      size: 12,
      created_at: 1,
    },
    existing: [],
    position: { x: 10, y: 20 },
  });
  assert.equal(templateForAssetKind('video').id, 'video');
  assert.equal(video.type, 'video');
  assert.equal(video.config.role, 'video');
  assert.equal(video.config.interaction_mode, 'upload');
  assert.equal(video.config.upload.filename, 'walk.mp4');
  assert.equal(video.output_ref.uri, 'blob:walk');
});

test('positionRightOfNode stacks below an occupied successor slot', () => {
  const source = { id: 'n_brief', layout: { x: 40, y: 100, width: 280, height: 160 } };
  const first = positionRightOfNode(source, []);
  assert.equal(first.x, 40 + 280 + DESIGNER_SUCCESSOR_GAP_X);
  assert.equal(first.y, 100);
  const second = positionRightOfNode(source, [
    { id: 'n_other', layout: { x: first.x, y: first.y } },
  ]);
  assert.equal(second.x, first.x);
  assert.equal(second.y, first.y + 160 + DESIGNER_SUCCESSOR_GAP_Y);
});

test('positionLeftOfNode stacks below an occupied predecessor slot', () => {
  const source = { id: 'n_brief', layout: { x: 400, y: 100, width: 280, height: 160 } };
  const first = positionLeftOfNode(source, []);
  assert.equal(first.x, 400 - 280 - DESIGNER_SUCCESSOR_GAP_X);
  assert.equal(first.y, 100);
  const second = positionLeftOfNode(source, [
    { id: 'n_other', layout: { x: first.x, y: first.y } },
  ]);
  assert.equal(second.x, first.x);
  assert.equal(second.y, first.y + 160 + DESIGNER_SUCCESSOR_GAP_Y);
});

test('appendUserCanvasEdit keeps add and remove for the manager', () => {
  const metadata = appendUserCanvasEdit(undefined, {
    op: 'add',
    node_id: 'n_image_2',
    label: 'Image 2',
    role: 'image',
    type: 'image',
    at: 1,
  });
  const next = appendUserCanvasEdit(metadata, {
    op: 'remove',
    node_id: 'n_clip_2',
    label: 'Clip 2',
    role: 'clip',
    type: 'video',
    at: 2,
  });
  assert.equal(next.user_topology_edit, true);
  assert.equal(next.user_canvas_edits.length, 2);
  assert.equal(next.user_canvas_edits[0].op, 'add');
  assert.equal(next.user_canvas_edits[1].node_id, 'n_clip_2');
  const wired = appendUserCanvasEdit(next, {
    op: 'connect',
    node_id: 'n_image_5',
    peer_id: 'n_clip_2',
    at: 3,
  });
  const swapped = appendUserCanvasEdit(wired, {
    op: 'replace',
    node_id: 'n_image_5',
    label: 'officer.png',
    at: 4,
  });
  assert.equal(swapped.user_canvas_edits[2].op, 'connect');
  assert.equal(swapped.user_canvas_edits[2].peer_id, 'n_clip_2');
  assert.equal(swapped.user_canvas_edits[3].op, 'replace');
  assert.equal(swapped.user_canvas_edits[3].label, 'officer.png');
  assert.equal(canvasEditGlance([{ op: 'add' }]), 'add');
  assert.equal(canvasEditGlance([{ op: 'connect' }]), 'connect');
  assert.equal(canvasEditGlance(swapped.user_canvas_edits.slice(2)), 'replace');
  assert.equal(canvasEditGlance([]), '');
});

test('connectNodeToGraph adds a data edge and predecessor input', () => {
  const template = DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'video');
  assert.ok(template);
  const source = {
    id: 'n_clip_1a',
    type: 'image',
    label: 'Clip 1',
    config: { role: 'clip' },
    layout: { x: 0, y: 0, width: 280, height: 160 },
  };
  const added = buildManualDesignerNode({
    template,
    existing: [source],
    position: positionRightOfNode(source, [source]),
  });
  const next = connectNodeToGraph({ nodes: [source], edges: [] }, source.id, added);
  assert.ok(next);
  assert.equal(next.nodes.length, 2);
  assert.equal(next.edges.length, 1);
  assert.equal(next.edges[0].source, 'n_clip_1a');
  assert.equal(next.edges[0].target, added.id);
  assert.deepEqual(next.nodes[1].config.inputs, ['n_clip_1a']);
});

test('removeNodesFromGraph drops edges and strips inputs', () => {
  const graph = {
    nodes: [
      { id: 'n_a', type: 'text', label: 'A', config: { role: 'brief' } },
      { id: 'n_b', type: 'image', label: 'B', config: { role: 'scene', inputs: ['n_a'] } },
      { id: 'n_c', type: 'video', label: 'C', config: { role: 'clip', inputs: ['n_a', 'n_b'] } },
    ],
    edges: [
      { id: 'e_ab', source: 'n_a', target: 'n_b' },
      { id: 'e_bc', source: 'n_b', target: 'n_c' },
    ],
  };
  const next = removeNodesFromGraph(graph, ['n_b']);
  assert.deepEqual(next.nodes.map((node) => node.id), ['n_a', 'n_c']);
  assert.equal(next.edges.length, 0);
  assert.deepEqual(next.nodes[1].config.inputs, ['n_a']);
});

test('parseContentAspect reads ratio and pixel sizes', () => {
  assert.equal(parseContentAspect('9:16'), 9 / 16);
  assert.equal(parseContentAspect('1080*1920'), 1080 / 1920);
  assert.equal(parseContentAspect('1K'), null);
});

test('contentAspectForNode reads the canvas setting for the node media type', () => {
  const metadata = { video_settings: { aspect_ratio: '9:16' }, image_settings: { aspect_ratio: '4:3' } };
  assert.equal(contentAspectForNode('video', {}, metadata), 9 / 16);
  assert.equal(contentAspectForNode('image', {}, metadata), 4 / 3);
  assert.equal(contentAspectForNode('text', {}, metadata), null);
  assert.equal(contentAspectForNode('video', {}, {}), null);
  assert.equal(
    contentAspectForNode('image', { comfyui: { fields: { width: 512, height: 1024 } } }, metadata),
    0.5,
  );
});

test('sizeNodeForContentAspect makes portrait nodes taller than wide', () => {
  const portrait = sizeNodeForContentAspect(9 / 16);
  assert.ok(portrait.height > portrait.width);
  const landscape = sizeNodeForContentAspect(16 / 9);
  assert.ok(landscape.width > landscape.height);
  assert.equal(isDefaultLandscapeNodeSize(280, 160), true);
  assert.equal(isDefaultLandscapeNodeSize(portrait.width, portrait.height), false);
});

test('packDesignerNodeLayouts separates stacked portrait media nodes', () => {
  const portrait = sizeNodeForContentAspect(9 / 16);
  const packed = packDesignerNodeLayouts([
    {
      id: 'n_clip_1a',
      type: 'image',
      label: 'Scene 1',
      config: { role: 'scene' },
      layout: { x: 1020, y: 40, width: 240, height: 140 },
    },
    {
      id: 'n_clip_2a',
      type: 'image',
      label: 'Scene 2',
      config: { role: 'scene' },
      layout: { x: 1020, y: 200, width: 240, height: 140 },
    },
    {
      id: 'n_clip_1',
      type: 'video',
      label: 'Clip 1',
      config: {},
      layout: { x: 1320, y: 40, width: 240, height: 140 },
    },
  ], { video_settings: { aspect_ratio: '9:16' }, image_settings: { aspect_ratio: '9:16' } });
  const scene1 = packed.find((node) => node.id === 'n_clip_1a');
  const scene2 = packed.find((node) => node.id === 'n_clip_2a');
  const clip1 = packed.find((node) => node.id === 'n_clip_1');
  assert.ok(scene1 && scene2 && clip1);
  const top = scene1.layout?.y ?? 0;
  const bottom = scene2.layout?.y ?? 0;
  const height = scene1.layout?.height ?? 0;
  assert.ok(height >= portrait.height - 1);
  assert.equal(bottom, top + height + DESIGNER_SUCCESSOR_GAP_Y);
  assert.equal(clip1.layout?.y, scene1.layout?.y);
  assert.equal(
    clip1.layout?.x,
    (scene1.layout?.x ?? 0) + (scene1.layout?.width ?? 0) + DESIGNER_SUCCESSOR_GAP_X,
  );
});

test('packDesignerNodeLayouts keeps a wide table from covering the next column', () => {
  const packed = packDesignerNodeLayouts([
    {
      id: 'n_scene',
      type: 'image',
      label: 'Scene',
      layout: { x: 400, y: 240, width: 280, height: 160 },
    },
    {
      id: 'n_storyboard',
      type: 'table',
      label: 'Table',
      layout: { x: 400, y: 440, width: 280, height: 280 },
    },
    {
      id: 'n_clip_1a',
      type: 'image',
      label: 'Scene 1',
      config: { role: 'scene' },
      layout: { x: 760, y: 240, width: 280, height: 160 },
    },
  ]);
  const scene = packed.find((node) => node.id === 'n_scene');
  const table = packed.find((node) => node.id === 'n_storyboard');
  const sceneSpec = packed.find((node) => node.id === 'n_clip_1a');
  assert.ok(scene && table && sceneSpec);
  assert.equal(scene.layout?.x, table.layout?.x);
  assert.equal(table.layout?.y, (scene.layout?.y ?? 0) + (scene.layout?.height ?? 0) + DESIGNER_SUCCESSOR_GAP_Y);
  assert.equal(
    sceneSpec.layout?.x,
    (table.layout?.x ?? 0) + (table.layout?.width ?? 0) + DESIGNER_SUCCESSOR_GAP_X,
  );
  assert.ok((sceneSpec.layout?.x ?? 0) >= (table.layout?.x ?? 0) + (table.layout?.width ?? 0));
});

test('resolvedNodeCanvasSize ignores a zero stored size', () => {
  const size = resolvedNodeCanvasSize({
    id: 'n_scene_1',
    type: 'image',
    label: 'Scene',
    config: {},
    layout: { x: 10, y: 20, width: 0, height: 0 },
  });
  assert.equal(size.width, 280);
  assert.equal(size.height, 160);
});

test('resolvedNodeCanvasSize uses the canvas ratio on default landscape cards', () => {
  const size = resolvedNodeCanvasSize(
    {
      id: 'n_clip_1',
      type: 'video',
      label: 'Clip 1',
      config: {},
      layout: { x: 0, y: 0, width: 280, height: 160 },
    },
    { video_settings: { aspect_ratio: '9:16' } },
  );
  assert.ok(size.height > size.width);
});

test('sizeNodeForDocumentContent hugs short text and keeps tables near square', () => {
  const short = sizeNodeForDocumentContent({ width: 72, height: 16 }, 'text');
  assert.ok(short.width >= 180);
  assert.ok(short.height >= DESIGNER_NODE_HEADER_HEIGHT + DESIGNER_DOC_FIT_MIN_BODY);
  assert.ok(short.height < 160);

  const compact = sizeNodeForDocumentContent({ width: 320, height: 120 }, 'table');
  assert.equal(compact.width, 180);
  assert.equal(compact.height, 120 + DESIGNER_NODE_HEADER_HEIGHT);

  const square = sizeNodeForDocumentContent({ width: 240, height: 200 }, 'table');
  assert.equal(square.width, square.height);

  const wide = sizeNodeForDocumentContent({ width: 900, height: 800 }, 'table');
  assert.equal(wide.width, 280);
  assert.equal(wide.height, 280);
});

test('autoLayoutDesignerNodes layers successors to the right and stacks without overlap', () => {
  const laid = autoLayoutDesignerNodes(
    [
      {
        id: 'n_brief',
        type: 'text',
        label: 'Brief',
        layout: { x: 900, y: 20, width: 280, height: 160 },
      },
      {
        id: 'n_character',
        type: 'image',
        label: 'Character',
        config: { pipeline: 'character', inputs: ['n_brief'] },
        layout: { x: 10, y: 400, width: 280, height: 160 },
      },
      {
        id: 'n_scene',
        type: 'image',
        label: 'Scene',
        config: { pipeline: 'scene', inputs: ['n_brief'] },
        layout: { x: 40, y: 10, width: 280, height: 160 },
      },
      {
        id: 'n_clip_2a',
        type: 'image',
        label: 'Scene 2',
        config: { pipeline: 'scene', clip_index: 2, inputs: ['n_scene'] },
        layout: { x: 40, y: 40, width: 180, height: 320 },
      },
      {
        id: 'n_clip_1a',
        type: 'image',
        label: 'Scene 1',
        config: { pipeline: 'scene', clip_index: 1, inputs: ['n_scene'] },
        layout: { x: 80, y: 80, width: 180, height: 320 },
      },
    ],
    [
      { id: 'e_brief_char', source: 'n_brief', target: 'n_character' },
      { id: 'e_brief_scene', source: 'n_brief', target: 'n_scene' },
      { id: 'e_scene_c1a', source: 'n_scene', target: 'n_clip_1a' },
      { id: 'e_scene_c2a', source: 'n_scene', target: 'n_clip_2a' },
    ],
  );
  const brief = laid.find((node) => node.id === 'n_brief');
  const character = laid.find((node) => node.id === 'n_character');
  const scene = laid.find((node) => node.id === 'n_scene');
  const scene1 = laid.find((node) => node.id === 'n_clip_1a');
  const scene2 = laid.find((node) => node.id === 'n_clip_2a');
  assert.ok(brief && character && scene && scene1 && scene2);
  assert.equal(brief.layout?.x, DESIGNER_LAYOUT_ORIGIN_X);
  assert.equal(brief.layout?.y, DESIGNER_LAYOUT_ORIGIN_Y);
  assert.equal(character.layout?.x, (brief.layout?.x ?? 0) + (brief.layout?.width ?? 0) + DESIGNER_SUCCESSOR_GAP_X);
  assert.equal(scene.layout?.x, (character.layout?.x ?? 0) + (character.layout?.width ?? 0) + DESIGNER_SUCCESSOR_GAP_X);
  assert.equal(scene1.layout?.x, scene.layout?.x);
  assert.equal(scene2.layout?.x, scene.layout?.x);
  assert.equal(scene2.layout?.y, (scene1.layout?.y ?? 0) + (scene1.layout?.height ?? 0) + DESIGNER_SUCCESSOR_GAP_Y);
});

test('autoLayoutDesignerNodes stacks every scene in one column and every clip in one column', () => {
  const laid = autoLayoutDesignerNodes(
    [
      {
        id: 'n_brief',
        type: 'text',
        label: 'Brief',
        layout: { x: 0, y: 0, width: 280, height: 160 },
      },
      {
        id: 'n_scene_1',
        type: 'image',
        label: 'Scene 1',
        config: { pipeline: 'scene', clip_index: 1, inputs: ['n_brief'] },
        layout: { x: 400, y: 0, width: 280, height: 160 },
      },
      {
        id: 'n_scene_2',
        type: 'image',
        label: 'Scene 2',
        config: { pipeline: 'scene', clip_index: 2, inputs: ['n_scene_1'] },
        layout: { x: 800, y: 0, width: 280, height: 160 },
      },
      {
        id: 'n_clip_1',
        type: 'video',
        label: 'Clip 1',
        config: { pipeline: 'clip', clip_index: 1, inputs: ['n_scene_1'] },
        layout: { x: 1200, y: 0, width: 280, height: 160 },
      },
      {
        id: 'n_clip_2',
        type: 'video',
        label: 'Clip 2',
        config: { pipeline: 'clip', clip_index: 2, inputs: ['n_scene_2'] },
        layout: { x: 1600, y: 200, width: 280, height: 160 },
      },
    ],
    [
      { id: 'e_b_s1', source: 'n_brief', target: 'n_scene_1' },
      { id: 'e_s1_s2', source: 'n_scene_1', target: 'n_scene_2' },
      { id: 'e_s1_c1', source: 'n_scene_1', target: 'n_clip_1' },
      { id: 'e_s2_c2', source: 'n_scene_2', target: 'n_clip_2' },
    ],
  );
  const scene1 = laid.find((node) => node.id === 'n_scene_1');
  const scene2 = laid.find((node) => node.id === 'n_scene_2');
  const clip1 = laid.find((node) => node.id === 'n_clip_1');
  const clip2 = laid.find((node) => node.id === 'n_clip_2');
  assert.ok(scene1 && scene2 && clip1 && clip2);
  assert.equal(scene1.layout?.x, scene2.layout?.x);
  assert.equal(clip1.layout?.x, clip2.layout?.x);
  assert.ok((clip1.layout?.x ?? 0) > (scene1.layout?.x ?? 0));
  assert.equal(scene2.layout?.y, (scene1.layout?.y ?? 0) + (scene1.layout?.height ?? 0) + DESIGNER_SUCCESSOR_GAP_Y);
  assert.equal(clip2.layout?.y, (clip1.layout?.y ?? 0) + (clip1.layout?.height ?? 0) + DESIGNER_SUCCESSOR_GAP_Y);
});

test('autoLayoutDesignerNodes does not put scene specs in the clip column', () => {
  const laid = autoLayoutDesignerNodes(
    [
      {
        id: 'n_scene_1',
        type: 'image',
        label: 'Scene 1',
        config: { role: 'scene', pipeline: 'scene', clip_index: 1 },
        layout: { x: 0, y: 0, width: 180, height: 320 },
      },
      {
        id: 'n_scene_2',
        type: 'image',
        label: 'Scene 2',
        config: { role: 'scene', pipeline: 'scene', clip_index: 2, inputs: ['n_scene_1'] },
        layout: { x: 400, y: 0, width: 180, height: 320 },
      },
      {
        id: 'n_clip_1',
        type: 'video',
        label: 'Clip 1',
        config: { role: 'clip', pipeline: 'clip', clip_index: 1, inputs: ['n_scene_1'] },
        layout: { x: 800, y: 0, width: 280, height: 160 },
      },
      {
        id: 'n_clip_2',
        type: 'video',
        label: 'Clip 2',
        config: { role: 'clip', pipeline: 'clip', clip_index: 2, inputs: ['n_scene_2'] },
        layout: { x: 1200, y: 200, width: 280, height: 160 },
      },
    ],
    [
      { id: 'e_s1_s2', source: 'n_scene_1', target: 'n_scene_2' },
      { id: 'e_s1_c1', source: 'n_scene_1', target: 'n_clip_1' },
      { id: 'e_s2_c2', source: 'n_scene_2', target: 'n_clip_2' },
    ],
  );
  const scene1 = laid.find((node) => node.id === 'n_scene_1');
  const scene2 = laid.find((node) => node.id === 'n_scene_2');
  const clip1 = laid.find((node) => node.id === 'n_clip_1');
  const clip2 = laid.find((node) => node.id === 'n_clip_2');
  assert.ok(scene1 && scene2 && clip1 && clip2);
  assert.equal(scene1.layout?.x, scene2.layout?.x);
  assert.equal(clip1.layout?.x, clip2.layout?.x);
  assert.ok((clip1.layout?.x ?? 0) > (scene1.layout?.x ?? 0));
  const boxes = laid.map((node) => ({
    id: node.id,
    x: node.layout?.x ?? 0,
    y: node.layout?.y ?? 0,
    w: node.layout?.width ?? 0,
    h: node.layout?.height ?? 0,
  }));
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const left = boxes[i];
      const right = boxes[j];
      const overlap =
        left.x < right.x + right.w &&
        right.x < left.x + left.w &&
        left.y < right.y + right.h &&
        right.y < left.y + left.h;
      assert.equal(overlap, false, `${left.id} overlaps ${right.id}`);
    }
  }
});

test('portAddTemplatesFor limits port menus to the allowed node types', () => {
  const types = (nodeType, side) => portAddTemplatesFor(nodeType, side).map((item) => item.type);
  // Port menus are derived from the shared wire matrix (issue #5382): a menu may
  // only offer connections that free dragging accepts, and only for the node
  // templates that exist (image / video / audio).
  for (const docType of ['text', 'table']) {
    assert.deepEqual(types(docType, 'predecessor'), []);
    assert.deepEqual(types(docType, 'successor'), ['image', 'video']);
  }
  // An image may also feed another image: `ImageNodeHandler` passes incoming
  // image edges to the model as references (style-reference generation).
  assert.deepEqual(types('image', 'predecessor'), ['image']);
  assert.deepEqual(types('image', 'successor'), ['image', 'video']);
  assert.deepEqual(types('video', 'predecessor'), ['image', 'video', 'audio']);
  assert.deepEqual(types('video', 'successor'), ['video']);
  assert.deepEqual(types('audio', 'predecessor'), []);
  assert.deepEqual(types('audio', 'successor'), ['video']);
  assert.deepEqual(types('unknown', 'successor'), []);
});
