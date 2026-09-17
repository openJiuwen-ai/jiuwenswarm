import { useTranslation } from 'react-i18next';

type WechatUnbindConfirmModalProps = {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  confirming: boolean;
};

export function WechatUnbindConfirmModal({
  open,
  onClose,
  onConfirm,
  confirming,
}: WechatUnbindConfirmModalProps) {
  const { t } = useTranslation();

  if (!open) {
    return null;
  }

  return (
    <div
      className="channels-panel__modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="wechat-unbind-confirm-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !confirming) {
          onClose();
        }
      }}
      data-testid="channels-panel-wechat-unbind-modal"
    >
      <div
        className="channels-panel__modal rounded-xl border border-border bg-card shadow-xl max-w-md w-full max-h-[90vh] overflow-auto flex flex-col"
        onClick={(e) => e.stopPropagation()}
        data-testid="channels-panel-wechat-unbind-modal-panel"
      >
        <div
          className="px-4 py-3 border-b border-border flex items-center justify-between gap-3"
          data-testid="channels-panel-wechat-unbind-modal-header"
        >
          <h3
            id="wechat-unbind-confirm-title"
            className="text-sm font-semibold text-text"
            data-testid="channels-panel-wechat-unbind-modal-title"
          >
            {t('channels.wechatUnbind.confirmTitle')}
          </h3>
          <button
            type="button"
            className="btn !px-2.5 !py-1 text-xs"
            onClick={onClose}
            disabled={confirming}
            data-testid="channels-panel-wechat-unbind-modal-close-btn"
          >
            {t('channels.wechatLogin.close')}
          </button>
        </div>
        <div
          className="p-4 text-sm text-text space-y-4"
          data-testid="channels-panel-wechat-unbind-modal-body"
        >
          <p
            className="text-text leading-relaxed"
            data-testid="channels-panel-wechat-unbind-modal-message"
          >
            {t('channels.wechatUnbind.confirm')}
          </p>
          <div className="flex flex-wrap items-center justify-end gap-2 pt-1">
            <button
              type="button"
              className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
              onClick={onClose}
              disabled={confirming}
              data-testid="channels-panel-wechat-unbind-modal-cancel-btn"
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="btn !px-3 !py-1.5 border border-[var(--color-feedback-danger)] text-[var(--color-feedback-danger)] hover:bg-[var(--color-feedback-danger)]/10 disabled:opacity-50 disabled:cursor-not-allowed"
              onClick={onConfirm}
              disabled={confirming}
              data-testid="channels-panel-wechat-unbind-modal-confirm-btn"
            >
              {confirming ? t('channels.wechatUnbind.unbinding') : t('channels.wechatUnbind.confirmAction')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
