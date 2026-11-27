import assert from 'node:assert/strict';
import { test } from 'node:test';

const { SequenceGuard, applyToggleToSelection } = await import(
  '../node_modules/.cache/skill-panel-races/skillPanelGuards.mjs'
);

test('SequenceGuard: a late response from an earlier request is not current', () => {
  const guard = new SequenceGuard();
  const first = guard.begin();
  assert.equal(guard.isCurrent(first), true);
  const second = guard.begin();
  assert.equal(guard.isCurrent(first), false, '旧请求在 新请求开始后即过期');
  assert.equal(guard.isCurrent(second), true);
});

test('SequenceGuard: invalidate voids in-flight requests without starting a new one', () => {
  const guard = new SequenceGuard();
  const seq = guard.begin();
  guard.invalidate();
  assert.equal(guard.isCurrent(seq), false, '关闭详情后晚到的响应不得回写');
});

test('SequenceGuard: sequences from different guards do not interfere', () => {
  const detail = new SequenceGuard();
  const hub = new SequenceGuard();
  const a = detail.begin();
  hub.begin();
  assert.equal(detail.isCurrent(a), true, '另一个守卫的请求不影响本守卫的轮次');
});

test('applyToggleToSelection: toggling the currently viewed skill updates enabled in place', () => {
  const current = { name: 'writer', enabled: true, description: 'kept as-is' };
  const next = applyToggleToSelection(current, 'writer', false);
  assert.equal(next.name, 'writer');
  assert.equal(next.enabled, false);
  assert.equal(next.description, 'kept as-is');
  assert.notEqual(next, current, '返回新对象，不原地改写');
});

test('applyToggleToSelection: navigating to another skill during the await keeps the new view', () => {
  const currentView = { name: 'reader', enabled: true };
  const next = applyToggleToSelection(currentView, 'writer', false);
  assert.equal(next, currentView, '被切换的技能不是当前选中时原样返回，详情页不被拉回旧技能');
});

test('applyToggleToSelection: closed detail (null) stays closed', () => {
  assert.equal(applyToggleToSelection(null, 'writer', false), null);
});
