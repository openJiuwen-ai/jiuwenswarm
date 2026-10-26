import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';

interface PermissionWarningDialogProps {
  onConfirm: () => void;
  onCancel: () => void;
}

export function PermissionWarningDialog({ onConfirm, onCancel }: PermissionWarningDialogProps) {
  const { t } = useTranslation();

  return createPortal(
    <div className="perm-warning-backdrop" onClick={onCancel}>
      <div className="perm-warning-modal" onClick={(e) => e.stopPropagation()}>
        <div className="perm-warning-header">
          <div className="perm-warning-icon">
            <svg viewBox="0 0 24 24" fill="currentColor" width="22" height="22" aria-hidden="true">
              <path fillRule="evenodd" d="M9.401 3.003c1.155-2 4.043-2 5.197 0l7.355 12.748c1.154 2-.29 4.5-2.599 4.5H4.645c-2.309 0-3.752-2.5-2.598-4.5L9.4 3.003zM12 8.25a.75.75 0 01.75.75v3.75a.75.75 0 01-1.5 0V9a.75.75 0 01.75-.75zm0 8.25a1 1 0 100-2 1 1 0 000 2z" clipRule="evenodd" />
            </svg>
          </div>
          <span className="perm-warning-title">{t('chat.config.permission.fullAccessWarning.title')}</span>
          <button type="button" className="perm-warning-close" onClick={onCancel} aria-label="close">
            <svg viewBox="0 0 20 20" fill="currentColor" width="14" height="14" aria-hidden="true">
              <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
            </svg>
          </button>
        </div>
        <div className="perm-warning-body">
          <p>{t('chat.config.permission.fullAccessWarning.body1')}</p>
          <p>{t('chat.config.permission.fullAccessWarning.body2')}</p>
        </div>
        <div className="perm-warning-footer">
          <button type="button" className="perm-warning-btn perm-warning-btn--cancel" onClick={onCancel}>
            {t('chat.config.permission.fullAccessWarning.cancel')}
          </button>
          <button type="button" className="perm-warning-btn perm-warning-btn--confirm" onClick={onConfirm}>
            {t('chat.config.permission.fullAccessWarning.confirm')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
