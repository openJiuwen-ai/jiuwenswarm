// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyTrajectoryDetailRecords,
  changedTrajectoryUsageTraceIds,
  collectSubjectRefreshWindow,
  createTrajectoryOperationCoordinator,
  createTrajectoryTraceHintCoordinator,
  createTrajectoryWindowState,
  resetTrajectoryWindowState,
  sameTrajectoryUsageMap,
  selectSummariesNeedingLoad,
  shouldCatchUpAfterTrajectoryTerminalEvent,
  shouldCatchUpStreamFrames,
  stageTrajectoryChainPages,
  trajectoryContentMode,
} from '../node_modules/.cache/trajectory-window/trajectoryWindow.mjs';

test('unchanged cumulative usage does not require another trajectory publish', () => {
  const previous = new Map([
    ['trace-a\0inference-1', { input: 10, cacheRead: 2, output: 3, reasoning: 1, total: 13 }],
  ]);
  const same = new Map([
    ['trace-a\0inference-1', { input: 10, cacheRead: 2, output: 3, reasoning: 1, total: 13 }],
  ]);
  const changed = new Map([
    ['trace-a\0inference-1', { input: 10, cacheRead: 2, output: 4, reasoning: 1, total: 14 }],
  ]);

  assert.equal(sameTrajectoryUsageMap(previous, same), true);
  assert.equal(sameTrajectoryUsageMap(previous, changed), false);
  assert.equal(sameTrajectoryUsageMap(previous, new Map()), false);
});

test('changed usage names the traces whose projections it affects', () => {
  const usage = total => ({ input: 10, cacheRead: 0, output: total - 10, reasoning: 0, total });
  const previous = new Map([
    ['trace-a\0inference-1', usage(13)],
    ['trace-b\0inference-1', usage(20)],
    ['trace-c\0inference-1', usage(30)],
  ]);
  const next = new Map([
    ['trace-a\0inference-1', usage(13)],
    ['trace-b\0inference-1', usage(21)],
    ['trace-d\0inference-1', usage(40)],
  ]);

  assert.deepEqual(
    [...changedTrajectoryUsageTraceIds(previous, next)].sort(),
    ['trace-b', 'trace-c', 'trace-d'],
  );
});

test('one hint flight chases the highest revision that arrives while loading', async () => {
  const coordinator = createTrajectoryTraceHintCoordinator();
  const batches = [];
  let releaseFirstBatch;
  const firstBatch = new Promise(resolve => {
    releaseFirstBatch = resolve;
  });
  coordinator.enqueue('a'.repeat(32), 10);
  const flight = coordinator.drain(async (hints) => {
    batches.push([...hints]);
    if (batches.length === 1) await firstBatch;
  });

  coordinator.enqueue('a'.repeat(32), 11);
  coordinator.enqueue('a'.repeat(32), 13);
  coordinator.enqueue('b'.repeat(32), 2);
  const sharedFlight = coordinator.drain(async () => {
    assert.fail('a second loader must not create a concurrent flight');
  });
  assert.equal(sharedFlight, flight);

  releaseFirstBatch();
  await flight;
  assert.deepEqual(batches, [
    [['a'.repeat(32), 10]],
    [['a'.repeat(32), 13], ['b'.repeat(32), 2]],
  ]);
});

test('a drained hint coordinator accepts a later independent flight', async () => {
  const coordinator = createTrajectoryTraceHintCoordinator();
  const batches = [];
  coordinator.enqueue('c'.repeat(32), 1);
  await coordinator.drain(async hints => batches.push([...hints]));
  coordinator.enqueue('c'.repeat(32), 2);
  await coordinator.drain(async hints => batches.push([...hints]));

  assert.deepEqual(batches, [
    [['c'.repeat(32), 1]],
    [['c'.repeat(32), 2]],
  ]);
});

test('a paced hint flight bounds pulls while retaining the newest watermark', async () => {
  const coordinator = createTrajectoryTraceHintCoordinator();
  const batches = [];
  const paceWaiters = [];
  coordinator.enqueue('d'.repeat(32), 1);
  const flight = coordinator.drain(
    async hints => {
      batches.push([...hints]);
      if (batches.length === 1) {
        coordinator.enqueue('d'.repeat(32), 2);
        coordinator.enqueue('d'.repeat(32), 9);
      }
    },
    () => new Promise(resolve => paceWaiters.push(resolve)),
  );

  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(batches, [[['d'.repeat(32), 1]]]);
  assert.equal(paceWaiters.length, 1);
  paceWaiters.shift()();
  await flight;
  assert.deepEqual(batches, [
    [['d'.repeat(32), 1]],
    [['d'.repeat(32), 9]],
  ]);
});

test('a failed hint batch is requeued for the next recovery drain', async () => {
  const coordinator = createTrajectoryTraceHintCoordinator();
  coordinator.enqueue('e'.repeat(32), 7);
  await assert.rejects(
    coordinator.drain(async () => {
      throw new Error('temporary detail failure');
    }),
    /temporary detail failure/,
  );

  const recovered = [];
  await coordinator.drain(async hints => recovered.push([...hints]));
  assert.deepEqual(recovered, [[['e'.repeat(32), 7]]]);
});
import { readFileSync } from 'node:fs';
import { zipSync } from 'fflate';

import {
  getTrajectoryArchive,
  getTrajectoryCheckpoints,
  getTrajectorySessionUsage,
  getTrajectorySubjectRecords,
  listTrajectorySubjects,
} from '../node_modules/.cache/trajectory-window/trajectoryClient.mjs';
import {
  exitTrajectoryReplay,
  isTrajectoryArchiveFormatError,
  isTrajectoryArchiveLimitError,
  readTrajectoryArchive,
  shouldCatchUpTrajectory,
  TrajectoryArchiveLimitError,
  trajectoryReplayTeamMode,
} from '../node_modules/.cache/trajectory-window/trajectoryArchive.mjs';
import {
  formatTokenCount,
  liveElapsedSeconds,
} from '../node_modules/.cache/trajectory-window/record.mjs';
import {
  absorbSequencePage,
  createSequenceCache,
  rebuildRecord,
} from '../node_modules/.cache/trajectory-window/trajectorySequences.mjs';
import { groupTrajectorySubjects } from '../node_modules/.cache/trajectory-window/trajectorySubjects.mjs';

test('token counts use grouped thousands for readability', () => {
  assert.equal(formatTokenCount(48111), '48,111 tok');
  assert.equal(formatTokenCount(999), '999 tok');
  assert.equal(formatTokenCount(undefined), '—');
});

const SESSION_ID = 'session-1';
const STORE_EPOCH = 'epoch-1';

