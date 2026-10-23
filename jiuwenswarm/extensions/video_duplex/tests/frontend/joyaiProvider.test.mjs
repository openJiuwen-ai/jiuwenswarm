import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(new URL('../../../../channels/web/frontend/package.json', import.meta.url));
const { build } = require('esbuild');
const compiled = await build({
  entryPoints: [fileURLToPath(new URL('../../frontend/VideoLivePanel/joyaiProvider.ts', import.meta.url))],
  bundle: true, platform: 'node', format: 'esm', write: false,
  plugins: [{
    name: 'offline-web-client',
    setup(builder) {
      builder.onResolve({ filter: /\/services\/webClient$/ }, () => ({ path: 'web-client', namespace: 'offline' }));
      builder.onLoad({ filter: /.*/, namespace: 'offline' }, () => ({ contents: `
        export const webClient = { on() { throw new Error('Unexpected network subscription'); } };
        export function webRequest(...args) {
          if (globalThis.joyaiTestRequest) return globalThis.joyaiTestRequest(...args);
          throw new Error('Unexpected network request');
        }
      ` }));
    },
  }],
});
const { JoyAIProvider } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);
globalThis.window = globalThis;

test('voice-only turns reach JoyAI before and after screen sharing without idle empty requests', async (t) => {
  const requests = [];
  const answers = [];
  const speech = [];
  let tick;
  let frame = '';
  t.mock.method(globalThis, 'setInterval', (callback) => { tick = callback; return 123; });
  t.mock.method(globalThis, 'clearInterval', () => {});
  globalThis.joyaiTestRequest = async (method, params) => {
    requests.push(params);
    return { response: '听见了', search_job: null };
  };
  t.after(() => { delete globalThis.joyaiTestRequest; });
  const provider = new JoyAIProvider({
    getSearchSessionId: () => 'scope', getLatestFrameDataUrl: () => frame, getFrameCount: () => frame ? 1 : 0,
    rememberSearchJob() {}, report() {}, commitAssistantAnswer: text => answers.push(text),
  });
  provider.sessionId = 'voice-only';
  provider.speakText = text => speech.push(text);
  provider.startFramePolling();
  tick();
  assert.equal(requests.length, 0, 'no picture and no instruction must not cause background inference');
  let pending = provider.submitUserInstruction('你能听见我吗？');
  tick();
  await pending;
  assert.equal(requests[0].frame_data_url, '');
  assert.equal(requests[0].request_kind, 'user');
  assert.equal(requests[0].question, '你能听见我吗？');
  assert.equal(requests[0].frame_time_range, '');
  assert.deepEqual(answers, ['听见了']);
  assert.deepEqual(speech, ['听见了']);

  frame = 'data:image/jpeg;base64,ZmFrZQ==';
  pending = provider.submitUserInstruction('现在共享画面');
  tick();
  await pending;
  assert.equal(requests[1].frame_data_url, frame);
  frame = '';
  provider.framePollingPausedUntil = Date.now() + 60_000;
  pending = provider.submitUserInstruction('关闭共享后继续说话');
  tick();
  assert.equal(requests.length, 2, 'voice-only turns must still respect quota cooldown');
  provider.framePollingPausedUntil = 0;
  tick();
  await pending;
  assert.equal(requests[2].frame_data_url, '', 'never resend an old screen image after sharing ends');
  assert.equal(requests[2].joyai_session_id, requests[1].joyai_session_id);
  tick();
  assert.equal(requests.length, 3, 'voice-only mode must not keep polling inference');
  provider.stop();
});

