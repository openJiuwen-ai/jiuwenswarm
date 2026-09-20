import assert from 'node:assert/strict';
import test from 'node:test';
import { buildSync } from 'esbuild';

const { outputFiles } = buildSync({
  entryPoints: ['src/applicationPlugins/taskProgressStore.ts'],
  bundle: true,
  platform: 'node',
  format: 'esm',
  write: false,
});
const { useApplicationTaskStore: store, applicationTasksToTeamTasks: display } = await import(
  `data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`
);
const task = (id, status = 'queued', sequence = 1) => ({
  id,
  pluginId: 'video-duplex',
  title: `Request ${id}`,
  status,
  sequence,
  detail: 'Latest step',
  createdAt: 100,
});
const labels = { queued: 'Queued', running: 'Running', completed: 'Completed', failed: 'Failed' };

test('queue is isolated by session and plugin, and replay does not duplicate jobs', () => {
  store.setState({ sessions: {} });
  store.getState().upsert('a', task('1'));
  store.getState().upsert('a', task('1', 'running', 2));
  store.getState().upsert('b', task('1'));
  store.getState().upsert('a', { ...task('1'), pluginId: 'another' });
  assert.equal(store.getState().sessions.a.length, 2);
  assert.equal(store.getState().sessions.a[0].status, 'running');
  assert.equal(store.getState().sessions.b[0].status, 'queued');
});

test('late acknowledgements and polling cannot regress execution or terminal status', () => {
  store.setState({ sessions: {} });
  for (const update of [task('1', 'running', 2), task('1'), task('1', 'queued', 2)]) {
    store.getState().upsert('a', update);
  }
  assert.equal(store.getState().sessions.a[0].status, 'running');
  store.getState().upsert('a', task('1', 'completed', 5));
  store.getState().upsert('a', task('1', 'running', 6));
  store.getState().upsert('a', task('1', 'failed', 3));
  assert.equal(store.getState().sessions.a[0].status, 'completed');
});

test('plan survives incremental updates and uses the existing planning item format', () => {
  store.setState({ sessions: {} });
  const steps = [{ id: 's', content: 'Read document', status: 'completed' }];
  store.getState().upsert('a', { ...task('1'), steps });
  store.getState().upsert('a', { ...task('1', 'running', 2), createdAt: 200 });
  const [saved] = store.getState().sessions.a;
  assert.deepEqual(saved.steps, steps);
  assert.equal(saved.createdAt, 100);
  const result = display([task('done', 'completed'), task('fail', 'failed'), saved, task('wait')], labels);
  assert.deepEqual(
    result.map((item) => item.status),
    ['in_progress', 'pending', 'cancelled', 'completed'],
  );
  assert.match(result[0].content, /✓ Read document/);
  assert.match(result[1].title, /Queued/);
  assert.match(result[2].title, /Failed/);
});
