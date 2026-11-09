import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { act, createElement, Fragment } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

await build({
  stdin: {
    contents: `
      export { TaskMeetingRuntime } from '../../../extensions/meeting/frontend/TaskMeetingRuntime';
      export { TaskMeetingAction } from '../../../extensions/meeting/frontend/TaskMeetingAction';
      export { publishMeeting } from '../../../extensions/meeting/frontend/taskMeetingStore';
      export { setTeamMeetingEnabled } from './src/features/teamMeeting/featureFlag';
    `,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  outfile: 'node_modules/.cache/meeting-controls.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  packages: 'external',
  loader: { '.css': 'empty' },
  plugins: [{
    name: 'meeting-service-boundaries',
    setup(builder) {
      const mocks = {
        '../../../channels/web/frontend/src/services/webClient': `
          export const webClient = {
            request: (...args) => globalThis.meetingFixture.request(...args),
            on: (...args) => globalThis.meetingFixture.on(...args),
            onStateChange: () => () => {},
            getState: () => 'ready',
          };
        `,
        '../../../channels/web/frontend/src/stores/sessionStore': `
          const store = { runtimes: {}, getRuntime: () => null };
          export const useSessionStore = Object.assign(selector => selector(store), { getState: () => store });
        `,
        '../../../channels/web/frontend/src/hooks/useAdaptiveTooltip':
          'export const useAdaptiveTooltip = () => ({ tooltip: null, handlers: {} });',
        '../../../channels/web/frontend/src/components/ui/Toast/toastStore':
          'export const toast = { open: () => {} };',
        '../../../channels/web/frontend/src/features/groupChatInput':
          'export const rememberGroupChatInput = () => {};',
        'react-i18next': `
          const t = key => key;
          const i18n = { language: 'en' };
          export const useTranslation = () => ({ t, i18n });
        `,
        './meetingRuntime': `
          export class MeetingSession {
            async start() {}
            async activatePlayback() {}
            stop() {}
            stopPlayback() {}
            finishCleanup() {}
            resume() {}
          }
        `,
      };
      builder.onResolve({ filter: /.*/ }, args =>
        mocks[args.path] ? { path: args.path, namespace: 'meeting-mock' } : undefined,
      );
      builder.onLoad({ filter: /.*/, namespace: 'meeting-mock' }, args => ({
        contents: mocks[args.path],
        loader: 'js',
      }));
    },
  }],
});

const { TaskMeetingRuntime, TaskMeetingAction, publishMeeting, setTeamMeetingEnabled } =
  await import('../node_modules/.cache/meeting-controls.mjs');

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

async function mount(options, run) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  const old = new Map(['window', 'document', 'IS_REACT_ACT_ENVIRONMENT', 'meetingFixture'].map(key => [
    key, Object.getOwnPropertyDescriptor(globalThis, key),
  ]));
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const events = new Map();
  const calls = [];
  const fixture = {
    status: { state: 'idle', team_exists: false, enabled: false, addressed_members: [] },
    statusHandler: params => options.status ? options.status(params) : fixture.status,
    request: async (method, params) => {
      calls.push({ method, params });
      if (method === 'config.get') return { team_meeting_enabled: options.enabled ?? true };
      if (method === 'meeting.status') return fixture.statusHandler(params);
      if (method === 'meeting.start')
        return { state: 'active', team_exists: true, enabled: true, meeting_id: 'meeting-1' };
      if (method === 'meeting.release' || method === 'meeting.end')
        return { state: 'idle', team_exists: true, enabled: false };
      throw new Error(`Unexpected RPC: ${method}`);
    },
    on: (event, handler) => {
      const handlers = events.get(event) ?? new Set();
      handlers.add(handler);
      events.set(event, handlers);
      return () => handlers.delete(handler);
    },
    emit: (event, payload) => act(async () => {
      for (const handler of events.get(event) ?? []) handler({ payload });
    }),
    button: () => document.querySelector('[data-testid="chat-panel-input-meeting"]'),
    calls,
  };
  globalThis.meetingFixture = fixture;
  setTeamMeetingEnabled(options.enabled ?? true);
  publishMeeting({ state: 'idle', sessionId: null, teamExists: false, enabled: false, playback: 'idle' });
  const root = createRoot(document.getElementById('root'));
  const render = sessionId => act(async () => root.render(createElement(Fragment, null,
    createElement(TaskMeetingRuntime, { sessionId }),
    createElement(TaskMeetingAction, { sessionId, fallback: null }),
  )));
  try {
    await render(options.sessionId === undefined ? 'team-chat' : options.sessionId);
    await run({ ...fixture, fixture, render });
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const [key, descriptor] of old) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}

