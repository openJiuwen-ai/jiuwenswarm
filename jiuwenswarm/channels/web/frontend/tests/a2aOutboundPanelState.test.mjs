import assert from 'node:assert/strict';
import test from 'node:test';
import {
  normalizeA2AOutboundAgent,
  normalizeA2AOutboundDiscovery,
  normalizeA2AOutboundList,
  normalizeA2AOutboundSettings,
  shouldAcceptA2AOutboundResponse,
  createA2AOutboundRequestScope,
  describeA2AOutboundAuthentication,
} from '../node_modules/.cache/a2a-outbound-panel-state/components/A2AIngressPanel/a2aOutboundPanelState.js';

const selectedInterface = {
  protocol_binding: 'JSONRPC',
  protocol_version: '1.0.0',
  url: 'https://agent.example.com/a2a',
};

const agent = {
  agent_id: 'agent-1',
  display_name: 'Research Agent',
  card_revision: 1,
  agent_card: { name: 'Research Agent' },
  selected_interface: selectedInterface,
  enabled: true,
  manager_enabled: true,
  user_enabled: true,
  effective_enabled: true,
  availability: 'available',
  has_credential: false,
  connect_timeout_seconds: 10,
  sync_wait_seconds: 300,
  last_checked_at: null,
  last_error_summary: null,
  pending_revision: null,
};

test('A2A outbound discovery requires an id, name, and compatible interface', () => {
  const discovery = normalizeA2AOutboundDiscovery({
    discovery_id: 'disc-1',
    expires_at: '2026-08-26T12:00:00Z',
    source_url: 'https://agent.example.com',
    card_path: '/.well-known/agent-card.json',
    card_fingerprint: 'sha256:card',
    agent_card: { securitySchemes: { auth: { apiKeySecurityScheme: { location: 'header', name: 'X-API-Key' } } } },
    agent: {
      name: 'Research Agent',
      description: 'Researches',
      version: '1.0.0',
      skills: [],
      compatible_interfaces: [selectedInterface],
    },
    security_requirements: [],
    warnings: [],
  });

  assert.ok(discovery);
  assert.equal(discovery.agent_card.securitySchemes.auth.apiKeySecurityScheme.name, 'X-API-Key');
  assert.equal(discovery.agent.compatible_interfaces[0].url, selectedInterface.url);
  assert.equal(normalizeA2AOutboundDiscovery({ discovery_id: 'disc-1', agent: { name: 'Agent' } }), null);
});

test('A2A outbound agent rejects unknown availability and malformed interfaces', () => {
  assert.ok(normalizeA2AOutboundAgent(agent));
  assert.equal(normalizeA2AOutboundAgent({ ...agent, availability: 'working' }), null);
  assert.equal(normalizeA2AOutboundAgent({ ...agent, selected_interface: { url: selectedInterface.url } }), null);
});

test('personal records derive enterprise enable states from enabled', () => {
  const { manager_enabled, user_enabled, effective_enabled } = normalizeA2AOutboundAgent({
    ...agent,
    manager_enabled: undefined,
    user_enabled: undefined,
    effective_enabled: undefined,
  });
  assert.deepEqual({ manager_enabled, user_enabled, effective_enabled }, {
    manager_enabled: true,
    user_enabled: true,
    effective_enabled: true,
  });
});

test('A2A outbound list rejects any malformed item', () => {
  assert.deepEqual(normalizeA2AOutboundList({ items: [agent] }), [agent]);
  assert.equal(normalizeA2AOutboundList({ items: [agent, { agent_id: '' }] }), null);
  assert.equal(normalizeA2AOutboundList({ items: 'invalid' }), null);
});

test('A2A outbound settings require an explicit loopback boolean', () => {
  assert.deepEqual(normalizeA2AOutboundSettings({ allow_loopback_http: true }), { allow_loopback_http: true });
  assert.equal(normalizeA2AOutboundSettings({ allow_loopback_http: 'true' }), null);
  assert.equal(normalizeA2AOutboundSettings({}), null);
});

test('A2A outbound ignores obsolete responses', () => {
  assert.equal(shouldAcceptA2AOutboundResponse(7, 7), true);
  assert.equal(shouldAcceptA2AOutboundResponse(6, 7), false);
});

test('outbound editing and list refresh accept either completion order independently', async () => {
  for (const editFirst of [true, false]) {
    const list = createA2AOutboundRequestScope();
    const edit = createA2AOutboundRequestScope();
    const oldList = list.next();
    const editRequest = edit.next();
    const listRequest = list.next();
    let resolveEdit, resolveList;
    const accepted = [];
    const editWork = new Promise(resolve => {
      resolveEdit = resolve;
    }).then(() => {
      if (edit.accepts(editRequest)) accepted.push('edit');
    });
    const listWork = new Promise(resolve => {
      resolveList = resolve;
    }).then(() => {
      if (list.accepts(listRequest)) accepted.push('list');
    });
    if (editFirst) {
      resolveEdit();
      await editWork;
      resolveList();
    } else {
      resolveList();
      await listWork;
      resolveEdit();
    }
    await Promise.all([editWork, listWork]);
    assert.deepEqual(accepted, editFirst ? ['edit', 'list'] : ['list', 'edit']);
    assert.equal(list.accepts(oldList), false);
    edit.next();
    assert.equal(edit.accepts(editRequest), false);
  }
});

test('Card credential help reads current scheme declarations without guessing', () => {
  const t = key => key.split('.').at(-1);
  const card = scheme => ({ securityRequirements: [{ schemes: { auth: {} } }], securitySchemes: { auth: scheme } });
  assert.equal(describeA2AOutboundAuthentication(card({ httpAuthSecurityScheme: { scheme: 'bearer' } }), t), 'Bearer Token');
  assert.equal(
    describeA2AOutboundAuthentication(card({ apiKeySecurityScheme: { location: 'header', name: 'X-Custom-Key' } }), t),
    'API Key (header: X-Custom-Key)',
  );
  assert.equal(describeA2AOutboundAuthentication({}, t), 'undeclared');
  assert.equal(describeA2AOutboundAuthentication({ securityRequirements: [{}] }, t), 'none');
  assert.equal(describeA2AOutboundAuthentication({ securityRequirements: [{ schemes: { bearer: {} } }] }, t), 'unknown');
});
