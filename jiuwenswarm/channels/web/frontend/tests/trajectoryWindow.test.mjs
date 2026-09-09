// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyTrajectoryDetailRecords,
  collectHeadRefreshWindow,
  collectRevisionRefreshWindow,
  createTrajectoryOperationCoordinator,
  createTrajectoryTraceHintCoordinator,
  createTrajectoryWindowState,
  resetTrajectoryWindowState,
  sameTrajectoryUsageMap,
  selectSummariesNeedingLoad,
  shouldCatchUpAfterTrajectoryTerminalEvent,
  stageTrajectoryTracePages,
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
import {
  getTrajectoryArchive,
  getTrajectorySessionUsage,
  getTrajectoryTrace,
  listTrajectoryTraceRevisions,
  listTrajectoryTraces,
} from '../node_modules/.cache/trajectory-window/trajectoryClient.mjs';
import {
  exitTrajectoryReplay,
  parseTrajectoryArchive,
  shouldCatchUpTrajectory,
  trajectoryArchiveView,
} from '../node_modules/.cache/trajectory-window/trajectoryArchive.mjs';
import {
  formatTokenCount,
  liveElapsedSeconds,
} from '../node_modules/.cache/trajectory-window/record.mjs';

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
    trace_id: hexId(value, 32),
    revision,
    start_time_unix_nano: String(value * 10),
    end_time_unix_nano: String(value * 10 + 1),
    span_count: 1,
    request_id: null,
    run_id: null,
    agent_mode: 'agent',
    has_error: false,
  };
}

function listPages(items) {
  const pages = [];
  for (let offset = 0; offset < items.length; offset += 30) {
    pages.push(items.slice(offset, offset + 30));
  }
  return pages;
}

async function collectNewPrefix(newCount) {
  const newItems = Array.from({ length: newCount }, (_, index) => summary(1 + index));
  const oldItems = Array.from({ length: 30 }, (_, index) => summary(10_000 + index));
  const pages = listPages([...newItems, ...oldItems]);
  const loaded = new Map(oldItems.map(item => [item.trace_id, item.revision]));
  const cursors = [];
  const controller = new AbortController();
  const window = await collectHeadRefreshWindow(
    loaded,
    STORE_EPOCH,
    controller.signal,
    async (cursor) => {
      cursors.push(cursor);
      const pageIndex = cursor === null ? 0 : Number(cursor.slice('page-'.length));
      return {
        schema_version: 1,
        session_id: SESSION_ID,
        store_epoch: STORE_EPOCH,
        items: pages[pageIndex],
        next_cursor: pageIndex + 1 < pages.length ? `page-${pageIndex + 1}` : null,
        revision_cursor: 'revision-baseline',
      };
    },
  );
  assert.ok(window);
  return {
    cursors,
    selected: selectSummariesNeedingLoad(loaded, window.summaries),
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
  otlp = otlpRecord(traceId, spanId),
  raw = otlp,
  rawValid = true,
} = {}) {
  return {
    record_id: `${traceId}:${spanId}`,
    trace_id: traceId,
    span_id: spanId,
    parent_span_id: null,
    record_revision: 3,
    lifecycle,
    operation: 'upsert',
    change_seq: '9007199254740993',
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
    raw_json_base64: Buffer.from(
      typeof raw === 'string' ? raw : JSON.stringify(raw),
      'utf8',
    ).toString('base64'),
    otlp,
    raw_valid: rawValid,
  };
}

function backendArchive(records) {
  return {
    format: 'openjiuwen.trajectory.archive',
    archive_version: 1,
    session_id: SESSION_ID,
    exported_at: '2026-08-21T00:00:00Z',
    store_epoch: STORE_EPOCH,
    revision: '9007199254740995',
    records,
  };
}

