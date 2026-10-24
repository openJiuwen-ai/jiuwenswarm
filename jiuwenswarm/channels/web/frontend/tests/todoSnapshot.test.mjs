import assert from 'node:assert/strict';
import test from 'node:test';
import { useTodoStore } from '../node_modules/.cache/todo-snapshot/todoStore.mjs';
import { decideTodoSnapshot, parseTodoMeta, todoFileItems } from '../node_modules/.cache/todo-snapshot/todoSnapshot.mjs';

const meta = (revision, extra = {}) => ({ schema_version: 2, store_id: 'store', plan_id: 'plan', revision: String(revision), view: 'display', ...extra });
const item = (id, status = 'pending') => ({ id, content: id, activeForm: id, status });
const payload = (revision, todos, extra = {}) => ({ todos, todo_meta: meta(revision, extra) });
function start() {
  useTodoStore.setState({ runtimes: {} });
  useTodoStore.getState().ensureRuntime('s');
  return useTodoStore.getState();
}
const current = () => useTodoStore.getState().getRuntime('s');

test('reordered commits keep both completed items and accept an empty newest snapshot', () => {
  const store = start();
  store.applySnapshot('s', payload(12, [item('A', 'completed'), item('B', 'completed')]));
  store.applySnapshot('s', payload(11, [item('A', 'pending')]));
  assert.deepEqual(current().todos.map((t) => t.status), ['completed', 'completed']);
  assert.equal(store.applySnapshot('s', payload(13, [])), true);
  store.applySnapshot('s', payload(12, [item('A', 'completed')]));
  assert.deepEqual(current().todos, []);
  assert.equal(current().snapshotMeta.revision, '13');
});

test('malformed or unversioned events cannot clear an established snapshot', () => {
  const store = start();
  store.applySnapshot('s', payload(1, [item('A')]));
  store.applySnapshot('s', { todos: [] });
  store.applySnapshot('s', { todo_meta: meta(2) });
  store.setTodos('s', []);
  store.clearTodos('s');
  assert.equal(current().todos[0].id, 'A');
  assert.equal(current().needsSync, true);
});

test('same version with changed content requests recovery, while duplicates retain the timer', () => {
  const store = start();
  store.applySnapshot('s', payload(1, [item('A', 'in_progress')]));
  const timer = current().todos[0].updatedAt;
  store.applySnapshot('s', payload(1, [item('A', 'in_progress')]));
  assert.equal(current().todos[0].updatedAt, timer);
  store.applySnapshot('s', payload(1, [item('A', 'completed')]));
  assert.equal(current().todos[0].status, 'in_progress');
  assert.equal(current().needsSync, true);
});

test('local activity labels do not erase metadata or alter committed snapshot equality', () => {
  const store = start();
  store.applySnapshot('s', payload(2, [item('A', 'in_progress')]));
  store.setActiveForm('s', 'A', 'Running bash');
  store.applySnapshot('s', payload(2, [item('A', 'in_progress')]));
  assert.equal(current().snapshotMeta.revision, '2');
  assert.equal(current().todos[0].activeForm, 'Running bash');
  assert.equal(current().needsSync, false);
});

test('a higher version may explicitly reopen an item and a new plan does not inherit its timer', () => {
  const store = start();
  store.applySnapshot('s', payload(1, [item('A', 'completed')]));
  store.applySnapshot('s', payload(2, [item('A', 'pending')]));
  assert.equal(current().todos[0].status, 'pending');
  useTodoStore.setState({ runtimes: { s: { ...current(), todos: [{ ...item('A', 'in_progress'), updatedAt: 'old-plan-time' }] } } });
  store.applySnapshot('s', payload(3, [item('A', 'in_progress')], { plan_id: 'new-plan' }));
  assert.notEqual(current().todos[0].updatedAt, 'old-plan-time');
});

test('a different store is established only by the matching recovery response', () => {
  const store = start();
  store.applySnapshot('s', payload(8, [item('old')]));
  const next = payload(1, [item('new')], { store_id: 'new-store' });
  assert.equal(store.applySnapshot('s', next), false);
  const request = store.beginRecovery('s');
  assert.equal(store.beginRecovery('s'), undefined);
  assert.equal(store.applySnapshot('s', next, 'unrelated-history'), false);
  assert.equal(store.applySnapshot('s', next, request), true);
  store.finishRecovery('s', request);
  assert.equal(current().todos[0].id, 'new');
  store.applySnapshot('s', payload(99, [item('old')]));
  assert.equal(current().todos[0].id, 'new');
});

test('history cannot reset store identity if live state advanced since recovery began', () => {
  const store = start();
  store.applySnapshot('s', payload(8, [item('A')]));
  store.applySnapshot('s', { todos: [] });
  const request = store.beginRecovery('s');
  store.applySnapshot('s', payload(9, [item('B')]));
  store.applySnapshot('s', payload(1, [item('wrong')], { store_id: 'different' }), request);
  assert.equal(current().todos[0].id, 'B');
});

test('recovery is bounded and one session cannot overwrite another', () => {
  const store = start();
  store.ensureRuntime('other');
  store.applySnapshot('other', payload(1, [item('other')]));
  store.applySnapshot('s', { broken: true });
  for (let i = 0; i < 3; i++) {
    const request = store.beginRecovery('s');
    assert.ok(request);
    store.finishRecovery('s', request);
  }
  assert.equal(store.beginRecovery('s'), undefined);
  assert.equal(useTodoStore.getState().getRuntime('other').todos[0].id, 'other');
});

test('revision comparison stays exact beyond Number precision and file preview reads both formats', () => {
  assert.equal(decideTodoSnapshot(meta('9007199254740993'), meta('9007199254740992'), 'a', 'b'), 'stale');
  assert.equal(parseTodoMeta(meta('01')), undefined);
  assert.deepEqual(todoFileItems([{ id: 'A' }]), [{ id: 'A' }]);
  assert.deepEqual(todoFileItems({ schema_version: 2, todos: [] }), []);
  assert.equal(todoFileItems({ todos: [] }), undefined);
});
