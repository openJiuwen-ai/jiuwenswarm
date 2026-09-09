import assert from 'node:assert/strict';
import test from 'node:test';

import {
  assembleWebRest,
  consumeSseBuffer,
  historyPageToEvents,
  interruptUnaryToEvents,
  lookupWebRestRoute,
  RestAssemblyError,
  sseFrameToWsEvent,
  unwrapHttpUnary,
} from '../node_modules/.cache/web-http-client/services/webHttpClient.js';

const BASE = '/api/v1';

test('unknown method is null, no rpc fallback', () => {
  assert.equal(assembleWebRest('command.goal', { session_id: 's1' }, BASE), null);
  assert.equal(assembleWebRest('memory.compute', {}, BASE), null);
  assert.equal(assembleWebRest('files.list', { session_id: 's1' }, BASE), null);
  assert.equal(lookupWebRestRoute('tts.synthesize'), null);
});

test('session.list is GET query, not H1 /api/sessions', () => {
  const assembled = assembleWebRest('session.list', { limit: 20 }, BASE);
  assert.ok(assembled);
  assert.equal(assembled.verb, 'GET');
  assert.equal(assembled.url, '/api/v1/sessions');
  assert.deepEqual(assembled.query, { limit: '20' });
  assert.equal(assembled.jsonBody, null);
});

test('session.create keeps client session_id in body', () => {
  const assembled = assembleWebRest('session.create', { session_id: 'web_1' }, BASE);
  assert.ok(assembled);
  assert.equal(assembled.verb, 'POST');
  assert.deepEqual(assembled.jsonBody, { session_id: 'web_1' });
});

test('chat.send is completions SSE with query dual-write', () => {
  const assembled = assembleWebRest(
    'chat.send',
    { session_id: 'sid', content: 'hi', mode: 'agent' },
    BASE
  );
  assert.ok(assembled);
  assert.equal(assembled.kind, 'sse');
  assert.equal(assembled.url, '/api/v1/chat/completions');
  assert.equal(assembled.jsonBody.enable_streaming, true);
  assert.equal(assembled.jsonBody.query, 'hi');
  assert.equal(assembled.jsonBody.content, 'hi');
});

test('interrupt and answer put session_id in path', () => {
  const interrupt = assembleWebRest(
    'chat.interrupt',
    { session_id: 'sid', intent: 'cancel' },
    BASE
  );
  assert.ok(interrupt);
  assert.equal(interrupt.url, '/api/v1/chat/sid/actions/interrupt');
  assert.deepEqual(interrupt.jsonBody, { intent: 'cancel' });

  const answer = assembleWebRest(
    'chat.user_answer',
    { session_id: 'sid', request_id: 'q1', answers: { a: 1 } },
    BASE
  );
  assert.ok(answer);
  assert.equal(answer.url, '/api/v1/chat/sid/actions/answer');
  assert.deepEqual(answer.jsonBody, { request_id: 'q1', answers: { a: 1 } });
});

test('missing path placeholder throws, no half url', () => {
  assert.throws(
    () => assembleWebRest('chat.interrupt', { intent: 'cancel' }, BASE),
    (error) => error instanceof RestAssemblyError && error.missing.includes('session_id')
  );
});

test('history.get uses /history with stream kind (Accept SSE)', () => {
  const assembled = assembleWebRest('history.get', { session_id: 'sid', page_idx: 2 }, BASE);
  assert.ok(assembled);
  assert.equal(assembled.kind, 'history-stream');
  assert.equal(assembled.url, '/api/v1/sessions/sid/history');
  assert.deepEqual(assembled.query, { page_idx: '2' });
});

test('cron update keeps patch object; marketplace remove is POST', () => {
  const update = assembleWebRest(
    'cron.job.update',
    { id: 'job-1', patch: { enabled: false, name: 'n' }, session_id: 'sid' },
    BASE
  );
  assert.ok(update);
  assert.equal(update.verb, 'PATCH');
  assert.equal(update.url, '/api/v1/cron/jobs/job-1');
  assert.deepEqual(update.jsonBody, {
    patch: { enabled: false, name: 'n' },
    session_id: 'sid',
  });

  const remove = assembleWebRest(
    'skills.marketplace.remove',
    { name: 'src', remove_cache: true, session_id: 'sid' },
    BASE
  );
  assert.ok(remove);
  assert.equal(remove.verb, 'POST');
  assert.equal(remove.url, '/api/v1/skills/marketplace/actions/remove');
});

