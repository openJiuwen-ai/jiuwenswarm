import { useState, type CSSProperties } from 'react';
import { LoaderCircle } from 'lucide-react';
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
  return (
    <>
      {status !== 'error' ? (
        <iframe
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
      {status !== 'loaded' ? (
        <div className="webui-iframe__notice" role="status" data-testid="webui-iframe-state" data-variant={status}>
          {status === 'loading' ? <LoaderCircle size={16} className="animate-spin" /> : null}
          {t(`webuiIframe.${status}`)}
        </div>
      ) : null}
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
