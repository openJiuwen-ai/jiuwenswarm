export type SubagentStatus = 'running' | 'idle' | 'closed';

export type SubagentTurnOutcome = 'completed' | 'failed' | 'cancelled' | 'parent_ended';

export type SubagentLifecycle = 'live' | 'closed';

export type SubagentClosedReason = 'completed' | 'failed' | 'cancelled' | 'parent_ended' | 'manual' | 'evicted';

export type SubagentActivityKind = 'tool_call' | 'tool_result' | 'thinking' | 'error' | 'truncated';

export interface SubagentError {
  code: string;
  message: string;
}

export interface Subagent {
  subagent_id: string;
  parent_session_id: string;
  subagent_type: string;
  display_name: string;
  role: string;
  task_description: string;
  status: SubagentStatus;
  turn_outcome: SubagentTurnOutcome | null;
  lifecycle: SubagentLifecycle | null;
  can_send_input: boolean | null;
  needs_resume: boolean | null;
  closed_at: number | null;
  closed_reason: SubagentClosedReason | null;
  error: SubagentError | null;
  created_at: number;
  updated_at: number;
  revision: number;
}

export interface SubagentActivity {
  activity_id: string;
  subagent_id: string;
  parent_session_id?: string;
  task_id: string;
  sequence: number;
  kind: SubagentActivityKind;
  summary: string;
  at_ms: number;
  phase_id?: number;
  tool_name?: string;
  tool_call_id?: string;
  ok?: boolean;
  dropped?: number;
}

export interface SubagentUpdatedEvent {
  event_type: 'chat.subtask_update';
  session_id: string;
  subagent: Subagent;
}

export interface SubagentActivityEvent {
  event_type: 'chat.subagent_activity';
  session_id: string;
  activity: SubagentActivity;
}

export interface SubagentResult {
  subagent_id: string;
  parent_session_id?: string;
  task_id?: string;
  at_ms?: number;
  content: string;
  output_file?: string;
  source?: 'wait' | 'transcript';
}

export interface SubagentTurn {
  task_id: string;
  task_description: string;
  /** Only set when the description came from a roster-level fallback. */
  description_source?: 'fallback';
  started_at: number;
  result?: SubagentResult;
}

export type SubagentEvent = SubagentUpdatedEvent | SubagentActivityEvent;
