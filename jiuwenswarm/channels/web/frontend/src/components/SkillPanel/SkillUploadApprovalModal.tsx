/**
 * 企业版「上传 Skill」分步弹窗：选包 → 暂存校验/查重 → 指定审批人提交。
 *
 * 暂存由 AgentServer 完成（返回签名描述符），提交后等管理员审批；
 * 审批通过后由用户在「我的上传」里手动安装。
 */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import TipIcon from '../../assets/tip.svg?react';
import UpFileIcon from '../../assets/upFile.svg?react';
import { CloseButton, LoadingSpinner, Select, Textarea } from '../ui';
import {
  listApprovalCandidates,
  type ApprovalCandidate,
  type SkillUploadStageResult,
  type SkillUploadSubmitResult,
  type StagedSkillUpload,
} from '../../services/enterpriseSkillUpload';
import { formatTimestamp } from '../../utils/formatters';

type Step = 'select' | 'review' | 'submit' | 'done';

interface SkillUploadApprovalModalProps {
  onStage: (file: File) => Promise<StagedSkillUpload>;
  /** 暂存成功后查重：命中同名或同包的进行中单时返回既有单。 */
  onFindActive: (
    packageInfo: SkillUploadStageResult['package_info'],
  ) => Promise<SkillUploadSubmitResult | null>;
  onSubmit: (input: {
    reason: string;
    approverId: string;
    replace: boolean;
    packageInfo: SkillUploadStageResult['package_info'];
  }) => Promise<SkillUploadSubmitResult>;
  onClose: () => void;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function SkillUploadApprovalModal({
  onStage,
  onFindActive,
  onSubmit,
  onClose,
}: SkillUploadApprovalModalProps) {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>('select');
  const [filePath, setFilePath] = useState('');
  const [stageLoading, setStageLoading] = useState(false);
  const [staged, setStaged] = useState<SkillUploadStageResult | null>(null);
  const [replace, setReplace] = useState(false);
  const [reason, setReason] = useState('');
  const [approverId, setApproverId] = useState('');
  const [candidates, setCandidates] = useState<ApprovalCandidate[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<SkillUploadSubmitResult | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    setCandidatesLoading(true);
    listApprovalCandidates()
      .then((items) => setCandidates(items))
      .catch((loadError) => setError(errorText(loadError)))
      .finally(() => setCandidatesLoading(false));
  }, []);

  const approverOptions = useMemo(
    () =>
      candidates.map((item) => ({
        value: item.user_id,
        label: item.display_name || item.user_id,
      })),
    [candidates],
  );

  const existingSkill = staged?.existing_skill ?? null;
  // 暂存内容与 OBS 副本同为 stage 后保留期；过期后需重新上传。
  const packageExpiresAt = staged?.package_info.stage_expires_at || '';
  const canSubmit = Boolean(approverId) && Boolean(reason.trim()) && !submitting;
  const canLeave = !stageLoading && !submitting;

  const runStage = async (nextFile: File) => {
    setError('');
    setStageLoading(true);
    setStaged(null);
    try {
      const data = await onStage(nextFile);
      setStaged(data);
      setReplace(false);
      // 暂存成功即查重：命中时直接提示，不再让用户白选审批人。
      const active = await onFindActive(data.package_info).catch(() => null);
      if (active) {
        setResult(active);
        setStep('done');
      } else {
        setStep('review');
      }
    } catch (stageError) {
      setError(errorText(stageError));
      setStep('select');
    } finally {
      setStageLoading(false);
    }
  };

  const pickFile = (nextFile: File | null | undefined) => {
    // 拖拽入口不受 input 的 disabled 约束，暂存期间必须在这里拦住，避免并发暂存。
    if (!nextFile || stageLoading) return;
    const lowerName = nextFile.name.toLowerCase();
    if (lowerName.endsWith('.skill.zip')) {
      setError(t('skills.errors.uploadSkillZipRejected'));
      return;
    }
    if (!lowerName.endsWith('.zip')) {
      setError(t('skills.errors.uploadZipOnly'));
      return;
    }
    setFilePath(nextFile.name);
    void runStage(nextFile);
  };

  const submit = async () => {
    if (!staged || !canSubmit) return;
    setError('');
    setSubmitting(true);
    try {
      const submitted = await onSubmit({
        reason: reason.trim(),
        approverId,
        replace: existingSkill ? replace : false,
        packageInfo: staged.package_info,
      });
      setResult(submitted);
      setStep('done');
    } catch (submitError) {
      setError(errorText(submitError));
    } finally {
      setSubmitting(false);
    }
  };

