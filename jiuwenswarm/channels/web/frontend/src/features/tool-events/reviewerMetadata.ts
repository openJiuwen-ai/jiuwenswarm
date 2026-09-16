import type { AutoReviewerMetadata, AutoReviewerStatus } from '../../types';

type UnknownPayload = Record<string, unknown>;

const REVIEWER_STATUSES = new Set<AutoReviewerStatus>([
  'aborted',
  'approved',
  'blocked',
  'denied',
  'deterministic_allow',
  'fallback',
  'host_revalidation_failed',
  'in_progress',
  'manual',
  'timed_out',
]);

function asRecord(value: unknown): UnknownPayload | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as UnknownPayload) : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

export function normalizeReviewerStatus(value: unknown): AutoReviewerStatus | undefined {
  const status = asString(value) as AutoReviewerStatus | undefined;
  return status && REVIEWER_STATUSES.has(status) ? status : undefined;
}

export function effectiveReviewerStatus(reviewer?: AutoReviewerMetadata): AutoReviewerStatus | undefined {
  return normalizeReviewerStatus(reviewer?.final_reviewer_status ?? reviewer?.reviewer_status);
}

export function reviewerIndicatesFailure(reviewer?: AutoReviewerMetadata): boolean {
  const status = effectiveReviewerStatus(reviewer);
  return status === 'denied' || status === 'blocked' || status === 'host_revalidation_failed';
}

export function normalizeReviewerMetadata(payload: UnknownPayload): AutoReviewerMetadata | undefined {
  const metadata = asRecord(payload.metadata);
  const source =
    asRecord(payload.reviewer_metadata) ??
    asRecord(metadata?.reviewer_metadata) ??
    metadata ??
    payload;
  const reviewer: AutoReviewerMetadata = {
    decision_source: asString(source.decision_source),
    evidence_summary: asString(source.evidence_summary),
    final_reviewer_status: normalizeReviewerStatus(source.final_reviewer_status),
    manual_reason_summary: asString(source.manual_reason_summary),
    reviewer_status: normalizeReviewerStatus(source.reviewer_status),
    risk_level: asString(source.risk_level),
    user_review_hint: asString(source.user_review_hint),
  };
  return Object.values(reviewer).some(value => value !== undefined) ? reviewer : undefined;
}
