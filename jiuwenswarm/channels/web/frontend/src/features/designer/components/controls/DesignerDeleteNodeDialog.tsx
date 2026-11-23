import { X } from 'lucide-react';
import { useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import {
  collectSceneDeletionImpacts,
  type DesignerSceneBindingNode,
} from '../../designerSceneBinding';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';

type DesignerDeleteNodeDialogProps = {
  onCancel: () => void;
  onConfirm: () => void;
};

export function DesignerDeleteNodeDialog({ onCancel, onConfirm }: DesignerDeleteNodeDialogProps) {
  const { t } = useTranslation();
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const pendingDeleteNodeIds = useDesignerUiStore((state) => state.pendingDeleteNodeIds);
  const sceneImpacts = useMemo(
    () =>
      collectSceneDeletionImpacts(
        (domainGraph?.nodes ?? []) as unknown as DesignerSceneBindingNode[],
        pendingDeleteNodeIds,
      ),
    [domainGraph, pendingDeleteNodeIds],
  );

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
        {sceneImpacts.length > 0 ? (
          <div
            className="designer-delete-node__scene-warning"
            data-testid="designer-delete-node-scene-warning"
          >
            <p>{t('designer.nodeActions.deleteSceneWarning', { count: sceneImpacts.length })}</p>
            <ul>
              {sceneImpacts.map((impact) => (
                <li
                  key={impact.clipId}
                  data-testid="designer-delete-node-scene-warning-item"
                  data-variant={impact.clipId}
                >
                  {t('designer.nodeActions.deleteSceneMissing', { clip: impact.clipLabel })}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
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
