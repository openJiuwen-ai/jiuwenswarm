// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Same-origin HTTP client for the JiuwenSwarm trajectory read API. */

import { getApiBase } from '../../utils/env';
import type { OtlpExportTraceServiceRequest } from './shared/otlp';
import type { TrajectoryUsage } from './trajectory/model';

/** One execution subject's chain, summarized without loading any payload. */
export interface TrajectorySubjectSummary {
  subject_id: string;
  display_name: string | null;
  kind: string | null;
  parent_id: string | null;
  record_count: number;
  trace_count: number;
  first_start_time_unix_nano: string;
  last_observed_time_unix_nano: string;
  first_revision: number;
  revision: number;
  has_error: boolean;
  running: boolean;
}

export interface TrajectorySubjectListResponse {
  schema_version: 1;
  session_id: string;
  store_epoch: string;
  items: TrajectorySubjectSummary[];
  watermark: number;
}

export interface TrajectorySessionUsageItem {
  trace_id: string;
  inference_id: string;
  subject_id: string;
  start_time_unix_nano: string;
  usage: TrajectoryUsage;
  cumulative_usage: TrajectoryUsage;
}

export interface TrajectorySessionUsageResponse {
  schema_version: 1;
  session_id: string;
  store_epoch: string;
  scope: 'session';
  items: TrajectorySessionUsageItem[];
}

export interface TrajectoryDetailRecord {
  ingest_seq: number;
  record_id?: string;
  record_revision?: number;
  lifecycle?: 'provisional' | 'running' | 'completed' | 'final' | 'abandoned' | 'error';
  operation?: 'upsert' | 'delete';
  change_seq?: number;
  observed_time_unix_nano?: string;
  otlp: OtlpExportTraceServiceRequest | null;
  raw_valid: boolean | null;
  trace_id?: string;
  span_id?: string;
  raw_size_bytes?: number;
  /** Chain each restated attribute refers to, by attribute key. */
  sequences?: Record<string, { hash: string; depth: number }>;
  /** Attributes whose chain could not be rebuilt from what is held. */
  incomplete_sequences?: string[];
  projection_omitted?: 'record_too_large';
}

/** One increment of a model's answer, as stored outside the span. */
export interface TrajectoryStreamFrame {
  frame_seq: number;
  trace_id: string;
  span_id: string;
  subject_id: string;
  sequence: number;
  kind: string;
  timestamp_unix_nano: number;
  text?: string;
  tool_call_id?: string;
  tool_name?: string;
  arguments_delta?: string;
}

export interface TrajectoryStreamFramesResponse {
  schema_version: 1;
  session_id: string;
  frame_seq: number;
  reset: boolean;
  frames: TrajectoryStreamFrame[];
  has_more: boolean;
  next_since_frame_seq: number;
}

export interface TrajectorySubjectRecordsResponse {
  schema_version: 1;
  session_id: string;
  subject_id: string;
  revision: number;
  reset: boolean;
  records: TrajectoryDetailRecord[];
  has_more: boolean;
  next_since_revision: number;
  projected_raw_bytes?: number;
  max_projected_raw_bytes?: number;
  /** Element hashes of every chain this page refers to, in order. */
  sequences?: Record<string, string[]>;
  /** Element content this reader was not assumed to already hold. */
  blobs?: Record<string, string>;
}

export class TrajectoryApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = 'TrajectoryApiError';
    this.status = status;
    this.code = code;
  }
}

/**
 * Download one session's archive: a zip holding its `trajectory.jsonl`.
 *
 * The bytes are handed back as they are. Nothing here parses them, so saving
 * a large session costs one copy of the compressed file and no more.
 */
