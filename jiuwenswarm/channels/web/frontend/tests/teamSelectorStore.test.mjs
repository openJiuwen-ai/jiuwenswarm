/**
 * team 选择器的两个纯函数：默认选中项解析、事件归属过滤。
 *
 * 这两个函数是最容易静默出错的地方（切错 team 会直接把别人的成员/任务显示给用户），
 * 所以单独抽出来做无依赖单测，而不是靠 UI 手点。
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createEmptyTeamSelectorRuntime,
  isEventForSelectedTeam,
  resolveSelectedTeamId,
} from '../node_modules/.cache/team-selector/teamSelectorStore.mjs';

const team = (team_id, overrides = {}) => ({
  team_id,
  team_name: team_id,
  state: 'running',
  ...overrides,
});

test('resolveSelectedTeamId keeps an explicit selection that is still listed', () => {
  const teams = [team('team-a'), team('team-b')];
  assert.equal(resolveSelectedTeamId('team-b', teams, 'team-a'), 'team-b');
});

test('resolveSelectedTeamId falls back to the default when the selection disappeared', () => {
  const teams = [team('team-a'), team('team-b')];
  assert.equal(resolveSelectedTeamId('team-gone', teams, 'team-b'), 'team-b');
});

test('resolveSelectedTeamId falls back to the first team when there is no default', () => {
  const teams = [team('team-a'), team('team-b')];
  assert.equal(resolveSelectedTeamId(null, teams, null), 'team-a');
});

test('resolveSelectedTeamId ignores a default that is not in the list', () => {
  // 后端理论上只返回列表内的默认项，但真出现不一致时也不能把选中态指到列表外，
  // 否则下拉的"当前值"会对着一个不存在的 team 渲染。
  const teams = [team('team-a')];
  assert.equal(resolveSelectedTeamId(null, teams, 'team-missing'), 'team-a');
});

test('resolveSelectedTeamId returns null for an empty list', () => {
  assert.equal(resolveSelectedTeamId('team-a', [], 'team-a'), null);
  assert.equal(resolveSelectedTeamId(null, [], null), null);
});

test('isEventForSelectedTeam drops events from another team', () => {
  assert.equal(isEventForSelectedTeam('team-a', 'team-b'), false);
});

test('isEventForSelectedTeam keeps events from the selected team', () => {
  assert.equal(isEventForSelectedTeam('team-a', 'team-a'), true);
});

test('isEventForSelectedTeam passes everything through when nothing is selected', () => {
  assert.equal(isEventForSelectedTeam(null, 'team-b'), true);
  assert.equal(isEventForSelectedTeam(undefined, 'team-b'), true);
  assert.equal(isEventForSelectedTeam('', 'team-b'), true);
});

test('isEventForSelectedTeam passes events without a team_id through', () => {
  // 老后端不带 team_id；漏掉这类事件比多收几个更糟，所以一律放行。
  assert.equal(isEventForSelectedTeam('team-a', null), true);
  assert.equal(isEventForSelectedTeam('team-a', undefined), true);
  assert.equal(isEventForSelectedTeam('team-a', ''), true);
});

test('createEmptyTeamSelectorRuntime starts unloaded with no selection', () => {
  assert.deepEqual(createEmptyTeamSelectorRuntime(), {
    teams: [],
    selectedTeamId: null,
    defaultTeamId: null,
    loading: false,
    error: null,
    loaded: false,
  });
});
