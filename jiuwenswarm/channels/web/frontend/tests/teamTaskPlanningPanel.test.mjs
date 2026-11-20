import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

await build({
  entryPoints: ['tests/fixtures/teamTaskPlanningExports.ts'],
  bundle: true, packages: 'external', platform: 'node', format: 'esm',
  outfile: 'node_modules/.cache/team-task-planning/panel.mjs',
  define: { 'import.meta.env': '{}' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [{
    name: 'ui-test-svg-components',
    setup(builder) {
      builder.onResolve({ filter: /\.css$/ }, ({ path }) => ({ path, namespace: 'empty-css' }));
      builder.onLoad({ filter: /.*/, namespace: 'empty-css' }, () => ({ contents: '', loader: 'js' }));
      builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-react-stub' }));
      builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
        contents: 'export default function SvgStub() { return null; }', loader: 'js',
      }));
    },
  }],
});

const bootstrapDom = new JSDOM('<!doctype html>', { url: 'http://localhost' });
globalThis.window = bootstrapDom.window;
globalThis.document = bootstrapDom.window.document;
const {
  TaskPlanningPanel, useTaskPlanningMetrics, useChatStore, useSessionStore,
  useTeamSelectorStore, webClient, setTeamOrganizationUiEnabled,
} = await import('../node_modules/.cache/team-task-planning/panel.mjs');
await i18n.use(initReactI18next).init({
  lng: 'en', fallbackLng: 'en', interpolation: { escapeValue: false },
  resources: Object.fromEntries(['en', 'zh'].map((language) => [
    language, { translation: JSON.parse(readFileSync('src/i18n/locales/' + language + '.json', 'utf8')) },
  ])),
});

const byTestId = (id) => document.querySelector('[data-testid="' + id + '"]');
const task = (id, status) => ({ task_id: id, title: id, content: '', status });

async function withPanel(run, { reducedMotion = false } = {}) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const originalRequest = webClient.request;
  const originalNow = performance.now;
  let now = 0;
  performance.now = () => now;
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const frames = new Map();
  const timers = new Map();
  let nextFrame = 0;
  dom.window.matchMedia = () => ({ matches: reducedMotion, addEventListener() {}, removeEventListener() {} });
  dom.window.requestAnimationFrame = (callback) => { frames.set(++nextFrame, callback); return nextFrame; };
  dom.window.cancelAnimationFrame = (id) => frames.delete(id);
  dom.window.setInterval = (callback) => { timers.set(++nextFrame, callback); return nextFrame; };
  dom.window.clearInterval = (id) => timers.delete(id);
  dom.window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  const browserGlobals = {
    requestAnimationFrame: dom.window.requestAnimationFrame,
    cancelAnimationFrame: dom.window.cancelAnimationFrame,
    ResizeObserver: dom.window.ResizeObserver,
    HTMLElement: dom.window.HTMLElement, SVGElement: dom.window.SVGElement,
    Element: dom.window.Element, getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  };
  const previousGlobals = Object.fromEntries(Object.keys(browserGlobals).map((key) => [key, globalThis[key]]));
  Object.assign(globalThis, browserGlobals);
  const root = createRoot(document.getElementById('root'));
  const sessionId = 'planning-session';
  useChatStore.setState({ activeSessionId: sessionId, runtimes: {} });
  useChatStore.getState().ensureRuntime(sessionId);
  useSessionStore.setState({ runtimes: {} });
  useSessionStore.getState().ensureRuntime(sessionId);
  useTeamSelectorStore.setState({
    runtimes: {
      [sessionId]: {
        teams: [{ team_id: 'owner', is_owner: true }, { team_id: 'expert', is_owner: false }],
        selectedTeamId: 'owner', loaded: true, loading: false,
      },
    },
  });
  setTeamOrganizationUiEnabled(true);
  const requests = [];
  webClient.request = async (method, params) => {
    requests.push({ method, params });
    return {};
  };
  const render = (props) => act(async () => root.render(createElement(TaskPlanningPanel, {
    variant: 'expanded', tasks: [], members: [], totalTasks: 0, completedTasks: 0, ...props,
  })));
  const click = (id) => act(async () => byTestId(id).click());
  const select = (teamId) => act(async () => useTeamSelectorStore.setState((state) => ({
    runtimes: {
      ...state.runtimes,
      [sessionId]: { ...state.runtimes[sessionId], selectedTeamId: teamId },
    },
  })));
  const advanceFrame = (time) => act(async () => {
    now = time;
    const callbacks = [...frames.values()];
    frames.clear();
    callbacks.forEach((callback) => callback(now));
  });
  const advanceSeconds = async (seconds) => {
    for (let second = 0; second < seconds; second += 1) {
      await act(async () => {
        now += 1000;
        [...timers.values()].forEach((callback) => callback());
      });
      await advanceFrame(now + 500);
    }
  };
  const setProcessing = (isProcessing, isPaused = false) => act(async () => useChatStore.setState((state) => ({
    runtimes: { ...state.runtimes, [sessionId]: { ...state.runtimes[sessionId], isProcessing, isPaused } },
  })));
  try {
    await run({ root, render, click, select, sessionId, requests, frames, timers, advanceFrame, advanceSeconds, setProcessing });
  } finally {
    await act(async () => root.unmount());
    assert.equal(timers.size, 0, 'progress timers must be cleared on unmount');
    webClient.request = originalRequest;
    setTeamOrganizationUiEnabled(false);
    useChatStore.setState({ activeSessionId: null });
    globalThis.window = previousWindow;
    globalThis.document = previousDocument;
    delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    performance.now = originalNow;
    for (const [key, value] of Object.entries(previousGlobals)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
    dom.window.close();
  }
}

