import { useTranslation } from 'react-i18next';

interface ConfirmDialogProps {
  title: string;
  message: string;
  confirmLabel?: string;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmDialog({ title, message, confirmLabel, onCancel, onConfirm }: ConfirmDialogProps) {
  const { t } = useTranslation();
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-overlay-cron-dialog"
      data-testid="connector-market-confirm-dialog"
      onClick={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <div className="w-[340px] rounded-2xl bg-card p-6 shadow-xl">
        <h2 className="mb-2 text-[15px] font-semibold text-text">{title}</h2>
        <p className="mb-5 text-[13px] leading-5 text-text-muted">{message}</p>
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="flex h-8 items-center justify-center rounded-lg border border-border px-3 text-[13px] text-text hover:border-border-hover"
            data-testid="connector-market-confirm-cancel"
          >
            {t('connectorMarket.common.cancel')}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="flex h-8 items-center justify-center rounded-lg bg-danger px-3 text-[13px] text-white hover:opacity-90"
            data-testid="connector-market-confirm-ok"
          >
            {confirmLabel ?? t('connectorMarket.common.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}
