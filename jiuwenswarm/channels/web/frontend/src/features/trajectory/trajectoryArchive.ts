// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Versioned browser-side archive contract for offline trajectory replay.
 *
 * Version 3 is a JSONL stream of content-addressed lines in commit order,
 * normally shipped as the single `trajectory.jsonl` entry of a zip. Each line
 * is one JSON object whose `type` says what it is: a `header` first, the
 * `checkpoint` lines retention left, `blob` and `sequence` lines defining
 * content just before the first line that uses it, `record` lines in
 * change_seq order, `usage` lines after every record, and an `end` line that
 * counts what came before it. The file is read as a
 * stream -- inflated, decoded and split into lines while it is still being
 * read -- so its size is bounded by the limits below rather than by holding
 * the whole text at once. Earlier versions are refused rather than half-read.
 */

import { Unzip, UnzipInflate, type UnzipFile } from 'fflate';
import type { WebConnectionState } from '../../types';
import { isTeamAgentMode } from '../planMode/wireMode';
import type { OtlpExportTraceServiceRequest } from './shared/otlp';
import type { TrajectoryUsage } from './trajectory/model';
import {
  resolveTrajectoryCheckpoints,
  trajectoryCheckpointHeads,
  type TrajectoryRetentionCheckpoints,
} from './trajectoryCheckpoints';
import {
  isTrajectorySessionUsageItem,
  type TrajectoryDetailRecord,
} from './trajectoryClient';
import {
  createSequenceCache,
  rebuildRecord,
} from './trajectorySequences';
import {
  applyTrajectoryDetailRecords,
  type TrajectoryRecordVersion,
  type TrajectoryChainBucket,
} from './trajectoryWindow';

export const TRAJECTORY_ARCHIVE_FORMAT = 'openjiuwen.trajectory.archive';
export const TRAJECTORY_ARCHIVE_VERSION = 3;
export const TRAJECTORY_ARCHIVE_ENTRY_NAME = 'trajectory.jsonl';
export const MAX_TRAJECTORY_ARCHIVE_RECORDS = 200_000;

const MEBIBYTE = 1024 * 1024;
const ZIP_LOCAL_FILE_SIGNATURE = [0x50, 0x4b, 0x03, 0x04] as const;
// Records are applied to the view this many at a time, so no single step of an
// import walks more than one batch of them.
const RECORD_APPLY_BATCH = 500;
// Longest stretch of line handling before the event loop gets a turn, which is
// also when progress is reported and can repaint.
const YIELD_INTERVAL_MS = 16;

export interface TrajectoryArchiveLimits {
  /** Longest single line, in UTF-16 code units. */
  maxLineLength: number;
  /** Most bytes the JSONL text may inflate to. */
  maxUncompressedBytes: number;
  /** Most entries a zip may declare before its JSONL entry is found. */
  maxEntries: number;
  maxRecords: number;
}

export type TrajectoryArchiveLimitKind =
  | 'line-length'
  | 'uncompressed-size'
  | 'entry-count'
  | 'record-count';

export const TRAJECTORY_ARCHIVE_LIMITS: TrajectoryArchiveLimits = {
  maxLineLength: 64 * MEBIBYTE,
  maxUncompressedBytes: 1024 * MEBIBYTE,
  maxEntries: 16,
  maxRecords: MAX_TRAJECTORY_ARCHIVE_RECORDS,
};

export class TrajectoryArchiveLimitError extends Error {
  constructor(readonly limit: TrajectoryArchiveLimitKind) {
    super(`The trajectory archive exceeds the ${limit} import limit`);
    this.name = 'TrajectoryArchiveLimitError';
  }
}

export function isTrajectoryArchiveLimitError(error: unknown): error is TrajectoryArchiveLimitError {
  return error instanceof TrajectoryArchiveLimitError;
}

/**
 * The imported bytes are not a trajectory archive this reader can replay:
 * not JSONL or a zip holding it, malformed lines, an unsupported header or a
 * truncated body. Its message names the first defect found.
 */
export class TrajectoryArchiveFormatError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TrajectoryArchiveFormatError';
  }
}

export function isTrajectoryArchiveFormatError(error: unknown): error is TrajectoryArchiveFormatError {
  return error instanceof TrajectoryArchiveFormatError;
}

