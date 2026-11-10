import assert from 'node:assert/strict';
import test from 'node:test';

import {
  clipSceneReferenceWarning,
  collectSceneDeletionImpacts,
  designerNodePipeline,
  resolveClipScene,
} from '../node_modules/.cache/designer-scene-binding/designerSceneBinding.js';

const scene = (id, settingId, label = '') => ({
  id,
  type: 'image',
  label: label || id,
  config: { role: 'scene', setting_id: settingId },
});

const clip = (id, config = {}, label = '') => ({
  id,
  type: 'video',
  label: label || id,
  config: { role: 'clip', ...config },
});

test('pipeline resolution mirrors backend: pipeline, role, then id prefix', () => {
  assert.equal(designerNodePipeline({ id: 'x1', config: { pipeline: 'clip' } }), 'clip');
  assert.equal(designerNodePipeline({ id: 'x2', config: { role: 'scene' } }), 'scene');
  assert.equal(designerNodePipeline({ id: 'n_scene_2', config: {} }), 'scene');
  assert.equal(designerNodePipeline({ id: 'n_clip_1', config: {} }), 'clip');
  assert.equal(designerNodePipeline({ id: 'node_9', config: {} }), '');
});

test('declared scene_node_id wins; setting_id matches; a lone scene binds', () => {
  const s1 = scene('n_scene_1', 'set_1');
  const s2 = scene('n_scene_2', 'set_2');
  const nodes = [s1, s2];

  assert.equal(resolveClipScene(nodes, { scene_node_id: 'n_scene_2' })?.id, 'n_scene_2');
  assert.equal(resolveClipScene(nodes, { setting_id: 'set_1' })?.id, 'n_scene_1');
  assert.equal(resolveClipScene(nodes, { setting_id: 'set_9' }), null);
  assert.equal(resolveClipScene([s1], { setting_id: 'set_9' }), null);
  assert.equal(resolveClipScene([s1], {})?.id, 'n_scene_1');
  assert.equal(resolveClipScene([], { setting_id: 'set_1' }), null);
});

test('no warning when the clip binds as declared or never declared a scene', () => {
  const s1 = scene('n_scene_1', 'set_1');
  const nodes = [s1, clip('n_clip_1', { scene_node_id: 'n_scene_1' }), clip('n_clip_2', {})];

  assert.equal(clipSceneReferenceWarning(nodes, nodes[1]), null);
  assert.equal(clipSceneReferenceWarning(nodes, nodes[2]), null);
  assert.equal(clipSceneReferenceWarning(nodes, scene('n_scene_x', 'set_1')), null);
});

test('a deleted declared scene warns rebind or missing by what remains', () => {
  const s1 = scene('n_scene_1', 'set_1', 'Library');
  const s2 = scene('n_scene_2', 'set_2');
  const gone = { scene_node_id: 'n_scene_gone' };
  const rebound = clip('n_clip_1', { ...gone, setting_id: 'set_1' });
  const orphaned = clip('n_clip_2', { ...gone, setting_id: 'set_9' });

  assert.deepEqual(clipSceneReferenceWarning([s1, s2, rebound], rebound), {
    kind: 'rebind',
    sceneLabel: 'Library',
  });
  assert.deepEqual(clipSceneReferenceWarning([s1, s2, orphaned], orphaned), { kind: 'missing' });
});

test('a setting_id no scene card provides warns missing (legacy graphs)', () => {
  const s1 = scene('n_scene_1', 'set_1');
  const s2 = scene('n_scene_2', 'set_2');
  const legacy = clip('n_clip_9', { setting_id: 'set_9' });

  assert.deepEqual(clipSceneReferenceWarning([s1, s2, legacy], legacy), { kind: 'missing' });
  // A lone scene with a different setting does not bind, so the warning stays.
  assert.deepEqual(clipSceneReferenceWarning([s1, legacy], legacy), { kind: 'missing' });
});

test('deletion impacts list only clips bound to the deleted scene', () => {
  const s1 = scene('n_scene_1', 'set_1', 'Library');
  const s2 = scene('n_scene_2', 'set_2', 'Pier');
  const c1 = clip('n_clip_1', { scene_node_id: 'n_scene_1' }, 'Opening');
  const c2 = clip('n_clip_2', { setting_id: 'set_1' }, 'Reading');
  const c3 = clip('n_clip_3', { setting_id: 'set_2' }, 'Walking');
  const nodes = [s1, s2, c1, c2, c3];

  // Deleting set_1's card: c1 declared it and has no setting_id, so the lone
  // remaining card (Pier) takes over; c2's setting set_1 has no card left and
  // the lone Pier card belongs to set_2, so it goes missing. c3 is untouched.
  assert.deepEqual(collectSceneDeletionImpacts(nodes, ['n_scene_1']), [
    { clipId: 'n_clip_1', clipLabel: 'Opening', outcome: { kind: 'rebind', sceneLabel: 'Pier' } },
    { clipId: 'n_clip_2', clipLabel: 'Reading', outcome: { kind: 'missing' } },
  ]);
  // Deleting set_2's card: c3's setting has no other card, and the lone
  // Library card belongs to set_1 ≠ set_2, so it goes missing.
  assert.deepEqual(collectSceneDeletionImpacts(nodes, ['n_scene_2']), [
    { clipId: 'n_clip_3', clipLabel: 'Walking', outcome: { kind: 'missing' } },
  ]);
  assert.deepEqual(collectSceneDeletionImpacts(nodes, []), []);
  assert.deepEqual(collectSceneDeletionImpacts(nodes, ['n_clip_1']), []);
});

test('a clip rebound to a surviving same-setting scene reports the rebind', () => {
  const primary = scene('n_scene_1', 'set_1', 'Library day');
  const duplicate = scene('n_scene_1b', 'set_1', 'Library night');
  const c1 = clip('n_clip_1', { scene_node_id: 'n_scene_1' }, 'Opening');

  assert.deepEqual(collectSceneDeletionImpacts([primary, duplicate, c1], ['n_scene_1']), [
    {
      clipId: 'n_clip_1',
      clipLabel: 'Opening',
      outcome: { kind: 'rebind', sceneLabel: 'Library night' },
    },
  ]);
});