test('skillnet search is POST actions/search', () => {
  const assembled = assembleWebRest(
    'skills.skillnet.search',
    { q: 'foo', limit: 10, session_id: 'sid' },
    BASE
  );
  assert.ok(assembled);
  assert.equal(assembled.verb, 'POST');
  assert.equal(assembled.url, '/api/v1/skills/skillnet/actions/search');
});

test('unwrapHttpUnary reads data and error.code', () => {
  assert.deepEqual(unwrapHttpUnary({ ok: true, data: { jobs: [] } }), {
    ok: true,
    payload: { jobs: [] },
    requestId: undefined,
  });
  const fail = unwrapHttpUnary({
    ok: false,
    error: { code: 'NOT_FOUND', message: 'missing' },
  });
  assert.equal(fail.ok, false);
  if (!fail.ok) {
    assert.equal(fail.code, 'NOT_FOUND');
    assert.equal(fail.message, 'missing');
  }
});

test('sse frames keep A1 event names', () => {
  const { frames } = consumeSseBuffer(
    'id: req_1\nevent: chat.delta\ndata: {"content":"a"}\n\n'
  );
  assert.equal(frames.length, 1);
  const event = sseFrameToWsEvent(frames[0]);
  assert.equal(event?.event, 'chat.delta');
  assert.equal(event?.payload.content, 'a');
  assert.equal(event?.request_id, 'req_1');
});

test('SSE frame id restores request_id when data omits it', () => {
  const { frames } = consumeSseBuffer(
    'id: chat-42\nevent: chat.tool_result\ndata: {\"tool_result\":{\"tool_call_id\":\"tool-1\"}}\n\n'
  );
  const event = sseFrameToWsEvent(frames[0]);
  assert.equal(event?.request_id, 'chat-42');
  assert.equal(event?.payload.tool_result.tool_call_id, 'tool-1');
});

test('history JSON page becomes history.message events plus done', () => {
  const events = historyPageToEvents(
    { session_id: 'sid', page_idx: 1, total_pages: 1, messages: [{ role: 'user', content: 'hi' }] },
    'sid'
  );
  assert.equal(events.length, 2);
  assert.equal(events[0].event, 'history.message');
  assert.equal(events[1].payload.status, 'done');
});

