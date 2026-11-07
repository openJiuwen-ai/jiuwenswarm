/**
 * Wire types of the trajectory diagnosis API.
 *
 * Mirrors 适配参考/12-诊断引擎接口.md. Time points are Unix-nanosecond
 * decimal strings and every step is a span anchor.
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
export type EvolutionCategory = 'skill' | 'experience';

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
  /** Narrative of the trajectory; also the record-row label. */
  trajectory_summary?: string;
  /** Legacy field of records written before the switch; used only as a fallback. */
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

/** Where a signal came from: a rule hit in tool output or a user message, or a Reward dimension. */
export type SignalSource = 'keyword_tool' | 'keyword_user' | 'reward' | 'user';

/** The diagnostic signal a failure cause answers (frontend badge only). */
export interface SignalRef {
  id: string;
  source: SignalSource | string;
  /** `perfect` / `fair` / `weak` for Reward signals, `uncertain` for rule signals. */
  tier?: string;
  /** Dimension score (0-100), Reward signals only. */
  score?: number | null;
  /** Reward dimension category. */
  category?: string | null;
  /** Nodes where the problem surfaced (a hint, not the cause). */
  node_ids?: string[];
  /** Original signal text, shown in the evidence fold; empty on older records. */
  text?: string;
}

/** One deliverable-evaluation failure cause (interface section 4.3). */
export interface RewardFailureCause {
  id: string;
  /** The reward dimension requirement this cause answers. */
  dimension: string;
  summary: string;
  evidence: StepRef[];
  suggestion: string[];
  signal?: SignalRef | null;
}

/** Per-signal detail behind one evolution item. */
export interface RelatedSignalAttribution {
  signal_id: string;
  source: string;
  tier: string;
  category?: string | null;
  score?: number | null;
  node_ids: string[];
  entries: Array<{ text_md: string; refs: StepRef[] }>;
}

/** One evolution item (interface section 4.4). */
export interface EvolutionJudge {
  id: string;
  category: EvolutionCategory;
  summary: string;
  evidence: StepRef[];
  suggestion: string[];
  /** Skill: confidence 0-1; experience: 1.0 / 0.5 / 0.0. */
  score: number;
  target?: string | null;
  outcome?: 'success' | 'partial' | 'fail' | null;
  related_signals?: RelatedSignalAttribution[];
}

/** Result of triggering TTSE with one experience evolution item. */
export interface TtseTriggerResult {
  result: 'triggered' | 'failed';
  message?: string;
  triggered_at: string;
}

export interface DiagnosisInput {
  mode_profile: ModeProfile;
  model: { type: string; id: string; display_name?: string };
  scope: DiagnosisScope;
  user_note?: string;
  evaluate_artifact: boolean;
  locale: 'zh' | 'en';
  resolved_span_count: number;
  /** Per-subject windows the run was fixed to, including a full-trajectory run. */
  resolved_ranges?: SelectionRange[];
  excluded_running: StepRef[];
}

export interface DiagnosisRecord extends DiagnosisSummary {
  session_id: string;
  input: DiagnosisInput;
  seq: number;
  stages: DiagnosisStage[];
  reward_failure_causes: RewardFailureCause[];
  evolution_judge: EvolutionJudge[];
  /** Experimental artifact-fix text (whole markdown block, A/B path). */
  artifact_improvement?: string | null;
  ttse_trigger: Record<string, TtseTriggerResult>;
  error?: { code: string; message: string };
  /** Full Reward evaluation payload (``attribution.reward.v1``); absent in rule mode. */
  reward?: RewardPayload | null;

  // ── legacy fields of records written before the switch (read-only fallback) ──
  root_causes?: LegacyRootCause[];
  suggestions?: LegacySuggestions;
  evidence?: { artifact_eval?: LegacyArtifactEval };
  task_description?: string | null;
}

// ── legacy shapes (records written before the new interface) ──────────────────

