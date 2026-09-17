import assert from 'node:assert/strict';
import test from 'node:test';
import {
  canOperateA2AIngress,
  draftFromA2AIngressSnapshot,
  isA2AIngressTransitioning,
  normalizeA2AIngressHistory,
  normalizeA2AOutboundDispatchHistory,
  normalizeA2AIngressSnapshot,
  shouldAcceptA2AIngressResponse,
  toA2AIngressPatch,
  validateA2AIngressDraft,
} from '../node_modules/.cache/a2a-ingress-panel-state/components/A2AIngressPanel/a2aIngressPanelState.js';

const snapshot = {
  enabled: true,
  state: 'running',
  desired_host: '127.0.0.1',
  desired_port: 19100,
  desired_rpc_path: '/a2a',
  desired_card_path: '/.well-known/agent-card.json',
  desired_extended_card_path: '/agent/authenticatedExtendedCard',
  desired_protocol_version: '1.0.0',
  desired_app_name: 'Test Agent',
  desired_app_description: 'Test description',
  desired_app_version: '1.2.3',
  desired_expose_reasoning: false,
  desired_rpc_url: 'http://127.0.0.1:19100/a2a',
  desired_card_url: 'http://127.0.0.1:19100/.well-known/agent-card.json',
  effective_host: '127.0.0.1',
  effective_port: 19100,
  effective_rpc_path: '/a2a',
  effective_card_path: '/.well-known/agent-card.json',
  effective_rpc_url: 'http://127.0.0.1:19100/a2a',
  effective_card_url: 'http://127.0.0.1:19100/.well-known/agent-card.json',
  exposure_warning: null,
  started_at: 1,
  last_error: null,
  config_revision: 2,
};

test('A2A ingress snapshot preserves desired configuration for the form', () => {
  const normalized = normalizeA2AIngressSnapshot(snapshot);
  assert.ok(normalized);
  assert.deepEqual(draftFromA2AIngressSnapshot(normalized), {
    host: '127.0.0.1',
    port: '19100',
    rpc_path: '/a2a',
    protocol_version: '1.0.0',
    card_path: '/.well-known/agent-card.json',
    extended_card_path: '/agent/authenticatedExtendedCard',
    app_name: 'Test Agent',
    app_description: 'Test description',
    app_version: '1.2.3',
    expose_reasoning: false,
    auth_type: 'none',
    api_key_header: 'X-API-Key',
    card_auth_required: false,
    credential: '',
    credential_configured: false,
    clear_credential: false,
  });
});

test('A2A ingress form validates locally and sends typed patch values', () => {
  const draft = draftFromA2AIngressSnapshot(normalizeA2AIngressSnapshot(snapshot));
  assert.equal(validateA2AIngressDraft({ ...draft, port: '0' }), 'port');
  assert.equal(validateA2AIngressDraft({ ...draft, rpc_path: 'a2a' }), 'rpc_path');
  assert.deepEqual(toA2AIngressPatch({ ...draft, host: ' 0.0.0.0 ', port: '19101' }), {
    host: '0.0.0.0',
    port: 19101,
    rpc_path: '/a2a',
    protocol_version: '1.0.0',
    card_path: '/.well-known/agent-card.json',
    extended_card_path: '/agent/authenticatedExtendedCard',
    app_name: 'Test Agent',
    app_description: 'Test description',
    app_version: '1.2.3',
    expose_reasoning: false,
    auth_type: 'none',
    api_key_header: 'X-API-Key',
    card_auth_required: false,
  });
});

test('A2A ingress only polls while a lifecycle transition is in progress', () => {
  assert.equal(isA2AIngressTransitioning('starting'), true);
  assert.equal(isA2AIngressTransitioning('stopping'), true);
  assert.equal(isA2AIngressTransitioning('running'), false);
});

test('A2A ingress lifecycle actions wait for a snapshot and a saved form', () => {
  const normalized = normalizeA2AIngressSnapshot(snapshot);
  assert.equal(canOperateA2AIngress(null, true, false, false, 'enable'), false);
  assert.equal(canOperateA2AIngress(normalized, true, false, true, 'enable'), false);
  assert.equal(canOperateA2AIngress(normalized, true, false, false, 'enable'), false);
  assert.equal(canOperateA2AIngress(normalized, true, false, false, 'disable'), true);
});

test('A2A ingress ignores obsolete refresh responses', () => {
  assert.equal(shouldAcceptA2AIngressResponse(4, 4), true);
  assert.equal(shouldAcceptA2AIngressResponse(3, 4), false);
});

test('A2A ingress history accepts lifecycle metadata and skips malformed rows', () => {
  assert.deepEqual(
    normalizeA2AIngressHistory({
      items: [
        {
          request_id: 'req-1',
          context_id: 'ctx-1',
          message_id: 'msg-1',
          operation: 'message',
          status: 'completed',
          started_at: 10,
          finished_at: 10.2,
          duration_ms: 200,
          error: null,
        },
      ],
      total: 1,
    }),
    {
      items: [
        {
          request_id: 'req-1',
          context_id: 'ctx-1',
          message_id: 'msg-1',
          operation: 'message',
          status: 'completed',
          started_at: 10,
          finished_at: 10.2,
          duration_ms: 200,
          error: null,
        },
      ],
      total: 1,
    },
  );
  assert.deepEqual(
    normalizeA2AIngressHistory({
      items: [
        { request_id: '', status: 'completed' },
        { request_id: 'req-valid', status: 'processing', started_at: 20 },
      ],
      total: 2,
    }),
    {
      items: [
        {
          request_id: 'req-valid',
          context_id: null,
          message_id: null,
          operation: 'message',
          status: 'processing',
          started_at: 20,
          finished_at: null,
          duration_ms: null,
          error: null,
        },
      ],
      total: 2,
    },
  );
});

