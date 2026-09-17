import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { AlertCircle, Download, Info, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useChatStore } from '../../stores';
import { executeDesktopSave, type DesktopSaveApiResult } from '../../utils/desktopSave';
import { FileIcon } from '../FileIcon';
import { FilePreview } from './FilePreview';
import { buildArtifacts, type ArtifactItem } from './artifactCollection';
import { artifactDownloadUrl } from './filePreviewModel';

type ListResizeDrag = { pointerId: number; startX: number; startWidth: number };

const MIN_LIST_WIDTH = 240;
const MAX_LIST_WIDTH = 520;

export { fileArtifactId } from './artifactCollection';

type DownloadCapableWindow = Window & {
  pywebview?: {
    api?: {
      download_file?: (url: string, filename: string) => DesktopSaveApiResult;
    };
  };
};

export function useSessionArtifacts(): ArtifactItem[] {
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const messages = useChatStore(s => s.runtimes[activeSessionId ?? '']?.messages ?? []);

  return useMemo(() => buildArtifacts(messages), [messages]);
}

export function useSessionArtifactsCount(): number {
  return useSessionArtifacts().length;
}

export function ArtifactsPanel({
  className,
  selectedArtifactId,
  onSelectArtifact,
}: {
  className?: string;
  selectedArtifactId?: string;
  onSelectArtifact?: (artifactId: string) => void;
}) {
  const { t } = useTranslation();
  const artifacts = useSessionArtifacts();
  const [selectedId, setSelectedId] = useState<string>('');
  const [isListOpen, setIsListOpen] = useState(false);
  const [listWidth, setListWidth] = useState(MIN_LIST_WIDTH);
  const [invalidPresentationIds, setInvalidPresentationIds] = useState<Set<string>>(() => new Set());
  const listResizeDragRef = useRef<ListResizeDrag | null>(null);
  const selectedArtifact = artifacts.find(artifact => artifact.id === selectedId) || artifacts[0] || null;
  const selectedPresentationIsInvalid = selectedArtifact ? invalidPresentationIds.has(selectedArtifact.id) : false;

  useEffect(() => {
    if (selectedArtifactId && artifacts.some(artifact => artifact.id === selectedArtifactId) && selectedArtifactId !== selectedId) {
      setSelectedId(selectedArtifactId);
      return;
    }
    if (!selectedArtifact) {
      setSelectedId('');
      return;
    }
    if (selectedArtifact.id !== selectedId) {
      setSelectedId(selectedArtifact.id);
    }
  }, [artifacts, selectedArtifact, selectedArtifactId, selectedId]);

  const handlePresentationStructureInvalidChange = useCallback((artifactId: string, invalid: boolean) => {
    setInvalidPresentationIds(current => {
      if (current.has(artifactId) === invalid) return current;
      const next = new Set(current);
      if (invalid) next.add(artifactId);
      else next.delete(artifactId);
      return next;
    });
  }, []);

  const handleDownload = async (artifact: ArtifactItem) => {
    const downloadUrl = artifactDownloadUrl(artifact);
    if (!downloadUrl) return;

    const pywebviewApi = (window as DownloadCapableWindow).pywebview?.api;
    if (pywebviewApi?.download_file) {
      const outcome = await executeDesktopSave(() =>
        pywebviewApi.download_file!(downloadUrl, artifact.name || 'download')
      );
      if (outcome === 'failed') {
        window.alert(t('artifacts.downloadFailed', { name: artifact.name }));
      }
      return;
    }

    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = artifact.name || '';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleListResizePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    listResizeDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: listWidth,
    };
  };

  const handleListResizePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = listResizeDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const nextWidth = drag.startWidth + event.clientX - drag.startX;
    setListWidth(Math.min(MAX_LIST_WIDTH, Math.max(MIN_LIST_WIDTH, nextWidth)));
  };

  const finishListResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (listResizeDragRef.current?.pointerId !== event.pointerId) return;
    listResizeDragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  return (
    <section className={clsx('flex min-h-0 min-w-0 flex-1 overflow-hidden bg-transparent', className)} data-testid="artifact-panel">
      {isListOpen && (
        <>
          <aside className="flex shrink-0 flex-col overflow-hidden bg-card" style={{ width: listWidth }} data-testid="artifact-list">
            <div className="flex h-12 shrink-0 items-center px-6">
              <div className="text-sm font-semibold text-text" data-testid="artifact-list-title">{t('artifacts.title')}</div>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-4" data-testid="artifact-list-scroll">
              {artifacts.length === 0 ? (
                <div className="flex h-full items-center justify-center px-5 text-center text-sm text-text-muted" data-testid="artifact-list-empty">{t('artifacts.empty')}</div>
              ) : (
                <div className="space-y-2" data-testid="artifact-list-items">
                  {artifacts.map(artifact => {
                    const selected = selectedArtifact?.id === artifact.id;
                    return (
                      <button
                        key={artifact.id}
                        type="button"
                        className={clsx(
                          'flex h-9 w-full min-w-0 items-center gap-2 rounded-lg px-3 text-left text-sm',
                          selected ? 'bg-secondary text-text' : 'text-text hover:bg-secondary',
                        )}
                        data-testid="artifact-list-item"
                        data-variant={artifact.id}
                        onClick={() => {
                          setSelectedId(artifact.id);
                          onSelectArtifact?.(artifact.id);
                        }}
                      >
                        <FileIcon fileName={artifact.name} size={16} className="shrink-0" />
                        <span className="truncate" data-testid="artifact-list-item-name">{artifact.name}</span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </aside>

          <div
            className="resize-divider touch-none select-none"
            data-testid="artifact-list-resize-divider"
            onPointerDown={handleListResizePointerDown}
            onPointerMove={handleListResizePointerMove}
            onPointerUp={finishListResize}
            onPointerCancel={finishListResize}
            onLostPointerCapture={() => {
              listResizeDragRef.current = null;
            }}
          />
        </>
      )}

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-transparent">
        <div className="flex h-12 shrink-0 items-center gap-3 border-b border-border px-3" data-testid="artifact-preview-toolbar">
          <button
            type="button"
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-text-muted hover:bg-secondary hover:text-text"
            title={t(isListOpen ? 'artifacts.collapseList' : 'artifacts.expandList')}
            aria-label={t(isListOpen ? 'artifacts.collapseList' : 'artifacts.expandList')}
            aria-expanded={isListOpen}
            data-testid="artifact-list-toggle"
            onClick={() => setIsListOpen(open => !open)}
          >
            {isListOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
          </button>
          <div className="h-5 w-px bg-border" />
          {selectedArtifact && (
            <div className="flex min-w-0 flex-1 items-center gap-2" data-testid="artifact-preview-meta">
              <div className="min-w-0 truncate text-sm font-medium text-text" data-testid="artifact-preview-name">{selectedArtifact.name}</div>
              {selectedPresentationIsInvalid && (
                <div className="flex min-w-0 items-center gap-1 text-xs text-danger" role="status" title={t('artifacts.presentationStructureInvalid')} data-testid="artifact-presentation-invalid-notice">
                  <AlertCircle size={14} className="shrink-0" />
                  <span className="truncate">{t('artifacts.presentationStructureInvalid')}</span>
                </div>
              )}
            </div>
          )}
          {selectedArtifact && (
            <button
              type="button"
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-text-muted hover:bg-secondary hover:text-text disabled:cursor-not-allowed disabled:opacity-40"
              title={t('artifacts.download')}
              aria-label={t('artifacts.download')}
              data-testid="artifact-download"
              disabled={!selectedArtifact.downloadUrl && !selectedArtifact.path}
              onClick={() => {
                void handleDownload(selectedArtifact);
              }}
            >
              <Download size={16} />
            </button>
          )}
        </div>

        {selectedArtifact ? (
          <div className="min-h-0 flex-1 overflow-hidden bg-transparent p-3" data-testid="artifact-preview-surface">
            <FilePreview artifact={selectedArtifact} onPresentationStructureInvalidChange={handlePresentationStructureInvalidChange} />
          </div>
        ) : (
          <PreviewNotice title={t('artifacts.selectArtifact')} fill />
        )}
      </div>
    </section>
  );
}

function PreviewNotice({ title, fill = false }: { title: string; fill?: boolean }) {
  return (
    <div className={clsx('flex items-center justify-center text-sm text-text-muted', fill ? 'h-full' : 'min-h-[240px]')} data-testid="artifact-preview-empty">
      <div className="flex items-center gap-2 rounded-md border border-border bg-secondary px-3 py-2">
        <Info size={15} />
        <span>{title}</span>
      </div>
    </div>
  );
}
