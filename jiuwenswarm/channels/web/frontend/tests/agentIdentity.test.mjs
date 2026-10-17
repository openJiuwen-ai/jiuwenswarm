import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeAgentTemplateName,
  readAgentTemplateName,
} from '../node_modules/.cache/agent-identity/agentIdentity.mjs';

test('normalizes the persisted Agent id and accepts the wire alias', () => {
  assert.equal(normalizeAgentTemplateName('  expert-a  '), 'expert-a');
  assert.equal(readAgentTemplateName({ agent_template_name: 'expert-a' }), 'expert-a');
  assert.equal(readAgentTemplateName({ agentTemplateName: 'expert-b' }), 'expert-b');
  assert.equal(readAgentTemplateName({ agent_template_name: '   ' }), undefined);
  assert.equal(readAgentTemplateName({}), undefined);
});
