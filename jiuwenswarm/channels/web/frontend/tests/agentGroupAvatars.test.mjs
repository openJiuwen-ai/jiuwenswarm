import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveAgentGroupMember } from '../node_modules/.cache/agent-group-avatars/agentGroupAvatars.js';

const group = {
  members: [
    { id: 'clip' },
    { id: 'scene' },
  ],
};

test('an expert node matches its member by name', () => {
  assert.equal(resolveAgentGroupMember(group, { agentTemplate: 'clip' }), true);
});

test('a real predefined member matches by member id; a workflow-scoped id does not', () => {
  assert.equal(resolveAgentGroupMember(group, { memberId: 'scene' }), true);
  assert.equal(resolveAgentGroupMember(group, { memberId: 'run-1:clip_3' }), false);
});

test('no group detail or empty key means not a member', () => {
  assert.equal(resolveAgentGroupMember(null, { agentTemplate: 'clip' }), false);
  assert.equal(resolveAgentGroupMember(undefined, { memberId: 'clip' }), false);
  assert.equal(resolveAgentGroupMember(group, {}), false);
});
