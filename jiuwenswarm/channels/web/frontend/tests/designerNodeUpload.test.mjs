import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildUploadedDesignerNode,
  canReplaceUploadedNode,
  storeDesignerUpload,
  uploadAcceptForNodeType,
  uploadKindFromMime,
  uploadedNodeConfig,
} from '../node_modules/.cache/designer-node-upload/designerNodeUpload.js';

const stored = {
  uri: 'file:///tmp/uploads/cat.png',
  filename: 'cat.png',
  mime_type: 'image/png',
};

test('uploadKindFromMime accepts image, video and audio only', () => {
  assert.equal(uploadKindFromMime('image/png'), 'image');
  assert.equal(uploadKindFromMime('video/mp4'), 'video');
  assert.equal(uploadKindFromMime('audio/mpeg'), 'audio');
  assert.equal(uploadKindFromMime('application/json'), null);
  assert.equal(uploadKindFromMime(''), null);
});

test('uploadAcceptForNodeType keeps replacements within the node type', () => {
  assert.equal(uploadAcceptForNodeType('image'), 'image/*');
  assert.equal(uploadAcceptForNodeType('video'), 'video/*');
  assert.equal(uploadAcceptForNodeType('audio'), 'audio/*');
});

test('buildUploadedDesignerNode creates an upload-mode node backed by the stored file', () => {
  const node = buildUploadedDesignerNode({
    kind: 'image',
    stored,
    existing: [],
    position: { x: 10, y: 20 },
  });
  assert.equal(node.type, 'image');
  assert.equal(node.config.interaction_mode, 'upload');
  assert.equal(node.config.user_replaced_output, true);
  assert.deepEqual(node.config.upload, {
    filename: 'cat.png',
    asset_id: '',
    mime_type: 'image/png',
    uri: stored.uri,
  });
  assert.deepEqual(node.output_ref, {
    kind: 'image',
    uri: stored.uri,
    mime_type: 'image/png',
    label: 'cat.png',
  });
  assert.equal(canReplaceUploadedNode(node), true);
});

test('uploadedNodeConfig swaps the upload while keeping unrelated config', () => {
  const next = uploadedNodeConfig(
    {
      role: 'image',
      interaction_mode: 'upload',
      upload: { filename: 'old.png', asset_id: 'asset_1', uri: 'file:///old.png' },
    },
    stored,
  );
  assert.equal(next.role, 'image');
  assert.equal(next.upload.uri, stored.uri);
  assert.equal(next.upload.asset_id, '');
});

test('storeDesignerUpload rejects files over 32 MB before reading them', async () => {
  const oversized = {
    name: 'clip.mp4',
    type: 'video/mp4',
    size: 32 * 1024 * 1024 + 1,
    slice() {
      throw new Error('oversized files must not be read');
    },
  };
  await assert.rejects(storeDesignerUpload(oversized), { message: 'file_too_large' });
});

test('canReplaceUploadedNode rejects generated and chat-reference nodes', () => {
  assert.equal(
    canReplaceUploadedNode({ id: 'n1', type: 'image', config: { interaction_mode: 'generate' } }),
    false,
  );
  assert.equal(
    canReplaceUploadedNode({
      id: 'n2',
      type: 'image',
      config: { user_reference_id: 'ref_01', interaction_mode: 'upload' },
      output_ref: { uri: 'file:///ref.png' },
    }),
    false,
  );
  assert.equal(canReplaceUploadedNode(null), false);
});