export async function getTrajectoryArchive(
  sessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<Blob> {
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/archive`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  if (!response.ok) {
    await readResponse(response);
  }
  return response.blob();
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function trajectoryUrl(path: string): string {
  return `${getApiBase()}${path}`;
}

async function readResponse(response: Response): Promise<unknown> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = undefined;
  }
  if (response.ok) return payload;
  const body = object(payload) ? payload : {};
  const message = typeof body.error === 'string'
    ? body.error
    : `Trajectory request failed (${response.status})`;
  throw new TrajectoryApiError(
    message,
    response.status,
    typeof body.code === 'string' ? body.code : undefined,
  );
}

function validSubjectSummary(value: unknown): value is TrajectorySubjectSummary {
  if (!object(value)) return false;
  return typeof value.subject_id === 'string'
    && value.subject_id.length > 0
    && Number.isSafeInteger(value.revision)
    && Number.isSafeInteger(value.first_revision)
    && Number.isSafeInteger(value.record_count)
    && Number.isSafeInteger(value.trace_count)
    && typeof value.first_start_time_unix_nano === 'string'
    && /^\d+$/.test(value.first_start_time_unix_nano)
    && typeof value.last_observed_time_unix_nano === 'string'
    && /^\d+$/.test(value.last_observed_time_unix_nano)
    && typeof value.has_error === 'boolean'
    && typeof value.running === 'boolean';
}

function validOtlp(value: unknown): value is OtlpExportTraceServiceRequest {
  return object(value) && Array.isArray(value.resourceSpans);
}

function validStoreEpoch(value: unknown): value is string {
  return typeof value === 'string'
    && value.trim().length > 0
    && value.length <= 512;
}

function validUsage(value: unknown): value is TrajectoryUsage {
  if (!object(value)) return false;
  return ['input', 'cacheRead', 'cacheWrite', 'output', 'reasoning', 'total'].every((key) => {
    const item = value[key];
    return item === undefined || (Number.isSafeInteger(item) && Number(item) >= 0);
  });
}

/** Whether a value is one well-formed session usage item, from the API or an archive. */
export function isTrajectorySessionUsageItem(value: unknown): value is TrajectorySessionUsageItem {
  if (!object(value)) return false;
  return typeof value.trace_id === 'string'
    && /^[0-9a-f]{32}$/.test(value.trace_id)
    && typeof value.inference_id === 'string'
    && value.inference_id.trim().length > 0
    && typeof value.subject_id === 'string'
    && value.subject_id.trim().length > 0
    && typeof value.start_time_unix_nano === 'string'
    && /^\d+$/.test(value.start_time_unix_nano)
    && validUsage(value.usage)
    && validUsage(value.cumulative_usage);
}

export async function getTrajectorySessionUsage(
  sessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<TrajectorySessionUsageResponse> {
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/usage`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!object(payload)
    || payload.schema_version !== 1
    || payload.session_id !== sessionId
    || !validStoreEpoch(payload.store_epoch)
    || payload.scope !== 'session'
    || !Array.isArray(payload.items)
    || !payload.items.every(isTrajectorySessionUsageItem)) {
    throw new TrajectoryApiError(
      'Trajectory session usage response is invalid',
      502,
      'INVALID_RESPONSE',
    );
  }
  return payload as unknown as TrajectorySessionUsageResponse;
}

/**
 * List the execution subjects that own a chain in one session.
 *
 * Passing `afterRevision` returns only the chains that advanced past it, which
 * is how the panel polls. The store epoch tells a caller its revisions are
 * stale and it must restart from zero.
 */
