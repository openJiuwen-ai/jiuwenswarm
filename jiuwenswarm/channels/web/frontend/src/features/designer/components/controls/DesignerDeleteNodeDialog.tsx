import { X } from 'lucide-react';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';

type DesignerDeleteNodeDialogProps = {
  onCancel: () => void;
  onConfirm: () => void;
};

export function DesignerDeleteNodeDialog({ onCancel, onConfirm }: DesignerDeleteNodeDialogProps) {
  const { t } = useTranslation();

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      onCancel();
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [onCancel]);

  return (
    <div
      className="designer-delete-node"
      role="dialog"
      aria-modal="true"
      aria-labelledby="designer-delete-node-title"
      data-testid="designer-delete-node-dialog"
    >
      <button
        type="button"
        className="designer-delete-node__backdrop"
        aria-label={t('designer.nodeActions.deleteConfirmCancel')}
        onClick={onCancel}
      />
      <div className="designer-delete-node__panel">
        <button
          type="button"
          className="designer-delete-node__close"
          data-testid="designer-delete-node-close"
          aria-label={t('designer.materials.closeViewer')}
          onClick={onCancel}
        >
          <X size={16} strokeWidth={2} aria-hidden />
        </button>
        <h2 id="designer-delete-node-title">{t('designer.nodeActions.deleteConfirmTitle')}</h2>
        <p>{t('designer.nodeActions.deleteConfirmMessage')}</p>
        <div className="designer-delete-node__actions">
          <button
            type="button"
            className="designer-delete-node__cancel"
            data-testid="designer-delete-node-cancel"
            onClick={onCancel}
          >
            {t('designer.nodeActions.deleteConfirmCancel')}
          </button>
          <button
            type="button"
            className="designer-delete-node__confirm"
            data-testid="designer-delete-node-confirm"
            onClick={onConfirm}
          >
            {t('designer.nodeActions.deleteConfirmOk')}
          </button>
        </div>
      </div>
    </div>
  );
}
