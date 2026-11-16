import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildJoyAIToolContextBatch,
  rememberJoyAIToolContext,
  removeSentJoyAIToolContext,
} from '../../../../channels/web/frontend/node_modules/.cache/joyai-tool-context/joyaiToolContext.js';

function entry(jobId, result = `${jobId} result`) {
  return {
    jobId,
    question: `${jobId} question`,
    query: `${jobId} query`,
    result,
    completedAt: '2026-08-26T08:00:00.000Z',
  };
}

test('tool context is deduplicated by search job id', () => {
  let pending = rememberJoyAIToolContext([], entry('job-1', 'old result'));
  pending = rememberJoyAIToolContext(pending, entry('job-1', 'new result'));

  assert.equal(pending.length, 1);
  assert.equal(pending[0].result, 'new result');
});

test('the next user request receives recent confirmed tool results', () => {
  let pending = [];
  pending = rememberJoyAIToolContext(pending, entry('job-1'));
  pending = rememberJoyAIToolContext(pending, entry('job-2'));

  const batch = buildJoyAIToolContextBatch(pending);
  assert.deepEqual(batch.jobIds, ['job-1', 'job-2']);
  assert.match(batch.text, /原问题：job-1 question/);
  assert.match(batch.text, /最终结果：job-2 result/);
  assert.match(batch.text, /完成时间：2026-08-26T08:00:00.000Z/);
});

test('only successfully attached results are removed', () => {
  const pending = [entry('job-1'), entry('job-2'), entry('job-3')];
  const remaining = removeSentJoyAIToolContext(pending, ['job-1', 'job-3']);

  assert.deepEqual(remaining.map((item) => item.jobId), ['job-2']);
});

test('one request attaches at most four recent tool results', () => {
  let pending = [];
  for (let index = 0; index < 6; index += 1) {
    pending = rememberJoyAIToolContext(pending, entry(`job-${index + 1}`));
  }
  const batch = buildJoyAIToolContextBatch(pending);

  assert.equal(pending.length, 4);
  assert.deepEqual(batch.jobIds, ['job-3', 'job-4', 'job-5', 'job-6']);
  assert.ok(batch.text.length <= 3_600);
});