export async function listTrajectorySubjects(
  sessionId: string,
  options: {
    signal?: AbortSignal;
    afterRevision?: number;
  } = {},
): Promise<TrajectorySubjectListResponse> {
  const query = new URLSearchParams({
    after_revision: String(options.afterRevision ?? 0),
  });
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/subjects?${query.toString()}`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!object(payload)
    || payload.schema_version !== 1
    || payload.session_id !== sessionId
    || !validStoreEpoch(payload.store_epoch)
    || !Array.isArray(payload.items)
    || !payload.items.every(validSubjectSummary)
    || !Number.isSafeInteger(payload.watermark)) {
    throw new TrajectoryApiError('Trajectory subject list is invalid', 502, 'INVALID_RESPONSE');
  }
  return payload as unknown as TrajectorySubjectListResponse;
}

/**
 * Read one page of an execution subject's chain, in commit order.
 *
 * Paging advances along the chain, so the window a page ends on is the base
 * the next page's first delta applies to.
 */
export async function getTrajectorySubjectRecords(
  sessionId: string,
  subjectId: string,
  options: {
    signal?: AbortSignal;
    sinceRevision?: number;
    limit?: number;
  } = {},
): Promise<TrajectorySubjectRecordsResponse> {
  const query = new URLSearchParams({
    since_revision: String(options.sinceRevision ?? 0),
    limit: String(options.limit ?? 1000),
  });
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}`
    + `/subjects/${encodeURIComponent(subjectId)}/records?${query.toString()}`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!object(payload)
    || payload.schema_version !== 1
    || payload.session_id !== sessionId
    || payload.subject_id !== subjectId
    || !Number.isSafeInteger(payload.revision)
    || typeof payload.reset !== 'boolean'
    || !Array.isArray(payload.records)
    || typeof payload.has_more !== 'boolean'
    || !Number.isSafeInteger(payload.next_since_revision)) {
    throw new TrajectoryApiError('Trajectory detail response is invalid', 502, 'INVALID_RESPONSE');
  }
  const records: TrajectoryDetailRecord[] = payload.records.map((candidate) => {
    if (!object(candidate) || !Number.isSafeInteger(candidate.ingest_seq)) {
      throw new TrajectoryApiError('Trajectory record response is invalid', 502, 'INVALID_RESPONSE');
    }
    const otlp = validOtlp(candidate.otlp) ? candidate.otlp : null;
    const traceId = typeof candidate.trace_id === 'string'
      && /^[0-9a-f]{32}$/.test(candidate.trace_id)
      ? candidate.trace_id
      : undefined;
    const spanId = typeof candidate.span_id === 'string'
      && /^[0-9a-f]{16}$/.test(candidate.span_id)
      ? candidate.span_id
      : undefined;
    const rawSizeBytes = Number.isSafeInteger(candidate.raw_size_bytes)
      && Number(candidate.raw_size_bytes) >= 0
      ? Number(candidate.raw_size_bytes)
      : undefined;
    const projectionOmitted = candidate.projection_omitted === 'record_too_large'
      ? candidate.projection_omitted
      : undefined;
    const recordId = typeof candidate.record_id === 'string'
      && /^[0-9a-f]{32}:[0-9a-f]{16}$/.test(candidate.record_id)
      ? candidate.record_id
      : undefined;
    const recordRevision = Number.isSafeInteger(candidate.record_revision)
      && Number(candidate.record_revision) >= 0
      ? Number(candidate.record_revision)
      : undefined;
    const lifecycle = candidate.lifecycle === 'provisional'
      || candidate.lifecycle === 'running'
      || candidate.lifecycle === 'completed'
      || candidate.lifecycle === 'final'
      || candidate.lifecycle === 'abandoned'
      || candidate.lifecycle === 'error'
      ? candidate.lifecycle
      : undefined;
    const operation = candidate.operation === 'upsert' || candidate.operation === 'delete'
      ? candidate.operation
      : undefined;
    const changeSeq = Number.isSafeInteger(candidate.change_seq) && Number(candidate.change_seq) >= 0
      ? Number(candidate.change_seq)
      : undefined;
    const observedTime = typeof candidate.observed_time_unix_nano === 'string'
      && /^\d+$/.test(candidate.observed_time_unix_nano)
      ? candidate.observed_time_unix_nano
      : undefined;
    if (recordId !== undefined
      && traceId !== undefined
      && spanId !== undefined
      && recordId !== `${traceId}:${spanId}`) {
      throw new TrajectoryApiError('Trajectory record identity is invalid', 502, 'INVALID_RESPONSE');
    }
    if (projectionOmitted !== undefined
      && (traceId === undefined || spanId === undefined || rawSizeBytes === undefined)) {
      throw new TrajectoryApiError('Trajectory record index is invalid', 502, 'INVALID_RESPONSE');
    }
    return {
      ingest_seq: Number(candidate.ingest_seq),
      otlp,
      raw_valid: projectionOmitted !== undefined
        ? null
        : candidate.raw_valid === true && otlp !== null,
      ...(recordId === undefined ? {} : { record_id: recordId }),
      ...(recordRevision === undefined ? {} : { record_revision: recordRevision }),
      ...(lifecycle === undefined ? {} : { lifecycle }),
      ...(operation === undefined ? {} : { operation }),
      ...(changeSeq === undefined ? {} : { change_seq: changeSeq }),
      ...(observedTime === undefined ? {} : { observed_time_unix_nano: observedTime }),
      ...(traceId === undefined ? {} : { trace_id: traceId }),
      ...(spanId === undefined ? {} : { span_id: spanId }),
      ...(rawSizeBytes === undefined ? {} : { raw_size_bytes: rawSizeBytes }),
      ...(object(candidate.sequences)
        ? { sequences: candidate.sequences as Record<string, { hash: string; depth: number }> }
        : {}),
      ...(projectionOmitted === undefined ? {} : { projection_omitted: projectionOmitted }),
    };
  });
  return {
    schema_version: 1,
    session_id: sessionId,
    subject_id: subjectId,
    revision: Number(payload.revision),
    reset: payload.reset,
    records,
    has_more: payload.has_more,
    next_since_revision: Number(payload.next_since_revision),
    ...(object(payload.sequences) ? { sequences: payload.sequences as Record<string, string[]> } : {}),
    ...(object(payload.blobs) ? { blobs: payload.blobs as Record<string, string> } : {}),
    ...(Number.isSafeInteger(payload.projected_raw_bytes)
      ? { projected_raw_bytes: Number(payload.projected_raw_bytes) }
      : {}),
    ...(Number.isSafeInteger(payload.max_projected_raw_bytes)
      ? { max_projected_raw_bytes: Number(payload.max_projected_raw_bytes) }
      : {}),
  };
}