  const titleKey =
    step === 'submit'
      ? 'skills.skillUploadApproval.submitTitle'
      : step === 'done'
        ? result?.reused
          ? 'skills.skillUploadApproval.duplicateTitle'
          : 'skills.skillUploadApproval.doneTitle'
        : 'skills.skillUploadApproval.title';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        onClick={() => {
          if (canLeave) onClose();
        }}
        aria-label={t('skills.uploadSkillModal.cancel')}
      />
      <div
        className="relative overflow-hidden rounded-[8px] border border-border bg-card shadow-2xl animate-rise flex flex-col"
        style={{ width: '620px' }}
        data-testid="skill-panel-upload-approval-modal"
        data-variant={step}
      >
        <div className="flex items-center justify-between gap-3 px-6 pt-6 bg-panel">
          <span
            data-testid="skill-panel-upload-approval-modal-title"
            className="text-lg font-semibold text-text-strong"
          >
            {t(titleKey)}
          </span>
          <CloseButton
            onClick={() => {
              if (canLeave) onClose();
            }}
          />
        </div>

        {step === 'select' && (
          <>
            <div className="px-6 pt-4">
              <div className="flex items-start gap-1.5 rounded-[8px] px-4 py-2 text-xs text-text bg-[var(--color-skill-notice-surface)]">
                <TipIcon className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                <span className="leading-4">{t('skills.skillUploadApproval.notice')}</span>
              </div>
            </div>
            <div className="px-6 pt-4 pb-4">
              <label
                onDragOver={(event) => {
                  event.preventDefault();
                }}
                onDrop={(event) => {
                  event.preventDefault();
                  pickFile(event.dataTransfer.files[0]);
                }}
                className="flex flex-col items-center justify-center gap-2 rounded-[12px] border border-dashed border-border cursor-pointer bg-[var(--color-skill-dropzone-surface)] hover:bg-[var(--color-skill-dropzone-hover-surface)]"
                style={{ height: '160px' }}
                data-testid="skill-panel-upload-approval-dropzone"
              >
                {stageLoading ? (
                  <LoadingSpinner />
                ) : (
                  <UpFileIcon className="w-6 h-6 text-text-muted" />
                )}
                <span className="text-sm text-text-muted">
                  {stageLoading
                    ? t('skills.skillUploadApproval.staging')
                    : filePath.trim() || t('skills.uploadSkillModal.dropHint')}
                </span>
                <input
                  type="file"
                  accept=".zip"
                  className="hidden"
                  disabled={stageLoading}
                  onChange={(event) => pickFile(event.target.files?.[0])}
                  data-testid="skill-panel-upload-approval-file-input"
                />
              </label>
            </div>
          </>
        )}

        {step === 'review' && staged && (
          <div className="px-6 pt-4 pb-4 flex flex-col gap-3">
            <dl className="grid grid-cols-[120px_1fr] gap-x-3 gap-y-2 text-sm">
              <dt className="text-text-muted">{t('skills.skillUploadApproval.skillName')}</dt>
              <dd className="break-all text-text-strong" data-testid="skill-panel-upload-approval-skill-name">
                {staged.package_info.skill_name}
              </dd>
              {staged.package_info.skill_version && (
                <>
                  <dt className="text-text-muted">{t('skills.skillUploadApproval.skillVersion')}</dt>
                  <dd className="break-all text-text">{staged.package_info.skill_version}</dd>
                </>
              )}
              <dt className="text-text-muted">{t('skills.skillUploadApproval.fileCount')}</dt>
              <dd className="text-text">{staged.package_info.file_count}</dd>
            </dl>
            {packageExpiresAt ? (
              <div className="flex items-start gap-1.5 rounded-[8px] px-4 py-2 text-xs text-text bg-[var(--color-skill-notice-surface)]">
                <TipIcon className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                <span
                  className="leading-4"
                  data-testid="skill-panel-upload-approval-retention"
                >
                  {t('skills.skillUploadApproval.retentionHint', {
                    expiresAt: formatTimestamp(packageExpiresAt),
                  })}
                </span>
              </div>
            ) : null}
            {existingSkill ? (
              <div className="rounded-[8px] border border-border px-4 py-3 text-xs text-text">
                <div className="text-warn" data-testid="skill-panel-upload-approval-existing-skill">
                  {t('skills.skillUploadApproval.existingSkill', {
                    name: existingSkill.name,
                    version: existingSkill.version
                      ? t('skills.skillUploadApproval.existingVersion', { version: existingSkill.version })
                      : '',
                  })}
                </div>
                <label className="mt-2 flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={replace}
                    onChange={(event) => setReplace(event.target.checked)}
                    data-testid="skill-panel-upload-approval-replace"
                  />
                  <span>{t('skills.skillUploadApproval.replaceHint')}</span>
                </label>
              </div>
            ) : null}
          </div>
        )}

