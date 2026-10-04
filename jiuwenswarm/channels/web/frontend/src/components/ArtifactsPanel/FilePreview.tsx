import { useCallback, useEffect, useRef, useState, type MutableRefObject, type ReactNode } from 'react';
import { AlertCircle, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { CodePreview } from './CodePreview';
import { DocxPreview } from './DocxPreview';
import { PresentationPreview } from './PresentationPreview';
import {
  SelectionFloat,
  type SelectionStyleAction,
  type StyleActionHandler,
  type StyleProbeHandler,
} from './SelectionFloat';
import { SpreadsheetPreview } from './SpreadsheetPreview';
import {
  artifactBinaryPreviewUrl,
  artifactTextPreviewUrl,
  previewKind,
  toWritableFileApiPath,
  type PreviewKind,
} from './filePreviewModel';
import { isPreviewStyleEditable, supportsPreviewSelection } from './previewSelection';
import { hasStyleAtOccurrence, stripHtmlScripts, toggleStyleAtOccurrence } from './previewTextEdit';

export type PreviewArtifact = {
  id: string;
  name: string;
  mimeType?: string;
  downloadUrl?: string;
  downloadToken?: string;
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

async function persistTextContent(
  path: string,
  content: string,
  downloadToken?: string,
): Promise<'ok' | 'forbidden' | 'error'> {
  try {
    const response = await fetch('/file-api/file-content', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        path,
        content,
        ...(downloadToken ? { download_token: downloadToken } : {}),
      }),
    });
    if (response.ok) return 'ok';
    const errorText = await response.text();
    if (response.status === 403 || errorText.includes('forbidden_path')) return 'forbidden';
    return 'error';
  } catch {
    return 'error';
  }
}

