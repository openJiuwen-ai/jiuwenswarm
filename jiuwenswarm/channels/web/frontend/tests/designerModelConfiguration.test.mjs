import assert from 'node:assert/strict';
import test from 'node:test';
import {
  readGenerationSelection,
  selectionAfterPick,
  selectionForReload,
  slotsToKeepOnReload,
} from '../node_modules/.cache/designer-model-configuration/designerModelConfiguration.mjs';

function model(overrides = {}) {
  return {
    model_id: 'visual-a',
    display_name: 'Visual A',
    purpose: 'generation',
    capabilities: ['visual_gen'],
    provider: 'MiniMax',
    protocol: 'minimax',
    model_name: 'MiniMax-H3-Image',
    api_base: 'https://api.minimax.io/v1',
    api_key_configured: true,
    references: [],
    ...overrides,
  };
}

const models = [
  model(),
  model({
    model_id: 'visual-b',
    display_name: 'Visual B',
    model_name: 'MiniMax-H3-Image-Highspeed',
  }),
  model({
    model_id: 'video-a',
    display_name: 'Video A',
    capabilities: ['video_gen'],
    model_name: 'MiniMax-H3',
  }),
  model({
    model_id: 'video-b',
    display_name: 'Video B',
    capabilities: ['video_gen'],
    model_name: 'MiniMax-H3-Highspeed',
  }),
];

test('picking a generation model updates that slot immediately and leaves the other slot alone', () => {
  const current = readGenerationSelection({ visual_gen: 'visual-a', video_gen: 'video-a' }, models);
  assert.deepEqual(current, { visual_gen: 'visual-a', video_gen: 'video-a' });
  assert.deepEqual(
    selectionAfterPick(current, 'visual_gen', 'visual-b', models),
    { visual_gen: 'visual-b', video_gen: 'video-a' },
  );
});

test('a config reload keeps an in-flight pick instead of restoring the stale saved value', () => {
  const saved = { visual_gen: 'visual-a', video_gen: 'video-a' };
  const current = { visual_gen: 'visual-b', video_gen: 'video-a' };
  assert.deepEqual(
    selectionForReload(saved, current, ['visual_gen'], models),
    { visual_gen: 'visual-b', video_gen: 'video-a' },
  );
  assert.deepEqual(
    selectionForReload({ visual_gen: 'visual-a', video_gen: 'video-b' }, current, ['visual_gen'], models),
    { visual_gen: 'visual-b', video_gen: 'video-b' },
  );
  assert.deepEqual(
    selectionForReload(saved, current, [], models),
    { visual_gen: 'visual-a', video_gen: 'video-a' },
  );
});

test('a reload that started before a pick keeps that slot after the write is no longer pending', () => {
  const lastPickEpoch = new Map([['visual_gen', 2]]);
  const startedBeforePick = 1;
  const keep = slotsToKeepOnReload([], lastPickEpoch, startedBeforePick);
  assert.deepEqual(keep, ['visual_gen']);
  const saved = { visual_gen: 'visual-a', video_gen: 'video-a' };
  const current = { visual_gen: 'visual-b', video_gen: 'video-a' };
  assert.deepEqual(
    selectionForReload(saved, current, keep, models),
    { visual_gen: 'visual-b', video_gen: 'video-a' },
  );
  assert.deepEqual(
    slotsToKeepOnReload([], lastPickEpoch, 2),
    [],
  );
  assert.deepEqual(
    selectionForReload(saved, current, slotsToKeepOnReload([], lastPickEpoch, 2), models),
    { visual_gen: 'visual-a', video_gen: 'video-a' },
  );
});
