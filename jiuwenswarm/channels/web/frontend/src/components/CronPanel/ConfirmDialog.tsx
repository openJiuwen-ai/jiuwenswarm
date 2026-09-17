import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

interface ConfirmDialogProps {
  title: string;
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
}

export default function ConfirmDialog({ title, message, onConfirm, onCancel, loading = false }: ConfirmDialogProps) {
  const { t } = useTranslation();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-overlay-cron-dialog" data-testid="cron-confirm-overlay" onClick={onCancel}>
      <div
        className="relative w-[420px] rounded-lg bg-card p-6 shadow-xl animate-scale-in"
        data-testid="cron-confirm-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-5 flex items-center justify-between">
          <h3 className="text-2xl font-bold text-text-strong" data-testid="cron-confirm-title">{title}</h3>
          <button onClick={onCancel} data-testid="cron-confirm-close-btn" className="text-text-muted hover:text-text">
            <X size={20} />
          </button>
        </div>
        <p className="mb-6 break-words text-sm text-text" data-testid="cron-confirm-message">{message}</p>
        <div className="flex justify-center gap-3">
          <button
            onClick={onConfirm}
            disabled={loading}
            data-testid="cron-confirm-submit-btn"
            className="rounded-full bg-cron-action px-10 py-1.5 text-sm font-bold text-cron-action-foreground hover:bg-cron-action-hover disabled:cursor-not-allowed disabled:opacity-60"
          >
            {t('cron.actions.confirm')}
          </button>
          <button
            onClick={onCancel}
            data-testid="cron-confirm-cancel-btn"
            className="rounded-full border border-border bg-card px-10 py-1.5 text-sm font-bold text-text hover:bg-bg-hover"
          >
            {t('common.cancel')}
          </button>
        </div>
      </div>
    </div>
  );
}
