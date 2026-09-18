import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  createTrajectoryV2Reducer,
  projectOtelTrajectory,
} from '../node_modules/.cache/trajectory-projector/projector.mjs';

function fixtureUrl(name) {
  return new URL(`../src/features/trajectory/fixtures/${name}`, import.meta.url);
}

async function fixtureRecords(name) {
  return JSON.parse(await readFile(fixtureUrl(name), 'utf8'));
}

async function projectFixture(name) {
  return projectOtelTrajectory(await fixtureRecords(name));
}

function cellsOf(snapshot) {
  return snapshot.turns.flatMap(turn => (
    turn.groups.flatMap(group => group.cells)
  ));
}

function spansOf(records) {
  return records.flatMap(record => (
    record.resourceSpans.flatMap(resource => (
      resource.scopeSpans.flatMap(scope => scope.spans)
    ))
  ));
}

function setStringAttribute(span, key, value) {
  const current = span.attributes.find(attribute => attribute.key === key);
  if (current === undefined) {
    span.attributes.push({ key, value: { stringValue: value } });
    return;
  }
  current.value = { stringValue: value };
}

function setIntAttribute(span, key, value) {
  const current = span.attributes.find(attribute => attribute.key === key);
  if (current === undefined) {
    span.attributes.push({ key, value: { intValue: String(value) } });
    return;
  }
  current.value = { intValue: String(value) };
}

function structuredMessage(role, content) {
  return { role, parts: [{ type: 'text', content }] };
}

function promptAttachmentMessage(mode, content) {
  return {
    ...structuredMessage('system', content),
    openjiuwen: { kind: 'prompt_attachment_history', mode },
  };
}

function v2Attribute(key, value, integer = false) {
  return { key, value: integer ? { intValue: String(value) } : { stringValue: String(value) } };
}

function v2Record({
  eventId,
  eventKind = 'context.window.commit',
  payload,
  requestId = 'request-1',
  sequence,
  sequenceEpoch = 'epoch-1',
  inferenceId = `inference-${sequence}`,
  step = sequence,
  stepId,
  subjectId = 'main',
  time = sequence * 1_000_000,
  traceId = '99999999999999999999999999999999',
  turn = 1,
  turnId,
}) {
  return {
    resourceSpans: [{
      scopeSpans: [{
        spans: [{
          traceId,
          spanId: String(sequence).padStart(16, '0'),
          parentSpanId: eventKind === 'context.window.commit' ? inferenceId : undefined,
          name: eventKind,
          startTimeUnixNano: String(time),
          endTimeUnixNano: String(time + 1),
          attributes: [
            v2Attribute('openjiuwen.trajectory.schema_version', '2'),
            v2Attribute('openjiuwen.trajectory.event_id', eventId),
            v2Attribute('openjiuwen.trajectory.event_kind', eventKind),
            v2Attribute('openjiuwen.trajectory.subject_id', subjectId),
            v2Attribute('openjiuwen.trajectory.subject_sequence', sequence, true),
            v2Attribute('openjiuwen.trajectory.sequence_epoch', sequenceEpoch),
            v2Attribute('openjiuwen.trajectory.session_id', 'session-v2'),
            v2Attribute('openjiuwen.trajectory.request_id', requestId),
            v2Attribute('openjiuwen.trajectory.recorded_at_unix_nano', time, true),
            v2Attribute('openjiuwen.turn.number', turn, true),
            v2Attribute('openjiuwen.step.number', step, true),
            ...(stepId === undefined
              ? []
              : [v2Attribute('openjiuwen.trajectory.step_id', stepId)]),
            ...(turnId === undefined
              ? []
              : [v2Attribute('openjiuwen.trajectory.turn_id', turnId)]),
            v2Attribute('openjiuwen.trajectory.payload', JSON.stringify(payload)),
            v2Attribute('langfuse.gen_ai.prompt.0.role', 'user'),
            v2Attribute('langfuse.gen_ai.prompt.0.content', 'must never become a v2 row'),
          ],
        }],
      }],
    }],
  };
}

function legacyInferenceRecord({
  output,
  requestNumber,
  spanId,
  startTimeUnixNano,
  stepId,
  stepNumber,
  toolCall,
}) {
  const traceId = 'abababababababababababababababab';
  const attributes = [
    v2Attribute('session.id', 'team-session'),
    v2Attribute('gen_ai.conversation.id', 'team-session'),
    v2Attribute('gen_ai.request.model', 'test-model'),
    v2Attribute('gen_ai.input.messages', JSON.stringify([
      structuredMessage('user', `input ${requestNumber}`),
    ])),
    v2Attribute('gen_ai.output.messages', JSON.stringify([{
      role: 'assistant',
      parts: [
        { type: 'text', content: output },
        ...(toolCall === undefined ? [] : [{ type: 'tool_call', ...toolCall }]),
      ],
    }])),
    v2Attribute('openjiuwen.execution.subject.id', 'team-leader'),
    v2Attribute('openjiuwen.execution.subject.kind', 'team_leader'),
    v2Attribute('openjiuwen.execution.subject.session_id', 'team-session'),
    v2Attribute('openjiuwen.execution.subject.request.number', requestNumber, true),
    v2Attribute('openjiuwen.request.number', requestNumber, true),
    v2Attribute('openjiuwen.request.purpose', 'assistant'),
    v2Attribute('openjiuwen.inference.id', spanId),
    v2Attribute('openjiuwen.turn.number', 1, true),
    v2Attribute('openjiuwen.step.number', stepNumber, true),
    v2Attribute('openjiuwen.step.id', stepId),
  ];
  return {
    resourceSpans: [{
      scopeSpans: [{
        spans: [{
          traceId,
          spanId,
          name: 'llm.call',
          startTimeUnixNano: String(startTimeUnixNano),
          endTimeUnixNano: String(startTimeUnixNano + 1000),
          attributes,
        }],
      }],
    }],
  };
}

function trajectoryLogEventRecord({ eventKind, eventId, payload, sequence, time }) {
  const traceId = 'abababababababababababababababab';
  return {
    resourceSpans: [{
      scopeSpans: [{
        spans: [{
          traceId,
          spanId: String(sequence + 100).padStart(16, '0'),
          name: `agent.team-leader.iteration.${sequence}`,
          startTimeUnixNano: String(time - 10),
          endTimeUnixNano: String(time + 10),
          attributes: [
            v2Attribute('session.id', 'team-session'),
            v2Attribute('openjiuwen.execution.subject.id', 'team-leader'),
            v2Attribute('openjiuwen.execution.subject.kind', 'team_leader'),
            v2Attribute('openjiuwen.turn.number', 1, true),
            v2Attribute('openjiuwen.step.number', sequence, true),
          ],
          events: [{
            name: eventKind,
            timeUnixNano: String(time),
            attributes: [
              v2Attribute('openjiuwen.trajectory.schema_version', '2'),
              v2Attribute('openjiuwen.trajectory.event_id', eventId),
              v2Attribute('openjiuwen.trajectory.event_kind', eventKind),
              v2Attribute('openjiuwen.trajectory.subject_id', 'team-leader'),
              v2Attribute('openjiuwen.trajectory.subject_sequence', sequence, true),
              v2Attribute('openjiuwen.trajectory.sequence_epoch', 'ask-user-epoch'),
              v2Attribute('openjiuwen.trajectory.session_id', 'team-session'),
              v2Attribute('openjiuwen.trajectory.recorded_at_unix_nano', time, true),
              v2Attribute('openjiuwen.trajectory.payload', JSON.stringify(payload)),
            ],
          }],
        }],
      }],
    }],
  };
}

function contextCommit(windowId, baseWindowId, messages, delta) {
  return {
    window_id: windowId,
    base_window_id: baseWindowId,
    complete: true,
    messages,
    delta: baseWindowId === null ? [] : delta,
    request_purpose: 'assistant',
    ...(baseWindowId === null ? {
      transition_kind: 'epoch_baseline',
      baseline_reason: 'runtime_epoch_start',
    } : {}),
  };
}

function contextMessage(messageId, role, content, origin) {
  const resolvedOrigin = origin ?? (role === 'user' ? 'external_user' : 'harness_internal');
  return {
    message_id: messageId,
    role,
    origin: resolvedOrigin,
    ...(resolvedOrigin === 'external_user' ? { source_kind: 'web' } : {}),
    content,
  };
}

function trajectoryPromptAttachmentMessage(messageId, content, state, mode = 'snapshot') {
  return {
    ...contextMessage(messageId, 'system', content, 'harness_internal'),
    metadata: {
      _openjiuwen_prompt_attachment_history: true,
      mode,
      session_id: 'session-v2',
      state,
      context_message_id: messageId,
    },
  };
}

function browserEphemeralContextMessage(messageId, slot, content) {
  const metadataKey = {
    working: 'browser_working_context',
    state: 'browser_state_context',
    progress: 'browser_state_progress_context',
  }[slot];
  return {
    ...contextMessage(messageId, 'user', content, 'harness_internal'),
    metadata: { [metadataKey]: true },
  };
}

test('Core forced-close child projects as error with its diagnostic reason', async () => {
  const snapshot = await projectFixture('core-contract-records.json');
  const forcedTool = cellsOf(snapshot).find(cell => (
    cell.kind === 'tool' && cell.callId === 'call-1'
  ));

  assert.ok(forcedTool, 'authoritative tool Span should remain visible');
  assert.equal(forcedTool.status, 'error');
  assert.equal(forcedTool.isError, true);
  assert.equal(forcedTool.result, 'trace_safety_flush');
});

test('historical MCP raw lifecycle span is folded into its authoritative tool', async () => {
  const records = await fixtureRecords('core-contract-records.json');
  const tools = spansOf(records).filter(span => span.name.startsWith('tool.'));
  const authoritative = tools.find(span => span.attributes.some(attribute => (
    attribute.key === 'openjiuwen.tool.authoritative' && attribute.value.boolValue === true
  )));
  const lifecycle = tools.find(span => span !== authoritative);
  assert.ok(authoritative && lifecycle);
  const resourceId = 'playwright.playwright-official.browser_navigate';
  setStringAttribute(authoritative, 'gen_ai.tool.type', 'mcp');
  setStringAttribute(authoritative, 'openjiuwen.tool.type', 'mcp');
  setStringAttribute(authoritative, 'openjiuwen.tool.resource_id', resourceId);
  setStringAttribute(lifecycle, 'gen_ai.tool.id', resourceId);
  lifecycle.parentSpanId = authoritative.spanId;
  lifecycle.attributes = lifecycle.attributes.filter(attribute => (
    attribute.key !== 'gen_ai.tool.call.id'
  ));

  const projectedTools = cellsOf(projectOtelTrajectory(records)).filter(cell => (
    cell.kind === 'tool' || cell.kind === 'subtool'
  ));

  assert.equal(projectedTools.length, 1);
  assert.equal(projectedTools[0].kind, 'tool');
});

test('OTLP JSON numeric error status marks a failed tool cell', async () => {
  const records = await fixtureRecords('agent-loop-records.json');
  const tool = spansOf(records).find(span => span.attributes.some(attribute => (
    attribute.key === 'gen_ai.tool.call.id'
      && attribute.value.stringValue === 'call-loop-1'
  )));
  assert.ok(tool);
  tool.status = { code: 2, message: 'tool reported failure' };

  const snapshot = projectOtelTrajectory(records);
  const failedTool = cellsOf(snapshot).find(cell => cell.callId === 'call-loop-1');
  assert.ok(failedTool);
  assert.equal(failedTool.status, 'error');
  assert.equal(failedTool.isError, true);
  assert.equal(failedTool.result, 'tool reported failure');
});

