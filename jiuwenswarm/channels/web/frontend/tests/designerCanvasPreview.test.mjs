import './fixtures/browserEnvironment.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  graphForBootstrapThinking,
  isDesignerPreviewGraph,
} from '../node_modules/.cache/designer-canvas-preview/designerBootstrapGraph.js';
import {
  EMPTY_STORYBOARD_TABLE,
  parseMarkdownTable,
  sanitizeMarkdownTableCell,
  serializeMarkdownTable,
  storyboardClipPreviews,
} from '../node_modules/.cache/designer-canvas-preview/designerNodePreview.js';
import {
  resolveDesignerGraphToLoad,
  summariesFromGraphList,
} from '../node_modules/.cache/designer-canvas-preview/designerGraphLoad.js';

test('bootstrap thinking canvas stays empty so it is not mistaken for a real graph', () => {
  const graph = graphForBootstrapThinking('火车进站');
  assert.equal(isDesignerPreviewGraph(graph), true);
  assert.deepEqual(graph.nodes, []);
  assert.deepEqual(graph.edges, []);
  assert.equal(graph.description, '火车进站');
});

test('bootstrap thinking canvas never reuses another project graph', () => {
  const previous = {
    ...graphForBootstrapThinking('旧项目'),
    graph_id: 'graph_previous_project',
    nodes: [{ id: 'n_brief' }],
  };
  const next = graphForBootstrapThinking('新的情人节短片');
  assert.equal(isDesignerPreviewGraph(next), true);
  assert.notEqual(next.graph_id, previous.graph_id);
  assert.equal(next.nodes.length, 0);
  assert.equal(next.description, '新的情人节短片');
});

const STORYBOARD_HEADER = '| Shot | Timeline | Camera | Move | On screen | Character action | Speech |';
const STORYBOARD_SEPARATOR = '| --- | --- | --- | --- | --- | --- | --- |';

test('storyboardClipPreviews shows action and camera from the storyboard columns', () => {
  const clips = storyboardClipPreviews(
    [
      STORYBOARD_HEADER,
      STORYBOARD_SEPARATOR,
      '| 1 | 0.0-2.0s | wide / eye-level | slow pan | Ann | steps off the train | |',
      '| 2 | 2.0-5.0s | medium | | Ann | walks toward the exit | "Home." |',
    ].join('\n'),
  );
  assert.equal(clips.length, 2);
  assert.equal(clips[0].clipNo, '1');
  assert.equal(clips[0].action, 'steps off the train');
  assert.equal(clips[0].picture, 'wide / eye-level / slow pan');
  assert.equal(clips[1].action, 'walks toward the exit');
  assert.equal(clips[1].picture, 'medium');
});

test('storyboardClipPreviews keeps every row', () => {
  const rows = Array.from(
    { length: 10 },
    (_, index) => `| ${index + 1} | ${index}.0s | wide | hold | Ann | beat ${index + 1} | |`,
  );
  const clips = storyboardClipPreviews([STORYBOARD_HEADER, STORYBOARD_SEPARATOR, ...rows].join('\n'), 20);
  assert.equal(clips.length, 10);
  assert.equal(clips[9].action, 'beat 10');
});

test('empty storyboard table is a blank seven-column frame', () => {
  assert.deepEqual(EMPTY_STORYBOARD_TABLE.headers, [
    'Shot',
    'Timeline',
    'Camera',
    'Move',
    'On screen',
    'Character action',
    'Speech',
  ]);
  assert.equal(EMPTY_STORYBOARD_TABLE.rows.length, 2);
  assert.ok(EMPTY_STORYBOARD_TABLE.rows.every((row) => row.length === 7 && row.every((cell) => cell === '')));
});

test('serializeMarkdownTable round-trips edited cells and added or removed rows', () => {
  const table = parseMarkdownTable(
    [
      STORYBOARD_HEADER,
      STORYBOARD_SEPARATOR,
      '| 1 | 0.0-2.0s | wide | static | Ann | steps off the train | |',
      '| 2 | 2.0-5.0s | medium | pan | Ann | walks to the exit | |',
    ].join('\n'),
  );
  assert.ok(table);
  const rows = [
    table.rows[0].map((cell, index) => (index === 5 ? 'steps off | the\ntrain' : cell)),
    ['3', '5.0-7.0s', '', '', '', 'waves', ''],
  ];
  const text = serializeMarkdownTable({ headers: table.headers, rows });
  assert.ok(text.startsWith(`${STORYBOARD_HEADER}\n${STORYBOARD_SEPARATOR}\n`));
  const reparsed = parseMarkdownTable(text);
  assert.ok(reparsed);
  assert.deepEqual(reparsed.headers, table.headers);
  assert.equal(reparsed.rows.length, 2);
  assert.equal(reparsed.rows[0][5], 'steps off / the train');
  assert.deepEqual(reparsed.rows[1], ['3', '5.0-7.0s', '', '', '', 'waves', '']);
  const clips = storyboardClipPreviews(text);
  assert.deepEqual(
    clips.map((clip) => clip.clipNo),
    ['1', '3'],
  );
});