const ENTERPRISE_ASSEMBLE = [
  ['connection.status', 'GET', '/api/v1/connection/status', {}, 'unary'],
  ['session.list', 'GET', '/api/v1/sessions', { limit: 20, offset: 0 }, 'unary'],
  ['session.create', 'POST', '/api/v1/sessions', { mode: 'agent' }, 'unary'],
  ['history.get', 'GET', '/api/v1/sessions/sid/history', { session_id: 'sid', page_idx: 1 }, 'history-stream'],
  ['chat.send', 'POST', '/api/v1/chat/completions', { session_id: 'sid', query: 'hi' }, 'sse'],
  ['chat.interrupt', 'POST', '/api/v1/chat/sid/actions/interrupt', { session_id: 'sid', intent: 'pause' }, 'unary'],
  ['chat.user_answer', 'POST', '/api/v1/chat/sid/actions/answer', { session_id: 'sid', request_id: 'q', answers: {} }, 'unary'],
  ['config.get', 'GET', '/api/v1/config', {}, 'unary'],
  ['models.list', 'GET', '/api/v1/models', {}, 'unary'],
  ['a2a.outbound.list', 'GET', '/api/v1/a2a/outbound/agents', {}, 'unary'],
  ['a2a.outbound.enabled.update', 'PATCH', '/api/v1/a2a/outbound/agents/agent-1/enabled', { agent_id: 'agent-1', user_enabled: false }, 'unary'],
  ['a2a.outbound.dispatch.list', 'GET', '/api/v1/a2a/outbound/dispatches', { limit: 200 }, 'unary'],
  ['locale.get_conf', 'GET', '/api/v1/locale', {}, 'unary'],
  ['locale.set_conf', 'PUT', '/api/v1/locale', { preferred_language: 'zh' }, 'unary'],
  ['cron.job.list', 'GET', '/api/v1/cron/jobs', {}, 'unary'],
  ['cron.job.get', 'GET', '/api/v1/cron/jobs/job-1', { id: 'job-1' }, 'unary'],
  ['cron.job.update', 'PATCH', '/api/v1/cron/jobs/job-1', { id: 'job-1', patch: { name: 'n' } }, 'unary'],
  ['cron.job.delete', 'DELETE', '/api/v1/cron/jobs/job-1', { id: 'job-1' }, 'unary'],
  ['cron.job.toggle', 'POST', '/api/v1/cron/jobs/job-1/actions/toggle', { id: 'job-1', enabled: true }, 'unary'],
  ['cron.job.preview', 'POST', '/api/v1/cron/jobs/job-1/actions/preview', { id: 'job-1', count: 3 }, 'unary'],
  ['cron.job.run_now', 'POST', '/api/v1/cron/jobs/job-1/actions/run-now', { id: 'job-1' }, 'unary'],
  ['skills.enterprise.list', 'GET', '/api/v1/skills/enterprise', {}, 'unary'],
  ['skills.enterprise.install', 'POST', '/api/v1/skills/enterprise/actions/install', { url: 'http://x' }, 'unary'],
  ['skills.enterprise.uninstall', 'POST', '/api/v1/skills/enterprise/actions/uninstall', { name: 's' }, 'unary'],
  ['skills.marketplace.list', 'GET', '/api/v1/skills/marketplace', {}, 'unary'],
  ['skills.marketplace.add', 'POST', '/api/v1/skills/marketplace', { name: 'm', url: 'http://m' }, 'unary'],
  ['skills.marketplace.remove', 'POST', '/api/v1/skills/marketplace/actions/remove', { name: 'm' }, 'unary'],
  ['skills.marketplace.toggle', 'POST', '/api/v1/skills/marketplace/actions/toggle', { name: 'm', enabled: true }, 'unary'],
  ['skills.clawhub.get_token', 'GET', '/api/v1/skills/clawhub/token', {}, 'unary'],
  ['skills.clawhub.set_token', 'PUT', '/api/v1/skills/clawhub/token', { token: 't' }, 'unary'],
  ['skills.clawhub.search', 'GET', '/api/v1/skills/clawhub/search', { q: 'foo', limit: 5 }, 'unary'],
  ['skills.clawhub.download', 'POST', '/api/v1/skills/clawhub/actions/download', { slug: 'x' }, 'unary'],
  ['skills.skillnet.search', 'POST', '/api/v1/skills/skillnet/actions/search', { q: 'foo' }, 'unary'],
  ['skills.skillnet.install', 'POST', '/api/v1/skills/skillnet/actions/install', { url: 'http://s' }, 'unary'],
  ['skills.skillnet.install_status', 'GET', '/api/v1/skills/skillnet/install-status', { install_id: 'inst-1' }, 'unary'],
  ['skills.skillnet.evaluate', 'POST', '/api/v1/skills/skillnet/actions/evaluate', { url: 'http://s' }, 'unary'],
  ['skills.evolution.get', 'GET', '/api/v1/skills/evolution', { name: 'evo-1' }, 'unary'],
  ['skills.evolution.save', 'PUT', '/api/v1/skills/evolution', { name: 'evo-1', entries: [] }, 'unary'],
];

test('every enterprise mapped method assembles verb+url+kind', () => {
  assert.equal(ENTERPRISE_ASSEMBLE.length, 38);
  const seen = new Set();
  for (const [method, verb, url, params, kind] of ENTERPRISE_ASSEMBLE) {
    seen.add(method);
    const assembled = assembleWebRest(method, params, BASE);
    assert.ok(assembled, method);
    assert.equal(assembled.verb, verb, method);
    assert.equal(assembled.url, url, method);
    assert.equal(assembled.kind, kind, method);
    if (verb === 'GET' || kind === 'history-stream') {
      assert.equal(assembled.jsonBody, null, method);
    } else {
      assert.ok(assembled.jsonBody && typeof assembled.jsonBody === 'object', method);
    }
  }
  assert.equal(seen.size, ENTERPRISE_ASSEMBLE.length);
});