test('ownerless ask_user result remains one routed TOOL while other ownerless tools stay isolated', async () => {
  const records = await fixtureRecords('core-contract-records.json');
  const toolRecord = records.find(record => spansOf([record]).some(span => (
    span.attributes.some(attribute => (
      attribute.key === 'openjiuwen.tool.authoritative'
        && attribute.value.boolValue === true
    ))
  )));
  assert.ok(toolRecord);
  const askUserRecord = structuredClone(toolRecord);
  const askUser = spansOf([askUserRecord])[0];
  assert.ok(askUser);
  const rootRecord = records.find(record => spansOf([record]).some(span => (
    span.spanId === askUser.parentSpanId
  )));
  assert.ok(rootRecord);
  const routedRootRecord = structuredClone(rootRecord);
  const routedRoot = spansOf([routedRootRecord])[0];
  assert.ok(routedRoot);
  askUser.name = 'tool.ask_user';
  setStringAttribute(askUser, 'gen_ai.tool.name', 'ask_user');
  setStringAttribute(askUser, 'gen_ai.tool.call.id', 'call-ask-user');
  setStringAttribute(askUser, 'gen_ai.tool.call.arguments', '{"questions":["Keep local?"]}');
  setStringAttribute(askUser, 'gen_ai.tool.call.result', 'Keep local');
  setStringAttribute(askUser, 'session.id', 'session-ask-user');
  setStringAttribute(askUser, 'gen_ai.conversation.id', 'session-ask-user');
  setStringAttribute(askUser, 'openjiuwen.execution.subject.id', 'main');
  setStringAttribute(askUser, 'openjiuwen.execution.subject.kind', 'main_agent');
  setStringAttribute(askUser, 'openjiuwen.execution.subject.session_id', 'session-ask-user');
  setStringAttribute(askUser, 'openjiuwen.request.id', 'resume-request');
  const turn = askUser.attributes.find(attribute => attribute.key === 'openjiuwen.turn.number');
  assert.ok(turn);
  askUser.attributes = askUser.attributes.filter(attribute => attribute !== turn);
  setStringAttribute(routedRoot, 'session.id', 'session-ask-user');
  setStringAttribute(routedRoot, 'gen_ai.conversation.id', 'session-ask-user');
  setStringAttribute(routedRoot, 'openjiuwen.execution.subject.id', 'main');
  setStringAttribute(routedRoot, 'openjiuwen.execution.subject.kind', 'main_agent');
  setStringAttribute(routedRoot, 'openjiuwen.execution.subject.session_id', 'session-ask-user');

  const askUserCells = cellsOf(projectOtelTrajectory([routedRootRecord, askUserRecord]));
  assert.equal(askUserCells.length, 1);
  assert.equal(askUserCells[0].kind, 'tool');
  assert.match(askUserCells[0].text, /^ask_user/);
  assert.equal(askUserCells[0].callId, 'call-ask-user');
  assert.match(askUserCells[0].inputDetail, /Keep local/);
  assert.match(askUserCells[0].outputDetail, /Keep local/);
  assert.equal(askUserCells[0].requestRecordId, undefined);
  assert.equal(askUserCells[0].requestless, true);

  assert.equal(cellsOf(projectOtelTrajectory([askUserRecord])).length, 0);
  setStringAttribute(askUser, 'gen_ai.tool.name', 'bash');
  askUser.name = 'tool.bash';
  assert.equal(cellsOf(projectOtelTrajectory([routedRootRecord, askUserRecord])).length, 0);
});

test('system and external user lead pre-model tools while generated context follows them', async () => {
  const records = await fixtureRecords('core-contract-records.json');
  const rootRecord = structuredClone(records.find(record => spansOf([record]).some(span => (
    span.attributes.some(attribute => (
      attribute.key === 'openjiuwen.trace.root' && attribute.value.boolValue === true
    ))
  ))));
  const toolRecord = structuredClone(records.find(record => spansOf([record]).some(span => (
    span.attributes.some(attribute => (
      attribute.key === 'openjiuwen.tool.authoritative'
        && attribute.value.boolValue === true
    ))
  ))));
  const llmRecord = structuredClone(records.find(record => spansOf([record]).some(span => (
    span.name === 'llm.call'
  ))));
  assert.ok(rootRecord && toolRecord && llmRecord);
  const root = spansOf([rootRecord])[0];
  const tool = spansOf([toolRecord])[0];
  const llm = spansOf([llmRecord])[0];
  assert.ok(root && tool && llm);
  root.startTimeUnixNano = '1000000';
  root.endTimeUnixNano = '9000000';
  tool.startTimeUnixNano = '2000000';
  tool.endTimeUnixNano = '2500000';
  llm.startTimeUnixNano = '3000000';
  llm.endTimeUnixNano = '5000000';
  tool.name = 'tool.ask_user';
  tool.parentSpanId = root.spanId;
  setStringAttribute(tool, 'gen_ai.tool.name', 'ask_user');
  setStringAttribute(tool, 'gen_ai.tool.call.id', 'call-pre-model');
  setStringAttribute(tool, 'gen_ai.tool.call.arguments', '{"question":"Continue?"}');
  setStringAttribute(tool, 'gen_ai.tool.call.result', 'Continue');
  for (const span of [root, tool, llm]) {
    setStringAttribute(span, 'session.id', 'session-pre-model');
    setStringAttribute(span, 'gen_ai.conversation.id', 'session-pre-model');
    setStringAttribute(span, 'openjiuwen.execution.subject.id', 'main');
    setStringAttribute(span, 'openjiuwen.execution.subject.kind', 'main_agent');
    setStringAttribute(span, 'openjiuwen.execution.subject.session_id', 'session-pre-model');
    setStringAttribute(span, 'openjiuwen.request.id', 'request-pre-model');
  }
  setStringAttribute(llm, 'openjiuwen.inference.id', llm.spanId);
  const inferenceId = llm.attributes.find(attribute => (
    attribute.key === 'openjiuwen.inference.id'
  ))?.value.stringValue;
  assert.ok(inferenceId);
  const system = contextMessage(
    'pre-model-system',
    'system',
    'system must lead the trajectory',
    'harness_internal',
  );
  const user = contextMessage('pre-model-user', 'user', 'current request');
  user.source_kind = 'query';
  const preparedContext = browserEphemeralContextMessage(
    'pre-model-state',
    'state',
    'prepared browser state',
  );
  const baseline = v2Record({
    eventId: 'event-pre-model-baseline',
    requestId: 'request-pre-model',
    sequence: 1,
    subjectId: 'main',
    time: 4_000_000,
    traceId: root.traceId,
    inferenceId,
    payload: contextCommit('window-pre-model', null, [system, user, preparedContext], []),
  });

  const snapshot = projectOtelTrajectory([rootRecord, toolRecord, llmRecord, baseline]);
  const cells = cellsOf(snapshot);

  assert.equal(snapshot.requests.length, 1);
  assert.equal(cells[0].kind, 'system');
  const setupTool = cells.find(cell => cell.kind === 'tool');
  assert.ok(setupTool);
  assert.equal(setupTool.requestless, true);
  assert.ok(cells.findIndex(cell => cell.text === 'current request') < cells.indexOf(setupTool));
  assert.ok(cells.findIndex(cell => cell.text === 'prepared browser state') > cells.indexOf(setupTool));
});

test('standard and OpenJiuwen fields win over conflicting legacy aliases', async () => {
  const records = await fixtureRecords('core-contract-records.json');
  const inference = spansOf(records).find(span => span.name === 'llm.call');
  assert.ok(inference);
  setIntAttribute(inference, 'gen_ai.usage.total_tokens', 999);
  const snapshot = projectOtelTrajectory(records);
  const request = snapshot.requests?.[0];
  const assistant = cellsOf(snapshot).find(cell => cell.kind === 'message');

  assert.ok(request, 'Core generation should produce a request inspector row');
  assert.equal(request.provider, 'openai');
  assert.equal(request.requestConfig?.temperature, 0);
  assert.equal(request.requestConfig?.maxTokens, 0);
  assert.equal(request.recordedFacts?.correlation?.sessionId, 'core-session');
  assert.deepEqual(request.recordedFacts?.response?.finishReasons, ['tool_calls']);
  assert.equal(request.usage?.input, 0);
  assert.equal(request.usage?.cacheRead, 3);
  assert.equal(request.usage?.cacheWrite, 2);
  assert.equal(request.usage?.output, 7);
  assert.equal(request.usage?.total, 7);
  assert.equal(assistant?.text, 'Core answer');
});

test('unknown request model falls back to the recorded response model', async () => {
  const records = await fixtureRecords('core-contract-records.json');
  const inference = spansOf(records).find(span => span.name === 'llm.call');
  assert.ok(inference);
  setStringAttribute(inference, 'gen_ai.request.model', 'unknown');
  setStringAttribute(inference, 'gen_ai.response.model', 'openai/Qwen3.7-Plus');

  const snapshot = projectOtelTrajectory(records);
  const request = snapshot.requests?.[0];

  assert.ok(request);
  assert.equal(request.model, 'openai/Qwen3.7-Plus');
  assert.equal(request.requestConfig?.model, 'openai/Qwen3.7-Plus');
});

test('schema-v2 context events preserve occurrence identity and never read Langfuse fields', () => {
  const first = contextMessage('message-a', 'user', 'same content');
  const second = contextMessage('message-b', 'user', 'same content');
  const record = v2Record({
    eventId: 'event-1',
    sequence: 1,
    payload: contextCommit('window-1', null, [first, second], [
      { op: 'insert', message_id: first.message_id, index: 0, message: first },
      { op: 'insert', message_id: second.message_id, index: 1, message: second },
    ]),
  });

  const cells = cellsOf(projectOtelTrajectory([record, record]));
  assert.deepEqual(cells.map(cell => cell.text), ['same content', 'same content']);
  assert.ok(cells.every(cell => cell.messageSource.kind === 'trajectory_context_delta'));
  assert.ok(cells.every(cell => !cell.text.includes('must never')));
});

test('schema-v2 user rows require the explicit Core external-user origin', () => {
  const external = contextMessage('external-user', 'user', 'same content');
  const internal = contextMessage(
    'internal-user',
    'user',
    'same content',
    'harness_internal',
  );
  const valid = v2Record({
    eventId: 'event-message-origins',
    sequence: 1,
    payload: contextCommit('window-message-origins', null, [external, internal], [
      { op: 'insert', message_id: external.message_id, index: 0, message: external },
      { op: 'insert', message_id: internal.message_id, index: 1, message: internal },
    ]),
  });
  const missingOriginMessage = { message_id: 'missing-origin', role: 'user', content: 'raw user' };
  const invalid = v2Record({
    eventId: 'event-missing-origin',
    sequence: 2,
    payload: contextCommit('window-missing-origin', 'window-message-origins', [
      external,
      internal,
      missingOriginMessage,
    ], [{
      op: 'insert',
      message_id: missingOriginMessage.message_id,
      index: 2,
      message: missingOriginMessage,
    }]),
  });

  const snapshot = projectOtelTrajectory([valid, invalid]);
  const cells = cellsOf(snapshot);

  assert.deepEqual(cells.map(cell => cell.kind), ['user', 'context']);
  assert.deepEqual(cells.map(cell => cell.messageSource.origin), [
    'external_user',
    'harness_internal',
  ]);
  assert.ok(snapshot.diagnostics.some(diagnostic => (
    diagnostic.code === 'v2.invalid_context_commit'
      && diagnostic.eventId === 'event-missing-origin'
  )));
});

