import { useEffect, useState } from 'react';
import { AlertCircle, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { CodePreview } from './CodePreview';
import { DocxPreview } from './DocxPreview';
import { PresentationPreview } from './PresentationPreview';
import { SpreadsheetPreview } from './SpreadsheetPreview';
import {
  HTML_PREVIEW_SANDBOX,
  MAX_TEXT_PREVIEW_BYTES,
  artifactBinaryPreviewUrl,
  artifactTextPreviewUrl,
  formatArtifactPreviewText,
  previewKind,
  readLimitedTextPreview,
  type PreviewKind,
} from './filePreviewModel';

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

const TEXT_PREVIEW_LIMIT_LABEL = '1 MiB';

function TruncationBanner({ children }: { children: string }) {
  return (
    <div
      className="mb-2 flex shrink-0 items-center gap-2 rounded-md border border-border bg-secondary px-3 py-2 text-sm text-text-muted"
      data-testid="artifact-text-preview-limit"
      data-variant="truncated"
    >
      <AlertCircle size={15} />
      {children}
    </div>
  );
}

function TextPreview({ artifact, kind }: { artifact: PreviewArtifact; kind: TextKind }) {
  const { t } = useTranslation();
  const [state, setState] = useState<{ content: string; truncated: boolean; error: boolean; loading: boolean }>({
    content: '',
    truncated: false,
    error: false,
    loading: true,
  });
  useEffect(() => {
    const url = artifactTextPreviewUrl(artifact, window.location.origin);
    if (!url) {
      setState({ content: '', truncated: false, error: true, loading: false });
      return;
    }
    const abortController = new AbortController();
    let cancelled = false;
    setState({ content: '', truncated: false, error: false, loading: true });
    void fetch(url, { cache: 'no-store', signal: abortController.signal })
      .then(async response => {
        const contentType = (response.headers.get('content-type') ?? '').toLowerCase();
        if (!response.ok || contentType.includes('text/html')) throw new Error('read_failed');
        return readLimitedTextPreview(response, MAX_TEXT_PREVIEW_BYTES);
      })
      .then(result => {
        if (!cancelled) setState({ content: result.text, truncated: result.truncated, error: false, loading: false });
      })
      .catch(error => {
        if (cancelled || abortController.signal.aborted) return;
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setState({ content: '', truncated: false, error: true, loading: false });
      });
    return () => {
      cancelled = true;
      abortController.abort();
    };
  }, [artifact.downloadUrl, artifact.path]);
  if (state.loading)
    return (
      <div className="flex min-h-[240px] items-center justify-center gap-2 text-sm text-text-muted" data-testid="artifact-text-preview-status" data-variant="loading">
        <LoaderCircle className="animate-spin" size={16} />
        {t('common.loading')}
      </div>
    );
  if (state.error) return <Notice>{t('artifacts.previewFailed')}</Notice>;
  if ((kind === 'json' || kind === 'jsonl') && state.truncated) {
    return (
      <div data-testid="artifact-text-preview-limit" data-variant="too-large">
        <Notice>{t('artifacts.textPreviewTooLarge', { size: TEXT_PREVIEW_LIMIT_LABEL })}</Notice>
      </div>
    );
  }

  const banner = state.truncated ? (
    <TruncationBanner>{t('artifacts.textPreviewTruncated', { size: TEXT_PREVIEW_LIMIT_LABEL })}</TruncationBanner>
  ) : null;
  let body;
  if (kind === 'markdown' && !state.truncated) {
    body = <MarkdownRenderer content={state.content} className="chat-text chat-markdown h-full max-w-none overflow-auto" testId="artifact-markdown-preview" />;
  } else if (kind === 'code') {
    body = <CodePreview content={state.content} name={artifact.name} mimeType={artifact.mimeType} />;
  } else if (kind === 'json' || kind === 'jsonl') {
    const pretty = formatArtifactPreviewText(kind, state.content);
    if (pretty === null) return <Notice>{t('artifacts.invalidJson')}</Notice>;
    body = (
      <pre
        className="m-0 h-full w-full max-w-full overflow-auto whitespace-pre-wrap bg-transparent text-xs text-text [overflow-wrap:anywhere]"
        data-testid="artifact-json-preview"
      >
        {pretty}
      </pre>
    );
  } else {
    body = (
      <pre
        className="m-0 h-full w-full max-w-full overflow-auto whitespace-pre-wrap bg-transparent text-xs text-text [overflow-wrap:anywhere]"
        data-testid="artifact-text-preview"
      >
        {state.content}
      </pre>
    );
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      {banner}
      <div className="min-h-0 flex-1 overflow-hidden">{body}</div>
    </div>
  );
}

export function FilePreview({
  artifact,
  onPresentationStructureInvalidChange,
}: {
  artifact: PreviewArtifact;
  onPresentationStructureInvalidChange?: (artifactId: string, invalid: boolean) => void;
}) {
  const { t } = useTranslation();
  const kind = previewKind(artifact);
  const url = artifactBinaryPreviewUrl(artifact, window.location.origin);
  if (!url) return <Notice>{t('artifacts.previewMissingPath')}</Notice>;
  if (kind === 'unsupported') return <Notice>{t('artifacts.previewUnsupported')}</Notice>;

  switch (kind) {
    case 'markdown':
    case 'text':
    case 'code':
    case 'json':
    case 'jsonl':
      return <TextPreview artifact={artifact} kind={kind} />;
    case 'html':
      return (
        <iframe
          title={artifact.name}
          src={url}
          sandbox={HTML_PREVIEW_SANDBOX}
          className="block h-full min-h-full w-full border-0 bg-transparent"
          data-testid="artifact-html-preview"
        />
      );
    case 'image':
      return (
        <div className="flex h-full min-h-0 w-full items-center justify-center overflow-hidden" data-testid="artifact-image-preview-frame">
          <img src={url} alt={artifact.name} className="h-full w-full object-contain" data-testid="artifact-image-preview" />
        </div>
      );
    case 'video':
      return (
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
    case 'pdf':
      return <iframe title={artifact.name} src={url} className="block h-full min-h-full w-full border-0 bg-transparent" data-testid="artifact-pdf-preview" />;
    case 'docx':
      return <DocxPreview url={url} title={artifact.name} />;
    case 'spreadsheet':
      return <SpreadsheetPreview url={url} title={artifact.name} size={artifact.size} />;
    case 'presentation':
      return (
        <PresentationPreview
          artifactId={artifact.id}
          url={url}
          title={artifact.name}
          size={artifact.size}
          onStructureInvalidChange={onPresentationStructureInvalidChange}
        />
      );
  }
}