test('session terminal events catch up missed trajectory hints without treating start as terminal', () => {
  assert.equal(shouldCatchUpAfterTrajectoryTerminalEvent(
    'chat.processing_status',
    { session_id: SESSION_ID, is_processing: false },
    SESSION_ID,
  ), true);
  assert.equal(shouldCatchUpAfterTrajectoryTerminalEvent(
    'chat.processing_status',
    { session_id: SESSION_ID, is_processing: true },
    SESSION_ID,
  ), false);
  assert.equal(shouldCatchUpAfterTrajectoryTerminalEvent(
    'chat.final',
    { session_id: SESSION_ID },
    SESSION_ID,
  ), true);
  assert.equal(shouldCatchUpAfterTrajectoryTerminalEvent(
    'harness.session_finished',
    { payload: { event: { session_id: SESSION_ID } } },
    SESSION_ID,
  ), true);
  assert.equal(shouldCatchUpAfterTrajectoryTerminalEvent(
    'chat.error',
    { session_id: 'another-session' },
    SESSION_ID,
  ), false);
});

function hexId(value, width) {
  return value.toString(16).padStart(width, '0');
}

function summary(value, revision = value) {
  return {
    subject_id: `subject-${value}`,
    display_name: `Subject ${value}`,
    kind: 'subagent',
    parent_id: 'main',
    record_count: 1,
    trace_count: 1,
    first_start_time_unix_nano: String(value * 10),
    last_observed_time_unix_nano: String(value * 10 + 1),
    first_revision: 1,
    revision,
    has_error: false,
    running: false,
  };
}



function otlpRecord(traceId, spanId) {
  return {
    resourceSpans: [{
      scopeSpans: [{
        spans: [{
          traceId,
          spanId,
          name: `span-${spanId}`,
        }],
      }],
    }],
  };
}

function backendArchiveRecord({
  traceId = hexId(91_000, 32),
  spanId = hexId(1, 16),
  lifecycle = 'final',
  changeSeq = '9007199254740993',
  subjectId = 'main',
  raw = otlpRecord(traceId, spanId),
  rawValid = true,
  sequences,
} = {}) {
  return {
    type: 'record',
    record_id: `${traceId}:${spanId}`,
    trace_id: traceId,
    span_id: spanId,
    parent_span_id: null,
    subject_id: subjectId,
    record_revision: 3,
    lifecycle,
    operation: 'upsert',
    change_seq: changeSeq,
    start_time_unix_nano: '1000000000',
    observed_time_unix_nano: '1500000000',
    end_time_unix_nano: lifecycle === 'running' ? '0' : '2000000000',
    session_id: SESSION_ID,
    request_id: null,
    run_id: null,
    agent_mode: 'agent',
    schema_version: '1',
    source: 'openjiuwen',
    created_at: 1,
    update_kind: lifecycle === 'final' ? 'span_end' : 'stream',
    raw_sha256: '0'.repeat(64),
    raw_size_bytes: 256,
    raw_valid: rawValid,
    raw_json: typeof raw === 'string' ? raw : JSON.stringify(raw),
    ...(sequences === undefined ? {} : { sequences }),
  };
}

function archiveHeader(overrides = {}) {
  return {
    type: 'header',
    format: 'openjiuwen.trajectory.archive',
    archive_version: 3,
    session_id: SESSION_ID,
    exported_at: '2026-08-21T00:00:00Z',
    store_epoch: STORE_EPOCH,
    revision: '9007199254740995',
    stream_frames: false,
    ...overrides,
  };
}

/** Frame body lines the way the exporter does: header first, end counting the rest. */
function archiveLines(body, headerOverrides = {}) {
  const lines = [archiveHeader(headerOverrides), ...body];
  return [
    ...lines,
    {
      type: 'end',
      records: body.filter(line => line.type === 'record').length,
      lines: lines.length,
    },
  ];
}

function jsonlBytes(lines) {
  return new TextEncoder().encode(lines.map(line => `${JSON.stringify(line)}\n`).join(''));
}

function zipBytes(jsonl, extraEntries = {}) {
  return zipSync({ ...extraEntries, 'trajectory.jsonl': [jsonl, { level: 6 }] });
}

/** A file whose stream hands out the bytes cut exactly where *cuts* say. */
function chunkedSource(bytes, cuts = []) {
  const bounds = [...new Set([0, ...cuts.filter(cut => cut > 0 && cut < bytes.length), bytes.length])]
    .sort((left, right) => left - right);
  return {
    size: bytes.length,
    stream: () => new ReadableStream({
      start(controller) {
        for (let index = 1; index < bounds.length; index += 1) {
          controller.enqueue(bytes.slice(bounds[index - 1], bounds[index]));
        }
        controller.close();
      },
    }),
  };
}

function seededCuts(length, count, seed) {
  let state = seed;
  const cuts = [];
  for (let index = 0; index < count; index += 1) {
    state = (state * 1103515245 + 12345) % 2147483648;
    cuts.push(1 + (state % Math.max(1, length - 1)));
  }
  return cuts;
}

function replayGroups(view) {
  return groupTrajectorySubjects(view.records, view.rawRecords, view.lifecycleByRecordId);
}

test('frontend replays the backend Archive v3 record lines', async () => {
  const finalRecord = backendArchiveRecord();
  const invalidRecord = backendArchiveRecord({
    spanId: hexId(2, 16),
    lifecycle: 'abandoned',
    changeSeq: '9007199254740994',
    raw: '{invalid-json',
    rawValid: false,
  });
  const binaryRecord = backendArchiveRecord({
    spanId: hexId(3, 16),
    changeSeq: '9007199254740995',
    rawValid: false,
  });
  delete binaryRecord.raw_json;
  binaryRecord.raw_json_base64 = Buffer.from([0x7b, 0xff, 0xfe, 0x7d]).toString('base64');
  const replay = await readTrajectoryArchive(chunkedSource(jsonlBytes(archiveLines(
    [finalRecord, invalidRecord, binaryRecord],
  ))));
  const { view } = replay;

  assert.equal(replay.container, 'jsonl');
  assert.equal(replay.header.revision, '9007199254740995');
  assert.equal(view.traceCount, 1);
  assert.equal(view.records.length, 1);
  assert.equal(view.rawRecords.length, 3);
  // Change sequences beyond the safe integer range cannot be ingest cursors,
  // so replay numbers the records in the order it read them.
  assert.deepEqual(view.rawRecords.map(record => record.ingest_seq), [1, 2, 3]);
  assert.equal(view.rawRecords[0].change_seq, undefined);
  assert.equal(view.lifecycleByRecordId.get(invalidRecord.record_id), 'error');
  assert.equal(view.rawDataByRecordId.get(invalidRecord.record_id), '{invalid-json');
  assert.equal(view.rawDataByRecordId.get(binaryRecord.record_id), '{��}');
  assert.equal(view.rawDataByRecordId.has(finalRecord.record_id), false);
  assert.equal(view.invalidRecordSeen, true);
});