test('schema-v2 system rows bind their own message and the complete prompt snapshot', () => {
  const stable = contextMessage('system-stable', 'system', 'stable instructions');
  const dynamic = contextMessage('system-dynamic', 'system', 'dynamic runtime context');
  const user = contextMessage('user-message', 'user', 'hello');
  const record = v2Record({
    eventId: 'event-system-detail',
    sequence: 1,
    payload: contextCommit('window-system-detail', null, [stable, dynamic, user], [
      { op: 'insert', message_id: stable.message_id, index: 0, message: stable },
      { op: 'insert', message_id: dynamic.message_id, index: 1, message: dynamic },
      { op: 'insert', message_id: user.message_id, index: 2, message: user },
    ]),
  });

  const systemCells = cellsOf(projectOtelTrajectory([record]))
    .filter(cell => cell.kind === 'system');
  assert.deepEqual(systemCells.map(cell => cell.text), [
    'stable instructions',
    'dynamic runtime context',
  ]);
  assert.deepEqual(systemCells.map(cell => cell.promptSystemMessageIndex), [0, 1]);
  assert.ok(systemCells.every(cell => (
    cell.promptDetail?.system === 'stable instructions\n\ndynamic runtime context'
  )));
  assert.deepEqual(systemCells[0].promptDetail?.systemMessages, [
    { index: 0, content: 'stable instructions' },
    { index: 1, content: 'dynamic runtime context' },
  ]);
});

test('schema-v2 removed system rows retain their message-detail binding', () => {
  const system = contextMessage('system-removal', 'system', 'instructions being removed');
  const initial = v2Record({
    eventId: 'event-system-before-removal',
    sequence: 1,
    payload: contextCommit('window-before-removal', null, [system], [
      { op: 'insert', message_id: system.message_id, index: 0, message: system },
    ]),
  });
  const removed = v2Record({
    eventId: 'event-system-removal',
    sequence: 2,
    payload: contextCommit('window-after-removal', 'window-before-removal', [], [
      { op: 'remove', message_id: system.message_id, from_index: 0 },
    ]),
  });

  const removalCell = cellsOf(projectOtelTrajectory([initial, removed]))
    .find(cell => cell.recordId?.includes('event-system-removal'));
  assert.equal(removalCell?.kind, 'system');
  assert.equal(removalCell?.text, 'instructions being removed');
  assert.equal(removalCell?.promptSystemMessageIndex, 0);
  assert.equal(removalCell?.promptDetail?.system, '');
  assert.equal(removalCell?.previousPromptDetail?.system, 'instructions being removed');
});

test('schema-v2 system updates retain the previous prompt for diff rendering', () => {
  const before = contextMessage('system-update', 'system', 'runtime mode: agent');
  const after = contextMessage('system-update', 'system', 'runtime mode: smart agent');
  const initial = v2Record({
    eventId: 'event-system-before-update',
    sequence: 1,
    payload: contextCommit('window-before-update', null, [before], [
      { op: 'insert', message_id: before.message_id, index: 0, message: before },
    ]),
  });
  const replaced = v2Record({
    eventId: 'event-system-update',
    sequence: 2,
    payload: contextCommit('window-after-update', 'window-before-update', [after], [
      { op: 'replace', message_id: after.message_id, index: 0, message: after },
    ]),
  });

  const updateCell = cellsOf(projectOtelTrajectory([initial, replaced]))
    .find(cell => cell.recordId?.includes('event-system-update'));
  assert.equal(updateCell?.kind, 'system');
  assert.equal(updateCell?.text, 'runtime mode: smart agent');
  assert.equal(updateCell?.previousPromptDetail?.system, 'runtime mode: agent');
  assert.equal(updateCell?.promptDetail?.system, 'runtime mode: smart agent');
});

test('schema-v2 prompt attachment deltas update one Full Prompt slot', () => {
  const stable = contextMessage('system-stable', 'system', 'stable instructions');
  const before = {
    ...contextMessage('dynamic-snapshot', 'system', 'runtime mode: agent'),
    metadata: {
      _openjiuwen_prompt_attachment_history: true,
      mode: 'snapshot',
    },
  };
  const after = {
    ...contextMessage('dynamic-delta', 'system', 'runtime mode: smart agent'),
    metadata: {
      _openjiuwen_prompt_attachment_history: true,
      mode: 'delta',
    },
  };
  const updated = v2Record({
    eventId: 'event-dynamic-update',
    sequence: 1,
    payload: contextCommit('window-dynamic-update', null, [stable, before, after], [
      { op: 'insert', message_id: stable.message_id, index: 0, message: stable },
      { op: 'insert', message_id: before.message_id, index: 1, message: before },
      { op: 'insert', message_id: after.message_id, index: 2, message: after },
    ]),
  });

  const systemCells = cellsOf(projectOtelTrajectory([updated]))
    .filter(cell => cell.kind === 'system');
  assert.deepEqual(systemCells.map(cell => cell.text), [
    'stable instructions',
    'runtime mode: agent',
    'runtime mode: smart agent',
  ]);
  const updateCell = systemCells[2];
  assert.equal(updateCell.promptSystemMessageIndex, 1);
  assert.deepEqual(updateCell.previousPromptDetail?.systemMessages, [
    { index: 0, content: 'stable instructions' },
    { index: 1, content: 'runtime mode: agent' },
  ]);
  assert.deepEqual(updateCell.promptDetail?.systemMessages, [
    { index: 0, content: 'stable instructions' },
    { index: 1, content: 'runtime mode: smart agent' },
  ]);
  assert.equal(
    updateCell.promptDetail?.system,
    'stable instructions\n\nruntime mode: smart agent',
  );
});

test('browser ephemeral context replacement does not replay removed or unchanged slots', () => {
  const system = contextMessage(
    'browser-system',
    'system',
    'browser agent prompt',
    'harness_internal',
  );
  const query = contextMessage('browser-query', 'user', 'research stocks');
  const workingA = browserEphemeralContextMessage('working-a', 'working', 'working memory');
  const stateA = browserEphemeralContextMessage('state-a', 'state', 'state: about:blank');
  const progressA = browserEphemeralContextMessage('progress-a', 'progress', 'progress: initial');
  const baselineMessages = [system, query, workingA, stateA, progressA];
  const baseline = v2Record({
    eventId: 'event-browser-baseline',
    sequence: 1,
    payload: contextCommit('window-browser-a', null, baselineMessages, []),
  });
  const workingB = browserEphemeralContextMessage('working-b', 'working', 'working memory');
  const stateB = browserEphemeralContextMessage('state-b', 'state', 'state: finance page');
  const progressB = browserEphemeralContextMessage('progress-b', 'progress', 'progress: changed');
  const nextMessages = [system, query, workingB, stateB, progressB];
  const next = v2Record({
    eventId: 'event-browser-next',
    sequence: 2,
    payload: contextCommit('window-browser-b', 'window-browser-a', nextMessages, [
      { op: 'remove', message_id: workingA.message_id, index: 2 },
      { op: 'remove', message_id: stateA.message_id, index: 3 },
      { op: 'remove', message_id: progressA.message_id, index: 4 },
      { op: 'insert', message_id: workingB.message_id, index: 2, message: workingB },
      { op: 'insert', message_id: stateB.message_id, index: 3, message: stateB },
      { op: 'insert', message_id: progressB.message_id, index: 4, message: progressB },
    ]),
  });
  const assistant = contextMessage(
    'browser-assistant',
    'assistant',
    'continue browsing',
    'harness_internal',
  );
  const stateC = browserEphemeralContextMessage('state-b', 'state', 'state: losers page');
  const stableMessages = [system, query, assistant, workingB, stateC, progressB];
  const stable = v2Record({
    eventId: 'event-browser-stable-id',
    sequence: 3,
    payload: contextCommit('window-browser-c', 'window-browser-b', stableMessages, [
      { op: 'insert', message_id: assistant.message_id, index: 2, message: assistant },
      { op: 'move', message_id: workingB.message_id, from_index: 2, index: 3 },
      { op: 'move', message_id: stateB.message_id, from_index: 3, index: 4 },
      { op: 'replace', message_id: stateB.message_id, index: 4, message: stateC },
      { op: 'move', message_id: progressB.message_id, from_index: 4, index: 5 },
    ]),
  });

  const contexts = cellsOf(projectOtelTrajectory([baseline, next, stable])).filter(cell => (
    cell.kind === 'context'
  ));

  assert.deepEqual(contexts.map(cell => cell.text), [
    'working memory',
    'state: about:blank',
    'progress: initial',
    'state: finance page',
    'progress: changed',
    'state: losers page',
  ]);
  assert.deepEqual(contexts.slice(-3).map(cell => cell.messageSource.operation), [
    'replace',
    'replace',
    'replace',
  ]);
  assert.deepEqual(contexts.slice(-3).map(cell => cell.previousInputDetail), [
    'state: about:blank',
    'progress: initial',
    'state: finance page',
  ]);
});

test('schema-v2 compaction stays at its event position without replaying removed users', () => {
  const requestId = 'request-compaction-real-shape';
  const turnId = 'turn-compaction-real-shape';
  const stepId = 'step-compaction-real-shape';
  const original = contextMessage('message-original-user', 'user', 'research the bash tool');
  const transient = contextMessage(
    'message-compaction-request',
    'user',
    '## NON-NEGOTIABLE OUTPUT RULES',
    'harness_internal',
  );
  const memory = contextMessage(
    'message-compacted-memory',
    'user',
    '<memory_block_current>compressed work</memory_block_current>',
    'harness_internal',
  );
  const recovered = contextMessage(
    'message-recovered-context',
    'user',
    '<recovered_context>restored state</recovered_context>',
    'harness_internal',
  );
  const before = v2Record({
    eventId: 'event-before-compaction',
    requestId,
    sequence: 1,
    step: 33,
    stepId,
    turnId,
    payload: {
      ...contextCommit('window-before-compaction', null, [original, transient], [
        { op: 'insert', message_id: original.message_id, index: 0, message: original },
        { op: 'insert', message_id: transient.message_id, index: 1, message: transient },
      ]),
      request_purpose: 'compaction',
    },
  });
  const compacted = v2Record({
    eventId: 'event-compaction-completed',
    eventKind: 'compaction.completed',
    requestId,
    sequence: 2,
    step: 33,
    stepId,
    turnId,
    payload: {
      type: 'context.compression_state',
      operation_id: 'operation-compaction-1',
      status: 'completed',
      phase: 'get_context_window',
      processor: 'CurrentRoundCompressor',
      model: 'openai/Deepseek-V4-Flash-0731',
      before: { messages: 101, tokens: 134929, context_percent: 67 },
      after: { messages: 8, tokens: 10326, context_percent: 5 },
      saved: { messages: 93, tokens: 124603, percent: 92.3 },
      duration_ms: 123645,
      model_requests: [{ request_id: 'physical-compaction-request', inference_id: 'inference-2' }],
      summary: 'Compressed 101 -> 8 messages',
      compact_summary: '# Compacted context\n\n- preserved the complete research result',
    },
  });
  const output = v2Record({
    eventId: 'event-after-compaction',
    requestId,
    sequence: 3,
    step: 33,
    stepId,
    turnId,
    payload: {
      ...contextCommit(
        'window-after-compaction',
        'window-before-compaction',
        [original, memory, recovered],
        [
          { op: 'remove', message_id: transient.message_id, from_index: 1 },
          { op: 'insert', message_id: memory.message_id, index: 1, message: memory },
          { op: 'insert', message_id: recovered.message_id, index: 2, message: recovered },
        ],
      ),
      transition_kind: 'compaction',
      caused_by_operation_id: 'operation-compaction-1',
      input_window_id: 'window-before-compaction',
      output_window_id: 'window-after-compaction',
    },
  });

  const snapshot = projectOtelTrajectory([output, compacted, before]);
  const cells = cellsOf(snapshot);

  assert.equal(snapshot.turns.length, 1);
  assert.deepEqual(cells.map(cell => cell.kind), [
    'user', 'context', 'compacted', 'context', 'context',
  ]);
  assert.deepEqual(cells.map(cell => cell.text), [
    'research the bash tool',
    '## NON-NEGOTIABLE OUTPUT RULES',
    'Compressed 101 -> 8 messages',
    '<memory_block_current>compressed work</memory_block_current>',
    '<recovered_context>restored state</recovered_context>',
  ]);
  assert.deepEqual(cells.map(cell => cell.behaviorOrder), [
    1 + 1 / 3,
    1 + 2 / 3,
    2,
    3.5,
    3.75,
  ]);
  assert.equal(cells[2].messageSource.operationId, 'operation-compaction-1');
  assert.equal(cells[2].outputDetail, '# Compacted context\n\n- preserved the complete research result');
  assert.deepEqual(cells[2].compactionDetail.before, {
    messages: 101,
    tokens: 134929,
    context_percent: 67,
  });
  assert.deepEqual(cells[2].compactionDetail.saved, {
    messages: 93,
    tokens: 124603,
    percent: 92.3,
  });
  assert.equal(cells[2].compactionDetail.duration_ms, 123645);
  assert.equal(cells[2].compactionDetail.processor, 'CurrentRoundCompressor');
  assert.equal(cells[2].compactionDetail.model, 'openai/Deepseek-V4-Flash-0731');
});

