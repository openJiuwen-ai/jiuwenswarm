import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';

interface ConfirmDialogProps {
  title: string;
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
  confirmLabel?: string;
}

/** 通用确认弹窗：Portal 挂到 body，避免被面板限宽/overflow 裁切。 */
export function ConfirmDialog({
  title,
  message,
  onConfirm,
  onCancel,
  loading = false,
  confirmLabel,
}: ConfirmDialogProps) {
  const { t } = useTranslation();
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay-cron-dialog"
      data-testid="common-confirm-dialog"
      onClick={event => {
        if (event.target === event.currentTarget && !loading) onCancel();
      }}
    >
      <div className="w-[360px] rounded-2xl bg-card p-6 shadow-xl">
        <h2 className="mb-2 text-[15px] font-semibold text-text">{title}</h2>
        <p className="mb-5 text-[13px] leading-5 text-text-muted break-words">{message}</p>
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="flex h-8 items-center justify-center rounded-lg border border-border px-3 text-[13px] text-text hover:border-border-hover disabled:cursor-not-allowed disabled:opacity-60"
            data-testid="common-confirm-cancel"
          >
            {t('common.cancel')}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={loading}
            className="flex h-8 items-center justify-center rounded-lg bg-danger px-3 text-[13px] text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            data-testid="common-confirm-ok"
          >
            {loading ? t('common.loading') : confirmLabel ?? t('common.confirm')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