test('meeting button stays visible but cannot start without a Team', async () => {
  for (const sessionId of [null, 'new', 'team-chat']) {
    await mount({ sessionId }, async ({ button, calls }) => {
      assert.ok(button());
      assert.equal(button().disabled, true);
      assert.match(button().getAttribute('aria-label'), /teamRequired/);
      await act(async () => button().click());
      assert.equal(calls.filter(call => call.method === 'meeting.start').length, 0);
    });
  }
});

test('global feature switch still hides meeting controls', async () => {
  await mount({ enabled: false }, async ({ button, calls }) => {
    assert.equal(button(), null);
    assert.equal(calls.filter(call => call.method === 'meeting.status').length, 0);
  });
});

test('closing retains its cleanup state when the Team is unavailable', async () => {
  await mount({}, async ({ button }) => {
    await act(async () => publishMeeting({ state: 'closing', teamExists: false }));
    assert.equal(button().disabled, false);
    assert.equal(button().getAttribute('aria-busy'), 'true');
    assert.match(button().getAttribute('aria-label'), /meeting.panel.states.closing/);
    assert.doesNotMatch(button().getAttribute('aria-label'), /teamRequired/);
    await act(async () => button().click());
    const end = document.querySelector('[data-testid="chat-panel-meeting-layer-end"]');
    assert.ok(end);
    assert.equal(end.disabled, true);
  });
});

test('reduced Team-created notification refreshes status and enables the button', async () => {
  await mount({}, async ({ button, emit, fixture, calls }) => {
    assert.equal(button().disabled, true);
    fixture.status = { ...fixture.status, team_exists: true };

    await emit('meeting.state', { session_id: 'team-chat', content: '' });

    assert.equal(button().disabled, false);
    assert.equal(calls.filter(call => call.method === 'meeting.status').length, 2);
    await act(async () => button().click());
    assert.equal(calls.filter(call => call.method === 'meeting.start').length, 1);
  });
});

test('reduced notifications preserve the known Team while status is loading', async () => {
  await mount({ status: () => ({ state: 'idle', team_exists: true, enabled: false }) },
    async ({ button, emit, fixture }) => {
      const pending = deferred();
      fixture.statusHandler = () => pending.promise;
      await emit('meeting.state', { session_id: 'team-chat', content: '' });
      assert.equal(button().disabled, false);
      await act(async () => pending.resolve({ state: 'idle', team_exists: true, enabled: false }));
      assert.equal(button().disabled, false);
    });
});

test('older status responses cannot undo the Team-created refresh', async () => {
  const initial = deferred();
  await mount({ status: () => initial.promise }, async ({ button, emit, fixture }) => {
    fixture.statusHandler = () => ({ state: 'idle', team_exists: true, enabled: false });
    await emit('meeting.state', { session_id: 'team-chat', content: '' });
    assert.equal(button().disabled, false);
    await act(async () => initial.resolve({ state: 'idle', team_exists: false, enabled: false }));
    assert.equal(button().disabled, false);
  });
});

test('complete notifications update directly and other sessions are ignored', async () => {
  await mount({}, async ({ button, emit, calls }) => {
    await emit('meeting.state', { session_id: 'other-chat', content: '' });
    assert.equal(button().disabled, true);
    await emit('meeting.state', {
      session_id: 'team-chat', state: 'idle', team_exists: true, enabled: false,
    });
    assert.equal(button().disabled, false);
    assert.equal(calls.filter(call => call.method === 'meeting.status').length, 1);
  });
});

test('switching to a chat without a Team disables the same visible button', async () => {
  await mount({ status: params => ({ state: 'idle', team_exists: params.session_id === 'team-chat', enabled: false }) },
    async ({ button, render, emit }) => {
      assert.equal(button().disabled, false);
      await render('empty-chat');
      assert.ok(button());
      assert.equal(button().disabled, true);
      await emit('meeting.state', { session_id: 'team-chat', state: 'idle', team_exists: true, enabled: false });
      assert.equal(button().disabled, true);
    });
});
