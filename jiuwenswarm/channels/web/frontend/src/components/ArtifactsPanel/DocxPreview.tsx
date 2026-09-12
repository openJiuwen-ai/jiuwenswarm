import { useEffect, useRef, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

type DocxPreviewState = 'loading' | 'ready' | 'error';

const DOCX_PAGE_CLASS = 'docx-artifact-page';

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
          className: DOCX_PAGE_CLASS,
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
        // docx-preview 注入的默认样式：wrapper 带 30px padding、灰色背景与 align-items: center，页面带 box-shadow。
        // wrapper 是块级元素，宽度被限制为容器宽度，会带来两个布局问题（均与默认视觉样式无关，予以保留）：
        // 1) 页面是固定像素宽度（如 A4 约 794px），容器更窄时 align-items: center 让页面左右对称溢出，
        //    左侧溢出部分 scrollLeft 永远滚不到而被持续裁剪；safe center 在溢出时回退为起始对齐，
        //    保证全部内容都能横向滚动到达（不支持的浏览器忽略此赋值，维持库默认行为）。
        // 2) 可滚动区域比 wrapper 宽时，padding-right 与灰色背景都到不了滚动区域右缘，
        //    造成“左有 padding 右没有”、底部灰色背景铺不满；min-width: max-content 让 wrapper
        //    完整覆盖内容宽度（页面宽 + 两侧 padding），右侧 padding 与背景随之铺满整个滚动区域。
        const wrapper = body.querySelector<HTMLElement>(`.${DOCX_PAGE_CLASS}-wrapper`);
        if (wrapper) {
          wrapper.style.alignItems = 'safe center';
          wrapper.style.minWidth = 'max-content';
        }
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
