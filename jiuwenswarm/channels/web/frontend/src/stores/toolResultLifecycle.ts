import { AutoReviewerMetadata, AutoReviewerStatus, ToolExecutionStatus, ToolResult } from '../types';

const TERMINAL_REVIEWER_STATUSES = new Set([
  'approved',
  'blocked',
  'denied',
  'deterministic_allow',
  'host_revalidation_failed',
]);

function hasTerminalReviewer(result?: ToolResult): boolean {
  const status = terminalReviewerStatus(result?.reviewer);
  return Boolean(
    result?.reviewer?.final_reviewer_status &&
    status &&
    TERMINAL_REVIEWER_STATUSES.has(status)
  );
}

function terminalReviewerStatus(reviewer?: AutoReviewerMetadata): AutoReviewerStatus | undefined {
  const status = reviewer?.final_reviewer_status;
  return status && TERMINAL_REVIEWER_STATUSES.has(status) ? status : undefined;
}

function terminalReviewerDenied(reviewer?: AutoReviewerMetadata): boolean {
  const status = terminalReviewerStatus(reviewer);
  return status === 'denied' || status === 'blocked' || status === 'host_revalidation_failed';
}

export function mergeReviewerProgress(
  existing: AutoReviewerMetadata | undefined,
  incoming: AutoReviewerMetadata,
): AutoReviewerMetadata {
  return terminalReviewerStatus(existing)
    ? existing as AutoReviewerMetadata
    : incoming;
}

export function mergeToolResultProgress(existing: ToolResult | undefined, incoming: ToolResult): ToolResult {
  const reviewer = hasTerminalReviewer(existing) && !hasTerminalReviewer(incoming)
    ? existing?.reviewer
    : incoming.reviewer ?? existing?.reviewer;
  const merged = { ...incoming };
  if (reviewer !== undefined) {
    merged.reviewer = reviewer;
  }
  if (!incoming.beamSearch && existing?.beamSearch) {
    merged.beamSearch = existing.beamSearch;
  }
  if (terminalReviewerDenied(reviewer)) {
    merged.success = false;
  }
  if (reviewer === incoming.reviewer && merged.beamSearch === incoming.beamSearch && merged.success === incoming.success) {
    return incoming;
  }
  return merged;
}

function hasSameResultData(existing: ToolResult, incoming: ToolResult): boolean {
  return (
    existing.result === incoming.result &&
    existing.success === incoming.success &&
    Boolean(existing.pending) === Boolean(incoming.pending) &&
    Boolean(existing.timedOut) === Boolean(incoming.timedOut) &&
    (existing.summary || '') === (incoming.summary || '') &&
    existing.beamSearch === incoming.beamSearch &&
    existing.reviewer?.reviewer_status === incoming.reviewer?.reviewer_status &&
    existing.reviewer?.final_reviewer_status === incoming.reviewer?.final_reviewer_status &&
    existing.reviewer?.decision_source === incoming.reviewer?.decision_source &&
    existing.reviewer?.risk_level === incoming.reviewer?.risk_level &&
    existing.reviewer?.evidence_summary === incoming.reviewer?.evidence_summary &&
    existing.reviewer?.manual_reason_summary === incoming.reviewer?.manual_reason_summary &&
    existing.reviewer?.user_review_hint === incoming.reviewer?.user_review_hint
  );
}

export function shouldDropToolResult(currentStatus: ToolExecutionStatus, existing: ToolResult | undefined, incoming: ToolResult): boolean {
  if (incoming.pending && (currentStatus === 'completed' || currentStatus === 'error' || currentStatus === 'timeout')) {
    return true;
  }
  const finalStatus: ToolExecutionStatus = incoming.pending
    ? 'pending'
    : incoming.timedOut
      ? 'timeout'
      : incoming.success
        ? 'completed'
        : 'error';
  return currentStatus === finalStatus && existing !== undefined && hasSameResultData(existing, incoming);
}
