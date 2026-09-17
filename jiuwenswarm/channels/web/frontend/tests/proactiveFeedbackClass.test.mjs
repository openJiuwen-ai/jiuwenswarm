import assert from 'node:assert/strict';
import test from 'node:test';

import { proactiveFeedbackButtonClass } from '../node_modules/.cache/proactive-feedback/components/ChatPanel/proactiveFeedbackClass.js';

test('未反馈时按钮使用可读的中等饱和文字', () => {
  assert.ok(proactiveFeedbackButtonClass(null, 'like').includes('text-green-400'));
  assert.ok(proactiveFeedbackButtonClass(null, 'dislike').includes('text-red-400'));
  assert.ok(!proactiveFeedbackButtonClass(null, 'like').includes('opacity-40'));
});

test('点击「有帮助」后，选中按钮文字使用深色档而非浅色 300 档', () => {
  const likeClass = proactiveFeedbackButtonClass('like', 'like');
  assert.ok(likeClass.includes('bg-green-500/30'));
  assert.ok(likeClass.includes('text-green-600'), '选中态应为深色文字 text-green-600');
  assert.ok(!likeClass.includes('text-green-300'), '不得回退到不可见的 text-green-300');
});

test('点击「不需要」后，选中按钮文字使用深色档而非浅色 300 档', () => {
  const dislikeClass = proactiveFeedbackButtonClass('dislike', 'dislike');
  assert.ok(dislikeClass.includes('bg-red-500/30'));
  assert.ok(dislikeClass.includes('text-red-600'), '选中态应为深色文字 text-red-600');
  assert.ok(!dislikeClass.includes('text-red-300'), '不得回退到不可见的 text-red-300');
});

test('点击一项后，另一项按钮进入弱化禁用态', () => {
  const dimmedLike = proactiveFeedbackButtonClass('dislike', 'like');
  assert.ok(dimmedLike.includes('opacity-40'));
  assert.ok(dimmedLike.includes('cursor-not-allowed'));

  const dimmedDislike = proactiveFeedbackButtonClass('like', 'dislike');
  assert.ok(dimmedDislike.includes('opacity-40'));
  assert.ok(dimmedDislike.includes('cursor-not-allowed'));
});