const brief = { status: 'completed', summary: '代码已生成，详情见界面。', source: 'core_agent' };
test('long user instructions and their final constraints reach the RPC intact', async (t) => {
  const calls = [];
  globalThis.joyaiTestRequest = async (...args) => {
    calls.push(args);
    return { response: '', search_job: null };
  };
  t.after(() => { delete globalThis.joyaiTestRequest; });
  const provider = new JoyAIProvider({
    getSearchSessionId: () => 'scope', rememberSearchJob() {}, report() {},
  });
  provider.sessionId = 'media-long-input';
  const tail = 'Do not modify any files.';
  const instruction = 'x'.repeat(16000 - tail.length) + tail;
  await provider.requestFrame(instruction, instruction, 'data:image/jpeg;base64,ZmFrZQ==');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'video.joyai.frame');
  assert.equal(calls[0][1].instruction, instruction);
  assert.equal(calls[0][1].question, instruction);
});

test('JoyAI attaches restored context only to actual user turns and clears it on stop', async (t) => {
  const calls = [];
  globalThis.joyaiTestRequest = async (method, params) => {
    calls.push({ method, params });
    return { response: '', search_job: null };
  };
  t.after(() => { delete globalThis.joyaiTestRequest; });
  const provider = new JoyAIProvider({
    getSearchSessionId: () => 'task-duplex:chat-a', rememberSearchJob() {}, report() {},
  });
  provider.sessionId = 'media-a';
  provider.restoreToken = 'restore-a';
  assert.deepEqual(calls, [], 'restoring memory must not synthesize a user request');
  await provider.requestFrame('', '', 'data:image/jpeg;base64,ZmFrZQ==');
  assert.equal(calls[0].params.request_kind, 'frame');
  assert.equal(calls[0].params.restore_token, undefined);
  await provider.requestFrame('What is my project codename?', 'What is my project codename?', 'data:image/jpeg;base64,ZmFrZQ==');
  assert.equal(calls[1].params.restore_token, 'restore-a');
  assert.equal(calls[1].params.search_session_id, 'task-duplex:chat-a');
  await provider.requestFrame('What about its task?', 'What about its task?', 'data:image/jpeg;base64,ZmFrZQ==');
  assert.equal(calls[2].params.restore_token, 'restore-a', 'each user turn receives fresh task state from the backend');
  provider.stop();
  assert.equal(provider.restoreToken, '');
  provider.sessionId = 'media-b';
  await provider.requestFrame('Hello', 'Hello', 'data:image/jpeg;base64,ZmFrZQ==');
  assert.equal(calls[3].params.restore_token, undefined, 'a new media session must not reuse the old restore token');
});

function setup() {
  const speech = [];
  const summaries = [];
  const provider = new JoyAIProvider({
    hasPendingTranscriptions: () => false,
    setToolStatus() {}, report() {},
    commitAssistantAnswer(text, jobId) { summaries.push({ text, jobId }); },
  });
  provider.sessionId = 'media-1';
  provider.speakText = (text) => { speech.push(text); };
  const payload = (id) => ({ job_id: id, question: `任务${id}`, result: '旧字段', display_result: '```python\nprint(1)\n```' });
  return { provider, speech, summaries, payload };
}

test('expired JoyAI context refreshes once in the same scope and retries the same command', async (t) => {
  const calls = [];
  const provider = new JoyAIProvider({ getSearchSessionId: () => 'task-duplex:chat-a', rememberSearchJob() {} });
  provider.sessionId = 'media-a';
  provider.restoreToken = 'expired';
  globalThis.joyaiTestRequest = async (method, params) => {
    calls.push({ method, params });
    if (method === 'video.realtime.config') return { context_restore: { token: 'fresh' } };
    if (params.restore_token === 'expired') throw Object.assign(new Error('expired'), { code: 'CONTEXT_RESTORE_EXPIRED' });
    return { response: '', search_job: null };
  };
  t.after(() => { delete globalThis.joyaiTestRequest; });
  await provider.requestFrame('Continue', 'Continue', 'data:image/jpeg;base64,ZmFrZQ==');
  assert.deepEqual(calls.map(call => call.method), ['video.joyai.frame', 'video.realtime.config', 'video.joyai.frame']);
  assert.equal(calls[1].params.search_session_id, 'task-duplex:chat-a');
  assert.equal(calls[2].params.command_id, calls[0].params.command_id);
  assert.equal(calls[2].params.restore_token, 'fresh');
});

