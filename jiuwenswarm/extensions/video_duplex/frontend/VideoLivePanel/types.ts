export interface ChatContextItem {
  id: number;
  role: 'user' | 'assistant' | 'tool';
  text: string;
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
  job_id?: string;
  search_session_id?: string;
  question?: string;
  query?: string;
  result?: string;
  display_result?: string;
  realtime_brief?: RealtimeBrief;
  error?: string;
  engine?: string;
  status?: 'running' | 'completed' | 'failed';
  latency_ms?: number;
  progress?: SearchProgressEntry;
  progress_history?: SearchProgressEntry[];
  tool_call_id?: string;
  tool_name?: string;
  turn_id?: string;
}

export interface SearchProgressEntry {
  stage: string;
  title: string;
  detail?: string;
  status: 'running' | 'completed' | 'failed';
  sequence: number;
  elapsed_ms?: number;
  timestamp?: number;
  content?: string;
  tool_name?: string;
  tool_call_id?: string;
  tool_arguments?: unknown;
  tool_description?: string;
  tool_formatted_args?: string;
  tool_display_name?: string;
  tool_result?: unknown;
  tool_summary?: string;
  tool_success?: boolean;
}

export interface SearchProgressJob {
  id: string;
  query: string;
  status: 'running' | 'completed' | 'failed';
  latencyMs?: number;
  progress: SearchProgressEntry[];
}

export interface SearchJobState {
  id: string;
  searchSessionId: string;
  turnId?: string;
  question: string;
  query: string;
  status: 'running' | 'queued' | 'failed';
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
  provider?: 'joyai' | 'qwen_omni';
  url?: string;
  model: string;
  voice?: string;
  tools?: Array<Record<string, unknown>>;
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
