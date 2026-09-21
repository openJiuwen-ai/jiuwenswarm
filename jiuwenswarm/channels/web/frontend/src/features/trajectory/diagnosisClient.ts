// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Diagnosis API client: POST /api/trajectory/sessions/{id}/diagnose → SSE 流式报告。
 * 证据收集与 LLM 分析在 Gateway 侧；前端只触发、可选上传日志、流式渲染报告。
 */

import { getApiBase } from '../../utils/env';

export type DiagnosisMode = 'error' | 'interrupt' | 'unexpected';

export interface DiagnosisRequest {
  traceId?: string;
  userNote?: string;
  mode?: DiagnosisMode;
  includeLogs?: boolean;
  model?: string;
  /** 上传日志文件（可选）。非空时端点改用 multipart/form-data。 */
  logFiles?: File[];
  /** 离线模式：不查 trace，纯日志诊断（会话无需在列表中、无 trace）。 */
  offline?: boolean;
}

/** SSE 事件：阶段进度 / 报告 token / 完成 / 失败。 */
export type DiagnosisEvent =
  | { type: 'progress'; stage: string; detail?: string }
  | { type: 'token'; text: string }
  | { type: 'final'; text: string }
  | { type: 'done'; report_path: string; evidence_path: string }
  | { type: 'error'; message: string };

/**
 * 触发诊断并流式消费 SSE 事件。
 * @param sessionId 会话 ID
 * @param request 诊断请求参数
 * @param onEvent 每个 SSE 事件的回调
 * @param signal 可选的 AbortSignal，取消则断流
 */
export async function runDiagnosis(
  sessionId: string,
  request: DiagnosisRequest,
  onEvent: (event: DiagnosisEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = `${getApiBase()}/api/trajectory/sessions/${encodeURIComponent(sessionId)}/diagnose`;

  let body: BodyInit;
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  if (request.logFiles && request.logFiles.length > 0) {
    const form = new FormData();
    if (request.traceId) form.set('trace_id', request.traceId);
    if (request.userNote) form.set('user_note', request.userNote);
    if (request.mode) form.set('mode', request.mode);
    if (request.includeLogs !== undefined) form.set('include_logs', String(request.includeLogs));
    if (request.model) form.set('model', request.model);
    if (request.offline !== undefined) form.set('offline', String(request.offline));
    for (const file of request.logFiles) {
      form.append('log_files', file);
    }
    body = form;
  } else {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify({
      trace_id: request.traceId,
      user_note: request.userNote,
      mode: request.mode,
      include_logs: request.includeLogs,
      model: request.model,
      offline: request.offline,
    });
  }

  const response = await fetch(url, {
    method: 'POST',
    headers,
    body,
    cache: 'no-store',
    signal,
  });

  if (!response.ok) {
    let message = `诊断请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      if (payload?.error?.message) message = payload.error.message;
    } catch {
      // 非 JSON 错误响应，用默认消息
    }
    throw new DiagnosisApiError(message, response.status);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new DiagnosisApiError('响应不支持流式读取', response.status);
  }

  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE 事件以空行分隔；每个事件含 `event: ...` 和 `data: ...`。
      // sse_starlette 发 CRLF（\r\n\r\n 事件终止），必须用 \r? 兼容正则，
      // 否则纯 split('\n\n') 永远匹配不到 CRLF 分隔符，onEvent 不触发。
      const events = buffer.split(/\r?\n\r?\n/);
      buffer = events.pop() ?? '';
      for (const raw of events) {
        const event = parseSseEvent(raw);
        if (event) onEvent(event);
      }
    }
    // 尾部残留
    if (buffer.trim()) {
      const event = parseSseEvent(buffer);
      if (event) onEvent(event);
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSseEvent(raw: string): DiagnosisEvent | null {
  const lines = raw.split('\n');
  let data = '';
  for (const line of lines) {
    if (line.startsWith('data:')) {
      data += line.slice(5).trim();
    }
  }
  if (!data) return null;
  try {
    return JSON.parse(data) as DiagnosisEvent;
  } catch {
    return null;
  }
}

export class DiagnosisApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = 'DiagnosisApiError';
  }
}

/** 历史诊断报告摘要（列表项）。 */
export interface DiagnosisReportSummary {
  report_id: string;
  session_id: string;
  trace_id: string;
  created_at: string;
  summary: string;
  report_path: string;
  evidence_path: string;
}

/** 单个历史诊断报告全文。 */
export interface DiagnosisReportDetail extends DiagnosisReportSummary {
  window: string | null;
  report_text: string;
}

/** 列出某 session 的所有历史诊断报告（按时间倒序）。 */
export async function listDiagnosisReports(
  sessionId: string,
): Promise<DiagnosisReportSummary[]> {
  const url = `${getApiBase()}/api/trajectory/sessions/${encodeURIComponent(sessionId)}/diagnose/reports`;
  const res = await fetch(url, { method: 'GET', cache: 'no-store' });
  if (!res.ok) {
    throw new DiagnosisApiError(`列出诊断历史失败 (${res.status})`, res.status);
  }
  const payload = await res.json() as { reports?: DiagnosisReportSummary[] };
  return payload.reports ?? [];
}

/** 读取单个历史诊断报告全文。 */
export async function getDiagnosisReport(
  sessionId: string,
  reportId: string,
): Promise<DiagnosisReportDetail> {
  const url = `${getApiBase()}/api/trajectory/sessions/${encodeURIComponent(sessionId)}/diagnose/reports/${encodeURIComponent(reportId)}`;
  const res = await fetch(url, { method: 'GET', cache: 'no-store' });
  if (!res.ok) {
    throw new DiagnosisApiError(`读取诊断报告失败 (${res.status})`, res.status);
  }
  return await res.json() as DiagnosisReportDetail;
}
