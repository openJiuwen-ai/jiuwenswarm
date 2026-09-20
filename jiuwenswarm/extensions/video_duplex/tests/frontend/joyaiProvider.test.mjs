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
        export function webRequest() { throw new Error('Unexpected network request'); }
      ` }));
    },
  }],
});
const { JoyAIProvider } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);
globalThis.window = globalThis;

const brief = { status: 'completed', summary: '代码已生成，详情见界面。', source: 'core_agent' };
function setup() {
  const speech = [];
  const provider = new JoyAIProvider({
    hasPendingTranscriptions: () => false,
    setToolStatus() {}, report() {},
    commitAssistantAnswer() { assert.fail('Tool results must use the shared display path'); },
  });
  provider.sessionId = 'media-1';
  provider.speakText = (text) => { speech.push(text); };
  const payload = (id) => ({ job_id: id, question: `任务${id}`, result: '旧字段', display_result: '```python\nprint(1)\n```' });
  return { provider, speech, payload };
}

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
