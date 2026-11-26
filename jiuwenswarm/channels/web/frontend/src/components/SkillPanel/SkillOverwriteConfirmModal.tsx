import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, X } from 'lucide-react';

interface SkillOverwriteConfirmModalProps {
  message: string;
  onCancel: () => void;
  onConfirm: () => void;
}

/** 页面内同名技能覆盖确认，替代浏览器原生 window.confirm。 */
export function SkillOverwriteConfirmModal({
  message,
  onCancel,
  onConfirm,
}: SkillOverwriteConfirmModalProps) {
  const { t } = useTranslation();

  return createPortal(
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center bg-black/40"
      data-testid="skill-panel-overwrite-confirm-modal"
    >
      <div
        className="relative w-[420px] rounded-2xl border border-border bg-card p-5 shadow-lg"
        data-testid="skill-panel-overwrite-confirm-modal-card"
      >
        <div className="mb-3 flex items-center justify-between">
          <div
            className="flex h-7 w-7 items-center justify-center rounded-full bg-warn-subtle text-warn"
            data-testid="skill-panel-overwrite-confirm-modal-icon"
          >
            <AlertTriangle size={15} strokeWidth={2} />
          </div>
          <button
            type="button"
            onClick={onCancel}
            aria-label={t('common.cancel')}
            className="rounded-md p-1 text-text-muted hover:bg-secondary hover:text-text"
            data-testid="skill-panel-overwrite-confirm-modal-close-button"
          >
            <X size={16} strokeWidth={2} />
          </button>
        </div>
        <p
          className="mb-4 text-[13px] leading-6 text-text"
          data-testid="skill-panel-overwrite-confirm-modal-message"
        >
          {message}
        </p>
        <div className="flex justify-end gap-2" data-testid="skill-panel-overwrite-confirm-modal-actions">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-border px-4 py-1.5 text-[13px] text-text-muted hover:bg-secondary"
            data-testid="skill-panel-overwrite-confirm-modal-cancel-button"
          >
            {t('common.cancel')}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="rounded-lg bg-control-emphasis px-4 py-1.5 text-[13px] text-text-inverse hover:opacity-85"
            data-testid="skill-panel-overwrite-confirm-modal-confirm-button"
          >
            {t('common.confirm')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
