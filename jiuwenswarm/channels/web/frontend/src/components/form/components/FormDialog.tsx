import { type ReactNode, useId } from 'react';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button, Dialog, Loading } from '../../ui';
import './FormDialog.css';

export function FormDialog({
  open,
  title,
  description,
  icon,
  loading = false,
  submitting = false,
  confirmLoading = false,
  confirmDisabled = false,
  confirmLabel,
  cancelLabel,
  secondaryAction,
  status,
  className,
  dialogClassName,
  testIdPrefix,
  testVariant,
  onConfirm,
  onCancel,
  children,
}: {
  open: boolean;
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  loading?: boolean;
  submitting?: boolean;
  confirmLoading?: boolean;
  confirmDisabled?: boolean;
  confirmLabel: string;
  cancelLabel: string;
  secondaryAction?: ReactNode;
  status?: ReactNode;
  className?: string;
  dialogClassName?: string;
  testIdPrefix?: string;
  testVariant?: string;
  onConfirm: () => void;
  onCancel: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const titleId = useId();
  return (
    <Dialog
      open={open}
      titleId={titleId}
      className={`form-dialog-surface${dialogClassName ? ` ${dialogClassName}` : ''}`}
      closeDisabled={submitting}
      onCancel={onCancel}
    >
      <div
        className={`form-dialog${className ? ` ${className}` : ''}`}
        data-testid={testIdPrefix}
        data-variant={testVariant}
      >
        <header
          className="form-dialog__header"
          data-testid={testIdPrefix ? `${testIdPrefix}-header` : undefined}
          data-variant={testVariant}
        >
          <div className="form-dialog__heading">
            {icon ? <div className="form-dialog__icon">{icon}</div> : null}
            <div className="form-dialog__heading-copy">
              <h2 id={titleId} data-testid={testIdPrefix ? `${testIdPrefix}-title` : undefined}>
                {title}
              </h2>
              {description ? (
                <p data-testid={testIdPrefix ? `${testIdPrefix}-subtitle` : undefined}>{description}</p>
              ) : null}
            </div>
          </div>
          <button
            type="button"
            className="form-dialog__close"
            aria-label={t('common.close')}
            disabled={submitting}
            onClick={onCancel}
            data-testid={testIdPrefix ? `${testIdPrefix}-close-btn` : undefined}
          >
            <X size={16} aria-hidden />
          </button>
        </header>
        <div className="form-dialog__body">
          {status}
          {loading ? <Loading aria-label={typeof title === 'string' ? title : ''} /> : children}
        </div>
        <footer className="form-dialog__footer">
          <div>{secondaryAction}</div>
          <div className="form-dialog__actions">
            <Button
              size="sm"
              disabled={submitting}
              onClick={onCancel}
              data-testid={testIdPrefix ? `${testIdPrefix}-cancel-btn` : undefined}
              data-variant={testVariant}
            >
              {cancelLabel}
            </Button>
            <Button
              variant="primary"
              size="sm"
              loading={submitting || confirmLoading}
              disabled={confirmDisabled}
              onClick={onConfirm}
              data-testid={testIdPrefix ? `${testIdPrefix}-save-btn` : undefined}
              data-variant={testVariant}
            >
              {confirmLabel}
            </Button>
          </div>
        </footer>
      </div>
    </Dialog>
  );
}
