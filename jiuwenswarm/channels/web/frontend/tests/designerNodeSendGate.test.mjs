import assert from 'node:assert/strict';
import test from 'node:test';
import { nodeSendBlockedByMissingModel } from '../node_modules/.cache/designer-node-send/designerNodeGenerationKind.mjs';

const both = { video_gen: 'video-1', visual_gen: 'image-1' };
const imageMissing = { video_gen: 'video-1', visual_gen: null };
const videoMissing = { video_gen: null, visual_gen: 'image-1' };

test('an image node send is disabled only when the image catalogue is empty', () => {
  const image = { id: 'img', type: 'image', config: {} };
  const video = { id: 'vid', type: 'video', config: {} };
  assert.equal(nodeSendBlockedByMissingModel(image, null, imageMissing), true);
  assert.equal(nodeSendBlockedByMissingModel(video, null, imageMissing), false);
  assert.equal(nodeSendBlockedByMissingModel(image, null, both), false);
  assert.equal(nodeSendBlockedByMissingModel(video, null, videoMissing), true);
  assert.equal(nodeSendBlockedByMissingModel(image, null, videoMissing), false);
});

test('a catalogue that has not loaded does not disable the send button', () => {
  const image = { id: 'img', type: 'image', config: {} };
  assert.equal(nodeSendBlockedByMissingModel(image, null, null), false);
});

test('nodes that do not call a generation model stay enabled', () => {
  const uploaded = {
    id: 'up',
    type: 'image',
    config: { interaction_mode: 'upload', upload: { uri: 'file://a.png' } },
  };
  const comfy = { id: 'cf', type: 'image', config: { is_comfyui: true } };
  const text = { id: 'tx', type: 'text', config: {} };
  assert.equal(nodeSendBlockedByMissingModel(uploaded, null, imageMissing), false);
  assert.equal(nodeSendBlockedByMissingModel(comfy, null, imageMissing), false);
  assert.equal(nodeSendBlockedByMissingModel(text, null, imageMissing), false);
});
