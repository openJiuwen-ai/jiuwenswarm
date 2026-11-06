import { TriangleAlert, X } from 'lucide-react';
import { useEffect, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

interface JiuwenMateDialogProps {
  title?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
  onClose: () => void;
  testId?: string;
}

export function JiuwenMateDialog({ title, children, footer, width = 480, onClose, testId }: JiuwenMateDialogProps) {
  const { t } = useTranslation();

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, [onClose]);

  return (
    <div className="jiuwenmate-dialog-backdrop" onMouseDown={onClose}>
      <section
        className="jiuwenmate-dialog"
        role="dialog"
        aria-modal="true"
        style={{ width }}
        onMouseDown={(event) => event.stopPropagation()}
        data-testid={testId}
      >
        <button type="button" className="jiuwenmate-dialog__close" aria-label={t('common.close')} onClick={onClose}>
          <X aria-hidden />
        </button>
        {title ? <h2 className="jiuwenmate-dialog__title">{title}</h2> : null}
        <div className="jiuwenmate-dialog__body">{children}</div>
        {footer ? <footer className="jiuwenmate-dialog__footer">{footer}</footer> : null}
      </section>
    </div>
  );
}

interface JiuwenMateConfirmDialogProps {
  title: string;
  warning: string;
  message: ReactNode;
  onCancel: () => void;
  onConfirm: () => void;
}

export function JiuwenMateConfirmDialog({ title, warning, message, onCancel, onConfirm }: JiuwenMateConfirmDialogProps) {
  const { t } = useTranslation();
  return (
    <JiuwenMateDialog
      title={title}
      width={480}
      onClose={onCancel}
      testId="jiuwenmate-confirm-dialog"
      footer={
        <>
          <button type="button" className="jiuwenmate-outline-button" onClick={onCancel}>
            {t('jiuwenmate.common.cancel')}
          </button>
          <button type="button" className="jiuwenmate-dark-button" onClick={onConfirm} data-testid="jiuwenmate-confirm-button">
            {t('jiuwenmate.common.confirm')}
          </button>
        </>
      }
    >
      <p className="jiuwenmate-dialog__warning">
        <TriangleAlert aria-hidden />
        <span>{warning}</span>
      </p>
      <p className="jiuwenmate-dialog__message">{message}</p>
    </JiuwenMateDialog>
  );
}