test('frontend parses and replays the real backend Archive v1 wire payload', () => {
  const finalRecord = backendArchiveRecord();
  const invalidRecord = backendArchiveRecord({
    spanId: hexId(2, 16),
    lifecycle: 'abandoned',
    otlp: null,
    raw: '{invalid-json',
    rawValid: false,
  });
  const archive = parseTrajectoryArchive(JSON.stringify(
    backendArchive([finalRecord, invalidRecord]),
  ));
  const view = trajectoryArchiveView(archive);

  assert.equal(archive.revision, '9007199254740995');
  assert.equal(archive.records[0].change_seq, '9007199254740993');
  assert.equal(view.traceCount, 1);
  assert.equal(view.records.length, 1);
  assert.equal(view.rawRecords.length, 2);
  assert.deepEqual(view.rawRecords.map(record => record.ingest_seq), [1, 2]);
  assert.equal(view.lifecycleByRecordId.get(invalidRecord.record_id), 'error');
  assert.equal(view.rawDataByRecordId.get(invalidRecord.record_id), '{invalid-json');
});

test('archive parser rejects frontend-only draft names and non-string cursors', () => {
  const record = backendArchiveRecord();
  const wrongVersionField = {
    ...backendArchive([record]),
    archive_version: undefined,
    format_version: 1,
  };
  const numericRevision = { ...backendArchive([record]), revision: 42 };
  const numericChangeSeq = backendArchive([{ ...record, change_seq: 42 }]);

  assert.throws(() => parseTrajectoryArchive(JSON.stringify(wrongVersionField)), /not supported/);
  assert.throws(() => parseTrajectoryArchive(JSON.stringify(numericRevision)), /not supported/);
  assert.throws(() => parseTrajectoryArchive(JSON.stringify(numericChangeSeq)), /invalid record/);
});

