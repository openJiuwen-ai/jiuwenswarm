import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { applyDuplexSubagentEvent } from '../../../../channels/web/frontend/node_modules/.cache/duplex-subagent-panel/duplexSubagentEvents.js';
import {
  selectSubagentActivities,
  selectSubagents,
  selectSubagentTurns,
  useSubagentStore,
} from '../../../../channels/web/frontend/node_modules/.cache/duplex-subagent-panel/subagentStore.js';

// A managed voice task runs in its own Core session; the frames still belong to the visible chat.
const roster = {
  event_type: 'chat.subtask_update', session_id: 'managed-task-1', parent_session_id: 'managed-task-1',
  subagent_id: 'cua-1', subagent_type: 'cua_agent', display_name: 'cua_agent',
  task_description: 'Open the settings page', status: 'running', created_at: 1000, updated_at: 1000, revision: 1,
};
const activity = {
  event_type: 'chat.subagent_activity', subagent_id: 'cua-1', parent_session_id: 'managed-task-1',
  task_id: 'cua-1', seq: 1, kind: 'tool_call', summary: 'screenshot()', tool_name: 'screenshot', at_ms: 2000,
};

test('voice task subagent frames populate the subagent panel of the visible conversation', () => {
  applyDuplexSubagentEvent('visible', roster);
  applyDuplexSubagentEvent('visible', activity);
  const runtime = useSubagentStore.getState().getRuntime('visible');
  assert.deepEqual(selectSubagents(runtime).map(agent => agent.display_name), ['cua_agent']);
  assert.deepEqual(selectSubagentActivities(runtime, 'cua-1').map(item => item.summary), ['screenshot()']);
  assert.equal(selectSubagentTurns(runtime, 'cua-1')[0].task_description, 'Open the settings page');
  // Frames never leak into another conversation's panel.
  assert.equal(useSubagentStore.getState().getRuntime('managed-task-1'), undefined);
});

test('malformed frames are ignored instead of creating phantom subagents', () => {
  applyDuplexSubagentEvent('other', { event_type: 'chat.subagent_activity', subagent_id: 'x' });
  applyDuplexSubagentEvent('other', null);
  applyDuplexSubagentEvent('', roster);
  assert.equal(selectSubagents(useSubagentStore.getState().getRuntime('other')).length, 0);
});

test('task full-duplex runtime routes subagent progress into the shared subagent panel', () => {
  const runtime = readFileSync('../../../extensions/video_duplex/frontend/TaskFullDuplexRuntime.tsx', 'utf8');
  assert.match(
    runtime,
    /entry\.stage === "subagent"[\s\S]{0,120}applyDuplexSubagentEvent\(targetSessionId, entry\.subagent_event\)/,
  );
});
