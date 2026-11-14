import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DESIGNER_REFS_MARKER,
  collectMentionedNodes,
  detectAtToken,
  detectMentionQuery,
  filterMentionOptions,
  insertMention,
  isPreviewableMention,
  mentionOptions,
  stripMentionAppendix,
  withMentionAppendix,
} from '../node_modules/.cache/designer-mentions/designerMentions.js';

const graph = {
  graph_id: 'g1',
  project_id: 'p1',
  title: 't',
  schema_version: '1',
  edges: [],
  nodes: [
    { id: 'n_brief', type: 'text', label: 'Brief: 肚皮滑行', output_ref: { kind: 'text', uri: 'file:///C:/w/brief.md' } },
    {
      id: 'n_character',
      type: 'image',
      label: 'Character 1: 小企鹅',
      output_ref: { kind: 'image', uri: 'file:///C:/Users/a/.jiuwenswarm/p/image_1.png' },
    },
    { id: 'n_clip_1', type: 'video', label: 'Clip 1', output_ref: { kind: 'video', uri: 'file:///C:/w/v1.mp4' } },
    { id: 'n_clip_10', type: 'video', label: 'Clip 10', output_ref: { kind: 'video', uri: 'file:///C:/w/v10.mp4' } },
    { id: 'n_scene', type: 'image', label: 'Scene 1', output_ref: { kind: 'image', uri: 'designer://placeholder' } },
  ],
};

test('detectAtToken finds the token the caret is in', () => {
  assert.deepEqual(detectAtToken('@', 1), { start: 0, query: '' });
  assert.deepEqual(detectAtToken('make @Cha', 9), { start: 5, query: 'Cha' });
  assert.equal(detectAtToken('mail a@b', 8), null, 'an @ inside a word is not a mention');
  assert.equal(detectAtToken('@Clip 1 done', 12), null, 'the token ended at the space');
  assert.equal(detectAtToken('no mention', 10), null);
});

test('mentionOptions lists labelled nodes newest first, without placeholder outputs', () => {
  const options = mentionOptions(graph);
  assert.deepEqual(options.map((o) => o.nodeId), ['n_scene', 'n_clip_10', 'n_clip_1', 'n_character', 'n_brief']);
  assert.equal(options.find((o) => o.nodeId === 'n_scene').uri, null);
  assert.deepEqual(filterMentionOptions(options, 'char').map((o) => o.nodeId), ['n_character']);
  assert.deepEqual(filterMentionOptions(options, '企鹅').map((o) => o.nodeId), ['n_character']);
  assert.equal(filterMentionOptions(options, '').length, 5);
});

test('insertMention replaces the typed token and places the caret after it', () => {
  const value = '把 @cha 表情改开心';
  const token = detectAtToken(value, 6);
  const out = insertMention(value, token, 'Character 1: 小企鹅');
  assert.equal(out.value, '把 @Character 1: 小企鹅  表情改开心');
  assert.equal(out.caret, '把 @Character 1: 小企鹅 '.length);
});

test('collectMentionedNodes prefers the longest label and keeps canvas order', () => {
  const nodes = collectMentionedNodes(graph, '@Clip 10 加快节奏，@Character 1: 小企鹅 更开心');
  assert.deepEqual(nodes.map((n) => n.nodeId), ['n_character', 'n_clip_10']);
  assert.deepEqual(collectMentionedNodes(graph, '@Clip 1 重做').map((n) => n.nodeId), ['n_clip_1']);
  assert.deepEqual(collectMentionedNodes(graph, 'no mentions'), []);
  assert.deepEqual(
    collectMentionedNodes(graph, '@Brief: 肚皮滑行 @Character 1: 小企鹅').filter(isPreviewableMention).map((n) => n.nodeId),
    ['n_character'],
    'only image/video outputs get a thumbnail chip',
  );
});

test('the leader appendix maps labels to node ids and is stripped for display', () => {
  const text = '@Character 1: 小企鹅 表情更开心';
  const sent = withMentionAppendix(text, graph);
  assert.ok(sent.startsWith(text));
  assert.ok(sent.includes(DESIGNER_REFS_MARKER));
  assert.ok(sent.includes('- @Character 1: 小企鹅 → node_id=n_character, type=image, output=C:/Users/a/.jiuwenswarm/p/image_1.png'));
  assert.ok(sent.includes('swarmflow(node_id=<id>)'), 'the leader is told to rerun the node with swarmflow');
  assert.ok(!sent.includes('designer_run_graph'), 'no stale tool name');
  assert.equal(stripMentionAppendix(sent), text);
  assert.equal(withMentionAppendix('plain request', graph), 'plain request');
  assert.ok(withMentionAppendix('@Scene 1 换成夜景', graph).includes('output=(not generated yet)'));
});

test('detectMentionQuery keeps the menu open across spaces inside a label', () => {
  const options = mentionOptions(graph);
  assert.deepEqual(detectMentionQuery('@Clip 1', 7, options), { start: 0, query: 'Clip 1' }, 'label prefix with a space');
  assert.deepEqual(
    detectMentionQuery('把 @Character 1: 小', 18, options),
    { start: 2, query: 'Character 1: 小' },
    'prefix through the colon and CJK',
  );
  const chosen = '@Character 1: 小企鹅 表情更开心';
  assert.equal(detectMentionQuery(chosen, chosen.length, options), null, 'closed once the request text follows');
  const justChosen = '@Character 1: 小企鹅 ';
  assert.equal(detectMentionQuery(justChosen, justChosen.length, options), null, 'closed right after insertion');
  assert.equal(detectMentionQuery('@Nothing here', 13, options), null, 'no label starts with it');
  assert.equal(detectMentionQuery('mail a@b c', 10, options), null, '@ inside a word');
});
