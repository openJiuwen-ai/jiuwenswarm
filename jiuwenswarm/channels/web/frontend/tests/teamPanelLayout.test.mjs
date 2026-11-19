import assert from 'node:assert/strict';
import test from 'node:test';

const builtPath = new URL('../node_modules/.cache/team-panel-layout/teamPanelLayout.mjs', import.meta.url);
const { TEAM_PANEL_MIN_WIDTH_PX, TEAM_PANEL_WIDTH_REFERENCE_NAME, getMaxTeamChatWidthPct } = await import(builtPath);

test('team panel width uses the representative research:agent name length', () => {
  assert.equal(TEAM_PANEL_WIDTH_REFERENCE_NAME, 'research:agent');
  assert.equal(TEAM_PANEL_MIN_WIDTH_PX, 520);
  assert.equal(getMaxTeamChatWidthPct(1095), ((1095 - 520 - 4) / 1095) * 100);
  assert.ok(1095 * (1 - getMaxTeamChatWidthPct(1095) / 100) >= 520);
});

test('team divider limit adapts to narrow and wide workspaces', () => {
  assert.equal(getMaxTeamChatWidthPct(512), 20);
  assert.equal(getMaxTeamChatWidthPct(1600), ((1600 - 520 - 4) / 1600) * 100);
  assert.equal(getMaxTeamChatWidthPct(0), 70);
});
