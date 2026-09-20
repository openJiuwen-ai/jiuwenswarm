import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

import { RealtimeDuplexSession } from '../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/realtimeDuplex.mjs';
import { SpeechGate } from '../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/speechGate.mjs';

test('manual task cancellation closes its function call silently and rejects a late result', () => {
  const { session, sent } = createSession();
  session.cancelToolTask('stopped-job', 'stopped-call');
  session.cancelToolTask('stopped-job', 'stopped-call');
  assert.equal(sent.length, 1);
  assert.equal(sent[0].item.call_id, 'stopped-call');
  assert.equal(JSON.parse(sent[0].item.output).status, 'cancelled');
  assert.equal(sent.some(event => event.type === 'response.create'), false);
  assert.equal(session.enqueueToolResult({ jobId: 'stopped-job', callId: 'stopped-call', question: '已取消任务', brief: { summary: '迟到的成功结果' } }), false);
});

test('confirmed speech interrupts each response once, including responses that start mid-utterance', () => {
  const { session, sent } = createSession();
  const gate = new SpeechGate();
  const feed = (probability, count, level = 1000) => {
    for (let i = 0; i < count; i++) session.handleSpeechDetection(gate.process(probability, level));
  };
  session.handleEvent({ type: 'response.created', response: { id: 'first' } });
  feed(0.1, 40, 15000);
  feed(0.99, 3);
  feed(0.1, 12);
  assert.equal(sent.length, 0);
  feed(0.95, 8);
  assert.equal(sent.filter((event) => event.type === 'response.cancel').length, 1);
  session.handleEvent({ type: 'response.created', response: { id: 'second' } });
  assert.equal(session.userActivityActive, true);
  feed(0.95, 25);
  assert.equal(sent.filter((event) => event.type === 'response.cancel').length, 2);
  feed(0.1, 40);
  session.handleEvent({ type: 'response.created', response: { id: 'third' } });
  feed(0.95, 8);
  assert.equal(sent.filter((event) => event.type === 'response.cancel').length, 3);
});

test('audio arriving during ongoing speech is blocked, but speech hangover permits the next answer', async () => {
  const { session, posted } = createSession();
  const gate = new SpeechGate();
  for (let i = 0; i < 8; i++) session.handleSpeechDetection(gate.process(0.95, 1000));
  session.handleEvent({ type: 'response.audio.delta', response_id: 'late-audio', delta: btoa('aa') });
  await session.playbackOperation;
  assert.ok(posted.every((message) => message.type !== 'audio'));
  for (let i = 0; i < 10; i++) session.handleSpeechDetection(gate.process(0.1, 20));
  assert.equal(session.userActivityActive, true); // the VAD's 640ms hangover has not expired
  session.handleEvent({ type: 'response.created', response: { id: 'after-speech' } });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'after-speech', delta: btoa('aa') });
  await session.playbackOperation;
  assert.equal(posted.filter((message) => message.type === 'audio').length, 1);
});

test('a stale speaking flag after a worker stall does not cancel a new answer', () => {
  const { session, sent } = createSession();
  session.userActivityActive = true;
  session.activeUserTurnId = 'old-voice';
  session.lastSpeechDetectionAt = performance.now() - 600;
  session.handleEvent({ type: 'response.created', response: { id: 'fresh-answer' } });
  assert.deepEqual(sent, []);
});

test('cancelled response late audio is discarded while its text remains visible', async () => {
  const { session, posted, assistantTexts } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'interrupted' } });
  session.interruptQwenResponse('voice-test', 256, 1000, 80);
  session.handleEvent({ type: 'response.audio.delta', response_id: 'interrupted', delta: btoa('aa') });
  session.handleEvent({ type: 'response.text.done', response_id: 'interrupted', text: '已经返回的文字' });
  await session.playbackOperation;
  assert.ok(posted.every((message) => message.type !== 'audio'));
  assert.equal(assistantTexts.at(-1).text, '已经返回的文字');
});

function realtimeBrief(summary, resultKind = 'generic') {
  return {
    status: 'completed',
    result_kind: resultKind,
    summary,
    displayed_in_ui: true,
    response_mode: 'brief',
    source: 'core_agent',
  };
}