test('board status counts match visible cards while completion excludes application tasks', async () => {
  await withPanel(async ({ render, click }) => {
    const teamTasks = [task('team-running', 'in_progress'), task('team-done', 'completed')];
    const tasks = [...teamTasks, task('application:running', 'in_progress'),
      task('application:done', 'completed'), task('application:waiting', 'pending'),
      task('application:failed', 'cancelled')];
    await render({ tasks, metricTasks: teamTasks, totalTasks: 2, completedTasks: 1 });
    await click('team-area-task-planning-view-board-button');
    for (const [column, expected] of Object.entries({ running: 2, completed: 2, waiting: 1, cancelled: 1 })) {
      const stat = document.querySelector('[data-testid="team-area-task-planning-column-stat"][data-variant="' + column + '"]');
      assert.equal(stat.querySelector('[data-testid="team-area-task-planning-column-count"]').textContent, String(expected));
      const board = document.querySelector('[data-testid="team-area-task-planning-board-column"][data-variant="' + column + '"]');
      assert.equal(board.querySelector('[data-testid="team-area-task-planning-board-column-task-list"]').children.length, expected);
    }
    assert.equal(document.querySelector('[data-variant="progress"] [data-testid="team-area-task-planning-column-count"]').textContent, '50%');
    assert.equal(byTestId('team-area-task-planning-metric-scope').textContent, i18n.t('team.planning.teamBoardMetricScope'));
  });
});

test('missing workflow counters show unavailable rather than loading or a partial total', async () => {
  await withPanel(async ({ render, sessionId }) => {
    const store = useSessionStore.getState();
    store.applyWorkflowUpdate(sessionId, {
      id: 'valid', name: 'Valid', status: 'completed', agent_count: 4, completed_agent_count: 4,
    });
    store.applyWorkflowUpdate(sessionId, { id: 'legacy', name: 'Legacy', status: 'failed' });
    await render();
    assert.equal(byTestId('team-area-workflow-agent-progress').textContent, i18n.t('team.planning.agentProgressUnavailable'));
    await act(async () => store.applyWorkflowUpdate(sessionId, {
      id: 'legacy', agent_count: 2, completed_agent_count: 1,
    }));
    assert.equal(byTestId('team-area-workflow-agent-progress').textContent, 'Loaded workflow nodes: 5 / 6 ended');
  });
});

test('phase summaries load through the base session and update the matching runtime', async () => {
  await withPanel(async ({ render, sessionId, requests }) => {
    const phaseTask = { ...task('run:phase', 'in_progress'), workflow_run_id: 'run' };
    const store = useSessionStore.getState();
    store.applyWorkflowUpdate(sessionId, {
      id: 'run', name: 'Restored workflow', status: 'running', agent_count: 1, completed_agent_count: 0,
      phases: [{ id: 'phase', name: 'Restored phase', status: 'running', agent_count: 1, completed_agent_count: 0 }],
    });
    webClient.request = async (method, params) => {
      requests.push({ method, params });
      return { phase: {
        id: 'phase', name: 'Restored phase', status: 'running', agent_count: 1, completed_agent_count: 0,
        agents: [{ id: 'worker', name: 'Loaded worker', status: 'running' }],
      } };
    };
    await render({ variant: 'compact', tasks: [phaseTask], totalTasks: 1 });
    assert.ok(requests.length > 0);
    assert.ok(requests.every(({ method, params }) =>
      method === 'command.workflows' && params.session_id === sessionId &&
      params.workflow_id === 'run' && params.phase_id === 'phase'));
    assert.equal(store.getRuntime(sessionId).workflowRuns[0].phases[0].agents[0].name, 'Loaded worker');
    assert.match(document.body.textContent, /0 \/ 1 ended/);
  });
});

