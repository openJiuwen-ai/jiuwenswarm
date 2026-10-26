import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_TEAM_PANEL_STATE,
  normalizeTeamPanelState,
  parseTeamPanelStateRaw,
} from '../node_modules/.cache/team-panel-state/features/teamPanelStateNormalize.js';

test('parseTeamPanelStateRaw returns defaults for null / empty', () => {
  assert.deepEqual(parseTeamPanelStateRaw(null), DEFAULT_TEAM_PANEL_STATE);
  assert.deepEqual(parseTeamPanelStateRaw(''), DEFAULT_TEAM_PANEL_STATE);
});

test('parseTeamPanelStateRaw recovers from malformed JSON', () => {
  assert.deepEqual(parseTeamPanelStateRaw('{invalid-json'), DEFAULT_TEAM_PANEL_STATE);
  assert.deepEqual(parseTeamPanelStateRaw('["not","an","object"]'), DEFAULT_TEAM_PANEL_STATE);
});

test('normalizeTeamPanelState keeps valid values', () => {
  assert.deepEqual(
    normalizeTeamPanelState({
      expanded: true,
      activeTab: 'planning',
      activeDetailTab: 'group',
      selectedMemberId: 'agent-1',
      selectedArtifactId: 'artifact-1',
    }),
    {
      expanded: true,
      activeTab: 'planning',
      activeDetailTab: 'group',
      selectedMemberId: 'agent-1',
      selectedArtifactId: 'artifact-1',
    },
  );
});

test('normalizeTeamPanelState falls back for invalid enum fields', () => {
  assert.deepEqual(
    normalizeTeamPanelState({
      expanded: 'yes',
      activeTab: 'unknown-tab',
      activeDetailTab: 123,
      selectedMemberId: '',
      selectedArtifactId: ' ',
    }),
    {
      expanded: false,
      activeTab: 'team',
      activeDetailTab: 'members',
    },
  );
});