export async function getTrajectoryStreamFrames(
  sessionId: string,
  options: {
    signal?: AbortSignal;
    sinceFrameSeq?: number;
    limit?: number;
  } = {},
): Promise<TrajectoryStreamFramesResponse> {
  const query = new URLSearchParams({
    since_frame_seq: String(options.sinceFrameSeq ?? 0),
    limit: String(options.limit ?? 500),
  });
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/stream-frames?${query.toString()}`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!object(payload)
    || payload.schema_version !== 1
    || payload.session_id !== sessionId
    || !Number.isSafeInteger(payload.frame_seq)
    || typeof payload.reset !== 'boolean'
    || !Array.isArray(payload.frames)
    || typeof payload.has_more !== 'boolean'
    || !Number.isSafeInteger(payload.next_since_frame_seq)) {
    throw new TrajectoryApiError('Trajectory frame response is invalid', 502, 'INVALID_RESPONSE');
  }
  const frames: TrajectoryStreamFrame[] = payload.frames.map((candidate) => {
    if (!object(candidate)
      || !Number.isSafeInteger(candidate.frame_seq)
      || !Number.isSafeInteger(candidate.sequence)
      || typeof candidate.kind !== 'string'
      || typeof candidate.trace_id !== 'string'
      || !/^[0-9a-f]{32}$/.test(candidate.trace_id)
      || typeof candidate.span_id !== 'string'
      || !/^[0-9a-f]{16}$/.test(candidate.span_id)
      || typeof candidate.subject_id !== 'string') {
      throw new TrajectoryApiError('Trajectory frame is invalid', 502, 'INVALID_RESPONSE');
    }
    // Text is taken exactly as stored. Trimming it here would glue the
    // answer's words together once the frames are concatenated.
    const text = typeof candidate.text === 'string' ? candidate.text : undefined;
    const toolCallId = typeof candidate.tool_call_id === 'string'
      ? candidate.tool_call_id
      : undefined;
    const toolName = typeof candidate.tool_name === 'string' ? candidate.tool_name : undefined;
    const argumentsDelta = typeof candidate.arguments_delta === 'string'
      ? candidate.arguments_delta
      : undefined;
    const timestamp = Number.isSafeInteger(candidate.timestamp_unix_nano)
      ? Number(candidate.timestamp_unix_nano)
      : 0;
    return {
      frame_seq: Number(candidate.frame_seq),
      trace_id: candidate.trace_id,
      span_id: candidate.span_id,
      subject_id: candidate.subject_id,
      sequence: Number(candidate.sequence),
      kind: candidate.kind,
      timestamp_unix_nano: timestamp,
      ...(text === undefined ? {} : { text }),
      ...(toolCallId === undefined ? {} : { tool_call_id: toolCallId }),
      ...(toolName === undefined ? {} : { tool_name: toolName }),
      ...(argumentsDelta === undefined ? {} : { arguments_delta: argumentsDelta }),
    };
  });
  return {
    schema_version: 1,
    session_id: sessionId,
    frame_seq: Number(payload.frame_seq),
    reset: payload.reset,
    frames,
    has_more: payload.has_more,
    next_since_frame_seq: Number(payload.next_since_frame_seq),
  };
}

/** Most hashes one request may name; the server rejects more. */
export interface TrajectoryCheckpointsResponse {
  schema_version: 1;
  session_id: string;
  store_epoch: string;
  /** One checkpoint per execution subject, as the store states it. */
  checkpoints: Record<string, unknown>[];
  /** Element hashes of every chain the checkpoints refer to, in order. */
  sequences: Record<string, string[]>;
  /** Content of every element those chains hold. */
  blobs: Record<string, string>;
}

/**
 * Read what retention left for one session's removed turns.
 *
 * A view seeds itself from these before it loads a record, so the turns that
 * remain render as they did. Every chain they refer to comes with the answer,
 * whatever this reader already holds.
 */
export async function getTrajectoryCheckpoints(
  sessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<TrajectoryCheckpointsResponse> {
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/checkpoints`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!object(payload)
    || payload.schema_version !== 1
    || payload.session_id !== sessionId
    || !validStoreEpoch(payload.store_epoch)
    || !Array.isArray(payload.checkpoints)
    || !payload.checkpoints.every(object)
    || !object(payload.sequences)
    || !object(payload.blobs)) {
    throw new TrajectoryApiError('Trajectory checkpoint response is invalid', 502, 'INVALID_RESPONSE');
  }
  return payload as unknown as TrajectoryCheckpointsResponse;
}

