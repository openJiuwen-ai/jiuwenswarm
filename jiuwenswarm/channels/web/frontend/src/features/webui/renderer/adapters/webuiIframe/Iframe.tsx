import { useEffect, useState, type CSSProperties } from 'react';
import { ExternalLink, LoaderCircle, RotateCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import './Iframe.css';

export interface IframeProps {
  src?: string;
  srcDoc?: string;
  title: string;
  minWidth?: number;
  active?: boolean;
  className?: string;
  style?: CSSProperties;
}

function FrameDocument({ src, srcDoc, title, minWidth }: Pick<IframeProps, 'src' | 'srcDoc' | 'title' | 'minWidth'>) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<'loading' | 'loaded' | 'error'>('loading');
  const [slow, setSlow] = useState(false);
  const [reloadVersion, setReloadVersion] = useState(0);
  const source = src ?? t('webuiIframe.pluginPage');

  useEffect(() => {
    if (status !== 'loading') return;
    const timer = window.setTimeout(() => setSlow(true), 15000);
    return () => window.clearTimeout(timer);
  }, [status, reloadVersion]);

  const reload = () => {
    setStatus('loading');
    setSlow(false);
    setReloadVersion((version) => version + 1);
  };

  return (
    <>
      <div className="webui-iframe__controls" data-testid="webui-iframe-controls">
        <span className="webui-iframe__source" title={source} data-testid="webui-iframe-source">
          {source}
        </span>
        <div className="webui-iframe__actions" data-testid="webui-iframe-actions">
          <button
            type="button"
            className="webui-iframe__action"
            onClick={reload}
            title={t('webuiIframe.reload')}
            aria-label={t('webuiIframe.reload')}
            data-testid="webui-iframe-reload"
          >
            <RotateCw size={16} aria-hidden="true" />
          </button>
          {src ? (
            <a
              href={src}
              target="_blank"
              rel="noopener noreferrer"
              className="webui-iframe__action"
              title={t('webuiIframe.openSource')}
              aria-label={t('webuiIframe.openSource')}
              data-testid="webui-iframe-open-source"
            >
              <ExternalLink size={16} aria-hidden="true" />
            </a>
          ) : null}
        </div>
      </div>
      <div className="webui-iframe__content" data-testid="webui-iframe-content">
        <div className="webui-iframe__viewport" data-testid="webui-iframe-viewport">
          {status !== 'error' ? (
            <iframe
              key={reloadVersion}
              src={src}
              srcDoc={srcDoc}
              title={title}
              sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads"
              referrerPolicy="no-referrer"
              className="webui-iframe__frame"
              style={{ minWidth }}
              data-testid="webui-iframe-frame"
              data-variant={status}
              aria-busy={status === 'loading'}
              onLoad={() => setStatus('loaded')}
              onErrorCapture={() => setStatus('error')}
            />
          ) : null}
        </div>
        {status !== 'loaded' ? (
          <div
            className="webui-iframe__notice"
            role="status"
            data-testid="webui-iframe-state"
            data-variant={status === 'loading' && slow ? 'slow' : status}
          >
            {status === 'loading' ? <LoaderCircle size={16} className="animate-spin shrink-0" aria-hidden="true" /> : null}
            {t(`webuiIframe.${status === 'loading' && slow ? 'slow' : status}`)}
          </div>
        ) : null}
      </div>
    </>
  );
}

/** Own the browsing context only while this content is active. */
export function Iframe({ src, srcDoc, title, minWidth = 0, active = true, className, style }: IframeProps) {
  const { t } = useTranslation();
  return (
    <section
      className={`webui-iframe${className ? ` ${className}` : ''}`}
      style={style}
      aria-label={title}
      data-testid="webui-iframe"
    >
      {active ? (
        <FrameDocument key={src ?? srcDoc} src={src} srcDoc={srcDoc} title={title} minWidth={minWidth} />
      ) : (
        <div className="webui-iframe__notice" role="status" data-testid="webui-iframe-state" data-variant="inactive">
          {t('webuiIframe.inactive')}
        </div>
      )}
    </section>
  );
}
