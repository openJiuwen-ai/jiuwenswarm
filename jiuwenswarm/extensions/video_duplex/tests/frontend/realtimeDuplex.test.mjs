import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

import { RealtimeDuplexSession } from '../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/realtimeDuplex.mjs';

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
  const rawEvent = '{"type":"error","error":{"code":"context_length_exceeded","type":"invalid_request_error","message":"input is too long"}}';

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
      result: '香港今天有雨。',
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

test('a stale tool result is delivered directly without starting another Qwen response', () => {
  const staleToolResults = [];
  const { session, sent, dispatchedToolResults, diagnostics } = createSession(null, {
    isToolTurnCurrent: (turnId) => turnId === 'turn-current',
    onStaleToolResult: (toolResult) => staleToolResults.push(toolResult),
  });

  assert.equal(
    session.enqueueToolResult({
      jobId: 'old-file-task',
      turnId: 'turn-old',
      question: '打开旧文件',
      result: '旧文件的完整内容',
      callId: 'call-old-file',
    }),
    true,
  );

  assert.deepEqual(dispatchedToolResults, ['old-file-task']);
  assert.equal(staleToolResults.length, 1);
  assert.equal(staleToolResults[0].result, '旧文件的完整内容');
  assert.deepEqual(
    sent.map((event) => event.type),
    ['conversation.item.create'],
  );
  assert.equal(sent[0].item.type, 'function_call_output');
  assert.doesNotMatch(sent[0].item.output, /旧文件的完整内容/);
  assert.equal(session.responseActive, false);
  assert.equal(diagnostics.at(-1).event, 'stale_tool_result_delivered_directly');
});

test('tool responses retain their own job id after another result is queued', () => {
  const { session, assistantTexts } = createSession();

  session.enqueueToolResult({
    jobId: 'job-first',
    question: '第一个问题',
    result: '第一个结果',
    callId: 'call-first',
  });
  session.enqueueToolResult({
    jobId: 'job-second',
    question: '第二个问题',
    result: '第二个结果',
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