export type LegacyRootCauseCategory =
  | 'skill_defect'
  | 'ttse_success'
  | 'ttse_partial'
  | 'ttse_fail'
  | 'tool_or_env'
  | 'model_capability'
  | 'requirement_unclear'
  | 'other';

export interface LegacyArtifactEval {
  applied: boolean;
  skipped_reason?: string;
  verdict?: 'pass' | 'partial' | 'fail';
  score?: number;
  summary?: string;
  issues: Array<{ description: string; refs: StepRef[] }>;
}

export interface LegacyRootCause {
  id: string;
  category: LegacyRootCauseCategory;
  subject_id: string;
  refs: StepRef[];
  summary: string;
  evidence: string;
  confidence?: number | null;
  signal?: SignalRef | null;
}

export interface LegacySkillSuggestion {
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

export interface LegacySuggestions {
  artifact_fix?: { instruction: string; root_cause_ids: string[] };
  skills: LegacySkillSuggestion[];
}

/** One Reward evaluation dimension carried by the ``reward`` payload. */
export interface RewardDimension {
  category: string;
  requirement: string;
  comment: string;
  score: number;
  related_signal_ids?: string[];
}

/** One Reward evaluation category carried by the ``reward`` payload. */
export interface RewardCategory {
  name: string;
  comment?: string;
  score?: number;
}

/** The ``attribution.reward.v1`` payload the backend stores on a record. */
export interface RewardPayload {
  schema_version: string;
  overall: { comment: string; total_score: number };
  categories: RewardCategory[];
  dimensions: RewardDimension[];
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/** Narrow an unknown wire value to a Reward payload; malformed shapes yield null. */
export function asRewardPayload(value: unknown): RewardPayload | null {
  if (!isObject(value)) return null;
  const overall = value.overall;
  if (!isObject(overall) || typeof overall.comment !== 'string' || typeof overall.total_score !== 'number') {
    return null;
  }
  const categories = (Array.isArray(value.categories) ? value.categories : [])
    .filter((item): item is Record<string, unknown> => isObject(item) && typeof item.name === 'string')
    .map((item) => ({
      name: String(item.name),
      ...(typeof item.comment === 'string' ? { comment: item.comment } : {}),
      ...(typeof item.score === 'number' ? { score: item.score } : {}),
    }));
  const dimensions = (Array.isArray(value.dimensions) ? value.dimensions : [])
    .filter((item): item is Record<string, unknown> => (
      isObject(item)
      && typeof item.category === 'string'
      && typeof item.requirement === 'string'
      && typeof item.comment === 'string'
      && typeof item.score === 'number'
    ))
    .map((item) => ({
      category: String(item.category),
      requirement: String(item.requirement),
      comment: String(item.comment),
      score: Number(item.score),
      ...(Array.isArray(item.related_signal_ids)
        ? { related_signal_ids: item.related_signal_ids.map(String) }
        : {}),
    }));
  return {
    schema_version: typeof value.schema_version === 'string' ? value.schema_version : '',
    overall: { comment: overall.comment, total_score: overall.total_score },
    categories,
    dimensions,
  };
}

export interface DiagnosisProgressEvent {
  session_id: string;
  diagnosis_id: string;
  seq: number;
  stage_id: string;
  kind:
    | 'stage_started'
    | 'output_delta'
    | 'stage_finished'
    | 'reward'
    | 'reward_failure_causes'
    | 'evolution_judge'
    | 'root_causes';
  title?: string;
  text_delta?: string;
  status?: 'completed' | 'failed' | 'skipped';
  summary?: string;
  skip_reason?: string;
  elapsed_ms?: number;
  /** Artifact evaluation, sent once when diagnosis finishes scoring. */
  reward?: RewardPayload | null;
  /** Whole-table replacement events: every item known so far. */
  reward_failure_causes?: RewardFailureCause[];
  evolution_judge?: EvolutionJudge[];
  // legacy per-signal root-cause event
  signal_id?: string;
  root_causes?: LegacyRootCause[];
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
