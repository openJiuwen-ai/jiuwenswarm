/**
 * Wire types of the trajectory diagnosis API.
 *
 * Mirrors docs/ai/design/轨迹诊断/01-接口与数据契约.md. Time points are
 * Unix-nanosecond decimal strings and every step is a span anchor.
 */

export interface StepRef {
  trace_id: string;
  span_id: string;
  subject_id: string;
  /** Message or tool row inside the span, formatted `<subject_id>:<seq>`. */
  node_id?: string;
}

export type DiagnosisStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
export type StageStatus = 'running' | 'completed' | 'failed' | 'skipped';
export type ModeProfile = 'agent.work' | 'agent.code' | 'team';
export type RootCauseCategory =
  | 'skill_defect'
  | 'ttse_gap'
  | 'tool_or_env'
  | 'model_capability'
  | 'requirement_unclear'
  | 'other';

export interface SelectionRange {
  subject_id: string;
  start_unix_nano: string;
  end_unix_nano: string;
}

export type DiagnosisScope = { kind: 'full' } | { kind: 'selection'; ranges: SelectionRange[] };

export interface DiagnosisRunRequest {
  scope: DiagnosisScope;
  user_note?: string;
  locale: 'zh' | 'en';
}

export interface DiagnosisSummary {
  diagnosis_id: string;
  ordinal: number;
  status: DiagnosisStatus;
  created_at: string;
  finished_at?: string;
  scope_kind: 'full' | 'selection';
  evaluate_artifact: boolean;
  artifact_eval_applied: boolean;
  headline?: string;
}

export interface DiagnosisStage {
  stage_id: string;
  title: string;
  status: StageStatus;
  summary?: string;
  output: string;
  skip_reason?: string;
  started_at?: string;
  elapsed_ms?: number;
}

export interface DiagnosisEvidence {
  artifact_eval?: {
    applied: boolean;
    skipped_reason?: string;
    verdict?: 'pass' | 'partial' | 'fail';
    score?: number;
    summary?: string;
    issues: Array<{ description: string; refs: StepRef[] }>;
  };
}

export interface FocusRange {
  subject_id: string;
  refs: StepRef[];
  start_unix_nano: string;
  end_unix_nano: string;
  reason?: string;
}

export interface RootCause {
  id: string;
  category: RootCauseCategory;
  subject_id: string;
  refs: StepRef[];
  summary: string;
  evidence: string;
  confidence: number;
}

export interface SkillSuggestion {
  id: string;
  skill_name: string;
  change_summary: string;
  rationale: string;
  message: string;
  root_cause_ids: string[];
  confidence: number;
  selectable: boolean;
  blocked_reason?: string;
}

/** A trigger signal for TTSE; TTSE itself generates and saves the experience. */
export interface TtseSignal {
  id: string;
  outcome: 'fail' | 'partial';
  score: number;
  reason: string;
  subject_id: string;
  start_unix_nano: string;
  end_unix_nano: string;
  root_cause_ids: string[];
  confidence: number;
}

export interface TtseTriggerResult {
  result: 'triggered' | 'failed';
  message?: string;
  triggered_at: string;
}

export interface DiagnosisSuggestions {
  artifact_fix?: { instruction: string; root_cause_ids: string[] };
  skills: SkillSuggestion[];
  ttse: TtseSignal[];
}

export interface DiagnosisInput {
  mode_profile: ModeProfile;
  model: { type: string; id: string; display_name?: string };
  scope: DiagnosisScope;
  user_note?: string;
  evaluate_artifact: boolean;
  locale: 'zh' | 'en';
  resolved_span_count: number;
  excluded_running: StepRef[];
}

export interface DiagnosisRecord extends DiagnosisSummary {
  session_id: string;
  input: DiagnosisInput;
  seq: number;
  stages: DiagnosisStage[];
  evidence: DiagnosisEvidence;
  focus_ranges: FocusRange[];
  root_causes: RootCause[];
  suggestions: DiagnosisSuggestions;
  ttse_trigger: Record<string, TtseTriggerResult>;
  error?: { code: string; message: string };
}

export interface DiagnosisProgressEvent {
  session_id: string;
  diagnosis_id: string;
  seq: number;
  stage_id: string;
  kind: 'stage_started' | 'output_delta' | 'stage_finished';
  title?: string;
  text_delta?: string;
  status?: 'completed' | 'failed' | 'skipped';
  summary?: string;
  skip_reason?: string;
  elapsed_ms?: number;
}

export interface DiagnosisStatusEvent {
  session_id: string;
  diagnosis_id: string;
  seq: number;
  status: DiagnosisStatus;
  summary: DiagnosisSummary;
}

export const DIAGNOSIS_PROGRESS_EVENT = 'trajectory.diagnosis.progress';
export const DIAGNOSIS_STATUS_EVENT = 'trajectory.diagnosis.status';
