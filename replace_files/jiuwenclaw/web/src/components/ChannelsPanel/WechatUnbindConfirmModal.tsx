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
    >
      <div
        className="channels-panel__modal rounded-xl border border-border bg-card shadow-xl max-w-md w-full max-h-[90vh] overflow-auto flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3">
          <h3 id="wechat-unbind-confirm-title" className="text-sm font-semibold text-text">
            {t('channels.wechatUnbind.confirmTitle')}
          </h3>
          <button
            type="button"
            className="btn !px-2.5 !py-1 text-xs"
            onClick={onClose}
            disabled={confirming}
          >
            {t('channels.wechatLogin.close')}
          </button>
        </div>
        <div className="p-4 text-sm text-text space-y-4">
          <p className="text-text leading-relaxed">{t('channels.wechatUnbind.confirm')}</p>
          <div className="flex flex-wrap items-center justify-end gap-2 pt-1">
            <button
              type="button"
              className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
              onClick={onClose}
              disabled={confirming}
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="btn !px-3 !py-1.5 border border-[var(--destructive)] text-[var(--destructive)] hover:bg-[var(--destructive)]/10 disabled:opacity-50 disabled:cursor-not-allowed"
              onClick={onConfirm}
              disabled={confirming}
            >
              {confirming ? t('channels.wechatUnbind.unbinding') : t('channels.wechatUnbind.confirmAction')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