test('schema-v2 context moves do not replay unchanged timeline rows', () => {
  const system = contextMessage(
    'message-moved-system',
    'system',
    'stable system instructions',
    'harness_internal',
  );
  const query = contextMessage('message-moved-user', 'user', 'current user request');
  const memory = contextMessage(
    'message-moved-context',
    'user',
    '<memory_block_dialogue>retained history</memory_block_dialogue>',
    'harness_internal',
  );
  const baseline = v2Record({
    eventId: 'event-before-context-moves',
    sequence: 1,
    payload: contextCommit('window-before-context-moves', null, [system, query, memory], []),
  });
  const reordered = v2Record({
    eventId: 'event-after-context-moves',
    sequence: 2,
    payload: contextCommit(
      'window-after-context-moves',
      'window-before-context-moves',
      [query, system, memory],
      [
        { op: 'move', message_id: system.message_id, from_index: 0, index: 2 },
        { op: 'move', message_id: query.message_id, from_index: 0, index: 1 },
        { op: 'move', message_id: memory.message_id, from_index: 0, index: 2 },
      ],
    ),
  });

  const cells = cellsOf(projectOtelTrajectory([reordered, baseline]));

  assert.deepEqual(cells.map(cell => cell.text), [
    'stable system instructions',
    'current user request',
    '<memory_block_dialogue>retained history</memory_block_dialogue>',
  ]);
  assert.ok(cells.every(cell => cell.messageSource.operation !== 'move'));
});

test('schema-v2 compaction never guesses missing or conflicting output correlation', () => {
  const build = (correlation) => {
    const retained = contextMessage('retained-user', 'user', 'retained user');
    const removed = contextMessage('removed-user', 'user', 'removed user');
    const before = v2Record({
      eventId: `before-${correlation.case}`,
      requestId: 'request-correlation',
      sequence: 1,
      step: 8,
      stepId: 'step-correlation',
      turnId: 'turn-correlation',
      payload: contextCommit('window-input', null, [retained, removed], [
        { op: 'insert', message_id: retained.message_id, index: 0, message: retained },
        { op: 'insert', message_id: removed.message_id, index: 1, message: removed },
      ]),
    });
    const compacted = v2Record({
      eventId: `compacted-${correlation.case}`,
      eventKind: 'compaction.completed',
      requestId: 'request-correlation',
      sequence: 2,
      step: 8,
      stepId: 'step-correlation',
      turnId: 'turn-correlation',
      payload: {
        operation_id: 'operation-correlation',
        input_window_id: 'window-input',
        output_window_id: 'window-output',
        summary: 'compacted',
        compact_summary: 'complete compacted output',
        model_requests: [{ request_id: 'physical-correlation-request', inference_id: 'inference-2' }],
      },
    });
    const output = v2Record({
      eventId: `output-${correlation.case}`,
      requestId: 'request-correlation',
      sequence: 3,
      step: 8,
      stepId: 'step-correlation',
      turnId: 'turn-correlation',
      payload: {
        ...contextCommit('window-output', 'window-input', [retained], [
          { op: 'remove', message_id: removed.message_id, from_index: 1 },
        ]),
        ...correlation.fields,
      },
    });
    return projectOtelTrajectory([output, compacted, before]);
  };

  const missing = build({ case: 'missing', fields: {} });
  assert.equal(cellsOf(missing).filter(cell => cell.text === 'removed user').length, 2);
  assert.ok(missing.diagnostics.some(diagnostic => (
    diagnostic.code === 'v2.missing_compaction_output_correlation'
  )));

  const incomplete = build({
    case: 'incomplete',
    fields: {
      transition_kind: 'compaction',
      caused_by_operation_id: 'operation-correlation',
    },
  });
  assert.equal(cellsOf(incomplete).filter(cell => cell.text === 'removed user').length, 2);
  assert.ok(incomplete.diagnostics.some(diagnostic => (
    diagnostic.code === 'v2.invalid_compaction_correlation'
  )));

  const conflicting = build({
    case: 'conflicting',
    fields: {
      transition_kind: 'compaction',
      caused_by_operation_id: 'operation-correlation',
      input_window_id: 'wrong-window',
      output_window_id: 'window-output',
    },
  });
  assert.equal(cellsOf(conflicting).filter(cell => cell.text === 'removed user').length, 2);
  assert.ok(conflicting.diagnostics.some(diagnostic => (
    diagnostic.code === 'v2.invalid_compaction_correlation'
  )));
});

test('epoch baseline preserves independent compaction correlation', () => {
  const compacted = v2Record({
    eventId: 'event-baseline-compaction',
    eventKind: 'compaction.completed',
    sequence: 1,
    payload: {
      operation_id: 'operation-baseline-compaction',
      model_requests: [{ request_id: 'physical-compaction', inference_id: 'inference-1' }],
      summary: 'Compacted before restart baseline',
      compact_summary: 'Compacted result',
    },
  });
  const memory = contextMessage(
    'baseline-memory',
    'user',
    '<memory_block_current>restored</memory_block_current>',
    'harness_internal',
  );
  const baseline = v2Record({
    eventId: 'event-correlated-baseline',
    sequence: 2,
    payload: {
      ...contextCommit('window-correlated-baseline', null, [memory], []),
      correlation_kind: 'compaction',
      caused_by_operation_id: 'operation-baseline-compaction',
      input_window_id: null,
      output_window_id: 'window-correlated-baseline',
    },
  });

  const snapshot = projectOtelTrajectory([baseline, compacted]);

  assert.ok(cellsOf(snapshot).some(cell => (
    cell.kind === 'compacted' && cell.text === 'Compacted before restart baseline'
  )));
  assert.ok(!(snapshot.diagnostics ?? []).some(diagnostic => (
    diagnostic.code === 'v2.invalid_compaction_correlation'
  )));
});

test('model-free compaction remains visible without a physical model request', () => {
  const compacted = v2Record({
    eventId: 'event-model-free-compaction',
    eventKind: 'compaction.completed',
    sequence: 1,
    payload: {
      operation_id: 'operation-model-free',
      status: 'completed',
      processor: 'ToolResultWindowProcessor',
      model: '',
      model_requests: [],
      summary: 'Compressed 8 -> 8 messages, saved 5.9k tokens',
      compact_summary: '',
    },
  });

  const snapshot = projectOtelTrajectory([compacted]);
  const cells = cellsOf(snapshot);

  assert.equal(cells.length, 1);
  assert.equal(cells[0].kind, 'compacted');
  assert.equal(cells[0].messageSource.modelFree, true);
  assert.equal(cells[0].requestRecordId, undefined);
  assert.equal(cells[0].requestless, true);
  assert.ok(!(snapshot.diagnostics ?? []).some(diagnostic => (
    diagnostic.code === 'v2.missing_physical_request'
      || diagnostic.code === 'v2.missing_compaction_output_correlation'
  )));
});

test('v2 request context keeps logical input order when its event timestamp follows inference start', async () => {
  const records = structuredClone(await fixtureRecords('core-contract-records.json'));
  const inferenceRecord = records[1];
  const inference = spansOf([inferenceRecord])[0];
  setStringAttribute(inference, 'openjiuwen.step.id', 'step-first-request');
  setStringAttribute(inference, 'openjiuwen.request.id', 'request-first-order');
  setStringAttribute(inference, 'openjiuwen.inference.id', 'inference-first-request');
  inference.startTimeUnixNano = '2000000000';
  inference.endTimeUnixNano = '4000000000';
  const system = contextMessage('system-first', 'system', 'stable system');
  const user = contextMessage('user-first', 'user', 'first user');
  const event = v2Record({
    eventId: 'event-first-order',
    inferenceId: 'inference-first-request',
    requestId: 'request-first-order',
    sequence: 1,
    step: 1,
    stepId: 'step-first-request',
    time: 2005000000,
    traceId: inference.traceId,
    payload: contextCommit('window-first-order', null, [system, user], [
      { op: 'insert', message_id: system.message_id, index: 0, message: system },
      { op: 'insert', message_id: user.message_id, index: 1, message: user },
    ]),
  });

  const cumulative = { input: 1200, output: 300, total: 1500 };
  const snapshot = projectOtelTrajectory([inferenceRecord, event], {
    sessionCumulativeUsageByRequestIdentity: new Map([
      [`${inference.traceId}\u0000inference-first-request`, cumulative],
    ]),
  });
  const cells = cellsOf(snapshot);
  assert.deepEqual(cells.map(cell => cell.kind), ['system', 'user', 'message']);
  assert.deepEqual(cells.map(cell => cell.text), ['stable system', 'first user', 'Core answer']);
  assert.ok(cells[2].startedAt < cells[0].startedAt, 'recorded timestamps must remain unmodified');
  assert.ok(cells.every(cell => cell.requestRecordId === cells[2].requestRecordId));
  assert.deepEqual(snapshot.requests[0].cumulativeUsage, cumulative);
});

