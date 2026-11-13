/**
 * 企业版「我的上传」：按进行中 / 历史 / 全部查看审批单。
 *
 * 审批通过后由用户在此手动安装；安装成功由父组件关闭弹窗并进入技能详情。
 */
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CloseButton, EmptyState, LoadingSpinner } from '../ui';
import {
  isSkillUploadInstallable,
  listSkillUploadRequests,
  type SkillUploadRequestItem,
  type SkillUploadScope,
} from '../../services/enterpriseSkillUpload';
import { formatTimestamp } from '../../utils/formatters';

const PAGE_SIZE = 10;

interface MySkillUploadsModalProps {
  jiuwenclawId: string;
  /** 安装审批通过的包；成功后由父组件关闭弹窗并打开技能详情。 */
  onInstall: (item: SkillUploadRequestItem) => Promise<void>;
  onClose: () => void;
}

function itemStatusKey(item: SkillUploadRequestItem): string {
  if (item.status === 'rejected') return 'rejected';
  if (item.status === 'expired') return 'expired';
  if (item.status === 'pending') return 'pending';
  if (item.install_status === 'installed') return 'installed';
  if (item.install_status === 'failed') return 'installFailed';
  if (!isSkillUploadInstallable(item)) return 'expired';
  return 'approved';
}

const STATUS_CLASS: Record<string, string> = {
  pending: 'bg-warn-subtle text-warn',
  approved: 'bg-secondary text-text-link',
  installed: 'bg-ok-subtle text-ok',
  installFailed: 'bg-danger-subtle text-danger',
  expired: 'bg-danger-subtle text-danger',
  rejected: 'bg-danger-subtle text-danger',
};