function TextPreviewSurface({
  artifact,
  kind,
  onRegisterStyleHandler,
  onRegisterStyleProbe,
}: {
  artifact: PreviewArtifact;
  kind: TextKind;
  onRegisterStyleHandler: (handler: StyleActionHandler | null) => void;
  onRegisterStyleProbe: (handler: StyleProbeHandler | null) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState('');
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const saveSeqRef = useRef(0);

  useEffect(() => {
    const url = artifactTextPreviewUrl(artifact, window.location.origin);
    if (!url) {
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
        setDraft(content);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setDraft('');
        setError(true);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [artifact.downloadUrl, artifact.path]);

  useEffect(() => {
    if (!isPreviewStyleEditable(kind)) {
      onRegisterStyleHandler(null);
      onRegisterStyleProbe(null);
      return;
    }
    onRegisterStyleProbe((action, selectedText, occurrenceIndex) =>
      hasStyleAtOccurrence(draftRef.current, selectedText, occurrenceIndex, action as SelectionStyleAction),
    );
    onRegisterStyleHandler((action, selectedText, occurrenceIndex, options) => {
      const result = toggleStyleAtOccurrence(
        draftRef.current,
        selectedText,
        occurrenceIndex,
        action as SelectionStyleAction,
        options?.linkUrl ?? 'https://',
        options?.codeLanguage ?? '',
      );
      if (!result) return null;
      draftRef.current = result.value;
      setDraft(result.value);

      // Persist markdown to disk (path allow-list or matching download token).
      const looksMarkdown = /\.mdx?$/i.test(artifact.path || '') || /\.mdx?$/i.test(artifact.name || '');
      if (looksMarkdown) {
        const writablePath = toWritableFileApiPath(artifact.path);
        const downloadToken =
          artifact.downloadToken?.trim() ||
          (() => {
            try {
              if (!artifact.downloadUrl) return undefined;
              return new URL(artifact.downloadUrl, window.location.origin).searchParams.get('token') || undefined;
            } catch {
              return undefined;
            }
          })();
        if (!writablePath) {
          window.alert(t('artifacts.saveFailed'));
        } else {
          const seq = ++saveSeqRef.current;
          void persistTextContent(writablePath, result.value, downloadToken).then(status => {
            if (seq !== saveSeqRef.current) return;
            if (status !== 'ok') {
              window.alert(t('artifacts.saveFailed'));
            }
          });
        }
      }
      return result.innerText;
    });
    return () => {
      onRegisterStyleHandler(null);
      onRegisterStyleProbe(null);
    };
  }, [
    artifact.downloadToken,
    artifact.downloadUrl,
    artifact.name,
    artifact.path,
    kind,
    onRegisterStyleHandler,
    onRegisterStyleProbe,
    t,
  ]);

  if (loading)
    return (
      <div className="flex min-h-[240px] items-center justify-center gap-2 text-sm text-text-muted" data-testid="artifact-text-preview-status" data-variant="loading">
        <LoaderCircle className="animate-spin" size={16} />
        {t('common.loading')}
      </div>
    );
  if (error) return <Notice>{t('artifacts.previewFailed')}</Notice>;

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
}: {
  artifact: PreviewArtifact;
  onPresentationStructureInvalidChange?: (artifactId: string, invalid: boolean) => void;
}) {
  const { t } = useTranslation();
  const kind = previewKind(artifact);
  const url = artifactBinaryPreviewUrl(artifact, window.location.origin);
  const panelRef = useRef<HTMLDivElement>(null);
  const htmlIframeRef = useRef<HTMLIFrameElement>(null);
  const [textStyleHandler, setTextStyleHandler] = useState<StyleActionHandler | null>(null);
  const [textStyleProbe, setTextStyleProbe] = useState<StyleProbeHandler | null>(null);
  // React treats setState(fn) as a functional updater — wrap so the handler itself is stored.
  const registerStyleHandler = useCallback((handler: StyleActionHandler | null) => {
    setTextStyleHandler(() => handler);
  }, []);
  const registerStyleProbe = useCallback((handler: StyleProbeHandler | null) => {
    setTextStyleProbe(() => handler);
  }, []);

  if (!url) return <Notice>{t('artifacts.previewMissingPath')}</Notice>;
  if (kind === 'unsupported') return <Notice>{t('artifacts.previewUnsupported')}</Notice>;

  const selectionEnabled = supportsPreviewSelection(kind);

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
          onRegisterStyleHandler={registerStyleHandler}
          onRegisterStyleProbe={registerStyleProbe}
        />
      );
      break;
    case 'html':
      body = <HtmlPreview artifact={artifact} title={artifact.name} iframeRef={htmlIframeRef} />;
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
      {selectionEnabled ? (
        <SelectionFloat
          kind={kind}
          path={artifact.path ?? ''}
          title={artifact.name ?? ''}
          panelRef={panelRef}
          iframeRef={kind === 'html' ? htmlIframeRef : undefined}
          onStyleAction={textStyleHandler ?? undefined}
          onStyleProbe={textStyleProbe ?? undefined}
        />
      ) : null}
    </div>
  );
}

function HtmlPreview({
  artifact,
  title,
  iframeRef,
}: {
  artifact: PreviewArtifact;
  title: string;
  iframeRef: MutableRefObject<HTMLIFrameElement | null>;
}) {
  const { t } = useTranslation();
  const [srcDoc, setSrcDoc] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const url = artifactBinaryPreviewUrl(artifact, window.location.origin) || artifactTextPreviewUrl(artifact, window.location.origin);
    if (!url) {
      setError(true);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(false);
    void fetch(url, { cache: 'no-store' })
      .then(response => {
        if (!response.ok) throw new Error('read_failed');
        return response.text();
      })
      .then(html => {
        if (cancelled) return;
        setSrcDoc(stripHtmlScripts(html));
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setError(true);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [artifact.downloadUrl, artifact.path]);

  if (loading) {
    return (
      <div className="flex min-h-[240px] items-center justify-center gap-2 text-sm text-text-muted" data-testid="artifact-html-preview-status" data-variant="loading">
        <LoaderCircle className="animate-spin" size={16} />
        {t('common.loading')}
      </div>
    );
  }
  if (error || srcDoc == null) return <Notice>{t('artifacts.previewFailed')}</Notice>;

  return (
    <iframe
      ref={node => {
        iframeRef.current = node;
      }}
      title={title}
      srcDoc={srcDoc}
      sandbox="allow-same-origin"
      className="block h-full min-h-full w-full border-0 bg-transparent"
      data-testid="artifact-html-preview"
    />
  );
}