test('schema-v2 subject state stays isolated when concurrent subagents reuse sequences and window IDs', () => {
  const firstMessage = contextMessage('first', 'user', 'first subagent');
  const secondMessage = contextMessage('second', 'user', 'second subagent');
  const first = v2Record({
    eventId: 'event-first',
    sequence: 1,
    subjectId: 'subagent:first',
    payload: contextCommit('shared-window-id', null, [firstMessage], [
      { op: 'insert', message_id: 'first', index: 0, message: firstMessage },
    ]),
  });
  const second = v2Record({
    eventId: 'event-second',
    sequence: 1,
    subjectId: 'subagent:second',
    payload: contextCommit('shared-window-id', null, [secondMessage], [
      { op: 'insert', message_id: 'second', index: 0, message: secondMessage },
    ]),
  });
  const reducer = createTrajectoryV2Reducer();
  const firstSnapshot = projectOtelTrajectory([first], { v2Reducer: reducer });
  const secondSnapshot = projectOtelTrajectory([second], { v2Reducer: reducer });

  assert.deepEqual(cellsOf(firstSnapshot).map(cell => cell.text), ['first subagent']);
  assert.deepEqual(cellsOf(secondSnapshot).map(cell => cell.text), ['second subagent']);
});

test('schema-v2 sequence validation is isolated across runtime epochs', () => {
  const beforeRestart = contextMessage('before-restart', 'user', 'before restart');
  const afterRestart = contextMessage('after-restart', 'user', 'after restart');
  const records = [
    v2Record({
      eventId: 'event-before-restart',
      sequence: 1,
      sequenceEpoch: 'runtime-a',
      time: 1_000_000,
      traceId: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      payload: contextCommit('window-before-restart', null, [beforeRestart], [{
        op: 'insert',
        message_id: beforeRestart.message_id,
        index: 0,
        message: beforeRestart,
      }]),
    }),
    v2Record({
      eventId: 'event-after-restart',
      sequence: 1,
      sequenceEpoch: 'runtime-b',
      time: 2_000_000,
      traceId: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      payload: contextCommit('window-after-restart', null, [afterRestart], [{
        op: 'insert',
        message_id: afterRestart.message_id,
        index: 0,
        message: afterRestart,
      }]),
    }),
  ];

  const snapshot = projectOtelTrajectory(records);

  assert.deepEqual(cellsOf(snapshot).map(cell => cell.text), [
    'before restart',
    'after restart',
  ]);
  assert.ok(!(snapshot.diagnostics ?? []).some(diagnostic => (
    diagnostic.code === 'v2.sequence_conflict'
  )));
});

test('schema-v2 sequence orders interleaved traces inside one epoch', () => {
  const first = contextMessage('epoch-first', 'user', 'first input');
  const second = contextMessage('epoch-second', 'user', 'second input');
  const records = [
    v2Record({
      eventId: 'event-epoch-second',
      sequence: 2,
      sequenceEpoch: 'runtime-a',
      step: 1,
      time: 1_000_000,
      traceId: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      payload: contextCommit('window-epoch-second', 'window-epoch-first', [first, second], [{
        op: 'insert', message_id: second.message_id, index: 1, message: second,
      }]),
    }),
    v2Record({
      eventId: 'event-epoch-first',
      sequence: 1,
      sequenceEpoch: 'runtime-a',
      step: 1,
      time: 2_000_000,
      traceId: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      payload: contextCommit('window-epoch-first', null, [first], [{
        op: 'insert', message_id: first.message_id, index: 0, message: first,
      }]),
    }),
  ];

  const snapshot = projectOtelTrajectory(records);

  assert.deepEqual(cellsOf(snapshot).map(cell => cell.text), [
    'first input',
    'second input',
  ]);
  assert.equal(snapshot.diagnostics, undefined);
});

test('new epoch baseline does not replay unchanged logical system slots', () => {
  const stable = contextMessage(
    'openjiuwen:request-system-slot:0',
    'system',
    'stable prompt',
    'harness_internal',
  );
  const dynamic = trajectoryPromptAttachmentMessage(
    'dynamic-a',
    'runtime state',
    { runtime: 'same' },
  );
  const restoredDynamic = trajectoryPromptAttachmentMessage(
    'dynamic-b',
    'runtime state',
    { runtime: 'same' },
  );
  const current = contextMessage('current-query', 'user', 'current input');
  current.source_kind = 'query';
  const records = [
    v2Record({
      eventId: 'event-baseline-a',
      sequence: 1,
      sequenceEpoch: 'runtime-a',
      time: 1_000_000,
      payload: contextCommit('window-baseline-a', null, [stable, dynamic], []),
    }),
    v2Record({
      eventId: 'event-baseline-b',
      sequence: 1,
      sequenceEpoch: 'runtime-b',
      time: 2_000_000,
      payload: contextCommit('window-baseline-b', null, [
        stable,
        restoredDynamic,
        current,
      ], []),
    }),
  ];

  const cells = cellsOf(projectOtelTrajectory(records));

  assert.equal(cells.filter(cell => cell.kind === 'system').length, 2);
  assert.deepEqual(cells.filter(cell => cell.kind === 'user').map(cell => cell.text), [
    'current input',
  ]);
});

test('new epoch baseline renders one update for a changed logical system slot', () => {
  const stable = contextMessage(
    'openjiuwen:request-system-slot:0',
    'system',
    'stable prompt',
    'harness_internal',
  );
  const before = trajectoryPromptAttachmentMessage(
    'dynamic-a',
    'runtime state: old',
    { runtime: 'old' },
  );
  const after = trajectoryPromptAttachmentMessage(
    'dynamic-b',
    'runtime state: new',
    { runtime: 'new' },
  );
  const records = [
    v2Record({
      eventId: 'event-update-a',
      sequence: 1,
      sequenceEpoch: 'runtime-a',
      time: 1_000_000,
      payload: contextCommit('window-update-a', null, [stable, before], []),
    }),
    v2Record({
      eventId: 'event-update-b',
      sequence: 1,
      sequenceEpoch: 'runtime-b',
      time: 2_000_000,
      payload: contextCommit('window-update-b', null, [stable, after], []),
    }),
  ];

  const systems = cellsOf(projectOtelTrajectory(records)).filter(cell => cell.kind === 'system');

  assert.equal(systems.length, 3);
  assert.equal(systems.at(-1).text, 'runtime state: new');
  assert.equal(systems.at(-1).messageSource.operation, 'replace');
  assert.ok(systems.at(-1).previousPromptDetail);
});

test('schema-v2 still rejects duplicate sequences inside one epoch', () => {
  const first = contextMessage('same-epoch-first', 'user', 'first');
  const second = contextMessage('same-epoch-second', 'user', 'second');
  const records = [
    v2Record({
      eventId: 'event-same-epoch-first',
      sequence: 1,
      sequenceEpoch: 'runtime-a',
      time: 1_000_000,
      payload: contextCommit('window-same-epoch-first', null, [first], [{
        op: 'insert', message_id: first.message_id, index: 0, message: first,
      }]),
    }),
    v2Record({
      eventId: 'event-same-epoch-second',
      sequence: 1,
      sequenceEpoch: 'runtime-a',
      time: 2_000_000,
      payload: contextCommit('window-same-epoch-second', null, [second], [{
        op: 'insert', message_id: second.message_id, index: 0, message: second,
      }]),
    }),
  ];

  const snapshot = projectOtelTrajectory(records);

  assert.deepEqual(cellsOf(snapshot).map(cell => cell.text), ['first']);
  assert.ok(snapshot.diagnostics.some(diagnostic => (
    diagnostic.code === 'v2.sequence_conflict'
  )));
});

test('attribute pressure and invalid v2 payloads cannot erase the last canonical event view', () => {
  const message = contextMessage('message-pressure', 'user', 'canonical pressure-safe input');
  const valid = v2Record({
    eventId: 'event-pressure',
    sequence: 1,
    payload: contextCommit('window-pressure', null, [message], [
      { op: 'insert', message_id: message.message_id, index: 0, message },
    ]),
  });
  const span = spansOf([valid])[0];
  for (let index = 0; index < 210; index += 1) {
    span.attributes.push(v2Attribute(`langfuse.gen_ai.prompt.${index}.content`, `legacy-${index}`));
  }
  const invalid = v2Record({
    eventId: 'event-invalid',
    sequence: 2,
    payload: { window_id: 'incomplete-window', complete: false },
  });
  const snapshot = projectOtelTrajectory([valid, invalid]);

  assert.deepEqual(cellsOf(snapshot).map(cell => cell.text), ['canonical pressure-safe input']);
  assert.ok(snapshot.diagnostics.some(item => item.code === 'v2.invalid_context_commit'));
});

test('only explicit compaction.completed events create v2 compaction history', () => {
  const record = v2Record({
    eventId: 'compaction-1',
    eventKind: 'compaction.completed',
    sequence: 1,
    payload: {
      operation_id: 'operation-1',
      input_window_id: 'window-before',
      output_window_id: 'window-after',
      summary: 'real compacted summary',
      compact_summary: 'full compacted result',
      model_requests: [{ request_id: 'physical-request-1', inference_id: 'inference-1' }],
    },
  });
  const missingCompactSummary = v2Record({
    eventId: 'compaction-missing-result',
    eventKind: 'compaction.completed',
    subjectId: 'subject-invalid-compaction',
    sequence: 1,
    payload: {
      operation_id: 'operation-missing-result',
      input_window_id: 'window-before-invalid',
      output_window_id: 'window-after-invalid',
      summary: 'statistics without the required result',
      model_requests: [{ request_id: 'physical-missing-result', inference_id: 'inference-1' }],
    },
  });
  const snapshot = projectOtelTrajectory([record, missingCompactSummary]);
  const cells = cellsOf(snapshot);

  assert.equal(cells.length, 1);
  assert.equal(cells[0].kind, 'compacted');
  assert.equal(cells[0].text, 'real compacted summary');
  assert.equal(cells[0].outputDetail, 'full compacted result');
  assert.equal(cells[0].compactionDetail.operation_id, 'operation-1');
  assert.equal(cells[0].messageSource.kind, 'trajectory_compaction');
  assert.ok(snapshot.diagnostics.some(diagnostic => (
    diagnostic.code === 'v2.invalid_compaction'
      && diagnostic.subjectId === 'subject-invalid-compaction'
  )));
});

test('final compaction Span revision replaces its provisional event payload', () => {
  const reducer = createTrajectoryV2Reducer();
  const provisional = v2Record({
    eventId: 'compaction-revision',
    eventKind: 'compaction.completed',
    sequence: 1,
    payload: {
      operation_id: 'operation-revision',
      summary: 'Compaction is still finalizing',
      model_requests: [{ request_id: 'request-revision', inference_id: 'inference-1' }],
    },
  });
  const completed = v2Record({
    eventId: 'compaction-revision',
    eventKind: 'compaction.completed',
    sequence: 1,
    payload: {
      operation_id: 'operation-revision',
      summary: 'Compressed 86 -> 16 messages',
      compact_summary: '<memory_block_dialogue>final summary</memory_block_dialogue>',
      model_requests: [{ request_id: 'request-revision', inference_id: 'inference-1' }],
    },
  });

  const provisionalView = reducer.apply([provisional]);
  const completedView = reducer.apply([completed]);
  const provisionalCells = [...provisionalView.subjects.values()].flatMap(subject => (
    subject.events.flatMap(event => event.cells)
  ));
  const completedCells = [...completedView.subjects.values()].flatMap(subject => (
    subject.events.flatMap(event => event.cells)
  ));

  assert.equal(provisionalCells.some(cell => cell.kind === 'compacted'), false);
  assert.equal(completedCells.filter(cell => cell.kind === 'compacted').length, 1);
  assert.ok(completedCells.some(cell => cell.text === 'Compressed 86 -> 16 messages'));
  assert.ok(!completedView.diagnostics.some(item => item.code === 'v2.event_id_conflict'));
});

