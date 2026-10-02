// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  compactionExplanation,
  compactionFacts,
  compactionsByToolCall,
} from '../node_modules/.cache/trajectory-compaction/compaction.mjs';

const offloadPayload = {
  type: 'context.compression_state',
  operation_id: 'be4d7840953548478c48bfb691c33480',
  status: 'completed',
  phase: 'add_messages',
  processor: 'MessageSummaryOffloader',
  model: '',
  before: { time: '2026-09-17T20:43:06.964+08:00', messages: 60, tokens: 103812, context_percent: 52 },
  after: { time: '2026-09-17T20:43:07.677+08:00', messages: 60, tokens: 75788, context_percent: 38 },
  saved: { messages: 0, tokens: 28024, percent: 27 },
  duration_ms: 713,
  modified_messages: [
    {
      message_id: 'bb5f8c19fabe470ca4f20b182835bd0f',
      role: 'tool',
      tool_call_id: 'call_1y5ik55vcuk9wb3zljjh65ys',
      offload_handle: 'c06572cf740749cdb62bb07268763054',
      offload_type: 'filesystem',
    },
  ],
  summary: 'Compressed 60 -> 60 messages, ~103.8k -> ~75.8k tokens, saved ~28k tokens (27.0%), modified 1 messages',
  model_requests: [],
};

test('model-free offload reads as a rule-based rewrite of one tool result', () => {
  const facts = compactionFacts(offloadPayload);

  assert.deepEqual(facts, {
    processor: 'MessageSummaryOffloader',
    trigger: 'New messages added to context',
    modelFree: true,
    before: { messages: 60, tokens: 103812 },
    after: { messages: 60, tokens: 75788 },
    savedTokens: 28024,
    savedPercent: 27,
    durationMs: 713,
    modifiedMessages: [
      {
        messageId: 'bb5f8c19fabe470ca4f20b182835bd0f',
        role: 'tool',
        toolCallId: 'call_1y5ik55vcuk9wb3zljjh65ys',
        offloadHandle: 'c06572cf740749cdb62bb07268763054',
        offloadType: 'filesystem',
      },
    ],
  });
  assert.equal(
    compactionExplanation(facts),
    '1 message was shortened in place with the original offloaded; the model can reload it by handle.',
  );
});

test('a summarizing compaction explains removed messages and in-place rewrites', () => {
  const facts = compactionFacts({
    operation_id: 'op-2',
    phase: 'active_compress',
    before: { messages: 40, tokens: 90000 },
    after: { messages: 12, tokens: 20000 },
    modified_messages: [{ message_id: 'u1', role: 'user' }],
    model_requests: [{ request_id: 'r1', inference_id: 'i1' }],
  });

  assert.equal(facts.modelFree, false);
  assert.equal(facts.trigger, 'Compaction requested explicitly');
  assert.equal(
    compactionExplanation(facts),
    '28 messages were folded away; 1 message was rewritten in place.',
  );
});

test('payloads from before modified_messages existed stay readable', () => {
  const facts = compactionFacts({
    operation_id: 'op-3',
    phase: 'unknown_phase',
    before: { messages: 'sixty' },
    modified_messages: [{ role: 'tool' }, 'not an object'],
  });

  assert.deepEqual(facts, {
    modelFree: false,
    before: { messages: null, tokens: null },
    modifiedMessages: [],
  });
  assert.equal(compactionExplanation(facts), undefined);
});

test('tool calls map to the last compaction that rewrote them', () => {
  const byCall = compactionsByToolCall([
    { index: 3 },
    { index: 7, compactionDetail: offloadPayload },
    {
      index: 12,
      compactionDetail: {
        modified_messages: [
          { message_id: 'm2', role: 'tool', tool_call_id: 'call_1y5ik55vcuk9wb3zljjh65ys' },
          { message_id: 'm3', role: 'tool', tool_call_id: 'call_other' },
        ],
      },
    },
  ]);

  assert.deepEqual([...byCall.entries()], [
    ['call_1y5ik55vcuk9wb3zljjh65ys', 12],
    ['call_other', 12],
  ]);
});
