import { useEffect, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { CodePreview } from './CodePreview';
import { DocxPreview } from './DocxPreview';
import { PresentationPreview } from './PresentationPreview';
import { SelectionFloat, type SelectionStyleAction } from './SelectionFloat';
import { SpreadsheetPreview } from './SpreadsheetPreview';
import { artifactBinaryPreviewUrl, artifactTextPreviewUrl, previewKind, type PreviewKind } from './filePreviewModel';
import { isPreviewLocallyEditable, supportsPreviewSelection } from './previewSelection';
import { wrapFirstOccurrence, type TextWrapStyle } from './previewTextEdit';

export type PreviewArtifact = {
  id: string;
  name: string;
  mimeType?: string;
  downloadUrl?: string;
  path?: string;
  size?: number;
};

type TextKind = Extract<PreviewKind, 'markdown' | 'text' | 'code' | 'json' | 'jsonl'>;

function Notice({ children }: { children: string }) {
  return (
    <div className="flex min-h-[240px] items-center justify-center text-sm text-text-muted" data-testid="artifact-preview-notice">
      <div className="flex items-center gap-2 rounded-md border border-border bg-secondary px-3 py-2">
        <AlertCircle size={15} />
        {children}
      </div>
    </div>
  );
}

function TextPreviewSurface({
  artifact,
  kind,
  editing,
  onDirtyChange,
  saveRequestId,
  onSaveResult,
  onRegisterStyleHandler,
}: {
  artifact: PreviewArtifact;
  kind: TextKind;
  editing: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  saveRequestId?: number;
  onSaveResult?: (ok: boolean, error?: string) => void;
  onRegisterStyleHandler: (handler: ((action: SelectionStyleAction, selectedText: string) => void) | null) => void;
}) {
  const { t } = useTranslation();
  const [baseline, setBaseline] = useState('');
  const [draft, setDraft] = useState('');
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const lastSaveRequestId = useRef(0);
  const draftRef = useRef(draft);
  draftRef.current = draft;

  useEffect(() => {
    const url = artifactTextPreviewUrl(artifact, window.location.origin);
    if (!url) {
      setBaseline('');
      setDraft('');
      setError(true);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(false);
    void fetch(url, { cache: 'no-store' })
      .then(async response => {
        const contentType = (response.headers.get('content-type') ?? '').toLowerCase();
        if (!response.ok || contentType.includes('text/html')) throw new Error('read_failed');
        return response.text();
      })
      .then(content => {
        if (cancelled) return;
        setBaseline(content);
        setDraft(content);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setBaseline('');
        setDraft('');
        setError(true);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [artifact.downloadUrl, artifact.path]);

  const dirty = draft !== baseline;
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (!editing) {
      onRegisterStyleHandler(null);
      return;
    }
    onRegisterStyleHandler((action, selectedText) => {
      const wrapped = wrapFirstOccurrence(draftRef.current, selectedText, action as TextWrapStyle);
      if (wrapped) setDraft(wrapped.value);
    });
    return () => onRegisterStyleHandler(null);
  }, [editing, onRegisterStyleHandler]);

  useEffect(() => {
    if (!saveRequestId || saveRequestId === lastSaveRequestId.current) return;
    lastSaveRequestId.current = saveRequestId;
    const path = artifact.path?.trim();
    if (!path) {
      onSaveResult?.(false, t('artifacts.previewMissingPath'));
      return;
    }
    void fetch('/file-api/file-content', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ path, content: draft }),
    })
      .then(async response => {
        if (!response.ok) {
          const errorText = await response.text();
          throw new Error(`HTTP ${response.status}: ${errorText.substring(0, 120)}`);
        }
        setBaseline(draft);
        onSaveResult?.(true);
      })
      .catch(err => {
        onSaveResult?.(false, err instanceof Error ? err.message : t('artifacts.saveFailed'));
      });
  }, [artifact.path, draft, onSaveResult, saveRequestId, t]);

  if (loading)
    return (
      <div className="flex min-h-[240px] items-center justify-center gap-2 text-sm text-text-muted" data-testid="artifact-text-preview-status" data-variant="loading">
        <LoaderCircle className="animate-spin" size={16} />
        {t('common.loading')}
      </div>
    );
  if (error) return <Notice>{t('artifacts.previewFailed')}</Notice>;

  if (editing && isPreviewLocallyEditable(kind)) {
    return (
      <CodePreview
        content={draft}
        name={artifact.name}
        mimeType={artifact.mimeType}
        editable
        onChange={setDraft}
      />
    );
  }

  if (kind === 'markdown')
    return <MarkdownRenderer content={draft} className="chat-text chat-markdown h-full max-w-none overflow-auto" testId="artifact-markdown-preview" />;
  if (kind === 'code') return <CodePreview content={draft} name={artifact.name} mimeType={artifact.mimeType} />;
  if (kind === 'json' || kind === 'jsonl') {
    try {
      const value =
        kind === 'json'
          ? JSON.parse(draft)
          : draft
              .split(/\r?\n/)
              .filter(Boolean)
              .map(line => JSON.parse(line));
      return (
        <pre className="m-0 h-full w-full max-w-full overflow-auto bg-transparent text-xs text-text" data-testid="artifact-json-preview">
          {JSON.stringify(value, null, 2)}
        </pre>
      );
    } catch {
      return <Notice>{t('artifacts.invalidJson')}</Notice>;
    }
  }
  return (
    <pre className="m-0 h-full w-full max-w-full overflow-auto bg-transparent text-xs text-text" data-testid="artifact-text-preview">
      {draft}
    </pre>
  );
}