test('one-to-many compaction inferences retain physical request boundaries', () => {
  const compacted = v2Record({
    eventId: 'compaction-many-requests',
    eventKind: 'compaction.completed',
    sequence: 1,
    payload: {
      operation_id: 'operation-many-requests',
      summary: 'compacted through two model calls',
      compact_summary: 'complete compacted output',
      model_requests: [
        { request_id: 'physical-request-a', inference_id: 'inference-a' },
        { request_id: 'physical-request-b', inference_id: 'inference-b' },
      ],
    },
  });
  const nextUser = contextMessage(
    'assistant-context',
    'user',
    'internal assistant context',
    'harness_internal',
  );
  const assistantContext = v2Record({
    eventId: 'assistant-physical-request',
    inferenceId: 'inference-assistant',
    sequence: 2,
    payload: contextCommit('assistant-window', null, [nextUser], [{
      op: 'insert',
      message_id: nextUser.message_id,
      index: 0,
      message: nextUser,
    }]),
  });

  const cells = cellsOf(projectOtelTrajectory([assistantContext, compacted]));
  const requestOnly = cells.find(cell => cell.requestOnly === true);
  const compactedCell = cells.find(cell => cell.kind === 'compacted' && cell.requestOnly !== true);
  const assistantCell = cells.find(cell => cell.text === 'internal assistant context');
  const traceId = compacted.resourceSpans[0].scopeSpans[0].spans[0].traceId;

  assert.equal(requestOnly?.requestRecordId, `${traceId}:inference:inference-a`);
  assert.equal(compactedCell?.requestRecordId, `${traceId}:inference:inference-b`);
  assert.equal(assistantCell?.requestRecordId, `${traceId}:inference:inference-assistant`);
  assert.notEqual(requestOnly?.requestRecordId, compactedCell?.requestRecordId);
  assert.notEqual(compactedCell?.requestRecordId, assistantCell?.requestRecordId);
});

test('schema-v2 reducer keeps raw moves without replaying them as timeline rows', () => {
  const alpha = contextMessage('alpha', 'user', 'alpha');
  const beta = contextMessage('beta', 'user', 'beta');
  const replacement = contextMessage('alpha', 'user', 'alpha replaced');
  const records = [
    v2Record({
      eventId: 'event-1',
      sequence: 1,
      payload: contextCommit('window-1', null, [alpha, beta], [
        { op: 'insert', message_id: 'alpha', index: 0, message: alpha },
        { op: 'insert', message_id: 'beta', index: 1, message: beta },
      ]),
    }),
    v2Record({
      eventId: 'event-2',
      sequence: 2,
      payload: contextCommit('window-2', 'window-1', [replacement], [
        { op: 'move', message_id: 'beta', from_index: 1, index: 0 },
        { op: 'remove', message_id: 'beta', from_index: 0 },
        { op: 'replace', message_id: 'alpha', index: 0, message: replacement },
      ]),
    }),
  ];

  const cells = cellsOf(projectOtelTrajectory(records));
  assert.deepEqual(cells.map(cell => cell.messageSource.operation), [
    'insert', 'insert', 'remove', 'replace',
  ]);
  assert.ok(cells.some(cell => cell.text === 'alpha'));
  assert.ok(cells.some(cell => cell.text === 'alpha replaced'));
  const replaced = cells.find(cell => cell.messageSource.operation === 'replace');
  assert.ok(replaced);
  const rawSpan = spansOf([replaced.traceDetail])[0];
  const payloadAttribute = rawSpan.attributes.find(attribute => (
    attribute.key === 'openjiuwen.trajectory.payload'
  ));
  assert.ok(payloadAttribute);
  const rawPayload = JSON.parse(payloadAttribute.value.stringValue);
  assert.deepEqual(rawPayload.delta.map(operation => operation.op), [
    'move', 'remove', 'replace',
  ]);
});

test('schema-v2 complete checkpoints recover across gaps and remain idempotent when gaps arrive later', () => {
  const one = contextMessage('one', 'user', 'one');
  const three = contextMessage('three', 'user', 'three');
  const first = v2Record({
    eventId: 'event-1',
    sequence: 1,
    payload: contextCommit('window-1', null, [one], [
      { op: 'insert', message_id: 'one', index: 0, message: one },
    ]),
  });
  const third = v2Record({
    eventId: 'event-3',
    sequence: 3,
    payload: contextCommit('window-3', 'missing-window', [one, three], [
      { op: 'insert', message_id: 'three', index: 1, message: three },
    ]),
  });
  const reducer = createTrajectoryV2Reducer();
  const recovered = projectOtelTrajectory([first, third], { v2Reducer: reducer });
  const duplicate = projectOtelTrajectory([third], { v2Reducer: reducer });

  assert.deepEqual(cellsOf(recovered).map(cell => cell.text), ['one', 'three']);
  assert.deepEqual(cellsOf(duplicate).map(cell => cell.text), ['one', 'three']);
  assert.ok(duplicate.diagnostics.some(item => item.code === 'v2.checkpoint_recovery'));
  assert.ok(duplicate.diagnostics.some(item => item.code === 'v2.missing_base_window'));
});

test('attribute-pressure llm.call spans keep tool-only and final Assistant requests visible', async () => {
  const snapshot = await projectFixture('attribute-pressure-llm-call-records.json');
  const cells = cellsOf(snapshot);
  const assistants = cells.filter(cell => cell.kind === 'message');
  const tool = cells.find(cell => cell.kind === 'tool');

  assert.equal(assistants.length, 2);
  assert.equal(assistants[0].text, 'Need one more tool');
  assert.equal(assistants[0].sourceBlocks.filter(block => block.type === 'tool-call').length, 1);
  assert.equal(assistants[1].text, 'Final pressure-safe answer');
  assert.ok(tool);
  assert.equal(tool.requestRecordId, assistants[0].requestRecordId);
  assert.notEqual(tool.requestRecordId, assistants[1].requestRecordId);
  assert.deepEqual(snapshot.requests.map(request => request.recordId), [
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:inference:inference-21',
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:inference:inference-22',
  ]);
});

test('canonical v2 context suppresses partial legacy diagnostics for its physical inference', async () => {
  const records = await fixtureRecords('attribute-pressure-llm-call-records.json');
  const llmSpans = spansOf(records).filter(span => span.name === 'llm.call');
  assert.ok(llmSpans[0]);
  const system = contextMessage(
    'pressure-system',
    'system',
    'complete canonical prompt',
    'harness_internal',
  );
  const context = v2Record({
    eventId: 'event-pressure-context',
    requestId: 'request-pressure',
    sequence: 1,
    subjectId: 'subagent:pressure',
    traceId: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    inferenceId: llmSpans[0].spanId,
    payload: contextCommit('window-pressure', null, [system], []),
  });

  const snapshot = projectOtelTrajectory([...records, context]);

  assert.ok(cellsOf(snapshot).some(cell => (
    cell.kind === 'system' && cell.text === 'complete canonical prompt'
  )));
  assert.ok(!(snapshot.diagnostics ?? []).some(diagnostic => (
    diagnostic.code === 'legacy.partial_snapshot'
  )));
});

test('canonical v2 context suppresses legacy input rows by physical inference', async () => {
  const records = await fixtureRecords('attribute-pressure-llm-call-records.json');
  const inferenceRecord = structuredClone(records.find(record => spansOf([record]).some(span => (
    span.name === 'llm.call'
  ))));
  assert.ok(inferenceRecord);
  const inference = spansOf([inferenceRecord])[0];
  assert.ok(inference);
  setStringAttribute(inference, 'gen_ai.system_instructions', JSON.stringify([
    { type: 'text', content: 'Stable system prompt' },
  ]));
  setStringAttribute(inference, 'gen_ai.input.messages', JSON.stringify([
    structuredMessage('user', '<system-reminder>Dynamic context</system-reminder>'),
    structuredMessage('user', 'Current user query'),
  ]));
  setStringAttribute(inference, 'gen_ai.output.messages', JSON.stringify([
    structuredMessage('assistant', 'Current assistant response'),
  ]));
  inference.attributes = inference.attributes.filter(attribute => (
    attribute.key !== 'openjiuwen.request.id'
  ));
  const system = contextMessage(
    'physical-system',
    'system',
    'Stable system prompt',
    'harness_internal',
  );
  const dynamic = contextMessage(
    'physical-dynamic',
    'user',
    '<system-reminder>Dynamic context</system-reminder>',
    'harness_internal',
  );
  const user = contextMessage(
    'physical-user',
    'user',
    'Current user query',
    'external_user',
  );
  const context = v2Record({
    eventId: 'event-physical-context',
    requestId: 'different-request-namespace',
    sequence: 1,
    subjectId: 'subagent:pressure',
    traceId: inference.traceId,
    inferenceId: inference.spanId,
    payload: contextCommit('window-physical', null, [system, dynamic, user], []),
  });

  const cells = cellsOf(projectOtelTrajectory([inferenceRecord, context]));

  assert.equal(cells.filter(cell => cell.text === 'Stable system prompt').length, 1);
  assert.equal(cells.filter(cell => cell.text === 'Current user query').length, 1);
  assert.equal(cells.filter(cell => (
    cell.text === '<system-reminder>Dynamic context</system-reminder>'
  )).length, 1);
  assert.equal(cells.find(cell => cell.text === 'Current user query')?.kind, 'user');
  assert.equal(cells.find(cell => (
    cell.text === '<system-reminder>Dynamic context</system-reminder>'
  ))?.kind, 'context');
  assert.equal(cells.filter(cell => cell.text === 'Current assistant response').length, 1);
  assert.equal(cells.find(cell => cell.text === 'Current assistant response')?.kind, 'message');
});

test('Request numbers are local to an execution subject without changing physical ownership', async () => {
  const snapshot = await projectFixture('request-subject-number-records.json');
  const requests = snapshot.requests ?? [];

  assert.deepEqual(
    requests.map(request => [request.recordId, request.number]),
    [
      ['11111111111111111111111111111111:request:1', 1],
      ['22222222222222222222222222222222:request:2', 2],
      ['33333333333333333333333333333333:request:3', 1],
      ['33333333333333333333333333333333:request:4', 2],
      ['22222222222222222222222222222222:request:5', 3],
      ['44444444444444444444444444444444:request:6', 1],
    ],
  );
  const cells = cellsOf(snapshot);
  const mainThird = cells.find(cell => (
    cell.kind === 'message'
      && cell.requestRecordId === '22222222222222222222222222222222:request:5'
  ));
  const ownedTool = cells.find(cell => cell.kind === 'tool' && cell.text === 'lookup');
  assert.ok(mainThird && ownedTool);
  assert.equal(ownedTool.requestRecordId, mainThird.requestRecordId);
  assert.equal(cells.filter(cell => cell.kind === 'message' && cell.text === 'Repeated output').length, 6);
});

test('Team requests remain chronological when task iterations restart Step numbering', () => {
  const records = [
    legacyInferenceRecord({
      output: 'first request',
      requestNumber: 1,
      spanId: '1111111111111111',
      startTimeUnixNano: 1_000_000,
      stepId: 'iteration-1-step-1',
      stepNumber: 1,
    }),
    legacyInferenceRecord({
      output: 'second request',
      requestNumber: 2,
      spanId: '2222222222222222',
      startTimeUnixNano: 2_000_000,
      stepId: 'iteration-1-step-2',
      stepNumber: 2,
    }),
    legacyInferenceRecord({
      output: 'third request',
      requestNumber: 3,
      spanId: '3333333333333333',
      startTimeUnixNano: 3_000_000,
      stepId: 'iteration-2-step-1',
      stepNumber: 1,
    }),
  ];

  const snapshot = projectOtelTrajectory(records);
  const messages = snapshot.turns[0].groups.flatMap(group => (
    group.cells.filter(cell => cell.kind === 'message').map(cell => cell.text)
  ));

  assert.deepEqual(messages, ['first request', 'second request', 'third request']);
  assert.deepEqual((snapshot.requests ?? []).map(request => request.number), [1, 2, 3]);
});