export interface TrajectoryArchiveHeader {
  format: typeof TRAJECTORY_ARCHIVE_FORMAT;
  archive_version: typeof TRAJECTORY_ARCHIVE_VERSION;
  session_id: string;
  exported_at: string;
  store_epoch: string;
  revision: string;
  stream_frames: false;
}

export interface TrajectoryArchiveView {
  records: OtlpExportTraceServiceRequest[];
  rawRecords: TrajectoryDetailRecord[];
  lifecycleByRecordId: Map<string, TrajectoryRecordVersion['lifecycle']>;
  traceCount: number;
  invalidRecordSeen: boolean;
  /** Stored payload of each record that has no projectable OTLP. */
  rawDataByRecordId: Map<string, unknown>;
}

/** Base mode of the session an archive was exported from. */
export type TrajectoryArchiveMode = 'agent' | 'team';

export interface TrajectoryArchiveReplay {
  header: TrajectoryArchiveHeader;
  /**
   * Mode of the exporting session, read from the `agent_mode` its records
   * carry: `team` once any record ran in a Team mode. `null` when no record
   * states a mode, so the replay follows the hosting session instead.
   */
  mode: TrajectoryArchiveMode | null;
  /** How the imported file was packaged, so a re-export keeps its extension. */
  container: 'zip' | 'jsonl';
  view: TrajectoryArchiveView;
  sessionCumulativeUsageByRequestIdentity: Map<string, TrajectoryUsage>;
  /** What retention left for the turns it removed before the export. */
  checkpoints: TrajectoryRetentionCheckpoints;
}

export interface TrajectoryArchiveProgress {
  bytesRead: number;
  totalBytes: number;
  records: number;
}

export type TrajectoryArchiveSource = Pick<Blob, 'size' | 'stream'>;

export interface TrajectoryArchiveReadOptions {
  limits?: Partial<TrajectoryArchiveLimits>;
}

