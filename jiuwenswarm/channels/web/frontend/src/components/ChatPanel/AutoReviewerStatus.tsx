import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import type { AutoReviewerMetadata, AutoReviewerStatus } from '../../types';
import { normalizeReviewerStatus } from '../../features/tool-events/reviewerMetadata';

export type AutoReviewerBadgeTone = 'danger' | 'info' | 'neutral' | 'success' | 'warning';

const BADGE_TONE_CLASS: Record<AutoReviewerBadgeTone, string> = {
  danger: 'border-danger bg-danger-subtle text-danger',
  info: 'border-accent bg-accent-subtle text-accent',
  neutral: 'border-border bg-secondary text-text-muted',
  success: 'border-ok bg-ok-subtle text-ok',
  warning: 'border-warn bg-warn-subtle text-warn',
};

const MANUAL_DECISION_SOURCES = new Set(['manual_approval']);
const FAILURE_STATUSES = new Set<AutoReviewerStatus>(['blocked', 'denied', 'host_revalidation_failed']);
const MANUAL_ACTION_STATUSES = new Set<AutoReviewerStatus>(['aborted', 'fallback', 'manual', 'timed_out']);
const RISK_LEVELS = new Set(['critical', 'high', 'medium', 'low']);

export type ReviewerDecisionSourceCategory = 'automatic' | 'manual' | 'system';

export function reviewerDecisionSourceCategory(source?: string): ReviewerDecisionSourceCategory | undefined {
  const normalized = source?.trim();
  if (!normalized) return undefined;
  if (normalized === 'auto_reviewer') return 'automatic';
  if (MANUAL_DECISION_SOURCES.has(normalized)) return 'manual';
  return 'system';
}

export function reviewerDisplayStatus(reviewer?: AutoReviewerMetadata): AutoReviewerStatus | undefined {
  const finalStatus = normalizeReviewerStatus(reviewer?.final_reviewer_status);
  const intermediateStatus = normalizeReviewerStatus(reviewer?.reviewer_status);
  const failureStatus = [finalStatus, intermediateStatus].find((status): status is AutoReviewerStatus => Boolean(status && FAILURE_STATUSES.has(status)));
  if (failureStatus) return failureStatus;
  if (finalStatus) return finalStatus;
  if (intermediateStatus === 'approved' || intermediateStatus === 'deterministic_allow') {
    return undefined;
  }
  return intermediateStatus;
}

export function reviewerBadgeTone(reviewer?: AutoReviewerMetadata): AutoReviewerBadgeTone {
  const status = reviewerDisplayStatus(reviewer);
  if (!status || status === 'aborted') return 'neutral';
  if (FAILURE_STATUSES.has(status)) return 'danger';
  if (status === 'manual' || status === 'fallback' || status === 'timed_out') {
    return 'warning';
  }
  if (status === 'approved' || status === 'deterministic_allow') {
    return reviewerDecisionSourceCategory(reviewer?.decision_source) === 'manual' ? 'info' : 'success';
  }
  return 'info';
}

export function reviewerNeedsManualAction(reviewer?: AutoReviewerMetadata): boolean {
  const status = reviewerDisplayStatus(reviewer);
  return Boolean(status && MANUAL_ACTION_STATUSES.has(status));
}

export function reviewerRiskLevel(reviewer?: AutoReviewerMetadata): string {
  const normalized = reviewer?.risk_level?.trim().toLowerCase();
  return normalized && RISK_LEVELS.has(normalized) ? normalized : 'unknown';
}

