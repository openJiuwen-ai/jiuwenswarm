import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSessionArtifacts } from '.';
import { ArtifactList } from '.';
import { FilePreview } from './FilePreview';
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
  const handlePresentationStructureInvalidChange = useCallback((artifactId: string, invalid: boolean) => {
    setInvalidPresentationIds(current => {
      if (current.has(artifactId) === invalid) return current;
      const next = new Set(current);
      if (invalid) next.add(artifactId);
      else next.delete(artifactId);
      return next;
    });
  }, []);

  if (selectedArtifact) {
    return (
      <div data-variant="artifacts-preview" className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div
          className="flex h-[46px] w-full shrink-0 items-center gap-4 border-b border-border pl-6 pr-3"
          data-testid="artifact-preview-toolbar"
          style={{ color: 'var(--color-artifact-toolbar-icon)' }}
        >
          <button type="button" className="shrink-0" data-testid="artifact-back" onClick={() => onSelectArtifact('')} style={{ display: 'flex' }}>
            <BackIcon width={16} height={16} />
          </button>
          <div className="w-px h-4 shrink-0" style={{ backgroundColor: 'var(--color-artifact-toolbar-divider)' }} />
          <div className="flex min-w-0 flex-1 items-center gap-2" data-testid="artifact-preview-name">
            <span className="min-w-0 truncate text-sm font-medium text-text">{selectedArtifact.name}</span>
          </div>
          <button
            type="button"
            className="shrink-0"
            data-testid="artifact-prev"
            disabled={!hasPrev}
            onClick={() => hasPrev && onSelectArtifact(artifacts[selectedIndex - 1].id)}
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
            onClick={() => hasNext && onSelectArtifact(artifacts[selectedIndex + 1].id)}
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
          <FilePreview artifact={selectedArtifact} onPresentationStructureInvalidChange={handlePresentationStructureInvalidChange} />
        </div>
      </div>
    );
  }

  return (
    <div data-variant="artifacts" className="flex min-w-0 flex-1 flex-col overflow-hidden px-6 pb-6">
      <div className="flex h-8 items-center justify-between gap-3 my-6">
        <h2 className="text-sm font-semibold leading-5 text-text-strong" data-testid="tool-panel-artifacts-title">
          {t('artifacts.title')}
        </h2>
      </div>
      <ArtifactList selectedArtifactId={selectedArtifactId} onSelectArtifact={onSelectArtifact} className="shrink-0" />
    </div>
  );
}