test('ask_user is not inferred from llm output without authoritative events', () => {
  const record = legacyInferenceRecord({
    output: 'Please confirm the member roles.',
    requestNumber: 1,
    spanId: '4444444444444444',
    startTimeUnixNano: 4_000_000,
    stepId: 'ask-user-step',
    stepNumber: 1,
    toolCall: {
      id: 'call-ask-user-interrupted',
      name: 'ask_user',
      arguments: { query: 'Which roles should be added?' },
    },
  });

  const tools = cellsOf(projectOtelTrajectory([record])).filter(cell => cell.kind === 'tool');

  assert.equal(tools.length, 0);
});

test('ask_user projects only from requested and resolved OTel events', () => {
  const callId = 'call-ask-user-resolved';
  const request = legacyInferenceRecord({
    output: 'Please choose a role plan.',
    requestNumber: 1,
    spanId: '5555555555555555',
    startTimeUnixNano: 5_000_000,
    stepId: 'ask-user-request-step',
    stepNumber: 1,
    toolCall: {
      id: callId,
      name: 'ask_user',
      arguments: { query: 'Which role plan?' },
    },
  });
  const requested = trajectoryLogEventRecord({
    eventKind: 'ask_user.requested',
    eventId: 'event-ask-user-requested',
    sequence: 1,
    time: 6_000_000,
    payload: {
      interaction_id: callId,
      tool_call_id: callId,
      tool_name: 'ask_user',
      arguments: { query: 'Which role plan?' },
      schema: {
        name: 'ask_user',
        description: 'Ask the user a structured question.',
        parameters: { type: 'object', properties: { query: { type: 'string' } } },
      },
      status: 'pending',
    },
  });
  const resolved = trajectoryLogEventRecord({
    eventKind: 'ask_user.resolved',
    eventId: 'event-ask-user-resolved',
    sequence: 2,
    time: 10_000_000,
    payload: {
      interaction_id: callId,
      tool_call_id: callId,
      tool_name: 'ask_user',
      answers: { plan: 'suggested' },
      outcome: 'answered',
      result: 'plan=suggested',
      status: 'completed',
    },
  });

  const pendingTool = cellsOf(projectOtelTrajectory([request, requested])).find(cell => (
    cell.kind === 'tool' && cell.callId === callId
  ));
  assert.ok(pendingTool);
  assert.equal(pendingTool.status, 'running');
  assert.equal(pendingTool.timeSeconds, null);

  const tool = cellsOf(projectOtelTrajectory([request, requested, resolved])).find(cell => (
    cell.kind === 'tool' && cell.callId === callId
  ));

  assert.ok(tool);
  assert.equal(tool.status, 'complete');
  assert.equal(tool.result, 'plan=suggested');
  assert.match(tool.schemaDetail, /Ask the user a structured question/);
  assert.equal(tool.startedAt, 6);
  assert.equal(tool.timeSeconds, 0.004);
  assert.ok(tool.requestRecordId?.includes('5555555555555555'));
});

test('legacy archives deterministically rebuild Request numbers per subject and physical time', async () => {
  const records = await fixtureRecords('request-subject-number-records.json');
  for (const span of spansOf(records)) {
    span.attributes = span.attributes.filter(attribute => (
      attribute.key !== 'openjiuwen.execution.subject.request.number'
    ));
  }
  const reversed = structuredClone(records).reverse();
  const projected = projectOtelTrajectory(records);
  const projectedReversed = projectOtelTrajectory(reversed);
  const numbers = (projected.requests ?? []).map(request => [request.recordId, request.number]);

  assert.deepEqual(numbers, [
    ['11111111111111111111111111111111:request:1', 1],
    ['22222222222222222222222222222222:request:2', 2],
    ['33333333333333333333333333333333:request:3', 1],
    ['33333333333333333333333333333333:request:4', 2],
    ['22222222222222222222222222222222:request:5', 3],
    ['44444444444444444444444444444444:request:6', 1],
  ]);
  assert.deepEqual(
    (projectedReversed.requests ?? []).map(request => [request.recordId, request.number]),
    numbers,
  );
});

test('partially upgraded subject numbers rebuild the whole subject without gaps or duplicates', async () => {
  const records = await fixtureRecords('request-subject-number-records.json');
  const inferenceSpans = spansOf(records).filter(span => span.name === 'llm.call');
  assert.ok(inferenceSpans.length > 2);
  inferenceSpans[0].attributes = inferenceSpans[0].attributes.filter(attribute => (
    attribute.key !== 'openjiuwen.execution.subject.request.number'
  ));
  const malformed = inferenceSpans[1].attributes.find(attribute => (
    attribute.key === 'openjiuwen.execution.subject.request.number'
  ));
  assert.ok(malformed);
  malformed.value = { intValue: '99' };

  assert.deepEqual(
    (projectOtelTrajectory(records).requests ?? []).map(request => [request.recordId, request.number]),
    [
      ['11111111111111111111111111111111:request:1', 1],
      ['22222222222222222222222222222222:request:2', 2],
      ['33333333333333333333333333333333:request:3', 1],
      ['33333333333333333333333333333333:request:4', 2],
      ['22222222222222222222222222222222:request:5', 3],
      ['44444444444444444444444444444444:request:6', 1],
    ],
  );
});

test('behavior projection tracks session prompts, repeated users, attachments, and tools', async () => {
  const snapshot = await projectFixture('agent-loop-records.json');
  const cells = cellsOf(snapshot);
  const inputCells = cells.filter(cell => (
    cell.kind === 'system' || cell.kind === 'user' || cell.kind === 'context'
  ));
  const tool = cells.find(cell => cell.kind === 'tool');
  const assistantCells = cells.filter(cell => cell.kind === 'message');
  const attachmentCells = cells.filter(cell => cell.text.includes('Dynamic context'));
  const contextCells = cells.filter(cell => cell.kind === 'context');
  const repeatedUserCells = cells.filter(cell => cell.text === 'Repeat question');
  const systemCells = cells.filter(cell => cell.kind === 'system');

  assert.deepEqual(systemCells.map(cell => cell.text), [
    'Session system v1',
    'Session system v2',
  ]);
  assert.equal(systemCells[0].previousPromptDetail, undefined);
  assert.equal(systemCells[1].previousPromptDetail?.system, 'Session system v1');
  assert.equal(repeatedUserCells.length, 2);
  assert.equal(cells.filter(cell => cell.text === 'New context fact').length, 1);
  assert.equal(attachmentCells.length, 4);
  assert.ok(attachmentCells.every(cell => cell.kind === 'context'));
  assert.equal(contextCells.length, attachmentCells.length);
  assert.ok(attachmentCells.every(cell => (
    cell.messageSource.role === 'user'
      && cell.messageSource.kind === 'prompt_attachment'
  )));
  assert.deepEqual(
    attachmentCells.map(cell => cell.messageSource.inputIndex),
    [1, 3, 5, 4],
  );
  assert.ok(inputCells.every(cell => cell.timeSeconds === null));

  assert.ok(tool, 'authoritative tool Span should produce one tool item');
  assert.equal(tool.callId, 'call-loop-1');
  assert.match(tool.inputDetail, /"q": "fact"/);
  assert.match(tool.outputDetail, /"answer": 42/);
  assert.equal(tool.timeSeconds, 0.2);

  assert.deepEqual(assistantCells.map(cell => cell.text), [
    'Historical output',
    'I will search',
    'The answer is 42',
    'Done',
  ]);
  assert.deepEqual(assistantCells.map(cell => cell.timeSeconds), [1, 1, 0.6, 0.7]);
  assert.ok(inputCells.every(input => assistantCells.every(assistant => (
    input.timeSeconds === null
      || input.startedAt + input.timeSeconds * 1_000 <= assistant.startedAt
      || assistant.startedAt + assistant.timeSeconds * 1_000 <= input.startedAt
  ))));
});

test('real system message boundaries stay separate and only the changed slot updates', async () => {
  const records = await fixtureRecords('agent-loop-records.json');
  for (const span of spansOf(records)) {
    const instructions = span.attributes.find(attribute => (
      attribute.key === 'gen_ai.system_instructions'
    ));
    if (instructions === undefined) continue;
    const parts = JSON.parse(instructions.value.stringValue);
    const dynamic = parts.map(part => part.content ?? '').join('\n\n');
    // The stable prompt is the instruction given outside the history;
    // prompt-attachment turns are injected into the history itself.
    setStringAttribute(span, 'gen_ai.system_instructions', JSON.stringify([
      { type: 'text', content: 'Stable identity' },
    ]));
    const inputMessages = dynamic === 'Session system v2'
      ? [
          promptAttachmentMessage('snapshot', 'Session system v1'),
          ...Array.from({ length: 5 }, (_, index) => (
            structuredMessage(index % 2 === 0 ? 'user' : 'assistant', `history ${index}`)
          )),
          promptAttachmentMessage('delta', dynamic),
        ]
      : [promptAttachmentMessage('snapshot', dynamic)];
    setStringAttribute(span, 'gen_ai.input.messages', JSON.stringify(inputMessages));
  }

  const systemCells = cellsOf(projectOtelTrajectory(records))
    .filter(cell => cell.kind === 'system');
  assert.deepEqual(systemCells.map(cell => cell.text), [
    'Stable identity',
    'Session system v1',
    'Session system v2',
  ]);
  assert.deepEqual(systemCells.map(cell => cell.promptSystemMessageIndex), [0, 1, 1]);
  assert.equal(systemCells[2].previousPromptDetail?.systemMessages[0].content, 'Stable identity');
  assert.equal(systemCells[2].previousPromptDetail?.systemMessages[1].content, 'Session system v1');
  assert.equal(systemCells[2].promptDetail?.systemMessages[1].content, 'Session system v2');
  assert.equal(systemCells[2].promptDetail?.system, 'Stable identity\n\nSession system v2');
});

test('legacy tool records without call ids use trace-local name and order fallback', async () => {
  const records = await fixtureRecords('agent-loop-records.json');
  const spans = records.flatMap(record => (
    record.resourceSpans.flatMap(resource => (
      resource.scopeSpans.flatMap(scope => scope.spans)
    ))
  ));
  for (const span of spans) {
    span.attributes = span.attributes.filter(attribute => (
      attribute.key !== 'gen_ai.tool.call.id'
    ));
    for (const attribute of span.attributes) {
      if (
        attribute.key !== 'gen_ai.input.messages'
        && attribute.key !== 'gen_ai.output.messages'
      ) continue;
      const messages = JSON.parse(attribute.value.stringValue);
      for (const message of messages) {
        for (const part of message.parts) delete part.id;
      }
      attribute.value.stringValue = JSON.stringify(messages);
    }
  }

  const tool = cellsOf(projectOtelTrajectory(records)).find(cell => cell.kind === 'tool');
  assert.ok(tool);
  assert.match(tool.outputDetail, /"answer": 42/);
});