test('switching from an owner workflow to an expert shows its task list without owner requests', async () => {
  await withPanel(async ({ root, sessionId, click, select, requests }) => {
    const store = useSessionStore.getState();
    store.applyWorkflowUpdate(sessionId, {
      id: 'owner-run', name: 'Owner workflow', status: 'completed', agent_count: 0, completed_agent_count: 0,
      phases: [],
    });
    store.ensureRuntime(sessionId + '::expert');
    store.upsertTeamTask(sessionId + '::expert', task('expert-task', 'pending'));
    function ConnectedPanel() {
      const { teamTasks } = useTaskPlanningMetrics();
      return createElement(TaskPlanningPanel, {
        variant: 'expanded', tasks: teamTasks, metricTasks: teamTasks, members: [],
        totalTasks: teamTasks.length, completedTasks: 0,
      });
    }
    await act(async () => root.render(createElement(ConnectedPanel)));
    await click('team-area-task-planning-view-workflow-button');
    assert.ok(byTestId('team-area-task-planning-workflow-view'));
    requests.length = 0;
    await select('expert');
    assert.ok(byTestId('team-area-task-planning-list-view'));
    assert.match(document.body.textContent, /expert-task/);
    assert.doesNotMatch(document.body.textContent, /Owner workflow/);
    assert.equal(byTestId('team-area-task-planning-view-workflow-button'), null);
    assert.deepEqual(requests, []);
    await select('owner');
    assert.ok(byTestId('team-area-task-planning-workflow-view'));
  });
});

test('compact progress animation does not rerender task rows on every frame', async () => {
  await withPanel(async ({ render, advanceFrame, frames }) => {
    let rowRenders = 0;
    const props = {
      variant: 'compact', totalTasks: 2,
      renderTaskIcon: () => { rowRenders += 1; return null; },
    };
    await render({ ...props, tasks: [task('one', 'in_progress'), task('two', 'pending')] });
    await render({ ...props, completedTasks: 1, tasks: [task('one', 'completed'), task('two', 'pending')] });
    const beforeFrames = rowRenders;
    assert.ok(frames.size > 0);
    await advanceFrame(performance.now() + 250);
    const width = parseFloat(byTestId('team-area-task-planning-progress-fill').style.width);
    assert.ok(width > 0 && width < 50);
    assert.equal(rowRenders, beforeFrames);
    await advanceFrame(performance.now() + 1000);
    assert.equal(byTestId('team-area-task-planning-progress-fill').style.width, '50%');
    assert.equal(rowRenders, beforeFrames);
    assert.equal(frames.size, 0);
  });
});

const progressWidth = () => parseFloat(byTestId('team-area-task-planning-progress-fill').style.width);

for (const variant of ['compact', 'expanded']) {
  test(variant + ' single task advances during execution, freezes on pause and reaches 100 only on completion', async () => {
    await withPanel(async ({ render, advanceSeconds, advanceFrame, setProcessing, timers }) => {
      const props = { variant, totalTasks: 1, completedTasks: 0 };
      await render({ ...props, tasks: [task('single', 'pending')] });
      await advanceSeconds(5);
      assert.equal(progressWidth(), 0);
      await setProcessing(true);
      await render({ ...props, tasks: [task('single', 'in_progress')] });
      await advanceSeconds(5);
      assert.ok(progressWidth() > 0 && progressWidth() < 100);
      assert.match(document.body.textContent, /Execution progress \(estimated\)/);
      if (variant === 'compact') {
        assert.equal(byTestId('team-area-task-planning-completed-count').textContent, '0');
      }
      await setProcessing(true, true);
      const paused = progressWidth();
      await advanceSeconds(10);
      assert.equal(progressWidth(), paused);
      assert.equal(timers.size, 0);
      assert.doesNotMatch(byTestId('team-area-task-planning-progress-track').className, /animate-pulse/);
      await setProcessing(true);
      await advanceSeconds(100);
      assert.equal(progressWidth(), 90);
      assert.equal(timers.size, 0);
      await render({ ...props, tasks: [task('single', 'completed')], completedTasks: 1 });
      await advanceFrame(performance.now() + 250);
      assert.ok(progressWidth() > 90 && progressWidth() < 100);
      await advanceFrame(performance.now() + 500);
      assert.equal(progressWidth(), 100);
      assert.doesNotMatch(document.body.textContent, /Execution progress \(estimated\)/);
    });
  });
}

