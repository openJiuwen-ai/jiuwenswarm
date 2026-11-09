import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  parsePresentationRequest, isWindowTransitionOptions, PRESENTATION_PROTOCOL,
  type ApplicationSurfaceConfig, type PresentationState, type PresentationWorkArea, type WindowTransition,
} from './protocol.ts';
import './PresentationWindow.css';

/** No WebUI runtime, server connection or conversation store is mounted in this renderer. */
export function PresentationWindow() {
  const { t } = useTranslation();
  const [initial, setInitial] = useState<ApplicationSurfaceConfig | null>(null);
  const [error, setError] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const current = useRef<{ state: PresentationState; config: Record<string, unknown>; initialWorkArea: PresentationWorkArea } | null>(null);
  const ready = useRef(false);
  const transition = useRef<string | null>(null);
  const api = window.jiuwenPresentation;
  useEffect(() => {
    if (error && api) void api.failed().catch((failure) => console.error('Presentation failure notification failed', failure));
  }, [error, api]);
  const postState = () => {
    if (ready.current && current.current) frame.current?.contentWindow?.postMessage({
      protocol: PRESENTATION_PROTOCOL, version: 1, type: 'state', ...current.current,
    }, '*');
  };
  useEffect(() => {
    if (!api) { setError(true); return; }
    let disposed = false;
    const offUpdate = api.onUpdate((payload) => {
      if (!current.current) return;
      current.current = { ...current.current, ...payload };
      postState();
    });
    const offResult = api.onResult((result) => frame.current?.contentWindow?.postMessage(result, '*'));
    const prepare = (value: WindowTransition) => {
      if (disposed) return;
      transition.current = value.id;
      frame.current?.contentWindow?.postMessage({ protocol: PRESENTATION_PROTOCOL, version: 1,
        type: 'transition', transition: value }, '*');
    };
    const offTransition = api.onTransition(prepare);
    const onMessage = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.data?.protocol !== PRESENTATION_PROTOCOL
        || event.data.version !== 1) return;
      const keys = Object.keys(event.data).sort().join(',');
      if (event.data.type === 'ready' && (keys === 'protocol,type,version'
        || (keys === 'protocol,transition,type,version' && isWindowTransitionOptions(event.data.transition)))) {
        if (ready.current) return;
        ready.current = true;
        postState();
        void api.ready(event.data.transition).then((value) => {
          if (value !== true) prepare(value);
        }).catch(() => { if (!disposed) setError(true); });
        return;
      }
      if (!ready.current) return;
      if (keys === 'id,protocol,type,version' && typeof event.data.id === 'string'
        && event.data.id === transition.current) {
        const id = event.data.id;
        if (event.data.type === 'transition-failed') { setError(true); return; }
        if (event.data.type === 'transition-ready') {
          void api.startTransition(id).then(() => {
            if (!disposed && transition.current === id) frame.current?.contentWindow?.postMessage({
              protocol: PRESENTATION_PROTOCOL, version: 1, type: 'transition-start', id }, '*');
          }).catch(() => { if (!disposed) setError(true); });
          return;
        }
        if (event.data.type === 'transition-finished') {
          void api.finishTransition(id).then(() => {
            if (!disposed && transition.current === id) {
              transition.current = null;
              frame.current?.contentWindow?.postMessage({ protocol: PRESENTATION_PROTOCOL, version: 1,
                type: 'transition-ended', id }, '*');
            }
          }).catch(() => { if (!disposed) setError(true); });
          return;
        }
      }
      const request = parsePresentationRequest(event.data);
      if (request) void api.request(request).catch(() => setError(true));
      else if (event.data.type === 'request' && Number.isSafeInteger(event.data.id) && event.data.id > 0) {
        frame.current?.contentWindow?.postMessage({ protocol: PRESENTATION_PROTOCOL, version: 1, type: 'result',
          id: event.data.id, ok: false, error: { code: 'SCHEMA_ERROR', message: t('webuiPresentation.actionFailed') } }, '*');
      }
    };
    window.addEventListener('message', onMessage);
    void api.bootstrap().then((config) => {
      if (disposed) return;
      current.current = { state: config.state, config: config.config, initialWorkArea: config.initialWorkArea };
      setInitial(config);
    }).catch(() => { if (!disposed) setError(true); });
    return () => { disposed = true; offUpdate(); offResult(); offTransition(); window.removeEventListener('message', onMessage); };
  }, [api]);
  if (error) return <div role="alert" data-testid="webui-presentation-window-error">{t('webuiPresentation.openFailed')}</div>;
  if (!initial) return null;
  return <iframe ref={frame} src={initial.resourceUrl} title={initial.title} sandbox="allow-scripts allow-forms"
    referrerPolicy="no-referrer" className="webui-presentation-frame" data-testid="webui-presentation-frame" />;
}
