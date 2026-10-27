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
    contents: `export { TaskFullDuplexAction } from ${JSON.stringify(`${pluginRoot}/TaskFullDuplexAction.tsx`)};
      export { TaskFullDuplexRuntime, flushConversationHistory } from ${JSON.stringify(`${pluginRoot}/TaskFullDuplexRuntime.tsx`)};
      export { JoyAIProvider } from ${JSON.stringify(`${pluginRoot}/VideoLivePanel/joyaiProvider.ts`)};
      export { ApplicationConversationRuntimes } from './src/applicationPlugins/ApplicationConversationRuntimes.tsx';
      export { useSessionAssetsStore } from './src/features/sessionAssets/sessionAssets.ts';
      export { useChatStore } from './src/stores/chatStore.ts';
      export { useSessionStore } from './src/stores/sessionStore.ts';
      export { toastStore } from './src/components/ui/Toast/toastStore.ts';
      export { buildArtifacts } from './src/components/ArtifactsPanel/artifactCollection.ts';
      export { parseHistoryJsonFileToPreviewMessages, parseHistoryJsonFileToTimelinePreview } from './src/features/historyRestore.ts';
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
  loader: { '.css': 'empty' },
  define: { 'import.meta.env.DEV': 'false' },
  plugins: [
    {
      name: 'task-runtime-ports',
      setup(builder) {
        builder.onResolve(
          { filter: /(?:services\/webClient|taskProgressStore|taskFullDuplex\/featureFlag|\/VideoLivePanel|components\/form|ApplicationPluginOutlet)$/ },
          ({ path }) => ({ path, namespace: 'ports' }),
        );
        builder.onLoad({ filter: /.*/, namespace: 'ports' }, ({ path }) => ({
          loader: 'tsx',
          resolveDir: process.cwd(),
          contents: path.endsWith('ApplicationPluginOutlet')
            ? `import { TaskFullDuplexRuntime } from ${JSON.stringify(`${pluginRoot}/TaskFullDuplexRuntime.tsx`)};
               export const ApplicationPluginTaskRuntimes = TaskFullDuplexRuntime;`
            : path.endsWith('components/form')
            ? `export function FormDialog(props) {
                return <div data-testid={props.testIdPrefix}>{props.children}
                  <button data-testid={props.testIdPrefix + '-confirm'} onClick={props.onConfirm}>Confirm</button>
                  <button data-testid={props.testIdPrefix + '-cancel'} onClick={props.onCancel}>Cancel</button>
                </div>;
              }`
            : path.endsWith('/VideoLivePanel')
            ? `
          import { forwardRef, useImperativeHandle } from 'react';
          export const VideoLivePanel = forwardRef(function Panel(props, ref) {
            globalThis.__duplexHarness.panelProps = props;
            useImperativeHandle(ref, () => ({
              async startScreenDuplex(id, shareScreen, visualSource) {
                if (globalThis.__duplexHarness.startError) throw new Error(globalThis.__duplexHarness.startError);
                globalThis.__duplexHarness.sharingRequests.push(shareScreen);
                globalThis.__duplexHarness.visualSources.push(visualSource);
                globalThis.__duplexHarness.starts.push(id);
                props.onRuntimeState('active');
                props.onScreenSharingChange(shareScreen);
                return true;
              },
              setMicrophoneMuted(value) { globalThis.__duplexHarness.mic.push(value); },
              setSpeakerMuted(value) { globalThis.__duplexHarness.speaker.push(value); },
              stopScreenSharing() { props.onScreenSharingChange(false); },
              stop() { globalThis.__duplexHarness.stops += 1; props.onRuntimeState('idle'); },
              deliverToolResult(payload) { globalThis.__duplexHarness.mediaResults.push(payload); },
              deliverQuestion(payload) { globalThis.__duplexHarness.mediaQuestions.push(payload); },
              deliverTaskInteraction(payload) { globalThis.__duplexHarness.interactions.push(payload); }
            }));
            return null;
          });`
            : path.endsWith('webClient')
              ? `
          export const webClient = { on(...args) { return globalThis.__duplexHarness.on(...args); } };
          export const webRequest = (...args) => globalThis.__duplexHarness.request(...args);
          export const requestAgentDetail = async () => null;
          export const requestPhaseAgents = async () => [];`
              : path.endsWith('featureFlag')
                ? 'export const useTaskFullDuplexEnabled = () => true; export const setTaskFullDuplexEnabled = () => {};'
                : `export const useApplicationTaskStore = { getState: () => globalThis.__duplexHarness.tasks };
                   export const registerApplicationTaskController = () => () => {};`,
        }));
      },
    },
  ],
});
const {
  TaskFullDuplexRuntime,
  flushConversationHistory,
  toastStore,
  ApplicationConversationRuntimes,
  useSessionAssetsStore,
  TaskFullDuplexAction,
  startTaskFullDuplex,
  stopTaskFullDuplex,
  JoyAIProvider,
  toggleTaskDuplexMicrophone,
  toggleTaskDuplexSpeaker,
  stopTaskDuplexScreenSharing,
  useChatStore,
  useSessionStore,
  buildArtifacts,
  parseHistoryJsonFileToPreviewMessages,
  parseHistoryJsonFileToTimelinePreview,
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
  dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/', pretendToBeVisual: true });
  dom.window.HTMLCanvasElement.prototype.getContext = () => null;
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
    stops: 0,
    starts: [],
    sharingRequests: [],
    visualSources: [],
    mic: [],
    speaker: [],
    mediaResults: [],
    mediaQuestions: [],
    interactions: [],
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
      if (method === 'session.assets.register') return { assets: params.items.map((item, index) => ({ ...item, asset_id: String(index), name: 'report', kind: 'document', created_at: 1 })) };
      if (method === 'video.search.list') return { jobs: this.recoveryJobs || [], next_offset: null };
      if (method === 'video.search.status') return this.statuses.get(params.job_id);
      return { persisted: true };
    },
  };
  globalThis.__duplexHarness = harness;
  useChatStore.setState({ runtimes: {} });
  useSessionAssetsStore.setState({ bySession: {} });
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

test('headless task runtime forwards waiting forms only to their active conversation', async () => {
  const scope = await start();
  const interaction = { id: 'form-a', request_id: 'request-a', state: 'pending', source: 'ask_user_interrupt',
    questions: [{ question: '什么时候执行？' }, { question: '内容是什么？' }] };
  await event('progress', job(scope, { status: 'waiting_user', interaction }));
  assert.deepEqual(harness.interactions.at(-1).interaction, interaction);
  assert.match(harness.tasks.sessions['conversation-a'][0].detail, /什么时候执行/);
  const count = harness.interactions.length;
  await event('progress', job('foreign-scope', { status: 'waiting_user', interaction }));
  assert.equal(harness.interactions.length, count);
  await act(async () => stopTaskFullDuplex());
  await event('progress', job(scope, { sequence: 3, status: 'waiting_user', interaction }));
  assert.equal(harness.interactions.length, count, 'stopped media must not receive voice questions');
});

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

test('a pending approval card reaches the live voice session, but not from replays or after stop', async () => {
  // In task mode the panel is headless; without this the voice model never
  // learns a card is waiting and cannot tell the user to click it (#8941).
  const scope = await start();
  const card = {
    id: 'interaction-a', request_id: 'call-a', source: 'confirm_interrupt', state: 'pending',
    questions: [{ question: 'cua_agent 将操作你的桌面', header: '操作确认: cua_agent', options: [] }],
  };
  await event('started', job(scope));
  await event('progress', job(scope, { status: 'waiting_user', interaction: card }));
  await event('progress', job(scope, { status: 'waiting_user', interaction: card, replay: true }));
  assert.equal(harness.mediaQuestions.length, 1);
  assert.equal(harness.mediaQuestions[0].interaction.source, 'confirm_interrupt');

  await act(async () => stopTaskFullDuplex());
  await event('progress', job(scope, { status: 'waiting_user', interaction: { ...card, id: 'interaction-b' } }));
  assert.equal(harness.mediaQuestions.length, 1);
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

test('navigation preserves voice, controls and result delivery to the original conversation', async () => {
  const renderPage = async (sessionId) => act(async () => root.render(createElement('div', null,
    createElement(TaskFullDuplexRuntime, { ...props, sessionId }),
    createElement('section', { 'data-testid': 'navigation-page' }, sessionId || 'Settings'),
  )));
  await act(async () => root.render(null));
  await renderPage('conversation-a');
  const scopeA = await start();
  const stopsBefore = harness.stops;
  await event('started', job(scopeA));
  for (const sessionId of ['conversation-b', null, 'conversation-a']) {
    await renderPage(sessionId);
    assert.equal(harness.stops, stopsBefore);
    assert.ok(document.querySelector('[data-testid="chat-panel-duplex-orb"]'));
  }
  await renderPage('conversation-b');
  await act(async () => harness.panelProps.onConversationItem('user', 'Still talking to A'));
  await event('completed', job(scopeA, { status: 'completed', result: 'Result for A' }));
  assert.equal(harness.messages.at(-2)[0], 'conversation-a');
  assert.equal(harness.messages.at(-1)[0], 'conversation-a');
  assert.equal(harness.mediaResults.at(-1).job_id, 'job-a');
  await act(async () => document.querySelector('[data-testid="chat-panel-duplex-exit"]').click());
  assert.equal(harness.stops, stopsBefore + 1);
  await act(async () => new Promise((resolve) => setTimeout(resolve, 250)));
  assert.equal(document.querySelector('[data-testid="chat-panel-duplex-orb"]'), null);
});

test('starting full-duplex in another conversation replaces the previous media run', async () => {
  await start();
  const stopsBefore = harness.stops;
  await act(async () => root.render(createElement(TaskFullDuplexRuntime, { ...props, sessionId: 'conversation-b' })));
  assert.equal(harness.stops, stopsBefore);
  await start('conversation-b');
  assert.equal(harness.stops, stopsBefore + 1);
  await act(async () => harness.panelProps.onConversationItem('user', 'Talking to B now'));
  assert.equal(harness.messages.at(-1)[0], 'conversation-b');
});

test('restart waits for saved conversation history and cancellation prevents a delayed start', async () => {
  await start();
  const request = harness.request.bind(harness);
  let finishWrite;
  harness.request = (method, params) => method === 'video.conversation.append'
    ? new Promise(resolve => { finishWrite = resolve; })
    : request(method, params);
  await act(async () => harness.panelProps.onConversationItem('user', 'Remember Cedar'));
  assert.equal(typeof finishWrite, 'function');
  let restarting;
  await act(async () => { restarting = startTaskFullDuplex('conversation-a'); });
  assert.equal(harness.starts.length, 1, 'history must be saved before the new transport starts');
  await act(async () => stopTaskFullDuplex());
  await act(async () => { finishWrite({ persisted: true }); await restarting; });
  assert.equal(harness.starts.length, 1, 'cancelled preparation must not reopen the microphone');
});

test('unregistered plugin sessions cannot inject results into the visible task', async () => {
  await start();
  await event('completed', job('standalone-plugin-session', { status: 'completed', result: 'unrelated' }));
  assert.equal(harness.messages.length, 0);
  assert.deepEqual(harness.tasks.sessions, {});
});

test('failed history writes block restart and retry the original events in order with stable IDs', async () => {
  await start();
  const request = harness.request.bind(harness);
  const writes = [];
  let fail = true;
  harness.request = async (method, params) => {
    if (method !== 'video.conversation.append') return request(method, params);
    writes.push({ ...params });
    if (fail) throw new Error('History storage unavailable');
    return { persisted: true };
  };
  await act(async () => harness.panelProps.onConversationItem('user', 'Remember Cedar'));
  await act(async () => harness.panelProps.onConversationItem('assistant', 'I will remember it'));
  await assert.rejects(flushConversationHistory('conversation-a'), /History storage unavailable/);
  await start('conversation-a');
  assert.equal(harness.starts.length, 1, 'do not start with incomplete history');
  assert.ok(toastStore.getSnapshot().some(notice => notice.content.includes('History storage unavailable')), 'surface the saving failure');
  fail = false;
  await start('conversation-a');
  assert.equal(harness.starts.length, 2);
  const firstId = writes[0].event_id;
  assert.ok(firstId);
  assert.ok(writes.slice(0, -1).every(event => event.event_id === firstId));
  assert.notEqual(writes.at(-1).event_id, firstId);
  assert.equal(writes.at(-1).content, 'I will remember it');
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
  assert.deepEqual(displayed, ['代码已经生成']);
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


test('recovery restores persisted tasks without replaying history or audio', async () => {
  const scope = 'task-duplex:conversation-a';
  harness.recoveryJobs = [job(scope, { status: 'completed', sequence: 8, display_result: 'Saved result' })];
  await act(async () => { for (const run of timers.values()) run(); });
  assert.equal(harness.tasks.sessions['conversation-a'][0].status, 'completed');
  assert.equal(harness.messages.length, 0);
  assert.equal(harness.mediaResults.length, 0);
  await event('completed', job(scope, { status: 'completed', sequence: 8, display_result: 'Saved result' }));
  assert.equal(harness.messages.length, 0);
  assert.equal(harness.requests.filter(r => r.method === 'video.conversation.append').length, 0);
});


test('voice starts without screen capture and forwards mute controls without stopping the session', async () => {
  await start();
  assert.deepEqual(harness.sharingRequests, [false]);
  await act(async () => {
    toggleTaskDuplexMicrophone();
    toggleTaskDuplexMicrophone();
    toggleTaskDuplexSpeaker();
    stopTaskDuplexScreenSharing();
  });
  assert.deepEqual(harness.mic, [true, false]);
  assert.deepEqual(harness.speaker, [true]);
  assert.equal(harness.starts.length, 1);
  await act(async () => stopTaskFullDuplex());
  await act(async () => toggleTaskDuplexMicrophone());
  assert.deepEqual(harness.mic, [true, false]);
});

const cuaCard = { request_id: 'card-a', source: 'confirm_interrupt',
  questions: [{ question: 'cua_agent 将操作你的桌面', header: '操作确认: cua_agent', options: [] }] };

async function showCard(sessionId, card) {
  await act(async () => {
    useChatStore.getState().ensureRuntime(sessionId);
    useChatStore.getState().enqueuePendingQuestion(sessionId, card);
  });
}

async function clickCard(sessionId, card) {
  await act(async () => useChatStore.getState().consumePendingQuestion(sessionId, card));
}

test('an open approval card holds the mic until it is clicked', async () => {
  // Voice answered a spoken "本次允许" with "已允许" while the card was still
  // unclicked (#5538). The model must not hear consent at all, the same way the
  // card locks the text composer, so the hold does not depend on the model.
  await start();
  await showCard('conversation-a', cuaCard);
  assert.deepEqual(harness.mic, [true]);
  await act(async () => toggleTaskDuplexMicrophone());
  assert.deepEqual(harness.mic, [true], 'the user cannot unmute past an open card');
  await clickCard('conversation-a', cuaCard);
  assert.deepEqual(harness.mic, [true, false]);
});

test('with two cards open, clicking one keeps the mic held until the last closes', async () => {
  // Two voice tasks can each wait on a card; answering one must not reopen the mic.
  await start();
  const second = { ...cuaCard, request_id: 'card-b', questions: [{ ...cuaCard.questions[0], question: '另一个桌面操作' }] };
  await showCard('conversation-a', cuaCard);
  await showCard('conversation-a', second);
  await clickCard('conversation-a', cuaCard);
  assert.deepEqual(harness.mic, [true]);
  await clickCard('conversation-a', second);
  assert.deepEqual(harness.mic, [true, false]);
});

test('a card of any non-form source holds the mic, information forms do not', async () => {
  // Default-deny: only plain information forms are voice-answerable.
  await start();
  await showCard('conversation-a', { request_id: 'form-a', source: 'ask_user_interrupt', questions: [{ question: '预算是多少？' }] });
  assert.deepEqual(harness.mic, []);
  await showCard('conversation-a', { ...cuaCard, request_id: 'new-kind', source: 'some_future_card' });
  assert.deepEqual(harness.mic, [true]);
});

test('closing a card keeps the mic muted when the user had muted it', async () => {
  await start();
  await act(async () => toggleTaskDuplexMicrophone());
  await showCard('conversation-a', cuaCard);
  await clickCard('conversation-a', cuaCard);
  assert.equal(harness.mic.at(-1), true, "the user's own mute is restored, not overridden");
});

test('a card in another conversation does not hold this voice session', async () => {
  await start();
  await showCard('conversation-b', cuaCard);
  assert.deepEqual(harness.mic, []);
});

test('a renewed media session re-applies the hold for a card still open', async () => {
  // Qwen replaces its connection after 300s without a response; the new session
  // starts unmuted, so the hold must be applied again.
  await start();
  await showCard('conversation-a', cuaCard);
  await act(async () => {
    harness.panelProps.onRuntimeState('starting');
    harness.panelProps.onRuntimeState('active');
  });
  assert.deepEqual(harness.mic, [true, true]);
});

test('screen sharing is opt-in on start and stopping sharing leaves voice controls active', async () => {
  window.localStorage.setItem('jiuwen.taskFullDuplex.screenSharing', 'true');
  await start();
  assert.deepEqual(harness.sharingRequests, [true]);
  await act(async () => { stopTaskDuplexScreenSharing(); toggleTaskDuplexSpeaker(); });
  assert.deepEqual(harness.speaker, [true]);
});


test('typing keeps the native send button alongside the active orb and accessible exit controls', async () => {
  await act(async () => root.render(null));
  await act(async () => root.render(createElement('div', { className: 'chat-input-frame' },
    createElement(TaskFullDuplexRuntime, props),
    createElement(TaskFullDuplexAction, {
      eligible: true, sessionId: props.sessionId, hasTextDraft: true,
      ensureSession: async () => props.sessionId,
      labels: { start: 'start', stop: 'stop', starting: 'starting' },
      fallback: createElement('button', { 'data-testid': 'native-send' }, 'Send'),
    }),
  )));
  await start();
  assert.ok(document.querySelector('[data-testid="native-send"]'));
  assert.ok(document.querySelector('[data-testid="chat-panel-duplex-orb"]'));
  assert.equal(document.querySelector('[data-testid="chat-panel-input-full-duplex"]'), null);
  const microphone = document.querySelector('[data-testid="chat-panel-duplex-microphone"]');
  await act(async () => microphone.click());
  assert.equal(microphone.getAttribute('aria-pressed'), 'true');
  await act(async () => document.querySelector('[data-testid="chat-panel-duplex-exit"]').click());
  // The orb fades out before it unmounts.
  assert.ok(document.querySelector('.task-full-duplex-orb.is-leaving'));
  await act(async () => new Promise((resolve) => setTimeout(resolve, 250)));
  assert.equal(document.querySelector('[data-testid="chat-panel-duplex-orb"]'), null);
  assert.ok(document.querySelector('[data-testid="native-send"]'));
});

test('the undragged orb stays above the input frame and any pending card docked over it', async () => {
  // Issue #8943: a fixed bottom offset let the orb cover the pending approval card.
  const rect = (top, height) => () => ({ top, bottom: top + height, left: 300, right: 1000, width: 700, height, x: 300, y: top });
  const renderComposer = (withCard) => act(async () => root.render(createElement('div', null,
    createElement(TaskFullDuplexRuntime, { ...props, key: 'runtime' }),
    withCard ? createElement('div', { key: 'card', 'data-testid': 'interaction-slot-root' }) : null,
    createElement('div', { key: 'frame', className: 'chat-input-frame' }),
  )));
  await act(async () => root.render(null));
  await renderComposer(true);
  const card = document.querySelector('[data-testid="interaction-slot-root"]');
  card.getBoundingClientRect = rect(560, 49);
  document.querySelector('.chat-input-frame').getBoundingClientRect = rect(618, 150);
  await start();
  const orb = () => document.querySelector('[data-testid="chat-panel-duplex-controls"]');
  // Layout changes are measured on the next animation frame.
  const nextFrame = () => act(() => new Promise((resolve) => dom.window.requestAnimationFrame(() => resolve())));
  // JSDOM viewport is 768px tall: 768 - card top 560 + 12px gap.
  assert.equal(orb().style.bottom, '220px');
  // An expanded card pushes the orb up with it.
  card.getBoundingClientRect = rect(480, 129);
  await act(async () => card.append(document.createElement('p')));
  await nextFrame();
  assert.equal(orb().style.bottom, '300px');
  // A streamed reply mutates the page per commit; it must not force a layout per mutation.
  let measured = 0;
  card.getBoundingClientRect = () => { measured += 1; return rect(480, 129)(); };
  await act(async () => {
    for (let i = 0; i < 20; i += 1) {
      card.append(document.createElement('span'));
      await Promise.resolve(); // separate commits, as while a reply streams
    }
  });
  await nextFrame();
  assert.equal(measured, 1);
  // Once the card is resolved the orb settles above the input frame.
  await renderComposer(false);
  await nextFrame();
  assert.equal(orb().style.bottom, '162px');
});

async function mountAction() {
  await act(async () => root.render(null));
  await act(async () => root.render(createElement('div', { className: 'chat-input-frame' },
    createElement(TaskFullDuplexRuntime, props),
    createElement(TaskFullDuplexAction, {
      eligible: true, sessionId: props.sessionId,
      ensureSession: async () => props.sessionId,
      labels: { start: 'start', stop: 'stop', starting: 'starting' },
      fallback: createElement('button', null, 'Send'),
    }),
  )));
}

test('a failed start does not leave its error on the button in another conversation', async () => {
  harness.startError = 'Not supported';
  await mountAction();
  await act(async () => document.querySelector('[data-testid="chat-panel-input-full-duplex"]').click());
  const button = () => document.querySelector('[data-testid="chat-panel-input-full-duplex"]');
  assert.match(button().getAttribute('title'), /Not supported/);
  await act(async () => root.render(createElement('div', { className: 'chat-input-frame' },
    createElement(TaskFullDuplexRuntime, props),
    createElement(TaskFullDuplexAction, {
      eligible: true, sessionId: 'another-session',
      ensureSession: async () => 'another-session',
      labels: { start: 'start', stop: 'stop', starting: 'starting' },
      fallback: createElement('button', null, 'Send'),
    }),
  )));
  assert.equal(button().getAttribute('title'), 'start');
  harness.startError = '';
});

test('canceling the share chooser starts no media session', async () => {
  window.localStorage.setItem('jiuwen.taskFullDuplex.screenSharing', 'true');
  await mountAction();
  await act(async () => document.querySelector('[data-testid="chat-panel-input-full-duplex"]').click());
  assert.ok(document.querySelector('[data-testid="chat-panel-duplex-share-dialog"]'));
  assert.equal(harness.starts.length, 0);
  await act(async () => document.querySelector('[data-testid="chat-panel-duplex-share-dialog-cancel"]').click());
  assert.equal(document.querySelector('[data-testid="chat-panel-duplex-share-dialog"]'), null);
  assert.equal(harness.starts.length, 0);
});

test('camera selection reaches media controller and closing sharing retains voice', async () => {
  window.localStorage.setItem('jiuwen.taskFullDuplex.screenSharing', 'true');
  await mountAction();
  await act(async () => document.querySelector('[data-testid="chat-panel-input-full-duplex"]').click());
  await act(async () => document.querySelector('[data-testid="chat-panel-duplex-source"][data-variant="camera"]').click());
  await act(async () => document.querySelector('[data-testid="chat-panel-duplex-share-dialog-confirm"]').click());
  assert.deepEqual(harness.visualSources, ['camera']);
  assert.ok(document.querySelector('[data-testid="chat-panel-duplex-sharing-indicator"]'));
  await act(async () => document.querySelector('[data-testid="chat-panel-duplex-close-sharing"]').click());
  assert.equal(document.querySelector('[data-testid="chat-panel-duplex-sharing-indicator"]'), null);
  assert.ok(document.querySelector('[data-testid="chat-panel-duplex-orb"]'));
  await act(async () => toggleTaskDuplexSpeaker());
  assert.deepEqual(harness.speaker, [true]);
});


test('final-only files populate artifacts and background session assets once through application callbacks', async (t) => {
  let now = Date.now();
  t.mock.method(Date, 'now', () => now);
  await act(async () => root.render(null));
  useChatStore.getState().ensureRuntime('conversation-a');
  await act(async () => root.render(createElement(ApplicationConversationRuntimes, { sessionId: 'conversation-a' })));
  const scope = await start();
  await event('started', job(scope));
  await act(async () => root.render(createElement(ApplicationConversationRuntimes, { sessionId: 'conversation-b' })));
  const file = { name: 'report.pdf', path: 'C:/work/report.pdf', download_token: 'initial' };
  const completed = job(scope, { status: 'completed', result: '报告完成', files: [file] });
  await event('completed', completed);
  await event('completed', completed);
  assert.equal(buildArtifacts(useChatStore.getState().getRuntime('conversation-a').messages).length, 1);
  assert.equal(useChatStore.getState().getRuntime('conversation-b'), undefined);
  assert.equal(useSessionAssetsStore.getState().bySession['conversation-a'][0].path, file.path);
  const registrations = harness.requests.filter(r => r.method === 'session.assets.register');
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].params.session_id, 'conversation-a');
  assert.deepEqual(registrations[0].params.items, [{ path: file.path, source: 'generated' }]);
  assert.equal(harness.requests.filter(r => r.method === 'video.conversation.append' && r.params.kind === 'file').length, 1);
  now += 1_000;
  await event('completed', job(scope, { ...completed, files: [{ ...file, download_token: 'renewed' }] }));
  const artifacts = buildArtifacts(useChatStore.getState().getRuntime('conversation-a').messages);
  assert.equal(artifacts.length, 1);
  assert.equal(artifacts[0].downloadToken, 'renewed');
});

test('task board displays the independent title, retains it through progress and falls back for legacy tasks', async () => {
  const scope = await start();
  await event('started', job(scope, { title: '修改打卡提醒为九点', question: '把刚才那个改到九点' }));
  assert.equal(harness.tasks.sessions['conversation-a'][0].title, '修改打卡提醒为九点');
  await event('progress', job(scope, { question: '把刚才那个改到九点' }));
  assert.equal(harness.tasks.sessions['conversation-a'][0].title, '修改打卡提醒为九点');
  await event('started', job(scope, { job_id: 'legacy-job', question: '完整的原始任务要求' }));
  assert.equal(harness.tasks.sessions['conversation-a'][1].title, '完整的原始任务要求');
});

async function renderWithControls() {
  await act(async () => root.render(null));
  await act(async () => root.render(createElement('div', { className: 'chat-input-frame' },
    createElement(TaskFullDuplexRuntime, props),
    createElement(TaskFullDuplexAction, {
      eligible: true, sessionId: props.sessionId, hasTextDraft: false,
      ensureSession: async () => props.sessionId,
      labels: { start: 'start', stop: 'stop', starting: 'starting' },
    }),
  )));
}

const cancelRequests = () => harness.requests
  .filter(({ method, params }) => method === 'video.search.control' && params.action === 'cancel')
  .map(({ params }) => params);

test('#5527 the exit control cancels this conversation\'s queued and running tasks; internal stop does not', async () => {
  await renderWithControls();
  const scope = await start();
  await event('progress', job(scope, { job_id: 'job-running', status: 'running', sequence: 1 }));
  await event('progress', job(scope, { job_id: 'job-queued', status: 'queued', sequence: 1 }));
  await event('completed', job(scope, { job_id: 'job-done', status: 'completed', sequence: 1, display_result: 'ok' }));
  await act(async () => stopTaskFullDuplex());
  assert.deepEqual(cancelRequests(), [], 'a media stop/restart keeps conversation-owned tasks');
  await start();
  await act(async () => document.querySelector('[data-testid="chat-panel-duplex-exit"]').click());
  await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  assert.deepEqual(cancelRequests(), [
    { job_id: 'job-queued', search_session_id: scope, action: 'cancel' },
    { job_id: 'job-running', search_session_id: scope, action: 'cancel' },
  ]);
});

test('#5570 a newer queue snapshot is applied even when its opaque queue_version hash is smaller', async () => {
  const scope = await start();
  await event('progress', job(scope, { job_id: 'job-q', status: 'queued', sequence: 2, queue_version: 999_999, queue_position: 1 }));
  await event('queue', { search_session_id: scope, queue_version: 5,
    jobs: [job(scope, { job_id: 'job-q', status: 'queued', sequence: 3, queue_version: 5, queue_position: 2, replay: true })] });
  const task = harness.tasks.sessions['conversation-a'].find((item) => item.id === 'job-q');
  assert.equal(task.queuePosition, 2);
  assert.equal(task.queueVersion, 5);
  await event('queue', { search_session_id: scope, queue_version: 7,
    jobs: [job(scope, { job_id: 'job-q', status: 'queued', sequence: 1, queue_version: 7, queue_position: 9, replay: true })] });
  assert.equal(harness.tasks.sessions['conversation-a'].find((item) => item.id === 'job-q').queuePosition, 2,
    'an older per-task sequence is still ignored');
});

test('a queue snapshot without a sequence still applies its terminal status', async () => {
  const scope = await start();
  await event('progress', job(scope, { job_id: 'job-s', status: 'running', sequence: 4 }));
  await event('queue', { search_session_id: scope, queue_version: 3,
    jobs: [job(scope, { job_id: 'job-s', status: 'cancelled', replay: true })] });
  assert.equal(harness.tasks.sessions['conversation-a'].find((item) => item.id === 'job-s').status, 'cancelled');
});

test('#5544 a capacity wait names the conversation and task it is waiting on', async () => {
  useSessionStore.setState({ sessions: [{ session_id: 'conversation-b', title: '整理周报', project_id: '', project_dir: '' }] });
  const scope = await start();
  await event('progress', job(scope, { job_id: 'job-wait', status: 'queued', sequence: 1,
    wait_reason: 'capacity_order_or_resource',
    waiting_on: [{ id: 'job-b', title: '打开浏览器下载报表', status: 'running', search_session_id: 'task-duplex:conversation-b' }] }));
  const detail = harness.tasks.sessions['conversation-a'].find((item) => item.id === 'job-wait').detail;
  assert.match(detail, /会话「整理周报」的任务「打开浏览器下载报表」（执行中）/);
});
