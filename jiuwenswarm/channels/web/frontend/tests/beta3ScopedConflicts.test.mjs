import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { transform } from 'esbuild';

const read = (path) => fs.readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8').replace(/\r\n/g, '\n');
function between(source, start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert.ok(first >= 0 && last > first, `source boundary: ${start}`);
  return source.slice(first + start.length, last);
}
async function compile(source) {
  const result = await transform(source, { loader: 'ts', format: 'esm', define: { 'import.meta.env': '{}' } });
  return import(`data:text/javascript;base64,${Buffer.from(result.code).toString('base64')}`);
}

const app = read('App.tsx');
const send = between(app, 'const handleSendMessage = useCallback(',
  ', [disposeInFlightHistoryHandles, mode, navigate, request, route.kind, sendMessage, setGoalObjective, t]);');
const dependencies = [
  'sessionIdRef', 'sendMessage', 'useSessionStore', 'useWorkspaceStore', 'useChatStore',
  'NEW_CONVERSATION_ID', 'isEnterprise', 'route', 'isConversationModelGroupBlocked',
  'isMultiModelCollaborationEnabled', 'disposeInFlightHistoryHandles', 'restoreUnsentInput',
  'parsePersistSessionCommand', 'creatingSessionRef', 'submitDesignComposer', 'navigate', 'toast', 't',
];
const { createSend } = await compile(`
  export function createSend(dependencies) {
    const { ${dependencies.join(', ')} } = dependencies;
    return ${send};
  }

`);

function sendFixture({ accepted = true, sessionId = 'session-1', design = false } = {}) {
  const sent = [];
  const restored = [];
  const refreshed = [];
  const processing = [];
  const projectReads = [];
  const workspace = {
    workMode: design ? 'design' : 'chat',
    projects: [{ project_id: 'project-1', is_default: false }],
    expandedProjectIds: {},
    toggleProjectExpanded: (id) => { workspace.expandedProjectIds[id] = true; },
    loadProjects: async () => { projectReads.push('projects'); },
    loadProjectSessions: async (id) => { projectReads.push(id); },
    setSelectedProject: () => {},
    refreshSessionWorkspace: async (session) => { refreshed.push(session.session_id); },
  };
  const creatingSessionRef = { current: false };
  const fn = createSend({
    sessionIdRef: { current: sessionId },
    sendMessage: async (...args) => { sent.push(args); return accepted; },
    useSessionStore: { getState: () => ({ getRuntime: () => ({ mode: 'agent' }),
      currentSession: { session_id: sessionId }, sessions: [] }) },
    useWorkspaceStore: { getState: () => workspace },
    useChatStore: { getState: () => ({ getRuntime: () => ({ isProcessing: false }),
      setProcessing: (...args) => processing.push(args) }) },
    NEW_CONVERSATION_ID: 'new', isEnterprise: () => false,
    route: { kind: sessionId === 'new' ? 'chat-new' : 'chat-session' },
    isConversationModelGroupBlocked: () => false, isMultiModelCollaborationEnabled: () => true,
    disposeInFlightHistoryHandles: () => {},
    restoreUnsentInput: (...args) => restored.push(args),
    parsePersistSessionCommand: (content) => ({ content, persistSession: false }),
    creatingSessionRef,
    submitDesignComposer: async () => ({ projectId: 'project-1', sessionId: 'design-1' }),
    navigate: () => {}, toast: { open: () => {} }, t: (key) => key,
  });
  return { fn, sent, restored, refreshed, processing, projectReads, creatingSessionRef };
}

test('existing conversation returns its accepted session identity and refreshes the workspace', async () => {
  const fixture = sendFixture();
  assert.equal(await fixture.fn('hello'), 'session-1');
  assert.deepEqual(fixture.sent, [['hello', 'session-1', undefined, undefined, undefined]]);
  assert.deepEqual(fixture.refreshed, ['session-1']);
});

test('rejected send returns no accepted session and restores the unsent input', async () => {
  const fixture = sendFixture({ accepted: false });
  assert.equal(await fixture.fn('keep this draft'), undefined);
  assert.deepEqual(fixture.restored, [['session-1', 'keep this draft']]);
  assert.deepEqual(fixture.refreshed, []);
});

for (const accepted of [true, false]) {
  test(`queued send retains its explicit options when the backend acceptance is ${accepted}`, async () => {
    const fixture = sendFixture({ accepted });
    const options = { queuedTaskId: '' };
    assert.equal(await fixture.fn('queued', undefined, options), undefined);
    assert.deepEqual(fixture.sent, [['queued', 'session-1', undefined, options]]);
    assert.deepEqual(fixture.refreshed, []);
  });
}

test('design creation refreshes each project list once and preserves its existing completion contract', async () => {
  const fixture = sendFixture({ sessionId: 'new', design: true });
  assert.equal(await fixture.fn('draw a poster'), undefined);
  assert.deepEqual(fixture.projectReads, ['projects', 'project-1']);
  assert.deepEqual(fixture.sent, []);
  assert.equal(fixture.creatingSessionRef.current, false);
  assert.deepEqual(fixture.processing, [['new', true], ['new', false]]);
});

test('ordinary conversation sends preserve the develop input provenance', async () => {
  const fixture = sendFixture();
  const provenance = { input_origin: 'prompt_suggestion', assistance_origin: 'prompt_suggestion',
    prompt_suggestion: { suggestion_id: 'suggestion-1', edited: false } };
  assert.equal(await fixture.fn('hello', undefined, provenance), 'session-1');
  assert.deepEqual(fixture.sent, [['hello', 'session-1', undefined, undefined, provenance]]);
});

