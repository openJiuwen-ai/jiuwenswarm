import assert from 'node:assert/strict';
import test from 'node:test';

import {
  effectiveImageSettings,
  effectiveVideoDuration,
  effectiveVideoSettings,
  generatesTimedVideo,
  generationModelLabel,
} from '../node_modules/.cache/designer-media-settings/designerMediaSettings.js';

const caps = {
  video: {
    configured_model: 'MiniMax-H3',
    backend: 'minimax',
    model_id: 'MiniMax-H3',
    provider: 'minimax',
    ratios: ['21:9', '16:9', '4:3', '1:1', '3:4', '9:16'],
    resolutions: ['768P', '2K'],
    default_ratio: '16:9',
    default_resolution: '768P',
    min_sec: 4,
    max_sec: 15,
    default_sec: 5,
  },
  image: {
    configured_model: 'seedream-4-0-250828',
    backend: 'modelark',
    ratios: ['16:9', '4:3', '1:1', '3:4', '9:16'],
    resolutions: ['1K', '2K', '4K'],
    default_ratio: '16:9',
    default_resolution: '1K',
  },
  settings: {
    video: { aspect_ratio: '16:9', resolution: '2K' },
    image: { aspect_ratio: '1:1', resolution: '2K' },
  },
  video_durations: { n_clip_1: 8 },
};

test('stored canvas settings are shown with the model spelling', () => {
  const metadata = {
    video_settings: { aspect_ratio: '9:16', resolution: '2k' },
    image_settings: { aspect_ratio: '3:4', resolution: '4k' },
  };
  assert.deepEqual(effectiveVideoSettings(metadata, caps), { aspect_ratio: '9:16', resolution: '2K' });
  assert.deepEqual(effectiveImageSettings(metadata, caps), { aspect_ratio: '3:4', resolution: '4K' });
});

test('values the model does not list fall back to the server snapped settings', () => {
  const metadata = {
    video_settings: { aspect_ratio: '5:4', resolution: '1080P' },
    image_settings: { aspect_ratio: '21:9', resolution: '1.5K' },
  };
  assert.deepEqual(effectiveVideoSettings(metadata, caps), { aspect_ratio: '16:9', resolution: '2K' });
  assert.deepEqual(effectiveImageSettings(metadata, caps), { aspect_ratio: '1:1', resolution: '2K' });
  const { settings: _settings, ...bare } = caps;
  assert.deepEqual(effectiveVideoSettings(undefined, bare), { aspect_ratio: '16:9', resolution: '768P' });
  assert.deepEqual(effectiveImageSettings(undefined, bare), { aspect_ratio: '16:9', resolution: '1K' });
});

test('video duration is the manual value, else the server reading, else the model default', () => {
  assert.equal(effectiveVideoDuration('n_clip_1', 11.4, caps), 11);
  assert.equal(effectiveVideoDuration('n_clip_1', 40, caps), 15);
  assert.equal(effectiveVideoDuration('n_clip_1', undefined, caps), 8);
  assert.equal(effectiveVideoDuration('n_video_9', undefined, caps), 5);
});

test('only generated video nodes that do not concat their inputs get a length', () => {
  const graph = {
    nodes: [
      { id: 'n_clip_1', type: 'video', config: { pipeline: 'clip' } },
      { id: 'v_free', type: 'video', config: { prompt: 'a fox' } },
      { id: 'v_concat', type: 'video', config: {} },
      { id: 'n_compose', type: 'video', config: { pipeline: 'compose' } },
      { id: 'v_up', type: 'video', config: { interaction_mode: 'upload', upload: { uri: 'file:///a.mp4' } } },
      { id: 'img', type: 'image', config: {} },
      { id: 'v_from_image', type: 'video', config: {} },
    ],
    edges: [
      { id: 'e1', source: 'n_clip_1', target: 'v_concat' },
      { id: 'e2', source: 'img', target: 'v_from_image' },
    ],
  };
  const timed = graph.nodes.filter((node) => generatesTimedVideo(graph, node.id)).map((node) => node.id);
  assert.deepEqual(timed, ['n_clip_1', 'v_free', 'v_from_image']);
});

test('the toolbar names the configured model, else the provider that needs no model name', () => {
  assert.equal(generationModelLabel(caps.video), 'MiniMax-H3');
  assert.equal(generationModelLabel({ configured_model: '', backend: 'vllm-omni' }), 'vLLM-Omni');
  assert.equal(generationModelLabel({ configured_model: '', backend: null }), '');
});