test('archive reader refuses earlier versions, foreign headers and non-string cursors', async () => {
  const record = backendArchiveRecord();
  const read = lines => readTrajectoryArchive(chunkedSource(jsonlBytes(lines)));
  const versionTwo = {
    format: 'openjiuwen.trajectory.archive',
    archive_version: 2,
    session_id: SESSION_ID,
    records: [],
  };

  await assert.rejects(read([versionTwo]), /version 2 is no longer supported/);
  await assert.rejects(read(archiveLines([record], { revision: 42 })), /not supported/);
  await assert.rejects(read(archiveLines([record], { type: undefined })), /not supported/);
  await assert.rejects(
    read(archiveLines([{ ...record, change_seq: 42 }])),
    /line 2 is an invalid record/,
  );
  await assert.rejects(
    readTrajectoryArchive(chunkedSource(new TextEncoder().encode('not json\n'))),
    /not supported/,
  );
});

test('archive reader reports files that are not trajectory archives as format errors', async () => {
  const encoder = new TextEncoder();
  const invalidFiles = [
    encoder.encode('{"broken": \n'),
    encoder.encode('plain text notes\nsecond line\n'),
    new Uint8Array(0),
    new Uint8Array([0xff, 0xfe, 0xfd, 0x0a]),
    zipSync({ 'notes.txt': encoder.encode('hello') }),
  ];

  for (const bytes of invalidFiles) {
    await assert.rejects(
      readTrajectoryArchive(chunkedSource(bytes)),
      error => isTrajectoryArchiveFormatError(error) && !isTrajectoryArchiveLimitError(error),
    );
  }
});

test('archive replay keeps the mode its records were exported in', async () => {
  const read = records => readTrajectoryArchive(chunkedSource(jsonlBytes(archiveLines(records))));
  const leader = backendArchiveRecord({ changeSeq: '1' });
  const member = backendArchiveRecord({ spanId: hexId(2, 16), changeSeq: '2', subjectId: 'member:alice' });
  const withMode = (record, agentMode) => ({ ...record, agent_mode: agentMode });
  const withoutMode = record => {
    const { agent_mode: _omitted, ...rest } = record;
    return rest;
  };

  assert.equal((await read([withMode(leader, 'agent.code.normal')])).mode, 'agent');
  assert.equal((await read([withMode(leader, 'team'), withMode(member, 'team')])).mode, 'team');
  assert.equal((await read([withMode(leader, 'team.work.plan')])).mode, 'team');
  assert.equal((await read([withMode(leader, 'agent.work.normal'), withMode(member, 'team')])).mode, 'team');
  assert.equal((await read([withoutMode(leader)])).mode, null);
  assert.equal((await read([])).mode, null);

  // A stated mode wins over the hosting session; only an unstated one follows it.
  assert.equal(trajectoryReplayTeamMode('agent', true), false);
  assert.equal(trajectoryReplayTeamMode('team', false), true);
  assert.equal(trajectoryReplayTeamMode(null, true), true);
  assert.equal(trajectoryReplayTeamMode(null, false), false);
});

test('archive reader rejects records out of commit order and unknown line types', async () => {
  const first = backendArchiveRecord({ changeSeq: '5' });
  const second = backendArchiveRecord({ spanId: hexId(2, 16), changeSeq: '4' });
  const read = lines => readTrajectoryArchive(chunkedSource(jsonlBytes(lines)));

  await assert.rejects(read(archiveLines([first, second])), /out of commit order/);
  await assert.rejects(read(archiveLines([first, { ...first, change_seq: '6' }])), /duplicate/);
  await assert.rejects(read(archiveLines([{ type: 'frame', text: 'w' }])), /unsupported type/);
});

test('an addressed archive rebuilds its references from lines defined before them', async () => {
  const traceId = hexId(91_000, 32);
  const spanId = hexId(1, 16);
  const raw = otlpRecord(traceId, spanId);
  raw.resourceSpans[0].scopeSpans[0].spans[0].attributes = [
    { key: 'gen_ai.input.messages', value: { stringValue: `@oj-seq:1:${'d'.repeat(64)}:2` } },
  ];
  const replay = await readTrajectoryArchive(chunkedSource(jsonlBytes(archiveLines([
    { type: 'blob', hash: 'e1', text: JSON.stringify({ role: 'user', parts: [] }) },
    { type: 'sequence', hash: 'c'.repeat(64), prev: null, blob: 'e1', depth: 1 },
    { type: 'blob', hash: 'e2', text: JSON.stringify({ role: 'assistant', parts: [] }) },
    { type: 'sequence', hash: 'd'.repeat(64), prev: 'c'.repeat(64), blob: 'e2', depth: 2 },
    backendArchiveRecord({
      raw,
      sequences: { 'gen_ai.input.messages': { hash: 'd'.repeat(64), depth: 2 } },
    }),
  ]))));
  const record = replay.view.rawRecords[0];
  const rebuilt = record.otlp.resourceSpans[0].scopeSpans[0].spans[0].attributes
    .find(attribute => attribute.key === 'gen_ai.input.messages').value.stringValue;

  assert.equal(record.incomplete_sequences, undefined);
  assert.deepEqual(JSON.parse(rebuilt).map(message => message.role), ['user', 'assistant']);
});

test('a reference the archive never defines stays marked as unresolved', async () => {
  const traceId = hexId(91_000, 32);
  const spanId = hexId(1, 16);
  const raw = otlpRecord(traceId, spanId);
  raw.resourceSpans[0].scopeSpans[0].spans[0].attributes = [
    { key: 'gen_ai.input.messages', value: { stringValue: `@oj-seq:1:${'d'.repeat(64)}:1` } },
  ];
  const replay = await readTrajectoryArchive(chunkedSource(jsonlBytes(archiveLines([
    { type: 'sequence', hash: 'd'.repeat(64), prev: null, blob: 'lost', depth: 1 },
    backendArchiveRecord({
      raw,
      sequences: { 'gen_ai.input.messages': { hash: 'd'.repeat(64), depth: 1 } },
    }),
  ]))));

  assert.deepEqual(replay.view.rawRecords[0].incomplete_sequences, ['gen_ai.input.messages']);
});

const FIXTURE_JSONL = readFileSync(new URL('./fixtures/trajectory-archive-subjects.jsonl', import.meta.url));
const FIXTURE_ZIP = readFileSync(new URL('./fixtures/trajectory-archive-subjects.trajectory.zip', import.meta.url));