function createSession(videoFrame = null, callbackOverrides = {}) {
  const states = [];
  const posted = [];
  const sent = [];
  const dispatchedToolResults = [];
  const assistantTexts = [];
  const userTexts = [];
  const diagnostics = [];
  const functionCalls = [];
  const session = new RealtimeDuplexSession(
    { url: 'ws://example.test/realtime' },
    {
      getVideoFrame: () => videoFrame,
      onAssistantText: (text, final, toolJobId, responseId) =>
        assistantTexts.push({ text, final, toolJobId, responseId }),
      onUserText: (text, final) => userTexts.push({ text, final }),
      onState: (state) => states.push(state),
      onError: () => undefined,
      onToolResultDispatched: (jobId) => dispatchedToolResults.push(jobId),
      onFunctionCall: (call) => functionCalls.push(call),
      onDiagnostic: (event) => diagnostics.push(event),
      ...callbackOverrides,
    },
  );
  session.playbackNode = { port: { postMessage: (message) => posted.push(message) } };
  session.socket = { readyState: 1, send: (message) => sent.push(JSON.parse(message)) };
  session.sessionReady = true;
  globalThis.WebSocket = { OPEN: 1 };
  return {
    session,
    states,
    posted,
    sent,
    dispatchedToolResults,
    assistantTexts,
    userTexts,
    diagnostics,
    functionCalls,
  };
}

test('playback waits for the target 400ms startup buffer and drains short tails', () => {
  const workletSource = readFileSync(
    new URL('../../frontend/VideoLivePanel/duplex-playback.js', import.meta.url),
    'utf8',
  );
  const workletEvents = [];
  let PlaybackProcessor;
  class FakeAudioWorkletProcessor {
    constructor() {
      this.port = { onmessage: null, postMessage: (message) => workletEvents.push(message) };
    }
  }
  runInNewContext(workletSource, {
    AudioWorkletProcessor: FakeAudioWorkletProcessor,
    Int16Array,
    Math,
    sampleRate: 1_000,
    registerProcessor: (_name, processor) => {
      PlaybackProcessor = processor;
    },
  });
  const processor = new PlaybackProcessor();
  const send = (data) => processor.port.onmessage({ data });
  const render = () => {
    const output = new Float32Array(2);
    processor.process([], [[output]]);
    return Array.from(output).map((sample) => Math.round(sample * 32768));
  };

  send({ type: 'audio', pcm: new Int16Array(400).fill(1000).buffer, responseId: 'response-1' });
  assert.deepEqual(render(), [0, 0]);
  for (let index = 0; index < 199; index += 1) render();
  assert.notDeepEqual(render(), [0, 0]);

  send({ type: 'drain', responseId: 'response-1' });
  for (let index = 0; index < 200; index += 1) render();
  assert.equal(workletEvents.at(-1).type, 'drained');
  assert.equal(workletEvents.at(-1).responseId, 'response-1');
});

test('native transcription events drive user text callbacks', () => {
  const { session, userTexts, diagnostics } = createSession();

  session.handleEvent({
    type: 'conversation.item.input_audio_transcription.delta',
    text: '香',
    stash: '港',
  });
  session.handleEvent({
    type: 'conversation.item.input_audio_transcription.completed',
    transcript: '香港天气',
  });

  assert.deepEqual(userTexts, [
    { text: '香港', final: false },
    { text: '香港天气', final: true },
  ]);
  assert.equal(diagnostics.at(-1).event, 'qwen_native_asr_completed');
});

test('Qwen error diagnostics preserve the original websocket event', () => {
  const errors = [];
  const { session, diagnostics } = createSession(null, {
    onError: (message) => errors.push(message),
  });
  const rawEvent =
    '{"type":"error","error":{"code":"context_length_exceeded","type":"invalid_request_error","message":"input is too long"}}';

  session.handleEvent(JSON.parse(rawEvent), rawEvent);

  assert.equal(errors.at(-1), 'input is too long');
  assert.deepEqual(
    {
      event: diagnostics.at(-1).event,
      raw_event: diagnostics.at(-1).raw_event,
      code: diagnostics.at(-1).code,
      error_type: diagnostics.at(-1).error_type,
      message: diagnostics.at(-1).message,
    },
    {
      event: 'qwen_realtime_error',
      raw_event: rawEvent,
      code: 'context_length_exceeded',
      error_type: 'invalid_request_error',
      message: 'input is too long',
    },
  );
});

test('the first image is deferred until a previous audio append exists', () => {
  const { session, sent } = createSession('dGVzdC1qcGVn');

  session.sendAudio(new Int16Array([1, -1]), true);
  session.sendAudio(new Int16Array([2, -2]), true);

  assert.deepEqual(
    sent.map((event) => event.type),
    ['input_audio_buffer.append', 'input_audio_buffer.append', 'input_image_buffer.append'],
  );
  assert.equal(sent[2].image, 'dGVzdC1qcGVn');
});