export interface TrajectoryReplayExit {
  archive: null;
  catchUpLiveRevision: boolean;
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function validIdentity(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{32}:[0-9a-f]{16}$/.test(value);
}

function validOtlp(value: unknown): value is OtlpExportTraceServiceRequest {
  return object(value) && Array.isArray(value.resourceSpans);
}

function validHash(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 256;
}

function decimal(value: unknown): value is string {
  return typeof value === 'string' && /^\d+$/.test(value);
}

function decodeBase64(value: unknown): Uint8Array | null {
  if (typeof value !== 'string'
    || value.length === 0
    || value.length % 4 !== 0
    || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return null;
  try {
    return Uint8Array.from(atob(value), character => character.charCodeAt(0));
  } catch {
    return null;
  }
}

function invalidLine(lineNumber: number, reason: string): TrajectoryArchiveFormatError {
  return new TrajectoryArchiveFormatError(`Trajectory archive line ${lineNumber} ${reason}`);
}

function parseHeader(value: unknown): TrajectoryArchiveHeader {
  if (object(value)
    && value.format === TRAJECTORY_ARCHIVE_FORMAT
    && typeof value.archive_version === 'number'
    && value.archive_version !== TRAJECTORY_ARCHIVE_VERSION) {
    throw new TrajectoryArchiveFormatError(
      `Trajectory archive version ${value.archive_version} is no longer supported; `
      + 'export the session again to replay it',
    );
  }
  if (!object(value)
    || value.type !== 'header'
    || value.format !== TRAJECTORY_ARCHIVE_FORMAT
    || value.archive_version !== TRAJECTORY_ARCHIVE_VERSION
    || typeof value.session_id !== 'string'
    || value.session_id.length === 0
    || typeof value.store_epoch !== 'string'
    || value.store_epoch.length === 0
    || !decimal(value.revision)
    || typeof value.exported_at !== 'string'
    || !Number.isFinite(Date.parse(value.exported_at))
    || value.stream_frames !== false) {
    throw new TrajectoryArchiveFormatError('Trajectory archive format or version is not supported');
  }
  return {
    format: TRAJECTORY_ARCHIVE_FORMAT,
    archive_version: TRAJECTORY_ARCHIVE_VERSION,
    session_id: value.session_id,
    exported_at: value.exported_at,
    store_epoch: value.store_epoch,
    revision: value.revision,
    stream_frames: false,
  };
}

function parseSequenceReferences(
  value: unknown,
): Record<string, { hash: string; depth: number }> | undefined | null {
  if (value === undefined) return undefined;
  if (!object(value)) return null;
  for (const reference of Object.values(value)) {
    if (!object(reference)
      || !validHash(reference.hash)
      || !Number.isSafeInteger(reference.depth)
      || Number(reference.depth) < 0) return null;
  }
  return value as Record<string, { hash: string; depth: number }>;
}

interface ArchiveRecordLine {
  subjectId: string;
  changeSeq: bigint;
  /** Canonical mode the record ran in, when the line states one. */
  agentMode?: string;
  record: TrajectoryDetailRecord;
  rawData?: unknown;
}

/**
 * Turn one record line into the detail record a live page would have carried.
 *
 * The live reader receives parsed OTLP and a handful of envelope fields; the
 * archive carries the stored bytes instead, so they are parsed here and the
 * envelope is cut back to the same fields. Replay then goes through exactly
 * the reducer path a live page does.
 */
function parseRecordLine(
  value: Record<string, unknown>,
  ordinal: number,
  lineNumber: number,
): ArchiveRecordLine {
  const hasText = typeof value.raw_json === 'string';
  const rawBytes = hasText ? null : decodeBase64(value.raw_json_base64);
  const references = parseSequenceReferences(value.sequences);
  if (!validIdentity(value.record_id)
    || typeof value.trace_id !== 'string'
    || typeof value.span_id !== 'string'
    || `${value.trace_id}:${value.span_id}` !== value.record_id
    || typeof value.subject_id !== 'string'
    || value.subject_id.length === 0
    || !Number.isSafeInteger(value.record_revision)
    || Number(value.record_revision) < 0
    || (value.lifecycle !== 'running'
      && value.lifecycle !== 'final'
      && value.lifecycle !== 'abandoned')
    || value.operation !== 'upsert'
    || !decimal(value.change_seq)
    || !decimal(value.observed_time_unix_nano)
    || (value.raw_size_bytes !== undefined
      && (!Number.isSafeInteger(value.raw_size_bytes) || Number(value.raw_size_bytes) < 0))
    || typeof value.raw_valid !== 'boolean'
    || (hasText === (value.raw_json_base64 !== undefined))
    || (!hasText && rawBytes === null)
    || references === null) {
    throw invalidLine(lineNumber, 'is an invalid record');
  }
  const rawText = hasText
    ? value.raw_json as string
    : new TextDecoder('utf-8', { fatal: false }).decode(rawBytes as Uint8Array);
  let otlp: OtlpExportTraceServiceRequest | null = null;
  let rawData: unknown;
  if (value.raw_valid) {
    try {
      const parsed: unknown = JSON.parse(rawText);
      if (validOtlp(parsed)) otlp = parsed;
    } catch {
      otlp = null;
    }
    if (otlp === null) throw invalidLine(lineNumber, 'states valid OTLP that does not parse');
  } else {
    try {
      rawData = JSON.parse(rawText) as unknown;
    } catch {
      rawData = rawText;
    }
  }
  const changeSeq = Number(value.change_seq);
  const safeChangeSeq = Number.isSafeInteger(changeSeq);
  const record: TrajectoryDetailRecord = {
    ingest_seq: safeChangeSeq ? changeSeq : ordinal,
    otlp,
    raw_valid: otlp !== null,
    record_id: value.record_id,
    record_revision: Number(value.record_revision),
    lifecycle: value.lifecycle,
    operation: 'upsert',
    ...(safeChangeSeq ? { change_seq: changeSeq } : {}),
    observed_time_unix_nano: value.observed_time_unix_nano as string,
    trace_id: value.trace_id,
    span_id: value.span_id,
    ...(value.raw_size_bytes === undefined ? {} : { raw_size_bytes: Number(value.raw_size_bytes) }),
    ...(references === undefined ? {} : { sequences: references }),
  };
  return {
    subjectId: value.subject_id,
    ...(typeof value.agent_mode === 'string' && value.agent_mode.length > 0
      ? { agentMode: value.agent_mode }
      : {}),
    changeSeq: BigInt(value.change_seq as string),
    record,
    ...(otlp === null ? { rawData } : {}),
  };
}

interface SequenceNode {
  prev: string | null;
  blob: string;
  depth: number;
}

interface ArchiveLineReader {
  /** Take in one line of text, applying pending records once a batch is full. */
  accept: (text: string) => void;
  records: () => number;
  finish: (container: 'zip' | 'jsonl') => TrajectoryArchiveReplay;
}

function createArchiveLineReader(limits: TrajectoryArchiveLimits): ArchiveLineReader {
  let header: TrajectoryArchiveHeader | null = null;
  let ended = false;
  let lineCount = 0;
  let recordCount = 0;
  let lastChangeSeq: bigint | null = null;
  let usageStarted = false;
  let invalidRecordSeen = false;
  let teamRecordSeen = false;
  let agentRecordSeen = false;
  const cache = createSequenceCache();
  const nodes = new Map<string, SequenceNode>();
  const recordIds = new Set<string>();
  const traceIds = new Set<string>();
  const usage = new Map<string, TrajectoryUsage>();
  const buckets = new Map<string, TrajectoryChainBucket>();
  const rawDataByRecordId = new Map<string, unknown>();
  const checkpointLines: Record<string, unknown>[] = [];
  let pending: { subjectId: string; record: TrajectoryDetailRecord }[] = [];

  const resolveHeads = (heads: readonly string[]) => {
    for (const head of heads) {
      if (cache.chains.has(head)) continue;
      const first = nodes.get(head);
      if (first === undefined) continue;
      const elements: string[] = [];
      let node: SequenceNode | undefined = first;
      // A chain that lost an ancestor stops early, as a live read does. The
      // head's own depth bounds the walk, so a malformed cycle cannot spin.
      while (node !== undefined && elements.length < first.depth) {
        elements.push(node.blob);
        node = node.prev === null ? undefined : nodes.get(node.prev);
      }
      elements.reverse();
      cache.chains.set(head, elements);
    }
  };

  const flush = () => {
    if (pending.length === 0 || header === null) return;
    const bySubject = new Map<string, TrajectoryDetailRecord[]>();
    for (const { subjectId, record } of pending) {
      const records = bySubject.get(subjectId) ?? [];
      records.push(record);
      bySubject.set(subjectId, records);
    }
    pending = [];
    // One bucket per execution subject, fed in commit order: the same shape
    // the live window builds from one subject's pages.
    for (const [subjectId, records] of bySubject) {
      const current = buckets.get(subjectId);
      const revision = Math.max(current?.revision ?? 0, records[records.length - 1].ingest_seq);
      const applied = applyTrajectoryDetailRecords(current, {
        schema_version: 1,
        session_id: header.session_id,
        subject_id: subjectId,
        revision,
        reset: false,
        records,
        has_more: false,
        next_since_revision: revision,
      });
      buckets.set(subjectId, applied.bucket);
      invalidRecordSeen = invalidRecordSeen || applied.invalidRecordSeen;
    }
  };

  const acceptRecord = (value: Record<string, unknown>, lineNumber: number) => {
    if (usageStarted) throw invalidLine(lineNumber, 'is a record after the usage lines');
    if (recordCount >= limits.maxRecords) throw new TrajectoryArchiveLimitError('record-count');
    const parsed = parseRecordLine(value, recordCount + 1, lineNumber);
    if (lastChangeSeq !== null && parsed.changeSeq <= lastChangeSeq) {
      throw invalidLine(lineNumber, 'is out of commit order');
    }
    const identity = parsed.record.record_id as string;
    if (recordIds.has(identity)) {
      throw new TrajectoryArchiveFormatError('Trajectory archive contains duplicate record identities');
    }
    lastChangeSeq = parsed.changeSeq;
    recordIds.add(identity);
    traceIds.add(parsed.record.trace_id as string);
    recordCount += 1;
    if (parsed.agentMode !== undefined) {
      if (isTeamAgentMode(parsed.agentMode)) {
        teamRecordSeen = true;
      } else {
        agentRecordSeen = true;
      }
    }
    resolveHeads(Object.values(parsed.record.sequences ?? {}).map(reference => reference.hash));
    pending.push({ subjectId: parsed.subjectId, record: rebuildRecord(parsed.record, cache) });
    if (parsed.record.otlp === null) rawDataByRecordId.set(identity, parsed.rawData);
  };

  const acceptLine = (value: Record<string, unknown>, lineNumber: number): void => {
    switch (value.type) {
      case 'blob':
        if (!validHash(value.hash) || typeof value.text !== 'string') {
          throw invalidLine(lineNumber, 'is an invalid blob');
        }
        cache.blobs.set(value.hash, value.text);
        return;
      case 'sequence':
        if (!validHash(value.hash)
          || !(value.prev === null || validHash(value.prev))
          || !validHash(value.blob)
          || !Number.isSafeInteger(value.depth)
          || Number(value.depth) < 1) {
          throw invalidLine(lineNumber, 'is an invalid sequence');
        }
        nodes.set(value.hash, {
          prev: value.prev as string | null,
          blob: value.blob,
          depth: Number(value.depth),
        });
        return;
      case 'checkpoint':
        if (recordCount > 0 || usageStarted) {
          throw invalidLine(lineNumber, 'is a checkpoint after the first record');
        }
        // Its content was defined by the lines before it, so the chains it
        // names are complete here even though they are read only at the end.
        resolveHeads(trajectoryCheckpointHeads(value));
        checkpointLines.push(value);
        return;
      case 'record':
        acceptRecord(value, lineNumber);
        return;
      case 'usage':
        if (!isTrajectorySessionUsageItem(value)) throw invalidLine(lineNumber, 'is an invalid usage');
        usageStarted = true;
        usage.set(`${value.trace_id}\u0000${value.inference_id}`, value.cumulative_usage);
        return;
      case 'end':
        if (!Number.isSafeInteger(value.records) || !Number.isSafeInteger(value.lines)) {
          throw invalidLine(lineNumber, 'is an invalid end');
        }
        if (value.records !== recordCount || value.lines !== lineCount) {
          throw new TrajectoryArchiveFormatError('Trajectory archive does not match the counts its end line states');
        }
        ended = true;
        return;
      default:
        throw invalidLine(lineNumber, 'has an unsupported type');
    }
  };

  return {
    accept: (text) => {
      if (text.trim().length === 0) return;
      const lineNumber = lineCount + 1;
      if (ended) throw invalidLine(lineNumber, 'follows the end line');
      let value: unknown;
      try {
        value = JSON.parse(text) as unknown;
      } catch {
        if (header === null) throw new TrajectoryArchiveFormatError('Trajectory archive format or version is not supported');
        throw invalidLine(lineNumber, 'is not valid JSON');
      }
      if (header === null) {
        header = parseHeader(value);
      } else if (!object(value)) {
        throw invalidLine(lineNumber, 'is not a JSON object');
      } else {
        acceptLine(value, lineNumber);
      }
      if (!ended) lineCount += 1;
      if (pending.length >= RECORD_APPLY_BATCH) flush();
    },
    records: () => recordCount,
    finish: (container) => {
      if (header === null) throw new TrajectoryArchiveFormatError('Trajectory archive is empty');
      if (!ended) throw new TrajectoryArchiveFormatError('Trajectory archive is truncated: it has no end line');
      flush();
      const bucketList = [...buckets.values()];
      return {
        header,
        mode: teamRecordSeen ? 'team' : agentRecordSeen ? 'agent' : null,
        container,
        checkpoints: resolveTrajectoryCheckpoints(checkpointLines, cache),
        view: {
          records: bucketList.flatMap(bucket => [...bucket.records.values()]),
          rawRecords: bucketList.flatMap(bucket => [...bucket.rawRecords.values()]),
          lifecycleByRecordId: new Map(
            bucketList.flatMap(bucket => [...(bucket.versions ?? [])].map(
              ([identity, version]) => [identity, version.lifecycle] as const,
            )),
          ),
          traceCount: traceIds.size,
          invalidRecordSeen,
          rawDataByRecordId,
        },
        sessionCumulativeUsageByRequestIdentity: usage,
      };
    },
  };
}

interface LineSplitter {
  push: (text: string) => void;
  finish: () => void;
}

/**
 * Split decoded text into lines as it arrives.
 *
 * A line may span any number of chunks, so the partial tail is kept as parts
 * and joined once its newline arrives; only the new text is ever searched.
 */
function createLineSplitter(
  maxLineLength: number,
  onLine: (line: string) => void,
): LineSplitter {
  let parts: string[] = [];
  let partsLength = 0;
  const emit = (tail: string) => {
    if (partsLength + tail.length > maxLineLength) throw new TrajectoryArchiveLimitError('line-length');
    const line = parts.length === 0 ? tail : parts.join('') + tail;
    parts = [];
    partsLength = 0;
    onLine(line.endsWith('\r') ? line.slice(0, -1) : line);
  };
  return {
    push: (text) => {
      let start = 0;
      let newline = text.indexOf('\n');
      while (newline !== -1) {
        emit(text.slice(start, newline));
        start = newline + 1;
        newline = text.indexOf('\n', start);
      }
      if (start >= text.length) return;
      const rest = start === 0 ? text : text.slice(start);
      if (partsLength + rest.length > maxLineLength) throw new TrajectoryArchiveLimitError('line-length');
      parts.push(rest);
      partsLength += rest.length;
    },
    finish: () => {
      if (partsLength > 0) emit('');
    },
  };
}

function startsWithZipSignature(bytes: Uint8Array): boolean {
  return ZIP_LOCAL_FILE_SIGNATURE.every((byte, index) => bytes[index] === byte);
}

function concatBytes(left: Uint8Array, right: Uint8Array): Uint8Array {
  const joined = new Uint8Array(left.length + right.length);
  joined.set(left);
  joined.set(right, left.length);
  return joined;
}

function yieldToEventLoop(): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, 0));
}

