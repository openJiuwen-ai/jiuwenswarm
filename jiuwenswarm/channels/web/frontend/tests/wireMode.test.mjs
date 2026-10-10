import assert from 'node:assert/strict';
import { test } from 'node:test';

// Requires build cache populated by `npm run test:wire-mode`; fresh clones must run that first or this test fails with MODULE_NOT_FOUND.
import {
  isPlanWireMode,
  isTeamAgentMode,
  resolvePlanWireMode,
  stripPlanSuffix,
  supportsPlanMode,
} from '../node_modules/.cache/wire-mode/features/planMode/wireMode.js';

// ── D1: resolvePlanWireMode 按 profile 产出 agent.{work|code}.plan ─────────

test('resolvePlanWireMode produces agent.work.plan when agent + plan on + work profile', () => {
  assert.equal(resolvePlanWireMode('agent', true, 'work'), 'agent.work.plan');
});

test('resolvePlanWireMode produces agent.code.plan when agent + plan on + code profile', () => {
  // D1 修复核心：code profile 下也能产出 agent.code.plan，不再被硬编码成 work。
  assert.equal(resolvePlanWireMode('agent', true, 'code'), 'agent.code.plan');
});

test('resolvePlanWireMode defaults profile to work when omitted', () => {
  assert.equal(resolvePlanWireMode('agent', true), 'agent.work.plan');
  assert.equal(resolvePlanWireMode('agent', true, undefined), 'agent.work.plan');
});

test('resolvePlanWireMode produces agent.work.normal when agent + plan off + work profile', () => {
  // 统一三段命名后 plan off 也带 profile 段，不再回落到裸 agent。
  assert.equal(resolvePlanWireMode('agent', false, 'work'), 'agent.work.normal');
  assert.equal(resolvePlanWireMode('agent', false, 'code'), 'agent.code.normal');
});

test('resolvePlanWireMode defaults profile to work when omitted', () => {
  assert.equal(resolvePlanWireMode('agent', true), 'agent.work.plan');
  assert.equal(resolvePlanWireMode('agent', true, undefined), 'agent.work.plan');
  assert.equal(resolvePlanWireMode('agent', false), 'agent.work.normal');
});

test('resolvePlanWireMode defaults to agent.work.plan when base is empty', () => {
  assert.equal(resolvePlanWireMode('', true, 'work'), 'agent.work.plan');
  assert.equal(resolvePlanWireMode(undefined, true, 'code'), 'agent.code.plan');
});

test('resolvePlanWireMode produces team.work.plan when team + plan on + work profile', () => {
  assert.equal(resolvePlanWireMode('team', true, 'work'), 'team.work.plan');
});

test('resolvePlanWireMode produces team.code.plan when team + plan on + code profile', () => {
  assert.equal(resolvePlanWireMode('team', true, 'code'), 'team.code.plan');
});

test('resolvePlanWireMode produces team.{work|code}.normal when team + plan off', () => {
  assert.equal(resolvePlanWireMode('team', false, 'work'), 'team.work.normal');
  assert.equal(resolvePlanWireMode('team', false, 'code'), 'team.code.normal');
});

test('resolvePlanWireMode keeps auto_harness as-is (no plan support)', () => {
  assert.equal(resolvePlanWireMode('auto_harness', true, 'work'), 'auto_harness');
});

// ── isPlanWireMode 认新串 agent.work.plan / agent.code.plan 与集群 ──────────

test('isPlanWireMode returns true for agent and team plan wire modes', () => {
  assert.equal(isPlanWireMode('agent.work.plan'), true);
  assert.equal(isPlanWireMode('agent.code.plan'), true);
  assert.equal(isPlanWireMode('team.work.plan'), true);
  assert.equal(isPlanWireMode('team.code.plan'), true);
});

test('isPlanWireMode returns false for legacy agent.plan', () => {
  // D1 后 wireMode 只产 agent.{work|code}.plan，旧 agent.plan 不再认作 plan wire mode
  assert.equal(isPlanWireMode('agent.plan'), false);
});

