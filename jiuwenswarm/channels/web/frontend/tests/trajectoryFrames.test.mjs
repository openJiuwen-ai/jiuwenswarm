// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyStreamFrames,
  emptyStreamFrameState,
  forgetStreamFrames,
  streamingTextFor,
  withStreamFrames,
} from '../node_modules/.cache/trajectory-frames/trajectoryFrames.mjs';

const TRACE = 'a'.repeat(32);
const SPAN = 'b'.repeat(16);
const OTHER_SPAN = 'c'.repeat(16);

function frame(sequence, overrides = {}) {
  return {
    frame_seq: sequence + 1,
    trace_id: TRACE,
    span_id: SPAN,
    subject_id: 'main',
    sequence,
    kind: 'text-delta',
    timestamp_unix_nano: 1700000000000000000 + sequence,
    text: `w${sequence} `,
    ...overrides,
  };
}

function page(frames, overrides = {}) {
  const last = frames.length > 0 ? frames[frames.length - 1].frame_seq : 0;
  return {
    frames,
    frame_seq: last,
    next_since_frame_seq: last,
    reset: false,
    ...overrides,
  };
}

test('frames concatenate into the answer with every space preserved', () => {
  const frames = Array.from({ length: 40 }, (_value, index) => frame(index));
  const state = applyStreamFrames(emptyStreamFrameState, page(frames));
  const span = streamingTextFor(state, TRACE, SPAN);

  const expected = frames.map((item) => item.text).join('');
  assert.equal(span.text, expected);
  assert.equal(span.frameCount, 40);
  assert.equal(state.frameSeq, 40);
});

test('a reader resumes across pages without losing or repeating text', () => {
  const all = Array.from({ length: 90 }, (_value, index) => frame(index));
  let state = applyStreamFrames(emptyStreamFrameState, page(all.slice(0, 30)));
  state = applyStreamFrames(state, page(all.slice(30, 60)));
  state = applyStreamFrames(state, page(all.slice(60)));

  assert.equal(
    streamingTextFor(state, TRACE, SPAN).text,
    all.map((item) => item.text).join(''),
  );
  assert.equal(state.frameSeq, 90);
});

test('a redelivered frame does not double a word in the answer', () => {
  const frames = [frame(0), frame(1), frame(2)];
  let state = applyStreamFrames(emptyStreamFrameState, page(frames));
  // The same page arriving twice must leave the answer unchanged.
  state = applyStreamFrames(state, page(frames));

  assert.equal(streamingTextFor(state, TRACE, SPAN).text, 'w0 w1 w2 ');
  assert.equal(streamingTextFor(state, TRACE, SPAN).frameCount, 3);
});

test('reset drops what the reader held instead of extending it', () => {
  let state = applyStreamFrames(emptyStreamFrameState, page([frame(0), frame(1)]));
  state = applyStreamFrames(state, page([frame(0, { text: 'fresh ' })], { reset: true }));

  assert.equal(streamingTextFor(state, TRACE, SPAN).text, 'fresh ');
});

test('spans, reasoning and tool arguments accumulate separately', () => {
  const state = applyStreamFrames(emptyStreamFrameState, page([
    frame(0, { kind: 'reasoning-delta', text: 'think ' }),
    frame(1, { kind: 'text-delta', text: 'hello ' }),
    frame(2, {
      kind: 'tool-call-delta',
      text: undefined,
      tool_call_id: 'call-1',
      arguments_delta: '{"a"',
    }),
    frame(3, {
      kind: 'tool-call-delta',
      text: undefined,
      tool_call_id: 'call-1',
      arguments_delta: ':1}',
    }),
    frame(4, { span_id: OTHER_SPAN, text: 'other ' }),
  ]));

  const span = streamingTextFor(state, TRACE, SPAN);
  assert.equal(span.reasoning, 'think ');
  assert.equal(span.text, 'hello ');
  assert.equal(span.toolArguments.get('call-1'), '{"a":1}');
  assert.equal(streamingTextFor(state, TRACE, OTHER_SPAN).text, 'other ');
});