test('GET leftover params go to query; POST leftover stay in body; path keys stripped', () => {
  const listed = assembleWebRest('session.list', { limit: 10, offset: 2 }, BASE);
  assert.deepEqual(listed.query, { limit: '10', offset: '2' });

  const claw = assembleWebRest('skills.clawhub.search', { q: 'x', limit: 3, session_id: 'sid' }, BASE);
  assert.equal(claw.verb, 'GET');
  assert.deepEqual(claw.query, { q: 'x', limit: '3', session_id: 'sid' });
  assert.equal(claw.jsonBody, null);

  const status = assembleWebRest(
    'skills.skillnet.install_status',
    { install_id: 'inst-1', session_id: 'sid' },
    BASE
  );
  assert.deepEqual(status.query, { install_id: 'inst-1', session_id: 'sid' });

  const evo = assembleWebRest('skills.evolution.get', { name: 'n1', session_id: 'sid' }, BASE);
  assert.deepEqual(evo.query, { name: 'n1', session_id: 'sid' });

  const interrupt = assembleWebRest(
    'chat.interrupt',
    { session_id: 'sid', intent: 'cancel', new_input: 'more' },
    BASE
  );
  assert.equal(interrupt.jsonBody.session_id, undefined);
  assert.deepEqual(interrupt.jsonBody, { intent: 'cancel', new_input: 'more' });

  const cronGet = assembleWebRest('cron.job.get', { id: 'job-1', session_id: 'sid' }, BASE);
  assert.equal(cronGet.url, '/api/v1/cron/jobs/job-1');
  assert.deepEqual(cronGet.query, { session_id: 'sid' });
});

test('path placeholder missing or empty throws; special chars are encoded', () => {
  assert.throws(
    () => assembleWebRest('cron.job.delete', {}, BASE),
    (error) => error instanceof RestAssemblyError && error.missing.includes('id')
  );
  assert.throws(
    () => assembleWebRest('history.get', { session_id: '' }, BASE),
    (error) => error instanceof RestAssemblyError && error.missing.includes('session_id')
  );
  assert.throws(
    () => assembleWebRest('   ', {}, BASE),
    (error) => error instanceof RestAssemblyError
  );
  const encoded = assembleWebRest('history.get', { session_id: 'sid/a b' }, BASE);
  assert.equal(encoded.url, '/api/v1/sessions/sid%2Fa%20b/history');
});

test('chat.send dual-write and does not clobber both fields', () => {
  const fromQuery = assembleWebRest('chat.send', { session_id: 's', query: 'q' }, BASE);
  assert.equal(fromQuery.jsonBody.query, 'q');
  assert.equal(fromQuery.jsonBody.content, 'q');
  assert.equal(fromQuery.jsonBody.enable_streaming, true);

  const fromContent = assembleWebRest('chat.send', { session_id: 's', content: 'c' }, BASE);
  assert.equal(fromContent.jsonBody.query, 'c');
  assert.equal(fromContent.jsonBody.content, 'c');

  const both = assembleWebRest(
    'chat.send',
    { session_id: 's', query: 'q', content: 'c' },
    BASE
  );
  assert.equal(both.jsonBody.query, 'q');
  assert.equal(both.jsonBody.content, 'c');
});

test('skills static prefixes are not /skills/{name}', () => {
  const prefixes = [
    ['skills.enterprise.list', '/api/v1/skills/enterprise'],
    ['skills.marketplace.list', '/api/v1/skills/marketplace'],
    ['skills.clawhub.get_token', '/api/v1/skills/clawhub/token'],
    ['skills.skillnet.search', '/api/v1/skills/skillnet/actions/search'],
    ['skills.evolution.get', '/api/v1/skills/evolution'],
  ];
  for (const [method, url] of prefixes) {
    const assembled = assembleWebRest(method, { name: 'would-be-skill', session_id: 'sid' }, BASE);
    assert.equal(assembled.url, url, method);
    assert.equal(assembled.url.includes('/skills/would-be-skill'), false, method);
  }
});