interface ByteSink {
  push: (chunk: Uint8Array, final: boolean) => void;
}

/**
 * Inflate the `trajectory.jsonl` entry of a zip as its bytes arrive.
 *
 * Only that entry is started; any other is left unread. Failures raised inside
 * fflate's callbacks are recorded and rethrown from `push`, where the reading
 * loop can see them.
 */
function createZipSink(
  limits: TrajectoryArchiveLimits,
  onBytes: (bytes: Uint8Array) => void,
): ByteSink {
  const unzip = new Unzip();
  unzip.register(UnzipInflate);
  let entries = 0;
  let entryFound = false;
  let entryDone = false;
  let failure: unknown = null;
  unzip.onfile = (file: UnzipFile) => {
    entries += 1;
    if (entries > limits.maxEntries) {
      failure ??= new TrajectoryArchiveLimitError('entry-count');
      return;
    }
    if (entryFound || file.name !== TRAJECTORY_ARCHIVE_ENTRY_NAME) return;
    entryFound = true;
    file.ondata = (error, data, final) => {
      if (failure !== null) return;
      if (error !== null) {
        failure = new TrajectoryArchiveFormatError('Trajectory archive entry could not be inflated');
        return;
      }
      try {
        onBytes(data);
        if (final) entryDone = true;
      } catch (sinkError) {
        failure = sinkError;
      }
    };
    file.start();
  };
  return {
    push: (chunk, final) => {
      try {
        unzip.push(chunk, final);
      } catch {
        failure ??= new TrajectoryArchiveFormatError('Trajectory archive is truncated or is not a valid zip');
      }
      if (failure !== null) throw failure;
      if (!final) return;
      if (!entryFound) {
        throw new TrajectoryArchiveFormatError(`Trajectory archive zip has no ${TRAJECTORY_ARCHIVE_ENTRY_NAME} entry`);
      }
      if (!entryDone) throw new TrajectoryArchiveFormatError('Trajectory archive is truncated');
    },
  };
}