test('A2A outbound history accepts dispatch metadata without result bodies', () => {
  assert.deepEqual(
    normalizeA2AOutboundDispatchHistory({
      items: [
        {
          dispatch_id: 'disp-1',
          agent_id: 'agent-1',
          agent_name: 'Research Agent',
          mode: 'sync',
          status: 'completed',
          remote_task_id: 'task-1',
          created_at: '2026-08-27T01:00:00Z',
          updated_at: '2026-08-27T01:00:02Z',
          accepted_at: '2026-08-27T01:00:01Z',
          finished_at: '2026-08-27T01:00:02Z',
          error_code: null,
          error_summary: null,
        },
      ],
      total: 1,
    }),
    {
      items: [
        {
          dispatch_id: 'disp-1',
          agent_id: 'agent-1',
          agent_name: 'Research Agent',
          mode: 'sync',
          status: 'completed',
          remote_task_id: 'task-1',
          created_at: '2026-08-27T01:00:00Z',
          updated_at: '2026-08-27T01:00:02Z',
          accepted_at: '2026-08-27T01:00:01Z',
          finished_at: '2026-08-27T01:00:02Z',
          error_code: null,
          error_summary: null,
        },
      ],
      total: 1,
    },
  );
});

test('A2A outbound history skips malformed rows without discarding valid records', () => {
  const history = normalizeA2AOutboundDispatchHistory({
    items: [
      { dispatch_id: 'bad-status', agent_id: 'agent-1', mode: 'sync', status: 'future_status', created_at: '2026-08-27T01:00:00Z' },
      {
        dispatch_id: 'disp-1',
        agent_id: 'agent-1',
        mode: 'async',
        status: 'timed_out',
        created_at: '2026-08-27T01:00:00Z',
        updated_at: '2026-08-27T01:00:10Z',
      },
    ],
    total: 2,
  });

  assert.equal(history?.items.length, 1);
  assert.equal(history?.items[0].dispatch_id, 'disp-1');
  assert.equal(history?.items[0].agent_name, 'agent-1');
  assert.equal(history?.items[0].updated_at, '2026-08-27T01:00:10Z');
  assert.equal(history?.total, 2);
});

test('A2A security reloads saved credentials for visibility, replacement and cancellation', () => {
  const savedCredential = 'test-saved-credential-long';
  const normalized = normalizeA2AIngressSnapshot({
    ...snapshot,
    desired_auth_type: 'bearer',
    credential_configured: true,
    credential: savedCredential,
  });
  assert.equal(JSON.stringify(normalized).includes(savedCredential), false);
  const draft = draftFromA2AIngressSnapshot(normalized, savedCredential);
  assert.equal(draft.credential, savedCredential);
  assert.equal(validateA2AIngressDraft(draft), null);
  assert.equal(toA2AIngressPatch(draft).credential, savedCredential);
  assert.equal(Object.hasOwn(toA2AIngressPatch({ ...draft, credential: '' }), 'credential'), false);
  assert.equal(Object.hasOwn(toA2AIngressPatch(draft), 'credential_configured'), false);
  assert.equal(validateA2AIngressDraft({ ...draft, credential: '', credential_configured: false }), 'credential');
  assert.equal(validateA2AIngressDraft({ ...draft, credential: 'short' }), 'credential');
  assert.equal(validateA2AIngressDraft({ ...draft, clear_credential: true }), 'credential');
  assert.equal(validateA2AIngressDraft({ ...draft, auth_type: 'api_key', api_key_header: 'Authorization' }), 'api_key_header');
  assert.equal(validateA2AIngressDraft({ ...draft, auth_type: 'none', api_key_header: 'Authorization' }), null);
  assert.equal(validateA2AIngressDraft({ ...draft, api_key_header: '' }), null);
  assert.equal(toA2AIngressPatch({ ...draft, auth_type: 'none', api_key_header: 'Authorization' }).api_key_header, 'X-API-Key');
  assert.equal(toA2AIngressPatch({ ...draft, api_key_header: 'X-Custom-Key' }).api_key_header, 'X-Custom-Key');
  const replacement = { ...draft, credential: 'test-replacement-credential' };
  assert.equal(validateA2AIngressDraft(replacement), null);
  assert.equal(toA2AIngressPatch(replacement).credential, replacement.credential);
  const cleared = { ...draft, auth_type: 'none', credential: '', clear_credential: true };
  assert.equal(validateA2AIngressDraft(cleared), null);
  assert.equal(toA2AIngressPatch(cleared).clear_credential, true);
  assert.equal(draftFromA2AIngressSnapshot(normalized, savedCredential).credential, savedCredential);
  assert.equal(draftFromA2AIngressSnapshot(normalized).credential, '');
  assert.equal(draftFromA2AIngressSnapshot(normalizeA2AIngressSnapshot(snapshot)).credential, '');
});