        {step === 'submit' && staged && (
          <div className="px-6 pt-4 pb-4 flex flex-col gap-3">
            <label className="flex flex-col gap-1.5">
              <span className="field-label">{t('skills.skillUploadApproval.approver')}</span>
              <Select
                options={approverOptions}
                value={approverId}
                onChange={setApproverId}
                placeholder={
                  candidatesLoading
                    ? t('skills.skillUploadApproval.approverLoading')
                    : t('skills.skillUploadApproval.approverPlaceholder')
                }
                disabled={candidatesLoading || candidates.length === 0}
                data-testid="skill-panel-upload-approval-approver"
              />
            </label>
            <label className="flex flex-col gap-1.5">
              <span className="field-label">{t('skills.skillUploadApproval.reason')}</span>
              <Textarea
                rows={4}
                maxLength={1024}
                value={reason}
                onChange={setReason}
                placeholder={t('skills.skillUploadApproval.reasonPlaceholder')}
                data-testid="skill-panel-upload-approval-reason"
              />
            </label>
          </div>
        )}

        {step === 'done' && result && (
          <div className="px-6 pt-4 pb-4 flex flex-col gap-2 text-sm">
            <div className="text-text" data-testid="skill-panel-upload-approval-result">
              {t(
                result.reused
                  ? 'skills.skillUploadApproval.reused'
                  : 'skills.skillUploadApproval.submitted',
                { orderNum: result.order_num },
              )}
            </div>
            <div className="text-xs text-text-muted" data-testid="skill-panel-upload-approval-submitted-hint">
              {t('skills.skillUploadApproval.submittedHint')}
            </div>
            {packageExpiresAt && !result.reused ? (
              <div className="text-xs text-warn" data-testid="skill-panel-upload-approval-done-retention">
                {t('skills.skillUploadApproval.retentionHint', {
                  expiresAt: formatTimestamp(packageExpiresAt),
                })}
              </div>
            ) : null}
          </div>
        )}

        {error ? (
          <div className="px-6 pb-2 text-xs text-danger" role="alert" data-testid="skill-panel-upload-approval-error">
            {error}
          </div>
        ) : null}

        <div className="flex items-center justify-end gap-3 px-5 pb-6 bg-panel">
          {step === 'select' && (
            <button
              type="button"
              onClick={onClose}
              disabled={!canLeave}
              className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
              style={{ height: '32px', padding: '0 32px' }}
              data-testid="skill-panel-upload-approval-cancel"
            >
              {t('skills.uploadSkillModal.cancel')}
            </button>
          )}
          {step === 'review' && (
            <>
              <button
                type="button"
                onClick={() => {
                  setStep('select');
                  setFilePath('');
                  setStaged(null);
                  setError('');
                }}
                className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
                style={{ height: '32px', padding: '0 32px' }}
                data-testid="skill-panel-upload-approval-back"
              >
                {t('skills.skillUploadApproval.back')}
              </button>
              <button
                type="button"
                disabled={Boolean(existingSkill) && !replace}
                onClick={() => {
                  setError('');
                  setStep('submit');
                }}
                className={`flex items-center justify-center rounded-[16px] text-sm whitespace-nowrap transition-colors ${
                  existingSkill && !replace
                    ? 'bg-bg-muted border border-text-divider text-text-disabled cursor-not-allowed'
                    : 'text-text-inverse bg-control-emphasis hover:opacity-80'
                }`}
                style={{ height: '32px', padding: '0 32px' }}
                data-testid="skill-panel-upload-approval-next"
              >
                {t('skills.skillUploadApproval.next')}
              </button>
            </>
          )}
          {step === 'submit' && (
            <>
              <button
                type="button"
                disabled={submitting}
                onClick={() => {
                  setError('');
                  setStep('review');
                }}
                className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
                style={{ height: '32px', padding: '0 32px' }}
                data-testid="skill-panel-upload-approval-back"
              >
                {t('skills.skillUploadApproval.back')}
              </button>
              <button
                type="button"
                disabled={!canSubmit}
                onClick={() => void submit()}
                className={`flex items-center justify-center rounded-[16px] text-sm whitespace-nowrap transition-colors ${
                  canSubmit
                    ? 'text-text-inverse bg-control-emphasis hover:opacity-80'
                    : 'bg-bg-muted border border-text-divider text-text-disabled cursor-not-allowed'
                }`}
                style={{ height: '32px', padding: '0 32px' }}
                data-testid="skill-panel-upload-approval-submit"
              >
                {submitting
                  ? t('skills.skillUploadApproval.submitting')
                  : t('skills.skillUploadApproval.submit')}
              </button>
            </>
          )}
          {step === 'done' && (
            <button
              type="button"
              onClick={onClose}
              className="flex items-center justify-center rounded-[16px] text-sm text-text-inverse bg-control-emphasis hover:opacity-80 whitespace-nowrap"
              style={{ height: '32px', padding: '0 32px' }}
              data-testid="skill-panel-upload-approval-finish"
            >
              {t('skills.skillUploadApproval.finish')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