/**
 * Read and replay a trajectory archive without holding its text in memory.
 *
 * The source is read as a stream. A leading zip signature selects streaming
 * inflation of its JSONL entry; anything else is read as JSONL directly.
 * Lines are validated and dispatched as they are split, records are applied
 * in batches, and the event loop gets a turn between batches.
 *
 * @param source The imported file, or anything else that can stream bytes.
 * @param onProgress Called as reading advances.
 * @param options Limits to tighten.
 * @returns The replayable view, its header, its session usage and its checkpoints.
 */
export async function readTrajectoryArchive(
  source: TrajectoryArchiveSource,
  onProgress?: (progress: TrajectoryArchiveProgress) => void,
  options: TrajectoryArchiveReadOptions = {},
): Promise<TrajectoryArchiveReplay> {
  const limits = { ...TRAJECTORY_ARCHIVE_LIMITS, ...options.limits };
  const lines = createArchiveLineReader(limits);
  const queue: string[] = [];
  const splitter = createLineSplitter(limits.maxLineLength, line => queue.push(line));
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let uncompressedBytes = 0;
  const decodeText = (decodeChunk: () => string) => {
    let text: string;
    try {
      text = decodeChunk();
    } catch {
      throw new TrajectoryArchiveFormatError('Trajectory archive is not valid UTF-8 text');
    }
    splitter.push(text);
  };
  const decode = (bytes: Uint8Array) => {
    uncompressedBytes += bytes.length;
    if (uncompressedBytes > limits.maxUncompressedBytes) {
      throw new TrajectoryArchiveLimitError('uncompressed-size');
    }
    decodeText(() => decoder.decode(bytes, { stream: true }));
  };
  const plainSink: ByteSink = {
    push: (chunk) => {
      if (chunk.length > 0) decode(chunk);
    },
  };
  let sink: ByteSink | null = null;
  let container: 'zip' | 'jsonl' = 'jsonl';
  let prefix: Uint8Array = new Uint8Array(0);
  let bytesRead = 0;
  let lastYield = Date.now();
  const report = () => onProgress?.({
    bytesRead,
    totalBytes: source.size,
    records: lines.records(),
  });
  const drain = async () => {
    for (let index = 0; index < queue.length; index += 1) {
      lines.accept(queue[index]);
      if (Date.now() - lastYield >= YIELD_INTERVAL_MS) {
        report();
        await yieldToEventLoop();
        lastYield = Date.now();
      }
    }
    queue.length = 0;
  };
  const deliver = async (chunk: Uint8Array, final: boolean) => {
    let bytes = chunk;
    if (sink === null) {
      // The container is decided by its first four bytes, which a stream may
      // split across chunks.
      prefix = concatBytes(prefix, chunk);
      if (prefix.length < ZIP_LOCAL_FILE_SIGNATURE.length && !final) return;
      if (startsWithZipSignature(prefix)) {
        container = 'zip';
        sink = createZipSink(limits, decode);
      } else {
        sink = plainSink;
      }
      bytes = prefix;
      prefix = new Uint8Array(0);
    }
    sink.push(bytes, final);
    if (final) {
      decodeText(() => decoder.decode());
      splitter.finish();
    }
    await drain();
  };

  const reader = source.stream().getReader();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytesRead += value.length;
      await deliver(value, false);
    }
    await deliver(new Uint8Array(0), true);
  } catch (readError) {
    await reader.cancel().catch(() => undefined);
    throw readError;
  } finally {
    reader.releaseLock();
  }
  const replay = lines.finish(container);
  report();
  return replay;
}

export function shouldCatchUpTrajectory(
  previous: WebConnectionState,
  next: WebConnectionState,
): boolean {
  return next === 'ready' && (previous === 'reconnecting' || previous === 'closed');
}

/**
 * Whether a replay renders as a Team.
 *
 * An archive keeps the mode it was exported in: a single-Agent file opened in
 * a Team session still shows one Agent, and a Team file opened in a
 * single-Agent session still shows its lanes. Only an archive whose records
 * state no mode follows the session hosting the replay.
 *
 * @param archiveMode The mode the archive's records state.
 * @param sessionTeamMode Whether the hosting session runs as a Team.
 * @returns True when the replay renders as a Team.
 */
export function trajectoryReplayTeamMode(
  archiveMode: TrajectoryArchiveMode | null,
  sessionTeamMode: boolean,
): boolean {
  return archiveMode === null ? sessionTeamMode : archiveMode === 'team';
}

export function exitTrajectoryReplay(archive: TrajectoryArchiveReplay | null): TrajectoryReplayExit {
  return { archive: null, catchUpLiveRevision: archive !== null };
}
