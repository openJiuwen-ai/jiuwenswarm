import { useId, type ReactNode } from 'react';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button, Dialog, type ButtonProps } from '../../../components/ui';
import './SettingsConfirmDialog.css';

type SettingsConfirmDialogProps = {
  open: boolean;
  title: string;
  message: ReactNode;
  confirming?: boolean;
  error?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  confirmVariant?: ButtonProps['variant'];
  onConfirm: () => void;
  onCancel: () => void;
  onDismiss?: () => void;
};

export function SettingsConfirmDialog({
  open,
  title,
  message,
  confirming = false,
  error,
  confirmLabel,
  cancelLabel,
  confirmVariant = 'primary',
  onConfirm,
  onCancel,
  onDismiss = onCancel,
}: SettingsConfirmDialogProps) {
  const { t } = useTranslation();
  const titleId = useId();

  return (
    <Dialog open={open} titleId={titleId} closeDisabled={confirming} onCancel={onDismiss}>
      <div className="settings-confirm-dialog">
        <header className="settings-confirm-dialog__header">
          <h2 id={titleId}>{title}</h2>
          <button
            type="button"
            className="settings-confirm-dialog__close"
            aria-label={t('common.close')}
            title={t('common.close')}
            disabled={confirming}
            onClick={onDismiss}
          >
            <X aria-hidden />
          </button>
        </header>
        <div className="settings-confirm-dialog__body">
          <div className="settings-confirm-dialog__message">{message}</div>
          {error ? (
            <div className="settings-confirm-dialog__error" role="alert">
              {error}
            </div>
          ) : null}
        </div>
        <footer className="settings-confirm-dialog__footer">
          <Button size="sm" disabled={confirming} onClick={onCancel}>
            {cancelLabel ?? t('common.cancel')}
          </Button>
          <Button variant={confirmVariant} size="sm" loading={confirming} onClick={onConfirm}>
            {confirmLabel ?? t('common.confirm')}
          </Button>
        </footer>
      </div>
    </Dialog>
  );
}