test('user speech cancels an active response and preserves completed text', () => {
  const { session, posted, sent, states, assistantTexts, diagnostics } = createSession();
  session.responseId = 'qwen-speaking';
  session.responseActive = true;
  session.assistantPlaying = true;
  session.assistantTranscript = '这是一段被用户打断的回答';

  assert.equal(session.interruptQwenResponse('voice-1', 240, 900, 350), true);
  assert.equal(session.interruptQwenResponse('voice-1', 260, 900, 350), false);

  assert.deepEqual(sent, [{ type: 'response.cancel' }]);
  assert.deepEqual(posted, [{ type: 'clear', cancelResponse: false }]);
  assert.equal(assistantTexts.at(-1).text, '这是一段被用户打断的回答');
  assert.equal(assistantTexts.at(-1).final, true);
  assert.equal(states.at(-1), 'listening');
  assert.equal(diagnostics.at(-1).event, 'qwen_response_interrupted_by_user');
});

test('server cancellation finalizes visible assistant text', () => {
  const { session, assistantTexts } = createSession();
  session.responseId = 'cancelled-answer';
  session.responseActive = true;
  session.assistantTranscript = '已经生成的部分回答';

  session.handleEvent({
    type: 'response.cancelled',
    response_id: 'cancelled-answer',
  });

  assert.deepEqual(assistantTexts.at(-1), {
    text: '已经生成的部分回答',
    final: true,
    toolJobId: undefined,
    responseId: 'cancelled-answer',
  });
});

test('text input uses a native conversation item and response request', async () => {
  const { session, sent } = createSession();

  assert.equal(await session.sendTextTurn('查询香港天气'), true);
  assert.deepEqual(sent, [
    {
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text: '查询香港天气' }],
      },
    },
    { type: 'response.create' },
  ]);
});

test('function calls are emitted once with parsed arguments', () => {
  const { session, functionCalls, diagnostics } = createSession();
  const event = {
    type: 'response.function_call_arguments.done',
    name: 'jiuwen_delegate',
    call_id: 'call-file',
    arguments: '{"task":"打开桌面的复习提纲并转换为 PDF"}',
  };

  session.handleEvent(event);
  session.handleEvent(event);

  assert.deepEqual(functionCalls, [
    {
      name: 'jiuwen_delegate',
      callId: 'call-file',
      arguments: '{"task":"打开桌面的复习提纲并转换为 PDF"}',
      task: '打开桌面的复习提纲并转换为 PDF',
    },
  ]);
  assert.equal(diagnostics.at(-1).event, 'qwen_tool_call_received');
});

test('Qwen acknowledgement survives a new tool-call response without transcript.done', () => {
  const { session, assistantTexts } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'ack' } });
  session.handleEvent({ type: 'response.audio_transcript.delta', response_id: 'ack', delta: '我来处理。' });
  session.handleEvent({ type: 'response.created', response: { id: 'delegate' } });
  assert.deepEqual(assistantTexts.at(-1), {
    text: '我来处理。',
    final: true,
    toolJobId: undefined,
    responseId: 'ack',
  });
});

test('Qwen tool receipt remains visible and response.done finalizes it', () => {
  const { session, assistantTexts } = createSession();
  session.enqueueToolResult({
    jobId: 'code-job',
    question: '请生成代码',
    callId: 'code-call',
    brief: realtimeBrief('代码已显示在界面中。', 'code'),
  });
  session.handleEvent({ type: 'response.created', response: { id: 'receipt' } });
  session.handleEvent({
    type: 'response.audio_transcript.delta',
    response_id: 'receipt',
    delta: '代码已显示在界面中。',
  });
  session.handleEvent({ type: 'response.done', response: { id: 'receipt' } });
  assert.deepEqual(assistantTexts.at(-1), {
    text: '代码已显示在界面中。',
    final: true,
    toolJobId: 'code-job',
    responseId: 'receipt',
  });
});

test('Qwen provider error preserves the received partial answer', () => {
  const { session, assistantTexts } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'partial' } });
  session.handleEvent({ type: 'response.text.delta', response_id: 'partial', delta: '已经生成的部分' });
  session.handleEvent({ type: 'error', error: { code: 'COMMON_ERROR', message: 'model repeat output happened' } });
  assert.deepEqual(assistantTexts.at(-1), {
    text: '已经生成的部分',
    final: true,
    toolJobId: undefined,
    responseId: 'partial',
  });
});