const messageItem = read('components/ChatPanel/MessageItem.tsx');
const download = between(messageItem, 'const handleDownload = ', '\n  const handleSaveSkill = ');
const { createDownload } = await compile(`
  export function createDownload(dependencies) {
    const { expiredSet, needsGatewayResource, downloadGatewayResource, isEnterprise,
      prepareFileDownload, executeDesktopSave, window, document, t } = dependencies;
    const handleDownload = ${download}
    return handleDownload;
  }
`);

function downloadFixture({ gateway = false, allowed = true, fail = false } = {}) {
  const calls = [];
  const fn = createDownload({
    expiredSet: new Set(), needsGatewayResource: () => gateway,
    downloadGatewayResource: async (...args) => {
      calls.push(['gateway', ...args]);
      if (fail) throw new Error('denied');
    },
    isEnterprise: () => true,
    prepareFileDownload: async (url) => { calls.push(['prepare', url]); return allowed; },
    executeDesktopSave: async (save) => { await save(); return 'saved'; },
    window: { alert: () => calls.push(['alert']),
      pywebview: { api: { download_file: async (...args) => calls.push(['desktop', ...args]) } } },
    document: {}, t: (key) => key,
  });
  return { fn, calls };
}
const file = { name: 'report.pdf', download_url: 'https://gateway.test/file/token' };

test('Gateway-bound downloads use the declared request context and do not bypass it', async () => {
  const fixture = downloadFixture({ gateway: true });
  await fixture.fn(file, 0);
  assert.deepEqual(fixture.calls, [['gateway', file.download_url, file.name]]);
});

test('a failed Gateway-bound download reports failure without falling through to direct download', async () => {
  const fixture = downloadFixture({ gateway: true, fail: true });
  await fixture.fn(file, 0);
  assert.deepEqual(fixture.calls, [['gateway', file.download_url, file.name], ['alert']]);
});

test('ordinary enterprise downloads keep the develop preflight and desktop save path', async () => {
  const fixture = downloadFixture();
  await fixture.fn(file, 0);
  assert.deepEqual(fixture.calls, [['prepare', file.download_url], ['desktop', file.download_url, file.name]]);
});

test('an explicitly denied enterprise download never reaches the desktop save bridge', async () => {
  const fixture = downloadFixture({ allowed: false });
  await fixture.fn(file, 0);
  assert.deepEqual(fixture.calls, [['prepare', file.download_url], ['alert']]);
});

const headEffect = between(messageItem, '  useEffect(() => {\n    let cancelled = false;', '\n  }, [files]);');
const { checkAvailability } = await compile(`
  export function checkAvailability(dependencies) {
    const { files, needsGatewayResource, isEnterprise, gatewayFetch, fileDownloadHead, setExpiredSet } = dependencies;
    let cancelled = false;
    ${headEffect}
  }
`);
for (const scenario of [
  { gateway: true, enterprise: true, status: 503, expired: true },
  { gateway: false, enterprise: false, status: 503, expired: true },
  { gateway: false, enterprise: true, status: 503, expired: false },
  { gateway: false, enterprise: true, status: 403, expired: true },
  { gateway: false, enterprise: true, status: 404, expired: true },
  { gateway: false, enterprise: true, status: 410, expired: true },
  { gateway: false, enterprise: true, status: 200, initiallyExpired: true, expired: false },
  { gateway: false, enterprise: true, status: 'network-error', expired: false },
]) {
  test(`HEAD availability follows the resource policy: ${JSON.stringify(scenario)}`, async () => {
    let expired = new Set(scenario.initiallyExpired ? [0] : []);
    const calls = [];
    const head = async (kind) => {
      calls.push(kind);
      if (scenario.status === 'network-error') throw new Error('offline');
      return new Response(null, { status: scenario.status });
    };
    checkAvailability({ files: [file], needsGatewayResource: () => scenario.gateway,
      isEnterprise: () => scenario.enterprise, gatewayFetch: () => head('gateway'),
      fileDownloadHead: () => head('ordinary'), setExpiredSet: (update) => { expired = update(expired); } });
    await new Promise(setImmediate);
    assert.deepEqual(calls, [scenario.gateway ? 'gateway' : 'ordinary']);
    assert.equal(expired.has(0), scenario.expired);
  });
}

const { prepareFileDownload } = await compile(read('services/fileDownload.ts'));
for (const [status, allowed] of [[200, true], [403, false], [404, false], [410, false], [405, true], [500, true]]) {
  test(`download helper preserves the HEAD policy for ${status}`, async (t) => {
    const fetch = t.mock.method(globalThis, 'fetch', async () => new Response(null, { status }));
    assert.equal(await prepareFileDownload(file.download_url), allowed);
    assert.deepEqual(fetch.mock.calls[0].arguments, [file.download_url, { method: 'HEAD' }]);
  });
}

const restore = between(app, 'function restoreUnsentInput(', '\nfunction waitForNextPaint');
const { createRestore } = await compile(`
  export function createRestore(useChatStore, window, CustomEvent) {
    function restoreUnsentInput(${restore}
    return restoreUnsentInput;
  }
`);
test('rejected input restores the store and notifies the mounted composer', () => {
  const writes = [];
  const events = [];
  const restoreInput = createRestore({ getState: () => ({ setInputValue: (...args) => writes.push(args) }) },
    { dispatchEvent: (event) => events.push(event) }, CustomEvent);
  restoreInput('saved', 'keep my draft');
  assert.deepEqual(writes, [['saved', 'keep my draft']]);
  assert.equal(events[0].type, 'chat-input-sync');
  assert.deepEqual(events[0].detail, { sessionId: 'saved', value: 'keep my draft' });
});