for (const view of ['compact', 'list', 'board', 'workflow']) {
  test(view + ' workflow progress reflects 16/17 ended nodes before the phase task completes', async () => {
    await withPanel(async ({ render, sessionId, click, advanceFrame, timers }) => {
      const store = useSessionStore.getState();
      const phaseTask = { ...task('issue-run:phase', 'in_progress'), workflow_run_id: 'issue-run' };
      store.applyWorkflowUpdate(sessionId, {
        id: 'issue-run', name: 'Issue workflow', status: 'running', agent_count: 17, completed_agent_count: 16,
        phases: [{ id: 'phase', name: 'Phase', status: 'running', agents: [] }],
      });
      await render({ variant: view === 'compact' ? 'compact' : 'expanded', tasks: [phaseTask], totalTasks: 1 });
      if (view === 'board' || view === 'workflow') await click('team-area-task-planning-view-' + view + '-button');
      assert.ok(Math.abs(progressWidth() - 1600 / 17) < 0.001);
      assert.match(document.body.textContent, /16 \/ 17 ended/);
      assert.equal(timers.size, 0, 'workflow counters must not be replaced by time estimates');
      await act(async () => store.applyWorkflowUpdate(sessionId, { id: 'issue-run', status: 'paused' }));
      assert.doesNotMatch(byTestId('team-area-task-planning-progress-track').className, /animate-pulse/);
      await act(async () => store.applyWorkflowUpdate(sessionId, {
        id: 'issue-run', status: 'failed', completed_agent_count: 17,
      }));
      await advanceFrame(performance.now() + 500);
      assert.equal(progressWidth(), 99, 'ended failed nodes must not imply successful completion');
      await act(async () => store.applyWorkflowUpdate(sessionId, { id: 'issue-run', status: 'completed' }));
      await advanceFrame(performance.now() + 500);
      assert.equal(progressWidth(), 100);
    });
  });
}

test('stopping a session or cancelling a task clears its estimate timer', async () => {
  await withPanel(async ({ render, advanceSeconds, advanceFrame, setProcessing, timers, frames }) => {
    await setProcessing(true);
    await render({ variant: 'compact', tasks: [task('single', 'in_progress')], totalTasks: 1 });
    await advanceSeconds(3);
    const beforeStop = progressWidth();
    await setProcessing(false);
    await advanceSeconds(5);
    assert.equal(progressWidth(), beforeStop);
    assert.equal(timers.size, 0);
    await render({ variant: 'compact', tasks: [task('single', 'cancelled')], totalTasks: 1 });
    await advanceFrame(performance.now() + 500);
    assert.equal(progressWidth(), 0);
    assert.equal(timers.size, 0);
    assert.equal(frames.size, 0);
  });
});

test('switching sessions resets estimates and cancels the old session animation', async () => {
  await withPanel(async ({ render, advanceSeconds, setProcessing, timers, frames }) => {
    await setProcessing(true);
    await render({ tasks: [task('single', 'in_progress')], totalTasks: 1 });
    await advanceSeconds(4);
    assert.ok(progressWidth() > 0);
    await act(async () => {
      useChatStore.getState().ensureRuntime('other-session');
      useSessionStore.getState().ensureRuntime('other-session');
      useChatStore.setState({ activeSessionId: 'other-session' });
    });
    assert.equal(progressWidth(), 0);
    assert.equal(timers.size, 0);
    assert.equal(frames.size, 0);
  });
});

