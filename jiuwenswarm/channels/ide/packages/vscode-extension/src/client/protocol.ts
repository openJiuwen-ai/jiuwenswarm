// JiuwenSwarm WebSocket protocol types

export interface JiuwenMessage {
  id?: string;
  type: 'req' | 'res' | 'event';
  channel_id?: string;
  method?: string;
  event?: string;
  ok?: boolean;
  params?: Record<string, unknown>;
  payload?: Record<string, unknown>;
  timestamp?: number;
  // E2A format fields (may appear at top level)
  response_kind?: string;
  request_id?: string;
  body?: Record<string, unknown>;
}

export interface SessionInfo {
  session_id: string;
  title?: string;
  last_message_at?: number;
  created_at?: number;
  message_count?: number;
  history_path?: string;
}

// Messages from extension host → webview
export type ExtToWebviewMsg =
  | { type: 'connected'; sessionId: string | null; sessionTitle: string; needsSession?: boolean; models?: ModelEntry[]; activeModel?: string; defaultMode?: string; historyPath?: string }
  | { type: 'disconnected' }
  | { type: 'reconnecting' }
  | { type: 'metrics'; metrics: SessionMetrics }
  | { type: 'memory'; rssMb: number; totalMb: number; availableMb: number }
  | { type: 'sessions'; sessions: SessionInfo[] }
  | { type: 'sessions_error'; message: string }
  | { type: 'session_deleted'; sessionId: string }
  | { type: 'session_renamed'; title: string }
  | { type: 'skills'; skills: SkillEntry[] }
  | { type: 'skill_toggled'; skillId: string; enabled: boolean }
  | { type: 'skills_error'; message: string }
  | { type: 'jiuwen_event'; event: Record<string, unknown> }
  | { type: 'error'; message?: string; requestId?: string }
  | { type: 'debug_log'; line: string }
  | { type: 'prefill'; content: string }
  | { type: 'history_loading'; loading: boolean }
  | { type: 'rewindable'; enabled: boolean }
  | { type: 'rewind_done'; message: string; restored: number; failed: number }
  | { type: 'files'; files: Array<{ name: string; path: string; rel: string }> }
  | { type: 'git_status'; branch: string; changedCount: number }
  | { type: 'git_committed'; hash: string }
  | { type: 'git_error'; message: string }
  | { type: 'git_pushed' }
  | { type: 'model_changed'; model: string | null }
  | { type: 'export_done'; path: string };

export interface SessionMetrics {
  contextBytes: number;
  contextEstimatedTokens: number;
  promptEstimatedTokens: number;
  sessionTokens: number;
  sessionCostUsd: number;
  hasCost: boolean;
  attachmentsCount: number;
}

export interface ModelEntry {
  model_name: string;
  alias?: string;
  model_provider?: string;
}

export interface SkillEntry {
  skill_id: string;
  name: string;
  description?: string;
  enabled?: boolean;
  trigger?: string;
}

// Messages from webview → extension host
export type WebviewToExtMsg =
  | { type: 'ready' }
  | { type: 'input_changed'; content: string }
  | { type: 'send'; content: string; mode: string; requestId: string; model?: string; mentionedPaths?: string[]; media_items?: unknown[] }
  | { type: 'answer'; requestId: string; answers: unknown[]; source: string; mode: string }
  | { type: 'new_session' }
  | { type: 'switch_session'; sessionId: string }
  | { type: 'delete_session'; sessionId: string }
  | { type: 'list_sessions' }
  | { type: 'list_skills' }
  | { type: 'toggle_skill'; skillId: string; enabled: boolean }
  | { type: 'toggle_debug'; enabled: boolean }
  | { type: 'set_mode'; mode: string }
  | { type: 'switch_model'; model: string }
  | { type: 'rename_session'; title: string }
  | { type: 'export_session'; historyPath: string; sessionTitle: string }
  | { type: 'open_file'; path: string; line?: number }
  | { type: 'rewind' }
  | { type: 'stop' };

function randomId(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export function makeRequest(
  method: string,
  params: Record<string, unknown> = {},
  channelId = 'ide',
): JiuwenMessage {
  return {
    id: randomId(),
    type: 'req',
    channel_id: channelId,
    method,
    params,
    timestamp: Date.now() / 1000,
  };
}