test('delegate calls accept query aliases and object arguments from Qwen', () => {
  const { session, functionCalls } = createSession();

  session.handleEvent({
    type: 'response.function_call_arguments.done',
    name: 'jiuwen_delegate',
    call_id: 'call-query-alias',
    arguments: { query: '查询香港今天的天气' },
  });

  assert.equal(functionCalls.length, 1);
  assert.equal(functionCalls[0].task, '查询香港今天的天气');
  assert.equal(functionCalls[0].arguments, '{"query":"查询香港今天的天气"}');
});

test('tool results wait for active generation but dispatch during queued audio playback', () => {
  const { session, sent, dispatchedToolResults, assistantTexts } = createSession();
  session.responseId = 'acknowledgement';
  session.responseActive = true;
  session.assistantPlaying = true;
  assert.equal(
    session.enqueueToolResult({
      jobId: 'search-weather',
      question: '香港今天的天气',
      brief: realtimeBrief('香港今天有雨，出门记得带伞。', 'research'),
      result: '```python\nprint("这段完整代码绝不能发送给Qwen")\n```',
      callId: 'call-weather',
    }),
    true,
  );

  assert.equal(session.pendingToolResults.length, 1);
  assert.deepEqual(sent, []);

  session.handleEvent({ type: 'response.done', response_id: 'acknowledgement' });

  assert.equal(session.pendingToolResults.length, 0);
  assert.equal(session.assistantPlaying, true);
  assert.deepEqual(dispatchedToolResults, ['search-weather']);
  assert.deepEqual(
    sent.map((event) => event.type),
    ['conversation.item.create', 'conversation.item.create', 'response.create'],
  );
  assert.equal(sent[0].item.type, 'function_call_output');
  assert.match(sent[0].item.output, /香港今天有雨/);
  assert.doesNotMatch(sent[0].item.output, /完整代码绝不能发送给Qwen/);
  assert.match(sent[1].item.content[0].text, /authoritative full answer is already visible/i);

  session.handleEvent({ type: 'response.created', response: { id: 'answer-weather' } });
  session.handleEvent({
    type: 'response.text.delta',
    response_id: 'answer-weather',
    delta: '香港今天有雨',
  });
  assert.deepEqual(assistantTexts.at(-1), {
    text: '香港今天有雨',
    final: false,
    toolJobId: 'search-weather',
    responseId: 'answer-weather',
  });
  assert.equal(session.assistantPlaying, true);
  session.handleEvent({
    type: 'response.text.done',
    response_id: 'answer-weather',
    text: '香港今天有雨，出门请带伞。',
  });
  assert.equal(assistantTexts.at(-1).toolJobId, 'search-weather');
});

test('an earlier task still gets a spoken follow-up after a newer question', () => {
  const { session, sent, dispatchedToolResults, diagnostics } = createSession();
  session.handleEvent({ type: 'response.created', response: { id: 'new-question' } });

  assert.equal(
    session.enqueueToolResult({
      jobId: 'old-file-task',
      turnId: 'turn-old',
      question: '打开旧文件',
      brief: realtimeBrief('旧文件任务已完成。', 'file'),
      callId: 'call-old-file',
    }),
    true,
  );

  assert.deepEqual(sent, []);
  assert.equal(diagnostics.at(-1).event, 'qwen_tool_result_waiting');
  session.handleEvent({ type: 'response.done', response_id: 'new-question' });
  assert.deepEqual(dispatchedToolResults, ['old-file-task']);
  assert.deepEqual(
    sent.map((event) => event.type),
    ['conversation.item.create', 'conversation.item.create', 'response.create'],
  );
  assert.equal(sent[0].item.type, 'function_call_output');
  const output = JSON.parse(sent[0].item.output);
  assert.equal(output.summary, '旧文件任务已完成。');
  assert.deepEqual(output.task_context, {
    job_id: 'old-file-task',
    turn_id: 'turn-old',
    original_question: '打开旧文件',
  });
  assert.match(sent[1].item.content[0].text, /even if the user has asked other questions/);
  assert.doesNotMatch(sent[0].item.output, /Do not answer it again/);
  assert.equal(session.responseActive, true);
  assert.equal(diagnostics.at(-1).event, 'qwen_tool_result_returned');
});

