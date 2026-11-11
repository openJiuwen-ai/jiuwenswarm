// Regression tests for GitCode issue #6388:
// duplicate recommended phrases were accepted silently when creating an
// employee. The fix adds duplicate detection at the editors (red hint +
// disabled submit) and dedup at the payload builders as a safety net.
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildAgentGroupCreatePayload,
  duplicatePromptIndices,
} from '../node_modules/.cache/agent-prompt-dedup/agentManagement/port.js';
import { duplicatePromptIndices as duplicatePromptIndicesMate } from '../node_modules/.cache/agent-prompt-dedup/jiuwenmate/createFlowState.js';
import { toWireSettings } from '../node_modules/.cache/agent-prompt-dedup/jiuwenmate/jiuwenmateApi.js';

test('duplicatePromptIndices marks later duplicates with the first position and skips blanks', () => {
  assert.deepEqual(duplicatePromptIndices(['帮我写周报', '帮我写周报', 'unique', ' 帮我写周报 ']), [null, 1, null, 1]);
  assert.deepEqual(duplicatePromptIndices(['a', 'b', 'c']), [null, null, null]);
  assert.deepEqual(duplicatePromptIndices(['', '  ', 'a', 'a']), [null, null, null, 3]);
  assert.deepEqual(duplicatePromptIndices([]), []);
});

test('jiuwenmate and agentManagement implementations agree on the same inputs', () => {
  const samples = [
    ['帮我写周报', '帮我写周报'],
    [' x ', 'x', 'y'],
    ['', '  '],
    [],
  ];
  for (const sample of samples) {
    assert.deepEqual(duplicatePromptIndicesMate(sample), duplicatePromptIndices(sample));
  }
});

test('agent group payload dedupes recommended prompts like members and skills', () => {
  const payload = buildAgentGroupCreatePayload({
    id: 'grp-1',
    name: '办公专家',
    description: '描述',
    persona: '人设',
    category: 'custom',
    tagIds: [],
    customTags: [],
    leaderId: 'leader',
    memberIds: ['leader', 'm1', 'm1'],
    skillRefs: ['s1', 's1', 's2'],
    suggestedPrompts: [' 帮我写周报 ', '帮我写周报', ''],
  });
  assert.deepEqual(payload.memberIds, ['m1']);
  assert.deepEqual(payload.skills, ['s1', 's2']);
  assert.deepEqual(payload.quickInputs, ['帮我写周报']);
});

test('agent group payload keeps a normal prompt list unchanged', () => {
  const payload = buildAgentGroupCreatePayload({
    id: 'grp-2',
    name: '办公专家',
    description: '描述',
    persona: '人设',
    category: 'custom',
    tagIds: [],
    customTags: [],
    leaderId: 'leader',
    memberIds: ['m1'],
    skillRefs: ['s1'],
    suggestedPrompts: ['第一问', '第二问', '第三问'],
  });
  assert.deepEqual(payload.quickInputs, ['第一问', '第二问', '第三问']);
});

test('jiuwenmate colleague settings dedupe starter prompts and keep first occurrence order', () => {
  const wire = toWireSettings({
    name: '办公专家',
    description: '描述',
    tags: [],
    category: 'custom',
    capabilities: {
      model: '',
      skills: [],
      connectors: [],
      plugins: [],
      knowledge: [],
      starterPrompts: ['帮我写周报', '帮我写周报', ' 别的 ', ''],
    },
  }, []);
  assert.deepEqual(wire.capabilities.starter_prompts, ['帮我写周报', '别的']);
});
