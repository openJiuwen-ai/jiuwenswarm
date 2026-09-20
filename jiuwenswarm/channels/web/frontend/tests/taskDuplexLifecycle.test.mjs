import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { afterEach, beforeEach, test } from 'node:test';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

// Exercise the real task runtime, registry and start/stop controller. Only media,
// network and application UI sinks are replaced; no remote model is needed.
const pluginRoot = resolve('../../../extensions/video_duplex/frontend');
const outfile = resolve('node_modules/.cache/task-duplex-lifecycle/runtime.mjs');
await build({
  stdin: {
    contents: `export { TaskFullDuplexRuntime } from ${JSON.stringify(`${pluginRoot}/TaskFullDuplexRuntime.tsx`)};
      export { JoyAIProvider } from ${JSON.stringify(`${pluginRoot}/VideoLivePanel/joyaiProvider.ts`)};
      export { useChatStore } from './src/stores/chatStore.ts';
      export { buildArtifacts } from './src/components/ArtifactsPanel/artifactCollection.ts';
      export { parseHistoryJsonFileToPreviewMessages } from './src/features/historyRestore.ts';
      export { artifactDownloadUrl, artifactTextPreviewUrl } from './src/components/ArtifactsPanel/filePreviewModel.ts';
      export * from ${JSON.stringify(`${pluginRoot}/taskFullDuplexRuntimeStore.ts`)};`,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  outfile,
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  external: ['react', 'react/*'],
  jsx: 'automatic',
  define: { 'import.meta.env.DEV': 'false' },
  plugins: [
    {
      name: 'task-runtime-ports',
      setup(builder) {
        builder.onResolve(
          { filter: /(?:services\/webClient|taskProgressStore|taskFullDuplex\/featureFlag|\/VideoLivePanel)$/ },
          ({ path }) => ({ path, namespace: 'ports' }),
        );
        builder.onLoad({ filter: /.*/, namespace: 'ports' }, ({ path }) => ({
          loader: 'tsx',
          contents: path.endsWith('/VideoLivePanel')
            ? `
          import { forwardRef, useImperativeHandle } from 'react';
          export const VideoLivePanel = forwardRef(function Panel(props, ref) {
            useImperativeHandle(ref, () => ({
              async startScreenDuplex(id) {
                globalThis.__duplexHarness.starts.push(id);
                props.onRuntimeState('active');
                return true;
              },
              stop() { props.onRuntimeState('idle'); },
              deliverToolResult(payload) { globalThis.__duplexHarness.mediaResults.push(payload); }
            }));
            return null;
          });`
            : path.endsWith('webClient')
              ? `
          export const webClient = { on(...args) { return globalThis.__duplexHarness.on(...args); } };
          export const webRequest = (...args) => globalThis.__duplexHarness.request(...args);`
              : path.endsWith('featureFlag')
                ? 'export const useTaskFullDuplexEnabled = () => true;'
                : `export const useApplicationTaskStore = { getState: () => globalThis.__duplexHarness.tasks };
                   export const registerApplicationTaskController = () => () => {};`,
        }));
      },
    },
  ],
});
const {
  TaskFullDuplexRuntime,
  startTaskFullDuplex,
  stopTaskFullDuplex,
  JoyAIProvider,
  useChatStore,
  buildArtifacts,
  parseHistoryJsonFileToPreviewMessages,
  artifactDownloadUrl,
  artifactTextPreviewUrl,
} = await import(pathToFileURL(outfile));

let dom;
let root;
let harness;
let props;
let timers;
const previousGlobals = new Map();

beforeEach(async () => {
  dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
  for (const [name, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previousGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  const listeners = new Map();
  timers = new Map();
  let timerId = 0;
  window.setInterval = (callback) => {
    timers.set(++timerId, callback);
    return timerId;
  };
  window.clearInterval = (id) => timers.delete(id);
  harness = {
    starts: [],
    mediaResults: [],
    requests: [],
    messages: [],
    tools: [],
    reasons: [],
    statuses: new Map(),
    tasks: {
      sessions: {},
      upsert(sessionId, task) {
        const jobs = (this.sessions[sessionId] ||= []);
        const index = jobs.findIndex((job) => job.id === task.id);
        if (index < 0) jobs.push(task);
        else jobs[index] = task;
      },
    },
    on(event, callback) {
      const callbacks = listeners.get(event) || new Set();
      callbacks.add(callback);
      listeners.set(event, callbacks);
      return () => callbacks.delete(callback);
    },
    emit(event, payload) {
      listeners.get(`video.search.${event}`)?.forEach((fn) => fn({ payload }));
    },
    async request(method, params) {
      this.requests.push({ method, params });
      if (method === 'video.search.status') return this.statuses.get(params.job_id);
      return { persisted: true };
    },
  };
  globalThis.__duplexHarness = harness;
  useChatStore.setState({ runtimes: {} });
  props = {
    sessionId: 'conversation-a',
    onConversationItem: (...args) => harness.messages.push(args),
    onAssistantStream() {},
    onReasoning: (...args) => harness.reasons.push(args),
    onReasoningClose() {},
    onToolCall: (...args) => harness.tools.push(args),
    onToolResult: (...args) => harness.tools.push(args),
    onFileItems: (sid, files, timestampIso) => {
      useChatStore.getState().ensureRuntime(sid);
      useChatStore.getState().addFileItems(sid, files, { timestampIso });
    },
  };
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(createElement(TaskFullDuplexRuntime, props)));
});

afterEach(async () => {
  await act(async () => {
    stopTaskFullDuplex();
    root.unmount();
  });
  dom.window.close();
  delete globalThis.__duplexHarness;
  for (const [name, descriptor] of previousGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
  previousGlobals.clear();
});

async function start(sessionId = props.sessionId) {
  await act(async () => startTaskFullDuplex(sessionId));
  return harness.starts.at(-1);
}

async function event(name, payload) {
  await act(async () => harness.emit(name, payload));
}

function job(searchSessionId, overrides = {}) {
  return { job_id: 'job-a', search_session_id: searchSessionId, question: '生成代码', status: 'running', ...overrides };
}

test('cancelled jobs remain cancelled after late success and persist one cancellation record', async () => {
  const scope = await start();
  await event('started', job(scope));
  await event('cancelled', job(scope, { status: 'cancelled' }));
  await event('completed', job(scope, { status: 'completed', display_result: '不应显示的迟到成功' }));
  assert.equal(harness.tasks.sessions['conversation-a'][0].status, 'cancelled');
  assert.equal(harness.messages.length, 1);
  assert.match(harness.messages[0][2], /已停止任务/);
  assert.equal(harness.mediaResults.length, 1);
  assert.equal(harness.mediaResults[0].status, 'cancelled');
});

test('media restart keeps the Core Agent scope, while a different conversation gets its own scope', async () => {
  const first = await start();
  await act(async () => stopTaskFullDuplex());
  assert.equal(await start(), first);
  await act(async () => root.render(createElement(TaskFullDuplexRuntime, { ...props, sessionId: 'conversation-b' })));
  assert.notEqual(await start('conversation-b'), first);
});

test('a stopped conversation receives and persists a completed result exactly once without audio', async () => {
  const scope = await start();
  await event('started', job(scope));
  await act(async () => stopTaskFullDuplex());
  const completed = job(scope, { status: 'completed', result: 'short receipt', display_result: '完整代码' });
  await event('completed', completed);
  await event('completed', completed);
  await event('progress', job(scope)); // delayed poll must not regress terminal status
  assert.deepEqual(harness.messages, [['conversation-a', 'assistant', '完整代码', 'tool_result']]);
  assert.equal(harness.tasks.sessions['conversation-a'][0].status, 'completed');
  assert.equal(harness.requests.filter((r) => r.method === 'video.conversation.append').length, 1);
  assert.equal(harness.mediaResults.length, 0);
});

test('polling recovers missing completion events after media stops, including tool history', async () => {
  const scope = await start();
  await event('started', job(scope));
  await act(async () => stopTaskFullDuplex());
  harness.statuses.set(
    'job-a',
    job(scope, {
      status: 'completed',
      result: '计算结果为 42',
      progress_history: [
        {
          sequence: 1,
          stage: 'tool_call',
          title: '计算',
          status: 'running',
          tool_call_id: 'call-a',
          tool_name: 'calculate',
        },
        {
          sequence: 2,
          stage: 'tool_result',
          title: '完成',
          status: 'completed',
          tool_call_id: 'call-a',
          tool_name: 'calculate',
          tool_result: '42',
        },
      ],
    }),
  );
  await act(async () => {
    for (const tick of timers.values()) tick();
  });
  assert.equal(harness.messages[0][2], '计算结果为 42');
  assert.equal(harness.tools.length, 2);
  assert.equal(harness.tools[0][1].id, harness.tools[1][1].toolCallId);
  await event('completed', harness.statuses.get('job-a'));
  assert.equal(harness.messages.length, 1);
  assert.equal(harness.tools.length, 2);
});

test('switching conversations keeps old jobs attached to their owner and never plays them in the new conversation', async () => {
  const scopeA = await start();
  await event('started', job(scopeA));
  await act(async () => root.render(createElement(TaskFullDuplexRuntime, { ...props, sessionId: 'conversation-b' })));
  const scopeB = await start('conversation-b');
  await event('started', job(scopeB, { job_id: 'job-b' }));
  await event('completed', job(scopeA, { status: 'completed', result: 'A 的结果' }));
  await event('failed', job(scopeB, { job_id: 'job-b', status: 'failed', error: '权限不足' }));
  assert.equal(harness.messages[0][0], 'conversation-a');
  assert.equal(harness.messages[1][0], 'conversation-b');
  assert.equal(harness.mediaResults.length, 1);
  assert.equal(harness.mediaResults[0].job_id, 'job-b');
});

test('unregistered plugin sessions cannot inject results into the visible task', async () => {
  await start();
  await event('completed', job('standalone-plugin-session', { status: 'completed', result: 'unrelated' }));
  assert.equal(harness.messages.length, 0);
  assert.deepEqual(harness.tasks.sessions, {});
});

test('a late job acknowledgement after stop is still tracked and recovered', async () => {
  const scope = await start();
  await act(async () => stopTaskFullDuplex());
  await event('started', job(scope));
  await event('failed', job(scope, { status: 'failed', error: '服务不可用' }));
  assert.equal(harness.messages[0][0], 'conversation-a');
  assert.match(harness.messages[0][2], /服务不可用/);
  assert.equal(harness.mediaResults.length, 0);
});

function joyaiCallbacks(overrides = {}) {
  return {
    getLatestFrameDataUrl: () => 'data:image/jpeg;base64,frame',
    getFrameCount: () => 1,
    getSearchSessionId: () => 'task-duplex:conversation-a',
    hasPendingTranscriptions: () => false,
    beginTranscription() {},
    finishTranscription() {},
    appendChat() {},
    commitAssistantAnswer() {},
    rememberSearchJob() {},
    updateSearchJob() {},
    setAwaitingVoiceTranscript() {},
    setError() {},
    setToolStatus() {},
    setRecording() {},
    setStatus() {},
    setStarting() {},
    report() {},
    ...overrides,
  };
}

test('JoyAI in-flight work is registered after media stop, but its stale answer is not shown or spoken', async () => {
  let resolveFrame;
  harness.request = () =>
    new Promise((resolve) => {
      resolveFrame = resolve;
    });
  const remembered = [];
  const answers = [];
  const provider = new JoyAIProvider(
    joyaiCallbacks({
      rememberSearchJob: (...args) => remembered.push(args),
      commitAssistantAnswer: (text) => answers.push(text),
    }),
  );
  provider.sessionId = 'media-a';
  const frame = provider.requestFrame('生成代码', '生成代码');
  await Promise.resolve();
  provider.stop();
  resolveFrame({ response: '旧回答', search_job: { id: 'late-job', search_session_id: 'task-duplex:conversation-a' } });
  assert.equal(await frame, null);
  assert.equal(remembered[0][0].id, 'late-job');
  assert.equal(remembered[0][1], false);
  assert.deepEqual(answers, []);
});

test('JoyAI receipt playback does not duplicate the conversation-owned full result', async () => {
  const displayed = [];
  const spoken = [];
  const provider = new JoyAIProvider(joyaiCallbacks({ commitAssistantAnswer: (text) => displayed.push(text) }));
  provider.sessionId = 'media-a';
  provider.speakText = (text) => spoken.push(text);
  const payload = job('task-duplex:conversation-a', { status: 'completed', display_result: '完整代码内容' });
  const brief = { status: 'completed', summary: '代码已经生成', result_kind: 'code', displayed_in_ui: true };
  assert.equal(provider.handleCompletedSearch(payload, brief), true);
  await provider.searchDeliveryQueue;
  assert.deepEqual(displayed, []);
  assert.deepEqual(spoken, ['代码已经生成']);
  assert.equal(provider.pendingToolContext[0].result, '完整代码内容');
  provider.stop();
});

test('files arriving after media stops or conversation switches populate original artifacts and survive native history restore', async () => {
  const scopeA = await start();
  await event('started', job(scopeA));
  await act(async () => stopTaskFullDuplex());
  await act(async () => root.render(createElement(TaskFullDuplexRuntime, { ...props, sessionId: 'conversation-b' })));
  await start('conversation-b');
  const file = {
    name: 'report.py',
    path: 'C:/work/report.py',
    size: 12,
    mime_type: 'text/x-python',
    download_url: '/file-api/download?token=test-token',
    download_token: 'test-token',
  };
  const entry = {
    sequence: 3,
    stage: 'file',
    status: 'completed',
    title: '文件已返回',
    timestamp: 1_800_000_000,
    files: [file],
  };
  await event('progress', job(scopeA, { progress: entry }));
  await event('progress', job(scopeA, { progress: entry }));
  const completed = job(scopeA, { status: 'completed', result: '文件已返回', progress_history: [entry] });
  harness.statuses.set('job-a', completed);
  await act(async () => {
    for (const tick of timers.values()) tick();
  });
  const artifacts = buildArtifacts(useChatStore.getState().getRuntime('conversation-a').messages);
  assert.equal(artifacts.length, 1);
  assert.equal(artifacts[0].path, file.path);
  assert.equal(artifactDownloadUrl(artifacts[0]), file.download_url);
  assert.equal(artifactTextPreviewUrl(artifacts[0], 'http://localhost'), `${file.download_url}&inline=1`);
  assert.equal(useChatStore.getState().getRuntime('conversation-b'), undefined);
  assert.equal(harness.mediaResults.length, 0);
  const savedFiles = harness.requests.filter(
    (r) => r.method === 'video.conversation.append' && r.params.kind === 'file',
  );
  assert.equal(savedFiles.length, 1);
  assert.equal(savedFiles[0].params.session_id, 'conversation-a');
  assert.deepEqual(savedFiles[0].params.files, [file]);
  const restored = parseHistoryJsonFileToPreviewMessages(
    [
      {
        role: 'assistant',
        channel_id: 'video_duplex',
        session_id: 'conversation-a',
        content: '',
        event_type: 'chat.file',
        timestamp: savedFiles[0].params.timestamp,
        files: savedFiles[0].params.files,
      },
    ],
    'conversation-a',
  );
  assert.deepEqual(
    buildArtifacts(restored).map((a) => [a.name, a.path, a.downloadUrl]),
    artifacts.map((a) => [a.name, a.path, a.downloadUrl]),
  );
});

test('a missed file push is recovered from terminal status and newer tokens retain one artifact', async () => {
  const scope = await start();
  await event('started', job(scope));
  await act(async () => stopTaskFullDuplex());
  const file = {
    name: 'report.txt',
    path: 'C:/work/report.txt',
    size: 1,
    mime_type: 'text/plain',
    download_url: '',
    download_token: 'first',
  };
  const entries = [
    { sequence: 1, stage: 'file', status: 'completed', title: 'file', timestamp: 1_800_000_000, files: [file] },
    {
      sequence: 2,
      stage: 'file',
      status: 'completed',
      title: 'file',
      timestamp: 1_800_000_001,
      files: [{ ...file, download_token: 'renewed' }],
    },
  ];
  harness.statuses.set('job-a', job(scope, { status: 'failed', error: '后续步骤失败', progress_history: entries }));
  await act(async () => {
    for (const tick of timers.values()) tick();
  });
  const artifacts = buildArtifacts(useChatStore.getState().getRuntime('conversation-a').messages);
  assert.equal(artifacts.length, 1);
  assert.equal(artifacts[0].downloadUrl, '/file-api/download?token=renewed');
  assert.equal(harness.mediaResults.length, 0);
});