test('each task waits for user speech to finish and is sent exactly once in completion order', () => {
  const { session, sent, diagnostics } = createSession();
  session.userActivityActive = true;
  const first = {
    jobId: 'first-async-task',
    turnId: 'old-turn',
    question: '打开复习提纲',
    brief: realtimeBrief('复习提纲已打开。'),
    callId: 'call-first-async',
  };
  const second = {
    jobId: 'second-async-task',
    turnId: 'another-turn',
    question: '解释差分约束',
    brief: realtimeBrief('差分约束的解题思路已整理。'),
    callId: 'call-second-async',
  };
  assert.equal(session.enqueueToolResult(first), true);
  assert.equal(session.enqueueToolResult(second), true);
  assert.equal(session.enqueueToolResult(first), false);
  assert.deepEqual(sent, []);
  assert.equal(diagnostics.at(-1).event, 'search_result_queued');
  session.dispatchQueuedToolResult();
  session.dispatchQueuedToolResult();
  assert.equal(diagnostics.filter((event) => event.event === 'qwen_tool_result_waiting').length, 1);
  session.userActivityActive = false;
  session.dispatchQueuedToolResult();
  assert.equal(sent[0].item.call_id, 'call-first-async');
  session.handleEvent({ type: 'response.created', response: { id: 'first-receipt' } });
  session.handleEvent({ type: 'response.done', response_id: 'first-receipt' });
  assert.equal(sent[3].item.call_id, 'call-second-async');
  assert.equal(JSON.parse(sent[3].item.output).task_context.original_question, '解释差分约束');
  assert.equal(session.enqueueToolResult(first), false);
});

test('task deduplication lasts for the entire session rather than just the last 32 results', () => {
  const { session } = createSession();
  session.responseActive = true;
  const task = (index) => ({
    jobId: `job-${index}`,
    question: `任务${index}`,
    brief: realtimeBrief('完成'),
    callId: `call-${index}`,
  });
  for (let index = 0; index < 40; index++) assert.equal(session.enqueueToolResult(task(index)), true);
  assert.equal(session.enqueueToolResult(task(0)), false);
  assert.equal(session.pendingToolResults.length, 40);
});

test('a disconnected socket does not consume a queued tool result', () => {
  const { session, sent, diagnostics } = createSession();
  session.socket.readyState = 3;
  session.enqueueToolResult({
    jobId: 'waiting-for-connection',
    question: '查看文档',
    brief: realtimeBrief('已查看'),
    callId: 'call-wait',
  });
  assert.equal(session.pendingToolResults.length, 1);
  assert.equal(diagnostics.at(-1).reason, 'connection_not_ready');
  assert.deepEqual(sent, []);
  session.socket.readyState = 1;
  session.dispatchQueuedToolResult();
  assert.equal(session.pendingToolResults.length, 0);
  assert.equal(sent.at(-1).type, 'response.create');
});

test('tool responses retain their own job id after another result is queued', () => {
  const { session, assistantTexts } = createSession();

  session.enqueueToolResult({
    jobId: 'job-first',
    question: '第一个问题',
    brief: realtimeBrief('第一个结果已完成。'),
    callId: 'call-first',
  });
  session.enqueueToolResult({
    jobId: 'job-second',
    question: '第二个问题',
    brief: realtimeBrief('第二个结果已完成。'),
    callId: 'call-second',
  });
  session.handleEvent({ type: 'response.created', response: { id: 'response-first' } });
  session.handleEvent({
    type: 'response.text.delta',
    response_id: 'response-first',
    delta: '第一个回答',
  });

  assert.equal(assistantTexts.at(-1).toolJobId, 'job-first');

  session.handleEvent({ type: 'response.done', response_id: 'response-first' });
  session.handleEvent({ type: 'response.created', response: { id: 'response-second' } });
  session.handleEvent({
    type: 'response.text.delta',
    response_id: 'response-second',
    delta: '第二个回答',
  });

  assert.equal(assistantTexts.at(-1).toolJobId, 'job-second');
});