/**
 * What the live panel holds for the fixture session.
 *
 * The fixture was written by the backend exporter from a real store, so its
 * lines state exactly the rows a live reader pages through. Each subject's
 * records are fed in commit order as one detail page carrying the chains and
 * content it refers to, the way the detail endpoint answers, and are rebuilt
 * and applied by the same helpers the panel uses.
 */
function liveFixtureView() {
  const lines = new TextDecoder().decode(FIXTURE_JSONL).trim().split('\n').map(line => JSON.parse(line));
  const blobs = {};
  const nodes = new Map();
  const pages = new Map();
  const usage = new Map();
  for (const line of lines) {
    if (line.type === 'blob') blobs[line.hash] = line.text;
    if (line.type === 'sequence') nodes.set(line.hash, line);
    if (line.type === 'usage') {
      usage.set(`${line.trace_id}\0${line.inference_id}`, line.cumulative_usage);
    }
    if (line.type !== 'record') continue;
    const sequences = {};
    for (const reference of Object.values(line.sequences ?? {})) {
      const elements = [];
      for (let node = nodes.get(reference.hash); node !== undefined; node = nodes.get(node.prev)) {
        elements.unshift(node.blob);
      }
      sequences[reference.hash] = elements;
    }
    const page = pages.get(line.subject_id) ?? { records: [], sequences: {} };
    page.records.push({
      ingest_seq: Number(line.change_seq),
      otlp: JSON.parse(line.raw_json),
      raw_valid: line.raw_valid,
      record_id: line.record_id,
      record_revision: line.record_revision,
      lifecycle: line.lifecycle,
      operation: line.operation,
      change_seq: Number(line.change_seq),
      observed_time_unix_nano: line.observed_time_unix_nano,
      trace_id: line.trace_id,
      span_id: line.span_id,
      raw_size_bytes: line.raw_size_bytes,
      ...(line.sequences === undefined ? {} : { sequences: line.sequences }),
    });
    Object.assign(page.sequences, sequences);
    pages.set(line.subject_id, page);
  }
  const cache = createSequenceCache();
  const buckets = [];
  for (const [subjectId, page] of pages) {
    absorbSequencePage(cache, { sequences: page.sequences, blobs });
    const revision = page.records[page.records.length - 1].ingest_seq;
    buckets.push(applyTrajectoryDetailRecords(undefined, {
      schema_version: 1,
      session_id: 'browser-subject-fixture',
      subject_id: subjectId,
      revision,
      reset: false,
      records: page.records.map(record => rebuildRecord(record, cache)),
      has_more: false,
      next_since_revision: revision,
    }).bucket);
  }
  return {
    records: buckets.flatMap(bucket => [...bucket.records.values()]),
    rawRecords: buckets.flatMap(bucket => [...bucket.rawRecords.values()]),
    lifecycleByRecordId: new Map(buckets.flatMap(bucket => [...bucket.versions].map(
      ([identity, version]) => [identity, version.lifecycle],
    ))),
    usage,
  };
}

test('zip and JSONL archives cut at any byte replay exactly the live view', async () => {
  const live = liveFixtureView();
  const liveGroups = replayGroups(live);
  assert.ok(liveGroups.groups.length >= 2);
  assert.ok(live.rawRecords.some(record => record.sequences !== undefined));
  assert.ok(live.rawRecords.every(record => record.incomplete_sequences === undefined));
  const zipped = zipBytes(new Uint8Array(FIXTURE_JSONL));
  const sources = [
    ['jsonl', FIXTURE_JSONL, []],
    ['jsonl', FIXTURE_JSONL, Array.from({ length: FIXTURE_JSONL.length }, (_, index) => index)],
    ['jsonl', FIXTURE_JSONL, seededCuts(FIXTURE_JSONL.length, 40, 7)],
    ['zip', zipped, []],
    ['zip', zipped, [1, 2, 3, 5, 31, 32, 33]],
    ['zip', zipped, Array.from({ length: zipped.length }, (_, index) => index)],
    ['zip', FIXTURE_ZIP, seededCuts(FIXTURE_ZIP.length, 25, 11)],
    ['zip', FIXTURE_ZIP, [2]],
  ];
  for (const [container, bytes, cuts] of sources) {
    const progress = [];
    const replay = await readTrajectoryArchive(
      chunkedSource(new Uint8Array(bytes), cuts),
      update => progress.push(update),
    );
    assert.equal(replay.container, container);
    assert.equal(replay.header.session_id, 'browser-subject-fixture');
    assert.deepEqual(replayGroups(replay.view), liveGroups);
    assert.deepEqual(replay.sessionCumulativeUsageByRequestIdentity, live.usage);
    assert.deepEqual(progress.at(-1), {
      bytesRead: bytes.length,
      totalBytes: bytes.length,
      records: live.rawRecords.length,
    });
  }
});

test('the backend zip writer produces an archive the streaming reader accepts', async () => {
  // Written by the gateway's own zip writer: the local header states its sizes
  // through a ZIP64 extra field rather than a trailing data descriptor.
  assert.deepEqual([...FIXTURE_ZIP.subarray(0, 4)], [0x50, 0x4b, 0x03, 0x04]);
  assert.equal(FIXTURE_ZIP.readUInt16LE(6) & 0x08, 0);
  assert.equal(FIXTURE_ZIP.readUInt32LE(18), 0xffffffff);
  const replay = await readTrajectoryArchive(chunkedSource(new Uint8Array(FIXTURE_ZIP)));

  assert.equal(replay.view.rawRecords.length, 4);
  assert.equal(replay.sessionCumulativeUsageByRequestIdentity.size, 2);
});

test('a truncated archive is refused rather than replayed in part', async () => {
  const jsonl = new Uint8Array(FIXTURE_JSONL);
  const withoutEnd = jsonl.slice(0, jsonl.lastIndexOf(0x7b));
  const zipped = zipBytes(jsonl);
  const miscounted = jsonlBytes(archiveLines([backendArchiveRecord()]).map(line => (
    line.type === 'end' ? { ...line, records: 2 } : line
  )));

  await assert.rejects(readTrajectoryArchive(chunkedSource(withoutEnd)), /truncated: it has no end line/);
  await assert.rejects(
    readTrajectoryArchive(chunkedSource(jsonl.slice(0, Math.floor(jsonl.length / 2)))),
    /line \d+ is not valid JSON/,
  );
  await assert.rejects(
    readTrajectoryArchive(chunkedSource(zipped.slice(0, Math.floor(zipped.length / 2)))),
    /truncated/,
  );
  await assert.rejects(
    readTrajectoryArchive(chunkedSource(new Uint8Array(FIXTURE_ZIP).slice(0, 700))),
    /truncated/,
  );
  await assert.rejects(readTrajectoryArchive(chunkedSource(miscounted)), /does not match the counts/);
  await assert.rejects(readTrajectoryArchive(chunkedSource(new Uint8Array(0))), /empty/);
});

