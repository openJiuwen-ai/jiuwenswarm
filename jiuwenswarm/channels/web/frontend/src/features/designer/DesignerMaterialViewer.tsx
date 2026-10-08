import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { DesignerTextEditor } from './DesignerTextEditor';
import {
  classifyDesignerOutput,
  isEditableDesignerMaterial,
  type DesignerMaterial,
} from './designerMaterials';

type DesignerMaterialViewerProps = {
  materials: DesignerMaterial[];
  selectedId: string;
  startEditKey?: number;
  onSelect: (id: string) => void;
  onClose: () => void;
};

export function viewableDesignerMaterials(materials: DesignerMaterial[]): DesignerMaterial[] {
  return materials.filter((item) => {
    const kind = classifyDesignerOutput(item);
    return kind !== 'placeholder' && Boolean(item.previewUrl || item.textUrl);
  });
}

export function DesignerMaterialViewer({
  materials,
  selectedId,
  startEditKey = 0,
  onSelect,
  onClose,
}: DesignerMaterialViewerProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [editKey, setEditKey] = useState(0);
  const viewable = useMemo(() => viewableDesignerMaterials(materials), [materials]);
  const index = Math.max(
    0,
    viewable.findIndex((item) => item.id === selectedId || item.nodeId === selectedId),
  );
  const current = viewable[index] ?? viewable[0];
  const kind = current ? classifyDesignerOutput(current) : 'placeholder';
  const canEdit = Boolean(current && isEditableDesignerMaterial(current));

  useEffect(() => {
    setEditKey(0);
  }, [current?.id]);

  useEffect(() => {
    if (!startEditKey) return;
    setEditKey((value) => value + 1);
  }, [startEditKey]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing =
        event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLInputElement;
      if (event.key === 'Escape') {
        if (editing || typing) return;
        onClose();
        return;
      }
      if (editing || typing || viewable.length < 2) return;
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        const next = viewable[(index - 1 + viewable.length) % viewable.length];
        if (next) onSelect(next.id);
      }
      if (event.key === 'ArrowRight') {
        event.preventDefault();
        const next = viewable[(index + 1) % viewable.length];
        if (next) onSelect(next.id);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [editing, index, onClose, onSelect, viewable]);

  if (!current) return null;

  const go = (offset: number) => {
    const next = viewable[(index + offset + viewable.length) % viewable.length];
    if (next) onSelect(next.id);
  };

  return (
    <div className="designer-material-viewer" role="dialog" aria-modal="true" data-testid="designer-material-viewer">
      <button
        type="button"
        className="designer-material-viewer__backdrop"
        aria-label={t('designer.materials.closeViewer')}
        onClick={onClose}
      />
      <div className="designer-material-viewer__panel">
        <header className="designer-material-viewer__header">
          <div>
            <p className="designer-material-viewer__kind">{current.kind}</p>
            <h2>{current.label}</h2>
            {viewable.length > 1 ? (
              <p className="designer-material-viewer__index">
                {t('designer.materials.viewerIndex', {
                  current: index + 1,
                  total: viewable.length,
                })}
              </p>
            ) : null}
          </div>
          <div className="designer-material-viewer__actions">
            {canEdit && !editing ? (
              <button
                type="button"
                className="btn"
                onClick={() => setEditKey((value) => value + 1)}
                data-testid="designer-material-viewer-edit"
              >
                {t('designer.materials.edit')}
              </button>
            ) : null}
            <button
              type="button"
              className="btn"
              onClick={onClose}
              data-testid="designer-material-viewer-close"
            >
              {t('designer.materials.closeViewer')}
            </button>
          </div>
        </header>
        <div className="designer-material-viewer__stage" data-testid="designer-material-viewer-stage">
          {viewable.length > 1 ? (
            <button
              type="button"
              className="designer-material-viewer__nav"
              onClick={() => go(-1)}
              aria-label={t('designer.materials.previous')}
            >
              ‹
            </button>
          ) : null}
          <div
            className={
              kind === 'text' || current.kind === 'table'
                ? 'designer-material-viewer__body designer-material-viewer__body--text'
                : 'designer-material-viewer__body'
            }
          >
            {kind === 'image' && current.previewUrl ? (
              <img src={current.previewUrl} alt={current.label} />
            ) : kind === 'video' && current.previewUrl ? (
              <video key={current.uri} src={current.previewUrl} controls playsInline autoPlay />
            ) : kind === 'audio' && current.previewUrl ? (
              <audio key={current.uri} src={current.previewUrl} controls />
            ) : (kind === 'text' || current.kind === 'table') && current.textUrl ? (
              <DesignerTextEditor
                material={current}
                showStartButton={false}
                startEditKey={editKey}
                onEditingChange={setEditing}
              />
            ) : kind === 'file' && current.previewUrl ? (
              <a href={current.previewUrl} target="_blank" rel="noreferrer">
                {t('designer.materials.openFile')}
              </a>
            ) : (
              <p>{t('designer.materials.placeholderHint')}</p>
            )}
          </div>
          {viewable.length > 1 ? (
            <button
              type="button"
              className="designer-material-viewer__nav"
              onClick={() => go(1)}
              aria-label={t('designer.materials.next')}
            >
              ›
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
