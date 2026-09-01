import { useEffect, useState } from 'react';
import { AlertCircle, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { CodePreview } from './CodePreview';
import { DocxPreview } from './DocxPreview';
import { PresentationPreview } from './PresentationPreview';
import { SpreadsheetPreview } from './SpreadsheetPreview';
import { artifactBinaryPreviewUrl, artifactTextPreviewUrl, previewKind, type PreviewKind } from './filePreviewModel';

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

function TextPreview({ artifact, kind }: { artifact: PreviewArtifact; kind: TextKind }) {
  const { t } = useTranslation();
  const [state, setState] = useState<{ content: string; error: boolean; loading: boolean }>({ content: '', error: false, loading: true });
  useEffect(() => {
    const url = artifactTextPreviewUrl(artifact, window.location.origin);
    if (!url) {
      setState({ content: '', error: true, loading: false });
      return;
    }
    let cancelled = false;
    setState({ content: '', error: false, loading: true });
    void fetch(url, { cache: 'no-store' })
      .then(async response => {
        const contentType = (response.headers.get('content-type') ?? '').toLowerCase();
        if (!response.ok || contentType.includes('text/html')) throw new Error('read_failed');
        return response.text();
      })
      .then(content => {
        if (!cancelled) setState({ content, error: false, loading: false });
      })
      .catch(() => {
        if (!cancelled) setState({ content: '', error: true, loading: false });
      });
    return () => {
      cancelled = true;
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
  if (kind === 'markdown')
    return <MarkdownRenderer content={state.content} className="chat-text chat-markdown h-full max-w-none overflow-auto" testId="artifact-markdown-preview" />;
  if (kind === 'code') return <CodePreview content={state.content} name={artifact.name} mimeType={artifact.mimeType} />;
  if (kind === 'json' || kind === 'jsonl') {
    try {
      const value =
        kind === 'json'
          ? JSON.parse(state.content)
          : state.content
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
      {state.content}
    </pre>
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
          sandbox=""
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
        <div className="flex h-full min-h-0 w-full items-center justify-center overflow-hidden p-2">
          <video
            controls
            preload="metadata"
            className="max-h-full max-w-full"
            data-testid="artifact-video-preview"
          >
            <source src={url} type={artifact.mimeType || 'video/mp4'} />
          </video>
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