test('unmapped A2 and personal methods stay null', () => {
  const unmapped = [
    'memory.compute',
    'files.list',
    'files.get',
    'tts.synthesize',
    'command.goal',
    'session.delete',
    'session.rename',
    'cron.job.create',
    'config.set',
    'chat.resume',
    'permissions.tools.get',
    'harness.packages',
    'skills.list',
  ];
  for (const method of unmapped) {
    assert.equal(assembleWebRest(method, { session_id: 's', id: '1', name: 'n' }, BASE), null, method);
    assert.equal(lookupWebRestRoute(method), null, method);
  }
});

test('base url already ending with /api/v1 is not doubled', () => {
  const assembled = assembleWebRest('config.get', {}, 'http://127.0.0.1:19002/api/v1');
  assert.equal(assembled.url, 'http://127.0.0.1:19002/api/v1/config');
});

test('unwrapHttpUnary covers envelope variants', () => {
  assert.equal(unwrapHttpUnary(null).ok, false);
  assert.equal(unwrapHttpUnary('x').ok, false);
  assert.deepEqual(unwrapHttpUnary({ ok: true, data: { a: 1 }, request_id: 'r1' }), {
    ok: true,
    payload: { a: 1 },
    requestId: 'r1',
  });
  assert.equal(unwrapHttpUnary({ ok: true }).payload !== undefined, true);
  const payloadFallback = unwrapHttpUnary({ ok: true, payload: { z: 1 } });
  assert.equal(payloadFallback.ok, true);
  assert.deepEqual(payloadFallback.payload, { z: 1 });
  const bare = unwrapHttpUnary({ agent_ready: true, protocol_version: '1.0' });
  assert.equal(bare.ok, true);
  const strErr = unwrapHttpUnary({ ok: false, error: 'boom' });
  assert.equal(strErr.ok, false);
  assert.equal(strErr.message, 'boom');
});

test('sse parser skips comments and keeps A1 names including history done', () => {
  const { frames } = consumeSseBuffer(
    [
      ': keepalive\n\n',
      'id: r1\nevent: chat.delta\ndata: {"content":"a"}\n\n',
      'event: history.message\ndata: {"status":"done","page_idx":1}\n\n',
      'event: chat.final\ndata: {"content":"a"}\n\n',
    ].join('')
  );
  assert.equal(frames.length, 3);
  assert.equal(sseFrameToWsEvent(frames[0]).event, 'chat.delta');
  assert.equal(sseFrameToWsEvent(frames[1]).payload.status, 'done');
  assert.equal(sseFrameToWsEvent(frames[2]).event, 'chat.final');
});

test('empty history JSON page still emits done', () => {
  const events = historyPageToEvents({ messages: [], page_idx: 1, total_pages: 1 }, 'sid');
  assert.equal(events.length, 1);
  assert.equal(events[0].payload.status, 'done');
});

test('interrupt unary maps only real interrupt_result, never forges success', () => {
  assert.equal(interruptUnaryToEvents({ accepted: true, session_id: 's1' }), null);
  assert.equal(interruptUnaryToEvents({ accepted: true, intent: 'cancel' }), null);
  const event = interruptUnaryToEvents({
    accepted: true,
    session_id: 's1',
    event_type: 'chat.interrupt_result',
    intent: 'cancel',
    success: true,
    message: '任务已取消',
  });
  assert.equal(event.event, 'chat.interrupt_result');
  assert.equal(event.payload.success, true);
  assert.equal(event.payload.intent, 'cancel');
  const pause = interruptUnaryToEvents({
    event_type: 'chat.interrupt_result',
    intent: 'pause',
    success: true,
    message: '任务已暂停',
    session_id: 's1',
  });
  assert.equal(pause.payload.intent, 'pause');
});

