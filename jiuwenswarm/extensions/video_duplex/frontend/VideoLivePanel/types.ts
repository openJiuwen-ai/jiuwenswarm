import type { FileDownloadItem } from '../../../../channels/web/frontend/src/types';

export interface ChatContextItem {
  id: number;
  role: 'user' | 'assistant' | 'tool';
  text: string;
  presentation?: 'tool_result';
  responseId?: string;
}

export interface RealtimeBrief {
  status: 'completed' | 'failed';
  result_kind: 'code' | 'research' | 'calculation' | 'action' | 'file' | 'generic';
  summary: string;
  displayed_in_ui: boolean;
  response_mode: 'brief' | 'acknowledge';
  source: 'core_agent' | 'derived' | 'fallback';
}

export interface SearchJobPayload {
  sequence?: number;
  revision?: number;
  replay?: boolean;
  queue_position?: number;
  queue_version?: number;
  job_id?: string;
  title?: string;
  search_session_id?: string;
  question?: string;
  query?: string;
  result?: string;
  display_result?: string;
  realtime_brief?: RealtimeBrief;
  files?: FileDownloadItem[];
  error?: string;
  interaction?: {
    id: string;
    request_id: string;
    state: string;
    source?: string;
    approval_schema?: unknown;
    evolution_meta?: unknown;
    questions: Array<{ question: string; options?: Array<{ label: string; description?: string }> }>;
  };
  wait_reason?: string;
  /** Tasks this queued job is held behind (same user, possibly other conversations). */
  waiting_on?: Array<{ id: string; title: string; status: string; search_session_id: string }>;
  engine?: string;
  status?: 'unknown' | 'queued' | 'running' | 'waiting_user' | 'cancelling' | 'cancelled' | 'completed' | 'failed';
  latency_ms?: number;
  progress?: SearchProgressEntry;
  progress_history?: SearchProgressEntry[];
  tool_call_id?: string;
  tool_name?: string;
  turn_id?: string;
}

export interface SearchProgressEntry {
  files?: FileDownloadItem[];
  stage: string;
  title: string;
  detail?: string;
  status: 'queued' | 'running' | 'completed' | 'failed';
  todos?: Array<{ id: string; content: string; status: string }>;
  sequence: number;
  elapsed_ms?: number;
  timestamp?: number;
  content?: string;
  tool_name?: string;
  tool_call_id?: string;
  tool_payload?: Record<string, unknown>;
  tool_arguments?: unknown;
  tool_description?: string;
  tool_formatted_args?: string;
  tool_display_name?: string;
  tool_result?: unknown;
  tool_summary?: string;
  tool_success?: boolean;
  subagent_event?: Record<string, unknown>;
}

export interface SearchProgressJob {
  id: string;
  query: string;
  status: NonNullable<SearchJobPayload['status']>;
  latencyMs?: number;
  progress: SearchProgressEntry[];
}

export interface SearchJobState {
  id: string;
  searchSessionId: string;
  turnId?: string;
  question: string;
  query: string;
  // waiting = backend execution queue; queued = completed result awaiting playback.
  status: 'waiting' | 'running' | 'queued' | 'failed';
  toolCallId?: string;
}

export interface AgentAction {
  search_job?: {
    id?: string;
    question?: string;
    query?: string;
    status?: string;
    search_session_id?: string;
    tool_call_id?: string;
    tool_name?: string;
    turn_id?: string;
    reused?: boolean;
  } | null;
}

export interface VideoSessionConfig {
  context_restore?: { token: string; instructions: string };
  provider?: 'joyai' | 'qwen_omni';
  url?: string;
  model: string;
  voice?: string;
  tools?: Array<Record<string, unknown>>;
  reply_language?: 'match' | 'zh-CN' | 'en';
}

export interface JoyAIFrameResult extends AgentAction {
  response?: string;
}

export interface TtsStreamPayload {
  stream_id?: string;
  audio_base64?: string;
  sample_rate?: number;
  error?: string;
  first_chunk_ms?: number;
}
