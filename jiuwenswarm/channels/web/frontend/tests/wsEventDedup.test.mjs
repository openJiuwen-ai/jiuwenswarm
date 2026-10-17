import assert from 'node:assert/strict';
import test from 'node:test';

import { makeEventDedupKey } from '../node_modules/.cache/ws-event-dedup/utils/wsEventDedup.js';

const session = 'web_sess_1';
const requestId = 'req_msr7rhfm_290';

test('processing_status true and false with the same request_id are distinct', () => {
  const started = makeEventDedupKey('chat.processing_status', {
    session_id: session,
    event_type: 'chat.processing_status',
    request_id: requestId,
    is_processing: true,
  });
  const finished = makeEventDedupKey('chat.processing_status', {
    session_id: session,
    event_type: 'chat.processing_status',
    request_id: requestId,
    is_processing: false,
  });
  assert.notEqual(started, finished);
  assert.match(started, /::proc:1$/);
  assert.match(finished, /::proc:0$/);
});

test('duplicate processing_status true with the same request_id still matches', () => {
  const payload = {
    session_id: session,
    event_type: 'chat.processing_status',
    request_id: requestId,
    is_processing: true,
  };
  assert.equal(
    makeEventDedupKey('chat.processing_status', payload),
    makeEventDedupKey('chat.processing_status', payload)
  );
});

test('tool results from different tool calls in one request stay distinct', () => {
  const firstClose = makeEventDedupKey('chat.tool_result', {
    session_id: session,
    event_type: 'chat.tool_result',
    request_id: requestId,
    tool_name: 'subagent_close',
    tool_call_id: 'call_close_guangzhou',
    result: "success=True data={'subagent_id': 'agent-guangzhou'}",
  });
  const secondClose = makeEventDedupKey('chat.tool_result', {
    session_id: session,
    event_type: 'chat.tool_result',
    request_id: requestId,
    tool_name: 'subagent_close',
    tool_call_id: 'call_close_shenzhen',
    result: "success=True data={'subagent_id': 'agent-shenzhen'}",
  });

  assert.notEqual(firstClose, secondClose);
  assert.equal(
    firstClose,
    makeEventDedupKey('chat.tool_result', {
      session_id: session,
      event_type: 'chat.tool_result',
      request_id: requestId,
      tool_name: 'subagent_close',
      tool_call_id: 'call_close_guangzhou',
      result: "success=True data={'subagent_id': 'agent-guangzhou'}",
    })
  );
});

test('tool calls from different tool calls in one request stay distinct', () => {
  const firstSpawn = makeEventDedupKey('chat.tool_call', {
    session_id: session,
    event_type: 'chat.tool_call',
    request_id: requestId,
    tool_call: {
      name: 'subagent_spawn',
      tool_call_id: 'call_spawn_shenzhen',
    },
  });
  const secondSpawn = makeEventDedupKey('chat.tool_call', {
    session_id: session,
    event_type: 'chat.tool_call',
    request_id: requestId,
    tool_call: {
      name: 'subagent_spawn',
      tool_call_id: 'call_spawn_guangzhou',
    },
  });

  assert.notEqual(firstSpawn, secondSpawn);
  assert.equal(
    firstSpawn,
    makeEventDedupKey('chat.tool_call', {
      session_id: session,
      event_type: 'chat.tool_call',
      request_id: requestId,
      tool_call: {
        name: 'subagent_spawn',
        tool_call_id: 'call_spawn_shenzhen',
      },
    })
  );
});

test('other chat events with the same request_id are unchanged', () => {
  const first = makeEventDedupKey('chat.final', {
    session_id: session,
    request_id: requestId,
    content: 'done',
  });
  const second = makeEventDedupKey('chat.final', {
    session_id: session,
    request_id: requestId,
    content: 'also done',
  });
  assert.equal(first, second);
  assert.doesNotMatch(first, /::proc:/);
});
