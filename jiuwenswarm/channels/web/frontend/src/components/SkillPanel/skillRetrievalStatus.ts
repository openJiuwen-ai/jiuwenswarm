export type SkillRetrievalBuildStatus = 'idle' | 'running' | 'success' | 'failed' | 'cancelled';
export type SkillRetrievalCandidateScale = 'small' | 'large';
export type SkillRetrievalStrategy =
  | 'legacy'
  | 'small_full'
  | 'large_flat'
  | 'indexed'
  | 'indexed_stale';

export interface SkillRetrievalStatus {
  enabled: boolean;
  index_enabled: boolean;
  candidate_scale: SkillRetrievalCandidateScale;
  estimated_candidate_tokens: number;
  candidate_budget_tokens: number;
  effective_strategy: SkillRetrievalStrategy;
  index_recommended: boolean;
  layout: 'flat' | 'tree';
  index_state: 'missing' | 'fresh' | 'stale' | 'not-required';
  build_supported: boolean;
  build_status: SkillRetrievalBuildStatus;
  build_progress: number;
  build_message: string;
  build_error: string;
  build_id: string;
  index_exists: boolean;
  indexed_count: number;
  searchable_count: number;
  profile_scope: 'session' | 'default';
  profile_session_id: string;
  profile_model_name: string;
  pinned_index_revision: string;
  profile_recovery_error: string;
}

export class SkillRetrievalStatusContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SkillRetrievalStatusContractError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function requireNonNegativeInteger(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new SkillRetrievalStatusContractError(`${field} must be a non-negative integer`);
  }
  return value;
}

function requireString(value: unknown, field: string): string {
  if (typeof value !== 'string') {
    throw new SkillRetrievalStatusContractError(`${field} must be a string`);
  }
  return value;
}

function requireEnum<T extends string>(
  value: unknown,
  field: string,
  allowed: readonly T[],
): T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) {
    throw new SkillRetrievalStatusContractError(`${field} is invalid`);
  }
  return value as T;
}

function requireBoolean(value: unknown, field: string): boolean {
  if (typeof value !== 'boolean') {
    throw new SkillRetrievalStatusContractError(`${field} must be a boolean`);
  }
  return value;
}

function requireProgress(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) {
    throw new SkillRetrievalStatusContractError('build_progress must be between 0 and 1');
  }
  return value;
}

export function canBuildSkillRetrievalIndex(status: SkillRetrievalStatus | null): boolean {
  return Boolean(status?.build_supported);
}

/** Validate the flat-or-indexed capability contract returned by the backend. */
export function parseSkillRetrievalStatus(value: unknown): SkillRetrievalStatus {
  if (!isRecord(value)) {
    throw new SkillRetrievalStatusContractError('status must be an object');
  }
  const enabled = requireBoolean(value.enabled, 'enabled');
  const indexEnabled = requireBoolean(value.index_enabled, 'index_enabled');
  const candidateScale = requireEnum(
    value.candidate_scale,
    'candidate_scale',
    ['small', 'large'] as const,
  );
  const effectiveStrategy = requireEnum(
    value.effective_strategy,
    'effective_strategy',
    ['legacy', 'small_full', 'large_flat', 'indexed', 'indexed_stale'] as const,
  );
  const indexRecommended = requireBoolean(value.index_recommended, 'index_recommended');
  const expectedRecommendation = enabled && candidateScale === 'large' && !indexEnabled;
  if (indexRecommended !== expectedRecommendation) {
    throw new SkillRetrievalStatusContractError('index_recommended is inconsistent with the active switches and candidate scale');
  }
  return {
    enabled,
    index_enabled: indexEnabled,
    candidate_scale: candidateScale,
    estimated_candidate_tokens: requireNonNegativeInteger(
      value.estimated_candidate_tokens,
      'estimated_candidate_tokens',
    ),
    candidate_budget_tokens: requireNonNegativeInteger(
      value.candidate_budget_tokens,
      'candidate_budget_tokens',
    ),
    effective_strategy: effectiveStrategy,
    index_recommended: indexRecommended,
    layout: requireEnum(value.layout, 'layout', ['flat', 'tree'] as const),
    index_state: requireEnum(
      value.index_state,
      'index_state',
      ['missing', 'fresh', 'stale', 'not-required'] as const,
    ),
    build_supported: requireBoolean(value.build_supported, 'build_supported'),
    build_status: requireEnum(
      value.build_status,
      'build_status',
      ['idle', 'running', 'success', 'failed', 'cancelled'] as const,
    ),
    build_progress: requireProgress(value.build_progress),
    build_message: requireString(value.build_message, 'build_message'),
    build_error: requireString(value.build_error, 'build_error'),
    build_id: requireString(value.build_id, 'build_id'),
    index_exists: requireBoolean(value.index_exists, 'index_exists'),
    indexed_count: requireNonNegativeInteger(value.indexed_count, 'indexed_count'),
    searchable_count: requireNonNegativeInteger(value.searchable_count, 'searchable_count'),
    profile_scope: requireEnum(
      value.profile_scope,
      'profile_scope',
      ['session', 'default'] as const,
    ),
    profile_session_id: requireString(value.profile_session_id, 'profile_session_id'),
    profile_model_name: requireString(value.profile_model_name, 'profile_model_name'),
    pinned_index_revision: requireString(
      value.pinned_index_revision,
      'pinned_index_revision',
    ),
    profile_recovery_error: requireString(
      value.profile_recovery_error,
      'profile_recovery_error',
    ),
  };
}
