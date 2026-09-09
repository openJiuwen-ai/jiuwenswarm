// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  trajectoryDisplayText,
} from '../node_modules/.cache/trajectory-preview/preview.mjs';

test('assistant source and its compact Markdown preview are displayed once', () => {
  const output = '你好！我是你的个人智能体，有什么可以帮你的吗？\n\n随时告诉我。';
  assert.equal(trajectoryDisplayText(output, output), output);
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
