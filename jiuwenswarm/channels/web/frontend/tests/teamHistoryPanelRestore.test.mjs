import assert from 'node:assert/strict';
import test from 'node:test';

import { parseTeamHistoryPanelRecords } from '../node_modules/.cache/team-history-panel/teamHistoryPanelRestore.mjs';

function spawnRecord() {
  return {
    mode: 'team',
    event_type: 'chat.tool_call',
    timestamp: 1_700_000_000_000,
    tool_call: { name: 'spawn_member', arguments: JSON.stringify({ member_name: 'dev-1' }) },
  };
}

function shutdownResultRecord(fields) {
  return {
    mode: 'team',
    event_type: 'chat.tool_result',
    timestamp: 1_700_000_001_000,
    tool_name: 'shutdown_member',
    tool_call_id: 'call-1',
    ...fields,
  };
}

test('member shutdown is read from rendered_result, the text the model read', () => {
  const state = parseTeamHistoryPanelRecords(
    [
      spawnRecord(),
      shutdownResultRecord({
        result: "success=True data={'member_name': 'dev-1'} error=None",
        rendered_result: 'Member shutdown: member_name=dev-1',
      }),
    ],
    'session-1',
  );

  assert.deepEqual(state.members.map((member) => member.member_id), []);
});

test('records without rendered_result still parse the compatibility result', () => {
  const state = parseTeamHistoryPanelRecords(
    [spawnRecord(), shutdownResultRecord({ result: 'Member shutdown: member_name=dev-1' })],
    'session-1',
  );

  assert.deepEqual(state.members.map((member) => member.member_id), []);
});