export function MySkillUploadsModal({ jiuwenclawId, onInstall, onClose }: MySkillUploadsModalProps) {
  const { t } = useTranslation();
  const [scope, setScope] = useState<SkillUploadScope>('active');
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<SkillUploadRequestItem[]>([]);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [installingOrder, setInstallingOrder] = useState('');
  const [installError, setInstallError] = useState<{ orderNum: string; message: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await listSkillUploadRequests({
        jiuwenclawId,
        scope,
        page,
        pageSize: PAGE_SIZE,
      });
      setItems(data.items ?? []);
      setHasNext(Boolean(data.has_next));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
      setItems([]);
      setHasNext(false);
    } finally {
      setLoading(false);
    }
  }, [jiuwenclawId, scope, page]);

  useEffect(() => {
    void load();
  }, [load]);

  const install = useCallback(
    async (item: SkillUploadRequestItem) => {
      setInstallError(null);
      setInstallingOrder(item.order_num);
      try {
        await onInstall(item);
      } catch (installFailure) {
        setInstallError({
          orderNum: item.order_num,
          message: installFailure instanceof Error ? installFailure.message : String(installFailure),
        });
      } finally {
        setInstallingOrder('');
      }
    },
    [onInstall],
  );

  const loadedCount = (page - 1) * PAGE_SIZE + items.length;
  const loadedLabel = hasNext ? `${loadedCount}+` : `${loadedCount}`;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t('skills.skillUploads.close')}
      />
      <div
        className="relative flex flex-col overflow-hidden rounded-[8px] border border-border bg-card shadow-2xl animate-rise"
        style={{ width: '720px', maxHeight: '80vh' }}
        data-testid="skill-panel-my-uploads-modal"
      >
        <div className="flex items-center justify-between gap-3 px-6 pt-6 pb-3 bg-panel">
          <span className="text-lg font-semibold text-text-strong" data-testid="skill-panel-my-uploads-title">
            {t('skills.skillUploads.title')}
          </span>
          <CloseButton onClick={onClose} />
        </div>

        <div className="flex items-center gap-2 px-6 pb-3" data-testid="skill-panel-my-uploads-scope">
          {(['active', 'history', 'all'] as SkillUploadScope[]).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => {
                setScope(value);
                setPage(1);
              }}
              aria-pressed={scope === value}
              className={`rounded-[16px] px-4 py-1 text-sm border ${
                scope === value
                  ? 'border-control-emphasis text-text-strong bg-secondary/40'
                  : 'border-border text-text-muted hover:text-text'
              }`}
              data-testid="skill-panel-my-uploads-scope-tab"
              data-variant={value}
            >
              {t(`skills.skillUploads.scope.${value}`)}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto px-6 pb-4" data-testid="skill-panel-my-uploads-list">
          {loading ? (
            <div className="flex justify-center py-10">
              <LoadingSpinner />
            </div>
          ) : error ? (
            <div className="py-6 text-sm text-danger" role="alert" data-testid="skill-panel-my-uploads-error">
              {error}
            </div>
          ) : items.length === 0 ? (
            <EmptyState id="skill-panel-my-uploads-empty" text={t('skills.skillUploads.empty')} />
          ) : (
            <ul className="flex flex-col gap-2">
              {items.map((item) => {
                const statusKey = itemStatusKey(item);
                const installable = isSkillUploadInstallable(item);
                const showInstall = item.status === 'approved' && item.install_status !== 'installed';
                const packageExpired = item.package_expires_at
                  ? new Date(item.package_expires_at).getTime() <= Date.now()
                  : false;
                return (
                  <li
                    key={item.order_num}
                    className="rounded-[8px] border border-border px-4 py-2.5"
                    data-testid="skill-panel-my-uploads-item"
                    data-variant={item.skill_name}
                  >
                    <div className="flex items-start gap-4">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span className="text-sm font-medium text-text-strong break-all" data-testid="skill-panel-my-uploads-skill-name">
                            {item.skill_name}
                          </span>
                          {item.skill_version ? (
                            <span
                              className="inline-flex items-center rounded-[4px] bg-secondary px-1.5 py-0.5 text-[11px] leading-none text-text-muted"
                              data-testid="skill-panel-my-uploads-skill-version"
                            >
                              {item.skill_version}
                            </span>
                          ) : null}
                          <span
                            className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium leading-none ${
                              STATUS_CLASS[statusKey] ?? 'bg-secondary text-text-muted'
                            }`}
                            data-testid="skill-panel-my-uploads-status"
                          >
                            {t(`skills.skillUploads.status.${statusKey}`)}
                          </span>
                        </div>
                        <div
                          className="mt-1 truncate text-xs text-text-muted"
                          title={item.order_num}
                          data-testid="skill-panel-my-uploads-order-num"
                        >
                          {item.order_num}
                        </div>
                        {item.package_expires_at
                        && (item.status === 'pending' || (item.status === 'approved' && installable)) ? (
                          <div
                            className={`mt-1 text-xs ${packageExpired ? 'text-danger' : 'text-text-muted'}`}
                            data-testid="skill-panel-my-uploads-retention"
                          >
                            {packageExpired
                              ? t('skills.skillUploads.packageExpired')
                              : t('skills.skillUploads.packageRetention', {
                                expiresAt: formatTimestamp(item.package_expires_at),
                              })}
                          </div>
                        ) : null}
                        {item.latest_reject_comment ? (
                          <div className="mt-1.5 text-xs text-danger whitespace-pre-wrap" data-testid="skill-panel-my-uploads-reject-comment">
                            {t('skills.skillUploads.rejectComment', { comment: item.latest_reject_comment })}
                          </div>
                        ) : null}
                        {item.install_status === 'failed' && item.install_detail ? (
                          <div className="mt-1.5 text-xs text-danger whitespace-pre-wrap" data-testid="skill-panel-my-uploads-install-detail">
                            {t('skills.skillUploads.installDetail', { detail: item.install_detail })}
                          </div>
                        ) : null}
                        {installError?.orderNum === item.order_num ? (
                          <div className="mt-1.5 text-xs text-danger whitespace-pre-wrap" role="alert" data-testid="skill-panel-my-uploads-install-error">
                            {t('skills.skillUploads.installFailedHint', { detail: installError.message })}
                          </div>
                        ) : null}
                        {showInstall && !installable ? (
                          <div className="mt-1 text-xs text-danger" data-testid="skill-panel-my-uploads-install-expired">
                            {t('skills.skillUploads.packageExpired')}
                          </div>
                        ) : null}
                      </div>
                      <div className="flex w-[120px] shrink-0 flex-col items-end gap-2">
                        <span className="whitespace-nowrap text-xs text-text-muted" data-testid="skill-panel-my-uploads-created-at">
                          {formatTimestamp(item.created_at ?? '')}
                        </span>
                        {showInstall ? (
                          <button
                            type="button"
                            className="inline-flex h-7 min-w-[64px] items-center justify-center rounded-[6px] bg-control-emphasis px-3 text-xs font-medium text-text-inverse transition-opacity hover:opacity-85 disabled:cursor-not-allowed disabled:opacity-50"
                            disabled={!installable || installingOrder !== ''}
                            onClick={() => void install(item)}
                            data-testid="skill-panel-my-uploads-install-btn"
                            data-variant={item.order_num}
                          >
                            {installingOrder === item.order_num
                              ? t('skills.actions.installing')
                              : t('skills.actions.install')}
                          </button>
                        ) : null}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <div className="flex items-center justify-between px-6 py-3 bg-panel text-xs text-text-muted">
          <span data-testid="skill-panel-my-uploads-total">{t('skills.skillUploads.total', { total: loadedLabel })}</span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="inline-flex h-7 items-center justify-center rounded-[6px] px-2.5 text-xs text-text-muted transition-colors hover:bg-secondary hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
              disabled={page <= 1 || loading}
              onClick={() => setPage((value) => Math.max(1, value - 1))}
              data-testid="skill-panel-my-uploads-prev"
            >
              {t('skills.skillUploads.prev')}
            </button>
            <span data-testid="skill-panel-my-uploads-page">
              {page}
            </span>
            <button
              type="button"
              className="inline-flex h-7 items-center justify-center rounded-[6px] px-2.5 text-xs text-text-muted transition-colors hover:bg-secondary hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
              disabled={!hasNext || loading}
              onClick={() => setPage((value) => value + 1)}
              data-testid="skill-panel-my-uploads-next"
            >
              {t('skills.skillUploads.next')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
