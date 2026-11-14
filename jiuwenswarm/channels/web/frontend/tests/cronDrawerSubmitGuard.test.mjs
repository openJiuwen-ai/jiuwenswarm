import assert from 'node:assert/strict';
import { test } from 'node:test';

// Requires build cache populated by `npm run test:cron-drawer-guard`; fresh clones must run that first or this test fails with MODULE_NOT_FOUND.
import { createSubmitGuard, deriveDrawerCanSubmit } from '../node_modules/.cache/cron-drawer-guard/components/CronPanel/drawerSubmitGuard.js';

// ── canSubmit 推导：issue #6561 的"提交中禁用确认按钮"是防重复提交的核心条件，
//    纯函数化后在这里锁定，防止后续改动把 submitting 条件弄丢 ──

test('deriveDrawerCanSubmit: 必填项齐全且未过期时可用', () => {
  assert.equal(deriveDrawerCanSubmit([], false, false), true);
});

test('deriveDrawerCanSubmit: 有缺失字段时不可用', () => {
  assert.equal(deriveDrawerCanSubmit(['任务名称'], false, false), false);
});

test('deriveDrawerCanSubmit: 执行计划已过期时不可用', () => {
  assert.equal(deriveDrawerCanSubmit([], true, false), false);
});

test('deriveDrawerCanSubmit: 提交在途时不可用（防重复提交核心条件）', () => {
  assert.equal(deriveDrawerCanSubmit([], false, true), false);
});

test('deriveDrawerCanSubmit: 表单完整优先于提交状态展示语义（三者同时成立时拒绝）', () => {
  assert.equal(deriveDrawerCanSubmit(['任务名称'], true, true), false);
});

// ── SubmitGuard：同一事件循环内的同步双击拦截（state 置位等下一次渲染，
//    兜不住同帧连点，必须靠同步标记）──

test('createSubmitGuard: 首次 tryBegin 放行，在途期间再次 tryBegin 拒绝', () => {
  const guard = createSubmitGuard();
  assert.equal(guard.isBusy(), false);
  assert.equal(guard.tryBegin(), true);
  assert.equal(guard.isBusy(), true);
  // 慢响应窗口内的第二次点击（包括同帧双击）在这里被拦下
  assert.equal(guard.tryBegin(), false);
  assert.equal(guard.tryBegin(), false);
});

test('createSubmitGuard: end 复位后放行下一次提交', () => {
  const guard = createSubmitGuard();
  guard.tryBegin();
  guard.end();
  assert.equal(guard.isBusy(), false);
  assert.equal(guard.tryBegin(), true);
});

test('createSubmitGuard: end 幂等，重复复位不产生副作用', () => {
  const guard = createSubmitGuard();
  guard.end();
  guard.end();
  assert.equal(guard.isBusy(), false);
  assert.equal(guard.tryBegin(), true);
});

test('createSubmitGuard: 各实例状态独立', () => {
  const a = createSubmitGuard();
  const b = createSubmitGuard();
  a.tryBegin();
  assert.equal(b.tryBegin(), true);
  b.end();
  assert.equal(a.isBusy(), true);
  a.end();
});