test('a finished span forgets its frames so the record can take over', () => {
  const state = applyStreamFrames(emptyStreamFrameState, page([
    frame(0),
    frame(1, { span_id: OTHER_SPAN }),
  ]));
  const pruned = forgetStreamFrames(state, [`${TRACE}:${SPAN}`]);

  assert.equal(streamingTextFor(pruned, TRACE, SPAN), undefined);
  assert.ok(streamingTextFor(pruned, TRACE, OTHER_SPAN) !== undefined);
  // The cursor survives: those frames were read, only their content is stale.
  assert.equal(pruned.frameSeq, state.frameSeq);
});

test('forgetting an unknown span leaves the state untouched', () => {
  const state = applyStreamFrames(emptyStreamFrameState, page([frame(0)]));
  assert.equal(forgetStreamFrames(state, ['nope:nope']), state);
});

function record(span) {
  return { resourceSpans: [{ scopeSpans: [{ spans: [span] }] }] };
}

function runningSpan(overrides = {}) {
  return {
    traceId: TRACE,
    spanId: SPAN,
    name: 'chat',
    startTimeUnixNano: '100',
    ...overrides,
  };
}

function eventsOf(records) {
  return records[0].resourceSpans[0].scopeSpans[0].spans[0].events ?? [];
}

function attributeOf(event, key) {
  const found = (event.attributes ?? []).find((item) => item.key === key);
  if (found === undefined) return undefined;
  return found.value.stringValue ?? found.value.intValue;
}

test('a running span carries its streamed answer as chunk events', () => {
  const state = applyStreamFrames(emptyStreamFrameState, page([
    frame(0, { kind: 'reasoning-delta', text: 'why ' }),
    frame(1, { text: 'hello ' }),
    frame(2, { text: 'world' }),
  ]));
  const injected = withStreamFrames([record(runningSpan())], state);
  const events = eventsOf(injected);

  const kinds = events.map((event) => attributeOf(event, 'openjiuwen.stream.kind'));
  assert.deepEqual(kinds, ['reasoning-delta', 'text-delta']);
  const text = events.find(
    (event) => attributeOf(event, 'openjiuwen.stream.kind') === 'text-delta',
  );
  assert.equal(attributeOf(text, 'openjiuwen.stream.text'), 'hello world');
});

test('an ended span keeps its own output instead of stale frames', () => {
  const state = applyStreamFrames(emptyStreamFrameState, page([frame(0)]));
  const ended = [record(runningSpan({ endTimeUnixNano: '200' }))];

  // Returned by reference: nothing about a finished span changed.
  assert.equal(withStreamFrames(ended, state), ended);
});

test('records without frames keep their identity for the projection cache', () => {
  const records = [record(runningSpan({ spanId: OTHER_SPAN }))];
  const state = applyStreamFrames(emptyStreamFrameState, page([frame(0)]));

  assert.equal(withStreamFrames(records, state), records);
  assert.equal(withStreamFrames(records, emptyStreamFrameState), records);
});

test('a tool call streams its arguments as one event per call id', () => {
  const state = applyStreamFrames(emptyStreamFrameState, page([
    frame(0, {
      kind: 'tool-call-delta',
      text: undefined,
      tool_call_id: 'call-7',
      arguments_delta: '{"path"',
    }),
    frame(1, {
      kind: 'tool-call-delta',
      text: undefined,
      tool_call_id: 'call-7',
      arguments_delta: ':"/tmp"}',
    }),
  ]));
  const events = eventsOf(withStreamFrames([record(runningSpan())], state));

  assert.equal(events.length, 1);
  assert.equal(
    attributeOf(events[0], 'openjiuwen.stream.tool_call.arguments_delta'),
    '{"path":"/tmp"}',
  );
  assert.equal(attributeOf(events[0], 'gen_ai.tool.call.id'), 'call-7');
});