test('reduced motion updates progress without scheduling animation frames', async () => {
  await withPanel(async ({ render, setProcessing, advanceSeconds, frames }) => {
    await setProcessing(true);
    await render({ tasks: [task('single', 'in_progress')], totalTasks: 1 });
    await advanceSeconds(3);
    assert.ok(progressWidth() > 0 && progressWidth() < 100);
    assert.equal(frames.size, 0);
    await render({ tasks: [task('single', 'completed')], totalTasks: 1, completedTasks: 1 });
    assert.equal(progressWidth(), 100);
    assert.equal(frames.size, 0);
  }, { reducedMotion: true });
});

test('an active session with a pending task shows bounded preparation progress and resets after cancellation', async () => {
  await withPanel(async ({ render, setProcessing, advanceSeconds, advanceFrame, timers }) => {
    await setProcessing(true);
    await render({ variant: 'compact', tasks: [task('preparing', 'pending')], totalTasks: 1 });
    await advanceSeconds(20);
    assert.equal(progressWidth(), 10);
    assert.equal(byTestId('team-area-task-planning-completed-count').textContent, '0');
    assert.equal(timers.size, 0);
    await render({ variant: 'compact', tasks: [task('preparing', 'cancelled')], totalTasks: 1 });
    await advanceFrame(performance.now() + 500);
    assert.equal(progressWidth(), 0);
    await render({ variant: 'compact', tasks: [task('preparing', 'in_progress')], totalTasks: 1 });
    assert.equal(progressWidth(), 0);
    await advanceSeconds(1);
    assert.equal(progressWidth(), 1);
  });
});

test('planning and review gates retain estimates and remain visibly active', async () => {
  await withPanel(async ({ render, setProcessing, advanceSeconds, timers }) => {
    await setProcessing(true);
    await render({ tasks: [task('gated', 'in_progress')], totalTasks: 1 });
    await advanceSeconds(30);
    let previous = progressWidth();
    for (const status of ['planning', 'in_review', 'in_progress']) {
      await render({ tasks: [task('gated', status)], totalTasks: 1 });
      assert.equal(progressWidth(), previous);
      assert.equal(timers.size, 1);
      assert.match(byTestId('team-area-task-planning-progress-track').className, /animate-pulse/);
      await advanceSeconds(2);
      assert.ok(progressWidth() > previous);
      previous = progressWidth();
    }
  });
});

test('view switches, compact layout and remounts share the session estimate', async () => {
  await withPanel(async ({ root, render, click, setProcessing, advanceSeconds, timers, frames }) => {
    const props = { tasks: [task('ordinary', 'in_progress')], totalTasks: 1 };
    await setProcessing(true);
    await render(props);
    await advanceSeconds(30);
    const before = progressWidth();
    await click('team-area-task-planning-view-board-button');
    assert.equal(progressWidth(), before);
    await click('team-area-task-planning-view-list-button');
    assert.equal(progressWidth(), before);
    await render({ ...props, variant: 'compact' });
    assert.equal(progressWidth(), before);
    await act(async () => root.render(null));
    assert.equal(timers.size, 0);
    assert.equal(frames.size, 0);
    await render(props);
    assert.equal(progressWidth(), before);
    assert.equal(timers.size, 1);
    await advanceSeconds(2);
    assert.ok(progressWidth() > before);
  });
});

test('adding a workflow preserves ordinary-task progress when the list layout changes', async () => {
  await withPanel(async ({ render, sessionId, setProcessing, advanceSeconds, advanceFrame }) => {
    await setProcessing(true);
    await render({ tasks: [task('ordinary', 'in_progress')], totalTasks: 1 });
    await advanceSeconds(30);
    const before = progressWidth();
    await act(async () => useSessionStore.getState().applyWorkflowUpdate(sessionId, {
      id: 'new-run', name: 'New workflow', status: 'running', agent_count: 1, completed_agent_count: 0,
      phases: [],
    }));
    assert.ok(Math.abs(progressWidth() - before / 2) < 0.001, 'the new work item changes the denominator only');
    await advanceSeconds(2);
    await advanceFrame(performance.now() + 500);
    assert.ok(progressWidth() > before / 2);
  });
});

