import { useEffect, useMemo, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../../../components/MarkdownRenderer';
import { latexToMarkdown } from '../latexPreview';

interface RsiLatexPreviewProps {
  url: string;
}

type LatexViewMode = 'preview' | 'source';

export function RsiLatexPreview({ url }: RsiLatexPreviewProps) {
  const { t } = useTranslation();
  const [content, setContent] = useState('');
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [mode, setMode] = useState<LatexViewMode>('preview');

  useEffect(() => {
    let cancelled = false;
    setState('loading');
    setMode('preview');

    void fetch(url, { cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const text = await response.text();
        if (!cancelled) {
          setContent(text);
          setState('ready');
        }
      })
      .catch(() => {
        if (!cancelled) setState('error');
      });

    return () => {
      cancelled = true;
    };
  }, [url]);

  const markdown = useMemo(() => latexToMarkdown(content), [content]);

  if (state === 'loading') {
    return (
      <div className="rsi-latex-preview__status">
        <LoaderCircle className="animate-spin" size={16} />
        <span>{t('common.loading')}</span>
      </div>
    );
  }

  if (state === 'error') {
    return <div className="rsi-latex-preview__status">{t('rsi.artifact.loadFailed')}</div>;
  }

  return (
    <div className="rsi-latex-preview">
      <div className="rsi-latex-preview__toolbar">
        <button
          type="button"
          className={`rsi-latex-preview__tab${mode === 'preview' ? ' rsi-latex-preview__tab--active' : ''}`}
          onClick={() => setMode('preview')}
        >
          {t('rsi.artifact.latexPreview')}
        </button>
        <button
          type="button"
          className={`rsi-latex-preview__tab${mode === 'source' ? ' rsi-latex-preview__tab--active' : ''}`}
          onClick={() => setMode('source')}
        >
          {t('rsi.artifact.latexSource')}
        </button>
      </div>
      {mode === 'preview' ? (
        <MarkdownRenderer
          content={markdown}
          className="rsi-latex-preview__markdown chat-text chat-markdown"
          testId="rsi-latex-rendered-preview"
        />
      ) : (
        <pre className="rsi-latex-preview__source" data-testid="rsi-latex-source-preview">
          {content}
        </pre>
      )}
    </div>
  );
}