export function FilePreview({
  artifact,
  onPresentationStructureInvalidChange,
  editing = false,
  onDirtyChange,
  saveRequestId,
  onSaveResult,
}: {
  artifact: PreviewArtifact;
  onPresentationStructureInvalidChange?: (artifactId: string, invalid: boolean) => void;
  editing?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
  saveRequestId?: number;
  onSaveResult?: (ok: boolean, error?: string) => void;
}) {
  const { t } = useTranslation();
  const kind = previewKind(artifact);
  const url = artifactBinaryPreviewUrl(artifact, window.location.origin);
  const panelRef = useRef<HTMLDivElement>(null);
  const [textStyleHandler, setTextStyleHandler] = useState<((action: SelectionStyleAction, selectedText: string) => void) | null>(null);

  if (!url) return <Notice>{t('artifacts.previewMissingPath')}</Notice>;
  if (kind === 'unsupported') return <Notice>{t('artifacts.previewUnsupported')}</Notice>;

  const selectionEnabled = supportsPreviewSelection(kind);
  // HTML iframe uses sandbox="" without allow-same-origin; parent cannot read selection (Task 5 limitation).
  const htmlSelectionUnsupported = kind === 'html';

  let body: ReactNode;
  switch (kind) {
    case 'markdown':
    case 'text':
    case 'code':
    case 'json':
    case 'jsonl':
      body = (
        <TextPreviewSurface
          artifact={artifact}
          kind={kind}
          editing={editing}
          onDirtyChange={onDirtyChange}
          saveRequestId={saveRequestId}
          onSaveResult={onSaveResult}
          onRegisterStyleHandler={setTextStyleHandler}
        />
      );
      break;
    case 'html':
      body = (
        <iframe
          title={artifact.name}
          src={url}
          sandbox=""
          className="block h-full min-h-full w-full border-0 bg-transparent"
          data-testid="artifact-html-preview"
        />
      );
      break;
    case 'image':
      body = (
        <div className="flex h-full min-h-0 w-full items-center justify-center overflow-hidden" data-testid="artifact-image-preview-frame">
          <img src={url} alt={artifact.name} className="h-full w-full object-contain" data-testid="artifact-image-preview" />
        </div>
      );
      break;
    case 'video':
      body = (
        <div className="flex h-full min-h-0 w-full items-center justify-center overflow-hidden" data-testid="artifact-video-preview-frame">
          <video
            src={url}
            controls
            preload="metadata"
            className="block h-full min-h-0 w-full object-contain"
            data-testid="artifact-video-preview"
          />
        </div>
      );
      break;
    case 'pdf':
      body = <iframe title={artifact.name} src={url} className="block h-full min-h-full w-full border-0 bg-transparent" data-testid="artifact-pdf-preview" />;
      break;
    case 'docx':
      body = <DocxPreview url={url} title={artifact.name} />;
      break;
    case 'spreadsheet':
      body = <SpreadsheetPreview url={url} title={artifact.name} size={artifact.size} />;
      break;
    case 'presentation':
      body = (
        <PresentationPreview
          artifactId={artifact.id}
          url={url}
          title={artifact.name}
          size={artifact.size}
          onStructureInvalidChange={onPresentationStructureInvalidChange}
        />
      );
      break;
  }

  return (
    <div ref={panelRef} className="relative h-full min-h-0 w-full overflow-hidden" data-testid="artifact-preview-selection-root">
      {body}
      {selectionEnabled && !htmlSelectionUnsupported ? (
        <SelectionFloat
          kind={kind}
          path={artifact.path ?? ''}
          title={artifact.name}
          panelRef={panelRef}
          editing={editing}
          onStyleAction={textStyleHandler ?? undefined}
        />
      ) : null}
    </div>
  );
}