test('archive export client downloads the backend session archive endpoint', async () => {
  const originalFetch = globalThis.fetch;
  const payload = backendArchive([backendArchiveRecord()]);
  let requestedUrl = '';
  globalThis.fetch = async (input) => {
    requestedUrl = String(input);
    return new Response(JSON.stringify(payload), { status: 200 });
  };
  try {
    const text = await getTrajectoryArchive('session / one');
    assert.equal(parseTrajectoryArchive(text).records.length, 1);
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

test('exiting browser-only replay requests one live revision catch-up', () => {
  const archive = parseTrajectoryArchive(JSON.stringify(
    backendArchive([backendArchiveRecord()]),
  ));

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

test('head refresh crosses the page boundary for 31 new traces', async () => {
  const result = await collectNewPrefix(31);

  assert.deepEqual(result.cursors, [null, 'page-1']);
  assert.equal(result.selected.length, 31);
  assert.equal(new Set(result.selected.map(item => item.trace_id)).size, 31);
});

test('head refresh crosses three fixed-size pages for 61 new traces', async () => {
  const result = await collectNewPrefix(61);

  assert.deepEqual(result.cursors, [null, 'page-1', 'page-2']);
  assert.equal(result.selected.length, 61);
  assert.equal(new Set(result.selected.map(item => item.trace_id)).size, 61);
});

test('revision feed finds a late update for an already loaded old trace', async () => {
  const oldTrace = summary(80_000, 10);
  const unchangedTrace = summary(80_001, 20);
  const newTrace = summary(80_002, 1);
  const loaded = new Map([
    [oldTrace.trace_id, oldTrace.revision],
    [unchangedTrace.trace_id, unchangedTrace.revision],
  ]);
  const requestedCursors = [];
  const controller = new AbortController();
  const revisions = await collectRevisionRefreshWindow(
    'revision-10',
    STORE_EPOCH,
    controller.signal,
    async (cursor) => {
      requestedCursors.push(cursor);
      if (cursor === 'revision-10') {
        return {
          schema_version: 1,
          session_id: SESSION_ID,
          store_epoch: STORE_EPOCH,
          reset: false,
          items: [{ ...oldTrace, revision: 11 }, newTrace],
          next_cursor: 'revision-page-1',
          watermark: 'revision-30',
          has_more: true,
        };
      }
      return {
        schema_version: 1,
        session_id: SESSION_ID,
        store_epoch: STORE_EPOCH,
        reset: false,
        items: [{ ...oldTrace, revision: 12 }, unchangedTrace],
        next_cursor: 'revision-30',
        watermark: 'revision-30',
        has_more: false,
      };
    },
  );

  assert.ok(revisions);
  assert.deepEqual(requestedCursors, ['revision-10', 'revision-page-1']);
  assert.equal(revisions.nextCursor, 'revision-30');
  const selected = selectSummariesNeedingLoad(loaded, revisions.summaries);
  assert.deepEqual(
    selected.map(item => [item.trace_id, item.revision]),
    [[oldTrace.trace_id, 12], [newTrace.trace_id, 1]],
  );
});

test('trajectory client consumes opaque list and revision cursors', async () => {
  const requestedUrls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    requestedUrls.push(url);
    if (url.includes('/revisions?')) {
      return new Response(JSON.stringify({
        schema_version: 1,
        session_id: SESSION_ID,
        store_epoch: STORE_EPOCH,
        reset: false,
        items: [summary(85_001, 8)],
        next_cursor: 'revision-next',
        watermark: 'revision-next',
        has_more: false,
      }), { status: 200 });
    }
    return new Response(JSON.stringify({
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: STORE_EPOCH,
      items: [summary(85_000, 7)],
      next_cursor: 'list-next',
      revision_cursor: 'revision-baseline',
    }), { status: 200 });
  };
  try {
    const list = await listTrajectoryTraces(SESSION_ID, { limit: 30 });
    const revisions = await listTrajectoryTraceRevisions(SESSION_ID, {
      afterRevision: 'opaque cursor/with symbols',
      limit: 100,
    });

    assert.equal(list.revision_cursor, 'revision-baseline');
    assert.equal(revisions.next_cursor, 'revision-next');
    assert.equal(requestedUrls.length, 2);
    assert.match(requestedUrls[0], /\/traces\?limit=30$/);
    assert.match(
      requestedUrls[1],
      /\/revisions\?after_revision=opaque\+cursor%2Fwith\+symbols&limit=100$/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
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

test('trajectory client accepts the additive provisional detail contract', async () => {
  const originalFetch = globalThis.fetch;
  const record = projectedRecord(11);
  const span = record.otlp.resourceSpans[0].scopeSpans[0].spans[0];
  span.startTimeUnixNano = '1000000000';
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({
      schema_version: 1,
      session_id: SESSION_ID,
      trace_id: record.trace_id,
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
    const detail = await getTrajectoryTrace(SESSION_ID, record.trace_id);

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
        ? listTrajectoryTraces(SESSION_ID)
        : listTrajectoryTraceRevisions(SESSION_ID, {
          afterRevision: 'revision-baseline',
        });
      await assert.rejects(request, error => error.code === 'INVALID_RESPONSE');
      assert.equal(state.buckets.size, 1);
      assert.equal(state.storeEpoch, STORE_EPOCH);
      assert.equal(state.pageCursor, 'list-page-2');
      assert.equal(state.revisionCursor, 'revision-9');
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
  state.pageCursor = 'list-page-2';
  state.revisionCursor = 'revision-9';
  state.listWindowInitialized = true;
  state.rawSelection = identity;
  return state;
}

function assertWindowReset(state) {
  assert.equal(state.buckets.size, 0);
  assert.equal(state.storeEpoch, null);
  assert.equal(state.pageCursor, null);
  assert.equal(state.revisionCursor, null);
  assert.equal(state.listWindowInitialized, false);
  assert.equal(state.rawSelection, '');
}

test('same-epoch revision reset still fully clears the browser window', async () => {
  const controller = new AbortController();
  const result = await collectRevisionRefreshWindow(
    'revision-9',
    STORE_EPOCH,
    controller.signal,
    async () => ({
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: STORE_EPOCH,
      reset: true,
      items: [],
      next_cursor: 'revision-baseline',
      watermark: 'revision-baseline',
      has_more: false,
    }),
  );
  const state = populatedWindowState();

  assert.deepEqual(result, { reset: true, storeEpoch: STORE_EPOCH });
  resetTrajectoryWindowState(state);
  assertWindowReset(state);
});

test('partial retention epoch reset removes buckets, cursors, and raw selection', async () => {
  const controller = new AbortController();
  const result = await collectRevisionRefreshWindow(
    'revision-9',
    STORE_EPOCH,
    controller.signal,
    async () => ({
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: 'epoch-after-partial-retention',
      reset: true,
      items: [],
      next_cursor: 'revision-baseline',
      watermark: 'revision-baseline',
      has_more: false,
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
  const result = await collectRevisionRefreshWindow(
    'revision-9',
    STORE_EPOCH,
    controller.signal,
    async () => ({
      schema_version: 1,
      session_id: SESSION_ID,
      store_epoch: 'epoch-mixed',
      reset: false,
      items: [],
      next_cursor: 'revision-baseline',
      watermark: 'revision-baseline',
      has_more: false,
    }),
  );
  const state = populatedWindowState();

  assert.deepEqual(result, { reset: true, storeEpoch: 'epoch-mixed' });
  resetTrajectoryWindowState(state);
  assertWindowReset(state);
});

test('a cross-page head epoch change discards every provisional summary', async () => {
  const loaded = new Map([[summary(99_999).trace_id, 1]]);
  const requested = [];
  const controller = new AbortController();
  const result = await collectHeadRefreshWindow(
    loaded,
    STORE_EPOCH,
    controller.signal,
    async (cursor) => {
      requested.push(cursor);
      if (cursor === null) {
        return {
          schema_version: 1,
          session_id: SESSION_ID,
          store_epoch: STORE_EPOCH,
          items: [summary(1)],
          next_cursor: 'page-1',
          revision_cursor: 'revision-1',
        };
      }
      return {
        schema_version: 1,
        session_id: SESSION_ID,
        store_epoch: 'epoch-after-retention',
        items: [],
        next_cursor: null,
        revision_cursor: 'revision-new',
      };
    },
  );

  assert.deepEqual(requested, [null, 'page-1']);
  assert.deepEqual(result, { reset: true, storeEpoch: 'epoch-after-retention' });
  assert.equal('summaries' in result, false);
});

test('a cross-page revision epoch change discards the fixed-window first page', async () => {
  const requested = [];
  const controller = new AbortController();
  const result = await collectRevisionRefreshWindow(
    'revision-1',
    STORE_EPOCH,
    controller.signal,
    async (cursor) => {
      requested.push(cursor);
      if (cursor === 'revision-1') {
        return {
          schema_version: 1,
          session_id: SESSION_ID,
          store_epoch: STORE_EPOCH,
          reset: false,
          items: [summary(1)],
          next_cursor: 'revision-page-1',
          watermark: 'revision-20',
          has_more: true,
        };
      }
      return {
        schema_version: 1,
        session_id: SESSION_ID,
        store_epoch: 'epoch-replaced-between-pages',
        reset: true,
        items: [],
        next_cursor: 'revision-new-baseline',
        watermark: 'revision-new-baseline',
        has_more: false,
      };
    },
  );

  assert.deepEqual(requested, ['revision-1', 'revision-page-1']);
  assert.deepEqual(result, {
    reset: true,
    storeEpoch: 'epoch-replaced-between-pages',
  });
  assert.equal('summaries' in result, false);
});

test('load-earlier has one panel-level flight for two synchronous entry points', async () => {
  const coordinator = createTrajectoryOperationCoordinator();
  const busy = [];
  let calls = 0;
  let resolveRequest;
  const request = new Promise(resolve => {
    resolveRequest = resolve;
  });
  const operation = async () => {
    calls += 1;
    return request;
  };

  const timelinePromise = coordinator.runLoadEarlier(operation, value => busy.push(value));
  const tablePromise = coordinator.runLoadEarlier(operation, value => busy.push(value));

  assert.equal(timelinePromise, tablePromise);
  assert.equal(calls, 1);
  assert.deepEqual(busy, [true]);
  resolveRequest(true);
  assert.equal(await timelinePromise, true);
  assert.deepEqual(busy, [true, false]);
});

test('head refresh waits for the shared load-earlier flight before reading its cursor', async () => {
  const coordinator = createTrajectoryOperationCoordinator();
  let pageCursor = 'list-page-2';
  let earlierCalls = 0;
  let resolveEarlierRequest;
  const earlierRequest = new Promise(resolve => {
    resolveEarlierRequest = resolve;
  });
  const earlierPromise = coordinator.runLoadEarlier(async (generation) => {
    earlierCalls += 1;
    const nextCursor = await earlierRequest;
    if (!coordinator.isCurrent(generation)) return false;
    pageCursor = nextCursor;
    return true;
  }, () => {});

  const headGeneration = coordinator.currentGeneration();
  const headPromise = (async () => {
    const pendingEarlier = coordinator.pendingLoadEarlier(headGeneration);
    if (pendingEarlier !== null) await pendingEarlier;
    if (!coordinator.isCurrent(headGeneration)) return null;
    return pageCursor;
  })();

  assert.equal(earlierCalls, 1);
  let headSettled = false;
  void headPromise.finally(() => {
    headSettled = true;
  });
  await Promise.resolve();
  assert.equal(headSettled, false);

  resolveEarlierRequest('list-page-3');
  assert.equal(await earlierPromise, true);
  assert.equal(await headPromise, 'list-page-3');
  assert.equal(earlierCalls, 1);
});

test('generation invalidation restores busy and rejects stale load-earlier writes', async () => {
  const coordinator = createTrajectoryOperationCoordinator();
  const busy = [];
  const state = populatedWindowState();
  const controller = new AbortController();
  let resolveOldRequest;
  const oldRequest = new Promise(resolve => {
    resolveOldRequest = resolve;
  });
  const oldPromise = coordinator.runLoadEarlier(async (generation) => {
    const nextCursor = await oldRequest;
    if (controller.signal.aborted || !coordinator.isCurrent(generation)) return false;
    state.pageCursor = nextCursor;
    return true;
  }, value => busy.push(value));

  controller.abort();
  coordinator.invalidate(() => busy.push(false));
  resetTrajectoryWindowState(state);
  assert.deepEqual(busy, [true, false]);

  let resolveNewRequest;
  const newRequest = new Promise(resolve => {
    resolveNewRequest = resolve;
  });
  const newPromise = coordinator.runLoadEarlier(
    async () => newRequest,
    value => busy.push(value),
  );
  assert.deepEqual(busy, [true, false, true]);

  resolveOldRequest('stale-page');
  assert.equal(await oldPromise, false);
  assert.equal(state.pageCursor, null);
  assert.deepEqual(busy, [true, false, true]);

  resolveNewRequest(true);
  assert.equal(await newPromise, true);
  assert.deepEqual(busy, [true, false, true, false]);
});

test('a failed revision page yields no advanced cursor and retries from the baseline', async () => {
  const controller = new AbortController();
  const requestedCursors = [];
  await assert.rejects(
    collectRevisionRefreshWindow(
      'revision-baseline',
      STORE_EPOCH,
      controller.signal,
      async (cursor) => {
        requestedCursors.push(cursor);
        if (cursor === 'revision-baseline') {
          return {
            schema_version: 1,
            session_id: SESSION_ID,
            store_epoch: STORE_EPOCH,
            reset: false,
            items: [summary(86_000, 2)],
            next_cursor: 'revision-page-1',
            watermark: 'revision-watermark',
            has_more: true,
          };
        }
        throw new Error('revision page two failed');
      },
    ),
    /revision page two failed/,
  );

  const retried = await collectRevisionRefreshWindow(
    'revision-baseline',
    STORE_EPOCH,
    controller.signal,
    async (cursor) => {
      requestedCursors.push(cursor);
      return {
        schema_version: 1,
        session_id: SESSION_ID,
        store_epoch: STORE_EPOCH,
        reset: false,
        items: [summary(86_000, 3)],
        next_cursor: 'revision-watermark',
        watermark: 'revision-watermark',
        has_more: false,
      };
    },
  );

  assert.ok(retried);
  assert.deepEqual(requestedCursors, [
    'revision-baseline',
    'revision-page-1',
    'revision-baseline',
  ]);
  assert.equal(retried.nextCursor, 'revision-watermark');
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
  const stagedPromise = stageTrajectoryTracePages(
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
    stageTrajectoryTracePages(current, controller.signal, async () => {
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
    stageTrajectoryTracePages(
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
  const staged = await stageTrajectoryTracePages(
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
  const stagedPromise = stageTrajectoryTracePages(
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