test('isPlanWireMode returns false for plain agent / team / undefined', () => {
  assert.equal(isPlanWireMode('agent'), false);
  assert.equal(isPlanWireMode('team'), false);
  assert.equal(isPlanWireMode(undefined), false);
});

test('isPlanWireMode returns false for plan-off normal wires', () => {
  // 统一三段命名后 plan off 产出 *.normal，非 plan 状态。
  assert.equal(isPlanWireMode('agent.work.normal'), false);
  assert.equal(isPlanWireMode('agent.code.normal'), false);
  assert.equal(isPlanWireMode('team.work.normal'), false);
  assert.equal(isPlanWireMode('team.code.normal'), false);
});

// ── stripPlanSuffix 去掉新 plan 段（agent / team 两种 profile）──────────────

test('stripPlanSuffix returns agent for agent plan wires and team for team plan wires', () => {
  assert.equal(stripPlanSuffix('agent.work.plan'), 'agent');
  assert.equal(stripPlanSuffix('agent.code.plan'), 'agent');
  assert.equal(stripPlanSuffix('team.work.plan'), 'team');
  assert.equal(stripPlanSuffix('team.code.plan'), 'team');
});

test('stripPlanSuffix collapses normal wires to base agent / team', () => {
  // plan off 的 *.normal 也要归一到基础模式，使 normalizeAgentMode 折叠成 agent/team。
  assert.equal(stripPlanSuffix('agent.work.normal'), 'agent');
  assert.equal(stripPlanSuffix('agent.code.normal'), 'agent');
  assert.equal(stripPlanSuffix('team.work.normal'), 'team');
  assert.equal(stripPlanSuffix('team.code.normal'), 'team');
});

test('stripPlanSuffix is identity for non-plan wires', () => {
  assert.equal(stripPlanSuffix('agent'), 'agent');
  assert.equal(stripPlanSuffix('team'), 'team');
  assert.equal(stripPlanSuffix(undefined), 'agent');
});

// ── supportsPlanMode: agent 与 team 都支持 Plan ──────────────────────────

test('supportsPlanMode returns true for agent and team', () => {
  assert.equal(supportsPlanMode('agent'), true);
  assert.equal(supportsPlanMode('team'), true);
  assert.equal(supportsPlanMode('auto_harness'), false);
  assert.equal(supportsPlanMode(undefined), false);
});

// ── R3: isTeamAgentMode 识别所有 team.* 与历史别名 ─────────────────────────

test('isTeamAgentMode returns true for team and team.* canonicals', () => {
  assert.equal(isTeamAgentMode('team'), true);
  assert.equal(isTeamAgentMode('team.work.normal'), true);
  assert.equal(isTeamAgentMode('team.work.plan'), true);
  assert.equal(isTeamAgentMode('team.code.normal'), true);
  assert.equal(isTeamAgentMode('team.code.plan'), true);
});

test('isTeamAgentMode returns true for legacy team canonicals and aliases', () => {
  assert.equal(isTeamAgentMode('team.code'), true);
  assert.equal(isTeamAgentMode('code.team'), true);
  assert.equal(isTeamAgentMode('team.plan'), true);
  assert.equal(isTeamAgentMode('team.plan.normal'), true);
  assert.equal(isTeamAgentMode('team.plan.code'), true);
});

test('isTeamAgentMode is case-insensitive and trims whitespace', () => {
  assert.equal(isTeamAgentMode('TEAM'), true);
  assert.equal(isTeamAgentMode('  team.work.normal  '), true);
});

test('isTeamAgentMode returns false for agent / non-team / empty', () => {
  assert.equal(isTeamAgentMode('agent'), false);
  assert.equal(isTeamAgentMode('agent.work.plan'), false);
  assert.equal(isTeamAgentMode('auto_harness'), false);
  assert.equal(isTeamAgentMode(''), false);
  assert.equal(isTeamAgentMode(undefined), false);
  assert.equal(isTeamAgentMode(null), false);
});