export const MAX_SEQUENCE_REQUEST = 200;

/**
 * Fetch chains by hash, for content this reader turned out not to hold.
 *
 * A page read delivers content only when the reader was not assumed to have
 * it already. That assumption holds for a reader following along from the
 * start, and this is the way back for one it does not hold for: a reload, a
 * second device, an entry dropped from the browser's cache. Asking by hash
 * rather than by position is what makes the answer conclusive -- content
 * still missing after this is content the store no longer has.
 */
export async function getTrajectorySequences(
  sessionId: string,
  hashes: readonly string[],
  options: { signal?: AbortSignal } = {},
): Promise<{ sequences: Record<string, string[]>; blobs: Record<string, string> }> {
  if (hashes.length === 0) return { sequences: {}, blobs: {} };
  const query = new URLSearchParams({ hashes: hashes.join(','), since_revision: '0' });
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/sequences?${query.toString()}`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!object(payload)
    || payload.schema_version !== 1
    || payload.session_id !== sessionId) {
    throw new TrajectoryApiError('Trajectory sequence response is invalid', 502, 'INVALID_RESPONSE');
  }
  return {
    sequences: object(payload.sequences) ? payload.sequences as Record<string, string[]> : {},
    blobs: object(payload.blobs) ? payload.blobs as Record<string, string> : {},
  };
}

export async function getTrajectoryRawRecord(
  sessionId: string,
  traceId: string,
  spanId: string,
  options: { signal?: AbortSignal } = {},
): Promise<unknown> {
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}`
    + `/traces/${encodeURIComponent(traceId)}/spans/${encodeURIComponent(spanId)}/raw`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  const text = await response.text();
  if (!response.ok) {
    let payload: unknown;
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      payload = undefined;
    }
    const body = object(payload) ? payload : {};
    throw new TrajectoryApiError(
      typeof body.error === 'string'
        ? body.error
        : `Trajectory request failed (${response.status})`,
      response.status,
      typeof body.code === 'string' ? body.code : undefined,
    );
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}
