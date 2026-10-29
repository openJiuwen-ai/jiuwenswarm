import assert from 'node:assert/strict';
import test from 'node:test';
import { runSkillPackInstall, readSkillPackInstallPlan } from '../node_modules/.cache/skillpack-install/skillPackInstall.mjs';

function plan(overrides = {}) {
  return {
    success: true, confirmation_required: true, plan_id: 'plan-1', skill_type: 'skillpack', can_install: true,
    install_plan: [
      { name: 'weekly-release-pack', kind: 'skillpack', version: '1.0.0', action: 'download' },
      { name: 'release-note', kind: 'skill', version: '1.0.0', action: 'reuse' },
      { name: 'risk-check', kind: 'skill', version: '2.0.0', action: 'download' },
    ], ...overrides,
  };
}

test('normal Skill installation retains the existing one-call contract', async () => {
  let requests = 0;
  const result = await runSkillPackInstall(async () => { requests++; return { success: true }; }, {}, async () => {
    assert.fail('ordinary Skill must not request Pack confirmation');
  });
  assert.equal(result.success, true);
  assert.equal(requests, 1);
});

test('Pack preparation is shown before any install confirmation is sent', async () => {
  const params = { asset_id: 'pack-asset', session_id: 'session', force: false };
  const calls = [];
  const result = await runSkillPackInstall(async next => {
    calls.push(next);
    return calls.length === 1 ? plan() : { success: true, skill: { name: 'weekly-release-pack' } };
  }, params, async prepared => {
    assert.equal(calls.length, 1);
    assert.equal(prepared.install_plan.length, 3);
    return true;
  });
  assert.deepEqual(calls[1], { ...params, plan_id: 'plan-1', confirmed: true });
  assert.equal(result.success, true);
  assert.equal(result.confirmation_required, undefined);
});

test('cancellation does not write or report preparation as installation success', async () => {
  let calls = 0;
  const result = await runSkillPackInstall(async () => { calls++; return plan(); }, {}, async () => false);
  assert.equal(calls, 1);
  assert.equal(result.success, false);
  assert.equal(result.cancelled, true);
});

test('a blocked member cannot be bypassed by a stale or faulty confirmation UI', async () => {
  let calls = 0;
  const result = await runSkillPackInstall(async () => {
    calls++;
    return plan({ install_plan: [{ name: 'risk-check', kind: 'skill', version: '1', action: 'blocked' }] });
  }, {}, async () => true);
  assert.equal(calls, 1);
  assert.equal(result.success, false);
});

test('a changed plan is never automatically confirmed a second time', async () => {
  let calls = 0;
  await assert.rejects(runSkillPackInstall(async () => {
    calls++;
    return plan({ plan_id: `plan-${calls}` });
  }, {}, async () => true), /skillPackInstall.changed/);
  assert.equal(calls, 2);
});

test('invalid preparation payload cannot masquerade as success', async () => {
  assert.equal(readSkillPackInstallPlan(plan({ plan_id: '' })), null);
  assert.equal(readSkillPackInstallPlan(plan({ install_plan: [] })), null);
  await assert.rejects(runSkillPackInstall(async () => plan({ skill_type: 'skill' }), {}, async () => true), /invalidPlan/);
});

test('installation errors remain errors for the caller to display', async () => {
  let calls = 0;
  const result = await runSkillPackInstall(async () => {
    calls++;
    return calls === 1 ? plan() : { success: false, detail: 'state changed' };
  }, {}, async () => true);
  assert.equal(result.success, false);
  assert.equal(result.detail, 'state changed');
});
