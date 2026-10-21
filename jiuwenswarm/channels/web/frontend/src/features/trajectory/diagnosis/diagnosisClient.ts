import { readResponse, TrajectoryApiError, trajectoryUrl } from '../trajectoryClient';
import type {
  DiagnosisRecord,
  DiagnosisRunRequest,
  DiagnosisSummary,
  TtseTriggerResult,
} from './diagnosisTypes';

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function diagnosesPath(sessionId: string, suffix = ''): string {
  return `/api/trajectory/sessions/${encodeURIComponent(sessionId)}/diagnoses${suffix}`;
}

function validRecord(value: unknown): value is DiagnosisRecord {
  return object(value)
    && typeof value.diagnosis_id === 'string'
    && typeof value.status === 'string'
    && Number.isSafeInteger(value.seq)
    && Array.isArray(value.stages);
}

function invalid(what: string): TrajectoryApiError {
  return new TrajectoryApiError(`Diagnosis ${what} is invalid`, 502, 'INVALID_RESPONSE');
}

async function sendJson(
  method: 'POST' | 'DELETE',
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(trajectoryUrl(path), {
    method,
    cache: 'no-store',
    signal,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** Start one diagnosis; resolves to the running record. */
export async function runDiagnosis(
  sessionId: string,
  request: DiagnosisRunRequest,
  options: { signal?: AbortSignal } = {},
): Promise<DiagnosisRecord> {
  const payload = await readResponse(await sendJson('POST', diagnosesPath(sessionId), request, options.signal));
  if (!validRecord(payload)) throw invalid('record');
  return payload;
}

/** List diagnosis summaries, newest first. */
export async function listDiagnoses(
  sessionId: string,
  options: { signal?: AbortSignal } = {},
): Promise<DiagnosisSummary[]> {
  const response = await fetch(trajectoryUrl(diagnosesPath(sessionId)), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!object(payload) || !Array.isArray(payload.items)) throw invalid('list');
  return payload.items as DiagnosisSummary[];
}

/** Fetch one record; a running diagnosis returns its live snapshot. */
export async function getDiagnosis(
  sessionId: string,
  diagnosisId: string,
  options: { signal?: AbortSignal } = {},
): Promise<DiagnosisRecord> {
  const response = await fetch(trajectoryUrl(diagnosesPath(sessionId, `/${encodeURIComponent(diagnosisId)}`)), {
    cache: 'no-store',
    signal: options.signal,
  });
  const payload = await readResponse(response);
  if (!validRecord(payload)) throw invalid('record');
  return payload;
}

/** Cancel a running diagnosis; terminal records are returned unchanged. */
export async function cancelDiagnosis(sessionId: string, diagnosisId: string): Promise<DiagnosisRecord> {
  const payload = await readResponse(
    await sendJson('POST', diagnosesPath(sessionId, `/${encodeURIComponent(diagnosisId)}/cancel`)),
  );
  if (!validRecord(payload)) throw invalid('record');
  return payload;
}

/** Delete a finished diagnosis record. */
export async function deleteDiagnosis(sessionId: string, diagnosisId: string): Promise<void> {
  await readResponse(await sendJson('DELETE', diagnosesPath(sessionId, `/${encodeURIComponent(diagnosisId)}`)));
}

/** Trigger TTSE experience generation with the selected experience root causes on the user's request. */
export async function triggerDiagnosisTtse(
  sessionId: string,
  diagnosisId: string,
  rootCauseIds: string[],
): Promise<Record<string, TtseTriggerResult>> {
  const payload = await readResponse(
    await sendJson('POST', diagnosesPath(sessionId, `/${encodeURIComponent(diagnosisId)}/ttse`), {
      root_cause_ids: rootCauseIds,
    }),
  );
  if (!object(payload) || !object(payload.results)) throw invalid('TTSE result');
  return payload.results as Record<string, TtseTriggerResult>;
}
