// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isThinkingOnlyPreview,
  trajectoryDisplayText,
} from '../node_modules/.cache/trajectory-preview/preview.mjs';

test('assistant source and its compact Markdown preview are displayed once', () => {
  const output = '你好！我是你的个人智能体，有什么可以帮你的吗？\n\n随时告诉我。';
  assert.equal(trajectoryDisplayText(output, output), output);
});

test('reasoning-only assistant previews are distinct, including tool-call steps', () => {
  assert.equal(isThinkingOnlyPreview({ kind: 'message', thinkingDetail: 'Let me inspect it.' }), true);
  assert.equal(isThinkingOnlyPreview({
    kind: 'message', thinkingDetail: 'Inspecting', sourceBlocks: [{ type: 'tool-call', content: '{}' }],
  }), true);
});

test('visible assistant output takes precedence over reasoning', () => {
  assert.equal(isThinkingOnlyPreview({
    kind: 'message', thinkingDetail: 'Internal reasoning', outputDetail: 'Done', previewMarkdown: 'Done',
  }), false);
  assert.equal(isThinkingOnlyPreview({
    kind: 'message', thinkingDetail: 'Internal reasoning', previewMarkdown: 'Streaming answer',
  }), false);
});

test('empty reasoning, tool-only rows and other roles are not thinking previews', () => {
  assert.equal(isThinkingOnlyPreview({ kind: 'message' }), false);
  assert.equal(isThinkingOnlyPreview({ kind: 'message', thinkingDetail: '  \n' }), false);
  assert.equal(isThinkingOnlyPreview({ kind: 'tool', thinkingDetail: 'Tool output' }), false);
});

test('equivalent whitespace-normalized assistant previews are displayed once', () => {
  assert.equal(
    trajectoryDisplayText('第一段\n\n第二段', '第一段  第二段'),
    '第一段\n\n第二段',
  );
});

test('a distinct record label still precedes its Markdown preview', () => {
  assert.equal(
    trajectoryDisplayText('Assistant response', '**完成**'),
    'Assistant response · 完成',
  );
});
