import './fixtures/browserEnvironment.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { A2UIProvider } from '@a2ui/react';
import { act } from 'react';
import { createRoot } from 'react-dom/client';

await build({
  stdin: { contents: `export * from './src/components/ChatPanel/MessageList.tsx';
    export * from './src/applicationPlugins/taskProgressStore.ts';
    export * from './src/applicationPlugins/taskTimelineClock.ts';`, resolveDir: process.cwd(), loader: 'tsx' },
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/chat-timeline-list/MessageList.mjs',
  define: { 'import.meta.env': '{}' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [
    {
      name: 'ui-test-svg-components',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-react-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
          contents: 'export default function SvgStub() { return null; }',
          loader: 'js',
        }));
      },
    },
  ],
});

const { ChatTimelineList, TurnElapsed, useApplicationTaskStore, taskTimelineClock } = await import('../node_modules/.cache/chat-timeline-list/MessageList.mjs');

test('#6010 duplex elapsed ticks without progress and freezes after cancellation', async () => {
  const originalNow = Date.now;
  const originalInterval = window.setInterval;
  const originalClear = window.clearInterval;
  const timers = new Map();
  let nextId = 0;
  const start = 1_700_000_000_000;
  let now = start + 5000;
  Date.now = () => now;
  window.setInterval = callback => { const id = ++nextId; timers.set(id, callback); return id; };
  window.clearInterval = id => timers.delete(id);
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const task = { id: 'elapsed-job', pluginId: 'video-duplex', title: 'Weather', status: 'running',
    sequence: 1, detail: '', createdAt: start + 1000 };
  useApplicationTaskStore.setState({ sessions: { 'clock-session': [task] } });
  const messages = [{ id: 'full-duplex-user', role: 'user', content: 'weather', timestamp: new Date(start).toISOString() }];
  const reasoningSegments = [{ id: 'reason', content: 'Checking', startedAt: start + 1000,
    updatedAt: start + 2000, isStreaming: false }];
  const label = () => container.querySelector('[data-testid="chat-panel-completed-work-chip-label"]')?.textContent;
  try {
    await act(async () => root.render(createElement(A2UIProvider, null, createElement(ChatTimelineList, {
      messages, reasoningSegments, sessionId: 'clock-session', virtualized: false,
    }))));
    assert.match(label(), /5s/);
    await act(async () => { now += 1000; for (const callback of [...timers.values()]) callback(); });
    assert.match(label(), /6s/, 'advances without receiving a new progress event');
    await act(async () => useApplicationTaskStore.getState().upsert('clock-session', { ...task, status: 'cancelled', sequence: 2 }));
    const stopped = label();
    await act(async () => { now += 10000; for (const callback of [...timers.values()]) callback(); });
    assert.equal(label(), stopped, 'terminal tasks do not continue counting');
    assert.equal(timers.size, 0);
  } finally {
    await act(async () => root.unmount());
    container.remove();
    useApplicationTaskStore.setState({ sessions: {} });
    Date.now = originalNow;
    window.setInterval = originalInterval;
    window.clearInterval = originalClear;
  }
});

test('#6010 task clocks isolate turns and never count queued or unknown tasks as running', () => {
  const base = { id: 'job', pluginId: 'video-duplex', createdAt: 2000 };
  assert.equal(taskTimelineClock([{ ...base, status: 'running' }], 3000, 4000, []), undefined);
  assert.equal(taskTimelineClock([{ ...base, status: 'running' }], 1000, 2000, []), undefined);
  assert.equal(taskTimelineClock([{ ...base, status: 'running' }], 2000, 3000, []).active, true);
  for (const status of ['queued', 'unknown', 'cancelled', 'failed', 'completed']) {
    assert.equal(taskTimelineClock([{ ...base, status }], 2000, 3000, []).active, false);
  }
  assert.equal(taskTimelineClock([{ ...base, status: 'cancelling' }], 3000, 4000, ['full-duplex-core-job-call']).active, true);
  useApplicationTaskStore.setState({ sessions: {} });
  useApplicationTaskStore.getState().upsert('recovered', { ...base, status: 'completed', sequence: 1 });
  assert.equal(useApplicationTaskStore.getState().sessions.recovered[0].finishedAt, undefined,
    'recovered terminal tasks must not use current time as completion time');
  useApplicationTaskStore.setState({ sessions: {} });
});

