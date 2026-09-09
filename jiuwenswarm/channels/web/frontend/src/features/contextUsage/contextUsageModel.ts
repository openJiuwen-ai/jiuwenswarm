import type { ContextUsagePart, ContextUsageSnapshot } from '../../types';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNonNegativeNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || isNonNegativeNumber(value);
}

function isTokenCount(value: unknown): value is number {
  return isNonNegativeNumber(value) && Number.isSafeInteger(value);
}

function isNullableTokenCount(value: unknown): value is number | null {
  return value === null || isTokenCount(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string';
}

function isOptionalContextRole(value: unknown): value is 'leader' | 'teammate' | null | undefined {
  return value === undefined || value === null || value === 'leader' || value === 'teammate';
}

/** Validate only the v1 fields we consume. Never read aliases or calculate missing usage. */
export function parseContextUsageSnapshot(value: unknown): ContextUsageSnapshot | null {
  if (
    !isRecord(value) ||
    value.event_type !== 'context.usage' ||
    value.schema_version !== 'context-usage.v1' ||
    value.phase !== 'post_call' ||
    typeof value.request_id !== 'string' ||
    !value.request_id ||
    typeof value.product_session_id !== 'string' ||
    !value.product_session_id ||
    !isOptionalContextRole(value.role) ||
    !isTokenCount(value.depth) ||
    !isNullableString(value.team_id) ||
    !isNullableString(value.member_name)
  )
    return null;

  const window = value.context_window;
  const parts = value.parts;
  const sessionCacheHitRate = value.session_kv_cache_hit_rate;
  if (
    !isRecord(window) ||
    !isNullableTokenCount(window.limit_tokens) ||
    !isNullableTokenCount(window.input_tokens) ||
    !isNullableNumber(window.occupancy_rate) ||
    !isRecord(parts) ||
    !isNullableNumber(sessionCacheHitRate) ||
    (sessionCacheHitRate !== null && sessionCacheHitRate > 1)
  )
    return null;

  const validatedParts: [string, ContextUsagePart][] = [];
  for (const [key, part] of Object.entries(parts)) {
    if (
      !key ||
      !isRecord(part) ||
      part.category !== key ||
      !isTokenCount(part.tokens) ||
      !isNullableNumber(part.percentage_of_window)
    )
      return null;
    validatedParts.push([key, { category: key, tokens: part.tokens, percentage_of_window: part.percentage_of_window }]);
  }

  const timestamp = typeof value.timestamp === 'string' && value.timestamp.trim()
    ? value.timestamp
    : undefined;

  return {
    event_type: value.event_type,
    schema_version: value.schema_version,
    phase: value.phase,
    request_id: value.request_id,
    product_session_id: value.product_session_id,
    role: value.role ?? null,
    depth: value.depth,
    team_id: value.team_id,
    member_name: value.member_name,
    context_window: {
      limit_tokens: window.limit_tokens,
      input_tokens: window.input_tokens,
      occupancy_rate: window.occupancy_rate,
    },
    parts: Object.fromEntries(validatedParts),
    session_kv_cache_hit_rate: sessionCacheHitRate,
    ...(timestamp ? { timestamp } : {}),
  };
}

/** Single-Agent snapshots keep the existing root-context identity contract. */
export function isSingleAgentContextUsageSnapshot(snapshot: ContextUsageSnapshot): boolean {
  return (
    snapshot.role === null &&
    snapshot.depth === 0 &&
    snapshot.team_id === null &&
    snapshot.member_name === null
  );
}

/** Team identity is backend-owned; do not infer it from depth, names, or IDs. */
export function isTeamLeaderContextUsageSnapshot(snapshot: ContextUsageSnapshot): boolean {
  return snapshot.role === 'leader';
}

export function formatContextPercent(ratio: number): string {
  return `${Number((ratio * 100).toFixed(1))}%`;
}

export function formatContextTokens(value: number): string {
  if (value < 1_000) return String(value);
  return `${Number((value / 1_000).toFixed(1))}K`;
}

export function formatContextLimitTokens(value: number): string {
  if (value < 1_000) return String(value);
  return `${(value / 1_000).toFixed(1)}K`;
}

/** Only the finite ring geometry is bounded; the displayed occupancy may exceed 100%. */
export function getContextRingPercent(ratio: number): number {
  return Math.min(ratio * 100, 100);
}