test('session update includes Gateway-provided tools', async () => {
  const tools = [{ type: 'function', function: { name: 'jiuwen_delegate' } }];
  let socket;
  class StartupSocket {
    static OPEN = 1;
    constructor() {
      socket = this;
      this.readyState = StartupSocket.OPEN;
      this.sent = [];
    }
    close() {}
    send(message) {
      this.sent.push(JSON.parse(message));
    }
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = StartupSocket;
  const session = new RealtimeDuplexSession(
    { url: 'ws://example.test/realtime', tools },
    {
      getVideoFrame: () => null,
      onAssistantText: () => undefined,
      onUserText: () => undefined,
      onState: () => undefined,
      onError: () => undefined,
    },
  );

  const opening = session.openSocket();
  socket.onopen();
  await opening;

  assert.deepEqual(socket.sent[0].session.tools, tools);
  assert.match(socket.sent[0].session.instructions, /MUST call jiuwen_delegate in the same turn/);
  assert.match(socket.sent[0].session.instructions, /brief, natural acknowledgement that you are handling the request/);
  assert.match(socket.sent[0].session.instructions, /acknowledgement describes work in progress only/);
  assert.match(
    socket.sent[0].session.instructions,
    /Before the function result arrives, never say the task is complete/,
  );
  assert.match(socket.sent[0].session.instructions, /file access, document processing/);
});

test('decoded response chunks stay ordered before the playback drain', async () => {
  const { session, posted } = createSession();
  const decoded = [];
  session.decodeOutputAudio = async (_event, encoded) => {
    if (encoded === 'first') await new Promise((resolve) => setTimeout(resolve, 5));
    decoded.push(encoded);
    return new Int16Array(encoded === 'first' ? [1] : [2]);
  };

  session.handleEvent({ type: 'response.created', response: { id: 'response-ordered' } });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'response-ordered', delta: 'first' });
  session.handleEvent({ type: 'response.audio.delta', response_id: 'response-ordered', delta: 'second' });
  session.handleEvent({ type: 'response.done', response_id: 'response-ordered' });
  await new Promise((resolve) => setTimeout(resolve, 15));

  assert.deepEqual(decoded, ['first', 'second']);
  assert.deepEqual(
    posted.map((message) => message.type),
    ['audio', 'audio', 'drain'],
  );
});

test('session.closed before session.created rejects startup with the backend reason', async () => {
  const diagnostics = [];
  let socket;
  class StartupSocket {
    static OPEN = 1;
    constructor() {
      socket = this;
      this.readyState = StartupSocket.OPEN;
    }
    close() {}
    send() {}
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = StartupSocket;
  const session = new RealtimeDuplexSession(
    { url: 'ws://example.test/realtime' },
    {
      getVideoFrame: () => null,
      onAssistantText: () => undefined,
      onUserText: () => undefined,
      onState: () => undefined,
      onError: () => undefined,
      onDiagnostic: (event) => diagnostics.push(event),
    },
  );

  const opening = session.openSocket();
  socket.onmessage({ data: JSON.stringify({ type: 'session.closed', reason: 'backend_error' }) });

  await assert.rejects(opening, /Realtime 会话初始化失败：backend_error/);
  assert.equal(diagnostics.at(-1).event, 'realtime_websocket_error');
});

test('remote disconnect releases media resources and pending receipts without losing received text', async () => {
  let socket;
  class ClosingSocket {
    static OPEN = 1;
    readyState = 1;
    constructor() {
      socket = this;
    }
    close() {
      this.readyState = 3;
    }
    send() {}
  }
  globalThis.window = globalThis;
  globalThis.WebSocket = ClosingSocket;
  const texts = [];
  const states = [];
  const released = [];
  const session = new RealtimeDuplexSession(
    { url: 'ws://example.test/realtime' },
    {
      getVideoFrame: () => null,
      onAssistantText: (text) => texts.push(text),
      onUserText() {},
      onError() {},
      onState: (state) => states.push(state),
    },
  );
  const opening = session.openSocket();
  socket.onopen();
  await opening;
  session.sessionReady = true;
  session.assistantTranscript = '已经收到的回答';
  session.microphone = { getTracks: () => [{ stop: () => released.push('microphone') }] };
  session.captureContext = { close: () => released.push('capture') };
  session.playbackContext = { close: () => released.push('playback') };
  session.pendingToolResults = [{ jobId: 'old-job' }];
  session.sendTimer = setInterval(() => {}, 60_000);
  socket.readyState = 3;
  socket.onclose({ code: 1006, reason: 'network interrupted' });
  assert.deepEqual(released, ['microphone', 'capture', 'playback']);
  assert.deepEqual(session.pendingToolResults, []);
  assert.equal(session.sendTimer, null);
  assert.equal(session.socket, null);
  assert.equal(texts.at(-1), '已经收到的回答');
  assert.equal(states.at(-1), 'closed');
});