test('additive OpenJiuwen provenance identifies attachments before XML fallback', async () => {
  const records = await fixtureRecords('agent-loop-records.json');
  for (const record of records) {
    for (const resource of record.resourceSpans) {
      for (const scope of resource.scopeSpans) {
        for (const span of scope.spans) {
          const provenance = [];
          for (const attribute of span.attributes) {
            if (attribute.key !== 'gen_ai.input.messages') continue;
            const messages = JSON.parse(attribute.value.stringValue);
            for (const [index, message] of messages.entries()) {
              const attachment = message.parts.some(part => (
                part.type === 'text' && part.content?.includes('Dynamic context')
              ));
              if (!attachment) continue;
              message.parts = [{ type: 'text', content: 'metadata attachment' }];
              provenance.push({
                request_message_index: index + 1,
                input_message_index: index,
                kind: 'prompt_attachment',
                scope: 'request',
                items: [],
              });
            }
            attribute.value.stringValue = JSON.stringify(messages);
          }
          if (provenance.length > 0) {
            span.attributes.push({
              key: 'openjiuwen.gen_ai.input.message_provenance',
              value: { stringValue: JSON.stringify(provenance) },
            });
          }
        }
      }
    }
  }

  const attachments = cellsOf(projectOtelTrajectory(records))
    .filter(cell => cell.text === 'metadata attachment');
  assert.equal(attachments.length, 4);
  assert.ok(attachments.every(cell => (
    cell.kind === 'context'
      && cell.messageSource.role === 'user'
      && cell.messageSource.kind === 'prompt_attachment'
  )));
});

test('ordered snapshots preserve reintroductions while output replay expires after one inference', async () => {
  const snapshot = await projectFixture('behavior-edge-records.json');
  const cells = cellsOf(snapshot);
  const inputCells = cells.filter(cell => (
    cell.kind === 'system' || cell.kind === 'user' || cell.kind === 'context'
  ));

  assert.deepEqual(
    cells.filter(cell => cell.kind === 'system').map(cell => cell.text),
    ['Stable system'],
  );
  assert.deepEqual(
    cells.filter(cell => cell.kind === 'message').map(cell => cell.text),
    ['Same output', 'Same output', 'Same output', 'Final output', 'Unique output', 'End output'],
  );
  assert.equal(cells.filter(cell => cell.kind === 'user' && cell.text === 'A').length, 1);
  assert.equal(cells.filter(cell => cell.kind === 'user' && cell.text === 'B').length, 3);
  assert.equal(cells.filter(cell => cell.kind === 'user' && cell.text === 'C').length, 1);
  assert.deepEqual(
    cells.filter(cell => cell.kind === 'context' && cell.text === 'Same output')
      .map(cell => cell.messageSource.inputIndex),
    [3],
  );
  assert.ok(inputCells.every(cell => cell.timeSeconds === null));
  assert.deepEqual(
    snapshot.requests?.map(request => request.model),
    ['model-a', 'model-b', 'model-c', 'model-d', 'model-e', 'model-f'],
  );
});

test('adjacent assistant replay uses stable text and tool-call projections across shape changes', async () => {
  const snapshot = await projectFixture('output-replay-shape-records.json');
  const contextCells = cellsOf(snapshot).filter(cell => cell.kind === 'context');

  assert.deepEqual(contextCells.map(cell => cell.text), ['Reasoned answer']);
  assert.equal(contextCells[0]?.messageSource.role, 'assistant');
  assert.equal(contextCells[0]?.messageSource.inputIndex, 3);
  assert.ok(contextCells.every(cell => cell.timeSeconds === null));
});

test('tool-ancestor branches do not reset the physical main context chain', async () => {
  const records = await fixtureRecords('agent-loop-records.json');
  const spans = spansOf(records);
  const first = spans.find(span => span.spanId === '1000000000000001');
  const second = spans.find(span => span.spanId === '2000000000000002');
  const branchTool = spans.find(span => span.spanId === '3000000000000003');
  const branch = spans.find(span => span.spanId === '4000000000000004');
  const final = spans.find(span => span.spanId === '5000000000000005');
  assert.ok(first && second && branchTool && branch && final);

  for (const span of spans) {
    setStringAttribute(span, 'openjiuwen.execution.subject.id', 'subagent-physical-1');
  }
  setStringAttribute(first, 'gen_ai.input.messages', JSON.stringify([
    structuredMessage('user', 'Root A'),
  ]));
  setStringAttribute(first, 'gen_ai.output.messages', JSON.stringify([
    structuredMessage('assistant', 'Root answer A'),
  ]));
  setStringAttribute(second, 'gen_ai.input.messages', JSON.stringify([
    structuredMessage('user', 'Root A'),
    structuredMessage('assistant', 'Root answer A'),
    structuredMessage('user', 'Before branch'),
  ]));
  setStringAttribute(second, 'gen_ai.output.messages', JSON.stringify([
    structuredMessage('assistant', 'Before branch answer'),
  ]));
  branch.parentSpanId = branchTool.spanId;
  setStringAttribute(branch, 'gen_ai.input.messages', JSON.stringify([
    structuredMessage('user', 'Nested multimodal branch'),
  ]));
  setStringAttribute(branch, 'gen_ai.output.messages', JSON.stringify([
    structuredMessage('assistant', 'Nested answer'),
  ]));
  setStringAttribute(final, 'gen_ai.input.messages', JSON.stringify([
    structuredMessage('user', 'Root A'),
    structuredMessage('assistant', 'Root answer A'),
    structuredMessage('user', 'Before branch'),
    structuredMessage('assistant', 'Before branch answer'),
    structuredMessage('user', 'After branch'),
  ]));
  setStringAttribute(final, 'gen_ai.output.messages', JSON.stringify([
    structuredMessage('assistant', 'Final answer'),
  ]));

  const inputs = cellsOf(projectOtelTrajectory(records)).filter(cell => (
    cell.kind === 'user' || cell.kind === 'context'
  ));
  assert.deepEqual(inputs.map(cell => cell.text), [
    'Root A',
    'Before branch',
    'Nested multimodal branch',
    'After branch',
  ]);
});

test('legacy tool-call aliases do not duplicate structured calls in one physical inference', async () => {
  const records = await fixtureRecords('core-contract-records.json');
  const inference = spansOf(records).find(span => span.spanId === '2000000000000000');
  assert.ok(inference);
  setStringAttribute(inference, 'gen_ai.output.messages', JSON.stringify([
    {
      role: 'assistant',
      parts: [
        { type: 'text', content: 'Core answer' },
        { type: 'tool_call', id: 'call-1', name: 'search', arguments: { q: 'next' } },
      ],
      tool_calls: [
        { id: 'call-1', name: 'search', arguments: { q: 'next' } },
      ],
    },
  ]));
  setStringAttribute(inference, 'gen_ai.tool_calls', JSON.stringify([
    { id: 'call-1', name: 'search', arguments: { q: 'next' } },
  ]));

  const assistant = cellsOf(projectOtelTrajectory(records)).find(cell => cell.kind === 'message');
  assert.ok(assistant);
  assert.equal(assistant.sourceBlocks.filter(block => block.type === 'tool-call').length, 1);
});

test('no-id calls keep physical multiplicity within one source and across output messages', async () => {
  const records = await fixtureRecords('core-contract-records.json');
  const inference = spansOf(records).find(span => span.spanId === '2000000000000000');
  assert.ok(inference);
  const call = { type: 'tool_call', name: 'search', arguments: { q: 'same' } };
  setStringAttribute(inference, 'gen_ai.output.messages', JSON.stringify([
    {
      role: 'assistant',
      parts: [call],
      tool_calls: [
        { name: 'search', arguments: { q: 'same' } },
        { name: 'search', arguments: { q: 'same' } },
      ],
    },
    { role: 'assistant', parts: [call] },
  ]));

  const assistant = cellsOf(projectOtelTrajectory(records)).find(cell => cell.kind === 'message');
  assert.ok(assistant);
  const calls = assistant.sourceBlocks.filter(block => block.type === 'tool-call');
  assert.equal(calls.length, 3);
});

test('provisional inference projects running lifecycle without a fabricated end time', async () => {
  const records = await fixtureRecords('standard-records.json');
  const inferenceRecord = records.find(record => record.resourceSpans.some(resource => (
    resource.scopeSpans.some(scope => scope.spans.some(span => (
      span.attributes.some(attribute => (
        attribute.key === 'openjiuwen.trajectory.record.kind'
          && attribute.value.stringValue === 'inference'
      ))
    )))
  )));
  assert.ok(inferenceRecord);
  const inferenceSpan = inferenceRecord.resourceSpans
    .flatMap(resource => resource.scopeSpans)
    .flatMap(scope => scope.spans)
    .find(span => span.attributes.some(attribute => (
      attribute.key === 'openjiuwen.trajectory.record.kind'
        && attribute.value.stringValue === 'inference'
    )));
  assert.ok(inferenceSpan);
  delete inferenceSpan.endTimeUnixNano;
  const identity = `${inferenceSpan.traceId}:${inferenceSpan.spanId}`;
  const snapshot = projectOtelTrajectory(records, {
    lifecycleByRecordId: new Map([[identity, 'running']]),
  });
  const assistant = cellsOf(snapshot).find(cell => cell.recordId === `${identity}:assistant`);
  const request = snapshot.requests?.find(candidate => candidate.status === 'running');

  assert.ok(assistant);
  assert.equal(assistant.status, 'running');
  assert.equal(assistant.timeSeconds, null);
  assert.equal(assistant.assistantMetrics.completedTime, null);
  assert.ok(request);
  assert.equal(request.completedAt, null);
});

test('every inference keeps an independent request identity inside a shared step', async () => {
  const records = await fixtureRecords('agent-loop-records.json');
  for (const record of records) {
    for (const resource of record.resourceSpans) {
      for (const scope of resource.scopeSpans) {
        for (const span of scope.spans) {
          if (span.name === 'llm.call') {
            span.attributes.push({
              key: 'openjiuwen.inference.id',
              value: { stringValue: span.spanId },
            });
          }
          for (const attribute of span.attributes) {
            if (attribute.key !== 'openjiuwen.step.number') continue;
            attribute.value = { intValue: '1' };
          }
        }
      }
    }
  }
  const snapshot = projectOtelTrajectory(records);
  const assistants = cellsOf(snapshot).filter(cell => cell.kind === 'message');
  const requestIds = snapshot.requests?.map(request => request.recordId) ?? [];

  assert.equal(snapshot.turns[0].groups[0].title, 'Step 1');
  assert.equal(new Set(requestIds).size, requestIds.length);
  assert.ok(requestIds.every(recordId => typeof recordId === 'string'));
  assert.ok(requestIds.every(recordId => recordId.includes(':inference:')));
  assert.deepEqual(
    assistants.map(cell => cell.requestRecordId),
    requestIds,
  );

  const spans = spansOf(records);
  const secondInference = spans.find(span => span.spanId === '2000000000000002');
  const tool = spans.find(span => span.spanId === '3000000000000003');
  assert.ok(secondInference && tool);
  tool.parentSpanId = secondInference.spanId;
  const ownedSnapshot = projectOtelTrajectory(records);
  const ownedTool = cellsOf(ownedSnapshot).find(cell => cell.kind === 'tool');
  const owner = cellsOf(ownedSnapshot).find(cell => (
    cell.kind === 'message' && cell.recordId?.includes(secondInference.spanId)
  ));
  assert.ok(ownedTool && owner);
  assert.equal(ownedTool.requestRecordId, owner.requestRecordId);
});
