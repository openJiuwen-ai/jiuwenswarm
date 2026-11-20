import { useEffect, useState, type CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import { CodePreview } from '../../../../../components/ArtifactsPanel/CodePreview';
import { previewKind } from '../../../../../components/ArtifactsPanel/filePreviewModel';
import { MAX_FILE_TEXT_PREVIEW_BYTES, type WebUIFileContent, type WebUIFileReadFailure } from '../../fileResources.ts';
import './FilePreview.css';

export interface FilePreviewProps {
  file: WebUIFileContent | null;
  status?: 'ready' | WebUIFileReadFailure;
  emptyText?: string;
  className?: string;
  style?: CSSProperties;
}

type PreviewStatus = 'loading' | 'ready' | WebUIFileReadFailure;
type TextResult = { status: PreviewStatus; content?: string };

async function readTextPreview(url: string, signal: AbortSignal): Promise<TextResult> {
  const response = await fetch(url, { signal });
  if (!response.ok) {
    return { status: response.status === 404 ? 'missing' : response.status === 403 ? 'forbidden' : 'error' };
  }
  if (!response.body) return { status: 'error' };
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_FILE_TEXT_PREVIEW_BYTES) {
        await reader.cancel();
        return { status: 'too_large' };
      }
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel();
    throw error;
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  if (bytes.includes(0)) return { status: 'unsupported' };
  try {
    return { status: 'ready', content: new TextDecoder('utf-8', { fatal: true }).decode(bytes) };
  } catch {
    return { status: 'unsupported' };
  }
}

function PreviewNotice({ status }: { status: PreviewStatus }) {
  const { t } = useTranslation();
  return <div className="webui-file-preview__notice" data-testid="webui-file-preview-state"
    data-variant={status}>{t(`webuiFiles.preview.${status}`)}</div>;
}

async function checkPdfPreview(downloadUrl: string, signal: AbortSignal): Promise<{ status: PreviewStatus; url?: string }> {
  const url = new URL(downloadUrl, window.location.href);
  if (url.origin !== window.location.origin || url.pathname !== '/file-api/download' || !url.searchParams.get('token')) {
    return { status: 'forbidden' };
  }
  url.searchParams.set('inline', '1');
  const response = await fetch(url.href, { signal, headers: { Range: 'bytes=0-4' }, redirect: 'error' });
  if (!response.ok) {
    return { status: response.status === 404 ? 'missing' : response.status === 403 ? 'forbidden' : 'error' };
  }
  const reader = response.body?.getReader();
  if (!reader) return { status: 'error' };
  const signature = new Uint8Array(5);
  let size = 0;
  try {
    if (response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/pdf') {
      return { status: 'unsupported' };
    }
    while (size < signature.length) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value.subarray(0, signature.length - size);
      signature.set(chunk, size);
      size += chunk.length;
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
  return size === 5 && [37, 80, 68, 70, 45].every((byte, index) => signature[index] === byte)
    ? { status: 'ready', url: url.href } : { status: 'unsupported' };
}

function PdfPreview({ downloadUrl, title }: { downloadUrl: string; title: string }) {
  const [result, setResult] = useState<{ status: PreviewStatus; url?: string }>({ status: 'loading' });
  useEffect(() => {
    const controller = new AbortController();
    checkPdfPreview(downloadUrl, controller.signal).then((value) => {
      if (!controller.signal.aborted) setResult(value);
    }).catch(() => {
      if (!controller.signal.aborted) setResult({ status: 'error' });
    });
    return () => controller.abort();
  }, [downloadUrl]);
  if (result.status !== 'ready' || !result.url) return <PreviewNotice status={result.status} />;
  // The host download endpoint must confirm PDF content before unsandboxed navigation.
  return <iframe src={result.url} title={title} className="webui-file-preview__pdf" referrerPolicy="no-referrer"
    data-testid="webui-file-preview-pdf" onError={() => setResult({ status: 'error' })} />;
}

function PreviewBody({ file }: { file: WebUIFileContent }) {
  const kind = previewKind(file);
  const isText = ['markdown', 'text', 'code', 'json', 'jsonl', 'html'].includes(kind);
  const [result, setResult] = useState<TextResult>({ status: 'loading' });
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!isText || file.content !== undefined || !file.downloadUrl) return;
    const controller = new AbortController();
    readTextPreview(file.downloadUrl, controller.signal).then((value) => {
      if (!controller.signal.aborted) setResult(value);
    }).catch(() => {
      if (!controller.signal.aborted) setResult({ status: 'error' });
    });
    return () => controller.abort();
  }, [isText, file.content, file.downloadUrl]);

  if (failed) return <PreviewNotice status="error" />;
  if (kind === 'pdf' && file.downloadUrl) return <PdfPreview downloadUrl={file.downloadUrl} title={file.name} />;
  if (kind === 'image' && file.downloadUrl) {
    const url = new URL(file.downloadUrl, typeof window === 'undefined' ? 'https://webui.invalid/' : window.location.href);
    url.searchParams.set('inline', '1');
    return <img src={url.href} alt={file.name} className="webui-file-preview__image"
      data-testid="webui-file-preview-image" onError={() => setFailed(true)} />;
  }
  if (!isText) return <PreviewNotice status="unsupported" />;
  if (file.content === undefined && result.status !== 'ready') return <PreviewNotice status={result.status} />;
  const content = file.content ?? result.content ?? '';
  return kind === 'code' ? <CodePreview content={content} name={file.name} mimeType={file.mimeType} /> :
    <pre className="m-0 h-full w-full overflow-auto whitespace-pre text-xs text-text"
      data-testid="webui-file-preview-text">{content}</pre>;
}

export function FilePreview({ file, status: sourceStatus = 'ready', emptyText, className, style }: FilePreviewProps) {
  const { t } = useTranslation();
  const status = sourceStatus !== 'ready' ? sourceStatus : !file ? 'empty' : 'ready';
  return (
    <section className={`webui-file-preview${className ? ` ${className}` : ''}`} data-testid="webui-file-preview"
      style={style}>
      <header className="webui-file-preview__header" title={file?.path}
        data-testid="webui-file-preview-title">{file?.name ?? t('webuiFiles.previewTitle')}</header>
      <div className="webui-file-preview__body" data-testid="webui-file-preview-body">
        {status !== 'ready' ? <div className="webui-file-preview__notice" data-testid="webui-file-preview-state"
          data-variant={status}>{status === 'empty' ? emptyText ?? t('webuiFiles.preview.empty')
            : t(`webuiFiles.preview.${status}`)}</div> :
          file ? <PreviewBody key={`${file.path}:${file.downloadUrl ?? ''}:${file.mimeType ?? ''}:${file.content !== undefined}`}
            file={file} /> : null}
      </div>
    </section>
  );
}
