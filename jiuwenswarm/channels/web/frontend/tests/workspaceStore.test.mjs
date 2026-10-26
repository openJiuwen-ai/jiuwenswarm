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
