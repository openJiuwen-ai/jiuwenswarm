export interface ChatContextItem {
  id: number;
  role: 'user' | 'assistant' | 'tool';
  text: string;
}

export interface SearchJobPayload {
  job_id?: string;
  search_session_id?: string;
  question?: string;
  query?: string;
  result?: string;
  error?: string;
  engine?: string;
  status?: 'running' | 'completed' | 'failed';
  latency_ms?: number;
  progress?: SearchProgressEntry;
  progress_history?: SearchProgressEntry[];
  tool_call_id?: string;
  tool_name?: string;
}

export interface SearchProgressEntry {
  stage: string;
  title: string;
  detail?: string;
  status: 'running' | 'completed' | 'failed';
  sequence: number;
  elapsed_ms?: number;
  tool_name?: string;
  tool_call_id?: string;
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
