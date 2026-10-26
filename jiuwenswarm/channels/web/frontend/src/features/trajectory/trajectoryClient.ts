// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Same-origin HTTP client for the JiuwenSwarm trajectory read API. */

import { getApiBase } from '../../utils/env';
import type { OtlpExportTraceServiceRequest } from './shared/otlp';
import type { TrajectoryUsage } from './trajectory/model';

export interface TrajectoryTraceSummary {
  trace_id: string;
  revision: number;
  start_time_unix_nano: string;
  end_time_unix_nano: string;
  span_count: number;
  request_id: string | null;
  run_id: string | null;
  agent_mode: string | null;
  has_error: boolean;
}

export interface TrajectoryTraceListResponse {
  schema_version: 1;
  session_id: string;
  store_epoch: string;
  items: TrajectoryTraceSummary[];
  next_cursor: string | null;
  revision_cursor: string;
}

export interface TrajectoryRevisionListResponse {
  schema_version: 1;
  session_id: string;
  store_epoch: string;
  reset: boolean;
  items: TrajectoryTraceSummary[];
  next_cursor: string;
  watermark: string;
  has_more: boolean;
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
  projection_omitted?: 'record_too_large';
}

export interface TrajectoryTraceDetailResponse {
  schema_version: 1;
  session_id: string;
  trace_id: string;
  revision: number;
  reset: boolean;
  records: TrajectoryDetailRecord[];
  has_more: boolean;
  next_since_revision: number;
  projected_raw_bytes?: number;
  max_projected_raw_bytes?: number;
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

export async function getTrajectoryArchive(
  sessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<string> {
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/archive`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  if (!response.ok) {
    await readResponse(response);
  }
  return response.text();
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

function validTraceSummary(value: unknown): value is TrajectoryTraceSummary {
  if (!object(value)) return false;
  return typeof value.trace_id === 'string'
    && /^[0-9a-f]{32}$/.test(value.trace_id)
    && Number.isSafeInteger(value.revision)
    && typeof value.start_time_unix_nano === 'string'
    && /^\d+$/.test(value.start_time_unix_nano)
    && typeof value.end_time_unix_nano === 'string'
    && /^\d+$/.test(value.end_time_unix_nano)
    && Number.isSafeInteger(value.span_count)
    && typeof value.has_error === 'boolean';
}

function validOtlp(value: unknown): value is OtlpExportTraceServiceRequest {
  return object(value) && Array.isArray(value.resourceSpans);
}

function validOpaqueCursor(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 512;
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

function validSessionUsageItem(value: unknown): value is TrajectorySessionUsageItem {
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
    || !payload.items.every(validSessionUsageItem)) {
    throw new TrajectoryApiError(
      'Trajectory session usage response is invalid',
      502,
      'INVALID_RESPONSE',
    );
  }
  return payload as unknown as TrajectorySessionUsageResponse;
}

export async function listTrajectoryTraces(
  sessionId: string,
  options: {
    signal?: AbortSignal;
    cursor?: string | null;
    limit?: number;
  } = {},
): Promise<TrajectoryTraceListResponse> {
  const query = new URLSearchParams({ limit: String(options.limit ?? 30) });
  if (options.cursor) query.set('cursor', options.cursor);
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/traces?${query.toString()}`,
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
    || !payload.items.every(validTraceSummary)
    || (payload.next_cursor !== null && !validOpaqueCursor(payload.next_cursor))
    || !validOpaqueCursor(payload.revision_cursor)) {
    throw new TrajectoryApiError('Trajectory list response is invalid', 502, 'INVALID_RESPONSE');
  }
  return payload as unknown as TrajectoryTraceListResponse;
}

export async function listTrajectoryTraceRevisions(
  sessionId: string,
  options: {
    signal?: AbortSignal;
    afterRevision: string;
    limit?: number;
  },
): Promise<TrajectoryRevisionListResponse> {
  const query = new URLSearchParams({
    after_revision: options.afterRevision,
    limit: String(options.limit ?? 100),
  });
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/revisions?${query.toString()}`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!object(payload)
    || payload.schema_version !== 1
    || payload.session_id !== sessionId
    || !validStoreEpoch(payload.store_epoch)
    || typeof payload.reset !== 'boolean'
    || !Array.isArray(payload.items)
    || !payload.items.every(validTraceSummary)
    || !validOpaqueCursor(payload.next_cursor)
    || !validOpaqueCursor(payload.watermark)
    || typeof payload.has_more !== 'boolean'
    || (payload.reset === true
      && (payload.items.length !== 0
        || payload.has_more !== false
        || payload.next_cursor !== payload.watermark))) {
    throw new TrajectoryApiError(
      'Trajectory revision response is invalid',
      502,
      'INVALID_RESPONSE',
    );
  }
  return payload as unknown as TrajectoryRevisionListResponse;
}

export async function getTrajectoryTrace(
  sessionId: string,
  traceId: string,
  options: {
    signal?: AbortSignal;
    sinceRevision?: number;
    limit?: number;
  } = {},
): Promise<TrajectoryTraceDetailResponse> {
  const query = new URLSearchParams({
    since_revision: String(options.sinceRevision ?? 0),
    limit: String(options.limit ?? 1000),
  });
  const response = await fetch(trajectoryUrl(
    `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/traces/${encodeURIComponent(traceId)}?${query.toString()}`,
  ), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!object(payload)
    || payload.schema_version !== 1
    || payload.session_id !== sessionId
    || payload.trace_id !== traceId
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
      ...(projectionOmitted === undefined ? {} : { projection_omitted: projectionOmitted }),
    };
  });
  return {
    schema_version: 1,
    session_id: sessionId,
    trace_id: traceId,
    revision: Number(payload.revision),
    reset: payload.reset,
    records,
    has_more: payload.has_more,
    next_since_revision: Number(payload.next_since_revision),
    ...(Number.isSafeInteger(payload.projected_raw_bytes)
      ? { projected_raw_bytes: Number(payload.projected_raw_bytes) }
      : {}),
    ...(Number.isSafeInteger(payload.max_projected_raw_bytes)
      ? { max_projected_raw_bytes: Number(payload.max_projected_raw_bytes) }
      : {}),
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