function statusLabelKey(status: AutoReviewerStatus, reviewer?: AutoReviewerMetadata): string {
  const sourceCategory = reviewerDecisionSourceCategory(reviewer?.decision_source);
  if (sourceCategory === 'manual' && (status === 'approved' || status === 'deterministic_allow')) {
    return 'chatUi.autoReviewer.status.userApproved';
  }
  if (sourceCategory === 'manual' && status === 'denied') {
    return 'chatUi.autoReviewer.status.userRejected';
  }
  if (status === 'approved' || status === 'deterministic_allow') {
    return 'chatUi.autoReviewer.status.approved';
  }
  if (status === 'denied') return 'chatUi.autoReviewer.status.denied';
  if (status === 'blocked') return 'chatUi.autoReviewer.status.blocked';
  if (status === 'host_revalidation_failed') {
    return 'chatUi.autoReviewer.status.hostRevalidationFailed';
  }
  if (status === 'in_progress') return 'chatUi.autoReviewer.status.inProgress';
  if (status === 'timed_out') return 'chatUi.autoReviewer.status.timedOut';
  if (status === 'fallback') return 'chatUi.autoReviewer.status.fallback';
  if (status === 'aborted') return 'chatUi.autoReviewer.status.aborted';
  return 'chatUi.autoReviewer.status.manual';
}

export function AutoReviewerStatusBadge({ reviewer }: { reviewer?: AutoReviewerMetadata }) {
  const { t } = useTranslation();
  const status = reviewerDisplayStatus(reviewer);
  if (!status) return null;
  const tone = reviewerBadgeTone(reviewer);
  return (
    <span
      className={clsx('inline-flex shrink-0 items-center rounded border px-2 py-0.5 text-[11px] font-medium', BADGE_TONE_CLASS[tone])}
      data-badge-tone={tone}
      data-testid={`auto-reviewer-badge-${status.replace(/_/g, '-')}`}
    >
      {t(statusLabelKey(status, reviewer))}
    </span>
  );
}

function displayText(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

export function reviewerDetailValues(reviewer?: AutoReviewerMetadata) {
  const manualReason = reviewerNeedsManualAction(reviewer) ? displayText(reviewer?.manual_reason_summary) : undefined;
  const evidence = displayText(reviewer?.evidence_summary);
  return {
    reason: manualReason ?? evidence,
  };
}

export function AutoReviewerDetails({ reviewer }: { reviewer?: AutoReviewerMetadata }) {
  const { t } = useTranslation();
  if (!reviewer) return null;
  const details = reviewerDetailValues(reviewer);
  const sourceCategory = reviewerDecisionSourceCategory(reviewer.decision_source);
  const sourceLabel = sourceCategory ? t(`chatUi.autoReviewer.details.sourceValues.${sourceCategory}`) : undefined;
  const status = reviewerDisplayStatus(reviewer);
  const genericReason = status
    ? t(
        `chatUi.autoReviewer.details.reasonValues.${
          FAILURE_STATUSES.has(status) ? 'denied' : MANUAL_ACTION_STATUSES.has(status) ? 'manual' : status === 'in_progress' ? 'inProgress' : 'approved'
        }`
      )
    : undefined;
  const manualGuidance = reviewerNeedsManualAction(reviewer)
    ? (displayText(reviewer.user_review_hint) ?? t('chatUi.autoReviewer.details.manualGuidanceFallback'))
    : undefined;
  const rows = [
    [t('chatUi.autoReviewer.details.source'), sourceLabel],
    [t('chatUi.autoReviewer.details.risk'), t(`chatUi.autoReviewer.details.riskValues.${reviewerRiskLevel(reviewer)}`)],
    [t('chatUi.autoReviewer.details.reason'), details.reason ?? genericReason],
    [t('chatUi.autoReviewer.details.hint'), manualGuidance],
  ].filter((row): row is [string, string] => Boolean(row[1]));
  if (!rows.length) return null;
  return (
    <div className="mt-3 rounded-lg border border-border bg-card p-3 text-xs" data-testid="auto-reviewer-details">
      <div className="mb-2 flex items-center gap-2 font-semibold text-text">
        <span>{t('chatUi.autoReviewer.title')}</span>
        <AutoReviewerStatusBadge reviewer={reviewer} />
      </div>
      <div className="grid gap-1 sm:grid-cols-2">
        {rows.map(([label, value]) => (
          <div className="min-w-0" key={label}>
            <span className="text-text-muted">{label}: </span>
            <span className="break-words text-text">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
