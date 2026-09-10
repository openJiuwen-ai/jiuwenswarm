import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveTaskAssigneePresentation } from '../node_modules/.cache/task-assignee-presentation/components/teamArea/taskAssigneePresentation.js';

test('keeps an assigned task identifiable before the roster entry arrives', () => {
  assert.deepEqual(resolveTaskAssigneePresentation('content-creator', []), {
    kind: 'roster-pending',
    assignee: 'content-creator',
    placeholder: 'CO',
  });
});

test('uses the normal member avatar after the roster is synchronized', () => {
  assert.deepEqual(resolveTaskAssigneePresentation('content-creator', [{ member_id: 'content-creator' }]), {
    kind: 'member',
    assignee: 'content-creator',
    placeholder: 'CO',
  });
});

test('only an absent assignee is unassigned', () => {
  assert.deepEqual(resolveTaskAssigneePresentation(undefined, []), { kind: 'unassigned' });
  assert.deepEqual(resolveTaskAssigneePresentation('   ', []), { kind: 'unassigned' });
});
