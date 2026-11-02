/**
 * Skill 上传资格申请表单（企业版，页内展开）。
 *
 * 用户在管理面无上传资格时从此处提交申请：选择审批人（须持有 approval:act）
 * 并填写申请说明，POST /manager-api/v1/user-console/skills/upload-permission-requests。
 * 审批状态由 Manager approval_order 维护；申请成功后父级应刷新资格状态。
 *
 * 按方案 §6 要求采用页内展开而非弹窗。
 */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Select, Textarea } from '../ui';
import {
  fetchApproverCandidates,
  submitSkillUploadRequest,
  type ApproverCandidate,
  type SkillUploadPermissionContext,
} from '../../services/skillUploadPermissionApi';

interface SkillUploadApplyDialogProps {
  context: SkillUploadPermissionContext | null;
  /** 申请提交成功后触发（父级刷新资格状态）。 */
  onSubmitted: () => void;
  onClose: () => void;
}

function useApproverCandidates(open: boolean): {
  items: ApproverCandidate[];
  loadFailed: boolean;
} {
  const [items, setItems] = useState<ApproverCandidate[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoadFailed(false);
    fetchApproverCandidates()
      .then((list) => {
        if (!cancelled) setItems(list);
      })
      .catch(() => {
        if (!cancelled) {
          setItems([]);
          setLoadFailed(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [open]);
  return { items, loadFailed };
}

export function SkillUploadApplyDialog({ context, onSubmitted, onClose }: SkillUploadApplyDialogProps) {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');
  const [approverId, setApproverId] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState('');
  const { items: candidates, loadFailed } = useApproverCandidates(true);

  useEffect(() => {
    if (!candidates.length || approverId) return;
    setApproverId(candidates[0].user_id);
  }, [candidates, approverId]);

  const approverOptions = useMemo(
    () =>
      candidates.map((item) => ({
        value: item.user_id,
        label: item.display_name || item.user_id,
      })),
    [candidates],
  );

  const canSubmit = Boolean(
    context && reason.trim() && approverId && !submitting && !loadFailed && candidates.length,
  );

  const handleSubmit = async () => {
    if (!context || !canSubmit) return;
    setSubmitting(true);
    setErrorText('');
    try {
      await submitSkillUploadRequest({
        ...context,
        reason: reason.trim(),
        approverId,
      });
      onSubmitted();
      onClose();
    } catch (error) {
      setErrorText(
        error instanceof Error
          ? error.message
          : t('skills.skillUpload.applyDialog.submitFailed'),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="flex flex-col gap-3 rounded-[8px] border border-border bg-card p-4"
      data-testid="skill-panel-upload-apply-form"
    >
      <div className="flex items-center justify-between gap-3">
        <span data-testid="skill-panel-upload-apply-dialog-title" className="text-base font-semibold text-text-strong">
          {t('skills.skillUpload.applyDialog.title')}
        </span>
      </div>
      <div>
        <div className="field-label text-sm text-text-muted mb-1">
          {t('skills.skillUpload.applyDialog.approverLabel')}
        </div>
        {loadFailed ? (
          <div data-testid="skill-panel-upload-apply-approver-error" className="text-sm text-warn">
            {t('skills.skillUpload.applyDialog.approverLoadFailed')}
          </div>
        ) : (
          <Select
            value={approverId}
            options={approverOptions}
            placeholder={t('skills.skillUpload.applyDialog.approverPlaceholder')}
            disabled={submitting}
            onChange={(value) => setApproverId(value)}
            style={{ width: '100%' }}
          />
        )}
      </div>
      <div>
        <div className="field-label text-sm text-text-muted mb-1">
          {t('skills.skillUpload.applyDialog.reasonLabel')}
        </div>
        <Textarea
          value={reason}
          onChange={setReason}
          placeholder={t('skills.skillUpload.applyDialog.reasonPlaceholder')}
          maxLength={500}
          rows={3}
          data-testid="skill-panel-upload-apply-reason-input"
          style={{ width: '100%' }}
        />
      </div>
      {errorText && (
        <div data-testid="skill-panel-upload-apply-error-text">
          <div className="rounded-[8px] px-3 py-2 text-xs text-warn bg-[var(--color-skill-notice-surface)]">
            {errorText}
          </div>
        </div>
      )}
      <div className="flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onClose}
          className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
          style={{ height: '32px', padding: '0 24px' }}
          data-testid="skill-panel-upload-apply-cancel-btn"
        >
          {t('skills.skillUpload.applyDialog.cancel')}
        </button>
        <button
          type="button"
          disabled={!canSubmit}
          onClick={() => void handleSubmit()}
          className={`flex items-center justify-center rounded-[16px] text-sm whitespace-nowrap transition-colors ${
            !canSubmit
              ? 'bg-bg-muted border border-text-divider text-text-disabled cursor-not-allowed'
              : 'text-text-inverse bg-control-emphasis hover:opacity-80'
          }`}
          style={{ height: '32px', padding: '0 24px' }}
          data-testid="skill-panel-upload-apply-submit-btn"
        >
          {submitting
            ? t('skills.skillUpload.applyDialog.submitting')
            : t('skills.skillUpload.applyDialog.submit')}
        </button>
      </div>
    </div>
  );
}
