import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, RefreshCw, Shield, X } from 'lucide-react';
import {
  fetchApprovalCandidates,
  type ApprovalCandidate,
  type SkillUploadPermissionState,
} from '../../services/skillUploadPermission';
import { Dialog, Select, Tag, Textarea } from '../ui';
import type { UploadPermissionPhase } from './skillUploadPermissionState';

/** 企业版「申请上传权限」弹窗：填写理由并选择审批人（与配额申请一致）。 */
export function SkillUploadPermissionPanel({
  state,
  phase,
  errorMessage,
  submitting,
  onRefresh,
  onSubmit,
  onCancel,
}: {
  state: SkillUploadPermissionState | null;
  phase: UploadPermissionPhase;
  errorMessage: string;
  submitting: boolean;
  onRefresh: () => void;
  onSubmit: (reason: string, approverId: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');
  const [approverId, setApproverId] = useState('');
  const [candidates, setCandidates] = useState<ApprovalCandidate[]>([]);
  const [candidatesError, setCandidatesError] = useState('');
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [candidatesReloadKey, setCandidatesReloadKey] = useState(0);

  const pending = state?.application_status === 'pending';
  const rejected = state?.application_status === 'rejected';
  const checking = phase === 'loading';
  const unavailable = phase === 'error';
  const canFillForm = !pending && !unavailable;

  useEffect(() => {
    if (!canFillForm) return;
    let cancelled = false;
    setCandidatesLoading(true);
    setCandidatesError('');
    fetchApprovalCandidates()
      .then((items) => {
        if (cancelled) return;
        setCandidates(items);
        setApproverId((current) =>
          current && items.some((item) => item.user_id === current)
            ? current
            : items[0]?.user_id || '',
        );
      })
      .catch((error) => {
        if (cancelled) return;
        setCandidates([]);
        setApproverId('');
        setCandidatesError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (!cancelled) setCandidatesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [canFillForm, candidatesReloadKey]);

  const statusLabel = pending
    ? t('skills.uploadPermission.statusPending')
    : rejected
      ? t('skills.uploadPermission.statusRejected')
      : unavailable
        ? t('skills.uploadPermission.statusUnavailable')
        : '';
  const approverOptions = candidates.map((candidate) => ({
    value: candidate.user_id,
    label: candidate.display_name || candidate.user_id,
  }));
  const approverPlaceholder = candidatesLoading
    ? t('skills.uploadPermission.approverLoading')
    : t('skills.uploadPermission.noApprover');
  const canSubmit =
    !checking &&
    !submitting &&
    !candidatesLoading &&
    candidates.length > 0 &&
    Boolean(reason.trim()) &&
    Boolean(approverId);

  return (
    <Dialog
      open
      titleId="skill-upload-permission-title"
      closeDisabled={submitting}
      onCancel={onCancel}
      onBackdropClick={onCancel}
      className="flex max-h-[calc(100vh-48px)] flex-col overflow-hidden"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border-weak px-5 py-4">
        <div className="flex min-w-0 items-start gap-2.5">
          <Shield
            size={18}
            strokeWidth={1.8}
            className="mt-0.5 shrink-0 text-accent"
            aria-hidden="true"
          />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3
                id="skill-upload-permission-title"
                className="text-base font-semibold leading-6 text-text-strong"
              >
                {t('skills.uploadPermission.title')}
              </h3>
              {statusLabel && (
                <Tag variant={pending ? 'warning' : 'danger'}>{statusLabel}</Tag>
              )}
            </div>
            <p className="mt-1 break-words text-xs leading-5 text-text-muted">
              {t('skills.uploadPermission.subtitle')}
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          aria-label={t('skills.uploadPermission.close')}
          className="shrink-0 rounded-md p-1.5 text-text-muted transition-colors hover:bg-secondary hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {pending && (
          <div className="flex w-full items-center gap-2">
            <div className="min-w-0 flex-1 break-words rounded-lg border border-warn/30 bg-warn-subtle px-3 py-2 text-xs leading-4 text-warn">
              {t('skills.uploadPermission.pendingHint', { orderNum: state?.order_num || '—' })}
            </div>
            <button
              type="button"
              title={t('skills.uploadPermission.refresh')}
              aria-label={t('skills.uploadPermission.refresh')}
              onClick={onRefresh}
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-text-muted transition-colors hover:bg-secondary hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
              data-testid="skill-upload-permission-refresh"
            >
              <RefreshCw size={14} aria-hidden="true" />
            </button>
          </div>
        )}

        {rejected && (
          <div className="rounded-lg border border-danger/30 bg-danger-subtle px-3 py-2 text-xs leading-4 text-danger">
            {state?.reject_comment
              ? t('skills.uploadPermission.rejectedWithComment', { comment: state.reject_comment })
              : t('skills.uploadPermission.rejected')}
          </div>
        )}

        {unavailable && (
          <div className="flex w-full items-center gap-2">
            <div className="min-w-0 flex-1 break-words rounded-lg border border-danger/30 bg-danger-subtle px-3 py-2 text-xs leading-4 text-danger">
              {t('skills.uploadPermission.unavailable')}
              {errorMessage ? `：${errorMessage}` : ''}
            </div>
            <button
              type="button"
              title={t('skills.uploadPermission.retry')}
              aria-label={t('skills.uploadPermission.retry')}
              onClick={onRefresh}
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-text-muted transition-colors hover:bg-secondary hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
              data-testid="skill-upload-permission-retry"
            >
              <RefreshCw size={14} aria-hidden="true" />
            </button>
          </div>
        )}

        {canFillForm && (
          <div className={`space-y-4${rejected ? ' mt-4' : ''}`}>
            <div>
              <label
                className="block text-sm font-medium text-text"
                htmlFor="skill-upload-approver"
              >
                {t('skills.uploadPermission.approver')}
                <span className="ml-0.5 text-danger" aria-hidden="true">
                  *
                </span>
              </label>
              <Select
                id="skill-upload-approver"
                className="mt-1.5 w-full"
                options={approverOptions}
                value={approverId}
                placeholder={approverPlaceholder}
                onChange={setApproverId}
                disabled={checking || submitting || candidatesLoading || candidates.length === 0}
                invalid={Boolean(candidatesError)}
                data-testid="skill-upload-permission-approver"
                aria-required="true"
              />
              {candidatesError ? (
                <div className="mt-1.5 flex flex-wrap items-center gap-1 text-[11px] leading-4 text-danger">
                  <span>{t('skills.uploadPermission.approverLoadFailed')}</span>
                  <button
                    type="button"
                    className="font-medium underline underline-offset-2"
                    onClick={() => setCandidatesReloadKey((value) => value + 1)}
                  >
                    {t('skills.uploadPermission.retry')}
                  </button>
                </div>
              ) : candidates.length === 0 && !candidatesLoading ? (
                <div className="mt-1.5 text-[11px] leading-4 text-danger">
                  {t('skills.uploadPermission.noApprover')}
                </div>
              ) : (
                <div className="mt-1.5 text-[11px] leading-4 text-text-muted">
                  {t('skills.uploadPermission.approverHint')}
                </div>
              )}
            </div>

            <div>
              <label
                className="block text-sm font-medium text-text"
                htmlFor="skill-upload-reason"
              >
                {t('skills.uploadPermission.reason')}
                <span className="ml-0.5 text-danger" aria-hidden="true">
                  *
                </span>
              </label>
              <Textarea
                id="skill-upload-reason"
                className="mt-1.5"
                rows={2}
                maxLength={1024}
                value={reason}
                disabled={checking || submitting}
                placeholder={t('skills.uploadPermission.reasonPlaceholder')}
                onChange={setReason}
                showCounter
                counterTestId="skill-upload-permission-reason-counter"
                data-testid="skill-upload-permission-reason"
                aria-required="true"
              />
            </div>
          </div>
        )}
      </div>

      {canFillForm && (
        <div className="flex flex-col-reverse gap-2 border-t border-border-weak px-5 py-3 sm:flex-row sm:justify-end">
          <button
            className="btn ghost h-8 px-4 text-xs"
            onClick={onCancel}
            disabled={submitting}
          >
            {t('skills.uploadPermission.cancel')}
          </button>
          <button
            className="btn primary inline-flex h-8 items-center justify-center gap-1.5 px-4 text-xs"
            disabled={!canSubmit}
            onClick={() => onSubmit(reason.trim(), approverId)}
            data-testid="skill-upload-permission-submit"
          >
            {submitting && <Loader2 size={14} className="animate-spin" aria-hidden="true" />}
            {submitting ? t('skills.uploadPermission.submitting') : t('skills.uploadPermission.submit')}
          </button>
        </div>
      )}
    </Dialog>
  );
}
