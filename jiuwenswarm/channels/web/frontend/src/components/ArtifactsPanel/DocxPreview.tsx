import { useEffect, useRef, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

type DocxPreviewState = 'loading' | 'ready' | 'error';

export function DocxPreview({ url, title }: { url: string; title: string }) {
  const { t } = useTranslation();
  const bodyRef = useRef<HTMLDivElement>(null);
  const styleRef = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<DocxPreviewState>('loading');

  useEffect(() => {
    const body = bodyRef.current;
    const styleHost = styleRef.current;
    if (!body || !styleHost) return;

    const abortController = new AbortController();
    let cancelled = false;
    body.replaceChildren();
    styleHost.replaceChildren();
    setState('loading');

    const handleLinkClick = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) return;
      const anchor = event.target.closest('a[href]');
      if (!anchor) return;
      event.preventDefault();
      const href = anchor.getAttribute('href');
      if (!href) return;

      if (href.startsWith('#')) {
        const target = body.querySelector(`[id="${CSS.escape(href.slice(1))}"]`);
        target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      if (/^(https?:|mailto:|tel:)/i.test(href)) {
        window.open(href, '_blank', 'noopener,noreferrer');
      }
    };
    body.addEventListener('click', handleLinkClick);

    void fetch(url, { cache: 'no-store', signal: abortController.signal })
      .then(async response => {
        const contentType = (response.headers.get('content-type') ?? '').toLowerCase();
        if (!response.ok || contentType.includes('text/html')) {
          throw new Error(`DOCX request failed with HTTP ${response.status}`);
        }
        return response.arrayBuffer();
      })
      .then(async content => {
        if (cancelled) return;
        const { renderAsync } = await import('docx-preview');
        if (cancelled) return;
        await renderAsync(content, body, styleHost, {
          className: 'docx-artifact-page',
          inWrapper: true,
          breakPages: true,
          ignoreHeight: false,
          ignoreWidth: false,
          renderHeaders: true,
          renderFooters: true,
          renderFootnotes: true,
          renderEndnotes: true,
          useBase64URL: true,
        });
        if (!cancelled) setState('ready');
      })
      .catch(error => {
        if (abortController.signal.aborted || cancelled) return;
        console.error('Failed to render DOCX preview', error);
        setState('error');
      });

    return () => {
      cancelled = true;
      abortController.abort();
      body.removeEventListener('click', handleLinkClick);
      body.replaceChildren();
      styleHost.replaceChildren();
    };
  }, [url]);

  return (
    <div className="relative flex h-full min-h-0 w-full flex-col" aria-label={title} data-testid="artifact-docx-preview">
      <div ref={styleRef} className="shrink-0" />
      <div ref={bodyRef} className="min-h-0 flex-1 overflow-auto bg-secondary" />
      {state === 'loading' && (
        <div className="absolute inset-0 flex items-center justify-center gap-2 bg-secondary text-sm text-text-muted" data-testid="artifact-docx-preview-overlay" data-variant="loading">
          <LoaderCircle className="animate-spin" size={16} />
          {t('common.loading')}
        </div>
      )}
      {state === 'error' && (
        <div className="absolute inset-0 flex items-center justify-center bg-secondary p-6 text-sm text-danger" role="alert" data-testid="artifact-docx-preview-overlay" data-variant="error">
          {t('artifacts.docxPreviewFailed')}
        </div>
      )}
    </div>
  );
}
