import assert from 'node:assert/strict';
import test, { beforeEach } from 'node:test';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: false,
  cacheDir: 'node_modules/.cache/workspace-store/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
  define: { 'import.meta.env.DEV': false },
});
let modules;
try {
  modules = await Promise.all([
    vite.ssrLoadModule('/src/stores/workspaceStore.ts'),
    vite.ssrLoadModule('/src/features/workspace/projectRegistryClient.ts'),
    vite.ssrLoadModule('/src/stores/cronStore.ts'),
    vite.ssrLoadModule('/src/stores/sessionStore.ts'),
  ]);
} finally {
  await vite.close();
}
const [{ useWorkspaceStore }, { projectRegistryClient }, { useCronStore }, { useSessionStore }] = modules;
const initialState = useWorkspaceStore.getState();
const initialSessionState = useSessionStore.getState();
const initialCronState = useCronStore.getState();
const project = {
  project_id: 'new-project',
  name: 'New project',
  project_dir: '/workspace/new-project',
  work_mode: 'code',
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

beforeEach((t) => {
  useWorkspaceStore.setState({ ...initialState, workMode: 'code' }, true);
  useSessionStore.setState(initialSessionState, true);
  useCronStore.setState(initialCronState, true);
  t.mock.method(projectRegistryClient, 'pinnedSessions', async () => ({ sessions: [] }));
  t.mock.method(useCronStore.getState(), 'loadJobs', async () => {});
});

test('a late pre-creation list cannot clear the newly created project selection', async (t) => {
  const oldList = deferred();
  let calls = 0;
  t.mock.method(projectRegistryClient, 'list', () => (
    calls++ === 0 ? oldList.promise : Promise.resolve({ projects: [project] })
  ));
  t.mock.method(projectRegistryClient, 'create', async () => ({ project_id: project.project_id }));

  const pending = useWorkspaceStore.getState().loadProjects();
  const created = await useWorkspaceStore.getState().createProject(project.name, project.project_dir);
  assert.equal(created.project_id, project.project_id);
  assert.equal(useWorkspaceStore.getState().selectedProject?.project_id, project.project_id);

  oldList.resolve({ projects: [] });
  await pending;
  assert.deepEqual(useWorkspaceStore.getState().projects.map((item) => item.project_id), [project.project_id]);
  assert.equal(useWorkspaceStore.getState().selectedProject?.project_id, project.project_id);
});

test('an older list failure cannot overwrite a newer successful refresh', async (t) => {
  const oldList = deferred();
  let calls = 0;
  t.mock.method(projectRegistryClient, 'list', () => (
    calls++ === 0 ? oldList.promise : Promise.resolve({ projects: [project] })
  ));
  const pending = useWorkspaceStore.getState().loadProjects();
  await useWorkspaceStore.getState().loadProjects();
  oldList.reject(new Error('old request failed'));
  await pending;
  assert.equal(useWorkspaceStore.getState().error, null);
  assert.equal(useWorkspaceStore.getState().isLoadingProjects, false);
  assert.equal(useWorkspaceStore.getState().projects[0]?.project_id, project.project_id);
});

for (const outcome of ['success', 'failure']) {
  test(`an older ${outcome} does not end loading while a newer list is pending`, async (t) => {
    const oldList = deferred();
    const newList = deferred();
    let calls = 0;
    t.mock.method(projectRegistryClient, 'list', () => (++calls === 1 ? oldList.promise : newList.promise));
    const first = useWorkspaceStore.getState().loadProjects();
    const second = useWorkspaceStore.getState().loadProjects();
    if (outcome === 'success') oldList.resolve({ projects: [] });
    else oldList.reject(new Error('old request failed'));
    await first;
    assert.equal(useWorkspaceStore.getState().isLoadingProjects, true);
    assert.equal(useWorkspaceStore.getState().error, null);
    newList.resolve({ projects: [project] });
    assert.equal(await second, true);
    assert.equal(useWorkspaceStore.getState().isLoadingProjects, false);
  });
}

test('the current request still reports failure and ends loading', async (t) => {
  t.mock.method(projectRegistryClient, 'list', async () => { throw new Error('current request failed'); });
  assert.equal(await useWorkspaceStore.getState().loadProjects(), false);
  assert.equal(useWorkspaceStore.getState().error, 'current request failed');
  assert.equal(useWorkspaceStore.getState().isLoadingProjects, false);
});

for (const removesCurrent of [true, false]) {
  test(`removing sessions ${removesCurrent ? 'leaves an archived' : 'keeps an unrelated'} chat route`, (t) => {
    const removed = { session_id: 'archived-session', project_id: project.project_id };
    const kept = { session_id: 'kept-session', project_id: project.project_id };
    const current = removesCurrent ? removed : kept;
    const navigations = [];
    const events = [];
    const previousWindow = globalThis.window;
    const previousPopStateEvent = globalThis.PopStateEvent;
    globalThis.window = {
      location: { pathname: `/chat/${current.session_id}` },
      history: { replaceState: (_state, _title, path) => navigations.push(path) },
      dispatchEvent: (event) => events.push(event.type),
    };
    globalThis.PopStateEvent = class extends Event {};
    t.after(() => {
      if (previousWindow === undefined) delete globalThis.window;
      else globalThis.window = previousWindow;
      if (previousPopStateEvent === undefined) delete globalThis.PopStateEvent;
      else globalThis.PopStateEvent = previousPopStateEvent;
    });
    useWorkspaceStore.setState({
      projects: [project],
      projectSessions: { [project.project_id]: [removed, kept] },
      pinnedSessions: [removed],
    });
    useSessionStore.setState({ sessions: [removed, kept], currentSession: current });

    useWorkspaceStore.getState().removeSessions([removed.session_id]);

    assert.deepEqual(navigations, removesCurrent ? ['/chat/new'] : []);
    assert.deepEqual(events, removesCurrent ? ['popstate'] : []);
    assert.deepEqual(useWorkspaceStore.getState().projectSessions[project.project_id], [kept]);
    assert.deepEqual(useWorkspaceStore.getState().pinnedSessions, []);
    assert.deepEqual(useSessionStore.getState().sessions, [kept]);
    assert.equal(useSessionStore.getState().currentSession, removesCurrent ? null : kept);
  });
}

test('an authoritative list still clears selection for a removed project', async (t) => {
  useWorkspaceStore.setState({ projects: [project], selectedProject: project });
  t.mock.method(projectRegistryClient, 'list', async () => ({ projects: [] }));
  await useWorkspaceStore.getState().loadProjects();
  assert.deepEqual(useWorkspaceStore.getState().projects, []);
  assert.equal(useWorkspaceStore.getState().selectedProject, null);
});

test('switching away and back invalidates the first request for the same mode', async (t) => {
  const lists = [deferred(), deferred(), deferred()];
  let calls = 0;
  const listMock = t.mock.method(projectRegistryClient, 'list', () => lists[calls++].promise);
  const originalCode = useWorkspaceStore.getState().loadProjects();
  const work = useWorkspaceStore.getState().setWorkMode('work');
  const code = useWorkspaceStore.getState().setWorkMode('code');
  lists[0].resolve({ projects: [project] });
  lists[1].resolve({ projects: [] });
  await Promise.all([originalCode, work]);
  assert.deepEqual(useWorkspaceStore.getState().projects, []);
  assert.equal(useWorkspaceStore.getState().isLoadingProjects, true);
  lists[2].resolve({ projects: [project] });
  await code;
  assert.deepEqual(listMock.mock.calls.map((call) => call.arguments[1]), ['code', 'work', 'code']);
  assert.equal(useWorkspaceStore.getState().projects[0]?.project_id, project.project_id);
});

test('createProject can finish its refresh while a later list request is still pending', async (t) => {
  const createdList = deferred();
  const laterList = deferred();
  const refreshStarted = deferred();
  let calls = 0;
  t.mock.method(projectRegistryClient, 'create', async () => ({ project_id: project.project_id }));
  t.mock.method(projectRegistryClient, 'list', () => {
    if (++calls === 1) {
      refreshStarted.resolve();
      return createdList.promise;
    }
    return laterList.promise;
  });
  const creation = useWorkspaceStore.getState().createProject(project.name, project.project_dir);
  await refreshStarted.promise;
  const later = useWorkspaceStore.getState().loadProjects();
  createdList.resolve({ projects: [project] });
  const created = await creation;
  assert.equal(created.project_id, project.project_id);
  assert.equal(useWorkspaceStore.getState().selectedProject?.project_id, project.project_id);
  assert.equal(useWorkspaceStore.getState().isLoadingProjects, true);
  laterList.resolve({ projects: [project] });
  await later;
  assert.equal(useWorkspaceStore.getState().selectedProject?.project_id, project.project_id);
  assert.equal(useWorkspaceStore.getState().isLoadingProjects, false);
});

function sessionRows(count, prefix = 'session') {
  return Array.from({ length: count }, (_, index) => ({
    session_id: `${prefix}-${index}`, title: `${prefix}-${index}`, work_mode: 'code',
    project_id: project.project_id, created_at: '', updated_at: '', last_user_message_at: 1,
  }));
}

for (const outcome of ['success', 'failure']) {
  test(`a late project session ${outcome} cannot undo load more`, async (t) => {
    const older = deferred();
    let calls = 0;
    t.mock.method(projectRegistryClient, 'getSessions', () => (++calls === 1
      ? older.promise : Promise.resolve({ sessions: sessionRows(20), total: 20 })));
    useWorkspaceStore.setState({
      projects: [project], projectSessions: { [project.project_id]: sessionRows(10) },
      sessionVisibility: { [project.project_id]: { visibleCount: 10 } },
    });
    const refresh = useWorkspaceStore.getState().loadProjectSessions(project.project_id, 10);
    await useWorkspaceStore.getState().showMoreSessions(project.project_id);
    if (outcome === 'success') older.resolve({ sessions: sessionRows(10), total: 20 });
    else older.reject(new Error('older session query failed'));
    await refresh;
    const state = useWorkspaceStore.getState();
    assert.equal(state.projectSessions[project.project_id].length, 20);
    assert.equal(state.sessionVisibility[project.project_id].visibleCount, 20);
    assert.equal(state.projectSessionTotals[project.project_id], 20);
    assert.equal(state.error, null);
  });

  test(`a late pinned session ${outcome} cannot replace a newer list`, async (t) => {
    const older = deferred();
    let calls = 0;
    const pinned = { ...sessionRows(1, 'new-pin')[0], pinned: true };
    t.mock.method(projectRegistryClient, 'pinnedSessions', () => (++calls === 1
      ? older.promise : Promise.resolve({ sessions: [pinned] })));
    const first = useWorkspaceStore.getState().loadPinnedSessions();
    await useWorkspaceStore.getState().loadPinnedSessions();
    if (outcome === 'success') older.resolve({ sessions: [] });
    else older.reject(new Error('older pinned query failed'));
    await first;
    assert.deepEqual(useWorkspaceStore.getState().pinnedSessions.map(row => row.session_id), [pinned.session_id]);
    assert.equal(useWorkspaceStore.getState().error, null);
  });

  test(`an older cron ${outcome} cannot finish a newer loading indicator`, async (t) => {
    const older = deferred();
    const newer = deferred();
    let calls = 0;
    t.mock.method(projectRegistryClient, 'getCronSessions', () => (++calls === 1 ? older.promise : newer.promise));
    const first = useCronStore.getState().loadCronSessions(project.project_id, 'job');
    const second = useCronStore.getState().loadCronSessions(project.project_id, 'job');
    if (outcome === 'success') older.resolve({ sessions: sessionRows(1, 'old-run') });
    else older.reject(new Error('older cron query failed'));
    await first;
    const stillLoading = useCronStore.getState().cronSessionsLoading.job;
    newer.resolve({ sessions: sessionRows(2, 'new-run') });
    await second;
    assert.equal(stillLoading, true);
    assert.equal(useCronStore.getState().cronSessions.job.length, 2);
    assert.equal(useCronStore.getState().cronSessionsLoading.job, false);
  });

  test(`a late cron ${outcome} cannot overwrite a completed newer query`, async (t) => {
    const older = deferred();
    let calls = 0;
    t.mock.method(projectRegistryClient, 'getCronSessions', () => (++calls === 1
      ? older.promise : Promise.resolve({ sessions: sessionRows(2, 'new-run') })));
    const first = useCronStore.getState().loadCronSessions(project.project_id, 'job');
    await useCronStore.getState().loadCronSessions(project.project_id, 'job');
    if (outcome === 'success') older.resolve({ sessions: sessionRows(1, 'old-run') });
    else older.reject(new Error('older cron query failed'));
    await first;
    assert.deepEqual(useCronStore.getState().cronSessions.job.map(row => row.session_id), ['new-run-0', 'new-run-1']);
    assert.equal(useCronStore.getState().cronSessionsLoading.job, false);
  });
}

test('project and cron session queries remain independent across resources', async (t) => {
  const projectA = deferred();
  const cronA = deferred();
  t.mock.method(projectRegistryClient, 'getSessions', (id) => id === 'a'
    ? projectA.promise : Promise.resolve({ sessions: sessionRows(1, 'b'), total: 1 }));
  t.mock.method(projectRegistryClient, 'getCronSessions', (_id, job) => job === 'a'
    ? cronA.promise : Promise.resolve({ sessions: sessionRows(1, 'b') }));
  const firstProject = useWorkspaceStore.getState().loadProjectSessions('a', 10);
  const firstCron = useCronStore.getState().loadCronSessions(project.project_id, 'a');
  await useWorkspaceStore.getState().loadProjectSessions('b', 10);
  await useCronStore.getState().loadCronSessions(project.project_id, 'b');
  projectA.resolve({ sessions: sessionRows(1, 'a'), total: 1 });
  cronA.resolve({ sessions: sessionRows(1, 'a') });
  await Promise.all([firstProject, firstCron]);
  assert.equal(useWorkspaceStore.getState().projectSessions.a[0].session_id, 'a-0');
  assert.equal(useWorkspaceStore.getState().projectSessions.b[0].session_id, 'b-0');
  assert.equal(useCronStore.getState().cronSessions.a[0].session_id, 'a-0');
  assert.equal(useCronStore.getState().cronSessions.b[0].session_id, 'b-0');
});

for (const target of ['project', 'pinned']) {
  test(`${target} results from before a work-mode round trip stay invalid`, async (t) => {
    const older = deferred();
    t.mock.method(projectRegistryClient, 'list', async () => ({ projects: [project] }));
    let first;
    if (target === 'project') {
      t.mock.method(projectRegistryClient, 'getSessions', () => older.promise);
      first = useWorkspaceStore.getState().loadProjectSessions(project.project_id, 10);
    } else {
      let calls = 0;
      t.mock.method(projectRegistryClient, 'pinnedSessions', () => (++calls === 1
        ? older.promise : Promise.resolve({ sessions: [] })));
      first = useWorkspaceStore.getState().loadPinnedSessions();
    }
    await useWorkspaceStore.getState().setWorkMode('work');
    await useWorkspaceStore.getState().setWorkMode('code');
    older.resolve({ sessions: sessionRows(1, 'old-mode'), total: 1 });
    await first;
    assert.deepEqual(useWorkspaceStore.getState().projectSessions, {});
    assert.deepEqual(useWorkspaceStore.getState().pinnedSessions, []);
  });
}

test('a current project or pinned failure remains visible and cron keeps its last list', async (t) => {
  t.mock.method(console, 'error', () => {});
  t.mock.method(projectRegistryClient, 'getSessions', async () => { throw new Error('current project failure'); });
  await useWorkspaceStore.getState().loadProjectSessions(project.project_id, 10);
  assert.equal(useWorkspaceStore.getState().error, 'current project failure');
  t.mock.method(projectRegistryClient, 'pinnedSessions', async () => { throw new Error('current pinned failure'); });
  await useWorkspaceStore.getState().loadPinnedSessions();
  assert.equal(useWorkspaceStore.getState().error, 'current pinned failure');
  useCronStore.setState({ cronSessions: { job: sessionRows(1, 'retained') } });
  t.mock.method(projectRegistryClient, 'getCronSessions', async () => { throw new Error('current cron failure'); });
  await useCronStore.getState().loadCronSessions(project.project_id, 'job');
  assert.equal(useCronStore.getState().cronSessions.job[0].session_id, 'retained-0');
  assert.equal(useCronStore.getState().cronSessionsLoading.job, false);
});

test('a workspace refresh retains a pending load-more limit', async (t) => {
  const older = deferred();
  let calls = 0;
  const limits = [];
  t.mock.method(projectRegistryClient, 'list', async () => ({ projects: [{ ...project, is_default: true }] }));
  t.mock.method(projectRegistryClient, 'getSessions', (_id, limit) => {
    limits.push(limit);
    return ++calls === 1 ? older.promise : Promise.resolve({ sessions: sessionRows(limit), total: 30 });
  });
  useWorkspaceStore.setState({ projects: [project], projectSessions: { [project.project_id]: sessionRows(10) } });
  const more = useWorkspaceStore.getState().showMoreSessions(project.project_id);
  await useWorkspaceStore.getState().refreshWorkspaceData();
  older.resolve({ sessions: sessionRows(20), total: 30 });
  await more;
  assert.deepEqual(limits, [20, 20]);
  assert.equal(useWorkspaceStore.getState().projectSessions[project.project_id].length, 20);
  assert.equal(useWorkspaceStore.getState().sessionVisibility[project.project_id].visibleCount, 20);
});

for (const target of ['project', 'pinned']) {
  test(`a new workspace refresh invalidates an older direct ${target} request`, async (t) => {
    const older = deferred();
    const list = deferred();
    t.mock.method(projectRegistryClient, 'list', () => list.promise);
    let first;
    if (target === 'project') {
      t.mock.method(projectRegistryClient, 'getSessions', () => older.promise);
      first = useWorkspaceStore.getState().loadProjectSessions(project.project_id, 10);
    } else {
      let calls = 0;
      t.mock.method(projectRegistryClient, 'pinnedSessions', () => ++calls === 1
        ? older.promise : Promise.resolve({ sessions: [] }));
      first = useWorkspaceStore.getState().loadPinnedSessions();
    }
    const refresh = useWorkspaceStore.getState().refreshWorkspaceData();
    older.resolve({ sessions: sessionRows(1, 'older-snapshot'), total: 1 });
    await first;
    const projectRows = { ...useWorkspaceStore.getState().projectSessions };
    const pins = [...useWorkspaceStore.getState().pinnedSessions];
    list.resolve({ projects: [] });
    await refresh;
    assert.deepEqual(projectRows, {});
    assert.deepEqual(pins, []);
  });
}
