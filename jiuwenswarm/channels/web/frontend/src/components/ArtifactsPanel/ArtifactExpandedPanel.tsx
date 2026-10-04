import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSessionArtifacts } from '.';
import { ArtifactList } from '.';
import { FilePreview } from './FilePreview';
import { previewKind } from './filePreviewModel';
import { isPreviewLocallyEditable } from './previewSelection';
import BackIcon from '../../assets/work-mode/back.svg?react';
import ArrowLeftIcon from '../../assets/work-mode/arrow-left.svg?react';
import ArrowRightIcon from '../../assets/work-mode/arrow-right.svg?react';

export function ArtifactExpandedPanel({
  selectedArtifactId,
  onSelectArtifact,
}: {
  selectedArtifactId?: string;
  onSelectArtifact: (artifactId: string) => void;
}) {
  const { t } = useTranslation();
  const artifacts = useSessionArtifacts();
  const selectedArtifact = artifacts.find(a => a.id === selectedArtifactId) ?? null;
  const selectedIndex = selectedArtifact ? artifacts.findIndex(a => a.id === selectedArtifact.id) : -1;
  const hasPrev = selectedIndex > 0;
  const hasNext = selectedArtifact && selectedIndex < artifacts.length - 1;
  const [, setInvalidPresentationIds] = useState<Set<string>>(() => new Set());
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saveRequestId, setSaveRequestId] = useState(0);

  const handlePresentationStructureInvalidChange = useCallback((artifactId: string, invalid: boolean) => {
    setInvalidPresentationIds(current => {
      if (current.has(artifactId) === invalid) return current;
      const next = new Set(current);
      if (invalid) next.add(artifactId);
      else next.delete(artifactId);
      return next;
    });
  }, []);

  const confirmLeaveIfDirty = useCallback(() => {
    if (!dirty) return true;
    return window.confirm(t('artifacts.unsavedConfirm'));
  }, [dirty, t]);

  const navigateTo = useCallback(
    (artifactId: string) => {
      if (!confirmLeaveIfDirty()) return;
      setEditing(false);
      setDirty(false);
      onSelectArtifact(artifactId);
    },
    [confirmLeaveIfDirty, onSelectArtifact],
  );

  const canLocalEdit =
    Boolean(selectedArtifact?.path?.trim()) &&
    selectedArtifact != null &&
    isPreviewLocallyEditable(previewKind(selectedArtifact));

  if (selectedArtifact) {
    return (
      <div data-variant="artifacts-preview" className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div
          className="flex h-[46px] w-full shrink-0 items-center gap-4 border-b border-border pl-6 pr-3"
          data-testid="artifact-preview-toolbar"
          style={{ color: 'var(--color-artifact-toolbar-icon)' }}
        >
          <button type="button" className="shrink-0" data-testid="artifact-back" onClick={() => navigateTo('')} style={{ display: 'flex' }}>
            <BackIcon width={16} height={16} />
          </button>
          <div className="w-px h-4 shrink-0" style={{ backgroundColor: 'var(--color-artifact-toolbar-divider)' }} />
          <div className="flex min-w-0 flex-1 items-center gap-2" data-testid="artifact-preview-name">
            <span className="min-w-0 truncate text-sm font-medium text-text">{selectedArtifact.name}</span>
          </div>
          {canLocalEdit ? (
            <>
              <button
                type="button"
                className="shrink-0 rounded px-2 py-1 text-xs text-text hover:bg-secondary"
                data-testid="artifact-edit-toggle"
                aria-pressed={editing}
                onClick={() => {
                  if (editing && dirty && !window.confirm(t('artifacts.unsavedConfirm'))) return;
                  setEditing(current => !current);
                  if (editing) setDirty(false);
                }}
              >
                {editing ? t('artifacts.done') : t('artifacts.edit')}
              </button>
              <button
                type="button"
                className="shrink-0 rounded px-2 py-1 text-xs text-text hover:bg-secondary disabled:cursor-not-allowed disabled:opacity-40"
                data-testid="artifact-save"
                disabled={!dirty}
                onClick={() => setSaveRequestId(value => value + 1)}
              >
                {t('artifacts.save')}
              </button>
            </>
          ) : null}
          <button
            type="button"
            className="shrink-0"
            data-testid="artifact-prev"
            disabled={!hasPrev}
            onClick={() => hasPrev && navigateTo(artifacts[selectedIndex - 1].id)}
            style={{
              color: hasPrev ? 'var(--color-artifact-toolbar-icon)' : 'var(--color-artifact-toolbar-icon-disabled)',
              cursor: hasPrev ? 'pointer' : 'default',
              display: 'flex',
            }}
          >
            <ArrowLeftIcon width={16} height={16} />
          </button>
          <button
            type="button"
            className="shrink-0"
            data-testid="artifact-next"
            disabled={!hasNext}
            onClick={() => hasNext && navigateTo(artifacts[selectedIndex + 1].id)}
            style={{
              color: hasNext ? 'var(--color-artifact-toolbar-icon)' : 'var(--color-artifact-toolbar-icon-disabled)',
              cursor: hasNext ? 'pointer' : 'default',
              display: 'flex',
            }}
          >
            <ArrowRightIcon width={16} height={16} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden bg-transparent p-3" data-testid="artifact-preview-surface">
          <FilePreview
            artifact={selectedArtifact}
            editing={editing && canLocalEdit}
            onDirtyChange={setDirty}
            saveRequestId={saveRequestId}
            onSaveResult={(ok, error) => {
              if (!ok) window.alert(error || t('artifacts.saveFailed'));
            }}
            onPresentationStructureInvalidChange={handlePresentationStructureInvalidChange}
          />
        </div>
      </div>
    );
  }

  return (
    <div data-variant="artifacts-list" className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      <ArtifactList selectedArtifactId={selectedArtifactId} onSelectArtifact={navigateTo} className="flex-1" />
    </div>
  );
}
