import { useEffect, useId, useState, type CSSProperties } from 'react';
import { ExternalLink, LoaderCircle, RotateCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import './Iframe.css';

const PREVIEW_SANDBOX = 'allow-scripts allow-forms allow-popups allow-modals allow-downloads';

function canPreserveSourceOrigin(src: string | undefined): boolean {
  if (
    !src ||
    typeof document === 'undefined' ||
    document.querySelector('meta[name="workswarm-iframe-host-isolation"]')?.getAttribute('content') !== 'true'
  ) {
    return false;
  }
  const url = new URL(src, document.baseURI);
  return (url.protocol === 'http:' || url.protocol === 'https:') && url.origin !== document.location.origin;
}

export interface IframeProps {
  src?: string;
  srcDoc?: string;
  title: string;
  minWidth?: number;
  permissions?: readonly string[];
  active?: boolean;
  className?: string;
  style?: CSSProperties;
}

type FrameDocumentProps = Pick<IframeProps, 'src' | 'srcDoc' | 'title' | 'minWidth'> & { allow?: string };

function FrameDocument({ src, srcDoc, title, minWidth, allow }: FrameDocumentProps) {
  const { t } = useTranslation();
  const frameId = useId();
  const [status, setStatus] = useState<'loading' | 'loaded' | 'error'>('loading');
  const [slow, setSlow] = useState(false);
  const [reloadVersion, setReloadVersion] = useState(0);
  const source = src ?? t('webuiIframe.pluginPage');
  const sandbox = canPreserveSourceOrigin(src) ? `${PREVIEW_SANDBOX} allow-same-origin` : PREVIEW_SANDBOX;
  const needsMediaBinding =
    typeof window !== 'undefined' &&
    window.jiuwenDesktop &&
    allow?.split('; ').some((feature) => feature === 'camera' || feature === 'microphone');

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
              name={needsMediaBinding ? `workswarm-media-${frameId}-${reloadVersion}` : undefined}
              src={src}
              srcDoc={srcDoc}
              title={title}
              sandbox={sandbox}
              allow={allow}
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
export function Iframe({ src, srcDoc, title, minWidth = 0, permissions, active = true, className, style }: IframeProps) {
  const { t } = useTranslation();
  const allow = src && permissions?.length ? Array.from(new Set(permissions)).join('; ') : undefined;
  const documentKey = JSON.stringify([src, srcDoc, allow]);
  return (
    <section
      className={`webui-iframe${className ? ` ${className}` : ''}`}
      style={style}
      aria-label={title}
      data-testid="webui-iframe"
    >
      {active ? (
        <FrameDocument key={documentKey} src={src} srcDoc={srcDoc} title={title} minWidth={minWidth} allow={allow} />
      ) : (
        <div className="webui-iframe__notice" role="status" data-testid="webui-iframe-state" data-variant="inactive">
          {t('webuiIframe.inactive')}
        </div>
      )}
    </section>
  );
}