test('archive reader enforces its line, size, entry and record limits', async () => {
  const jsonl = new Uint8Array(FIXTURE_JSONL);
  const limitOf = async (source, limits) => {
    try {
      await readTrajectoryArchive(source, undefined, { limits });
    } catch (error) {
      assert.ok(error instanceof TrajectoryArchiveLimitError, String(error));
      return error.limit;
    }
    assert.fail('the archive was accepted');
  };

  assert.equal(await limitOf(chunkedSource(jsonl, [7, 900]), { maxLineLength: 256 }), 'line-length');
  assert.equal(
    await limitOf(chunkedSource(zipBytes(jsonl), [100]), { maxUncompressedBytes: 4096 }),
    'uncompressed-size',
  );
  assert.equal(
    await limitOf(
      chunkedSource(zipBytes(jsonl, { 'a.txt': new Uint8Array(3), 'b.txt': new Uint8Array(3) })),
      { maxEntries: 2 },
    ),
    'entry-count',
  );
  assert.equal(await limitOf(chunkedSource(jsonl), { maxRecords: 3 }), 'record-count');
  await assert.rejects(
    readTrajectoryArchive(chunkedSource(zipSync({ 'other.jsonl': jsonl }))),
    /no trajectory\.jsonl entry/,
  );
});

test('checkpoint lines become view seeds and are accepted only before the first record', async () => {
  const checkpoint = {
    type: 'checkpoint',
    subject_id: 'main',
    boundary_turn_id: 'id:turn-2',
    boundary_change_seq: '9',
    state: {
      version: 1,
      subject: {
        display_name: 'Main Agent',
        kind: 'main_agent',
        parent_id: null,
        session_id: null,
        projected: true,
        first_observed_time_unix_nano: '5',
      },
      turns: { max_number: 2, unnumbered: 1, trace_turn_ids: { ['a'.repeat(32)]: ['turn-2'] } },
      requests: { 'session|main': 3 },
      lineage: [],
      v2: {
        main: {
          event_count: 4,
          sequence_epoch: 'epoch-1',
          next_sequence: 5,
          blocked: false,
          window: { id: 'window-4', messages: { hash: null, depth: 0 } },
          epoch_baseline_base: null,
          held: null,
        },
      },
      usage: { input: 10 },
    },
  };
  const replay = await readTrajectoryArchive(
    chunkedSource(jsonlBytes(archiveLines([checkpoint, backendArchiveRecord()]))),
  );

  const projection = replay.checkpoints.projections.get('main');
  assert.deepEqual(projection.turns, {
    maxNumber: 2,
    unnumbered: 1,
    traceTurnIds: new Map([['a'.repeat(32), ['turn-2']]]),
  });
  assert.deepEqual(projection.requestOffsets, new Map([['session|main', 3]]));
  assert.equal(replay.checkpoints.retiredSubjects.get('main').firstObservedTimeUnixNano, '5');
  assert.deepEqual(replay.checkpoints.v2Seeds.get('main'), {
    eventCount: 4,
    blocked: false,
    sequenceEpoch: 'epoch-1',
    nextSequence: 5,
    window: { id: 'window-4', messages: [] },
  });
  await assert.rejects(
    readTrajectoryArchive(chunkedSource(jsonlBytes(archiveLines([backendArchiveRecord(), checkpoint])))),
    /checkpoint after the first record/,
  );
  const unresolved = {
    ...checkpoint,
    state: {
      ...checkpoint.state,
      v2: { main: { ...checkpoint.state.v2.main, window: { id: 'window-4', messages: { hash: 'f'.repeat(64), depth: 1 } } } },
    },
  };
  await assert.rejects(
    readTrajectoryArchive(chunkedSource(jsonlBytes(archiveLines([unresolved, backendArchiveRecord()])))),
    /content is missing/,
  );
});

