import { useEffect, useState, type CSSProperties } from 'react';
import { ExternalLink, LoaderCircle, RotateCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import './Iframe.css';

export interface IframeProps {
  src: string;
  title: string;
  active?: boolean;
  className?: string;
  style?: CSSProperties;
}

function FrameDocument({ src, title }: Pick<IframeProps, 'src' | 'title'>) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<'loading' | 'loaded' | 'error'>('loading');
  const [slow, setSlow] = useState(false);
  const [desktop, setDesktop] = useState(false);
  const [reloadVersion, setReloadVersion] = useState(0);

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
        <div className="webui-iframe__modes" role="group" aria-label={t('webuiIframe.displayMode')}
          data-testid="webui-iframe-modes">
          <button type="button" className="webui-iframe__mode" aria-pressed={!desktop}
            data-testid="webui-iframe-fit" onClick={() => setDesktop(false)}>
            {t('webuiIframe.fit')}
          </button>
          <button type="button" className="webui-iframe__mode" aria-pressed={desktop}
            title={t('webuiIframe.desktopHint')} data-testid="webui-iframe-desktop" onClick={() => setDesktop(true)}>
            {t('webuiIframe.desktop')}
          </button>
        </div>
        <div className="webui-iframe__actions" data-testid="webui-iframe-actions">
          <button type="button" className="webui-iframe__action" onClick={reload}
            title={t('webuiIframe.reload')} aria-label={t('webuiIframe.reload')} data-testid="webui-iframe-reload">
            <RotateCw size={16} aria-hidden="true" />
          </button>
          <a href={src} target="_blank" rel="noopener noreferrer" className="webui-iframe__action"
            title={t('webuiIframe.openSource')} aria-label={t('webuiIframe.openSource')}
            data-testid="webui-iframe-open-source">
            <ExternalLink size={16} aria-hidden="true" />
          </a>
        </div>
      </div>
      <div className="webui-iframe__content" data-testid="webui-iframe-content">
        <div className="webui-iframe__viewport" data-testid="webui-iframe-viewport"
          data-variant={desktop ? 'desktop' : 'fit'}>
          {status !== 'error' ? (
            <iframe
              key={reloadVersion}
              src={src}
              title={title}
              sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads"
              referrerPolicy="no-referrer"
              className="webui-iframe__frame"
              data-testid="webui-iframe-frame"
              data-variant={status}
              aria-busy={status === 'loading'}
              onLoad={() => setStatus('loaded')}
              onErrorCapture={() => setStatus('error')}
            />
          ) : null}
        </div>
        {status !== 'loaded' ? (
          <div className="webui-iframe__notice" role="status" data-testid="webui-iframe-state"
            data-variant={status === 'loading' && slow ? 'slow' : status}>
            {status === 'loading' ? <LoaderCircle size={16} className="animate-spin shrink-0" aria-hidden="true" /> : null}
            {t(`webuiIframe.${status === 'loading' && slow ? 'slow' : status}`)}
          </div>
        ) : null}
      </div>
    </>
  );
}

/** Own the browsing context only while this content is active. */
export function Iframe({ src, title, active = true, className, style }: IframeProps) {
  const { t } = useTranslation();
  return (
    <section className={`webui-iframe${className ? ` ${className}` : ''}`} style={style}
      aria-label={title} data-testid="webui-iframe">
      {active ? <FrameDocument key={src} src={src} title={title} /> : (
        <div className="webui-iframe__notice" role="status" data-testid="webui-iframe-state" data-variant="inactive">
          {t('webuiIframe.inactive')}
        </div>
      )}
    </section>
  );
}
