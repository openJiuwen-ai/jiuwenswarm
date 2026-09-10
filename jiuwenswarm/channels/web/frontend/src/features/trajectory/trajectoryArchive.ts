// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Versioned browser-side archive contract for offline trajectory replay. */

import type { WebConnectionState } from '../../types';
import type { OtlpExportTraceServiceRequest } from './shared/otlp';
import type { TrajectoryDetailRecord } from './trajectoryClient';
import {
  applyTrajectoryDetailRecords,
  recordIdentity,
  type TrajectoryRecordVersion,
  type TrajectoryTraceBucket,
} from './trajectoryWindow';

export const TRAJECTORY_ARCHIVE_FORMAT = 'openjiuwen.trajectory.archive';
export const TRAJECTORY_ARCHIVE_VERSION = 1;
export const MAX_TRAJECTORY_ARCHIVE_RECORDS = 200_000;

export type TrajectoryArchiveRecord = Omit<TrajectoryDetailRecord, 'ingest_seq' | 'change_seq'> & {
  record_id: string;
  record_revision: number;
  lifecycle: 'running' | 'final' | 'abandoned';
  operation: 'upsert';
  change_seq: string;
  observed_time_unix_nano: string;
  trace_id: string;
  span_id: string;
  raw_json_base64: string;
};

export interface TrajectoryArchive {
  format: typeof TRAJECTORY_ARCHIVE_FORMAT;
  archive_version: typeof TRAJECTORY_ARCHIVE_VERSION;
  session_id: string;
  store_epoch: string;
  revision: string;
  exported_at: string;
  records: TrajectoryArchiveRecord[];
}

export interface TrajectoryArchiveView {
  records: OtlpExportTraceServiceRequest[];
  rawRecords: TrajectoryDetailRecord[];
  lifecycleByRecordId: Map<string, TrajectoryRecordVersion['lifecycle']>;
  traceCount: number;
  invalidRecordSeen: boolean;
  rawDataByRecordId: Map<string, unknown>;
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

function validBase64(value: unknown): value is string {
  if (typeof value !== 'string'
    || value.length === 0
    || value.length % 4 !== 0
    || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return false;
  try {
    atob(value);
    return true;
  } catch {
    return false;
  }
}

function parseRecord(value: unknown): TrajectoryArchiveRecord {
  if (!object(value)
    || !validIdentity(value.record_id)
    || !Number.isSafeInteger(value.record_revision)
    || Number(value.record_revision) < 0
    || (value.lifecycle !== 'running'
      && value.lifecycle !== 'final'
      && value.lifecycle !== 'abandoned')
    || value.operation !== 'upsert'
    || typeof value.change_seq !== 'string'
    || !/^\d+$/.test(value.change_seq)
    || typeof value.observed_time_unix_nano !== 'string'
    || !/^\d+$/.test(value.observed_time_unix_nano)
    || typeof value.trace_id !== 'string'
    || typeof value.span_id !== 'string'
    || `${value.trace_id}:${value.span_id}` !== value.record_id
    || !validBase64(value.raw_json_base64)
    || typeof value.raw_valid !== 'boolean'
    || (value.otlp !== null && !validOtlp(value.otlp))) {
    throw new Error('Trajectory archive contains an invalid record');
  }
  const record = value as unknown as TrajectoryArchiveRecord;
  if (record.otlp !== null && recordIdentity(record.otlp) !== record.record_id) {
    throw new Error('Trajectory archive record identity does not match its OTLP span');
  }
  return record;
}

export function parseTrajectoryArchive(text: string): TrajectoryArchive {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error('Trajectory archive is not valid JSON');
  }
  if (!object(value)
    || value.format !== TRAJECTORY_ARCHIVE_FORMAT
    || value.archive_version !== TRAJECTORY_ARCHIVE_VERSION
    || typeof value.session_id !== 'string'
    || value.session_id.length === 0
    || typeof value.store_epoch !== 'string'
    || value.store_epoch.length === 0
    || typeof value.revision !== 'string'
    || !/^\d+$/.test(value.revision)
    || typeof value.exported_at !== 'string'
    || !Number.isFinite(Date.parse(value.exported_at))
    || !Array.isArray(value.records)
    || value.records.length > MAX_TRAJECTORY_ARCHIVE_RECORDS) {
    throw new Error('Trajectory archive format or version is not supported');
  }
  const records = value.records.map(parseRecord);
  if (new Set(records.map(record => record.record_id)).size !== records.length) {
    throw new Error('Trajectory archive contains duplicate record identities');
  }
  return { ...value, records } as unknown as TrajectoryArchive;
}

function decodeRawJson(record: TrajectoryArchiveRecord): unknown {
  try {
    const binary = atob(record.raw_json_base64);
    const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
    const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  } catch {
    return record.raw_json_base64;
  }
}

export function trajectoryArchiveView(archive: TrajectoryArchive): TrajectoryArchiveView {
  const buckets = new Map<string, TrajectoryTraceBucket>();
  let invalidRecordSeen = false;
  const rawDataByRecordId = new Map<string, unknown>();
  for (const [index, record] of archive.records.entries()) {
    const current = buckets.get(record.trace_id);
    const detailRecord: TrajectoryDetailRecord = {
      ...record,
      ingest_seq: index + 1,
      change_seq: undefined,
    };
    const applied = applyTrajectoryDetailRecords(current, {
      schema_version: 1,
      session_id: archive.session_id,
      trace_id: record.trace_id,
      revision: Math.max(current?.revision ?? 0, index + 1),
      reset: false,
      records: [detailRecord],
      has_more: false,
      next_since_revision: index + 1,
    });
    buckets.set(record.trace_id, applied.bucket);
    invalidRecordSeen = invalidRecordSeen || applied.invalidRecordSeen;
    rawDataByRecordId.set(record.record_id, decodeRawJson(record));
  }
  return {
    records: [...buckets.values()].flatMap(bucket => [...bucket.records.values()]),
    rawRecords: [...buckets.values()].flatMap(bucket => [...bucket.rawRecords.values()]),
    lifecycleByRecordId: new Map(
      [...buckets.values()].flatMap(bucket => [...(bucket.versions ?? [])].map(
        ([identity, version]) => [identity, version.lifecycle] as const,
      )),
    ),
    traceCount: buckets.size,
    invalidRecordSeen,
    rawDataByRecordId,
  };
}

export function shouldCatchUpTrajectory(
  previous: WebConnectionState,
  next: WebConnectionState,
): boolean {
  return next === 'ready' && (previous === 'reconnecting' || previous === 'closed');
}

export function exitTrajectoryReplay(archive: TrajectoryArchive | null): TrajectoryReplayExit {
  return { archive: null, catchUpLiveRevision: archive !== null };
}
