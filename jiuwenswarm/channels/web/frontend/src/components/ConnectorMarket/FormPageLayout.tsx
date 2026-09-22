import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, Loader2 } from 'lucide-react';
import './FormPageLayout.css';

export interface FormPageLayoutProps {
  onBack: () => void;
  title: ReactNode;
  children: ReactNode;
  onConfirm: () => void;
  cancelLabel: string;
  confirmLabel?: string;
  confirmDisabled?: boolean;
  confirmLoading?: boolean;
  footerSlot?: ReactNode;
  testId: string;
  contentClassName?: string;
  headerClassName?: string;
  bodyClassName?: string;
  footerClassName?: string;
}

export function FormPageLayout({
  onBack,
  title,
  children,
  onConfirm,
  cancelLabel,
  confirmLabel = 'Confirm',
  confirmDisabled,
  confirmLoading,
  footerSlot,
  testId,
  contentClassName = 'page-shell',
  headerClassName,
  bodyClassName,
  footerClassName,
}: FormPageLayoutProps) {
  const { t } = useTranslation();
  return (
    <div className="form-page-layout" data-testid={testId}>
      <div className={`form-page-layout__header ${contentClassName}${headerClassName ? ` ${headerClassName}` : ''}`}>
        <button
          type="button"
          onClick={onBack}
          className="form-page-layout__back"
          data-testid={`${testId}-back`}
        >
          <ChevronLeft size={16} />
          {t('connectorMarket.common.back')}
        </button>

        <h1 className="form-page-layout__title" data-testid={`${testId}-title`}>
          {title}
        </h1>
      </div>

      <div className={`form-page-layout__body${bodyClassName ? ` ${bodyClassName}` : ''}`}>
        <div className={contentClassName}>
          {children}
        </div>
      </div>

      <div className={`form-page-layout__footer${footerClassName ? ` ${footerClassName}` : ''}`}>
        <div
          className={`form-page-layout__footer-inner ${contentClassName}${footerSlot ? ' form-page-layout__footer-inner--with-slot' : ''}`}
        >
          {footerSlot && <div className="form-page-layout__footer-slot">{footerSlot}</div>}
          <div className="form-page-layout__footer-buttons">
            <button
              type="button"
              onClick={onBack}
              className="form-page-layout__footer-btn form-page-layout__footer-btn-secondary"
            >
              {cancelLabel}
            </button>
            <button
              type="button"
              onClick={onConfirm}
              disabled={confirmDisabled || confirmLoading}
              className="form-page-layout__footer-btn form-page-layout__footer-btn-primary"
            >
              {confirmLoading && <Loader2 size={14} className="animate-spin" />}
              {confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