test('sanitizeMarkdownTableCell keeps a cell on one Markdown row', () => {
  assert.equal(sanitizeMarkdownTableCell('a | b\r\nc'), 'a / b c');
});

test('parseMarkdownTable keeps a table frame from generated markdown', () => {
  const table = parseMarkdownTable(
    [
      '| Clip | Timeline | Camera | Move | Character action | Scene change | Comment |',
      '| --- | --- | --- | --- | --- | --- | --- |',
      '| 1 | 0.0-2.0s | wide | static | steps off the train | platform morning light | Wide shot of a young man leaving the train |',
      '| 2 | 2.0-5.0s | medium | pan | walks toward the exit | same station | Medium shot walking through the concourse |',
    ].join('\n'),
  );
  assert.ok(table);
  assert.deepEqual(table.headers, [
    'Clip',
    'Timeline',
    'Camera',
    'Move',
    'Character action',
    'Scene change',
    'Comment',
  ]);
  assert.equal(table.rows.length, 2);
  assert.equal(table.rows[0][6], 'Wide shot of a young man leaving the train');
  assert.equal(table.rows[1][4], 'walks toward the exit');
});

test('parseMarkdownTable keeps every storyboard row', () => {
  const lines = [
    '| Clip | Timeline | Action |',
    '| --- | --- | --- |',
    ...Array.from({ length: 24 }, (_, index) => `| ${index + 1} | ${index}.0s | beat ${index + 1} |`),
  ];
  const table = parseMarkdownTable(lines.join('\n'));
  assert.ok(table);
  assert.equal(table.rows.length, 24);
  assert.equal(table.rows[23][2], 'beat 24');
});

test('resolveDesignerGraphToLoad keeps the selected graph instead of the first listed one', () => {
  assert.equal(
    resolveDesignerGraphToLoad({
      currentId: 'graph_second',
      listedIds: ['graph_first', 'graph_second'],
    }),
    'graph_second',
  );
  assert.equal(
    resolveDesignerGraphToLoad({
      currentId: 'graph_other_project',
      listedIds: ['graph_first'],
    }),
    'graph_other_project',
  );
  assert.equal(
    resolveDesignerGraphToLoad({
      currentId: 'preview_bootstrap',
      isPreview: true,
      listedIds: ['graph_first'],
    }),
    'graph_first',
  );
  assert.equal(
    resolveDesignerGraphToLoad({
      currentId: '',
      listedIds: ['graph_first', 'graph_second'],
    }),
    'graph_first',
  );
});

test('resolveDesignerGraphToLoad restores the last opened graph after refresh', () => {
  assert.equal(
    resolveDesignerGraphToLoad({
      currentId: '',
      listedIds: ['graph_first', 'graph_second'],
      lastId: 'graph_second',
    }),
    'graph_second',
  );
  assert.equal(
    resolveDesignerGraphToLoad({
      currentId: '',
      isPreview: true,
      listedIds: ['graph_first'],
      lastId: 'graph_kept',
    }),
    'graph_kept',
  );
  assert.equal(
    resolveDesignerGraphToLoad({
      currentId: 'preview_bootstrap',
      isPreview: true,
      listedIds: [],
      lastId: 'graph_kept',
    }),
    'graph_kept',
  );
});

test('summariesFromGraphList prefers summaries then falls back to graphs', () => {
  const summaries = summariesFromGraphList({
    summaries: [
      { graph_id: 'graph_a', project_id: 'p1', title: 'First', has_video: false },
      { graph_id: '', project_id: 'p1', title: 'Skipped', has_video: false },
    ],
    graphs: [{ graph_id: 'graph_b', project_id: 'p1', title: 'Ignored' }],
  });
  assert.deepEqual(
    summaries.map((item) => item.graph_id),
    ['graph_a'],
  );
  const fromGraphs = summariesFromGraphList({
    summaries: [],
    graphs: [
      {
        graph_id: 'graph_b',
        project_id: 'p1',
        title: 'Station',
        nodes: [{ output_ref: { kind: 'video', uri: 'file:///clip.mp4' } }],
      },
    ],
  });
  assert.equal(fromGraphs[0]?.graph_id, 'graph_b');
  assert.equal(fromGraphs[0]?.has_video, true);
});