test('owner, expert and other-session estimates stay isolated and survive switching back', async () => {
  await withPanel(async ({ render, select, sessionId, setProcessing, advanceSeconds }) => {
    const props = { tasks: [task('shared-id', 'in_progress')], totalTasks: 1 };
    await setProcessing(true);
    await render(props);
    await advanceSeconds(30);
    const ownerProgress = progressWidth();
    await act(async () => {
      useSessionStore.getState().ensureRuntime(sessionId + '::expert');
      useChatStore.getState().ensureTeamRuntime(sessionId, 'expert');
      useChatStore.getState().setProcessing(sessionId + '::expert', true);
    });
    await select('expert');
    assert.equal(progressWidth(), 0);
    await advanceSeconds(5);
    const expertProgress = progressWidth();
    assert.ok(expertProgress > 0 && expertProgress < ownerProgress);
    await select('owner');
    assert.equal(progressWidth(), ownerProgress);
    await select('expert');
    assert.equal(progressWidth(), expertProgress);
    await act(async () => {
      useChatStore.getState().ensureRuntime('other-session');
      useSessionStore.getState().ensureRuntime('other-session');
      useChatStore.setState({ activeSessionId: 'other-session' });
    });
    assert.equal(progressWidth(), 0);
    await act(async () => useChatStore.setState({ activeSessionId: sessionId }));
    assert.equal(progressWidth(), expertProgress);
  });
});

for (const action of ['upsertTeamTask', 'updateTeamTask', 'setTeamTasks']) {
  test(action + ' clears terminal estimates even while the panel is unmounted', async () => {
    await withPanel(async ({ root, render, sessionId, setProcessing, advanceSeconds }) => {
      const store = useSessionStore.getState();
      const running = task('retry', 'in_progress');
      store.upsertTeamTask(sessionId, running);
      await setProcessing(true);
      await render({ tasks: [running], totalTasks: 1 });
      await advanceSeconds(30);
      assert.ok(progressWidth() > 0);
      await act(async () => root.render(null));
      for (const status of ['completed', 'cancelled']) {
        const terminal = { ...running, status };
        if (action === 'updateTeamTask') store[action](sessionId, running.task_id, terminal);
        else if (action === 'setTeamTasks') store[action](sessionId, [terminal]);
        else store[action](sessionId, terminal);
        assert.equal(store.getRuntime(sessionId).taskProgressEstimates.retry, undefined);
        store.upsertTeamTask(sessionId, running);
        await render({ tasks: [running], totalTasks: 1 });
        assert.equal(progressWidth(), 0);
        await advanceSeconds(2);
        await act(async () => root.render(null));
      }
      store.removeRuntime(sessionId);
      store.ensureRuntime(sessionId);
      assert.deepEqual(store.getRuntime(sessionId).taskProgressEstimates, {});
    });
  });
}

test('compact phase rows show a short count with localized accessible context', async () => {
  try {
    for (const language of ['en', 'zh']) {
      await i18n.changeLanguage(language);
      await withPanel(async ({ render, sessionId }) => {
        useSessionStore.getState().applyWorkflowUpdate(sessionId, {
          id: 'run', name: 'Workflow', status: 'running', agent_count: 17, completed_agent_count: 16,
          phases: [{ id: 'phase', name: 'Phase', status: 'running', agent_count: 17, completed_agent_count: 16, agents: [] }],
        });
        await render({ variant: 'compact', tasks: [{ ...task('run:phase', 'in_progress'), workflow_run_id: 'run' }], totalTasks: 1 });
        const count = byTestId('team-area-task-agent-progress');
        assert.equal(count.textContent, '16/17');
        const context = i18n.t('team.planning.phaseAgentProgress', { ended: 16, total: 17 });
        assert.equal(count.title, context);
        assert.equal(count.getAttribute('aria-label'), context);
      });
    }
  } finally {
    await i18n.changeLanguage('en');
  }
});

test('task snapshots clear removed estimates without clearing application progress', async () => {
  await withPanel(async ({ sessionId }) => {
    const store = useSessionStore.getState();
    store.setTeamTasks(sessionId, [task('removed', 'in_progress'), task('reviewing', 'in_review')]);
    store.setTaskProgressEstimates(sessionId, { removed: 0.3, reviewing: 0.4, 'application:job': 0.2 });
    store.upsertTeamTask(sessionId, { task_id: 'reviewing', content: 'Updated review details' });
    assert.equal(store.getRuntime(sessionId).taskProgressEstimates.reviewing, 0.4);
    store.setTeamTasks(sessionId, [task('reviewing', 'in_review')]);
    assert.deepEqual(store.getRuntime(sessionId).taskProgressEstimates, { reviewing: 0.4, 'application:job': 0.2 });
    store.setTeamTasks(sessionId, []);
    assert.deepEqual(store.getRuntime(sessionId).taskProgressEstimates, { 'application:job': 0.2 });
  });
});
