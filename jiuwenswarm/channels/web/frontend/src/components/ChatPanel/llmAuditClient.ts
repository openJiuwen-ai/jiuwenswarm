import { WebClient, webClient } from '../../services/webClient';
import { getWebTransport } from '../../utils/env';

export interface AuditReference {
  sha256: string;
  bytes: number;
  media_type: string;
  messages?: number;
  tools?: number;
}

export interface AuditCall {
  call_id: string;
  request_id: string;
  model_name: string;
  started_ns: number;
  status: 'running' | 'completed' | 'failed' | 'interrupted';
  latency_ms?: number;
  usage?: Record<string, number>;
  error?: string;
  capture_error?: string;
  subagent_type?: string;
  member_name?: string;
  input?: AuditReference;
  response?: AuditReference;
}

export interface AuditPage {
  calls: AuditCall[];
  next_cursor: string | null;
}

export type AuditCollection = 'messages' | 'tools' | 'response';
export interface AuditParts {
  collection: AuditCollection;
  total: number;
  parts: (AuditReference & { index: number })[];
}

export interface AuditChunk extends AuditReference {
  content: string;
  offset: number;
  next_offset: number;
  eof: boolean;
}

/** Reuse authenticated Gateway RPC; the pinned HTTP history route only accepts page cursors. */
export class LlmAuditClient {
  private readonly ownsClient = getWebTransport() === 'http';
  private readonly client = this.ownsClient ? new WebClient() : webClient;

  async connect(): Promise<void> {
    if (this.ownsClient) await this.client.connect();
  }

  async query<T>(sessionId: string, query: Record<string, unknown>): Promise<T> {
    const result = await this.client.request<{ llm_audit: T }>('session.get_metadata', {
      session_id: sessionId,
      llm_audit: query,
    });
    if (!result || !('llm_audit' in result)) throw new Error('LLM_AUDIT_UNAVAILABLE');
    return result.llm_audit;
  }

  close(): void {
    if (this.ownsClient) this.client.disconnect();
  }
}