test('archive export client downloads the backend session archive as a blob', async () => {
  const originalFetch = globalThis.fetch;
  const zipped = zipBytes(new Uint8Array(FIXTURE_JSONL));
  let requestedUrl = '';
  globalThis.fetch = async (input) => {
    requestedUrl = String(input);
    return new Response(zipped, { status: 200, headers: { 'Content-Type': 'application/zip' } });
  };
  try {
    const blob = await getTrajectoryArchive('session / one');
    assert.deepEqual(new Uint8Array(await blob.arrayBuffer()), zipped);
    assert.equal((await readTrajectoryArchive(blob)).view.rawRecords.length, 4);
    assert.match(requestedUrl, /\/sessions\/session%20%2F%20one\/archive$/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('only a recovered websocket connection triggers revision catch-up', () => {
  assert.equal(shouldCatchUpTrajectory('reconnecting', 'ready'), true);
  assert.equal(shouldCatchUpTrajectory('closed', 'ready'), true);
  assert.equal(shouldCatchUpTrajectory('connecting', 'ready'), false);
  assert.equal(shouldCatchUpTrajectory('ready', 'ready'), false);
  assert.equal(shouldCatchUpTrajectory('ready', 'reconnecting'), false);
});

test('exiting browser-only replay requests one live revision catch-up', async () => {
  const archive = await readTrajectoryArchive(chunkedSource(jsonlBytes(
    archiveLines([backendArchiveRecord()]),
  )));

  assert.deepEqual(exitTrajectoryReplay(archive), {
    archive: null,
    catchUpLiveRevision: true,
  });
  assert.deepEqual(exitTrajectoryReplay(null), {
    archive: null,
    catchUpLiveRevision: false,
  });
});

function projectedRecord(ingestSeq, traceValue = 70_000) {
  const traceId = hexId(traceValue, 32);
  const spanId = hexId(ingestSeq, 16);
  return {
    ingest_seq: ingestSeq,
    trace_id: traceId,
    span_id: spanId,
    raw_size_bytes: 256,
    otlp: otlpRecord(traceId, spanId),
    raw_valid: true,
  };
}

function detailPage({
  revision,
  records,
  hasMore,
  nextSinceRevision,
}) {
  return {
    schema_version: 1,
    session_id: SESSION_ID,
    trace_id: hexId(70_000, 32),
    revision,
    reset: false,
    records,
    has_more: hasMore,
    next_since_revision: nextSinceRevision,
  };
}



test('revision feed finds a late update for an already loaded old trace', async () => {
  const oldTrace = summary(80_000, 10);
  const unchangedTrace = summary(80_001, 20);
  const newTrace = summary(80_002, 1);
  const loaded = new Map([
    [oldTrace.subject_id, oldTrace.revision],
    [unchangedTrace.subject_id, unchangedTrace.revision],
  ]);
  const requestedFloors = [];
  const controller = new AbortController();
  const revisions = await collectSubjectRefreshWindow(
    10,
    STORE_EPOCH,
    controller.signal,
    async (afterRevision) => {
      requestedFloors.push(afterRevision);
      return {
        schema_version: 1,
        session_id: SESSION_ID,
        store_epoch: STORE_EPOCH,
        items: [{ ...oldTrace, revision: 12 }, newTrace, unchangedTrace],
        watermark: 30,
      };
    },
  );

  assert.ok(revisions);
  assert.deepEqual(requestedFloors, [10]);
  assert.equal(revisions.watermark, 30);
  const selected = selectSummariesNeedingLoad(loaded, revisions.summaries);
  assert.deepEqual(
    selected.map(item => [item.subject_id, item.revision]),
    [[oldTrace.subject_id, 12], [newTrace.subject_id, 1]],
  );
});


test('trajectory client reads session cumulative usage by physical request identity', async () => {
  const originalFetch = globalThis.fetch;
  const traceId = 'a'.repeat(32);
  try {
    globalThis.fetch = async input => {
      assert.match(String(input), /\/sessions\/session-1\/usage$/);
      return new Response(JSON.stringify({
        schema_version: 1,
        session_id: SESSION_ID,
        store_epoch: STORE_EPOCH,
        scope: 'session',
        items: [{
          trace_id: traceId,
          inference_id: 'inference-1',
          subject_id: 'main',
          start_time_unix_nano: '10',
          usage: { input: 2, output: 1, total: 3 },
          cumulative_usage: { input: 5, output: 3, total: 8 },
        }],
      }), { status: 200 });
    };

    const usage = await getTrajectorySessionUsage(SESSION_ID);

    assert.equal(usage.scope, 'session');
    assert.equal(usage.items[0].trace_id, traceId);
    assert.deepEqual(usage.items[0].cumulative_usage, {
      input: 5,
      output: 3,
      total: 8,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('trajectory client reads retention checkpoints with the content they refer to', async () => {
  const originalFetch = globalThis.fetch;
  try {
    const body = {
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: STORE_EPOCH,
      checkpoints: [{ subject_id: 'main', boundary_turn_id: 'id:turn-1', boundary_change_seq: '4', state: {} }],
      sequences: { ['a'.repeat(64)]: ['b'.repeat(64)] },
      blobs: { ['b'.repeat(64)]: '{}' },
    };
    globalThis.fetch = async (input) => {
      assert.match(String(input), /\/sessions\/session-1\/checkpoints$/);
      return new Response(JSON.stringify(body), { status: 200 });
    };
    assert.deepEqual(await getTrajectoryCheckpoints(SESSION_ID), body);

    globalThis.fetch = async () => new Response(JSON.stringify({ ...body, checkpoints: [null] }), { status: 200 });
    await assert.rejects(getTrajectoryCheckpoints(SESSION_ID), /checkpoint response is invalid/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('trajectory client accepts the additive provisional detail contract', async () => {
  const originalFetch = globalThis.fetch;
  const record = projectedRecord(11);
  const span = record.otlp.resourceSpans[0].scopeSpans[0].spans[0];
  span.startTimeUnixNano = '1000000000';
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({
      schema_version: 1,
      session_id: SESSION_ID,
      subject_id: 'main',
      revision: 44,
      reset: false,
      records: [{
        ...record,
        change_seq: 44,
        record_id: `${record.trace_id}:${record.span_id}`,
        record_revision: 2,
        lifecycle: 'provisional',
        operation: 'upsert',
        observed_time_unix_nano: '1500000000',
      }],
      has_more: false,
      next_since_revision: 44,
    }), { status: 200 });
    const detail = await getTrajectorySubjectRecords(SESSION_ID, 'main');

    assert.equal(detail.records[0].lifecycle, 'provisional');
    assert.equal(detail.records[0].record_revision, 2);
    assert.equal(detail.records[0].change_seq, 44);
    assert.equal(detail.records[0].record_id, `${record.trace_id}:${record.span_id}`);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('trajectory client rejects missing, blank, and oversized epochs plus missing reset', async () => {
  const originalFetch = globalThis.fetch;
  const state = populatedWindowState();
  const invalidPayloads = [
    {
      schema_version: 1,
      session_id: SESSION_ID,
      items: [],
      next_cursor: null,
      revision_cursor: 'revision-baseline',
    },
    {
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: '   ',
      items: [],
      next_cursor: null,
      revision_cursor: 'revision-baseline',
    },
    {
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: 'x'.repeat(513),
      items: [],
      next_cursor: null,
      revision_cursor: 'revision-baseline',
    },
    {
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: STORE_EPOCH,
      items: [],
      next_cursor: 'revision-baseline',
      watermark: 'revision-baseline',
      has_more: false,
    },
  ];
  try {
    for (let index = 0; index < invalidPayloads.length; index += 1) {
      globalThis.fetch = async () => new Response(JSON.stringify(invalidPayloads[index]), {
        status: 200,
      });
      const request = index < 3
        ? listTrajectorySubjects(SESSION_ID)
        : listTrajectorySubjects(SESSION_ID, {
          afterRevision: 'revision-baseline',
        });
      await assert.rejects(request, error => error.code === 'INVALID_RESPONSE');
      assert.equal(state.buckets.size, 1);
      assert.equal(state.storeEpoch, STORE_EPOCH);
      assert.equal(state.watermark, 9);
      assert.notEqual(state.rawSelection, '');
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

function populatedWindowState() {
  const state = createTrajectoryWindowState();
  const record = projectedRecord(1);
  const identity = `${record.trace_id}:${record.span_id}`;
  state.buckets.set(record.trace_id, {
    revision: 9,
    records: new Map([[identity, record.otlp]]),
    rawRecords: new Map([[identity, record]]),
  });
  state.storeEpoch = STORE_EPOCH;
  state.watermark = 9;
  state.listWindowInitialized = true;
  state.rawSelection = identity;
  return state;
}

function assertWindowReset(state) {
  assert.equal(state.buckets.size, 0);
  assert.equal(state.storeEpoch, null);
  assert.equal(state.watermark, 0);
  assert.equal(state.listWindowInitialized, false);
  assert.equal(state.rawSelection, '');
}


test('partial retention epoch reset removes buckets, the watermark, and raw selection', async () => {
  const controller = new AbortController();
  const result = await collectSubjectRefreshWindow(
    9,
    STORE_EPOCH,
    controller.signal,
    async () => ({
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: 'epoch-after-partial-retention',
      items: [],
      watermark: 0,
    }),
  );
  const state = populatedWindowState();

  assert.deepEqual(result, {
    reset: true,
    storeEpoch: 'epoch-after-partial-retention',
  });
  resetTrajectoryWindowState(state);
  assertWindowReset(state);
});

test('an eligible-to-mixed epoch change yields reset and removes old trace state', async () => {
  const controller = new AbortController();
  const result = await collectSubjectRefreshWindow(
    9,
    STORE_EPOCH,
    controller.signal,
    async () => ({
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: 'epoch-mixed',
      items: [],
      watermark: 0,
    }),
  );
  const state = populatedWindowState();

  assert.deepEqual(result, { reset: true, storeEpoch: 'epoch-mixed' });
  resetTrajectoryWindowState(state);
  assertWindowReset(state);
});



test('a hinted refresh pages frames only when the hint is ahead of what is held', () => {
  assert.equal(shouldCatchUpStreamFrames('ifBehind', 40, 41), true);
  assert.equal(shouldCatchUpStreamFrames('ifBehind', 41, 41), false);
  assert.equal(shouldCatchUpStreamFrames('ifBehind', 50, 41), false);
  // Rebuilds, reconnects and terminal events cannot trust a hint.
  assert.equal(shouldCatchUpStreamFrames('always', 50, 41), true);
});

test('generation invalidation marks earlier operations stale', () => {
  const coordinator = createTrajectoryOperationCoordinator();
  const before = coordinator.currentGeneration();

  const after = coordinator.invalidate();

  assert.equal(coordinator.isCurrent(before), false);
  assert.equal(coordinator.isCurrent(after), true);
  assert.equal(coordinator.currentGeneration(), after);
});


test('head and revision windows deduplicate one trace at its highest revision', () => {
  const target = summary(90_000, 3);
  const selected = selectSummariesNeedingLoad(
    new Map([[target.trace_id, 2]]),
    [target, target],
    [{ ...target, revision: 4 }, { ...target, revision: 4 }],
  );

  assert.equal(selected.length, 1);
  assert.equal(selected[0].revision, 4);
});

test('detail pages stage 1001 records and retain an oversize raw descriptor', async () => {
  const current = {
    revision: 0,
    records: new Map(),
    rawRecords: new Map(),
  };
  const firstRecords = Array.from({ length: 1000 }, (_, index) => projectedRecord(index + 1));
  const traceId = hexId(70_000, 32);
  const oversize = {
    ingest_seq: 1001,
    trace_id: traceId,
    span_id: hexId(1001, 16),
    raw_size_bytes: 8_388_608,
    otlp: null,
    raw_valid: null,
    projection_omitted: 'record_too_large',
  };
  let resolveSecondPage;
  let secondPageStarted;
  const secondPageStartedPromise = new Promise(resolve => {
    secondPageStarted = resolve;
  });
  const secondPagePromise = new Promise(resolve => {
    resolveSecondPage = resolve;
  });
  const controller = new AbortController();
  let calls = 0;
  const stagedPromise = stageTrajectoryChainPages(
    current,
    controller.signal,
    async (sinceRevision) => {
      calls += 1;
      if (calls === 1) {
        assert.equal(sinceRevision, 0);
        return detailPage({
          revision: 1001,
          records: firstRecords,
          hasMore: true,
          nextSinceRevision: 1000,
        });
      }
      assert.equal(sinceRevision, 1000);
      secondPageStarted();
      return secondPagePromise;
    },
  );

  await secondPageStartedPromise;
  assert.equal(current.records.size, 0);
  assert.equal(current.rawRecords.size, 0);
  assert.equal(current.revision, 0);
  resolveSecondPage(detailPage({
    revision: 1001,
    records: [oversize],
    hasMore: false,
    nextSinceRevision: 1001,
  }));
  const staged = await stagedPromise;

  assert.ok(staged);
  assert.equal(staged.bucket.revision, 1001);
  assert.equal(staged.bucket.records.size, 1000);
  assert.equal(staged.bucket.rawRecords.size, 1001);
  assert.equal(staged.invalidRecordSeen, true);
  assert.equal(
    staged.bucket.rawRecords.get(`${traceId}:${oversize.span_id}`)?.projection_omitted,
    'record_too_large',
  );
});

test('a later detail-page failure leaves the current bucket untouched', async () => {
  const existing = projectedRecord(10);
  const identity = `${existing.trace_id}:${existing.span_id}`;
  const current = {
    revision: 10,
    records: new Map([[identity, existing.otlp]]),
    rawRecords: new Map([[identity, existing]]),
  };
  let calls = 0;
  const controller = new AbortController();

  await assert.rejects(
    stageTrajectoryChainPages(current, controller.signal, async () => {
      calls += 1;
      if (calls === 1) {
        return detailPage({
          revision: 12,
          records: [projectedRecord(11)],
          hasMore: true,
          nextSinceRevision: 11,
        });
      }
      throw new Error('page two failed');
    }),
    /page two failed/,
  );
  assert.equal(current.revision, 10);
  assert.deepEqual([...current.records.keys()], [identity]);
  assert.deepEqual([...current.rawRecords.keys()], [identity]);
});

test('progressive detail publish exposes only consumed page revisions before a later failure', async () => {
  const existing = projectedRecord(20);
  const identity = `${existing.trace_id}:${existing.span_id}`;
  const current = {
    revision: 20,
    records: new Map([[identity, existing.otlp]]),
    rawRecords: new Map([[identity, existing]]),
  };
  const published = [];
  let calls = 0;
  const controller = new AbortController();

  await assert.rejects(
    stageTrajectoryChainPages(
      current,
      controller.signal,
      async () => {
        calls += 1;
        if (calls === 1) {
          return detailPage({
            revision: 30,
            records: [projectedRecord(21)],
            hasMore: true,
            nextSinceRevision: 21,
          });
        }
        throw new Error('page two failed after visible progress');
      },
      progress => published.push(progress),
    ),
    /page two failed after visible progress/,
  );

  assert.equal(published.length, 1);
  assert.equal(published[0].bucket.revision, 21);
  assert.equal(published[0].bucket.records.size, 2);
  assert.notEqual(published[0].bucket.revision, 30);
  assert.equal(current.revision, 20);
});

test('progressive detail publish advances each page cursor and reaches target only at the final page', async () => {
  const published = [];
  let calls = 0;
  const controller = new AbortController();
  const staged = await stageTrajectoryChainPages(
    undefined,
    controller.signal,
    async (sinceRevision) => {
      calls += 1;
      if (calls === 1) {
        assert.equal(sinceRevision, 0);
        return detailPage({
          revision: 3,
          records: [projectedRecord(1)],
          hasMore: true,
          nextSinceRevision: 1,
        });
      }
      if (calls === 2) {
        assert.equal(sinceRevision, 1);
        return detailPage({
          revision: 3,
          records: [projectedRecord(2)],
          hasMore: true,
          nextSinceRevision: 2,
        });
      }
      assert.equal(sinceRevision, 2);
      return detailPage({
        revision: 3,
        records: [projectedRecord(3)],
        hasMore: false,
        nextSinceRevision: 3,
      });
    },
    progress => published.push(progress),
  );

  assert.ok(staged);
  assert.deepEqual(published.map(progress => progress.bucket.revision), [1, 2, 3]);
  assert.deepEqual(published.map(progress => progress.bucket.records.size), [1, 2, 3]);
  assert.equal(staged, published[2]);
});

test('aborting after a staged detail page never returns a publishable bucket', async () => {
  const current = {
    revision: 0,
    records: new Map(),
    rawRecords: new Map(),
  };
  let resolveSecondPage;
  let secondPageStarted;
  const secondPageStartedPromise = new Promise(resolve => {
    secondPageStarted = resolve;
  });
  const secondPagePromise = new Promise(resolve => {
    resolveSecondPage = resolve;
  });
  const controller = new AbortController();
  let calls = 0;
  const stagedPromise = stageTrajectoryChainPages(
    current,
    controller.signal,
    async () => {
      calls += 1;
      if (calls === 1) {
        return detailPage({
          revision: 2,
          records: [projectedRecord(1)],
          hasMore: true,
          nextSinceRevision: 1,
        });
      }
      secondPageStarted();
      return secondPagePromise;
    },
  );

  await secondPageStartedPromise;
  controller.abort();
  resolveSecondPage(detailPage({
    revision: 2,
    records: [projectedRecord(2)],
    hasMore: false,
    nextSinceRevision: 2,
  }));

  assert.equal(await stagedPromise, null);
  assert.equal(current.revision, 0);
  assert.equal(current.records.size, 0);
  assert.equal(current.rawRecords.size, 0);
});

test('raw-only data keeps the content surface available during a refresh error', () => {
  assert.equal(trajectoryContentMode({
    sessionId: SESSION_ID,
    loading: false,
    error: 'gateway offline',
    projectedCount: 0,
    rawCount: 1,
  }), 'data');
  assert.equal(trajectoryContentMode({
    sessionId: SESSION_ID,
    loading: false,
    error: 'gateway offline',
    projectedCount: 0,
    rawCount: 0,
  }), 'blocking-error');
});

test('versioned upsert replaces one running identity with its terminal record', () => {
  const running = projectedRecord(1);
  running.record_id = `${running.trace_id}:${running.span_id}`;
  running.record_revision = 1;
  running.change_seq = 10;
  running.lifecycle = 'running';
  const started = applyTrajectoryDetailRecords(undefined, detailPage({
    revision: 10,
    records: [running],
    hasMore: false,
    nextSinceRevision: 10,
  }));
  const final = {
    ...projectedRecord(2),
    trace_id: running.trace_id,
    span_id: running.span_id,
    record_id: running.record_id,
    record_revision: 3,
    change_seq: 12,
    lifecycle: 'final',
  };
  final.otlp = otlpRecord(final.trace_id, final.span_id);
  const completed = applyTrajectoryDetailRecords(started.bucket, detailPage({
    revision: 12,
    records: [final],
    hasMore: false,
    nextSinceRevision: 12,
  }));

  assert.equal(completed.bucket.records.size, 1);
  assert.equal(completed.bucket.rawRecords.size, 1);
  assert.deepEqual(completed.bucket.versions.get(running.record_id), {
    lifecycle: 'completed',
    recordRevision: 3,
  });
  assert.equal(completed.bucket.rawRecords.get(running.record_id).change_seq, 12);
  // Only the page that finished the span names it for frame release.
  assert.deepEqual(started.finishedSpanKeys, []);
  assert.deepEqual(completed.finishedSpanKeys, [running.record_id]);
});

test('late running revisions cannot downgrade a terminal trajectory identity', () => {
  const record = projectedRecord(3);
  record.record_id = `${record.trace_id}:${record.span_id}`;
  record.record_revision = 5;
  record.lifecycle = 'completed';
  const completed = applyTrajectoryDetailRecords(undefined, detailPage({
    revision: 20,
    records: [record],
    hasMore: false,
    nextSinceRevision: 20,
  }));
  const late = {
    ...record,
    ingest_seq: 4,
    record_revision: 6,
    lifecycle: 'running',
  };
  const unchanged = applyTrajectoryDetailRecords(completed.bucket, detailPage({
    revision: 21,
    records: [late],
    hasMore: false,
    nextSinceRevision: 21,
  }));

  assert.deepEqual(unchanged.bucket.versions.get(record.record_id), {
    lifecycle: 'completed',
    recordRevision: 5,
  });
  assert.equal(unchanged.bucket.rawRecords.get(record.record_id).ingest_seq, 3);
});

test('authoritative final absorbs a higher provisional record revision', () => {
  const provisional = projectedRecord(5);
  provisional.record_id = `${provisional.trace_id}:${provisional.span_id}`;
  provisional.record_revision = 9;
  provisional.lifecycle = 'provisional';
  const started = applyTrajectoryDetailRecords(undefined, detailPage({
    revision: 30,
    records: [provisional],
    hasMore: false,
    nextSinceRevision: 30,
  }));
  const final = {
    ...provisional,
    ingest_seq: 6,
    record_revision: 1,
    lifecycle: 'final',
  };
  const completed = applyTrajectoryDetailRecords(started.bucket, detailPage({
    revision: 31,
    records: [final],
    hasMore: false,
    nextSinceRevision: 31,
  }));

  assert.deepEqual(completed.bucket.versions.get(provisional.record_id), {
    lifecycle: 'completed',
    recordRevision: 1,
  });
  assert.equal(completed.bucket.rawRecords.get(provisional.record_id).ingest_seq, 6);
});

test('live elapsed grows only for running open intervals', () => {
  const running = {
    index: 1,
    kind: 'message',
    status: 'running',
    text: 'streaming',
    startedAt: 1_000,
    timeSeconds: null,
  };
  const completed = { ...running, status: 'complete', timeSeconds: 1.25 };
  const input = { ...running, kind: 'user', status: 'complete' };

  assert.equal(liveElapsedSeconds(running, 2_750), 1.75);
  assert.equal(liveElapsedSeconds(completed, 99_000), 1.25);
  assert.equal(liveElapsedSeconds(input, 99_000), null);
});
