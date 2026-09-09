import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';

import { RealtimeDuplexSession } from '../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/realtimeDuplex.mjs';

function createSession(videoFrame = null) {
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
      onAssistantText: (text, final, toolJobId) => (
        assistantTexts.push({ text, final, toolJobId })
      ),
      onUserText: (text, final) => userTexts.push({ text, final }),
      onState: (state) => states.push(state),
      onError: () => undefined,
      onToolResultDispatched: (jobId) => dispatchedToolResults.push(jobId),
      onFunctionCall: (call) => functionCalls.push(call),
      onDiagnostic: (event) => diagnostics.push(event),
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
    registerProcessor: (_name, processor) => { PlaybackProcessor = processor; },
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

test('the first image is deferred until a previous audio append exists', () => {
  const { session, sent } = createSession('dGVzdC1qcGVn');

  session.sendAudio(new Int16Array([1, -1]), true);
  session.sendAudio(new Int16Array([2, -2]), true);

  assert.deepEqual(sent.map((event) => event.type), [
    'input_audio_buffer.append',
    'input_audio_buffer.append',
    'input_image_buffer.append',
  ]);
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
    name: 'jiuwen_research',
    call_id: 'call-weather',
    arguments: '{"query":"香港今天的天气"}',
  };

  session.handleEvent(event);
  session.handleEvent(event);

  assert.deepEqual(functionCalls, [{
    name: 'jiuwen_research',
    callId: 'call-weather',
    arguments: '{"query":"香港今天的天气"}',
    query: '香港今天的天气',
  }]);
  assert.equal(diagnostics.at(-1).event, 'qwen_tool_call_received');
});

test('tool results wait while busy then return through the native call id', () => {
  const { session, sent, dispatchedToolResults, assistantTexts } = createSession();
  session.responseActive = true;
  assert.equal(session.enqueueToolResult({
    jobId: 'search-weather',
    question: '香港今天的天气',
    result: '香港今天有雨。',
    callId: 'call-weather',
  }), true);

  session.dispatchQueuedToolResult();
  assert.equal(session.pendingToolResults.length, 1);
  assert.deepEqual(sent, []);

  session.responseActive = false;
  session.dispatchQueuedToolResult();

  assert.equal(session.pendingToolResults.length, 0);
  assert.deepEqual(dispatchedToolResults, ['search-weather']);
  assert.deepEqual(sent.map((event) => event.type), [
    'conversation.item.create',
    'conversation.item.create',
    'response.create',
  ]);

  session.handleEvent({ type: 'response.created', response: { id: 'answer-weather' } });
  session.handleEvent({
    type: 'response.text.done',
    response_id: 'answer-weather',
    text: '香港今天有雨，出门请带伞。',
  });
  assert.equal(assistantTexts.at(-1).toolJobId, 'search-weather');
});

test('session update includes Gateway-provided tools', async () => {
  const tools = [{ type: 'function', function: { name: 'jiuwen_research' } }];
  let socket;
  class StartupSocket {
    static OPEN = 1;
    constructor() {
      socket = this;
      this.readyState = StartupSocket.OPEN;
      this.sent = [];
    }
    close() {}
    send(message) { this.sent.push(JSON.parse(message)); }
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
  assert.match(socket.sent[0].session.instructions, /MUST call jiuwen_research in the same turn/);
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
  assert.deepEqual(posted.map((message) => message.type), ['audio', 'audio', 'drain']);
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
