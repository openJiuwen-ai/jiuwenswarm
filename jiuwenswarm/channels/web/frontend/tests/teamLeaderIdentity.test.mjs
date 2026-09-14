import test from 'node:test';
import assert from 'node:assert/strict';
import {
  normalizeTeamLeaderIdentity,
  resolveTeamLeaderDisplayName,
} from '../node_modules/.cache/team-leader-identity/features/teamLeaderIdentity.js';

test('normalizes the session snake_case identity and safe remote avatar', () => {
  assert.deepEqual(
    normalizeTeamLeaderIdentity({
      agent_template_id: ' leader-template ',
      display_name: ' 专家团负责人 ',
      avatar: 'https://example.test/leader.png',
    }),
    {
      agentTemplateId: 'leader-template',
      displayName: '专家团负责人',
      avatar: 'https://example.test/leader.png',
    },
  );
});

test('keeps the leader name and falls back when avatar is missing or unsafe', () => {
  assert.deepEqual(
    normalizeTeamLeaderIdentity({
      agentTemplateId: 'leader-template',
      displayName: '专家团负责人',
      avatar: 'javascript:alert(1)',
    }),
    {
      agentTemplateId: 'leader-template',
      displayName: '专家团负责人',
    },
  );
  assert.deepEqual(
    normalizeTeamLeaderIdentity({
      agent_template_id: 'leader-template',
      display_name: '专家团负责人',
    }),
    {
      agentTemplateId: 'leader-template',
      displayName: '专家团负责人',
    },
  );
});

test('accepts a localized display_name object for compatibility', () => {
  const identity = normalizeTeamLeaderIdentity({
    agent_template_id: 'leader-template',
    display_name: { zh: '中文负责人', en: 'English Lead' },
  });

  assert.deepEqual(identity, {
    agentTemplateId: 'leader-template',
    displayName: '中文负责人',
    displayNameI18n: { zh: '中文负责人', en: 'English Lead' },
  });
});

test('preserves the localized leader name and resolves the active language', () => {
  const identity = normalizeTeamLeaderIdentity({
    agent_template_id: 'leader-template',
    display_name: '中文负责人',
    display_name_i18n: { zh: '中文负责人', en: 'English Lead' },
  });

  assert.deepEqual(identity, {
    agentTemplateId: 'leader-template',
    displayName: '中文负责人',
    displayNameI18n: { zh: '中文负责人', en: 'English Lead' },
  });
  assert.equal(resolveTeamLeaderDisplayName(identity, 'zh'), '中文负责人');
  assert.equal(resolveTeamLeaderDisplayName(identity, 'en'), 'English Lead');
});

test('rejects malformed identity without affecting ordinary Team fallback', () => {
  assert.equal(normalizeTeamLeaderIdentity({ display_name: '专家团负责人' }), null);
  assert.equal(normalizeTeamLeaderIdentity({ agent_template_id: 'leader-template' }), null);
  assert.equal(normalizeTeamLeaderIdentity(null), null);
});
