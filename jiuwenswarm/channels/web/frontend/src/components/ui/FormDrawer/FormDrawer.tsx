import type { ReactNode, Ref } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { CloseButton } from '../CloseButton/CloseButton';
import './FormDrawer.css';

export interface FormDrawerProps {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  testId?: string;
  width?: number | string;
  notice?: ReactNode;
  footer?: ReactNode;
  onConfirm?: () => void;
  confirmLabel?: string;
  confirmDisabled?: boolean;
  confirmLoading?: boolean;
  panelRef?: Ref<HTMLElement>;
  className?: string;
  closeTestId?: string;
}

export function FormDrawer({
  title,
  onClose,
  children,
  testId = 'form-drawer',
  width,
  notice,
  footer,
  onConfirm,
  confirmLabel,
  confirmDisabled,
  confirmLoading,
  panelRef,
  className,
  closeTestId,
}: FormDrawerProps) {
  const { t } = useTranslation();
  const drawerStyle = width != null ? { width: typeof width === 'number' ? `${width}px` : width } : undefined;

  return createPortal(
    <div
      className="form-drawer-backdrop"
      data-testid={`${testId}-backdrop`}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        className={`form-drawer${className ? ` ${className}` : ''}`}
        data-testid={testId}
        style={drawerStyle}
      >
        <header className="form-drawer__header">
          <h2 data-testid={`${testId}-title`}>{title}</h2>
          <CloseButton onClick={onClose} testId={closeTestId} />
        </header>

        {notice && <div data-testid={`${testId}-notice`}>{notice}</div>}

        <div className="form-drawer__body" data-testid={`${testId}-body`}>
          {children}
        </div>

        <footer
          className={`form-drawer__footer${footer ? ' form-drawer__footer--slot' : ''}`}
          data-testid={`${testId}-footer`}
        >
          {footer ?? (
            <>
              <button type="button" onClick={onClose} className="form-drawer__btn" data-testid={`${testId}-cancel`}>
                {t('connectorMarket.common.cancel')}
              </button>
              <button
                type="button"
                onClick={onConfirm}
                disabled={confirmDisabled || confirmLoading}
                className="form-drawer__btn form-drawer__btn--primary"
                data-testid={`${testId}-confirm`}
              >
                {confirmLoading && <Loader2 size={14} className="animate-spin" />}
                {confirmLabel ?? t('connectorMarket.common.confirm')}
              </button>
            </>
          )}
        </footer>
      </aside>
    </div>,
    document.body,
  );
}