test('in-progress timeline summary uses the selected Agent identity', () => {
  const markup = renderToStaticMarkup(
    createElement(TurnElapsed, {
      startMs: 1_700_000_000_000,
      endMs: 1_700_000_001_000,
      isLastTurn: false,
      showAvatar: true,
      agentTemplateName: 'expert-a',
      teamLayout: false,
    }),
  );

  assert.equal(markup.includes('team_leader avatar'), false);
  assert.match(markup, /chat-panel-agent-avatar-name/);
  assert.match(markup, />expert-a<\/span>/);
});

test('Team timeline summary keeps the existing leader identity', () => {
  const markup = renderToStaticMarkup(
    createElement(TurnElapsed, {
      startMs: 1_700_000_000_000,
      endMs: 1_700_000_001_000,
      isLastTurn: false,
      showAvatar: true,
      agentTemplateName: 'expert-a',
      teamLayout: true,
    }),
  );

  assert.match(markup, /team_leader avatar/);
  assert.equal(markup.includes('chat-panel-agent-avatar-name'), false);
});

test('expert Team timeline summary uses the frozen leader identity', () => {
  const markup = renderToStaticMarkup(
    createElement(TurnElapsed, {
      startMs: 1_700_000_000_000,
      endMs: 1_700_000_001_000,
      isLastTurn: false,
      showAvatar: true,
      teamLayout: true,
      teamLeaderIdentity: {
        agentTemplateId: 'leader-template',
        displayName: '专家团负责人',
      },
    }),
  );

  assert.match(markup, /专家团负责人/);
  assert.equal(markup.includes('team_leader avatar'), false);
});

test('Expert Team timeline surface uses the selected group identity', () => {
  const markup = renderToStaticMarkup(
    createElement(TurnElapsed, {
      startMs: 1_700_000_000_000,
      endMs: 1_700_000_001_000,
      isLastTurn: false,
      showAvatar: true,
      teamLayout: true,
      teamLeaderIdentity: {
        agentTemplateId: 'leader-template',
        displayName: '专家团负责人',
      },
      teamGroupIdentity: {
        id: 'reply-confirmation',
        displayName: '只会回复好的收到',
        avatarUrl: null,
      },
    }),
  );

  assert.match(markup, /只会回复好的收到/);
  assert.doesNotMatch(markup, /专家团负责人/);
});

test('expert group identity is preserved in bounded history and complete static export', () => {
  const messages = Array.from({ length: 200 }, (_, index) => ({
    id: `history-${index}`,
    role: index % 2 === 0 ? 'user' : 'assistant',
    content: `history-content-${index}-end`,
    timestamp: new Date(1_700_000_000_000 + index * 1_000).toISOString(),
  }));
  for (const virtualized of [true, false]) {
    const markup = renderToStaticMarkup(createElement(A2UIProvider, null, createElement(ChatTimelineList, {
      messages,
      mode: 'team',
      staticTimeline: true,
      virtualized,
      teamGroupIdentityOverride: {
        id: 'history-experts',
        displayName: 'History Expert Group',
        avatarUrl: null,
      },
    })));

    assert.match(markup, /History Expert Group/);
    const renderedMessages = [...markup.matchAll(/history-content-\d+-end/g)];
    if (virtualized) {
      // SSR has no laid-out viewport; bottom scrolling happens after mount.
      assert.ok(renderedMessages.length > 0 && renderedMessages.length < messages.length);
    } else {
      assert.equal(renderedMessages.length, messages.length);
      assert.match(markup, /history-content-0-end/);
      assert.match(markup, /history-content-199-end/);
    }
    assert.equal(markup.includes('data-virtualized="true"'), virtualized);
  }
});