test('JoyAI restore errors do not loop, retry authorization failures, or replay cancelled runs', async (t) => {
  t.after(() => { delete globalThis.joyaiTestRequest; });
  for (const mode of ['failed', 'expired-again', 'stop', 'replace', 'empty']) {
    const calls = [];
    const provider = new JoyAIProvider({ getSearchSessionId: () => 'task-duplex:chat-a', rememberSearchJob() {} });
    provider.sessionId = 'media-a';
    provider.restoreToken = 'expired';
    globalThis.joyaiTestRequest = async (method, params) => {
      calls.push({ method, params });
      if (method === 'video.realtime.config') {
        if (mode === 'stop') provider.stop();
        if (mode === 'replace') provider.sessionId = 'media-b';
        return mode === 'empty' ? {} : { context_restore: { token: 'fresh' } };
      }
      if (mode === 'empty' && !params.restore_token) return { response: '', search_job: null };
      throw Object.assign(new Error(mode), { code: mode === 'failed' ? 'CONTEXT_RESTORE_FAILED' : 'CONTEXT_RESTORE_EXPIRED' });
    };
    const request = provider.requestFrame('Continue', 'Continue', 'data:image/jpeg;base64,ZmFrZQ==');
    if (mode === 'failed' || mode === 'expired-again') await assert.rejects(request);
    else await request;
    assert.equal(calls.length, mode === 'failed' ? 1 : ['stop', 'replace'].includes(mode) ? 2 : 3);
    if (mode === 'empty') assert.equal(provider.restoreToken, '');
  }
});

test('Core summary is displayed immediately and spoken once without repeating the full tool result', async () => {
  const { provider, speech, summaries, payload } = setup();
  provider.userSpeechActive = true;
  provider.handleCompletedSearch(payload('a'), brief);
  provider.handleCompletedSearch(payload('a'), brief);
  assert.deepEqual(summaries, [{ text: brief.summary, jobId: 'a' }]);
  assert.deepEqual(speech, []);
  assert.doesNotMatch(summaries[0].text, /print\(1\)/);
  provider.userSpeechActive = false;
  await provider.searchDeliveryQueue;
  assert.deepEqual(speech, [brief.summary]);
  provider.stop();
  assert.equal(provider.deliveredSearchIds.size, 0);
});

test('tool context is immediate; only the brief waits for speech, in task order', async () => {
  const { provider, speech, payload } = setup();
  provider.userSpeechActive = true;
  assert.equal(provider.handleCompletedSearch(payload('a'), brief), true);
  assert.equal(provider.handleCompletedSearch(payload('b'), { ...brief, summary: '第二项已完成。' }), true);
  assert.equal(provider.pendingToolContext.length, 2);
  assert.equal(provider.pendingToolContext[0].result, payload('a').display_result);
  await Promise.resolve();
  assert.deepEqual(speech, []);
  provider.userSpeechActive = false;
  await provider.searchDeliveryQueue;
  assert.deepEqual(speech, [brief.summary, '第二项已完成。']);
});

test('continuous frame inference does not block a ready tool receipt', async () => {
  const { provider, speech, payload } = setup();
  provider.requestQueue = new Promise(() => {});
  provider.queuedRequestCount = 1;
  provider.handleCompletedSearch(payload('a'), brief);
  await provider.searchDeliveryQueue;
  assert.deepEqual(speech, [brief.summary]);
});

test('tool receipts wait for playback and do not leak into a restarted media session', async () => {
  const { provider, speech, payload } = setup();
  let release;
  provider.ttsQueue = new Promise((resolve) => { release = resolve; });
  provider.handleCompletedSearch(payload('a'), brief);
  const delivery = provider.searchDeliveryQueue;
  await Promise.resolve();
  assert.deepEqual(speech, []);
  provider.stop();
  provider.sessionId = 'media-2';
  release();
  await delivery;
  assert.deepEqual(speech, []);
});
